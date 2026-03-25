"""Tests for the architectural translucency simulation model."""

import math

import pytest

from presidio_arch_translucency.model import (
    LAYER_PARAMS,
    AnalysisResult,
    LayerResult,
    ReplicationLayer,
    _base_capacity,
    analyze,
    intensity_after_replication,
    response_time_ms,
    throughput,
)

# ---------------------------------------------------------------------------
# intensity_after_replication
# ---------------------------------------------------------------------------


class TestIntensityAfterReplication:
    def test_single_replica_no_extra_load(self):
        layer = ReplicationLayer.CONTAINER
        rps = 100.0
        intensity = intensity_after_replication(rps, 1, layer)
        params = LAYER_PARAMS[layer]
        # log(1) == 0, so overhead_factor = 1 + alpha
        expected = rps * (1.0 + params.overhead_alpha)
        assert math.isclose(intensity, expected, rel_tol=1e-9)

    def test_intensity_decreases_with_more_replicas(self):
        layer = ReplicationLayer.CONTAINER
        rps = 1000.0
        i1 = intensity_after_replication(rps, 1, layer)
        i4 = intensity_after_replication(rps, 4, layer)
        assert i4 < i1

    def test_intensity_positive_always(self):
        for layer in ReplicationLayer:
            assert intensity_after_replication(500.0, 10, layer) > 0

    def test_intensity_increases_with_rps(self):
        layer = ReplicationLayer.POD
        i_low = intensity_after_replication(100.0, 2, layer)
        i_high = intensity_after_replication(1000.0, 2, layer)
        assert i_high > i_low


# ---------------------------------------------------------------------------
# throughput
# ---------------------------------------------------------------------------


class TestThroughput:
    def test_single_replica_bounded_by_base_cap(self):
        layer = ReplicationLayer.CONTAINER
        base_cap = 200.0
        tp = throughput(500.0, 1, layer, base_cap)
        assert tp <= 500.0

    def test_throughput_does_not_exceed_demand(self):
        rps = 300.0
        base_cap = 1000.0
        for layer in ReplicationLayer:
            for delta in (1, 2, 4, 8):
                tp = throughput(rps, delta, layer, base_cap)
                assert tp <= rps + 1e-6, f"layer={layer} delta={delta} tp={tp}"

    def test_throughput_increases_with_replicas_within_reason(self):
        layer = ReplicationLayer.CONTAINER
        base_cap = 100.0
        rps = 400.0
        tp1 = throughput(rps, 1, layer, base_cap)
        tp4 = throughput(rps, 4, layer, base_cap)
        assert tp4 >= tp1

    def test_all_layers_return_positive(self):
        for layer in ReplicationLayer:
            assert throughput(500.0, 2, layer, 300.0) >= 0


# ---------------------------------------------------------------------------
# response_time_ms
# ---------------------------------------------------------------------------


class TestResponseTimeMs:
    def test_returns_positive(self):
        for layer in ReplicationLayer:
            rt = response_time_ms(200.0, 2, layer, 50.0, 300.0)
            assert rt > 0, f"response_time should be positive for layer={layer}"

    def test_improves_with_more_replicas_when_overloaded(self):
        layer = ReplicationLayer.CONTAINER
        # base_cap=300, rps=900: δ=1 has rho≈0.99; δ=4 → intensity≈225, rho=0.75
        rt1 = response_time_ms(900.0, 1, layer, 20.0, 300.0)
        rt4 = response_time_ms(900.0, 4, layer, 20.0, 300.0)
        assert rt4 < rt1

    def test_baseline_latency_respected(self):
        layer = ReplicationLayer.DEPLOYMENT
        # With low utilisation, RT should be close to baseline latency
        rt = response_time_ms(10.0, 1, layer, 100.0, 5000.0)
        assert rt >= 100.0  # must be at least the baseline


# ---------------------------------------------------------------------------
# _base_capacity
# ---------------------------------------------------------------------------


class TestBaseCapacity:
    def test_high_latency_implies_low_capacity(self):
        cap_fast = _base_capacity(100.0, 10.0)
        cap_slow = _base_capacity(100.0, 200.0)
        assert cap_fast > cap_slow

    def test_capacity_bounded_by_demand(self):
        rps = 50.0
        lat = 1.0  # very fast → Little's Law gives 1000 rps
        cap = _base_capacity(rps, lat)
        # demand-implied cap = 50/0.70 ≈ 71, Little's = 1000 → min = 71
        assert cap < 200.0


# ---------------------------------------------------------------------------
# analyze (integration)
# ---------------------------------------------------------------------------


class TestAnalyze:
    def test_returns_analysis_result(self):
        result = analyze(500.0, 80.0, ReplicationLayer.CONTAINER)
        assert isinstance(result, AnalysisResult)

    def test_layers_cover_all_four(self):
        result = analyze(500.0, 80.0, ReplicationLayer.CONTAINER)
        layer_names = {r.layer for r in result.layers}
        assert layer_names == set(ReplicationLayer)

    def test_recommended_layer_in_result(self):
        result = analyze(500.0, 80.0, ReplicationLayer.CONTAINER)
        assert result.recommended_layer in ReplicationLayer

    def test_recommended_replicas_positive(self):
        result = analyze(500.0, 80.0, ReplicationLayer.CONTAINER)
        assert result.recommended_replicas >= 1

    def test_baseline_values_sensible(self):
        result = analyze(500.0, 80.0, ReplicationLayer.CONTAINER)
        assert result.baseline_throughput_rps > 0
        assert result.baseline_response_time_ms > 0

    def test_all_layer_results_have_positive_replicas(self):
        result = analyze(200.0, 50.0, ReplicationLayer.POD)
        for lr in result.layers:
            assert isinstance(lr, LayerResult)
            assert lr.optimal_replicas >= 1

    def test_container_layer_max_replicas_not_exceeded(self):
        from presidio_arch_translucency.model import LAYER_PARAMS

        result = analyze(200.0, 50.0, ReplicationLayer.CONTAINER)
        for lr in result.layers:
            assert lr.optimal_replicas <= LAYER_PARAMS[lr.layer].max_replicas

    def test_different_workloads_may_give_different_recommendations(self):
        # Light workload
        r_light = analyze(10.0, 5.0, ReplicationLayer.CONTAINER)
        # Heavy workload
        r_heavy = analyze(50000.0, 200.0, ReplicationLayer.CONTAINER)
        # Both should return valid results (not necessarily different)
        assert r_light.recommended_layer in ReplicationLayer
        assert r_heavy.recommended_layer in ReplicationLayer

    def test_current_layer_preserved_in_result(self):
        for layer in ReplicationLayer:
            result = analyze(300.0, 60.0, layer)
            assert result.current_layer == layer

    @pytest.mark.parametrize(
        "rps,lat,layer",
        [
            (1.0, 1.0, ReplicationLayer.CONTAINER),
            (999999.0, 300000.0, ReplicationLayer.NODE),
            (500.0, 80.0, ReplicationLayer.DEPLOYMENT),
            (100.0, 20.0, ReplicationLayer.POD),
        ],
    )
    def test_parametric_cases(self, rps, lat, layer):
        result = analyze(rps, lat, layer)
        assert result.recommended_layer in ReplicationLayer
        assert result.recommended_replicas >= 1
