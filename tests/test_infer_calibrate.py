"""Tests for inference calibration, profiles and validation (infer_calibrate.py)."""

import json
import math
import random
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.infer_calibrate import (
    COMMITMENT_KEY,
    HardwareSpec,
    InferenceCalibrationError,
    InferenceCalibrationTamperError,
    InferencePoint,
    build_profile_record,
    fit_inference_calibration,
    load_inference_profile,
    load_points_file,
    parse_point,
    point_context_tokens,
    predict_point,
    require_hardware_match,
    validate_holdout,
    validate_leave_one_out,
    validate_profile_name,
    write_inference_profile,
)
from presidio_arch_translucency.inference import DEFAULT_ENGINE_PARAMS

HW = HardwareSpec(
    weights_gb=16.0,
    kv_bytes_per_token=131_072.0,
    gpu_bandwidth_gbs=864.0,
    gpu_memory_gb=48.0,
)
TRUE = replace(
    DEFAULT_ENGINE_PARAMS,
    bandwidth_efficiency=0.72,
    step_overhead_ms=3.1,
    tensor_alpha=0.08,
    tensor_beta=0.15,
    prefill_tokens_per_s_per_gpu=14_000.0,
)
LAYOUTS = ((1, 4), (1, 48), (2, 8), (2, 64), (4, 16), (4, 96))


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


def _synthetic(tp, batch, noise=0.0, rng=None, prefill=True):
    """A point generated from TRUE, optionally with multiplicative noise."""
    probe = InferencePoint(tp, float(batch), 900.0, 300.0, 1.0, 1.0)
    pred = predict_point(probe, HW, TRUE)
    jitter = (lambda: 1.0 + rng.gauss(0.0, noise)) if noise else (lambda: 1.0)
    return InferencePoint(
        tp,
        float(batch),
        900.0,
        300.0,
        pred.tpot_ms * jitter(),
        pred.prefill_ms * jitter() if prefill else None,
    )


def _points(noise=0.0, seed=1):
    rng = random.Random(seed)  # noqa: S311 -- seeded test noise
    return [_synthetic(tp, b, noise, rng) for tp, b in LAYOUTS]


# ---------------------------------------------------------------------------
# Point parsing
# ---------------------------------------------------------------------------


def test_parse_point_short_and_long_keys():
    p = parse_point("tp=2,batch=24.5,prompt=1000,output=256,tpot_ms=35.1,prefill_ms=60")
    assert (p.tp, p.batch, p.prompt_tokens, p.output_tokens) == (2, 24.5, 1000, 256)
    assert p.prefill_ms == 60 and p.kv_usage is None
    q = parse_point(
        "tp=1,batch=4,prompt_tokens=10,output_tokens=5,tpot_ms=1,kv_usage=0.5"
    )
    assert q.kv_usage == 0.5


@pytest.mark.parametrize(
    "raw",
    [
        "tp=3,batch=1,prompt=1,output=1,tpot_ms=1",  # unsupported tp
        "tp=1,batch=1,prompt=1,output=1",  # missing tpot
        "tp=1,batch=1,prompt=1,output=1,tpot_ms=1,watts=3",  # unknown key
        "tp=1,batch=1,prompt=1,output=1,tpot_ms=nan",  # non-finite
        "tp=1,batch=1,prompt=1,output=1,tpot_ms=1,tpot_ms=2",  # duplicate
        "tp=1,batch=0,prompt=1,output=1,tpot_ms=1,kv_usage=0.3",  # usage needs batch
        "tp=1,batch=1,prompt=1,output=1,tpot_ms=1,kv_usage=1.5",  # usage > 1
        "tp=1;batch=1",  # malformed
    ],
)
def test_parse_point_rejects_junk(raw):
    with pytest.raises(InferenceCalibrationError):
        parse_point(raw)


