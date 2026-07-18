"""Tests for the ML training parallelism domain (training.py + training evidence)."""

import json
import math

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.evidence_producer import (
    TRAINING_SCHEMA_ID,
    EvidenceProducerError,
    build_training_run_reading,
    sha256_hex,
)
from presidio_arch_translucency.training import (
    DEFAULT_MICROBATCHES,
    MEMORY_HEADROOM,
    ORDERED_STRATEGIES,
    STRATEGY_PARAMS,
    VALID_STRATEGIES,
    ParallelismStrategy,
    analyze_training,
    evaluate_strategy,
    memory_feasible,
    per_device_memory_gb,
    resolve_strategy_params,
    scaling_efficiency,
    training_throughput,
)

# ---------------------------------------------------------------------------
# Model equations
# ---------------------------------------------------------------------------


def test_valid_strategies_match_enum():
    assert VALID_STRATEGIES == tuple(s.value for s in ORDERED_STRATEGIES)


def test_efficiency_is_one_at_degree_one():
    for strategy in ORDERED_STRATEGIES:
        assert scaling_efficiency(strategy, 1) == 1.0


def test_alpha_beta_efficiency_decreases_with_degree():
    for strategy in (
        ParallelismStrategy.DATA,
        ParallelismStrategy.FSDP,
        ParallelismStrategy.TENSOR,
    ):
        effs = [scaling_efficiency(strategy, d) for d in (2, 4, 8)]
        assert effs == sorted(effs, reverse=True)
        params = STRATEGY_PARAMS[strategy]
        expected = 1.0 - params.overhead_alpha - params.overhead_beta * math.log(4)
        assert scaling_efficiency(strategy, 4) == pytest.approx(expected)


def test_pipeline_uses_exact_bubble_formula():
    params = STRATEGY_PARAMS[ParallelismStrategy.PIPELINE]
    m, delta = DEFAULT_MICROBATCHES, 4
    expected = (1.0 - params.overhead_alpha) * m / (m + delta - 1)
    assert scaling_efficiency(ParallelismStrategy.PIPELINE, delta) == pytest.approx(
        expected
    )
    # More microbatches → smaller bubble → better efficiency.
    assert scaling_efficiency(
        ParallelismStrategy.PIPELINE, delta, microbatches=32
    ) > scaling_efficiency(ParallelismStrategy.PIPELINE, delta, microbatches=4)


def test_throughput_is_compute_bound_and_uncapped():
    tp = training_throughput(100.0, ParallelismStrategy.DATA, 8)
    eff = scaling_efficiency(ParallelismStrategy.DATA, 8)
    assert tp == pytest.approx(100.0 * 8 * eff)
    assert tp > 100.0  # no demand cap in the training domain


def test_efficiency_never_negative():
    assert scaling_efficiency(ParallelismStrategy.TENSOR, 8) >= 0.0


# ---------------------------------------------------------------------------
# Memory: hard constraint, not penalty
# ---------------------------------------------------------------------------


def test_data_parallelism_holds_full_replica():
    assert per_device_memory_gb(ParallelismStrategy.DATA, 8, 40.0) == 40.0


def test_sharded_strategies_divide_model_state():
    for strategy in (
        ParallelismStrategy.FSDP,
        ParallelismStrategy.TENSOR,
        ParallelismStrategy.PIPELINE,
    ):
        assert per_device_memory_gb(strategy, 8, 40.0) == pytest.approx(5.0)


def test_memory_feasibility_uses_headroom():
    # 9.0 GB model on a 10 GB device: 9.0 <= 10 * 0.9 → feasible (boundary).
    assert MEMORY_HEADROOM == pytest.approx(0.9)
    assert memory_feasible(ParallelismStrategy.DATA, 1, 9.0, 10.0)
    assert not memory_feasible(ParallelismStrategy.DATA, 1, 9.1, 10.0)


def test_oversized_model_infeasible_for_data_but_feasible_sharded():
    # 40 GB model, 24 GB devices: DDP can never fit; FSDP at δ>=2 can.
    assert not memory_feasible(ParallelismStrategy.DATA, 8, 40.0, 24.0)
    assert memory_feasible(ParallelismStrategy.FSDP, 2, 40.0, 24.0)


