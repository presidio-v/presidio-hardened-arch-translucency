"""
Rolling observation store (v0.8.0, Phase 1).

A source-agnostic SQLite store for workload measurements.  Any source -- a
``pat demo`` run, a Prometheus scrape, a load test, or a manual entry -- builds
an :class:`Observation` and calls :func:`record_observation`; ``pat optimize``
reads them back via :func:`load_observations` / :func:`latest_observations`.

The store is intentionally **single-shot** (decision D2): a caller records one
measurement and returns.  Recurring collection is scheduled externally
(cron / launchd / a Kubernetes CronJob), not by a daemon or a foreground loop.

Storage (decision D5 / cross-cutting): the global store lives at
``~/.pat/observations.db``.  The schema matches PRESIDIO-REQ.md exactly:

    timestamp, rps, avg_latency_ms, p99_latency_ms, throughput, layer, replicas

An autoincrement ``id`` is added as the primary key (insertion order, used only
to break ties between identical timestamps), plus a ``source`` column
(``manual`` / ``demo`` / ``prometheus`` / ...) so later phases can tell where a
measurement came from.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Storage location & schema
# ---------------------------------------------------------------------------

_GLOBAL_DB_RELPATH: tuple[str, str] = (".pat", "observations.db")

_TABLE = "observations"

#: Parallel hash-chain table (v0.19.0). Kept separate from ``observations`` so
#: the measurement schema is untouched (what ``pat observe`` records does not
#: change) and pre-chain stores keep working: rows without a chain entry are
#: reported as UNVERIFIABLE legacy, never silently "verified".
_CHAIN_TABLE = "observation_chain"

#: Documented genesis link: the ``prev_hash`` of the first chained observation.
#: A fixed sentinel (not a real content hash) so the head of the chain is
#: distinguishable from a broken/missing link during verification.
GENESIS_PREV_HASH = "0" * 64

_DEFAULT_SOURCE = "manual"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_TABLE} (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp      TEXT    NOT NULL,
    rps            REAL    NOT NULL,
    avg_latency_ms REAL    NOT NULL,
    p99_latency_ms REAL    NOT NULL,
    throughput     REAL    NOT NULL,
    layer          TEXT    NOT NULL,
    replicas       INTEGER NOT NULL,
    source         TEXT    NOT NULL DEFAULT '{_DEFAULT_SOURCE}'
);
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_ts ON {_TABLE} (timestamp);
CREATE INDEX IF NOT EXISTS idx_{_TABLE}_layer_ts ON {_TABLE} (layer, timestamp);
"""

#: The chain is a parallel table keyed by observation ``id`` (one row per
#: chained observation). ``record_hash`` binds the record's content; ``prev_hash``
#: links to the prior chained record (or GENESIS for the first). ``seq`` is the
#: chain position (monotonic, gap-free) so insertion/reorder is detectable.
_CHAIN_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_CHAIN_TABLE} (
    obs_id      INTEGER PRIMARY KEY,
    seq         INTEGER NOT NULL UNIQUE,
    prev_hash   TEXT    NOT NULL,
    record_hash TEXT    NOT NULL,
    FOREIGN KEY (obs_id) REFERENCES {_TABLE} (id)
);
CREATE INDEX IF NOT EXISTS idx_{_CHAIN_TABLE}_seq ON {_CHAIN_TABLE} (seq);
"""

#: Measured-energy store (v0.21.0, ADR-0011 §2). A **parallel** table to
#: ``observations`` — deliberately NOT new columns on ``observations`` (that
#: schema is test-pinned and its rows are content-hashed; new columns would
#: break the pin and perturb existing record hashes). Because the table is new
#: in v0.21.0 it has no legacy prefix by construction: every row it ever holds
#: is chained. It lives in the SAME ``~/.pat/observations.db`` file as the
#: serving store and reuses the identical private-file discipline.
_ENERGY_TABLE = "energy_observations"

#: Parallel hash-chain table for the energy store — IDENTICAL discipline to
#: ``observation_chain`` (keyed by energy-observation id, gap-free ``seq``).
_ENERGY_CHAIN_TABLE = "energy_observation_chain"

#: The strict meter enum for the measured-energy store (ADR-0011 §2 amendment,
#: corollary E1a: *pat never signs a watt it did not measure*). Deliberately NO
#: ``"manual"`` and NO ``"analytic"``. Kepler workload metrics are also not
#: accepted as direct measurement: current Kepler supports a synthetic CPU meter
#: with the same metric/zone shape and proportionally attributes node energy to
#: workloads. Only direct hardware-counter sources remain preset-attested.
VALID_METERS: tuple[str, ...] = ("rapl", "dcgm")

#: The valid sources for a measured-energy reading (v0.21.0, ADR-0011 E1a,
#: override-marking). A reading whose queries all matched the meter's pinned
#: presets is recorded as ``"prometheus"`` (the pinned-metric attestation
#: applies); a reading where the caller overrode EITHER the gate query or the
#: watts query away from the preset is recorded as ``"prometheus-override"`` so
#: that — because ``source`` is hash-chained — an overridden reading is
#: permanently distinguishable from a preset-attested one. No other source can
#: enter the measured-energy store: it is fed only by the Prometheus meter path.
VALID_ENERGY_SOURCES: tuple[str, ...] = ("prometheus", "prometheus-override")

_ENERGY_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_ENERGY_TABLE} (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT    NOT NULL,
    watts     REAL    NOT NULL,
    joules    REAL    NOT NULL,
    window_s  REAL    NOT NULL,
    rps       REAL    NOT NULL,
    layer     TEXT    NOT NULL,
    replicas  INTEGER NOT NULL,
    meter     TEXT    NOT NULL,
    source    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_{_ENERGY_TABLE}_ts ON {_ENERGY_TABLE} (timestamp);
CREATE INDEX IF NOT EXISTS idx_{_ENERGY_TABLE}_layer_ts
    ON {_ENERGY_TABLE} (layer, timestamp);
CREATE INDEX IF NOT EXISTS idx_{_ENERGY_TABLE}_layer_meter
    ON {_ENERGY_TABLE} (layer, meter);
"""

