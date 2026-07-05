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
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
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

    chained = len(rows)
    legacy_count = total - chained
    prev_hash = GENESIS_PREV_HASH
    expected_seq = 0
    verified = 0

    for row in rows:
        seq = int(row["_seq"])
        obs_id = int(row["id"])
        stored_hash = str(row["_record_hash"])
        stored_prev = str(row["_prev_hash"])

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
        obs = _row_to_obs(row)
        recomputed = record_hash(obs, prev_hash, seq)
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