# ---------------------------------------------------------------------------
# Analysis / recommendation
# ---------------------------------------------------------------------------


def test_analyze_recommends_data_when_model_fits():
    # Small model, plenty of memory → DDP has the lowest overhead and wins.
    result = analyze_training(
        baseline_samples_per_second=100.0,
        model_memory_gb=4.0,
        device_memory_gb=24.0,
        device_count=8,
    )
    assert result.recommended_strategy is ParallelismStrategy.DATA
    assert result.recommended_degree > 1


def test_analyze_excludes_infeasible_data_parallelism():
    # Model larger than a device → DDP infeasible, a sharded strategy wins.
    result = analyze_training(
        baseline_samples_per_second=50.0,
        model_memory_gb=40.0,
        device_memory_gb=24.0,
        device_count=8,
    )
    data_row = next(
        r for r in result.strategies if r.strategy is ParallelismStrategy.DATA
    )
    assert not data_row.feasible
    assert data_row.optimal_degree == 0
    assert result.recommended_strategy is not None
    assert result.recommended_strategy is not ParallelismStrategy.DATA


def test_analyze_nothing_feasible_returns_none():
    # Model so large not even full sharding across all devices fits.
    result = analyze_training(
        baseline_samples_per_second=10.0,
        model_memory_gb=1000.0,
        device_memory_gb=8.0,
        device_count=4,
    )
    assert result.recommended_strategy is None
    assert result.recommended_degree == 0
    assert all(not r.feasible for r in result.strategies)


def test_analyze_respects_device_count_bound():
    result = analyze_training(
        baseline_samples_per_second=100.0,
        model_memory_gb=4.0,
        device_memory_gb=24.0,
        device_count=2,
    )
    for r in result.strategies:
        assert r.optimal_degree <= 2


def test_evaluate_strategy_single_point():
    r = evaluate_strategy(
        ParallelismStrategy.PIPELINE,
        4,
        baseline_samples_per_second=100.0,
        model_memory_gb=40.0,
        device_memory_gb=24.0,
    )
    assert r.optimal_degree == 4
    assert r.feasible
    assert r.per_device_memory_gb == pytest.approx(10.0)
    assert r.estimated_samples_per_second > 100.0


# ---------------------------------------------------------------------------
# Calibration overrides (``training`` section of .pat-model.json)
# ---------------------------------------------------------------------------


def test_calibrated_training_overrides_are_honoured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".pat-model.json").write_text(
        json.dumps(
            {"training": {"data": {"overhead_alpha": 0.2, "overhead_beta": 0.2}}}
        )
    )
    params = resolve_strategy_params(ParallelismStrategy.DATA)
    assert params.overhead_alpha == pytest.approx(0.2)
    assert params.overhead_beta == pytest.approx(0.2)
    # Un-overridden strategies keep their defaults.
    fsdp = resolve_strategy_params(ParallelismStrategy.FSDP)
    defaults_fsdp = STRATEGY_PARAMS[ParallelismStrategy.FSDP]
    assert fsdp.overhead_alpha == defaults_fsdp.overhead_alpha


def test_malformed_calibration_falls_back_to_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".pat-model.json").write_text(
        json.dumps(
            {"training": {"data": {"overhead_alpha": "bogus", "overhead_beta": 7}}}
        )
    )
    params = resolve_strategy_params(ParallelismStrategy.DATA)
    defaults = STRATEGY_PARAMS[ParallelismStrategy.DATA]
    assert params.overhead_alpha == defaults.overhead_alpha
    assert params.overhead_beta == defaults.overhead_beta  # 7 rejected (>=1.0)


# ---------------------------------------------------------------------------
# training-run@1 evidence (Layer 0, key-less) + parents provenance convention
# ---------------------------------------------------------------------------

_PARENT = "a" * 64
_OTHER_PARENT = "b" * 64


def test_training_run_reading_shape_and_hash():
    reading = build_training_run_reading(
        run_id="run-2026-07-02-001",
        strategy="fsdp",
        degree=8,
        samples_per_second=712,
        duration_s=3600,
        device_count=8,
    )
    assert reading["schema"] == TRAINING_SCHEMA_ID
    content = reading["attested_content"]
    assert content["strategy"] == "fsdp"
    assert "parents" not in content  # omitted when empty
    # content_hash binds the attested content (recomputable by the sidecar).
    assert reading["content_hash"] == sha256_hex(content)


