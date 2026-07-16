"""Tests for training-calibration from step-time logs (L-TR-1, v0.23.0).

Covers step-log ingestion (happy path + every malformed class + bounds), the
per-strategy fit (synthetic recovery, degree requirements, energy aggregation),
and the committed training-fit record (roundtrip, tamper-each-field fail-closed,
legacy compat, section preservation, no cross-contamination with the serving
commitment).
"""

from __future__ import annotations

import json
import math

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.calibrate import (
    Observation,
    fit_calibration,
    training_commitment_of,
    verify_commitment,
    verify_training_commitment,
    write_model_file,
)
from presidio_arch_translucency.model import load_calibrated_model
from presidio_arch_translucency.train_calibrate import (
    MAX_STEP_LOG_LINES,
    StepLog,
    StepLogError,
    TrainingCalibrationError,
    fit_training_calibration,
    parse_step_log,
    write_training_fit,
)
from presidio_arch_translucency.training import (
    STRATEGY_PARAMS,
    ParallelismStrategy,
    TrainingCalibrationTamperError,
    resolve_strategy_params,
    resolve_training_commitment,
    resolve_training_energy,
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".pat").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


# ── step-log helpers ──────────────────────────────────────────────────────────


def _write_log(path, rows) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _row(step, duration_s, samples, power_w=None):
    obj = {"step": step, "duration_s": duration_s, "samples": samples}
    if power_w is not None:
        obj["power_w"] = power_w
    return obj


# ── parse_step_log: happy path & aggregates ───────────────────────────────────


def test_parse_step_log_happy_path(tmp_path):
    p = tmp_path / "log.jsonl"
    _write_log(p, [_row(0, 2.0, 20), _row(1, 4.0, 40)])
    log = parse_step_log(p)
    assert log.total_samples == 60
    assert log.total_duration_s == pytest.approx(6.0)
    assert log.samples_per_second == pytest.approx(10.0)
    assert log.mean_power_w is None
    assert log.line_count == 2


def test_parse_step_log_duration_weighted_power(tmp_path):
    p = tmp_path / "log.jsonl"
    # Duration-weighted mean of 100 (for 1s) and 200 (for 3s) = (100+600)/4 = 175.
    _write_log(p, [_row(0, 1.0, 5, power_w=100.0), _row(1, 3.0, 15, power_w=200.0)])
    log = parse_step_log(p)
    assert log.mean_power_w == pytest.approx(175.0)


