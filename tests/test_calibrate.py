"""Tests for analytical model calibration (`pat calibrate`, v0.7.0)."""

import json

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.calibrate import (
    CalibrationError,
    Observation,
    fit_calibration,
    global_model_path,
    parse_observation,
    predict_rps,
    write_model_file,
)
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.model import resolve_concurrency

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def combined_output(result) -> str:
    """Runner stdout+stderr, robust across click versions.

    The click resolved on Python 3.9 mixes stderr into stdout and leaves
    ``stderr_bytes`` None, so ``result.stderr`` raises; newer click captures it
    apart. Append stderr only when it was captured separately.
    """
    text = result.output or ""
    if getattr(result, "stderr_bytes", None) is not None:
        text += result.stderr
    return text


# ── parse_observation ─────────────────────────────────────────────────────────


def test_parse_observation_valid() -> None:
    obs = parse_observation("300:80:5")
    assert obs == Observation(rps=300.0, latency_ms=80.0, replicas=5)


@pytest.mark.parametrize(
    "raw",
    ["300:80", "300:80:5:1", "abc:80:5", "300:80:0", "0:80:5", "300:-1:5"],
)
def test_parse_observation_invalid(raw: str) -> None:
    with pytest.raises(CalibrationError):
        parse_observation(raw)


# ── fit_calibration ───────────────────────────────────────────────────────────


def _synthetic(kappa: float, beta: float) -> list[Observation]:
    """Generate observations that exactly satisfy the calibration model."""
    points = [(50.0, 2), (80.0, 5), (40.0, 8), (100.0, 3), (60.0, 12)]
    return [
        Observation(
            rps=float(predict_rps(lat, rep, kappa, beta)), latency_ms=lat, replicas=rep
        )
        for lat, rep in points
    ]


def test_fit_recovers_known_parameters() -> None:
    obs = _synthetic(kappa=10.0, beta=0.03)
    result = fit_calibration(obs)
    assert result.concurrency == pytest.approx(10.0, rel=1e-4)
    assert result.overhead_beta == pytest.approx(0.03, abs=1e-4)
    assert result.r_squared == pytest.approx(1.0, abs=1e-6)
    assert result.rmse < 1e-6


def test_fit_recovers_second_parameter_set() -> None:
    obs = _synthetic(kappa=6.5, beta=0.01)
    result = fit_calibration(obs)
    assert result.concurrency == pytest.approx(6.5, rel=1e-3)
    assert result.overhead_beta == pytest.approx(0.01, abs=1e-3)


def test_fit_predictions_and_residuals_aligned() -> None:
    obs = _synthetic(kappa=8.0, beta=0.02)
    result = fit_calibration(obs)
    assert len(result.predictions) == len(obs)
    assert len(result.residuals) == len(obs)
    for o, pred, resid in zip(obs, result.predictions, result.residuals, strict=True):
        assert resid == pytest.approx(o.rps - pred, abs=1e-9)


def test_fit_single_observation_holds_beta() -> None:
    result = fit_calibration([Observation(rps=500.0, latency_ms=80.0, replicas=6)])
    assert result.concurrency > 0
    assert result.overhead_beta == pytest.approx(0.02)
    # one point is reproduced exactly
    assert result.residuals[0] == pytest.approx(0.0, abs=1e-6)


def test_fit_empty_raises() -> None:
    with pytest.raises(CalibrationError):
        fit_calibration([])


# ── write_model_file ──────────────────────────────────────────────────────────


def test_write_model_file_creates_global_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = fit_calibration(_synthetic(kappa=9.0, beta=0.02))
    path = write_model_file(result)
    assert path == global_model_path()
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["concurrency"] == pytest.approx(9.0, rel=1e-4)
    assert "overhead_beta" in payload
    assert "calibrated_at" in payload
    assert payload["observations"]