def test_training_run_parents_are_attested_inside_content():
    reading = build_training_run_reading(
        run_id="run-1",
        strategy="data",
        degree=2,
        samples_per_second=100,
        duration_s=60,
        device_count=2,
        parents=[_PARENT, _OTHER_PARENT],
        model_hash="c" * 64,
        dataset_hash="d" * 64,
    )
    content = reading["attested_content"]
    assert content["parents"] == [_PARENT, _OTHER_PARENT]
    assert content["model_hash"] == "c" * 64
    # Parents are inside the hashed content → tampering changes the hash.
    without_parents = build_training_run_reading(
        run_id="run-1",
        strategy="data",
        degree=2,
        samples_per_second=100,
        duration_s=60,
        device_count=2,
    )
    assert reading["content_hash"] != without_parents["content_hash"]


@pytest.mark.parametrize(
    "bad_parent",
    ["", "xyz", "A" * 64, "a" * 7, "a" * 129, "deadbeef!"],
)
def test_training_run_rejects_malformed_parent(bad_parent):
    with pytest.raises(EvidenceProducerError):
        build_training_run_reading(
            run_id="run-1",
            strategy="data",
            degree=1,
            samples_per_second=1,
            duration_s=1,
            device_count=1,
            parents=[bad_parent],
        )


def test_training_run_rejects_empty_run_id_and_bad_degree():
    with pytest.raises(EvidenceProducerError):
        build_training_run_reading(
            run_id="",
            strategy="data",
            degree=1,
            samples_per_second=1,
            duration_s=1,
            device_count=1,
        )
    with pytest.raises(EvidenceProducerError):
        build_training_run_reading(
            run_id="run-1",
            strategy="data",
            degree=0,
            samples_per_second=1,
            duration_s=1,
            device_count=1,
        )


# Family golden vector: presidio-evidence vectors/training-run/ (appended
# 2026-07-02, both suites green — see presidio-evidence
# docs/conformance/full-run-conformance-suite-2026-07-02T234741+0200.md).
# Pinned here exactly as the evidence-ref@1 vector is pinned in
# test_evidence_producer.py: if this producer ever drifts from the family
# canonical profile, this constant catches it.
_FAMILY_TRAINING_RUN_VECTOR_HASH = (
    "91733915b4797d71bfc42422dcfff105b512f613c4d6ad3f1013463d1853b378"
)


def test_training_run_content_hash_matches_family_golden_vector():
    """Byte-identity with the family vector, not just self-consistency."""
    reading = build_training_run_reading(
        run_id="golden-run",
        strategy="pipeline",
        degree=4,
        samples_per_second=250,
        duration_s=7200,
        device_count=4,
        parents=[_PARENT],
        observed_at="2026-07-02T00:00:00+00:00",
        source_version="test",
    )
    # Cross-repo pin: the family vector's content hash, byte-for-byte.
    assert reading["content_hash"] == _FAMILY_TRAINING_RUN_VECTOR_HASH
    # And self-consistency of the canonical layer (sidecar recompute path).
    assert reading["content_hash"] == sha256_hex(reading["attested_content"])


# ---------------------------------------------------------------------------
# training-run@1 optional energy fields (v0.23.0) — producer claims (E1a),
# int-or-decimal-string on the wire (floats rejected), additive & conditional.
# ---------------------------------------------------------------------------


def _golden_energy_kwargs(**extra):
    """The golden-vector inputs, so the no-energy hash pins byte-identity."""
    base = dict(
        run_id="golden-run",
        strategy="pipeline",
        degree=4,
        samples_per_second=250,
        duration_s=7200,
        device_count=4,
        parents=[_PARENT],
        observed_at="2026-07-02T00:00:00+00:00",
        source_version="test",
    )
    base.update(extra)
    return base


