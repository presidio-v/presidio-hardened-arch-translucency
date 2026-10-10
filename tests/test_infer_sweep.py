"""Tests for per-layout SLO capacity validation from λ sweeps (infer_sweep.py)."""

import json
import math
import os
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.infer_calibrate import (
    HardwareSpec,
    InferenceCalibrationError,
    InferencePoint,
    fit_inference_calibration,
    load_inference_profile,
    predict_point,
    write_inference_profile,
)
from presidio_arch_translucency.infer_sweep import (
    BENCHMARK_SCHEMA,
    FAIL,
    PASS,
    Bracket,
    Slo,
    Sweep,
    capacity_error,
    classify_saturation,
    find_bracket,
    load_sweep,
    predict_capacity,
    slo_classifier,
    ttft_fraction_at,
    validate_sweeps,
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
PROMPT, OUTPUT = 900, 300


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


def _profile(name="p", hardware=HW):
    points = []
    for tp, batch in LAYOUTS:
        probe = InferencePoint(tp, float(batch), float(PROMPT), float(OUTPUT), 1.0, 1.0)
        pred = predict_point(probe, hardware, TRUE)
        points.append(
            InferencePoint(
                tp,
                float(batch),
                float(PROMPT),
                float(OUTPUT),
                pred.tpot_ms,
                pred.prefill_ms,
            )
        )
    write_inference_profile(name, fit_inference_calibration(points, hardware))
    return load_inference_profile(name)


def _level(lam, saturated=False, verdict="recorded", tpot=30.0, cdf=None):
    return {
        "level": lam,
        "verdict": verdict,
        "saturated": saturated,
        "point": {"tpot_ms": tpot} if verdict == "recorded" and tpot else None,
        "ttft_cdf": cdf,
    }


def _ladder(capacity, start=0.4, ratio=1.25, steps=8):
    """Levels at ×ratio spacing, saturated at and above the true capacity."""
    levels = []
    lam = start * capacity
    for _ in range(steps):
        levels.append(_level(round(lam, 6), saturated=lam >= capacity))
        if lam >= capacity:
            break
        lam *= ratio
    return levels


def _report(levels, tp=1, instances=None, model="m", error=None, mode="open"):
    config = {
        "model": model,
        "tp": tp,
        "prompt_tokens": PROMPT,
        "output_tokens": OUTPUT,
        "mode": mode,
    }
    if instances is not None:
        config["instances"] = instances
    return {
        "schema": BENCHMARK_SCHEMA,
        "config": config,
        "prompt_calibration": None,
        "stopped_early": True,
        "error": error,
        "levels": levels,
    }


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data))
    return str(path)


# ---------------------------------------------------------------------------
# Classification and brackets
# ---------------------------------------------------------------------------


def test_saturation_classifier():
    assert classify_saturation(_level(1.0)) == PASS
    assert classify_saturation(_level(1.0, verdict="refused")) == PASS
    assert classify_saturation(_level(1.0, saturated=True)) == FAIL
    # Failed requests without saturation prove nothing about capacity.
    assert classify_saturation({**_level(1.0, verdict="refused"), "errors": 2}) is None


def test_slo_classifier_tpot():
    classify = slo_classifier(Slo(tpot_ms=40.0))
    assert classify(_level(1.0, tpot=39.0)) == PASS
    assert classify(_level(1.0, tpot=41.0)) == FAIL
    assert classify(_level(1.0, saturated=True, verdict="refused")) == FAIL
    # Refused but not saturated tells nothing about the SLO.
    assert classify(_level(1.0, verdict="refused")) is None


