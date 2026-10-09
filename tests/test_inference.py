"""Tests for the LLM inference serving domain (inference.py + infer-* CLI)."""

import json
import math

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.inference import (
    DEFAULT_ENGINE_PARAMS,
    OVERHEAD_PARAMS,
    EngineParams,
    EngineSpec,
    GpuSpec,
    InferenceDomainError,
    InferenceWorkload,
    ModelSpec,
    ServingStrategy,
    analyze_inference,
    coordination_overhead,
    decode_step_s,
    erlang_c,
    evaluate_config,
    kv_tokens_per_instance,
    max_batch,
    representative_configs,
    strategy_for,
    weights_fit,
)

# Llama-3.1-8B bf16: 32 layers, 8 KV heads, head_dim 128 → 2·32·8·128·2 bytes.
KV_8B = 131_072.0
# Mistral-Small-24B bf16: 40 layers, 8 KV heads, head_dim 128.
KV_24B = 163_840.0

L40S = {"memory_gb": 48.0, "bandwidth_gbs": 864.0}


def _workload(rps: float = 2.0) -> InferenceWorkload:
    return InferenceWorkload(
        requests_per_second=rps, prompt_tokens=1000, output_tokens=256
    )


def _gpu(count: int = 4, **kw) -> GpuSpec:
    return GpuSpec(count=count, **{**L40S, **kw})


# ---------------------------------------------------------------------------
# Strategy labels and parameters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tp", "n", "expected"),
    [
        (1, 1, ServingStrategy.REPLICA),
        (1, 4, ServingStrategy.REPLICA),
        (4, 1, ServingStrategy.TENSOR),
        (2, 2, ServingStrategy.HYBRID),
    ],
)
def test_strategy_label_is_derived(tp, n, expected):
    assert strategy_for(tp, n) is expected


def test_coordination_overhead_grows_with_tp():
    assert coordination_overhead(ServingStrategy.REPLICA, 1) == pytest.approx(
        OVERHEAD_PARAMS[ServingStrategy.REPLICA].overhead_alpha
    )
    assert coordination_overhead(ServingStrategy.TENSOR, 8) > coordination_overhead(
        ServingStrategy.TENSOR, 2
    )


# ---------------------------------------------------------------------------
# Memory: weights and KV cache are hard constraints
# ---------------------------------------------------------------------------


def test_kv_capacity_formula():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    gpu = _gpu()
    expected = (1 * 0.9 * 48.0 - 16.0) * 1e9 / KV_8B
    assert kv_tokens_per_instance(model, gpu, 1) == pytest.approx(expected)
    # TP pools memory: tp·h·M − W, so KV grows faster than linearly in tp.
    assert kv_tokens_per_instance(model, gpu, 2) > 2 * expected


def test_weights_that_do_not_fit_are_infeasible():
    model = ModelSpec(weights_gb=65.0, kv_bytes_per_token=KV_24B)
    gpu = _gpu()
    assert not weights_fit(model, gpu, 1)
    assert weights_fit(model, gpu, 2)
    r = evaluate_config(1, 1, _workload(), model, gpu)
    assert not r.feasible
    assert r.ttft_p99_ms is None and r.tpot_ms is None
    assert not r.slo_ok


def test_weights_fit_but_no_room_for_one_sequence_is_infeasible():
    # 43 GB of weights on 0.9·48 = 43.2 GB leaves ~0.2 GB: < one 1256-token sequence.
    model = ModelSpec(weights_gb=43.0, kv_bytes_per_token=KV_24B)
    gpu = _gpu()
    assert weights_fit(model, gpu, 1)
    assert max_batch(_workload(), model, gpu, EngineSpec(), 1) == 0
    assert not evaluate_config(1, 1, _workload(), model, gpu).feasible


def test_max_batch_capped_by_max_num_seqs():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    assert max_batch(_workload(), model, _gpu(), EngineSpec(max_num_seqs=32), 4) == 32