def test_points_file_round_trip_and_rejections(tmp_path):
    good = tmp_path / "pts.jsonl"
    good.write_text(
        "\n".join(json.dumps(p.as_row()) for p in _points()) + "\n\n", encoding="utf-8"
    )
    assert load_points_file(good) == _points()

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"tp": 1, "batch": NaN}\n', encoding="utf-8")
    with pytest.raises(InferenceCalibrationError, match="line 1"):
        load_points_file(bad)

    dup = tmp_path / "dup.jsonl"
    dup.write_text('{"tp": 1, "tp": 2}\n', encoding="utf-8")
    with pytest.raises(InferenceCalibrationError, match="duplicate"):
        load_points_file(dup)

    link = tmp_path / "link.jsonl"
    link.symlink_to(good)
    with pytest.raises(InferenceCalibrationError, match="symbolic link"):
        load_points_file(link)

    with pytest.raises(InferenceCalibrationError):
        load_points_file(tmp_path / "missing.jsonl")


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def test_fit_recovers_ground_truth_exactly():
    result = fit_inference_calibration(_points(), HW)
    for name in (
        "bandwidth_efficiency",
        "step_overhead_ms",
        "tensor_alpha",
        "tensor_beta",
        "prefill_tokens_per_s_per_gpu",
    ):
        assert getattr(result.params, name) == pytest.approx(
            getattr(TRUE, name), rel=1e-4
        ), name
    assert result.held == ("replica_alpha",)
    assert result.at_bound == ()
    assert result.degrees_of_freedom == 2
    assert result.r_squared == pytest.approx(1.0)
    assert result.rmse_ms == pytest.approx(0.0, abs=1e-6)


def test_noisy_fit_predicts_held_out_tp8_within_ten_percent():
    """The paper's H3 shape: calibrate on tp ∈ {1,2,4}, predict tp=8."""
    rng = random.Random(7)  # noqa: S311 -- seeded test noise
    result = fit_inference_calibration(_points(noise=0.02, seed=7), HW)
    holdout = [_synthetic(8, b, 0.02, rng) for b in (32, 128)]
    report = validate_holdout(holdout, HW, result.params)
    assert report.tpot_mape_pct < 10.0


def test_noisy_fit_pins_collinear_alpha_at_bound_rather_than_failing():
    result = fit_inference_calibration(_points(noise=0.03, seed=1), HW)
    assert "tensor_alpha" in result.at_bound
    assert result.params.tensor_alpha == pytest.approx(0.0, abs=1e-6)


def test_single_multi_tp_level_holds_beta():
    pts = [p for p in _points() if p.tp in (1, 2)]
    result = fit_inference_calibration(pts, HW)
    assert "tensor_beta" in result.held
    assert result.params.tensor_beta == DEFAULT_ENGINE_PARAMS.tensor_beta
    assert result.degrees_of_freedom == 1
    assert result.r_squared is None  # fewer than 2 spare points


def test_prefill_held_without_prefill_points():
    rng = random.Random(0)  # noqa: S311 -- seeded test noise
    pts = [_synthetic(tp, b, rng=rng, prefill=False) for tp, b in LAYOUTS]
    result = fit_inference_calibration(pts, HW)
    assert "prefill_tokens_per_s_per_gpu" in result.held
    assert result.predictions[0].prefill_ms is None


@pytest.mark.parametrize(
    ("layouts", "match"),
    [
        (((1, 4), (2, 8), (2, 64), (4, 16)), "distinct mean batch"),
        (((1, 4), (1, 48), (1, 96)), "tp > 1"),
        (((1, 4), (1, 48), (2, 8)), "need ≥4 points"),
    ],
)
def test_fit_refuses_unidentifiable_point_sets(layouts, match):
    pts = [_synthetic(tp, b) for tp, b in layouts]
    with pytest.raises(InferenceCalibrationError, match=match):
        fit_inference_calibration(pts, HW)


def test_fit_refuses_flat_tpot():
    pts = [InferencePoint(tp, float(b), 900, 300, 20.0) for tp, b in LAYOUTS]
    with pytest.raises(
        InferenceCalibrationError, match="does not grow|bandwidth efficiency"
    ):
        fit_inference_calibration(pts, HW)


def test_fit_refuses_wrong_bandwidth_spec():
    """Points measured on ~860 GB/s cannot be explained by a 50 GB/s card."""
    slow = replace(HW, gpu_bandwidth_gbs=50.0)
    with pytest.raises(InferenceCalibrationError, match="bandwidth efficiency"):
        fit_inference_calibration(_points(), slow)


def test_kv_usage_gives_measured_context():
    capacity = (0.9 * 48.0 - 16.0) * 1e9 / 131_072.0
    usage = 1000.0 * 10.0 / capacity  # 1000 live tokens per sequence at b = 10
    p = InferencePoint(1, 10.0, 900, 300, 30.0, kv_usage=usage)
    assert point_context_tokens(p, HW) == pytest.approx(1000.0)
    q = InferencePoint(1, 10.0, 900, 300, 30.0)
    assert point_context_tokens(q, HW) == pytest.approx(900 + 150)