def test_slo_classifier_ttft_on_bucket_edges():
    cdf = {"0.1": 0.5, "0.25": 0.991, "0.5": 1.0, "+Inf": 1.0}
    assert ttft_fraction_at(_level(1, cdf=cdf), 250.0) == 0.991
    assert ttft_fraction_at(_level(1, cdf=cdf), 300.0) is None
    assert ttft_fraction_at(_level(1), 250.0) is None
    assert slo_classifier(Slo(ttft_ms=250.0))(_level(1, cdf=cdf)) == PASS
    assert slo_classifier(Slo(ttft_ms=100.0))(_level(1, cdf=cdf)) == FAIL
    # Both SLOs: any failure fails, a missing metric with no failure is unknown.
    both = slo_classifier(Slo(tpot_ms=40.0, ttft_ms=250.0))
    assert both(_level(1, tpot=50.0)) == FAIL
    assert both(_level(1, tpot=30.0)) is None
    assert both(_level(1, tpot=30.0, cdf=cdf)) == PASS


@pytest.mark.parametrize(
    ("value", "message"),
    [(0.0, "must be in"), (math.inf, "must be in"), (True, "number"), ("40", "number")],
)
def test_slo_validation(value, message):
    with pytest.raises(InferenceCalibrationError, match=message):
        Slo(tpot_ms=value)


def test_bracket_monotone_sweep():
    levels = [_level(lam, saturated=lam >= 4.0) for lam in (2.0, 2.5, 3.125, 3.9, 4.9)]
    bracket = find_bracket(levels, classify_saturation)
    assert (bracket.lo, bracket.hi) == (3.9, 4.9)
    assert bracket.estimate == pytest.approx(math.sqrt(3.9 * 4.9))
    assert bracket.resolved and not bracket.non_monotone


def test_bracket_flags_non_monotone_and_uninformative_levels():
    classify = slo_classifier(Slo(tpot_ms=40.0))
    levels = [
        _level(1.0, tpot=30.0),
        _level(1.25, verdict="refused"),  # no information inside the bracket
        _level(1.6, tpot=45.0),
        _level(2.0, tpot=35.0),  # passes above the first failure
    ]
    bracket = find_bracket(levels, classify)
    assert (bracket.lo, bracket.hi) == (1.0, 1.6)
    assert bracket.non_monotone and bracket.uninformative_inside == 1


def test_bracket_open_ends():
    never = find_bracket([_level(1.0), _level(2.0)], classify_saturation)
    assert (never.lo, never.hi, never.resolved) == (2.0, None, False)
    assert never.estimate is None
    first = find_bracket(
        [_level(1.0, saturated=True), _level(2.0, saturated=True)],
        classify_saturation,
    )
    assert (first.lo, first.hi) == (None, 1.0)


# ---------------------------------------------------------------------------
# Error interval
# ---------------------------------------------------------------------------


def test_error_inside_bracket_is_zero_to_bracket():
    error = capacity_error(10.5, Bracket(10.0, 11.0, False, 0))
    assert error["to_bracket_pct"] == 0.0
    assert error["worst_pct"] == pytest.approx(5.0)
    assert error["verdict"] == "pass"


def test_error_verdicts():
    wide = Bracket(10.0, 12.5, False, 0)
    assert capacity_error(11.0, wide)["verdict"] == "pass"
    # Inside a bracket that is itself wider than the claim: inconclusive.
    assert capacity_error(10.0, Bracket(10.0, 15.0, False, 0))["verdict"] == (
        "inconclusive"
    )
    below = capacity_error(7.0, wide)
    assert below["to_bracket_pct"] == pytest.approx(30.0)
    assert below["verdict"] == "fail"
    above = capacity_error(16.0, wide)
    assert above["to_bracket_pct"] == pytest.approx(28.0)
    assert above["verdict"] == "fail"
    # An unrefined ×1.25 bracket passes a prediction up to 1.2·λ_lo only.
    assert capacity_error(12.0, wide)["verdict"] == "pass"
    assert capacity_error(12.4, wide)["verdict"] == "inconclusive"
    # A non-monotone sweep is suspect: never pass, never the kill criterion.
    assert capacity_error(11.0, Bracket(10.0, 12.5, True, 0))["verdict"] == (
        "inconclusive"
    )
    assert capacity_error(7.0, Bracket(10.0, 12.5, True, 0))["verdict"] == (
        "inconclusive"
    )
    # The capacity row is a mechanism check, worded as such.
    assert capacity_error(11.0, wide, role="mechanism")["verdict"] == "consistent"
    assert capacity_error(7.0, wide, role="mechanism")["verdict"] == "inconsistent"
    assert capacity_error(11.0, Bracket(10.0, None, False, 0)) == {
        "to_bracket_pct": None,
        "worst_pct": None,
        "verdict": "unresolved",
    }