def test_kv_crossover_mechanism():
    """As W → h·M the replica's batch budget collapses while TP=2's does not."""
    gpu = _gpu()
    small = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    large = ModelSpec(weights_gb=40.0, kv_bytes_per_token=KV_8B)
    engine = EngineSpec()
    ratio_small = max_batch(_workload(), small, gpu, engine, 2) / max_batch(
        _workload(), small, gpu, engine, 1
    )
    ratio_large = max_batch(_workload(), large, gpu, engine, 2) / max_batch(
        _workload(), large, gpu, engine, 1
    )
    assert ratio_large > 5 * ratio_small


# ---------------------------------------------------------------------------
# Timing equations
# ---------------------------------------------------------------------------


def test_decode_step_is_affine_in_batch_and_faster_with_tp():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    gpu = _gpu()
    args = (_workload(), model, gpu)
    p = DEFAULT_ENGINE_PARAMS
    t0 = decode_step_s(0, *args, 1, ServingStrategy.REPLICA, p)
    t1 = decode_step_s(1, *args, 1, ServingStrategy.REPLICA, p)
    t10 = decode_step_s(10, *args, 1, ServingStrategy.REPLICA, p)
    assert t10 - t0 == pytest.approx(10 * (t1 - t0))
    # Weights read only (α_replica = 0): W / (η·BW) + t₀.
    expected = 16e9 / (0.6 * 864e9) + 0.002
    assert t0 == pytest.approx(expected)
    assert decode_step_s(0, *args, 2, ServingStrategy.TENSOR, p) < t0


def test_coordination_cost_does_not_shrink_with_tp():
    """TP speed-up stays sub-linear: TP=8 is well under 8× (and under 3×) TP=1."""
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    gpu = _gpu(count=8)
    p = DEFAULT_ENGINE_PARAMS
    t1 = decode_step_s(1, _workload(), model, gpu, 1, ServingStrategy.REPLICA, p)
    t8 = decode_step_s(1, _workload(), model, gpu, 8, ServingStrategy.TENSOR, p)
    assert 1.5 < t1 / t8 < 3.0


def test_erlang_c_known_values():
    # M/M/1: P(wait) = ρ.
    assert erlang_c(1, 0.5) == pytest.approx(0.5)
    # M/M/2 with a = 1: C = 1/3.
    assert erlang_c(2, 1.0) == pytest.approx(1.0 / 3.0)
    # Large c, light load: essentially no waiting, and no overflow.
    assert erlang_c(256, 10.0) < 1e-12


def test_little_law_holds_at_effective_batch():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    w = _workload(rps=4.0)
    r = evaluate_config(1, 1, w, model, _gpu(count=1))
    assert not r.saturated
    p = DEFAULT_ENGINE_PARAMS
    t_pre = 1000 / p.prefill_tokens_per_s_per_gpu / (1 - 0.02)
    step = decode_step_s(r.effective_batch, w, model, _gpu(count=1), 1, r.strategy, p)
    service = t_pre + 256 * step
    assert r.effective_batch == pytest.approx(4.0 * service, rel=1e-3)


def test_saturation_is_reported_not_recommended():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    r = evaluate_config(1, 1, _workload(rps=500.0), model, _gpu(count=1))
    assert r.feasible and r.saturated
    assert r.ttft_p99_ms is None
    assert r.served_rps == pytest.approx(r.capacity_rps)
    assert r.served_rps < 500.0
    a = analyze_inference(_workload(rps=500.0), model, _gpu(count=1))
    assert a.recommended is None
    assert a.best_effort is not None


def test_ttft_rises_near_capacity():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    gpu = _gpu(count=1)
    engine = EngineSpec(max_num_seqs=4)
    light = evaluate_config(1, 1, _workload(rps=0.05), model, gpu, engine)
    assert not light.saturated
    cap = light.capacity_rps
    heavy = evaluate_config(1, 1, _workload(rps=cap * 0.97), model, gpu, engine)
    assert not heavy.saturated
    assert heavy.ttft_p99_ms > light.ttft_p99_ms


def test_cost_per_million_output_tokens():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    r = evaluate_config(1, 2, _workload(rps=2.0), model, _gpu(), cost_per_gpu_hour=1.0)
    assert r.cost_per_hour == pytest.approx(2.0)
    assert r.cost_per_million_output_tokens == pytest.approx(
        2.0 / (2.0 * 256 * 3600) * 1e6, rel=1e-4
    )


# ---------------------------------------------------------------------------
# Analysis / recommendation objective
# ---------------------------------------------------------------------------