@pytest.mark.parametrize("usage", [1e-9, 0.5])
def test_kv_usage_outside_prompt_band_is_rejected(usage):
    """Live context below P (prompt resident) or above P + O is impossible."""
    p = InferencePoint(1, 10.0, 900, 300, 30.0, kv_usage=usage)
    with pytest.raises(InferenceCalibrationError, match="outside"):
        point_context_tokens(p, HW)


def test_rank_check_is_scale_free():
    """Huge contexts make the bytes column ~1e16; the fit must not falsely refuse."""
    big = HardwareSpec(16.0, 131_072.0, 100_000.0, 48.0)
    probe = replace(TRUE, tensor_beta=0.15)
    pts = []
    for tp, b in ((1, 2000), (1, 60_000), (2, 4000), (2, 50_000), (4, 30_000)):
        pred = predict_point(InferencePoint(tp, float(b), 1e6, 1e6, 1.0), big, probe)
        pts.append(InferencePoint(tp, float(b), 1e6, 1e6, pred.tpot_ms))
    result = fit_inference_calibration(pts, big)
    assert result.params.bandwidth_efficiency == pytest.approx(0.72, rel=1e-3)


def test_leave_one_out_skips_unidentifiable_folds():
    report = validate_leave_one_out(_points(noise=0.01), HW)
    # Dropping either tp=1 point leaves one batch level: those folds cannot fit.
    assert report.skipped_folds == 2
    assert report.degraded_folds == 0
    assert len(report.predictions) == 4
    assert report.tpot_mape_pct < 5.0


def test_leave_one_out_excludes_folds_that_fall_back_to_defaults():
    """Without (4, 96) the only tp=4 point's fold holds β: excluded, not scored."""
    pts = [p for p in _points() if (p.tp, p.batch) != (4, 96.0)]
    with pytest.raises(InferenceCalibrationError, match="no points"):
        validate_leave_one_out(pts, HW)  # every fold is skipped or degraded
    rng = random.Random(3)  # noqa: S311 -- seeded test noise
    pts = _points() + [_synthetic(4, 200, rng=rng)]
    pts = [p for p in pts if (p.tp, p.batch) != (4, 96.0)]
    report = validate_leave_one_out(pts + [_synthetic(1, 100)], HW)
    assert report.degraded_folds == 0


def test_validation_needs_points():
    with pytest.raises(InferenceCalibrationError):
        validate_holdout([], HW, TRUE)


# ---------------------------------------------------------------------------
# Committed profiles
# ---------------------------------------------------------------------------


def _model_file(home: Path) -> Path:
    return home / ".pat" / "model.json"


def test_profile_round_trip_and_sections_preserved(_home):
    path = _model_file(_home)
    path.parent.mkdir()
    path.write_text(json.dumps({"concurrency": 7, "training": {"data": {}}}))
    result = fit_inference_calibration(_points(), HW)
    write_inference_profile("l40s-8b", result)
    data = json.loads(path.read_text())
    assert data["concurrency"] == 7 and "training" in data
    profile = load_inference_profile("l40s-8b")
    assert profile.params.tensor_beta == pytest.approx(TRUE.tensor_beta, rel=1e-4)
    assert profile.hardware == HW
    assert len(profile.points) == len(LAYOUTS)
    assert len(profile.digest) == 64
    assert oct(path.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["params"].__setitem__("tensor_beta", 0.01),
        lambda r: r["points"][0].__setitem__("tpot_ms", 1.0),
        lambda r: r["hardware"].__setitem__("weights_gb", 15.0),
        lambda r: r.__setitem__("r_squared", 0.5),
        lambda r: r[COMMITMENT_KEY].__setitem__("digest", "0" * 64),
        lambda r: r.pop(COMMITMENT_KEY),
        lambda r: r[COMMITMENT_KEY].__setitem__("schema", "other@1"),
    ],
)
def test_tampered_profile_fails_closed(_home, mutate):
    write_inference_profile("p", fit_inference_calibration(_points(), HW))
    path = _model_file(_home)
    data = json.loads(path.read_text())
    mutate(data["inference"]["p"])
    path.write_text(json.dumps(data))
    with pytest.raises(InferenceCalibrationTamperError):
        load_inference_profile("p")


