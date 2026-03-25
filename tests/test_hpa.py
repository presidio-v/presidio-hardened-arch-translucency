"""
Unit tests for the HPA lag model (hpa.py).
No Docker or external services required.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.hpa import (
    DEFAULT_COLD_START_S,
    DEFAULT_HPA_POLL_S,
    DEFAULT_POD_STARTUP_S,
    ScaleEventParams,
    ScaleEventResult,
    TimePoint,
    _p99_multiplier,
    _utilization,
    optimal_replicas_for_rps,
    save_hpa_plot,
    simulate_scale_event,
)
from presidio_arch_translucency.model import ReplicationLayer

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ── ScaleEventParams ───────────────────────────────────────────────────────────


def test_scale_event_params_defaults() -> None:
    p = ScaleEventParams()
    assert p.hpa_poll_s == DEFAULT_HPA_POLL_S
    assert p.pod_startup_s == DEFAULT_POD_STARTUP_S
    assert p.cold_start_s == DEFAULT_COLD_START_S


def test_time_to_ready_no_cold_start() -> None:
    p = ScaleEventParams(hpa_poll_s=15.0, pod_startup_s=30.0, cold_start_s=0.0)
    assert p.time_to_ready_s == 45.0


def test_time_to_ready_with_cold_start() -> None:
    p = ScaleEventParams(hpa_poll_s=15.0, pod_startup_s=30.0, cold_start_s=20.0)
    assert p.time_to_ready_s == 65.0


def test_custom_params() -> None:
    p = ScaleEventParams(hpa_poll_s=10.0, pod_startup_s=20.0, cold_start_s=5.0)
    assert p.time_to_ready_s == 35.0


# ── _utilization ──────────────────────────────────────────────────────────────


def test_utilization_basic() -> None:
    # 100 rps, 1 replica, 10ms latency → μ = 100/s → ρ = 100/100 = 1.0
    assert _utilization(100.0, 1, 10.0) == pytest.approx(1.0)


def test_utilization_below_capacity() -> None:
    # 50 rps, 2 replicas, 10ms → μ = 100/s each → ρ = 50/200 = 0.25
    assert _utilization(50.0, 2, 10.0) == pytest.approx(0.25)


def test_utilization_zero_replicas() -> None:
    assert _utilization(100.0, 0, 10.0) == 1.0


def test_utilization_zero_latency() -> None:
    assert _utilization(100.0, 2, 0.0) == 1.0


# ── _p99_multiplier ───────────────────────────────────────────────────────────


def test_p99_overloaded() -> None:
    assert _p99_multiplier(1.0) == 15.0
    assert _p99_multiplier(2.0) == 15.0


def test_p99_high_utilization() -> None:
    assert _p99_multiplier(0.95) == 8.0


def test_p99_medium_utilization() -> None:
    assert _p99_multiplier(0.75) == 4.0


def test_p99_moderate_utilization() -> None:
    assert _p99_multiplier(0.6) == 2.5


def test_p99_low_utilization() -> None:
    assert _p99_multiplier(0.3) == 1.8


def test_p99_boundary_exactly_09() -> None:
    assert _p99_multiplier(0.9) == 8.0


# ── optimal_replicas_for_rps ──────────────────────────────────────────────────


def test_optimal_replicas_positive() -> None:
    r = optimal_replicas_for_rps(50.0, 80.0, ReplicationLayer.CONTAINER)
    assert r >= 1


def test_optimal_replicas_spike_needs_more_than_baseline() -> None:
    r_base = optimal_replicas_for_rps(50.0, 80.0, ReplicationLayer.CONTAINER)
    r_spike = optimal_replicas_for_rps(200.0, 80.0, ReplicationLayer.CONTAINER)
    assert r_spike >= r_base


def test_optimal_replicas_capped_at_max() -> None:
    # node layer max_replicas = 8
    r = optimal_replicas_for_rps(100_000.0, 80.0, ReplicationLayer.NODE)
    assert r == 8


def test_optimal_replicas_all_layers_positive() -> None:
    for layer in ReplicationLayer:
        assert optimal_replicas_for_rps(100.0, 50.0, layer) >= 1


# ── simulate_scale_event ──────────────────────────────────────────────────────


def _run(layer: ReplicationLayer = ReplicationLayer.CONTAINER) -> ScaleEventResult:
    return simulate_scale_event(
        rps_baseline=50.0,
        rps_spike=200.0,
        avg_latency_ms=80.0,
        layer=layer,
    )


def test_simulate_returns_result() -> None:
    r = _run()
    assert isinstance(r, ScaleEventResult)


def test_trough_throughput_less_than_spike() -> None:
    r = _run()
    assert r.trough_throughput_rps <= r.rps_spike


def test_trough_latency_higher_than_steady() -> None:
    r = _run()
    assert r.trough_avg_latency_ms >= r.steady_avg_latency_ms


def test_trough_p99_higher_than_steady_p99() -> None:
    r = _run()
    assert r.trough_p99_latency_ms >= r.steady_p99_latency_ms


def test_missed_requests_positive_when_overloaded() -> None:
    r = _run()
    assert r.missed_requests >= 0  # could be 0 if trough_tp >= spike
    # For a 4× spike with 1 replica before, should have misses
    r2 = simulate_scale_event(
        50.0, 200.0, 80.0, ReplicationLayer.CONTAINER, replicas_before=1
    )
    assert r2.missed_requests > 0


def test_replicas_after_gt_before_on_spike() -> None:
    r = _run()
    assert r.replicas_after >= r.replicas_before


def test_trough_duration_equals_time_to_ready() -> None:
    params = ScaleEventParams(hpa_poll_s=10.0, pod_startup_s=20.0)
    r = simulate_scale_event(
        50.0, 200.0, 80.0, ReplicationLayer.CONTAINER, params=params
    )
    assert r.trough_duration_s == pytest.approx(30.0)


def test_custom_replicas_respected() -> None:
    r = simulate_scale_event(
        50.0,
        200.0,
        80.0,
        ReplicationLayer.CONTAINER,
        replicas_before=2,
        replicas_after=8,
    )
    assert r.replicas_before == 2
    assert r.replicas_after == 8


def test_timeline_has_points() -> None:
    r = _run()
    assert len(r.timeline) >= 5


def test_timeline_points_are_timepoints() -> None:
    r = _run()
    for pt in r.timeline:
        assert isinstance(pt, TimePoint)


def test_timeline_sorted_by_time() -> None:
    r = _run()
    times = [pt.t_s for pt in r.timeline]
    assert times == sorted(times)


def test_timeline_first_point_in_trough() -> None:
    r = _run()
    first = r.timeline[0]
    assert first.t_s == 0.0
    assert first.replicas == r.replicas_before


def test_timeline_last_point_in_steady() -> None:
    r = _run()
    last = r.timeline[-1]
    assert last.replicas == r.replicas_after


def test_all_layers_simulate() -> None:
    for layer in ReplicationLayer:
        r = simulate_scale_event(50.0, 200.0, 80.0, layer)
        assert r.steady_throughput_rps > 0


# ── save_hpa_plot ─────────────────────────────────────────────────────────────


def test_save_hpa_plot_creates_file(tmp_path: Path) -> None:
    r = _run()
    out = tmp_path / "hpa.png"
    save_hpa_plot(r, out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_save_hpa_plot_with_cold_start(tmp_path: Path) -> None:
    params = ScaleEventParams(cold_start_s=15.0)
    r = simulate_scale_event(50.0, 200.0, 80.0, ReplicationLayer.POD, params=params)
    out = tmp_path / "hpa_cold.png"
    save_hpa_plot(r, out)
    assert out.exists()


# ── CLI: pat what-if ──────────────────────────────────────────────────────────


def test_what_if_basic() -> None:
    result = runner.invoke(
        app,
        [
            "--skip-audit",
            "what-if",
            "--current-rps",
            "50",
            "--spike-rps",
            "200",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "container",
        ],
    )
    assert result.exit_code == 0, result.output
    out = strip_ansi(result.output)
    assert "TROUGH" in out
    assert "STEADY" in out
    assert "Missed" in out


def test_what_if_spike_must_exceed_current() -> None:
    result = runner.invoke(
        app,
        [
            "--skip-audit",
            "what-if",
            "--current-rps",
            "200",
            "--spike-rps",
            "100",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "container",
        ],
    )
    assert result.exit_code == 2


def test_what_if_custom_hpa_params() -> None:
    result = runner.invoke(
        app,
        [
            "--skip-audit",
            "what-if",
            "--current-rps",
            "50",
            "--spike-rps",
            "200",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "pod",
            "--hpa-poll-s",
            "10",
            "--pod-startup-s",
            "20",
            "--cold-start-s",
            "5",
        ],
    )
    assert result.exit_code == 0
    assert "35" in strip_ansi(result.output)  # 10+20+5 = 35s trough


def test_what_if_with_plot_output(tmp_path: Path) -> None:
    out = tmp_path / "event.png"
    result = runner.invoke(
        app,
        [
            "--skip-audit",
            "what-if",
            "--current-rps",
            "50",
            "--spike-rps",
            "200",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "container",
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0
    assert out.exists()


# ── CLI: pat slo ──────────────────────────────────────────────────────────────


def test_slo_basic() -> None:
    result = runner.invoke(
        app,
        [
            "--skip-audit",
            "slo",
            "--requests-per-second",
            "50",
            "--avg-latency-ms",
            "80",
            "--p99-target-ms",
            "500",
        ],
    )
    assert result.exit_code == 0, result.output
    out = strip_ansi(result.output)
    assert "container" in out
    assert "SLO" in out
    assert "Recommendation" in out


def test_slo_all_layers_shown() -> None:
    result = runner.invoke(
        app,
        [
            "--skip-audit",
            "slo",
            "--requests-per-second",
            "50",
            "--avg-latency-ms",
            "80",
            "--p99-target-ms",
            "500",
        ],
    )
    out = strip_ansi(result.output)
    for layer in ("container", "pod", "deployment", "node"):
        assert layer in out


def test_slo_custom_spike_multiplier() -> None:
    result = runner.invoke(
        app,
        [
            "--skip-audit",
            "slo",
            "--requests-per-second",
            "50",
            "--avg-latency-ms",
            "80",
            "--p99-target-ms",
            "200",
            "--spike-multiplier",
            "2.0",
        ],
    )
    assert result.exit_code == 0