# ---------------------------------------------------------------------------
# Predicted capacity
# ---------------------------------------------------------------------------


def test_predicted_capacity_is_the_recommenders_boundary():
    profile = _profile()
    open_slo = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, Slo())
    # Without SLOs the recommender's limit is ρ = 0.95.
    assert open_slo.lambda_slo == pytest.approx(0.95 * open_slo.lambda_sat, rel=0.006)
    assert not open_slo.extrapolated

    assert open_slo.binding == "utilization"

    tight = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, Slo(tpot_ms=60.0))
    assert 0.0 < tight.lambda_slo < open_slo.lambda_slo
    assert tight.lambda_sat == open_slo.lambda_sat
    assert tight.binding == "slo"
    loose = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, Slo(tpot_ms=10_000.0))
    assert loose.binding == "utilization"


def test_unattainable_slo_predicts_zero():
    profile = _profile()
    assert (
        predict_capacity(profile, 1, 1, PROMPT, OUTPUT, Slo(tpot_ms=1.0)).lambda_slo
        == 0.0
    )


def test_prediction_flags_extrapolation_and_infeasibility():
    profile = _profile()
    assert predict_capacity(profile, 8, 1, PROMPT, OUTPUT, Slo()).extrapolated
    assert predict_capacity(profile, 1, 2, PROMPT, OUTPUT, Slo()).extrapolated
    small = replace(profile, hardware=replace(HW, gpu_memory_gb=8.0))
    with pytest.raises(InferenceCalibrationError, match="infeasible"):
        predict_capacity(small, 1, 1, PROMPT, OUTPUT, Slo())


# ---------------------------------------------------------------------------
# Sweep files
# ---------------------------------------------------------------------------


def test_load_sweep_reads_shape_and_digest(tmp_path):
    path = _write(tmp_path, "s.json", _report([_level(1.0)], tp=2))
    sweep = load_sweep(path)
    assert (sweep.tp, sweep.instances, sweep.gpus, sweep.label) == (2, 1, 2, "tp2×1")
    assert len(sweep.sha256) == 64


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "schema"),
        ({**_report([_level(1.0)]), "schema": "other@1"}, "schema"),
        (_report([_level(1.0)], error="interrupted"), "ended with an error"),
        (_report([_level(1.0)], mode="closed"), "open-loop"),
        (_report([_level(1.0)], tp=3), "tp must be one of"),
        (_report([_level(1.0)], instances=0), "instances"),
        (_report([_level(1.0)], instances=2), "not measurable yet"),
        (_report([_level(1.0)], model=""), "no model"),
        (_report([_level(1.0)], model="[bold]x\x1b"), "printable"),
        (_report([_level(1.0)], model=7), "printable"),
        (_report([{**_level(1.0), "errors": -1}]), "error count"),
        (
            _report([{**_level(1.0), "ttft_cdf": {"0.25": 0.5, "0.250": 0.99}}]),
            "ttft_cdf",
        ),
        (_report([{**_level(1.0), "ttft_cdf": {"0.1": 0.9, "0.25": 0.5}}]), "ttft_cdf"),
        (_report([{**_level(1.0), "ttft_cdf": {"soon": 0.5}}]), "ttft_cdf"),
        (_report([{**_level(1.0), "ttft_cdf": ["0.1"]}]), "ttft_cdf"),
        (_report([]), "no levels"),
        (_report([{"level": -1.0}]), "positive rate"),
        (_report([{**_level(1.0), "verdict": "maybe"}]), "verdict"),
        (_report([{**_level(1.0), "saturated": "no"}]), "saturation flag"),
        (_report([{**_level(1.0), "point": {"tpot_ms": 0}}]), "TPOT"),
        (_report([{**_level(1.0), "ttft_cdf": {"0.1": 2.0}}]), "ttft_cdf"),
        (_report(["x"]), "not an object"),
    ],
)
def test_load_sweep_refuses_malformed_reports(tmp_path, data, message):
    with pytest.raises(InferenceCalibrationError, match=message):
        load_sweep(_write(tmp_path, "s.json", data))