def test_training_run_no_energy_is_byte_identical_pin():
    """No-energy record: energy keys omitted; hash byte-identical to pre-v0.23.

    Passing ``energy_wh=None``/``mean_power_w=None`` must be indistinguishable
    from omitting them — the additive fields never perturb the canonical bytes
    of a power-free record (ADR-0011), so it re-hashes to the family vector.
    """
    reading = build_training_run_reading(
        **_golden_energy_kwargs(energy_wh=None, mean_power_w=None)
    )
    content = reading["attested_content"]
    assert "energy_wh" not in content
    assert "mean_power_w" not in content
    assert reading["content_hash"] == _FAMILY_TRAINING_RUN_VECTOR_HASH


# Energy-bearing family pin (L-EV-7): byte-for-byte the content hash of the
# cross-repo family vector presidio-evidence
# vectors/training-run/valid-envelope-energy.json (both suites there recompute
# and pin it). A drift in this producer's string-decimal / field profile now
# fails against the shared family reference, not just a local self-pin.
_FAMILY_TRAINING_RUN_ENERGY_VECTOR_HASH = (
    "d674a11562def33ba92b54ab946d7782b1d3a111ec6a8f8f22a541788a57ffb0"
)


def test_training_run_energy_bearing_matches_family_vector():
    reading = build_training_run_reading(
        **_golden_energy_kwargs(energy_wh="840.0", mean_power_w="420.0")
    )
    content = reading["attested_content"]
    # Decimal strings normalised to the IEEE-754 round-trip repr (string on wire).
    assert content["energy_wh"] == "840.0"
    assert content["mean_power_w"] == "420.0"
    assert reading["content_hash"] == _FAMILY_TRAINING_RUN_ENERGY_VECTOR_HASH
    assert reading["content_hash"] == sha256_hex(content)


def test_training_run_energy_int_canonicalized_to_string():
    reading = build_training_run_reading(
        **_golden_energy_kwargs(energy_wh=840, mean_power_w=420)
    )
    content = reading["attested_content"]
    assert content["energy_wh"] == "840.0"
    assert content["mean_power_w"] == "420.0"
    string_reading = build_training_run_reading(
        **_golden_energy_kwargs(energy_wh="840.0", mean_power_w="420.0")
    )
    assert reading["content_hash"] == string_reading["content_hash"]


def test_training_run_energy_negative_zero_normalized():
    """ "-0"/"-0.0" collapse to "0.0" — one energy value, one content hash."""
    minus = build_training_run_reading(**_golden_energy_kwargs(energy_wh="-0.0"))
    plus = build_training_run_reading(**_golden_energy_kwargs(energy_wh="0.0"))
    assert minus["attested_content"]["energy_wh"] == "0.0"
    assert minus["content_hash"] == plus["content_hash"]


def test_training_run_energy_float_grammar_spellings_normalized():
    """Python float-grammar spellings are accepted and land on one canonical
    wire form (documented behaviour — not a strict decimal grammar)."""
    for spelling in ("1e3", " 1000.0 ", "+1000.0", "1000.0"):
        reading = build_training_run_reading(
            **_golden_energy_kwargs(energy_wh=spelling)
        )
        assert reading["attested_content"]["energy_wh"] == "1000.0", spelling


def test_training_run_energy_independently_optional():
    # energy_wh alone is allowed (both-or-neither is NOT required).
    reading = build_training_run_reading(**_golden_energy_kwargs(energy_wh=5))
    content = reading["attested_content"]
    assert content["energy_wh"] == "5.0"
    assert "mean_power_w" not in content


def test_training_run_energy_power_duration_mismatch_rejected():
    with pytest.raises(EvidenceProducerError, match="contradict"):
        build_training_run_reading(
            **_golden_energy_kwargs(energy_wh="12.5", mean_power_w="420.0")
        )


@pytest.mark.parametrize("field", ["energy_wh", "mean_power_w"])
def test_training_run_energy_float_rejected_on_wire(field):
    with pytest.raises(EvidenceProducerError, match="float"):
        build_training_run_reading(**_golden_energy_kwargs(**{field: 12.5}))


@pytest.mark.parametrize(
    "bad",
    [
        -1,  # negative int
        "-2.0",  # negative decimal string
        "not-a-number",  # unparseable
        "NaN",  # non-finite decimal string
        "Infinity",  # non-finite decimal string
        "1" * 65,  # bounded before Decimal parsing
        "0.1234567890123456789",  # loses precision on IEEE-754 round-trip
        True,  # bool rejected
    ],
)
def test_training_run_energy_rejects_bad_values(bad):
    with pytest.raises(EvidenceProducerError):
        build_training_run_reading(**_golden_energy_kwargs(energy_wh=bad))


