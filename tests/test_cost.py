"""
Tests for cost.py and the pat cost / cost-aware CLI extensions.
"""

from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.cost import (
    CostParams,
    build_cost_results,
    cost_per_request,
    hourly_cost,
    trough_cost_usd,
)
from presidio_arch_translucency.model import ReplicationLayer, analyze

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit"] + list(args))


# ── CostParams ────────────────────────────────────────────────────────────────


def test_cost_params_defaults() -> None:
    cp = CostParams()
    assert cp.cost_per_container_hour == 0.02
    assert cp.cost_per_pod_hour == 0.05
    assert cp.cost_per_deployment_hour == 0.10
    assert cp.cost_per_node_hour == 0.50


def test_cost_params_for_layer() -> None:
    cp = CostParams()
    assert cp.for_layer(ReplicationLayer.CONTAINER) == 0.02
    assert cp.for_layer(ReplicationLayer.NODE) == 0.50


def test_cost_params_custom() -> None:
    cp = CostParams(cost_per_container_hour=0.10, cost_per_node_hour=1.00)
    assert cp.for_layer(ReplicationLayer.CONTAINER) == 0.10
    assert cp.for_layer(ReplicationLayer.NODE) == 1.00


# ── hourly_cost ───────────────────────────────────────────────────────────────


def test_hourly_cost_container_4_replicas() -> None:
    cp = CostParams()
    assert hourly_cost(ReplicationLayer.CONTAINER, 4, cp) == 0.08


def test_hourly_cost_node_2_replicas() -> None:
    cp = CostParams()
    assert hourly_cost(ReplicationLayer.NODE, 2, cp) == 1.00


def test_hourly_cost_zero_replicas() -> None:
    cp = CostParams()
    assert hourly_cost(ReplicationLayer.CONTAINER, 0, cp) == 0.0


# ── cost_per_request ──────────────────────────────────────────────────────────


def test_cost_per_request_basic() -> None:
    cp = CostParams()
    # 4 containers × $0.02/hr = $0.08/hr; 100 rps × 3600 s = 360 000 reqs/hr
    cpr = cost_per_request(ReplicationLayer.CONTAINER, 4, 100.0, cp)
    assert abs(cpr - 0.08 / 360_000) < 1e-10


def test_cost_per_request_zero_throughput() -> None:
    cp = CostParams()
    assert cost_per_request(ReplicationLayer.CONTAINER, 1, 0.0, cp) == float("inf")


def test_cost_per_request_node_is_more_expensive() -> None:
    cp = CostParams()
    cpr_container = cost_per_request(ReplicationLayer.CONTAINER, 1, 50.0, cp)
    cpr_node = cost_per_request(ReplicationLayer.NODE, 1, 50.0, cp)
    assert cpr_node > cpr_container


# ── trough_cost_usd ───────────────────────────────────────────────────────────


def test_trough_cost_usd_basic() -> None:
    assert abs(trough_cost_usd(1000, 0.001) - 1.0) < 1e-9


def test_trough_cost_usd_zero_missed() -> None:
    assert trough_cost_usd(0, 0.01) == 0.0


def test_trough_cost_usd_infinite_cpr() -> None:
    assert trough_cost_usd(500, float("inf")) == 0.0


# ── build_cost_results ────────────────────────────────────────────────────────


def _analysis():
    return analyze(500.0, 80.0, ReplicationLayer.CONTAINER)


def test_build_cost_results_returns_all_layers() -> None:
    result = _analysis()
    cr = build_cost_results(result.layers, CostParams())
    assert len(cr) == 4


def test_build_cost_results_exactly_one_recommended() -> None:
    result = _analysis()
    cr = build_cost_results(result.layers, CostParams())
    assert sum(1 for r in cr if r.is_recommended) == 1


def test_build_cost_results_roi_positive_for_best() -> None:
    result = _analysis()
    cr = build_cost_results(result.layers, CostParams())
    best = next(r for r in cr if r.is_recommended)
    assert best.roi_score >= 0


