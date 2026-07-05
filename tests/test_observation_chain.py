"""Tests for the observation hash chain (v0.19.0).

The chain proves the local observation history was not rewritten after the fact
relative to the chain head. These tests cover chain build + verify, each tamper
class (row edit, row delete, row insert, reorder), and honest legacy-prefix
reporting for stores that predate chaining. They do NOT assert anything about
whether the readings were honest at capture — that is out of scope by design.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import (
    GENESIS_PREV_HASH,
    Observation,
    record,
    record_observation,
    verify_chain,
)

runner = CliRunner()

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "observations.db"


def _obs(ts: datetime, rps: float = 500.0, layer: str = "container") -> Observation:
    return Observation(
        timestamp=ts,
        rps=rps,
        avg_latency_ms=80.5,
        p99_latency_ms=140.0,
        throughput=rps * 0.96,
        layer=layer,
        replicas=6,
    )


def _seed(db, n: int = 4) -> None:
    for i in range(n):
        record_observation(_obs(_T0 + timedelta(minutes=i), rps=500.0 + i), db_path=db)


# ── build + verify ────────────────────────────────────────────────────────────


def test_empty_store_verifies_ok(db):
    report = verify_chain(db)
    assert report.ok is True
    assert report.total == 0
    assert report.legacy_count == 0


def test_clean_chain_verifies(db):
    _seed(db, 5)
    report = verify_chain(db)
    assert report.ok is True
    assert report.total == 5
    assert report.chained == 5
    assert report.verified == 5
    assert report.legacy_count == 0
    assert report.fully_covered is True
    assert report.broken_obs_id is None


def test_genesis_prev_hash_documented_and_used(db):
    record_observation(_obs(_T0), db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT seq, prev_hash FROM observation_chain ORDER BY seq"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 0
    assert row[1] == GENESIS_PREV_HASH


def test_record_convenience_also_chains(db):
    record(
        rps=500,
        avg_latency_ms=80,
        p99_latency_ms=140,
        throughput=480,
        layer="container",
        replicas=6,
        db_path=db,
    )
    assert verify_chain(db).ok is True


# ── tamper class: row edit ────────────────────────────────────────────────────


def test_row_edit_detected(db):
    _seed(db, 4)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE observations SET rps = 9999 WHERE id = 2")
    conn.commit()
    conn.close()

    report = verify_chain(db)
    assert report.ok is False
    assert report.broken_obs_id == 2
    assert "hash mismatch" in report.break_reason


# ── tamper class: row delete (chain link removed) ─────────────────────────────


def test_chain_link_delete_detected(db):
    _seed(db, 4)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM observation_chain WHERE seq = 1")
    conn.commit()
    conn.close()

    report = verify_chain(db)
    assert report.ok is False
    assert "sequence gap" in report.break_reason


def test_observation_row_delete_orphans_chain(db):
    # Deleting the observation row (but not its chain link) drops it from the
    # verified join, leaving a seq gap the walk detects.
    _seed(db, 4)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM observations WHERE id = 2")
    conn.commit()
    conn.close()

    report = verify_chain(db)
    assert report.ok is False


# ── tamper class: row insert ──────────────────────────────────────────────────


def test_unchained_insert_reported_as_legacy_not_verified(db):
    # An attacker-inserted observation with no chain link cannot masquerade as
    # verified history: it is counted as an UNVERIFIABLE legacy row.
    _seed(db, 3)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO observations "
        "(timestamp, rps, avg_latency_ms, p99_latency_ms, throughput, layer, "
        "replicas, source) VALUES "
        "('2026-05-01T00:00:00+00:00', 100, 50, 90, 95, 'pod', 3, 'manual')"
    )
    conn.commit()
    conn.close()

    report = verify_chain(db)
    assert report.ok is False
    assert report.legacy_count == 1
    assert report.verified == 3


def test_forged_chain_insert_detected(db):
    # Inserting a fabricated chain link with a shifted seq breaks the running
    # prev_hash / seq expectation.
    _seed(db, 3)
    conn = sqlite3.connect(str(db))
    # Duplicate obs id 1's content into a new row, then chain it at seq 1.
    conn.execute(
        "INSERT INTO observations "
        "(timestamp, rps, avg_latency_ms, p99_latency_ms, throughput, layer, "
        "replicas, source) VALUES "
        "('2026-06-01T12:00:30+00:00', 501, 80, 140, 480, 'container', 6, 'manual')"
    )
    new_id = conn.execute("SELECT MAX(id) FROM observations").fetchone()[0]
    # Shift existing seq>=1 up by one to make room, then insert a bogus link.
    conn.execute("UPDATE observation_chain SET seq = seq + 100 WHERE seq >= 1")
    conn.execute(
        "INSERT INTO observation_chain (obs_id, seq, prev_hash, record_hash) "
        "VALUES (?, 1, 'deadbeef', 'cafebabe')",
        (new_id,),
    )
    conn.execute("UPDATE observation_chain SET seq = seq - 99 WHERE seq >= 100")
    conn.commit()
    conn.close()

    report = verify_chain(db)
    assert report.ok is False


# ── tamper class: reorder ─────────────────────────────────────────────────────


def test_reorder_detected(db):
    _seed(db, 4)
    conn = sqlite3.connect(str(db))
    # Swap seq of two adjacent chained records.
    conn.execute("UPDATE observation_chain SET seq = 99 WHERE seq = 1")
    conn.execute("UPDATE observation_chain SET seq = 1 WHERE seq = 2")
    conn.execute("UPDATE observation_chain SET seq = 2 WHERE seq = 99")
    conn.commit()
    conn.close()

    report = verify_chain(db)
    assert report.ok is False
    assert "prev_hash mismatch" in report.break_reason


# ── legacy-prefix reporting ───────────────────────────────────────────────────


def test_legacy_prefix_reported_unverifiable_not_ok(db):
    # Simulate a pre-chain store: observations with no chain links, followed by
    # freshly chained rows.
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, rps REAL NOT NULL,
            avg_latency_ms REAL NOT NULL, p99_latency_ms REAL NOT NULL,
            throughput REAL NOT NULL, layer TEXT NOT NULL,
            replicas INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'manual'
        );
        """
    )
    for i in range(2):
        conn.execute(
            "INSERT INTO observations "
            "(timestamp, rps, avg_latency_ms, p99_latency_ms, throughput, layer, "
            "replicas, source) VALUES "
            "(?, 400, 70, 120, 390, 'container', 4, 'manual')",
            ((_T0 + timedelta(minutes=i)).isoformat(),),
        )
    conn.commit()
    conn.close()

    # New observations chain on top of the legacy prefix.
    record_observation(_obs(_T0 + timedelta(minutes=10)), db_path=db)
    record_observation(_obs(_T0 + timedelta(minutes=11)), db_path=db)

    report = verify_chain(db)
    assert report.ok is False  # not fully covered
    assert report.legacy_count == 2
    assert report.verified == 2
    assert report.fully_covered is False