def test_cli_train_evidence_emit_energy_string_decimal():
    result = _invoke(
        "train-evidence-emit",
        "--run-id",
        "run-energy",
        "--strategy",
        "data",
        "--degree",
        "2",
        "-s",
        "100",
        "--duration-s",
        "60",
        "-n",
        "2",
        "--energy-wh",
        "7.0",
        "--mean-power-w",
        "420.0",
    )
    assert result.exit_code == 0
    content = json.loads(result.output.strip())["attested_content"]
    assert content["energy_wh"] == "7.0"  # string-decimal on the wire
    assert content["mean_power_w"] == "420.0"


def test_cli_train_evidence_emit_rejects_float_energy():
    # A bare float string that is not round-trip-lossless is rejected; a clean
    # decimal string is accepted — the CLI passes the raw string through.
    result = _invoke(
        "train-evidence-emit",
        "--run-id",
        "run-energy-bad",
        "--strategy",
        "data",
        "--degree",
        "1",
        "-s",
        "10",
        "--duration-s",
        "1",
        "-n",
        "1",
        "--energy-wh",
        "0.1234567890123456789",
    )
    assert result.exit_code == 1
    assert not result.output.strip().startswith("{")


# ---------------------------------------------------------------------------
# CLI (train-analyze / train-what-if / train-evidence-emit)
# ---------------------------------------------------------------------------

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def test_cli_train_analyze_recommends_sharded_for_large_model():
    result = _invoke(
        "train-analyze", "-s", "120", "-m", "40", "-d", "24", "-n", "8", "--show-all"
    )
    assert result.exit_code == 0
    assert "fsdp" in result.output
    assert "Recommendation" in result.output


def test_cli_train_what_if_json():
    result = _invoke(
        "train-what-if",
        "--strategy",
        "pipeline",
        "--degree",
        "4",
        "-s",
        "100",
        "-m",
        "40",
        "-d",
        "24",
        "--json",
    )
    assert result.exit_code == 0
    payload = json.loads(result.output.strip())
    assert payload["strategy"] == "pipeline"
    assert payload["feasible"] is True


def test_cli_train_what_if_rejects_unknown_strategy():
    result = _invoke(
        "train-what-if",
        "--strategy",
        "voodoo",
        "--degree",
        "2",
        "-s",
        "100",
        "-m",
        "4",
        "-d",
        "24",
    )
    assert result.exit_code == 2


def test_cli_train_evidence_emit_roundtrip():
    parent = "a" * 64
    result = _invoke(
        "train-evidence-emit",
        "--run-id",
        "run-cli-1",
        "--strategy",
        "data",
        "--degree",
        "2",
        "-s",
        "99.6",
        "--duration-s",
        "60",
        "-n",
        "2",
        "--parent",
        parent,
    )
    assert result.exit_code == 0
    reading = json.loads(result.output.strip())
    assert reading["schema"] == TRAINING_SCHEMA_ID
    content = reading["attested_content"]
    assert content["samples_per_second"] == 100  # rounded — no floats on the wire
    assert content["parents"] == [parent]
    assert reading["content_hash"] == sha256_hex(content)


def test_cli_train_evidence_emit_fails_closed_on_bad_parent():
    result = _invoke(
        "train-evidence-emit",
        "--run-id",
        "run-cli-2",
        "--strategy",
        "data",
        "--degree",
        "1",
        "-s",
        "10",
        "--duration-s",
        "1",
        "-n",
        "1",
        "--parent",
        "NOT-HEX",
    )
    assert result.exit_code == 1
    assert not result.output.strip().startswith("{")


# ---------------------------------------------------------------------------
# v0.18.0 third-party audit regressions (nan/inf, contract hardening, degree)
# ---------------------------------------------------------------------------


