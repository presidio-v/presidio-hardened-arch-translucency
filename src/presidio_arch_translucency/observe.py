"""
Rolling observation store (v0.8.0, Phase 1).

A source-agnostic SQLite store for workload measurements.  Any source — a
``pat demo`` run, a Prometheus scrape, a load test, or a manual entry — builds an
:class:`Observation` and calls :func:`record_observation`; ``pat optimize`` reads
them back via :func:`load_observations` / :func:`latest_observations`.

The store is intentionally **single-shot** (decision D2): a caller records one
measurement and returns.  Recurring collection is scheduled externally
(cron / launchd / a Kubernetes CronJob), not by a daemon or a foreground loop.

Storage (decision D5 / cross-cutting): the global store lives at
``~/.pat/observations.db``.  The schema matches PRESIDIO-REQ.md exactly:

    timestamp, rps, avg_latency_ms, p99_latency_ms, throughput, layer, replicas

An autoincrement ``id`` is added as the primary key (insertion order, used only
to break ties between identical timestamps), plus a ``source`` column
(``manual`` / ``demo`` / ``prometheus`` / …) so later phases can tell where a
measurement came from.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Storage location & schema
# ---------------------------------------------------------------------------

_GLOBAL_DB_RELPATH: tuple[str, str] = (".pat", "observations.db")

_TABLE = "observations"

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

_COLUMNS = (
    "timestamp",
    "rps",
    "avg_latency_ms",
    "p99_latency_ms",
    "throughput",
    "layer",
    "replicas",
    "source",
)


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
# Timestamp helpers — canonical UTC ISO-8601 (lexicographically chronological)
# ---------------------------------------------------------------------------


def _to_utc(dt: datetime) -> datetime:
    """Normalise *dt* to a timezone-aware UTC datetime (naive ⇒ assumed UTC)."""
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


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Open the store, ensuring the parent dir and schema exist."""
    path = Path(db_path) if db_path is not None else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
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
            f"ALTER TABLE {_TABLE} "  # noqa: S608 — fixed identifiers
            f"ADD COLUMN source TEXT NOT NULL DEFAULT '{_DEFAULT_SOURCE}'"
        )


def init_store(db_path: Path | None = None) -> Path:
    """Create the store + schema if absent; return the resolved path."""
    with _connect(db_path):
        pass
    return Path(db_path) if db_path is not None else default_db_path()


# ---------------------------------------------------------------------------
# Ingestion (source-agnostic)
# ---------------------------------------------------------------------------


def record_observation(obs: Observation, db_path: Path | None = None) -> int:
    """Validate and persist *obs*; return the new row id."""
    obs.validate()
    with _connect(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO {_TABLE} "  # noqa: S608 — fixed identifiers, params bound
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
        return int(cur.lastrowid)


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

    ``timestamp`` defaults to now (UTC) — callers replaying historical data
    (e.g. a Prometheus range query) should pass the measurement's own time.
    ``source`` records where the measurement came from (``manual`` / ``demo`` /
    ``prometheus`` / …); it defaults to ``manual``.
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
    (``manual`` / ``demo`` / ``prometheus`` / …); ``since`` keeps only rows
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