_ENERGY_CHAIN_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {_ENERGY_CHAIN_TABLE} (
    eobs_id     INTEGER PRIMARY KEY,
    seq         INTEGER NOT NULL UNIQUE,
    prev_hash   TEXT    NOT NULL,
    record_hash TEXT    NOT NULL,
    FOREIGN KEY (eobs_id) REFERENCES {_ENERGY_TABLE} (id)
);
CREATE INDEX IF NOT EXISTS idx_{_ENERGY_CHAIN_TABLE}_seq
    ON {_ENERGY_CHAIN_TABLE} (seq);
"""


def default_db_path() -> Path:
    """Return the global observation store path (``~/.pat/observations.db``)."""
    return Path.home() / _GLOBAL_DB_RELPATH[0] / _GLOBAL_DB_RELPATH[1]


# ---------------------------------------------------------------------------
# Observation record
# ---------------------------------------------------------------------------


class ObservationError(ValueError):
    """Raised when an observation fails validation."""


@dataclass(frozen=True)
class Observation:
    """A single workload measurement, independent of where it came from."""

    timestamp: datetime
    rps: float
    avg_latency_ms: float
    p99_latency_ms: float
    throughput: float
    layer: str
    replicas: int
    source: str = _DEFAULT_SOURCE

    def validate(self) -> Observation:
        """Return self if valid, else raise ObservationError."""
        if not self.layer or not str(self.layer).strip():
            raise ObservationError("layer must be a non-empty string")
        if not self.source or not str(self.source).strip():
            raise ObservationError("source must be a non-empty string")
        for name in ("rps", "avg_latency_ms", "p99_latency_ms", "throughput"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value < 0:
                raise ObservationError(f"{name} must be a non-negative number")
        if not isinstance(self.replicas, int) or self.replicas < 1:
            raise ObservationError("replicas must be an integer >= 1")
        if not isinstance(self.timestamp, datetime):
            raise ObservationError("timestamp must be a datetime")
        return self


class EnergyObservationError(ValueError):
    """Raised when a measured-energy observation fails validation."""


@dataclass(frozen=True)
class EnergyObservation:
    """A single **measured** energy reading (v0.21.0, ADR-0011 §2).

    Unlike the analytic energy model (``energy.py``), this is a watt that a real
    power meter reported — RAPL or DCGM. The ``meter`` field is a strict
    enum (:data:`VALID_METERS`) with deliberately **no** ``"manual"`` and no
    ``"analytic"`` value: corollary E1a says *pat never signs a watt it did not
    measure*, so an unmeasured or modelled watt cannot construct a valid record
    and therefore can never enter the chained store.
    """

    timestamp: datetime
    watts: float
    joules: float
    window_s: float
    rps: float
    layer: str
    replicas: int
    meter: str
    source: str

    def validate(self) -> EnergyObservation:
        """Return self if valid, else raise EnergyObservationError."""
        if not isinstance(self.timestamp, datetime):
            raise EnergyObservationError("timestamp must be a datetime")
        for name in ("watts", "joules", "rps"):
            value = getattr(self, name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise EnergyObservationError(f"{name} must be a finite number >= 0")
        if (
            not isinstance(self.window_s, (int, float))
            or isinstance(self.window_s, bool)
            or not math.isfinite(self.window_s)
            or self.window_s <= 0
        ):
            raise EnergyObservationError("window_s must be a finite number > 0")
        # joules must equal watts × window_s: the recorded energy is the measured
        # power integrated over the collection window, so an inconsistent pair
        # means the record was assembled wrongly or edited after the fact. Refuse
        # it (relative tolerance 1e-6, absolute 1e-9) — watts and window_s are
        # already validated finite above (ADR-0011 §2, P3).
        expected_joules = self.watts * self.window_s
        if not math.isclose(self.joules, expected_joules, rel_tol=1e-6, abs_tol=1e-9):
            raise EnergyObservationError(
                f"joules ({self.joules!r}) must equal watts×window_s "
                f"({expected_joules!r}); the recorded energy is inconsistent with "
                "the measured power over the collection window"
            )
        if not isinstance(self.replicas, int) or isinstance(self.replicas, bool):
            raise EnergyObservationError("replicas must be an integer >= 1")
        if self.replicas < 1:
            raise EnergyObservationError("replicas must be an integer >= 1")
        if not self.layer or not str(self.layer).strip():
            raise EnergyObservationError("layer must be a non-empty string")
        if self.meter not in VALID_METERS:
            raise EnergyObservationError(
                f"meter must be one of {VALID_METERS!r} — a measured meter. "
                "'manual' and 'analytic' are deliberately rejected: an unmeasured "
                "watt cannot enter the energy store (ADR-0011 §2 amendment, E1a)."
            )
        if not self.source or not str(self.source).strip():
            raise EnergyObservationError("source must be a non-empty string")
        if self.source not in VALID_ENERGY_SOURCES:
            raise EnergyObservationError(
                f"source must be one of {VALID_ENERGY_SOURCES!r}: a measured "
                "reading is recorded as 'prometheus' (all queries matched the "
                "meter's pinned presets, so the attestation applies) or "
                "'prometheus-override' (a query was overridden away from the "
                "preset, so the pinned-metric attestation does not apply). "
                "'prometheus-override' is accepted wherever 'prometheus' is "
                "(ADR-0011 E1a, override-marking)."
            )
        return self


_COLLECTION_PROOF = object()


@dataclass(frozen=True)
class CollectedEnergyObservation(EnergyObservation):
    """Energy reading sealed by the supported Prometheus collection path.

    The marker is deliberately process-local and is not persisted.  It is a
    capability boundary for the supported Python API, not a cryptographic
    attestation: a caller with arbitrary code execution in this process or
    write access to the SQLite file remains inside the documented local trust
    boundary.
    """

    _collection_proof: object = field(default=None, repr=False, compare=False)


def _seal_collected_energy_observation(
    obs: EnergyObservation,
) -> CollectedEnergyObservation:
    """Return *obs* sealed for :func:`record_energy_observation`."""
    obs.validate()
    return CollectedEnergyObservation(
        timestamp=obs.timestamp,
        watts=obs.watts,
        joules=obs.joules,
        window_s=obs.window_s,
        rps=obs.rps,
        layer=obs.layer,
        replicas=obs.replicas,
        meter=obs.meter,
        source=obs.source,
        _collection_proof=_COLLECTION_PROOF,
    )


# ---------------------------------------------------------------------------
# Timestamp helpers -- canonical UTC ISO-8601 (lexicographically chronological)
# ---------------------------------------------------------------------------


def _to_utc(dt: datetime) -> datetime:
    """Normalise *dt* to a timezone-aware UTC datetime (naive -> assumed UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _to_utc(dt).isoformat()


def _parse_iso(value: str) -> datetime:
    return _to_utc(datetime.fromisoformat(value))


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime (default record timestamp)."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Connection / schema management
# ---------------------------------------------------------------------------


def _prepare_store_path(path: Path, *, private_parent: bool) -> None:
    if private_parent:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            path.parent.chmod(0o700)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)