def test_write_then_resolve_concurrency(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    write_model_file(fit_calibration(_synthetic(kappa=11.0, beta=0.02)))
    assert resolve_concurrency() == pytest.approx(11.0, rel=1e-4)


# ── pat calibrate CLI ─────────────────────────────────────────────────────────


def test_calibrate_cmd_writes_model_and_reports_quality(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    obs = _synthetic(kappa=8.0, beta=0.02)
    args = ["calibrate"]
    for o in obs:
        args += ["--observation", f"{o.rps}:{o.latency_ms}:{o.replicas}"]
    result = invoke(*args)
    assert result.exit_code == 0
    assert "Calibration" in result.output
    assert "R²" in result.output
    assert (tmp_path / ".pat" / "model.json").is_file()


def test_calibrate_then_analyze_warning_suppressed(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    # Before calibration: warning present.
    before = invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
    assert "pat calibrate" in combined_output(before)

    cal = invoke(
        "calibrate",
        "--observation",
        "100:50:2",
        "--observation",
        "300:80:5",
        "--observation",
        "500:80:6",
    )
    assert cal.exit_code == 0

    # After calibration: warning suppressed and the fitted model is used.
    after = invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
    assert after.exit_code == 0
    assert "pat calibrate" not in combined_output(after)


def test_calibrate_cmd_invalid_observation_exits_2(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = invoke("calibrate", "--observation", "not-a-triple")
    assert result.exit_code == 2
    assert not (tmp_path / ".pat" / "model.json").exists()


def test_calibrate_cmd_requires_observation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = invoke("calibrate")
    assert result.exit_code != 0


# ── per-layer calibration (v0.9.0) ────────────────────────────────────────────


def test_write_model_file_per_layer_upserts(tmp_path, monkeypatch) -> None:
    """A named layer is written under layers.<name>; the global fit is preserved."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # Seed the global (pooled) fit first.
    global_fit = fit_calibration(_synthetic(kappa=9.0, beta=0.02))
    write_model_file(global_fit)

    api_fit = fit_calibration(_synthetic(kappa=12.0, beta=0.02))
    path = write_model_file(api_fit, layer="api")
    payload = json.loads(path.read_text(encoding="utf-8"))

    # Global params untouched, layer record added.
    assert payload["concurrency"] == pytest.approx(9.0, rel=1e-4)
    assert payload["layers"]["api"]["concurrency"] == pytest.approx(12.0, rel=1e-4)
    assert "calibrated_at" in payload["layers"]["api"]


def test_write_model_file_layers_independent(tmp_path, monkeypatch) -> None:
    """Writing one layer leaves previously-written layers untouched."""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_model_file(fit_calibration(_synthetic(kappa=9.0, beta=0.02)))
    write_model_file(fit_calibration(_synthetic(kappa=12.0, beta=0.02)), layer="api")
    path = write_model_file(
        fit_calibration(_synthetic(kappa=6.0, beta=0.02)), layer="worker"
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert set(payload["layers"]) == {"api", "worker"}
    assert payload["layers"]["api"]["concurrency"] == pytest.approx(12.0, rel=1e-4)
    assert payload["layers"]["worker"]["concurrency"] == pytest.approx(6.0, rel=1e-4)
    # Re-writing 'api' must not disturb 'worker'.
    write_model_file(fit_calibration(_synthetic(kappa=15.0, beta=0.02)), layer="api")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["layers"]["api"]["concurrency"] == pytest.approx(15.0, rel=1e-4)
    assert payload["layers"]["worker"]["concurrency"] == pytest.approx(6.0, rel=1e-4)


def test_write_global_preserves_existing_layers(tmp_path, monkeypatch) -> None:
    """Re-fitting the global (pooled) params keeps per-layer records intact."""
    monkeypatch.setenv("HOME", str(tmp_path))
    write_model_file(fit_calibration(_synthetic(kappa=9.0, beta=0.02)))
    write_model_file(fit_calibration(_synthetic(kappa=12.0, beta=0.02)), layer="api")
    path = write_model_file(fit_calibration(_synthetic(kappa=10.0, beta=0.02)))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["concurrency"] == pytest.approx(10.0, rel=1e-4)
    assert payload["layers"]["api"]["concurrency"] == pytest.approx(12.0, rel=1e-4)


def test_resolve_concurrency_layer_override_and_fallback(tmp_path, monkeypatch) -> None:
    """layer override → global fallback → default, per v0.9.0 model loading."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    write_model_file(fit_calibration(_synthetic(kappa=9.0, beta=0.02)))
    write_model_file(fit_calibration(_synthetic(kappa=12.0, beta=0.02)), layer="api")

    # Named layer resolves to its own fit.
    assert resolve_concurrency("api") == pytest.approx(12.0, rel=1e-4)
    # Unknown layer falls back to the global fit.
    assert resolve_concurrency("missing") == pytest.approx(9.0, rel=1e-4)
    # No layer / the reserved "default" both mean the global fit.
    assert resolve_concurrency() == pytest.approx(9.0, rel=1e-4)
    assert resolve_concurrency("default") == pytest.approx(9.0, rel=1e-4)


def test_resolve_concurrency_backward_compatible_no_layers(
    tmp_path, monkeypatch
) -> None:
    """A pre-v0.9.0 model file (no 'layers' key) still resolves, layer or not."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    legacy = global_model_path()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"concurrency": 3.5, "overhead_beta": 0.02}), encoding="utf-8"
    )
    assert resolve_concurrency() == pytest.approx(3.5)
    # A layer request on a layerless file falls back to the global value.
    assert resolve_concurrency("api") == pytest.approx(3.5)


def test_resolve_concurrency_no_model_returns_default(tmp_path, monkeypatch) -> None:
    from presidio_arch_translucency.model import DEFAULT_CONCURRENCY

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert resolve_concurrency("api") == DEFAULT_CONCURRENCY


def test_resolve_concurrency_bad_layer_record_falls_back(tmp_path, monkeypatch) -> None:
    """A malformed layer record is ignored; resolution falls back to global."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    path = global_model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "concurrency": 7.0,
                "layers": {
                    "api": {"concurrency": "not-a-number"},  # bad value
                    "broken": "not-a-dict",  # bad record type
                },
            }
        ),
        encoding="utf-8",
    )
    assert resolve_concurrency("api") == pytest.approx(7.0)
    assert resolve_concurrency("broken") == pytest.approx(7.0)


def test_write_model_file_corrupt_existing_is_replaced(tmp_path, monkeypatch) -> None:
    """A corrupt model file is treated as empty rather than crashing the write."""
    monkeypatch.setenv("HOME", str(tmp_path))
    path = global_model_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json", encoding="utf-8")
    # Per-layer write over a corrupt file must succeed and produce valid JSON.
    write_model_file(fit_calibration(_synthetic(kappa=8.0, beta=0.02)), layer="api")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["layers"]["api"]["concurrency"] == pytest.approx(8.0, rel=1e-4)


def test_calibrate_cmd_layer_writes_layers_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = invoke(
        "calibrate",
        "--layer",
        "api",
        "--observation",
        "200:40:3",
        "--observation",
        "600:55:8",
    )
    assert result.exit_code == 0
    # Final-line / panel mention the layer destination.
    assert "layers.api" in result.output
    payload = json.loads((tmp_path / ".pat" / "model.json").read_text(encoding="utf-8"))
    assert "api" in payload["layers"]


def test_calibrate_cmd_show_global(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    invoke("calibrate", "--observation", "100:50:2", "--observation", "300:80:5")
    result = invoke(
        "calibrate",
        "--layer",
        "api",
        "--observation",
        "200:40:3",
        "--observation",
        "600:55:8",
        "--show-global",
    )
    assert result.exit_code == 0
    assert "Global (pooled) fit" in result.output


def test_analyze_layer_uses_layer_calibration(tmp_path, monkeypatch) -> None:
    """`pat analyze --layer` picks the per-layer fit (no uncalibrated warning)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    invoke("calibrate", "--observation", "100:50:2", "--observation", "300:80:5")
    invoke(
        "calibrate",
        "--layer",
        "api",
        "--observation",
        "200:40:3",
        "--observation",
        "600:55:8",
    )
    result = invoke(
        "analyze", "-r", "500", "-l", "80", "-c", "container", "--layer", "api"
    )
    assert result.exit_code == 0
    assert "pat calibrate" not in combined_output(result)
