"""Tests for calibration commitments (v0.19.0).

A calibration commitment binds the fitted per-layer α/β to the observation set
that produced them: `pat calibrate` writes a SHA-256 over the calibration
inputs+outputs into the model file, and every model consumer (`pat analyze`)
re-hashes the stored parameters and **fails closed** if they no longer match.
Legacy model files (written before commitments) carry no commitment and are
reported as such, never rejected.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.calibrate import (
    Observation,
    commitment_digest,
    commitment_of,
    fit_calibration,
    global_model_path,
    verify_commitment,
    write_model_file,
)
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.model import (
    CalibrationTamperError,
    commitment_status,
    load_calibrated_model,
    resolve_calibration_commitment,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".pat").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


def _fit():
    return fit_calibration(
        [
            Observation(rps=100, latency_ms=50, replicas=2),
            Observation(rps=300, latency_ms=80, replicas=5),
        ]
    )


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


# ── digest determinism / roundtrip ────────────────────────────────────────────


def test_commitment_digest_is_deterministic():
    assert commitment_digest(_fit()) == commitment_digest(_fit())


def test_commitment_roundtrips_through_model_file():
    result = _fit()
    write_model_file(result)
    model = load_calibrated_model()
    stored = commitment_of(model)
    assert stored is not None
    # Stored digest equals a fresh digest over the same fit.
    assert stored == commitment_digest(result)
    assert verify_commitment(model) is True


def test_new_calibration_always_writes_commitment():
    write_model_file(_fit())
    raw = json.loads(global_model_path().read_text())
    assert "calibration_commitment" in raw
    assert raw["calibration_commitment"]["schema"].endswith("calibration-commitment@1")


def test_resolve_status_ok_after_calibration():
    write_model_file(_fit())
    status = resolve_calibration_commitment(None)
    assert status["status"] == "ok"
    assert status["digest"] == commitment_of(load_calibrated_model())


# ── tamper detection / fail closed ────────────────────────────────────────────


def _tamper(**changes) -> None:
    path = global_model_path()
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


def test_tampered_concurrency_fails_verify():
    write_model_file(_fit())
    _tamper(concurrency=999.0)
    assert verify_commitment(load_calibrated_model()) is False


def test_tampered_beta_fails_verify():
    write_model_file(_fit())
    _tamper(overhead_beta=0.4)
    assert verify_commitment(load_calibrated_model()) is False


def test_tampered_observations_fails_verify():
    write_model_file(_fit())
    _tamper(observations=[[1, 1, 1]])
    assert verify_commitment(load_calibrated_model()) is False


def test_resolve_raises_tamper_error_fail_closed():
    write_model_file(_fit())
    _tamper(concurrency=999.0)
    with pytest.raises(CalibrationTamperError):
        resolve_calibration_commitment(None)


def test_analyze_fails_closed_on_tampered_model():
    write_model_file(_fit())
    _tamper(concurrency=999.0)
    result = _invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
    assert result.exit_code == 2
    assert "tamper" in result.output.lower()


def test_analyze_includes_commitment_on_clean_model():
    write_model_file(_fit())
    result = _invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
    assert result.exit_code == 0
    assert "commitment" in result.output.lower()


# ── legacy handling: reported, not rejected ───────────────────────────────────


def test_legacy_model_reported_not_rejected():
    # A model file predating commitments (no commitment key, no observations).
    global_model_path().write_text('{"concurrency": 8.0, "overhead_beta": 0.02}')
    model = load_calibrated_model()
    assert commitment_of(model) is None
    assert verify_commitment(model) is False  # not "ok", but not a tamper
    status = resolve_calibration_commitment(None)
    assert status["status"] == "legacy"


def test_analyze_runs_on_legacy_model():
    global_model_path().write_text('{"concurrency": 8.0, "overhead_beta": 0.02}')
    result = _invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
    assert result.exit_code == 0
    assert "legacy" in result.output.lower()


def test_unknown_commitment_schema_is_tampered_and_rejected():
    write_model_file(_fit())
    path = global_model_path()
    raw = json.loads(path.read_text())
    raw["calibration_commitment"]["schema"] = (
        "presidio-hardened/calibration-commitment@999"
    )
    path.write_text(json.dumps(raw))

    model = load_calibrated_model()
    assert commitment_of(model) is None
    assert commitment_status(model) == "tampered"
    with pytest.raises(CalibrationTamperError):
        resolve_calibration_commitment()

    result = _invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
    assert result.exit_code == 2
    assert "tamper" in result.output.lower()


def test_per_layer_commitment_roundtrips():
    write_model_file(_fit(), layer="api")
    model = load_calibrated_model()
    layer_record = model["layers"]["api"]
    assert commitment_of(layer_record) is not None
    assert verify_commitment(layer_record) is True
    assert resolve_calibration_commitment("api")["status"] == "ok"


def test_per_layer_tamper_fails_closed():
    write_model_file(_fit(), layer="api")
    path = global_model_path()
    data = json.loads(path.read_text())
    data["layers"]["api"]["concurrency"] = 999.0
    path.write_text(json.dumps(data))
    with pytest.raises(CalibrationTamperError):
        resolve_calibration_commitment("api")