def _chmod_private_file(path: Path) -> None:
    with suppress(OSError):
        path.chmod(0o600)


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the store, ensuring the parent dir and schema exist."""
    use_default = db_path is None
    path = default_db_path() if use_default else Path(db_path)
    _prepare_store_path(path, private_parent=use_default)
    conn = sqlite3.connect(str(path))
    _chmod_private_file(path)
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.executescript(_CHAIN_SCHEMA)
        conn.executescript(_ENERGY_SCHEMA)
        conn.executescript(_ENERGY_CHAIN_SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older store forward (additive, idempotent)."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({_TABLE})")}
    if "source" not in cols:
        # Stores created before the source column gain it with the default.
        conn.execute(
            f"ALTER TABLE {_TABLE} "  # noqa: S608 -- fixed identifiers
            f"ADD COLUMN source TEXT NOT NULL DEFAULT '{_DEFAULT_SOURCE}'"
        )


def init_store(db_path: Path | None = None) -> Path:
    """Create the store + schema if absent; return the resolved path."""
    with _connect(db_path):
        pass
    return Path(db_path) if db_path is not None else default_db_path()


# ---------------------------------------------------------------------------
# Hash chain (v0.19.0) -- tamper-evidence for the local observation history
# ---------------------------------------------------------------------------
#
# Each chained observation carries a content hash over its own fields plus a
# link to the previous record's hash, so any post-hoc edit, deletion, insertion
# or reorder of the local history breaks the chain at the first affected row.
#
# Honest scope (do not overclaim): the chain proves the local history was not
# rewritten after the fact *relative to the chain head*. It does NOT prove the
# readings were honest at capture time -- a source can still record a false
# measurement; the chain only makes silent rewriting of what was recorded
# detectable. It is O(1) work per insert and O(n) to verify, all local.
#
# Float discipline: the evidence family's canonical profile rejects floats
# (evidence_producer._reject_floats). rps / latency / throughput are naturally
# floats, and rounding them for the hash would let two distinct observations
# collide. So the chain encodes every numeric reading as its shortest
# round-trip decimal string (repr) before hashing -- the repo's string-decimal
# encoding for numeric readings -- keeping the hash deterministic and lossless.


def _num_str(value: float | int) -> str:
    """Shortest round-trip decimal string for a reading (lossless, portable).

    ``repr(float)`` yields the shortest string that round-trips to the same
    IEEE-754 double across platforms, so the hash is deterministic without the
    non-portability that made the family reject bare floats on the wire.
    """
    return repr(float(value))


def observation_content(obs: Observation) -> dict[str, object]:
    """The exact, canonical field set hashed for the chain record.

    Mirrors what is persisted (post-normalisation): the UTC ISO-8601 timestamp
    and every measurement field, numerics as round-trip decimal strings.
    """
    return {
        "timestamp": _iso(obs.timestamp),
        "rps": _num_str(obs.rps),
        "avg_latency_ms": _num_str(obs.avg_latency_ms),
        "p99_latency_ms": _num_str(obs.p99_latency_ms),
        "throughput": _num_str(obs.throughput),
        "layer": str(obs.layer),
        "replicas": int(obs.replicas),
        "source": str(obs.source),
    }


def _canonical_bytes(payload: object) -> bytes:
    """Family canonical JSON (sorted keys, tight separators) -- floats already
    string-encoded upstream, so this stays byte-deterministic."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def record_hash(obs: Observation, prev_hash: str, seq: int) -> str:
    """SHA-256 over ``{content, prev_hash, seq}`` -- the chained record hash.

    Binding ``prev_hash`` links the record to its predecessor; binding ``seq``
    (the gap-free chain position) makes a reorder or a silent insertion/deletion
    change every downstream hash, not just the neighbours'.
    """
    record = {
        "content": observation_content(obs),
        "prev_hash": prev_hash,
        "seq": int(seq),
    }
    return hashlib.sha256(_canonical_bytes(record)).hexdigest()