def test_analysis_sweeps_all_configs_within_budget():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(_workload(), model, _gpu(count=4))
    pairs = {(c.tp, c.instances) for c in a.configs}
    assert pairs == {(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2), (4, 1)}
    assert all(c.gpus <= 4 for c in a.configs)
    assert a.baseline is not None and (a.baseline.tp, a.baseline.instances) == (1, 4)


def test_tp_capped_by_gpus_per_node():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(_workload(), model, _gpu(count=8, gpus_per_node=2))
    assert max(c.tp for c in a.configs) == 2


def test_recommends_fewest_gpus_meeting_slo():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(_workload(rps=4.0), model, _gpu(count=4), tpot_slo_ms=60)
    by_layout = {(c.tp, c.instances): c for c in a.configs}
    assert by_layout[(1, 1)].slo_ok and by_layout[(1, 4)].slo_ok
    assert a.recommended is not None
    assert (a.recommended.tp, a.recommended.instances) == (1, 1)


def test_large_model_on_small_gpus_recommends_tensor_parallel():
    model = ModelSpec(weights_gb=47.0, kv_bytes_per_token=KV_24B)
    a = analyze_inference(_workload(rps=5.0), model, _gpu(count=4), tpot_slo_ms=60)
    assert a.baseline is None  # all-replica layout is infeasible
    assert a.recommended is not None
    assert a.recommended.strategy is ServingStrategy.TENSOR


def test_tight_slo_makes_tp_cheaper_than_replicas():
    """TPOT ≤ 50 ms at 10 req/s: two replicas miss it (≈57 ms), TP=2 meets it.

    The recommendation is decided by GPU count, not by the TPOT tie-break.
    """
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(_workload(rps=10.0), model, _gpu(count=4), tpot_slo_ms=50)
    by_layout = {(c.tp, c.instances): c for c in a.configs}
    assert not by_layout[(1, 2)].slo_ok
    assert by_layout[(1, 4)].slo_ok  # replicas can meet it, but need 4 GPUs
    assert a.recommended is not None
    assert (a.recommended.tp, a.recommended.instances) == (2, 1)


def test_equal_gpus_tie_broken_by_tpot():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(_workload(rps=10.0), model, _gpu(count=4), tpot_slo_ms=60)
    by_layout = {(c.tp, c.instances): c for c in a.configs}
    assert by_layout[(1, 2)].slo_ok and by_layout[(2, 1)].slo_ok
    assert (a.recommended.tp, a.recommended.instances) == (2, 1)


def test_hybrid_recommended_when_tp_capped_by_node():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(
        _workload(rps=20.0), model, _gpu(count=8, gpus_per_node=2), tpot_slo_ms=45
    )
    assert a.recommended is not None
    assert a.recommended.strategy is ServingStrategy.HYBRID
    assert (a.recommended.tp, a.recommended.instances) == (2, 2)


def test_near_saturation_is_not_recommended():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    gpu = _gpu(count=1)
    cap = evaluate_config(1, 1, _workload(rps=0.1), model, gpu).capacity_rps
    w = _workload(rps=cap * 0.97)
    r = evaluate_config(1, 1, w, model, gpu)
    assert not r.saturated and r.near_saturation
    assert analyze_inference(w, model, gpu).recommended is None


def test_ttft_flat_across_replicas_when_queueing_is_negligible():
    """Pins the documented bound: with b_eff ≪ b_max, TTFT p99 ≈ prefill time."""
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(_workload(rps=2.0), model, _gpu(count=4))
    ttfts = {c.ttft_p99_ms for c in a.configs if c.tp == 1 and not c.saturated}
    assert len(ttfts) == 1


def test_analyze_skips_but_what_if_rejects_tp_beyond_node():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    gpu = _gpu(count=16, gpus_per_node=8)
    a = analyze_inference(_workload(), model, gpu, EngineSpec(tp_degrees=(8, 16)))
    assert {c.tp for c in a.configs} == {8}
    with pytest.raises(InferenceDomainError, match="cross-node"):
        evaluate_config(16, 1, _workload(), model, gpu)


def test_unreachable_slo_recommends_nothing():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(_workload(), model, _gpu(count=4), tpot_slo_ms=1.0)
    assert a.recommended is None
    assert a.best_effort is not None