def test_project_local_model_file_neither_hides_nor_supplies_profiles(tmp_path):
    write_inference_profile("p", fit_inference_calibration(_points(), HW))
    good = load_inference_profile("p")
    # A repo's serving calibration (ADR-0005) must not hide the global profile...
    (tmp_path / ".pat-model.json").write_text(json.dumps({"concurrency": 8}))
    assert load_inference_profile("p").digest == good.digest
    # ...and a profile shipped inside a repo is never trusted.
    record = json.loads(
        json.dumps(build_profile_record("q", fit_inference_calibration(_points(), HW)))
    )
    (tmp_path / ".pat-model.json").write_text(json.dumps({"inference": {"q": record}}))
    with pytest.raises(InferenceCalibrationError, match="no inference calibration"):
        load_inference_profile("q")


def test_renamed_profile_is_refused(_home):
    write_inference_profile("p", fit_inference_calibration(_points(), HW))
    path = _model_file(_home)
    data = json.loads(path.read_text())
    data["inference"]["q"] = data["inference"]["p"]
    path.write_text(json.dumps(data))
    with pytest.raises(InferenceCalibrationTamperError, match="renamed"):
        load_inference_profile("q")


def test_missing_profile_is_an_error_not_a_default():
    with pytest.raises(InferenceCalibrationError, match="no inference calibration"):
        load_inference_profile("absent")


def test_hardware_mismatch_fails_closed():
    write_inference_profile("p", fit_inference_calibration(_points(), HW))
    profile = load_inference_profile("p")
    require_hardware_match(profile, 16.1, 131_072, 864, 48)  # within 1%
    with pytest.raises(InferenceCalibrationTamperError, match="weights"):
        require_hardware_match(profile, 47, 131_072, 864, 48)
    with pytest.raises(InferenceCalibrationTamperError, match="bandwidth"):
        require_hardware_match(profile, 16, 131_072, 3350, 48)


@pytest.mark.parametrize("name", ["", ".hidden", "a/b", "x" * 65, "café", "a b"])
def test_profile_name_validation(name):
    with pytest.raises(InferenceCalibrationError):
        validate_profile_name(name)


def test_corrupt_inference_section_is_refused(_home):
    path = _model_file(_home)
    path.parent.mkdir()
    path.write_text(json.dumps({"inference": [1, 2]}))
    with pytest.raises(InferenceCalibrationError, match="JSON object"):
        write_inference_profile("p", fit_inference_calibration(_points(), HW))


def test_hardware_spec_validation():
    with pytest.raises(InferenceCalibrationError):
        HardwareSpec(16, 131_072, 864, math.inf)
    with pytest.raises(InferenceCalibrationError):
        HardwareSpec(16, 131_072, 864, 48, max_num_seqs=True)
    with pytest.raises(InferenceCalibrationError):
        HardwareSpec(16, 131_072, 864, 48, gpu_type="\x1b[31m")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

runner = CliRunner()
_HW_ARGS = ["-w", "16", "-k", "131072", "-b", "864", "-m", "48"]
_ANALYZE = ["-r", "10", "-p", "900", "-o", "300", "-n", "4", *_HW_ARGS]


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def _write_points(tmp_path, points) -> str:
    path = tmp_path / "pts.jsonl"
    path.write_text("\n".join(json.dumps(p.as_row()) for p in points))
    return str(path)