def _chain_head(conn: sqlite3.Connection) -> tuple[int, str]:
    """Return ``(seq, record_hash)`` of the current chain head, or genesis.

    Genesis (empty chain) is ``(-1, GENESIS_PREV_HASH)`` so the first chained
    record gets ``seq = 0`` and ``prev_hash = GENESIS``.
    """
    row = conn.execute(
        f"SELECT seq, record_hash FROM {_CHAIN_TABLE} "  # noqa: S608 -- fixed ident
        "ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return (-1, GENESIS_PREV_HASH)
    return (int(row["seq"]), str(row["record_hash"]))


def _append_chain_link(conn: sqlite3.Connection, obs_id: int, obs: Observation) -> str:
    """Append a chain link for a just-inserted observation; return its hash."""
    head_seq, head_hash = _chain_head(conn)
    seq = head_seq + 1
    prev_hash = head_hash
    rec_hash = record_hash(obs, prev_hash, seq)
    conn.execute(
        f"INSERT INTO {_CHAIN_TABLE} "  # noqa: S608 -- fixed identifiers, params bound
        "(obs_id, seq, prev_hash, record_hash) VALUES (?, ?, ?, ?)",
        (int(obs_id), int(seq), prev_hash, rec_hash),
    )
    return rec_hash


# ---------------------------------------------------------------------------
# Ingestion (source-agnostic)
# ---------------------------------------------------------------------------


def record_observation(obs: Observation, db_path: Path | None = None) -> int:
    """Validate and persist *obs*; return the new row id."""
    obs.validate()
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO {_TABLE} "  # noqa: S608 -- fixed identifiers, params bound
            "(timestamp, rps, avg_latency_ms, p99_latency_ms, throughput, layer, "
            "replicas, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(obs.timestamp),
                float(obs.rps),
                float(obs.avg_latency_ms),
                float(obs.p99_latency_ms),
                float(obs.throughput),
                str(obs.layer),
                int(obs.replicas),
                str(obs.source),
            ),
        )
        obs_id = int(cur.lastrowid)
        # New observations always chain (v0.19.0). Legacy pre-chain rows keep
        # their unchained state and are reported UNVERIFIABLE by verify_chain.
        _append_chain_link(conn, obs_id, obs)
        return obs_id