def test_representative_configs_one_row_per_tp():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    a = analyze_inference(_workload(), model, _gpu(count=8))
    rows = representative_configs(a)
    assert [r.tp for r in rows] == [1, 2, 4, 8]


# ---------------------------------------------------------------------------
# Fail-closed validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [math.nan, math.inf, -1.0, 0.0, True, "3"])
def test_workload_rejects_junk(bad):
    with pytest.raises(InferenceDomainError):
        InferenceWorkload(requests_per_second=bad, prompt_tokens=100, output_tokens=100)


def test_specs_reject_junk():
    with pytest.raises(InferenceDomainError):
        ModelSpec(weights_gb=math.nan, kv_bytes_per_token=KV_8B)
    with pytest.raises(InferenceDomainError):
        GpuSpec(count=0, memory_gb=48, bandwidth_gbs=864)
    with pytest.raises(InferenceDomainError):
        GpuSpec(count=1, memory_gb=48, bandwidth_gbs=864, memory_utilization=1.5)
    with pytest.raises(InferenceDomainError):
        EngineSpec(tp_degrees=(3,))
    with pytest.raises(InferenceDomainError):
        EngineSpec(tp_degrees=())


def test_evaluate_config_rejects_out_of_domain():
    model = ModelSpec(weights_gb=16.0, kv_bytes_per_token=KV_8B)
    with pytest.raises(InferenceDomainError, match="one of"):
        evaluate_config(3, 1, _workload(), model, _gpu(count=8))
    with pytest.raises(InferenceDomainError, match="cross-node"):
        evaluate_config(4, 1, _workload(), model, _gpu(count=8, gpus_per_node=2))
    with pytest.raises(InferenceDomainError, match="budget"):
        evaluate_config(2, 3, _workload(), model, _gpu(count=4))
    with pytest.raises(InferenceDomainError):
        evaluate_config(1, 1, _workload(), model, _gpu(), ttft_slo_ms=math.nan)
    with pytest.raises(InferenceDomainError):
        evaluate_config(
            1,
            1,
            _workload(),
            model,
            _gpu(),
            params=EngineParams(bandwidth_efficiency=2.0),
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

runner = CliRunner()

_COMMON = [
    "-r", "5", "-p", "1000", "-o", "256", "-w", "47", "-k", "163840",
    "-m", "48", "-b", "864",
]  # fmt: skip


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def test_cli_infer_analyze_table():
    result = _invoke("infer-analyze", *_COMMON, "-n", "4", "--tpot-slo-ms", "60")
    assert result.exit_code == 0, result.output
    assert "Recommended:  tensor" in result.output
    assert "uncalibrated" in result.output
    assert "no fit" in result.output  # the all-replica layout cannot hold W


def test_cli_infer_analyze_json():
    result = _invoke(
        "infer-analyze", *_COMMON, "-n", "4", "--cost-per-gpu-hour", "1.2", "--json"
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["modelled"] is True and data["calibrated"] is False
    assert data["recommended"]["strategy"] == "tensor"
    assert data["baseline"] is None
    assert len(data["configs"]) == 7


def test_cli_infer_analyze_unreachable_slo():
    result = _invoke("infer-analyze", *_COMMON, "-n", "4", "--tpot-slo-ms", "1")
    assert result.exit_code == 0, result.output
    assert "No configuration meets the SLO" in result.output


def test_cli_infer_analyze_rejects_nan():
    result = _invoke("infer-analyze", *_COMMON[:-2], "-b", "nan", "-n", "4")
    assert result.exit_code == 2


def test_cli_infer_what_if_json():
    result = _invoke("infer-what-if", "--tp", "2", *_COMMON, "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["strategy"] == "tensor"
    assert data["gpus"] == 2
    assert data["feasible"] is True


def test_cli_infer_what_if_infeasible_table():
    result = _invoke("infer-what-if", "--tp", "1", "-i", "2", *_COMMON)
    assert result.exit_code == 0, result.output
    assert "infeasible" in result.output


def test_cli_infer_what_if_rejects_unsupported_tp():
    result = _invoke("infer-what-if", "--tp", "3", *_COMMON)
    assert result.exit_code == 2