def test_analyze_rejects_non_finite_workload():
    from presidio_arch_translucency.training import TrainingDomainError

    for bad in (float("nan"), float("inf"), float("-inf"), 0.0, -1.0):
        with pytest.raises(TrainingDomainError):
            analyze_training(
                baseline_samples_per_second=bad,
                model_memory_gb=4.0,
                device_memory_gb=24.0,
                device_count=4,
            )
        with pytest.raises(TrainingDomainError):
            analyze_training(
                baseline_samples_per_second=100.0,
                model_memory_gb=bad,
                device_memory_gb=24.0,
                device_count=4,
            )


def test_evaluate_strategy_rejects_out_of_domain_degree():
    from presidio_arch_translucency.training import TrainingDomainError

    # pipeline max_degree is 16 — 999 is out of the model's domain.
    with pytest.raises(TrainingDomainError):
        evaluate_strategy(
            ParallelismStrategy.PIPELINE,
            999,
            baseline_samples_per_second=100.0,
            model_memory_gb=40.0,
            device_memory_gb=24.0,
        )
    with pytest.raises(TrainingDomainError):
        evaluate_strategy(
            ParallelismStrategy.DATA,
            0,
            baseline_samples_per_second=100.0,
            model_memory_gb=4.0,
            device_memory_gb=24.0,
        )


def test_cli_train_analyze_rejects_nan_and_inf():
    for bad in ("nan", "inf", "-inf"):
        result = _invoke("train-analyze", "-s", bad, "-m", "40", "-d", "24", "-n", "8")
        assert result.exit_code == 2
        assert "Traceback" not in (result.output or "")


def test_cli_train_what_if_rejects_out_of_domain_degree():
    result = _invoke(
        "train-what-if",
        "--strategy",
        "pipeline",
        "--degree",
        "999",
        "-s",
        "100",
        "-m",
        "40",
        "-d",
        "24",
        "--json",
    )
    assert result.exit_code == 2


def test_cli_train_evidence_emit_rejects_inf_samples():
    result = _invoke(
        "train-evidence-emit",
        "--run-id",
        "run-inf",
        "--strategy",
        "data",
        "--degree",
        "1",
        "-s",
        "inf",
        "--duration-s",
        "1",
        "-n",
        "1",
    )
    assert result.exit_code == 1
    assert "Traceback" not in (result.output or "")
    assert not (result.output or "").strip().startswith("{")


def test_evidence_strategies_pinned_to_training_domain():
    from presidio_arch_translucency.evidence_producer import _TRAINING_STRATEGIES

    assert _TRAINING_STRATEGIES == VALID_STRATEGIES


def test_training_run_rejects_unknown_strategy():
    with pytest.raises(EvidenceProducerError):
        build_training_run_reading(
            run_id="run-1",
            strategy="voodoo",
            degree=1,
            samples_per_second=1,
            duration_s=1,
            device_count=1,
        )


@pytest.mark.parametrize(
    "bad_run_id",
    ["bad\nrun", "bad\rrun", "bad\x00run", "tab\tid", " ", "x" * 513],
)
def test_training_run_rejects_malformed_run_id(bad_run_id):
    with pytest.raises(EvidenceProducerError):
        build_training_run_reading(
            run_id=bad_run_id,
            strategy="data",
            degree=1,
            samples_per_second=1,
            duration_s=1,
            device_count=1,
        )


def test_training_run_rejects_bad_integers():
    for kwargs in (
        {"samples_per_second": -1},
        {"samples_per_second": float("inf")},
        {"samples_per_second": float("nan")},
        {"samples_per_second": 99.6},  # no silent truncation
        {"duration_s": -5},
        {"degree": True},  # bool is not an integer here
        {"device_count": "many"},
    ):
        args = {
            "run_id": "run-1",
            "strategy": "data",
            "degree": 1,
            "samples_per_second": 1,
            "duration_s": 1,
            "device_count": 1,
        }
        args.update(kwargs)
        with pytest.raises(EvidenceProducerError):
            build_training_run_reading(**args)


def test_cli_train_evidence_emit_logs_digest_not_raw_run_id(caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="presidio.arch_translucency.security"):
        result = _invoke(
            "train-evidence-emit",
            "--run-id",
            "sensitive-project-name",
            "--strategy",
            "data",
            "--degree",
            "1",
            "-s",
            "10",
            "--duration-s",
            "1",
            "-n",
            "1",
        )
    assert result.exit_code == 0
    assert "sensitive-project-name" not in caplog.text