def record(
    rps: float,
    avg_latency_ms: float,
    p99_latency_ms: float,
    throughput: float,
    layer: str,
    replicas: int,
    timestamp: datetime | None = None,
    source: str = _DEFAULT_SOURCE,
    db_path: Path | None = None,
) -> Observation:
    """
    Convenience: build, persist, and return an Observation in one call.

    ``timestamp`` defaults to now (UTC) -- callers replaying historical data
    (e.g. a Prometheus range query) should pass the measurement's own time.
    ``source`` records where the measurement came from (``manual`` / ``demo`` /
    ``prometheus`` / ...); it defaults to ``manual``.
    """
    obs = Observation(
        timestamp=timestamp if timestamp is not None else utcnow(),
        rps=rps,
        avg_latency_ms=avg_latency_ms,
        p99_latency_ms=p99_latency_ms,
        throughput=throughput,
        layer=layer,
        replicas=replicas,
        source=source,
    )
    record_observation(obs, db_path=db_path)
    return obs


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _row_to_obs(row: sqlite3.Row) -> Observation:
    return Observation(
        timestamp=_parse_iso(row["timestamp"]),
        rps=row["rps"],
        avg_latency_ms=row["avg_latency_ms"],
        p99_latency_ms=row["p99_latency_ms"],
        throughput=row["throughput"],
        layer=row["layer"],
        replicas=row["replicas"],
        source=row["source"],
    )


def load_observations(
    db_path: Path | None = None,
    layer: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    source: str | None = None,
) -> list[Observation]:
    """
    Return observations in chronological order (oldest first).

    ``layer`` filters to one layer; ``source`` filters to one origin
    (``manual`` / ``demo`` / ``prometheus`` / ...); ``since`` keeps only rows
    at/after a time; ``limit`` caps to the most recent N (still oldest-first).
    """
    clauses: list[str] = []
    params: list[object] = []
    if layer is not None:
        clauses.append("layer = ?")
        params.append(layer)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    if since is not None:
        clauses.append("timestamp >= ?")
        params.append(_iso(since))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _connect(db_path) as conn:
        if limit is not None:
            # Take the most recent `limit`, then re-sort oldest-first.
            sql = (
                f"SELECT * FROM (SELECT * FROM {_TABLE} {where} "  # noqa: S608
                "ORDER BY timestamp DESC, id DESC LIMIT ?) "
                "ORDER BY timestamp ASC, id ASC"
            )
            rows = conn.execute(sql, (*params, int(limit))).fetchall()
        else:
            sql = f"SELECT * FROM {_TABLE} {where} ORDER BY timestamp ASC, id ASC"  # noqa: S608
            rows = conn.execute(sql, params).fetchall()
    return [_row_to_obs(r) for r in rows]


def latest_observations(
    n: int,
    db_path: Path | None = None,
    layer: str | None = None,
    source: str | None = None,
) -> list[Observation]:
    """Return the most recent *n* observations, oldest-first (SMA window helper)."""
    if n <= 0:
        return []
    return load_observations(db_path=db_path, layer=layer, source=source, limit=n)


def count_observations(
    db_path: Path | None = None,
    layer: str | None = None,
    source: str | None = None,
) -> int:
    """Return the number of stored observations (optionally filtered)."""
    clauses: list[str] = []
    params: list[object] = []
    if layer is not None:
        clauses.append("layer = ?")
        params.append(layer)
    if source is not None:
        clauses.append("source = ?")
        params.append(source)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(db_path) as conn:
        sql = f"SELECT COUNT(*) AS c FROM {_TABLE} {where}"  # noqa: S608
        row = conn.execute(sql, params).fetchone()
    return int(row["c"])


# ---------------------------------------------------------------------------
# Chain verification (v0.19.0)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChainReport:
    """Result of walking the observation hash chain.

    ``ok`` is True only when every chained record verifies AND the chained set
    covers the whole store (no legacy prefix). ``legacy_count`` counts rows that
    predate the chain (no chain entry) -- reported UNVERIFIABLE, never counted
    as verified. ``broken_obs_id`` / ``break_reason`` locate the first break.
    """

    total: int
    chained: int
    verified: int
    legacy_count: int
    ok: bool
    broken_obs_id: int | None = None
    broken_seq: int | None = None
    break_reason: str | None = None

    @property
    def fully_covered(self) -> bool:
        """True when the chain covers every observation (no legacy prefix)."""
        return self.legacy_count == 0