# ── CLI: pat observe verify ───────────────────────────────────────────────────


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def test_cli_verify_clean_exits_zero(db):
    _seed(db, 3)
    result = _invoke("observe", "verify", "--db", str(db))
    assert result.exit_code == 0
    assert "verified" in result.output.lower() or "intact" in result.output.lower()


def test_cli_verify_tampered_exits_one(db):
    _seed(db, 3)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE observations SET rps = 9999 WHERE id = 2")
    conn.commit()
    conn.close()
    result = _invoke("observe", "verify", "--db", str(db))
    assert result.exit_code == 1
    assert "broken" in result.output.lower()


def test_cli_verify_tampered_with_allow_legacy_still_exits_one(db):
    _seed(db, 3)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE observations SET rps = 9999 WHERE id = 2")
    conn.commit()
    conn.close()
    result = _invoke("observe", "verify", "--db", str(db), "--allow-legacy")
    assert result.exit_code == 1
    assert "broken" in result.output.lower()


def _seed_legacy_prefix(db) -> None:
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, rps REAL NOT NULL,
            avg_latency_ms REAL NOT NULL, p99_latency_ms REAL NOT NULL,
            throughput REAL NOT NULL, layer TEXT NOT NULL,
            replicas INTEGER NOT NULL, source TEXT NOT NULL DEFAULT 'manual'
        );
        """
    )
    conn.execute(
        "INSERT INTO observations "
        "(timestamp, rps, avg_latency_ms, p99_latency_ms, throughput, layer, "
        "replicas, source) VALUES "
        "('2026-05-01T00:00:00+00:00', 100, 50, 90, 95, 'pod', 3, 'manual')"
    )
    conn.commit()
    conn.close()
    record_observation(_obs(_T0 + timedelta(minutes=10)), db_path=db)


def test_cli_verify_legacy_reports_partial_and_exits_two(db):
    _seed_legacy_prefix(db)

    result = _invoke("observe", "verify", "--db", str(db))
    # Legacy prefix present but chained suffix intact -> partial, exit 2 by default.
    assert result.exit_code == 2
    assert "legacy" in result.output.lower()


def test_cli_verify_legacy_with_allow_legacy_exits_zero(db):
    _seed_legacy_prefix(db)

    result = _invoke("observe", "verify", "--db", str(db), "--allow-legacy")
    assert result.exit_code == 0
    assert "legacy" in result.output.lower()