def test_load_sweep_refuses_non_json_constants_and_links(tmp_path):
    bad = tmp_path / "nan.json"
    bad.write_text('{"schema": NaN}')
    with pytest.raises(InferenceCalibrationError, match="non-finite"):
        load_sweep(bad)
    dup = tmp_path / "dup.json"
    dup.write_text('{"a": 1, "a": 2}')
    with pytest.raises(InferenceCalibrationError, match="duplicate"):
        load_sweep(dup)
    target = _write(tmp_path, "real.json", _report([_level(1.0)]))
    link = tmp_path / "link.json"
    os.symlink(target, link)
    with pytest.raises(InferenceCalibrationError, match="symbolic link"):
        load_sweep(link)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validation_passes_when_the_engine_matches_the_model(tmp_path):
    profile = _profile()
    capacity = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, Slo()).lambda_sat
    # A refined bracket around the true capacity: ±3 %.
    levels = [_level(capacity * f, saturated=f >= 1.0) for f in (0.6, 0.97, 1.03)]
    sweep = load_sweep(_write(tmp_path, "s.json", _report(levels)))
    result = validate_sweeps(profile, [sweep], Slo())
    row = result["layouts"][0]
    assert row["saturation"]["role"] == "mechanism"
    assert row["saturation"]["error"]["verdict"] == "consistent"
    assert row["saturation"]["error"]["to_bracket_pct"] == 0.0
    assert row["slo"] is None and result["ranking"] == []


def test_validation_fails_when_capacity_is_far_off(tmp_path):
    profile = _profile()
    capacity = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, Slo()).lambda_sat
    sweep = load_sweep(_write(tmp_path, "s.json", _report(_ladder(capacity * 0.5))))
    row = validate_sweeps(profile, [sweep], Slo())["layouts"][0]
    assert row["saturation"]["error"]["verdict"] == "inconsistent"


def test_validation_with_slo_and_unrefined_spacing_is_inconclusive(tmp_path):
    profile = _profile()
    slo = Slo(tpot_ms=60.0)
    predicted = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, slo).lambda_slo
    # TPOT crosses 60 ms just below the prediction; ×1.25 spacing only, so
    # the prediction sits at the bracket's top edge, 25 % above its bottom.
    levels = [
        _level(lam, tpot=55.0 if lam < predicted * 0.99 else 65.0)
        for lam in (predicted * 0.64, predicted * 0.8, predicted * 1.0)
    ]
    sweep = load_sweep(_write(tmp_path, "s.json", _report(levels)))
    row = validate_sweeps(profile, [sweep], slo)["layouts"][0]
    assert row["slo"]["error"]["to_bracket_pct"] == 0.0
    assert row["slo"]["error"]["worst_pct"] == pytest.approx(25.0, abs=0.1)
    assert row["slo"]["error"]["verdict"] == "inconclusive"
    assert row["slo"]["binding"] == {"predicted": "slo", "measured": "slo"}
    assert row["slo"]["latency_tested"]