def test_parse_step_log_skips_blank_lines(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text(
        json.dumps(_row(0, 2.0, 20))
        + "\n\n   \n"
        + json.dumps(_row(1, 2.0, 20))
        + "\n",
        encoding="utf-8",
    )
    log = parse_step_log(p)
    assert log.line_count == 2


# ── parse_step_log: malformed classes ─────────────────────────────────────────


def test_parse_step_log_not_a_file(tmp_path):
    with pytest.raises(StepLogError, match="regular file"):
        parse_step_log(tmp_path / "missing.jsonl")


def test_parse_step_log_rejects_symbolic_link(tmp_path):
    target = tmp_path / "target.jsonl"
    _write_log(target, [_row(0, 1.0, 1)])
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(StepLogError, match="symbolic link"):
        parse_step_log(link)


@pytest.mark.parametrize("steps", [[0, 0], [2, 1]])
def test_parse_step_log_rejects_duplicate_or_reordered_steps(tmp_path, steps):
    p = tmp_path / "log.jsonl"
    _write_log(p, [_row(step, 1.0, 1) for step in steps])
    with pytest.raises(StepLogError, match="strictly increasing"):
        parse_step_log(p)


def test_parse_step_log_bad_json_names_line(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text(json.dumps(_row(0, 2.0, 20)) + "\nnot json\n", encoding="utf-8")
    with pytest.raises(StepLogError, match="line 2"):
        parse_step_log(p)


def test_parse_step_log_non_object_line(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text("[1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(StepLogError, match="JSON object"):
        parse_step_log(p)


def test_parse_step_log_unknown_key_rejected(tmp_path):
    p = tmp_path / "log.jsonl"
    # A typo'd "power" must not silently vanish.
    p.write_text(
        json.dumps({"step": 0, "duration_s": 2.0, "samples": 20, "power": 100.0})
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(StepLogError, match="unknown key"):
        parse_step_log(p)


def test_parse_step_log_duplicate_key_rejected(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text(
        '{"step":0,"duration_s":1,"samples":1,"samples":999}\n',
        encoding="utf-8",
    )
    with pytest.raises(StepLogError, match="duplicate key"):
        parse_step_log(p)


@pytest.mark.parametrize("key", ["step", "duration_s", "samples"])
def test_parse_step_log_missing_required_key(tmp_path, key):
    obj = _row(0, 2.0, 20)
    del obj[key]
    p = tmp_path / "log.jsonl"
    p.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    with pytest.raises(StepLogError, match="missing required key"):
        parse_step_log(p)


@pytest.mark.parametrize(
    "obj",
    [
        {"step": 1.5, "duration_s": 2.0, "samples": 20},  # step not int
        {"step": 0, "duration_s": 2.0, "samples": 2.5},  # samples not int
        {"step": 0, "duration_s": "2", "samples": 20},  # duration not number
        {"step": True, "duration_s": 2.0, "samples": 20},  # bool rejected
    ],
)
def test_parse_step_log_wrong_types(tmp_path, obj):
    p = tmp_path / "log.jsonl"
    p.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    with pytest.raises(StepLogError):
        parse_step_log(p)


@pytest.mark.parametrize(
    "obj",
    [
        {"step": -1, "duration_s": 2.0, "samples": 20},  # step < 0
        {"step": 0, "duration_s": 0.0, "samples": 20},  # duration not > 0
        {"step": 0, "duration_s": -2.0, "samples": 20},  # duration negative
        {"step": 0, "duration_s": 2.0, "samples": 0},  # samples < 1
        {"step": 0, "duration_s": 2.0, "samples": 20, "power_w": 0.0},  # power not > 0
        {"step": 0, "duration_s": 2.0, "samples": 20, "power_w": -5.0},  # power neg
    ],
)
def test_parse_step_log_out_of_bounds(tmp_path, obj):
    p = tmp_path / "log.jsonl"
    p.write_text(json.dumps(obj) + "\n", encoding="utf-8")
    with pytest.raises(StepLogError):
        parse_step_log(p)


def test_parse_step_log_rejects_nan_inf(tmp_path):
    p = tmp_path / "log.jsonl"
    # NaN/Inf are non-standard JSON literals accepted by json.loads by default.
    p.write_text('{"step": 0, "duration_s": NaN, "samples": 20}\n', encoding="utf-8")
    with pytest.raises(StepLogError):
        parse_step_log(p)
    p.write_text(
        '{"step": 0, "duration_s": 2.0, "samples": 20, "power_w": Infinity}\n',
        encoding="utf-8",
    )
    with pytest.raises(StepLogError):
        parse_step_log(p)


def test_parse_step_log_empty_file(tmp_path):
    p = tmp_path / "log.jsonl"
    p.write_text("\n  \n", encoding="utf-8")
    with pytest.raises(StepLogError, match="no data lines"):
        parse_step_log(p)


def test_parse_step_log_partial_power_coverage(tmp_path):
    p = tmp_path / "log.jsonl"
    _write_log(p, [_row(0, 2.0, 20, power_w=100.0), _row(1, 2.0, 20)])
    with pytest.raises(StepLogError, match="partial power"):
        parse_step_log(p)


def test_parse_step_log_size_bound(tmp_path, monkeypatch):
    monkeypatch.setitem(parse_step_log.__globals__, "MAX_STEP_LOG_BYTES", 10)
    p = tmp_path / "log.jsonl"
    _write_log(p, [_row(0, 2.0, 20)])
    with pytest.raises(StepLogError, match="exceeds"):
        parse_step_log(p)


def test_parse_step_log_line_count_bound(tmp_path, monkeypatch):
    monkeypatch.setitem(parse_step_log.__globals__, "MAX_STEP_LOG_LINES", 2)
    p = tmp_path / "log.jsonl"
    _write_log(p, [_row(i, 2.0, 20) for i in range(3)])
    with pytest.raises(StepLogError, match="lines"):
        parse_step_log(p)


def test_max_step_log_lines_is_large_by_default():
    assert MAX_STEP_LOG_LINES == 100_000


# ── fit: synthetic recovery ───────────────────────────────────────────────────


def _synth_log(strategy, degree, baseline, alpha, beta, power=None, microbatches=8):
    """A StepLog whose samples/s equals the model throughput at *degree*."""
    if strategy is ParallelismStrategy.PIPELINE:
        bubble = 1.0 if degree <= 1 else microbatches / (microbatches + degree - 1)
        eff = 1.0 if degree <= 1 else (1.0 - alpha) * bubble
    else:
        eff = 1.0 if degree <= 1 else max(0.0, 1.0 - alpha - beta * math.log(degree))
    tp = baseline * degree * eff
    duration = 10.0
    return StepLog(
        total_samples=int(round(tp * duration)),
        total_duration_s=duration,
        samples_per_second=tp,
        mean_power_w=power,
        line_count=1,
    )


@pytest.mark.parametrize(
    "strategy", [ParallelismStrategy.DATA, ParallelismStrategy.FSDP]
)
def test_fit_recovers_alpha_beta_strategy(strategy):
    d = STRATEGY_PARAMS[strategy]
    baseline, alpha, beta = 100.0, d.overhead_alpha, d.overhead_beta
    runs = [
        (deg, _synth_log(strategy, deg, baseline, alpha, beta)) for deg in (1, 2, 4, 8)
    ]
    res = fit_training_calibration(strategy, runs)
    assert res.r_squared > 0.999
    assert res.overhead_alpha == pytest.approx(alpha, abs=0.02)
    assert res.overhead_beta == pytest.approx(beta, abs=0.02)
    assert res.baseline_samples_per_second == pytest.approx(baseline, rel=0.05)
    # Predictions track the observed throughput tightly.
    for run, pred in zip(res.runs, res.predictions, strict=True):
        assert pred == pytest.approx(run.samples_per_second, rel=0.02)


def test_parse_step_log_deeply_nested_json_fails_closed(tmp_path):
    """RecursionError from json.loads is re-raised as StepLogError.

    A pathologically nested line inside the size bounds must honour the
    fail-closed ingestion contract (StepLogError naming the line), not escape
    as a RecursionError traceback (pre-audit review P2).
    """
    p = tmp_path / "deep.jsonl"
    p.write_text("[" * 100_000 + "]" * 100_000 + "\n", encoding="utf-8")
    with pytest.raises(StepLogError, match="line 1.*nested"):
        parse_step_log(p)


def test_fit_saturated_flag_set_on_exactly_determined_fits():
    """2 distinct degrees / 2 free params → saturated; 4 degrees → not."""
    strategy = ParallelismStrategy.DATA
    d = STRATEGY_PARAMS[strategy]
    two = [
        (deg, _synth_log(strategy, deg, 100.0, d.overhead_alpha, d.overhead_beta))
        for deg in (1, 4)
    ]
    four = [
        (deg, _synth_log(strategy, deg, 100.0, d.overhead_alpha, d.overhead_beta))
        for deg in (1, 2, 4, 8)
    ]
    assert fit_training_calibration(strategy, two).saturated is True
    assert fit_training_calibration(strategy, four).saturated is False
    # Pipeline: 2 free params — saturated at exactly 2 distinct degrees.
    pipe = ParallelismStrategy.PIPELINE
    pd = STRATEGY_PARAMS[pipe]
    pipe_two = [
        (deg, _synth_log(pipe, deg, 100.0, pd.overhead_alpha, 0.0)) for deg in (1, 4)
    ]
    assert fit_training_calibration(pipe, pipe_two).saturated is True


def test_fit_recovers_pipeline_bubble():
    strategy = ParallelismStrategy.PIPELINE
    d = STRATEGY_PARAMS[strategy]
    baseline, alpha = 100.0, d.overhead_alpha
    runs = [
        (deg, _synth_log(strategy, deg, baseline, alpha, 0.0)) for deg in (1, 2, 4, 8)
    ]
    res = fit_training_calibration(strategy, runs)
    assert res.r_squared > 0.999
    assert res.overhead_beta == 0.0  # unused for pipeline
    assert res.baseline_samples_per_second == pytest.approx(baseline, rel=0.02)
    assert res.overhead_alpha == pytest.approx(alpha, abs=0.02)


def test_fit_two_degrees_holds_beta_at_default():
    strategy = ParallelismStrategy.DATA
    d = STRATEGY_PARAMS[strategy]
    runs = [
        (deg, _synth_log(strategy, deg, 100.0, d.overhead_alpha, d.overhead_beta))
        for deg in (1, 4)
    ]
    res = fit_training_calibration(strategy, runs)
    assert res.overhead_beta == pytest.approx(d.overhead_beta)  # held at default


def test_fit_requires_distinct_degrees():
    strategy = ParallelismStrategy.DATA
    log = _synth_log(strategy, 2, 100.0, 0.02, 0.03)
    with pytest.raises(TrainingCalibrationError, match="distinct"):
        fit_training_calibration(strategy, [(2, log), (2, log)])


def test_fit_requires_degree_one_anchor():
    strategy = ParallelismStrategy.DATA
    runs = [(deg, _synth_log(strategy, deg, 100.0, 0.02, 0.03)) for deg in (2, 4, 8)]
    with pytest.raises(TrainingCalibrationError, match="degree-1 anchor"):
        fit_training_calibration(strategy, runs)


def test_fit_single_degree_errors():
    strategy = ParallelismStrategy.DATA
    log = _synth_log(strategy, 2, 100.0, 0.02, 0.03)
    with pytest.raises(TrainingCalibrationError, match="degree-1 anchor"):
        fit_training_calibration(strategy, [(2, log)])


def test_fit_pipeline_single_degree_errors():
    strategy = ParallelismStrategy.PIPELINE
    log = _synth_log(strategy, 2, 100.0, 0.03, 0.0)
    with pytest.raises(TrainingCalibrationError, match="degree-1 anchor"):
        fit_training_calibration(strategy, [(2, log)])


def test_fit_degree_out_of_range():
    strategy = ParallelismStrategy.TENSOR  # max_degree 8
    log = _synth_log(strategy, 2, 100.0, 0.05, 0.12)
    with pytest.raises(TrainingCalibrationError, match="out of range"):
        fit_training_calibration(strategy, [(1, log), (99, log)])


def test_fit_empty_runs_errors():
    with pytest.raises(TrainingCalibrationError):
        fit_training_calibration(ParallelismStrategy.DATA, [])


# ── fit: energy aggregation ───────────────────────────────────────────────────


def test_fit_energy_aggregation_all_powered():
    strategy = ParallelismStrategy.DATA
    runs = [
        (deg, _synth_log(strategy, deg, 100.0, 0.02, 0.03, power=300.0 * deg))
        for deg in (1, 2, 4)
    ]
    res = fit_training_calibration(strategy, runs)
    # watts_per_device = mean over runs of power/degree = mean(300,300,300) = 300.
    assert res.watts_per_device == pytest.approx(300.0)
    # mean_power_w is the duration-weighted mean of 300, 600, 1200 (equal durations).
    assert res.mean_power_w == pytest.approx((300 + 600 + 1200) / 3)


def test_fit_no_power_no_energy_keys():
    strategy = ParallelismStrategy.DATA
    runs = [(deg, _synth_log(strategy, deg, 100.0, 0.02, 0.03)) for deg in (1, 2, 4)]
    res = fit_training_calibration(strategy, runs)
    assert res.mean_power_w is None
    assert res.watts_per_device is None


def test_fit_partial_power_across_runs_errors():
    strategy = ParallelismStrategy.DATA
    runs = [
        (1, _synth_log(strategy, 1, 100.0, 0.02, 0.03, power=300.0)),
        (2, _synth_log(strategy, 2, 100.0, 0.02, 0.03)),  # no power
    ]
    with pytest.raises(TrainingCalibrationError, match="partial power"):
        fit_training_calibration(strategy, runs)


def test_fit_rejects_excessive_per_device_power():
    strategy = ParallelismStrategy.DATA
    runs = [
        (1, _synth_log(strategy, 1, 100.0, 0.02, 0.03, power=2001.0)),
        (2, _synth_log(strategy, 2, 100.0, 0.02, 0.03, power=4002.0)),
    ]
    with pytest.raises(TrainingCalibrationError, match="per device"):
        fit_training_calibration(strategy, runs)


# ── committed record: roundtrip / write / preserve ────────────────────────────


def _fit(strategy=ParallelismStrategy.DATA, power=None):
    runs = [
        (deg, _synth_log(strategy, deg, 100.0, 0.02, 0.03, power=power and power * deg))
        for deg in (1, 2, 4, 8)
    ]
    return fit_training_calibration(strategy, runs)


def test_write_training_fit_roundtrips_commitment():
    res = _fit()
    write_training_fit(ParallelismStrategy.DATA, res)
    model = load_calibrated_model()
    record = model["training"]["data"]
    assert training_commitment_of(record) is not None
    assert verify_training_commitment(record) is True
    assert resolve_training_commitment(ParallelismStrategy.DATA)["status"] == "ok"


def test_write_training_fit_energy_roundtrips():
    res = _fit(power=300.0)
    write_training_fit(ParallelismStrategy.DATA, res)
    record = load_calibrated_model()["training"]["data"]
    assert "mean_power_w" in record and "watts_per_device" in record
    assert verify_training_commitment(record) is True
    energy = resolve_training_energy(ParallelismStrategy.DATA)
    assert energy["watts_per_device"] == pytest.approx(300.0)


def test_write_training_fit_preserves_other_sections():
    # Pre-existing serving fit + another strategy's training record must survive.
    write_model_file(
        fit_calibration(
            [
                Observation(rps=100, latency_ms=50, replicas=2),
                Observation(rps=300, latency_ms=80, replicas=5),
            ]
        )
    )
    write_training_fit(ParallelismStrategy.FSDP, _fit(ParallelismStrategy.FSDP))
    write_training_fit(ParallelismStrategy.DATA, _fit(ParallelismStrategy.DATA))
    model = load_calibrated_model()
    # Serving fit intact and still verifies.
    assert "concurrency" in model
    assert verify_commitment(model) is True
    # Both training records present.
    assert set(model["training"]) == {"data", "fsdp"}
    assert verify_training_commitment(model["training"]["fsdp"]) is True


def test_write_training_fit_rejects_symlink_target(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "model.json"
    link.symlink_to(target)
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(write_training_fit.__globals__, "global_model_path", lambda: link)
        with pytest.raises(TrainingCalibrationError, match="symbolic link"):
            write_training_fit(ParallelismStrategy.DATA, _fit())
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_write_training_fit_rejects_corrupt_existing_model(tmp_path):
    path = tmp_path / "model.json"
    path.write_text("not-json\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(write_training_fit.__globals__, "global_model_path", lambda: path)
        with pytest.raises(TrainingCalibrationError, match="could not be read"):
            write_training_fit(ParallelismStrategy.DATA, _fit())
    assert path.read_text(encoding="utf-8") == "not-json\n"


def test_training_write_does_not_touch_serving_commitment():
    """Cross-contamination guard: a training write leaves the serving commitment."""
    write_model_file(
        fit_calibration(
            [
                Observation(rps=100, latency_ms=50, replicas=2),
                Observation(rps=300, latency_ms=80, replicas=5),
            ]
        )
    )
    before = load_calibrated_model()["calibration_commitment"]["digest"]
    write_training_fit(ParallelismStrategy.DATA, _fit())
    after = load_calibrated_model()["calibration_commitment"]["digest"]
    assert before == after


# ── committed record: tamper each field → fail closed ─────────────────────────


def _tamper_training(field, value):
    model = load_calibrated_model()
    model["training"]["data"][field] = value
    from presidio_arch_translucency.calibrate import global_model_path  # noqa: PLC0415

    global_model_path().write_text(json.dumps(model), encoding="utf-8")


@pytest.mark.parametrize(
    "field,value",
    [
        ("overhead_alpha", 0.49),
        ("overhead_beta", 0.4),
        ("baseline_samples_per_second", 9999.0),
        ("r_squared", 0.1),
        ("rmse", 123.0),
        ("calibrated_at", "2099-01-01T00:00:00+00:00"),
        ("microbatches", 4096),
        ("strategy", "fsdp"),
        ("runs", [[1, 1.0, 1.0, None]]),
    ],
)
def test_tamper_each_field_fails_closed(field, value):
    write_training_fit(ParallelismStrategy.DATA, _fit())
    _tamper_training(field, value)
    record = load_calibrated_model()["training"]["data"]
    assert verify_training_commitment(record) is False
    with pytest.raises(TrainingCalibrationTamperError):
        resolve_training_commitment(ParallelismStrategy.DATA)
    with pytest.raises(TrainingCalibrationTamperError):
        resolve_strategy_params(ParallelismStrategy.DATA)


def test_tamper_energy_field_fails_closed():
    write_training_fit(ParallelismStrategy.DATA, _fit(power=300.0))
    _tamper_training("watts_per_device", 1.0)
    with pytest.raises(TrainingCalibrationTamperError):
        resolve_training_commitment(ParallelismStrategy.DATA)


def test_committed_record_cannot_be_moved_across_strategy_keys():
    write_training_fit(ParallelismStrategy.FSDP, _fit(ParallelismStrategy.FSDP))
    model = load_calibrated_model()
    model["training"]["data"] = model["training"].pop("fsdp")
    from presidio_arch_translucency.calibrate import global_model_path  # noqa: PLC0415

    global_model_path().write_text(json.dumps(model), encoding="utf-8")
    with pytest.raises(TrainingCalibrationTamperError, match="cross-strategy"):
        resolve_strategy_params(ParallelismStrategy.DATA)


# ── legacy compat: hand-written training sections still work ───────────────────


def test_legacy_training_section_unchanged_behaviour(tmp_path, monkeypatch):
    # A v0.22-era hand-written training section (no commitment) is honoured.
    (tmp_path / ".pat-model.json").write_text(
        json.dumps(
            {"training": {"data": {"overhead_alpha": 0.2, "overhead_beta": 0.2}}}
        )
    )
    params = resolve_strategy_params(ParallelismStrategy.DATA)
    assert params.overhead_alpha == pytest.approx(0.2)
    assert params.overhead_beta == pytest.approx(0.2)
    assert resolve_training_commitment(ParallelismStrategy.DATA)["status"] == "legacy"


def test_uncalibrated_strategy_reports_uncalibrated():
    assert resolve_training_commitment(ParallelismStrategy.DATA) == {
        "status": "uncalibrated",
        "digest": None,
    }
    # Defaults are used when there is no record.
    params = resolve_strategy_params(ParallelismStrategy.DATA)
    assert (
        params.overhead_alpha
        == STRATEGY_PARAMS[ParallelismStrategy.DATA].overhead_alpha
    )


def test_resolve_training_energy_none_without_power():
    write_training_fit(ParallelismStrategy.DATA, _fit())  # no power
    assert resolve_training_energy(ParallelismStrategy.DATA) is None


# ── CLI: train-calibrate end-to-end + samples/s/W + commitment line ───────────
# All CLI tests inherit the autouse ``_home`` fixture, so `pat train-calibrate`
# writes into an isolated HOME (no real-store pollution).

from presidio_arch_translucency.cli import app  # noqa: E402

runner = CliRunner()


def _invoke(*args):
    # Wide console so Rich does not truncate the additive Samples/s/W / Energy
    # column headers (the default 80-col width abbreviates them to "Sampl…").
    return runner.invoke(app, ["--skip-audit", *args], env={"COLUMNS": "220"})


def _emit_log(tmp_path, name, rows):
    p = tmp_path / name
    _write_log(p, rows)
    return str(p)


def _calibrate_data(tmp_path, *, power=False):
    """Write 4 step logs (δ=1,2,4,8) and run `pat train-calibrate --strategy data`."""
    strategy = ParallelismStrategy.DATA
    d = STRATEGY_PARAMS[strategy]
    specs = []
    for deg in (1, 2, 4, 8):
        log = _synth_log(strategy, deg, 100.0, d.overhead_alpha, d.overhead_beta)
        rows = [_row(0, log.total_duration_s, log.total_samples)]
        if power:
            # power on every line (constant W across the single aggregated step).
            rows = [
                _row(0, log.total_duration_s, log.total_samples, power_w=300.0 * deg)
            ]
        specs += ["--run", f"{deg}:{_emit_log(tmp_path, f'ddp{deg}.jsonl', rows)}"]
    return _invoke("train-calibrate", "--strategy", "data", *specs)


def test_cli_train_calibrate_end_to_end(tmp_path):
    result = _calibrate_data(tmp_path)
    assert result.exit_code == 0
    assert "Training calibration" in result.output
    # The fit was committed and re-verifies.
    record = load_calibrated_model()["training"]["data"]
    assert verify_training_commitment(record) is True


def test_cli_train_calibrate_json(tmp_path):
    strategy = ParallelismStrategy.DATA
    d = STRATEGY_PARAMS[strategy]
    specs = []
    for deg in (1, 2, 4):
        log = _synth_log(strategy, deg, 100.0, d.overhead_alpha, d.overhead_beta)
        rows = [_row(0, log.total_duration_s, log.total_samples)]
        specs += ["--run", f"{deg}:{_emit_log(tmp_path, f'j{deg}.jsonl', rows)}"]
    result = _invoke("train-calibrate", "--strategy", "data", "--json", *specs)
    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["strategy"] == "data"
    assert payload["baseline_samples_per_second"] == pytest.approx(100.0, rel=0.05)
    assert "overhead_alpha" in payload and "runs" in payload


def test_cli_train_calibrate_malformed_log_exits_2(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("not json\n", encoding="utf-8")
    result = _invoke(
        "train-calibrate",
        "--strategy",
        "data",
        "--run",
        f"1:{p}",
        "--run",
        f"2:{p}",
    )
    assert result.exit_code == 2
    assert "Traceback" not in (result.output or "")


def test_cli_train_analyze_shows_commitment_line_after_calibrate(tmp_path):
    assert _calibrate_data(tmp_path).exit_code == 0
    result = _invoke("train-analyze", "-s", "100", "-m", "4", "-d", "24", "-n", "8")
    assert result.exit_code == 0
    assert "Training calibration commitment" in result.output
    assert "data" in result.output


def test_cli_train_analyze_default_no_energy_is_byte_identical(tmp_path):
    """Additive-only: no calibration + no --device-power-watts → pre-v0.23 output.

    No Samples/s/W column, no Energy column, and no commitment line are rendered
    when there is nothing to show, so the default table is byte-identical.
    """
    result = _invoke("train-analyze", "-s", "100", "-m", "4", "-d", "24", "-n", "8")
    assert result.exit_code == 0
    assert "Samples/s/W" not in result.output
    assert "Training calibration commitment" not in result.output


def test_cli_train_analyze_partial_power_has_no_energy_best(tmp_path):
    assert _calibrate_data(tmp_path, power=True).exit_code == 0
    result = _invoke("train-analyze", "-s", "100", "-m", "4", "-d", "24", "-n", "8")
    assert result.exit_code == 0
    assert "Samples/s/W" in result.output
    assert "⚡ best" not in result.output
    assert "Energy ranking incomplete" in result.output


def test_cli_train_analyze_device_power_flag_shows_column_without_calibration(tmp_path):
    result = _invoke(
        "train-analyze",
        "-s",
        "100",
        "-m",
        "4",
        "-d",
        "24",
        "-n",
        "8",
        "--device-power-watts",
        "400",
    )
    assert result.exit_code == 0
    assert "Samples/s/W" in result.output
    assert "best" in result.output


def test_cli_train_analyze_energy_does_not_change_recommendation(tmp_path):
    base = _invoke("train-analyze", "-s", "100", "-m", "4", "-d", "24", "-n", "8")

    def _recommendation(text):
        for line in text.splitlines():
            if "Recommend" in line:
                return line
        return ""

    with_power = _invoke(
        "train-analyze",
        "-s",
        "100",
        "-m",
        "4",
        "-d",
        "24",
        "-n",
        "8",
        "--device-power-watts",
        "400",
    )
    assert base.exit_code == with_power.exit_code == 0
    # The recommendation is throughput-driven; energy never changes it.
    assert _recommendation(base.output) == _recommendation(with_power.output)


def test_cli_train_analyze_tamper_exits_2(tmp_path):
    assert _calibrate_data(tmp_path).exit_code == 0
    _tamper_training("overhead_alpha", 0.49)
    result = _invoke("train-analyze", "-s", "100", "-m", "4", "-d", "24", "-n", "8")
    assert result.exit_code == 2
    assert "tamper" in result.output.lower()


def test_cli_train_what_if_shows_commitment_line(tmp_path):
    assert _calibrate_data(tmp_path).exit_code == 0
    result = _invoke(
        "train-what-if",
        "--strategy",
        "data",
        "--degree",
        "4",
        "-s",
        "100",
        "-m",
        "4",
        "-d",
        "24",
    )
    assert result.exit_code == 0
    assert "Training calibration commitment" in result.output


def test_cli_train_what_if_tamper_exits_2(tmp_path):
    assert _calibrate_data(tmp_path).exit_code == 0
    _tamper_training("baseline_samples_per_second", 9999.0)
    result = _invoke(
        "train-what-if",
        "--strategy",
        "data",
        "--degree",
        "4",
        "-s",
        "100",
        "-m",
        "4",
        "-d",
        "24",
    )
    assert result.exit_code == 2
    assert "tamper" in result.output.lower()
