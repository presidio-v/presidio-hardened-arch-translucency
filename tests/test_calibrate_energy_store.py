"""Tests for `pat calibrate --energy-from-store` (v0.21.0).

The energy fit takes its inputs from the chained measured-energy store instead
of --energy-observation quads. The resulting record is identical in shape to a
v0.20 fit, so the calibration commitment binds it exactly the same way — a
store-fitted record must verify and tamper-fail.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.calibrate import (
    ENERGY_STORE_FIT_LATENCY_MS,
    CalibrationError,
    energy_observations_from_store,
    verify_commitment,
)
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import (
    EnergyObservation,
    _record_energy_observation_unchecked,
    load_energy_observations,
)

runner = CliRunner()
_T0 = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


_DEFAULT_POINTS = ((1, 60.0), (2, 110.0), (4, 210.0), (8, 410.0))


def _seed_energy(db, layer="node", points=_DEFAULT_POINTS):
    for i, (rep, watts) in enumerate(points):
        _record_energy_observation_unchecked(
            EnergyObservation(
                _T0 + timedelta(minutes=i),
                watts,
                watts * 60.0,
                60.0,
                500.0,
                layer,
                rep,
                "rapl",
                "prometheus",
            ),
            db_path=db,
        )


def _model_path(home):
    return home / ".pat" / "model.json"


def _energy_db(home):
    return home / ".pat" / "observations.db"


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "calibrate", *args])


class TestEnergyFromStore:
    def test_end_to_end_fit_written_and_verifies(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        db = _energy_db(tmp_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        _seed_energy(db)
        res = _invoke(
            "--observation",
            "100:50:2",
            "--observation",
            "300:80:5",
            "--energy-from-store",
        )
        assert res.exit_code == 0, res.output
        record = json.loads(_model_path(tmp_path).read_text())
        assert "energy_idle_w" in record
        assert "calibration_commitment" in record
        assert verify_commitment(record) is True

    def test_store_fitted_record_tamper_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        db = _energy_db(tmp_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        _seed_energy(db)
        _invoke(
            "--observation",
            "100:50:2",
            "--observation",
            "300:80:5",
            "--energy-from-store",
        )
        path = _model_path(tmp_path)
        record = json.loads(path.read_text())
        record["energy_idle_w"] = float(record["energy_idle_w"]) + 42.0
        path.write_text(json.dumps(record))
        assert verify_commitment(json.loads(path.read_text())) is False

    def test_mutually_exclusive_with_energy_observation(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        res = _invoke(
            "--observation",
            "300:80:5",
            "--energy-observation",
            "300:80:5:420",
            "--energy-from-store",
        )
        assert res.exit_code == 2
        # Rich may wrap the message; check for a stable substring.
        assert "mutually" in res.output and "exclusive" in res.output

    def test_empty_store_errors(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".pat").mkdir(parents=True, exist_ok=True)
        res = _invoke("--observation", "300:80:5", "--energy-from-store")
        assert res.exit_code == 2
        assert "no measured energy observations" in res.output.lower()

    def test_layer_filter_isolates_store_rows(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        db = _energy_db(tmp_path)
        db.parent.mkdir(parents=True, exist_ok=True)
        # Only 'container' rows, but we ask for layer 'node' -> no rows -> error.
        _seed_energy(db, layer="container")
        res = _invoke(
            "--layer", "node", "--observation", "300:80:5", "--energy-from-store"
        )
        assert res.exit_code == 2
        assert "no measured energy observations" in res.output.lower()


class TestMapping:
    def test_maps_rps_replicas_watts_with_sentinel_latency(self):
        rows = [
            EnergyObservation(
                _T0, 60.0, 3600.0, 60.0, 500.0, "node", 1, "rapl", "prometheus"
            ),
            EnergyObservation(
                _T0, 110.0, 6600.0, 60.0, 500.0, "node", 2, "rapl", "prometheus"
            ),
        ]
        mapped = energy_observations_from_store(rows)
        assert [m.replicas for m in mapped] == [1, 2]
        assert [m.watts for m in mapped] == [60.0, 110.0]
        assert all(m.latency_ms == ENERGY_STORE_FIT_LATENCY_MS for m in mapped)

    def test_non_numeric_store_value_raises_calibration_error(self, tmp_path):
        # A TEXT value injected into a numeric column raises a clear
        # CalibrationError, not a bare ValueError traceback (P3).
        db = tmp_path / "obs.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        _seed_energy(db)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "UPDATE energy_observations SET watts = 'not-a-number' WHERE id = 1"
        )
        conn.commit()
        conn.close()
        rows = load_energy_observations(db_path=db)
        with pytest.raises(CalibrationError, match="non-numeric"):
            energy_observations_from_store(rows)
