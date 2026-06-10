"""Tests for the rolling observation store (v0.8.0 Phase 1)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import (
    Observation,
    ObservationError,
    count_observations,
    default_db_path,
    init_store,
    latest_observations,
    load_observations,
    record,
    record_observation,
)

runner = CliRunner()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "observations.db"


def _obs(ts: datetime, layer: str = "container", rps: float = 500.0) -> Observation:
    return Observation(
        timestamp=ts,
        rps=rps,
        avg_latency_ms=80.0,
        p99_latency_ms=140.0,
        throughput=rps * 0.96,
        layer=layer,
        replicas=6,
    )


_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# schema / init
# ---------------------------------------------------------------------------


def test_default_db_path_is_global_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_db_path() == tmp_path / ".pat" / "observations.db"


def test_init_store_creates_file_and_schema(db):
    path = init_store(db)
    assert path == db
    assert db.is_file()
    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(observations)")}
    finally:
        conn.close()
    assert cols == {
        "id",
        "timestamp",
        "rps",
        "avg_latency_ms",
        "p99_latency_ms",
        "throughput",
        "layer",
        "replicas",
    }


def test_connect_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "nested" / "obs.db"
    init_store(nested)
    assert nested.is_file()


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------


def test_record_observation_returns_incrementing_ids(db):
    id1 = record_observation(_obs(_T0), db_path=db)
    id2 = record_observation(_obs(_T0 + timedelta(minutes=1)), db_path=db)
    assert id1 == 1
    assert id2 == 2


def test_record_convenience_defaults_timestamp(db):
    obs = record(
        rps=500,
        avg_latency_ms=80,
        p99_latency_ms=140,
        throughput=480,
        layer="container",
        replicas=6,
        db_path=db,
    )
    assert obs.timestamp.tzinfo is not None
    assert count_observations(db_path=db) == 1


def test_record_roundtrip_preserves_values(db):
    record_observation(_obs(_T0, layer="pod", rps=321.0), db_path=db)
    (loaded,) = load_observations(db_path=db)
    assert loaded.layer == "pod"
    assert loaded.rps == pytest.approx(321.0)
    assert loaded.timestamp == _T0


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_layer_rejected(self, db):
        with pytest.raises(ObservationError, match="layer"):
            record_observation(_obs(_T0, layer="  "), db_path=db)

    def test_negative_rps_rejected(self, db):
        bad = Observation(_T0, -1.0, 80, 140, 100, "container", 6)
        with pytest.raises(ObservationError, match="rps"):
            record_observation(bad, db_path=db)

    def test_replicas_below_one_rejected(self, db):
        bad = Observation(_T0, 500, 80, 140, 480, "container", 0)
        with pytest.raises(ObservationError, match="replicas"):
            record_observation(bad, db_path=db)

    def test_non_datetime_timestamp_rejected(self, db):
        bad = Observation("2026-01-01", 500, 80, 140, 480, "container", 6)  # type: ignore[arg-type]
        with pytest.raises(ObservationError, match="timestamp"):
            record_observation(bad, db_path=db)


# ---------------------------------------------------------------------------
# timestamp normalisation
# ---------------------------------------------------------------------------


def test_naive_timestamp_assumed_utc(db):
    naive = datetime(2026, 6, 1, 12, 0, 0)  # no tzinfo
    record_observation(_obs(naive), db_path=db)
    (loaded,) = load_observations(db_path=db)
    assert loaded.timestamp == _T0  # same instant, now tz-aware UTC


def test_aware_non_utc_timestamp_normalised(db):
    plus2 = timezone(timedelta(hours=2))
    ts = datetime(2026, 6, 1, 14, 0, 0, tzinfo=plus2)  # == 12:00 UTC
    record_observation(_obs(ts), db_path=db)
    (loaded,) = load_observations(db_path=db)
    assert loaded.timestamp == _T0


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


class TestQueries:
    def _seed(self, db):
        # Insert out of order; the store must return chronological order.
        for minute in (3, 0, 2, 1, 4):
            record_observation(
                _obs(_T0 + timedelta(minutes=minute), rps=500 + minute), db_path=db
            )

    def test_load_returns_chronological_order(self, db):
        self._seed(db)
        rows = load_observations(db_path=db)
        ts = [r.timestamp for r in rows]
        assert ts == sorted(ts)
        assert rows[0].rps == pytest.approx(500)  # minute 0
        assert rows[-1].rps == pytest.approx(504)  # minute 4

    def test_limit_returns_most_recent_oldest_first(self, db):
        self._seed(db)
        rows = load_observations(db_path=db, limit=2)
        assert [r.rps for r in rows] == [pytest.approx(503), pytest.approx(504)]

    def test_layer_filter(self, db):
        record_observation(_obs(_T0, layer="container"), db_path=db)
        record_observation(_obs(_T0 + timedelta(minutes=1), layer="pod"), db_path=db)
        rows = load_observations(db_path=db, layer="pod")
        assert len(rows) == 1
        assert rows[0].layer == "pod"

    def test_since_filter(self, db):
        self._seed(db)
        rows = load_observations(db_path=db, since=_T0 + timedelta(minutes=2))
        assert len(rows) == 3  # minutes 2, 3, 4
        assert all(r.timestamp >= _T0 + timedelta(minutes=2) for r in rows)

    def test_latest_observations(self, db):
        self._seed(db)
        rows = latest_observations(3, db_path=db)
        assert [r.rps for r in rows] == [
            pytest.approx(502),
            pytest.approx(503),
            pytest.approx(504),
        ]

    def test_latest_observations_non_positive_returns_empty(self, db):
        self._seed(db)
        assert latest_observations(0, db_path=db) == []
        assert latest_observations(-5, db_path=db) == []

    def test_count_total_and_per_layer(self, db):
        record_observation(_obs(_T0, layer="container"), db_path=db)
        record_observation(_obs(_T0 + timedelta(minutes=1), layer="pod"), db_path=db)
        record_observation(_obs(_T0 + timedelta(minutes=2), layer="pod"), db_path=db)
        assert count_observations(db_path=db) == 3
        assert count_observations(db_path=db, layer="pod") == 2
        assert count_observations(db_path=db, layer="node") == 0

    def test_queries_on_empty_store(self, db):
        assert load_observations(db_path=db) == []
        assert count_observations(db_path=db) == 0


# ---------------------------------------------------------------------------
# CLI: pat observe
# ---------------------------------------------------------------------------


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "observe", *args])


class TestObserveCLI:
    def test_record_then_list(self, db):
        rec = _invoke(
            "--layer",
            "container",
            "--rps",
            "500",
            "--avg-latency-ms",
            "80",
            "--p99-latency-ms",
            "140",
            "--throughput",
            "480",
            "--replicas",
            "6",
            "--db",
            str(db),
        )
        assert rec.exit_code == 0
        assert "Recorded" in rec.output
        assert count_observations(db_path=db) == 1

        listed = _invoke("--list", "--db", str(db))
        assert listed.exit_code == 0
        # Table cells can be width-truncated at 80 cols; assert the row count
        # header (robust) and rely on the store round-trip tests for fidelity.
        assert "Recent observations (showing 1 of 1)" in listed.output

    def test_record_missing_fields_exits_2(self, db):
        result = _invoke("--layer", "container", "--rps", "500", "--db", str(db))
        assert result.exit_code == 2
        assert "requires all measurement options" in result.output

    def test_list_empty_store(self, db):
        result = _invoke("--list", "--db", str(db))
        assert result.exit_code == 0
        assert "No observations recorded yet" in result.output

    def test_invalid_layer_exits_2(self, db):
        result = _invoke(
            "--layer",
            "not-a-layer",
            "--rps",
            "500",
            "--avg-latency-ms",
            "80",
            "--p99-latency-ms",
            "140",
            "--throughput",
            "480",
            "--replicas",
            "6",
            "--db",
            str(db),
        )
        assert result.exit_code == 2
        assert count_observations(db_path=db) == 0
