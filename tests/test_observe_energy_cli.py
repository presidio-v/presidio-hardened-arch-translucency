"""CLI tests for measured-energy mode: `pat observe --energy` + `verify`.

Covers the round-trip against a stubbed fetch, argument-guard errors, the
"nothing written on refusal" property, the energy-observation listing, and the
two-section `pat observe verify` output with its exit-code matrix (ADR-0011 §2).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import (
    EnergyObservation,
    Observation,
    _record_energy_observation_unchecked,
    _seal_collected_energy_observation,
    count_energy_observations,
    load_energy_observations,
    record_observation,
)
from presidio_arch_translucency.prometheus import PrometheusError

runner = CliRunner()
_URL = "https://prometheus.monitoring.svc:9090"
_T0 = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "observe", *args])


def _fake(watts=120.0, layer="node", meter="rapl"):
    return _seal_collected_energy_observation(
        EnergyObservation(
            datetime.now(timezone.utc),
            watts,
            watts * 60.0,
            60.0,
            500.0,
            layer,
            4,
            meter,
            "prometheus",
        )
    )


# ── record mode ───────────────────────────────────────────────────────────────


class TestObserveEnergyRecord:
    def test_round_trip_against_stubbed_fetch(self, tmp_path):
        db = tmp_path / "obs.db"
        with patch(
            "presidio_arch_translucency.prometheus.fetch_energy_observation",
            return_value=_fake(),
        ):
            res = _invoke(
                "--prometheus",
                _URL,
                "--energy",
                "--energy-meter",
                "rapl",
                "--layer",
                "node",
                "--db",
                str(db),
            )
        assert res.exit_code == 0
        assert "Measured" in res.output
        assert "seq 0" in res.output
        assert count_energy_observations(db_path=db) == 1
        (stored,) = load_energy_observations(db_path=db)
        assert stored.meter == "rapl"
        assert stored.source == "prometheus"

    def test_override_source_prints_warning_and_is_recorded(self, tmp_path):
        db = tmp_path / "obs.db"
        override_obs = _seal_collected_energy_observation(
            EnergyObservation(
                datetime.now(timezone.utc),
                120.0,
                120.0 * 60.0,
                60.0,
                500.0,
                "node",
                4,
                "rapl",
                "prometheus-override",
            )
        )
        with patch(
            "presidio_arch_translucency.prometheus.fetch_energy_observation",
            return_value=override_obs,
        ):
            res = _invoke(
                "--prometheus",
                _URL,
                "--energy",
                "--energy-meter",
                "rapl",
                "--layer",
                "node",
                "--energy-watts-query",
                "sum(increase(node_rapl_package_joules_total[60s])) / 60",
                "--db",
                str(db),
            )
        assert res.exit_code == 0
        assert "override active" in res.output
        assert "prometheus-override" in res.output
        (stored,) = load_energy_observations(db_path=db)
        assert stored.source == "prometheus-override"

    def test_no_override_prints_no_warning(self, tmp_path):
        db = tmp_path / "obs.db"
        with patch(
            "presidio_arch_translucency.prometheus.fetch_energy_observation",
            return_value=_fake(),
        ):
            res = _invoke(
                "--prometheus",
                _URL,
                "--energy",
                "--energy-meter",
                "rapl",
                "--layer",
                "node",
                "--db",
                str(db),
            )
        assert res.exit_code == 0
        assert "override active" not in res.output

    def test_energy_without_meter_errors(self, tmp_path):
        res = _invoke(
            "--prometheus",
            _URL,
            "--energy",
            "--layer",
            "node",
            "--db",
            str(tmp_path / "obs.db"),
        )
        assert res.exit_code == 2
        assert "requires --energy-meter" in res.output

    def test_energy_without_prometheus_errors(self, tmp_path):
        res = _invoke(
            "--energy",
            "--energy-meter",
            "rapl",
            "--layer",
            "node",
            "--db",
            str(tmp_path / "obs.db"),
        )
        assert res.exit_code == 2
        assert "needs a source" in res.output

    def test_energy_without_layer_errors(self, tmp_path):
        res = _invoke(
            "--prometheus",
            _URL,
            "--energy",
            "--energy-meter",
            "rapl",
            "--db",
            str(tmp_path / "obs.db"),
        )
        assert res.exit_code == 2
        assert "requires --layer" in res.output

    def test_bad_meter_errors(self, tmp_path):
        res = _invoke(
            "--prometheus",
            _URL,
            "--energy",
            "--energy-meter",
            "manual",
            "--layer",
            "node",
            "--db",
            str(tmp_path / "obs.db"),
        )
        assert res.exit_code == 2
        assert "Invalid --energy-meter" in res.output

    def test_gate_refusal_writes_nothing_and_exits_nonzero(self, tmp_path):
        db = tmp_path / "obs.db"
        with patch(
            "presidio_arch_translucency.prometheus.fetch_energy_observation",
            side_effect=PrometheusError("no real power source detected: ..."),
        ):
            res = _invoke(
                "--prometheus",
                _URL,
                "--energy",
                "--energy-meter",
                "rapl",
                "--layer",
                "node",
                "--db",
                str(db),
            )
        assert res.exit_code != 0
        assert "Measured-energy collection failed" in res.output
        assert count_energy_observations(db_path=db) == 0


# ── list mode ─────────────────────────────────────────────────────────────────


class TestObserveEnergyList:
    def test_list_energy_observations(self, tmp_path):
        db = tmp_path / "obs.db"
        _record_energy_observation_unchecked(_fake(), db_path=db)
        res = _invoke("--energy", "--list", "--db", str(db))
        assert res.exit_code == 0
        assert "energy observations" in res.output.lower()

    def test_list_empty_store(self, tmp_path):
        res = _invoke("--energy", "--list", "--db", str(tmp_path / "obs.db"))
        assert res.exit_code == 0
        assert "No measured energy observations" in res.output


# ── verify: two sections + exit-code matrix ───────────────────────────────────


def _seed_energy(db, n=3):
    for i in range(n):
        _record_energy_observation_unchecked(
            EnergyObservation(
                _T0 + timedelta(minutes=i),
                100.0 + i,
                (100.0 + i) * 60,
                60.0,
                500.0,
                "node",
                1 + i,
                "rapl",
                "prometheus",
            ),
            db_path=db,
        )


def _seed_serving(db, n=3):
    for i in range(n):
        record_observation(
            Observation(
                _T0 + timedelta(minutes=i),
                500.0 + i,
                80.0,
                140.0,
                480.0,
                "container",
                6,
                "manual",
            ),
            db_path=db,
        )


def _verify(db, *extra):
    return runner.invoke(
        app, ["--skip-audit", "observe", "verify", "--db", str(db), *extra]
    )


class TestObserveVerifyBothChains:
    def test_two_sections_rendered(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_serving(db)
        _seed_energy(db)
        res = _verify(db)
        assert res.exit_code == 0
        assert "Serving observation chain" in res.output
        assert "Measured energy chain" in res.output

    def test_both_intact_exits_zero(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_serving(db)
        _seed_energy(db)
        assert _verify(db).exit_code == 0

    def test_energy_broken_exits_one(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_serving(db)
        _seed_energy(db)
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE energy_observations SET watts = 1 WHERE id = 2")
        conn.commit()
        conn.close()
        res = _verify(db)
        assert res.exit_code == 1
        assert "broken" in res.output.lower()

    def test_serving_broken_exits_one(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_serving(db)
        _seed_energy(db)
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE observations SET rps = 9999 WHERE id = 2")
        conn.commit()
        conn.close()
        assert _verify(db).exit_code == 1

    def test_serving_legacy_energy_ok_exits_two(self, tmp_path):
        db = tmp_path / "obs.db"
        # legacy serving prefix (no chain link), then a chained serving row
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
        _seed_serving(db, 1)
        _seed_energy(db)  # energy has no legacy by construction
        res = _verify(db)
        assert res.exit_code == 2
        assert "legacy" in res.output.lower()

    def test_allow_legacy_downgrades_to_zero(self, tmp_path):
        db = tmp_path / "obs.db"
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
        _seed_serving(db, 1)
        _seed_energy(db)
        assert _verify(db, "--allow-legacy").exit_code == 0

    def test_allow_legacy_does_not_mask_energy_break(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_serving(db)
        _seed_energy(db)
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE energy_observations SET watts = 1 WHERE id = 2")
        conn.commit()
        conn.close()
        assert _verify(db, "--allow-legacy").exit_code == 1

    def test_empty_store_exits_zero(self, tmp_path):
        db = tmp_path / "obs.db"
        res = _verify(db)
        assert res.exit_code == 0
        assert "nothing to verify" in res.output.lower()