def _load_chained_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Join chain links to their observations, ordered by chain ``seq``."""
    sql = (
        f"SELECT o.*, c.seq AS _seq, c.prev_hash AS _prev_hash, "  # noqa: S608
        f"c.record_hash AS _record_hash "
        f"FROM {_CHAIN_TABLE} c JOIN {_TABLE} o ON o.id = c.obs_id "
        "ORDER BY c.seq ASC"
    )
    return conn.execute(sql).fetchall()


def _walk_chain(
    total: int,
    rows: list[sqlite3.Row],
    record_hash_fn: object,
    row_to_obs_fn: object,
) -> ChainReport:
    """Walk chain-joined *rows* and return the first break, if any.

    Shared by :func:`verify_chain` (serving store) and
    :func:`verify_energy_chain` (measured-energy store): the two tables use the
    identical ADR-0010 discipline, so the walk is identical — only the row->obs
    decoder and the record-hash function differ. Keeping one walker means the
    serving chain's semantics cannot silently diverge from the energy chain's.
    ``broken_obs_id`` is the row's ``id`` in whichever table is being walked.
    """
    chained = len(rows)
    legacy_count = total - chained
    prev_hash = GENESIS_PREV_HASH
    expected_seq = 0
    verified = 0

    for row in rows:
        try:
            seq = int(row["_seq"])
            obs_id = int(row["id"])
            stored_hash = str(row["_record_hash"])
            stored_prev = str(row["_prev_hash"])
        except (TypeError, ValueError, OverflowError, KeyError, IndexError) as exc:
            return ChainReport(
                total=total,
                chained=chained,
                verified=verified,
                legacy_count=legacy_count,
                ok=False,
                broken_obs_id=-1,
                break_reason=f"malformed chain row: {exc}",
            )

        # Gap-free monotonic seq: a hole means a chained row was deleted.
        if seq != expected_seq:
            return ChainReport(
                total=total,
                chained=chained,
                verified=verified,
                legacy_count=legacy_count,
                ok=False,
                broken_obs_id=obs_id,
                broken_seq=seq,
                break_reason=(
                    f"chain sequence gap: expected seq {expected_seq}, found {seq} "
                    "(a chained observation was deleted or reordered)"
                ),
            )

        # The stored prev_hash must equal the running head.
        if stored_prev != prev_hash:
            return ChainReport(
                total=total,
                chained=chained,
                verified=verified,
                legacy_count=legacy_count,
                ok=False,
                broken_obs_id=obs_id,
                broken_seq=seq,
                break_reason=(
                    "prev_hash mismatch: link does not point at the previous "
                    "record (insertion, deletion, or reorder)"
                ),
            )

        # Recompute the record hash from the (possibly mutated) row content.
        try:
            obs = row_to_obs_fn(row)  # type: ignore[operator]
            recomputed = record_hash_fn(obs, prev_hash, seq)  # type: ignore[operator]
        except (
            TypeError,
            ValueError,
            OverflowError,
            KeyError,
            IndexError,
            json.JSONDecodeError,
        ) as exc:
            return ChainReport(
                total=total,
                chained=chained,
                verified=verified,
                legacy_count=legacy_count,
                ok=False,
                broken_obs_id=obs_id,
                broken_seq=seq,
                break_reason=f"malformed chained observation: {exc}",
            )
        if recomputed != stored_hash:
            return ChainReport(
                total=total,
                chained=chained,
                verified=verified,
                legacy_count=legacy_count,
                ok=False,
                broken_obs_id=obs_id,
                broken_seq=seq,
                break_reason=(
                    "record hash mismatch: the observation's contents were "
                    "modified after it was chained"
                ),
            )

        verified += 1
        prev_hash = stored_hash
        expected_seq += 1

    ok = legacy_count == 0 and verified == chained
    return ChainReport(
        total=total,
        chained=chained,
        verified=verified,
        legacy_count=legacy_count,
        ok=ok,
    )


def verify_chain(db_path: Path | None = None) -> ChainReport:
    """Walk the observation hash chain and report the first break, if any.

    Detects any post-hoc mutation, insertion, deletion or reorder of chained
    rows relative to the chain head. Rows predating the chain (no chain entry)
    are reported as an UNVERIFIABLE legacy prefix, not as verified coverage.

    Honest scope: a clean report means the local chained history was not
    rewritten after the fact; it does not attest that the readings were honest
    when captured.
    """
    with _connect(db_path) as conn:
        total = int(conn.execute(f"SELECT COUNT(*) AS c FROM {_TABLE}").fetchone()["c"])  # noqa: S608, E501
        rows = _load_chained_rows(conn)
    return _walk_chain(total, rows, record_hash, _row_to_obs)


# ---------------------------------------------------------------------------
# Measured-energy store: records, chain, queries, verification (v0.21.0)
# ---------------------------------------------------------------------------
#
# A byte-for-byte copy of the serving-side discipline above, over the parallel
# ``energy_observations`` / ``energy_observation_chain`` tables. The measurement
# schema of ``observations`` is untouched; the energy tables are additive and
# new, so they carry no legacy prefix (every energy row is chained on insert).


def energy_observation_content(obs: EnergyObservation) -> dict[str, object]:
    """The exact, canonical field set hashed for the energy chain record.

    Mirrors what is persisted (post-normalisation): the UTC ISO-8601 timestamp
    and every measurement field, numerics as round-trip decimal strings — the
    same string-decimal discipline the serving chain uses so the hash is
    deterministic and lossless.
    """
    return {
        "timestamp": _iso(obs.timestamp),
        "watts": _num_str(obs.watts),
        "joules": _num_str(obs.joules),
        "window_s": _num_str(obs.window_s),
        "rps": _num_str(obs.rps),
        "layer": str(obs.layer),
        "replicas": int(obs.replicas),
        "meter": str(obs.meter),
        "source": str(obs.source),
    }


def energy_record_hash(obs: EnergyObservation, prev_hash: str, seq: int) -> str:
    """SHA-256 over ``{content, prev_hash, seq}`` — the chained energy record hash.

    Identical construction to :func:`record_hash`; binding ``prev_hash`` and the
    gap-free ``seq`` makes any reorder/insertion/deletion change every
    downstream hash.
    """
    record = {
        "content": energy_observation_content(obs),
        "prev_hash": prev_hash,
        "seq": int(seq),
    }
    return hashlib.sha256(_canonical_bytes(record)).hexdigest()


def _energy_chain_head(conn: sqlite3.Connection) -> tuple[int, str]:
    """Return ``(seq, record_hash)`` of the energy chain head, or genesis."""
    row = conn.execute(
        f"SELECT seq, record_hash FROM {_ENERGY_CHAIN_TABLE} "  # noqa: S608 -- fixed
        "ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return (-1, GENESIS_PREV_HASH)
    return (int(row["seq"]), str(row["record_hash"]))


def _append_energy_chain_link(
    conn: sqlite3.Connection, eobs_id: int, obs: EnergyObservation
) -> str:
    """Append a chain link for a just-inserted energy observation; return its hash."""
    head_seq, head_hash = _energy_chain_head(conn)
    seq = head_seq + 1
    prev_hash = head_hash
    rec_hash = energy_record_hash(obs, prev_hash, seq)
    conn.execute(
        f"INSERT INTO {_ENERGY_CHAIN_TABLE} "  # noqa: S608 -- fixed ident, params bound
        "(eobs_id, seq, prev_hash, record_hash) VALUES (?, ?, ?, ?)",
        (int(eobs_id), int(seq), prev_hash, rec_hash),
    )
    return rec_hash


def record_energy_observation(
    obs: CollectedEnergyObservation, db_path: Path | None = None
) -> int:
    """Persist an energy reading sealed by the supported collector.

    The chain link is appended in the SAME transaction as the insert (via the
    shared ``_connect`` context manager's single commit), exactly like
    :func:`record_observation`. Plain caller-constructed
    :class:`EnergyObservation` objects are rejected so the supported API cannot
    silently label arbitrary numbers as preset-attested Prometheus readings.
    """
    if (
        not isinstance(obs, CollectedEnergyObservation)
        or obs._collection_proof is not _COLLECTION_PROOF
    ):
        raise EnergyObservationError(
            "energy observations must be collector-sealed by the supported, "
            "platform-gated "
            "Prometheus collector; caller-constructed readings cannot be recorded "
            "as measured"
        )
    return _record_energy_observation_unchecked(obs, db_path=db_path)


def _record_energy_observation_unchecked(
    obs: EnergyObservation, db_path: Path | None = None
) -> int:
    """Persist a validated row without a collection proof.

    Private test/migration primitive. Production callers must use
    :func:`record_energy_observation`, which requires a collector-sealed object.
    This function is intentionally underscored and is not a measurement-origin
    attestation.
    """
    obs.validate()
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO {_ENERGY_TABLE} "  # noqa: S608 -- fixed idents, params bound
            "(timestamp, watts, joules, window_s, rps, layer, replicas, meter, "
            "source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _iso(obs.timestamp),
                float(obs.watts),
                float(obs.joules),
                float(obs.window_s),
                float(obs.rps),
                str(obs.layer),
                int(obs.replicas),
                str(obs.meter),
                str(obs.source),
            ),
        )
        eobs_id = int(cur.lastrowid)
        _append_energy_chain_link(conn, eobs_id, obs)
        return eobs_id


def _row_to_energy_obs(row: sqlite3.Row) -> EnergyObservation:
    return EnergyObservation(
        timestamp=_parse_iso(row["timestamp"]),
        watts=row["watts"],
        joules=row["joules"],
        window_s=row["window_s"],
        rps=row["rps"],
        layer=row["layer"],
        replicas=row["replicas"],
        meter=row["meter"],
        source=row["source"],
    )


def load_energy_observations(
    db_path: Path | None = None,
    layer: str | None = None,
    meter: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[EnergyObservation]:
    """Return measured-energy observations in chronological order (oldest first).

    ``layer`` filters to one layer; ``meter`` filters to one measured meter;
    ``since`` keeps rows at/after a time; ``limit`` caps to the most recent N
    (still oldest-first). Mirrors :func:`load_observations`.
    """
    clauses: list[str] = []
    params: list[object] = []
    if layer is not None:
        clauses.append("layer = ?")
        params.append(layer)
    if meter is not None:
        clauses.append("meter = ?")
        params.append(meter)
    if since is not None:
        clauses.append("timestamp >= ?")
        params.append(_iso(since))
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with _connect(db_path) as conn:
        if limit is not None:
            sql = (
                f"SELECT * FROM (SELECT * FROM {_ENERGY_TABLE} {where} "  # noqa: S608
                "ORDER BY timestamp DESC, id DESC LIMIT ?) "
                "ORDER BY timestamp ASC, id ASC"
            )
            rows = conn.execute(sql, (*params, int(limit))).fetchall()
        else:
            sql = (
                f"SELECT * FROM {_ENERGY_TABLE} {where} "  # noqa: S608
                "ORDER BY timestamp ASC, id ASC"
            )
            rows = conn.execute(sql, params).fetchall()
    return [_row_to_energy_obs(r) for r in rows]


def load_verified_energy_observations(
    db_path: Path | None = None,
    layer: str | None = None,
    meter: str | None = None,
    since: datetime | None = None,
    limit: int | None = None,
) -> list[EnergyObservation]:
    """Read energy rows only when the complete chain verifies.

    This is the consumer-facing read path. It opens an existing SQLite file in
    read-only mode, never creates or migrates storage, verifies full chain
    coverage in the same snapshot, and validates each decoded row. A missing
    database or a pre-v0.21 database without energy tables is an empty store.
    Broken/incomplete chains fail closed with :class:`EnergyObservationError`.
    """
    path = default_db_path() if db_path is None else Path(db_path)
    if not path.is_file():
        return []

    uri_path = urllib.parse.quote(str(path.resolve()), safe="/")
    conn = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if _ENERGY_TABLE not in tables or _ENERGY_CHAIN_TABLE not in tables:
            return []

        report = _energy_chain_report(conn)
        if not report.ok:
            detail = report.break_reason or (
                f"incomplete chain coverage: {report.verified}/{report.total} verified"
            )
            raise EnergyObservationError(
                f"measured-energy chain is not fully verified; refusing to consume "
                f"the store ({detail})"
            )

        clauses: list[str] = []
        params: list[object] = []
        if layer is not None:
            clauses.append("layer = ?")
            params.append(layer)
        if meter is not None:
            clauses.append("meter = ?")
            params.append(meter)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(_iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        if limit is not None:
            sql = (
                f"SELECT * FROM (SELECT * FROM {_ENERGY_TABLE} {where} "  # noqa: S608
                "ORDER BY timestamp DESC, id DESC LIMIT ?) "
                "ORDER BY timestamp ASC, id ASC"
            )
            rows = conn.execute(sql, (*params, int(limit))).fetchall()
        else:
            sql = (
                f"SELECT * FROM {_ENERGY_TABLE} {where} "  # noqa: S608
                "ORDER BY timestamp ASC, id ASC"
            )
            rows = conn.execute(sql, params).fetchall()

        decoded: list[EnergyObservation] = []
        for row in rows:
            try:
                decoded.append(_row_to_energy_obs(row).validate())
            except (TypeError, ValueError, OverflowError) as exc:
                raise EnergyObservationError(
                    f"verified energy row {row['id']!r} is malformed: {exc}"
                ) from exc
        return decoded
    finally:
        conn.close()


def count_energy_observations(
    db_path: Path | None = None,
    layer: str | None = None,
    meter: str | None = None,
) -> int:
    """Return the count of stored measured-energy observations (optionally filtered)."""
    clauses: list[str] = []
    params: list[object] = []
    if layer is not None:
        clauses.append("layer = ?")
        params.append(layer)
    if meter is not None:
        clauses.append("meter = ?")
        params.append(meter)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _connect(db_path) as conn:
        sql = f"SELECT COUNT(*) AS c FROM {_ENERGY_TABLE} {where}"  # noqa: S608
        row = conn.execute(sql, params).fetchone()
    return int(row["c"])


def _load_energy_chained_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Join energy chain links to their observations, ordered by chain ``seq``."""
    sql = (
        f"SELECT o.*, c.seq AS _seq, c.prev_hash AS _prev_hash, "  # noqa: S608
        f"c.record_hash AS _record_hash "
        f"FROM {_ENERGY_CHAIN_TABLE} c JOIN {_ENERGY_TABLE} o ON o.id = c.eobs_id "
        "ORDER BY c.seq ASC"
    )
    return conn.execute(sql).fetchall()


def _energy_chain_report(conn: sqlite3.Connection) -> ChainReport:
    """Verify the energy chain using the caller's existing DB snapshot."""
    total = int(
        conn.execute(
            f"SELECT COUNT(*) AS c FROM {_ENERGY_TABLE}"  # noqa: S608
        ).fetchone()["c"]
    )
    return _walk_chain(
        total, _load_energy_chained_rows(conn), energy_record_hash, _row_to_energy_obs
    )


def verify_energy_chain(db_path: Path | None = None) -> ChainReport:
    """Walk the measured-energy hash chain and report the first break, if any.

    Same :class:`ChainReport` shape and semantics as :func:`verify_chain`, over
    the parallel energy tables. Because ``energy_observations`` is new in
    v0.21.0, a well-formed store has ``legacy_count == 0`` — there is no
    pre-chain era to leave an unverifiable prefix.
    """
    with _connect(db_path) as conn:
        return _energy_chain_report(conn)


def verify_all_chains(
    db_path: Path | None = None,
) -> tuple[ChainReport, ChainReport]:
    """Verify both chains; return ``(serving_report, energy_report)``.

    A convenience for ``pat observe verify``, which walks and renders both. The
    two reports are independent — a break in either yields exit 1 at the CLI, per
    ADR-0011 §2 — so this never conflates them into a single verdict.
    """
    return (verify_chain(db_path=db_path), verify_energy_chain(db_path=db_path))