def test_cli_calibrate_validate_analyze_flow(tmp_path):
    pts = _write_points(tmp_path, _points(noise=0.01))
    result = _invoke(
        "infer-calibrate", "--profile", "l40s", "--points-file", pts, *_HW_ARGS,
        "--gpu-type", "L40S", "--json",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["profile"] == "l40s" and len(data["digest"]) == 64
    assert data["degrees_of_freedom"] == 2

    loo = _invoke(
        "infer-validate", "--calibration", "l40s", "--leave-one-out", "--json"
    )
    assert loo.exit_code == 0, loo.output
    assert json.loads(loo.output)["skipped_folds"] == 2

    hold = _invoke(
        "infer-validate", "--calibration", "l40s", "--point",
        "tp=8,batch=32,prompt=900,output=300,tpot_ms=15",
    )  # fmt: skip
    assert hold.exit_code == 0, hold.output
    assert "TPOT MAPE" in hold.output

    analyzed = _invoke("infer-analyze", *_ANALYZE, "--calibration", "l40s", "--json")
    assert analyzed.exit_code == 0, analyzed.output
    payload = json.loads(analyzed.output)
    assert payload["calibrated"] is True
    assert payload["calibration"]["digest"] == data["digest"]

    calibration = payload["calibration"]
    assert calibration["calibrated_tp_max"] == 4
    flags = {(c["tp"], c["instances"]): c["extrapolated"] for c in payload["configs"]}
    assert flags[(2, 1)] is False
    assert flags[(1, 2)] is True  # n ≥ 2: router cost unobserved

    wide = _invoke(
        "infer-analyze", "-r", "10", "-p", "900", "-o", "300", "-n", "8", *_HW_ARGS,
        "--calibration", "l40s", "--json", "--gpu-memory-utilization", "0.95",
    )  # fmt: skip
    wide_payload = json.loads(wide.output)
    assert {c["extrapolated"] for c in wide_payload["configs"] if c["tp"] == 8} == {
        True
    }
    assert wide_payload["calibration"]["gpu_memory_utilization"] == {
        "profile": 0.9,
        "analysed": 0.95,
    }

    table = _invoke(
        "infer-analyze", *_ANALYZE, "--calibration", "l40s", "--max-num-seqs", "64"
    )
    text = " ".join(table.output.split())
    assert "Calibrated: profile 'l40s'" in text
    assert "max_num_seqs: profile 256, analysed 64" in text

    what_if = _invoke(
        "infer-what-if", "--tp", "2", "-r", "5", "-p", "900", "-o", "300", *_HW_ARGS,
        "--calibration", "l40s", "--json",
    )  # fmt: skip
    assert what_if.exit_code == 0, what_if.output
    assert json.loads(what_if.output)["calibrated"] is True


def test_cli_calibrate_table_and_dry_run(tmp_path, _home):
    pts = _write_points(tmp_path, _points(noise=0.03))
    result = _invoke(
        "infer-calibrate", "--profile", "x", "--points-file", pts, *_HW_ARGS,
        "--dry-run",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "fitted, at bound" in result.output
    assert "Dry run" in result.output
    assert not _model_file(_home).exists()


def test_cli_calibrate_inline_points_r2_suppressed():
    args = []
    for p in _points()[:4]:  # tp ∈ {1, 2}: β held, 3 free params, dof 1
        args += ["--point", ",".join(f"{k}={v}" for k, v in p.as_row().items())]
    result = _invoke("infer-calibrate", "--profile", "y", *args, *_HW_ARGS)
    assert result.exit_code == 0, result.output
    assert "R² not reported" in result.output
    assert "held at default" in result.output


def test_cli_calibrate_errors():
    no_points = _invoke("infer-calibrate", "--profile", "z", *_HW_ARGS)
    assert no_points.exit_code == 2
    bad_name = _invoke(
        "infer-calibrate", "--profile", "../x", "--point", "tp=1", *_HW_ARGS
    )
    assert bad_name.exit_code == 2


def test_cli_analyze_calibration_refusals(tmp_path, _home):
    pts = _write_points(tmp_path, _points())
    assert (
        _invoke(
            "infer-calibrate", "--profile", "p", "--points-file", pts, *_HW_ARGS
        ).exit_code
        == 0
    )
    mismatch = _invoke(
        "infer-analyze", "-r", "1", "-p", "900", "-o", "300", "-n", "4",
        "-w", "47", "-k", "131072", "-b", "864", "-m", "48", "--calibration", "p",
    )  # fmt: skip
    assert mismatch.exit_code == 2
    assert "refused" in mismatch.output

    conflict = _invoke(
        "infer-analyze", *_ANALYZE, "--calibration", "p", "--step-overhead-ms", "1"
    )
    assert conflict.exit_code == 2
    assert "cannot be combined" in conflict.output

    missing = _invoke("infer-analyze", *_ANALYZE, "--calibration", "nope")
    assert missing.exit_code == 2

    path = _model_file(_home)
    data = json.loads(path.read_text())
    data["inference"]["p"]["params"]["tensor_alpha"] = 0.2
    path.write_text(json.dumps(data))
    tampered = _invoke("infer-analyze", *_ANALYZE, "--calibration", "p")
    assert tampered.exit_code == 2
    assert "does not match its calibration commitment" in " ".join(
        tampered.output.split()
    )
    assert (
        _invoke("infer-validate", "--calibration", "p", "--leave-one-out").exit_code
        == 2
    )


def test_cli_validate_mode_conflict(tmp_path):
    pts = _write_points(tmp_path, _points())
    _invoke("infer-calibrate", "--profile", "p", "--points-file", pts, *_HW_ARGS)
    both = _invoke(
        "infer-validate", "--calibration", "p", "--leave-one-out", "--points-file", pts
    )
    assert both.exit_code == 2


def test_cli_engine_override_without_calibration():
    result = _invoke(
        "infer-analyze", *_ANALYZE, "--bandwidth-efficiency", "0.9", "--json"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["calibrated"] is False