def test_slo_row_flags_what_was_actually_tested(tmp_path):
    profile = _profile()
    capacity = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, Slo()).lambda_sat
    # A loose SLO never binds: the model stops at ρ = 0.95, the sweep at
    # saturation. The verdict stands, but latency was not tested.
    loose = Slo(tpot_ms=10_000.0)
    levels = [_level(capacity * f, saturated=f >= 0.97) for f in (0.6, 0.94, 0.98)]
    sweep = load_sweep(_write(tmp_path, "a.json", _report(levels)))
    row = validate_sweeps(profile, [sweep], loose)["layouts"][0]
    assert row["slo"]["binding"] == {
        "predicted": "utilization",
        "measured": "utilization",
    }
    assert not row["slo"]["latency_tested"]
    assert row["slo"]["error"]["verdict"] == "pass"
    # The model says the SLO binds, but the sweep only ever saturated: the
    # two failure modes differ, so the comparison says nothing about latency.
    tight = Slo(tpot_ms=60.0)
    predicted = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, tight).lambda_slo
    levels = [
        _level(predicted * f, tpot=50.0, saturated=f >= 1.0) for f in (0.9, 0.98, 1.02)
    ]
    sweep = load_sweep(_write(tmp_path, "b.json", _report(levels)))
    row = validate_sweeps(profile, [sweep], tight)["layouts"][0]
    assert row["slo"]["binding"] == {"predicted": "slo", "measured": "utilization"}
    assert row["slo"]["error"]["verdict"] == "inconclusive"
    assert "first failed on utilization" in row["slo"]["error"]["reason"]


def test_ranking_at_equal_gpu_budget(tmp_path):
    profile = _profile()
    replica = predict_capacity(profile, 1, 2, PROMPT, OUTPUT, Slo()).lambda_sat
    tensor = predict_capacity(profile, 2, 1, PROMPT, OUTPUT, Slo()).lambda_sat
    # The model ranks tp2×1 just above tp1×2 (within 2 %): a measured order
    # needs brackets narrower than that gap, or it is withheld.
    assert tensor > replica

    def sweeps(replica_capacity, tensor_capacity):
        out = []
        for name, tp, instances, capacity in (
            ("r.json", 1, 2, replica_capacity),
            ("t.json", 2, 1, tensor_capacity),
        ):
            levels = [
                _level(capacity * f, saturated=f >= 1.0) for f in (0.5, 0.99, 1.01)
            ]
            out.append(
                Sweep(name, "0" * 64, "m", tp, instances, PROMPT, OUTPUT, levels)
            )
        return out

    def rank(replica_capacity, tensor_capacity):
        result = validate_sweeps(
            profile, sweeps(replica_capacity, tensor_capacity), Slo()
        )
        assert len(result["ranking"]) == 1
        return result["ranking"][0]

    agree = rank(replica * 0.9, tensor)
    assert agree["gpus"] == 2 and agree["predicted"] == ["tp2×1", "tp1×2"]
    assert agree["measured"] == ["tp2×1", "tp1×2"] and agree["agree"] is True
    disagree = rank(tensor * 1.1, tensor)
    assert disagree["measured"] == ["tp1×2", "tp2×1"] and disagree["agree"] is False
    overlap = rank(tensor, tensor)
    assert overlap["measured"] is None and overlap["agree"] is None
    assert "overlap" in overlap["reason"]


def test_ranking_withholds_tied_predictions_and_suspect_brackets():
    profile = _profile()
    capacity = predict_capacity(profile, 2, 1, PROMPT, OUTPUT, Slo()).lambda_sat

    def sweep(name, levels):
        return Sweep(name, "0" * 64, "m", 2, 1, PROMPT, OUTPUT, levels)

    clean = [_level(capacity * f, saturated=f >= 1.0) for f in (0.5, 0.99, 1.01)]
    low = [_level(capacity * f, saturated=f >= 0.8) for f in (0.5, 0.79, 0.81)]
    # The same layout twice: identical predictions are a tie, no order.
    tie = validate_sweeps(profile, [sweep("a", clean), sweep("b", low)], Slo())
    ranking = tie["ranking"][0]
    assert ranking["predicted"] is None and ranking["agree"] is None
    assert "within 1 %" in ranking["reason"]
    # A non-monotone bracket never orders the measurement.
    odd = [*low, _level(capacity * 0.9)]
    result = validate_sweeps(profile, [sweep("a", clean), sweep("b", odd)], Slo())
    assert result["ranking"][0]["measured"] is None