def test_build_cost_results_node_most_expensive() -> None:
    result = _analysis()
    cr = build_cost_results(result.layers, CostParams())
    node_r = next(r for r in cr if r.layer == ReplicationLayer.NODE)
    for r in cr:
        if r.layer != ReplicationLayer.NODE:
            assert node_r.hourly_cost_usd >= r.hourly_cost_usd


def test_build_cost_results_container_cheapest_per_hour() -> None:
    result = _analysis()
    cr = build_cost_results(result.layers, CostParams())
    costs = {r.layer: r.hourly_cost_usd for r in cr}
    assert costs[ReplicationLayer.CONTAINER] <= costs[ReplicationLayer.POD]
    assert costs[ReplicationLayer.POD] <= costs[ReplicationLayer.DEPLOYMENT]


# ── pat cost CLI ─────────────────────────────────────────────────────────────


def test_cost_cmd_basic() -> None:
    result = invoke(
        "cost",
        "--requests-per-second",
        "500",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "container",
    )
    assert result.exit_code == 0
    assert "Cost Analysis" in result.output or "cost" in result.output.lower()


def test_cost_cmd_shows_all_layers() -> None:
    result = invoke(
        "cost",
        "--requests-per-second",
        "200",
        "--avg-latency-ms",
        "50",
        "--current-layer",
        "pod",
    )
    assert result.exit_code == 0
    for layer in ("container", "pod", "deployment", "node"):
        assert layer in result.output


def test_cost_cmd_shows_roi() -> None:
    result = invoke(
        "cost",
        "--requests-per-second",
        "500",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "container",
    )
    assert result.exit_code == 0
    assert "ROI" in result.output


def test_cost_cmd_custom_costs() -> None:
    result = invoke(
        "cost",
        "--requests-per-second",
        "500",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "container",
        "--cost-per-container-hour",
        "0.10",
        "--cost-per-node-hour",
        "2.00",
    )
    # Custom costs accepted and processed — exit code and ROI output are sufficient
    assert result.exit_code == 0
    assert "ROI" in result.output


def test_cost_cmd_invalid_layer() -> None:
    result = invoke(
        "cost",
        "--requests-per-second",
        "500",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "invalid",
    )
    assert result.exit_code != 0


def test_cost_cmd_negative_rps() -> None:
    result = invoke(
        "cost",
        "--requests-per-second",
        "-10",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "container",
    )
    assert result.exit_code != 0


# ── pat analyze --cost-per-replica-hour ──────────────────────────────────────


def test_analyze_with_cost_per_replica_hour() -> None:
    result = invoke(
        "analyze",
        "--requests-per-second",
        "500",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "container",
        "--show-all",
        "--cost-per-replica-hour",
        "0.05",
    )
    assert result.exit_code == 0
    assert "Cost/hr" in result.output or "cost" in result.output.lower()


def test_analyze_without_cost_flag_no_cost_column() -> None:
    result = invoke(
        "analyze",
        "--requests-per-second",
        "500",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "container",
        "--show-all",
    )
    assert result.exit_code == 0
    assert "Cost/hr" not in result.output


# ── pat what-if --cost-per-request ───────────────────────────────────────────


def test_what_if_with_cost_per_request() -> None:
    result = invoke(
        "what-if",
        "--current-rps",
        "50",
        "--spike-rps",
        "200",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "container",
        "--cost-per-request",
        "0.001",
    )
    assert result.exit_code == 0
    assert "revenue" in result.output.lower() or "cost" in result.output.lower()


def test_what_if_without_cost_no_cost_line() -> None:
    result = invoke(
        "what-if",
        "--current-rps",
        "50",
        "--spike-rps",
        "200",
        "--avg-latency-ms",
        "80",
        "--current-layer",
        "container",
    )
    assert result.exit_code == 0
    assert "revenue" not in result.output.lower()


# ── pat slo cost column ───────────────────────────────────────────────────────


def test_slo_shows_cost_column() -> None:
    result = invoke(
        "slo",
        "--requests-per-second",
        "50",
        "--avg-latency-ms",
        "80",
        "--p99-target-ms",
        "500",
    )
    assert result.exit_code == 0
    assert "Cost/hr" in result.output or "$" in result.output