def test_validation_refuses_inconsistent_inputs(tmp_path):
    profile = _profile()
    a = load_sweep(_write(tmp_path, "a.json", _report([_level(1.0)])))
    other = _report([_level(1.0)])
    other["config"]["prompt_tokens"] = 1000
    b = load_sweep(_write(tmp_path, "b.json", other))
    with pytest.raises(InferenceCalibrationError, match="one workload"):
        validate_sweeps(profile, [a, b], Slo())
    with pytest.raises(InferenceCalibrationError, match="1–16"):
        validate_sweeps(profile, [], Slo())
    named = _profile("named", replace(HW, model_name="llama-8b"))
    with pytest.raises(InferenceCalibrationError, match="calibrated on"):
        validate_sweeps(named, [a], Slo())


def test_ttft_slo_must_be_a_bucket_edge(tmp_path):
    profile = _profile()
    plain = load_sweep(_write(tmp_path, "a.json", _report([_level(1.0)])))
    with pytest.raises(InferenceCalibrationError, match="needs ttft_cdf"):
        validate_sweeps(profile, [plain], Slo(ttft_ms=250.0))
    cdf = {"0.1": 0.9, "0.25": 1.0, "+Inf": 1.0}
    edged = load_sweep(_write(tmp_path, "b.json", _report([_level(1.0, cdf=cdf)])))
    with pytest.raises(InferenceCalibrationError, match="not a histogram bucket edge"):
        validate_sweeps(profile, [edged], Slo(ttft_ms=300.0))
    result = validate_sweeps(profile, [edged], Slo(ttft_ms=250.0))
    assert result["slo"]["ttft_ms"] == 250.0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

runner = CliRunner()


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "infer-validate", *args])


def test_cli_sweep_table_and_json(tmp_path):
    profile = _profile()
    capacity = predict_capacity(profile, 1, 1, PROMPT, OUTPUT, Slo()).lambda_sat
    levels = [_level(capacity * f, saturated=f >= 1.0) for f in (0.6, 0.97, 1.03)]
    path = _write(tmp_path, "s.json", _report(levels))
    table = _invoke("--calibration", "p", "--sweep", path, "--tpot-slo-ms", "60")
    assert table.exit_code == 0, table.output
    flat = " ".join(table.output.split())
    assert "tp1×1" in flat and "capacity (check)" in flat and "consistent" in flat
    assert "TPOT 60 ms" in flat

    out = _invoke("--calibration", "p", "--sweep", path, "--json")
    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)
    assert data["mode"] == "sweep" and data["commitment"] == profile.digest
    assert data["layouts"][0]["saturation"]["error"]["verdict"] == "consistent"


@pytest.mark.parametrize(
    "extra",
    [
        ["--leave-one-out"],
        ["--point", "tp=1,batch=4,prompt=900,output=300,tpot_ms=30"],
        ["--tpot-slo-ms", "0"],
    ],
)
def test_cli_sweep_refusals(tmp_path, extra):
    _profile()
    path = _write(tmp_path, "s.json", _report([_level(1.0)]))
    result = _invoke("--calibration", "p", "--sweep", path, *extra)
    assert result.exit_code == 2


def test_cli_slo_without_sweep_is_refused():
    _profile()
    result = _invoke("--calibration", "p", "--leave-one-out", "--tpot-slo-ms", "50")
    assert result.exit_code == 2
    assert "--sweep" in " ".join(result.output.split())
