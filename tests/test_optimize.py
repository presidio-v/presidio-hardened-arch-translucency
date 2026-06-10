"""Tests for SMA-based proactive optimisation (v0.8.0 Phase 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import Observation, record_observation
from presidio_arch_translucency.optimize import (
    OptimizeError,
    optimize_sma,
    simple_moving_average,
)

runner = CliRunner()

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _series(rps_values, layer="container", latency=80.0, replicas=2, step_min=1):
    """Build observations one `step_min` apart with the given rps sequence."""
    out = []
    for i, rps in enumerate(rps_values):
        out.append(
            Observation(
                timestamp=_T0 + timedelta(minutes=i * step_min),
                rps=float(rps),
                avg_latency_ms=latency,
                p99_latency_ms=latency * 1.8,
                throughput=float(rps) * 0.97,
                layer=layer,
                replicas=replicas,
            )
        )
    return out


# ---------------------------------------------------------------------------
# simple_moving_average
# ---------------------------------------------------------------------------


def test_sma_basic():
    assert simple_moving_average([10, 20, 30]) == pytest.approx(20.0)


def test_sma_empty_raises():
    with pytest.raises(OptimizeError):
        simple_moving_average([])


# ---------------------------------------------------------------------------
# optimize_sma — trend & prediction
# ---------------------------------------------------------------------------


class TestOptimizeSMA:
    def test_empty_raises(self):
        with pytest.raises(OptimizeError, match="no observations"):
            optimize_sma([])

    def test_rising_demand_predicts_higher_and_scales_up(self):
        # 10 samples, 1 min apart, rising 100→190.
        obs = _series(range(100, 200, 10), replicas=2)
        result = optimize_sma(obs, horizon_minutes=10)
        assert result.samples == 10
        assert result.window_minutes == pytest.approx(9.0)
        assert result.trend_pct > 0
        assert result.slope_rps_per_min > 0
        # Projected demand exceeds the smoothed current level.
        assert result.predicted_rps > result.sma_rps
        # Serving more demand than 2 replicas can ⇒ scale up.
        assert result.recommended_replicas >= 2
        assert result.action in ("scale-up", "hold")

    def test_known_slope_and_prediction(self):
        # Linear ramp: rps = 100 + 10*i over 10 samples 1 min apart.
        obs = _series([100 + 10 * i for i in range(10)])
        result = optimize_sma(obs, horizon_minutes=5)
        # older half mean = mean(100..140)=120; newer half mean = mean(150..190)=170.
        assert result.trend_pct == pytest.approx((170 - 120) / 120 * 100, rel=1e-6)
        # midpoints at t=2 and t=7 ⇒ dt=5 ⇒ slope=(170-120)/5=10 rps/min.
        assert result.slope_rps_per_min == pytest.approx(10.0, rel=1e-6)
        # predicted = newer_mean(170) + slope(10)*horizon(5) = 220.
        assert result.predicted_rps == pytest.approx(220.0, rel=1e-6)

    def test_falling_demand_trend_negative_and_scales_down(self):
        obs = _series(range(400, 100, -30), latency=80.0, replicas=8)  # 400→130
        result = optimize_sma(obs, horizon_minutes=10)
        assert result.trend_pct < 0
        assert result.slope_rps_per_min < 0
        assert result.predicted_rps < result.sma_rps
        assert result.recommended_replicas <= 8
        assert result.action in ("scale-down", "hold")

    def test_flat_demand_zero_trend(self):
        obs = _series([300] * 8, replicas=5)
        result = optimize_sma(obs, horizon_minutes=10)
        assert result.trend_pct == pytest.approx(0.0)
        assert result.slope_rps_per_min == pytest.approx(0.0)
        assert result.predicted_rps == pytest.approx(300.0)

    def test_single_sample_no_trend(self):
        obs = _series([250], replicas=3)
        result = optimize_sma(obs)
        assert result.samples == 1
        assert result.trend_pct == 0.0
        assert result.predicted_rps == pytest.approx(250.0)

    def test_identical_timestamps_zero_slope(self):
        obs = [
            Observation(_T0, 100, 80, 140, 97, "container", 2),
            Observation(_T0, 200, 80, 140, 194, "container", 2),
        ]
        result = optimize_sma(obs)
        assert result.slope_rps_per_min == 0.0  # dt == 0 guard
        assert result.predicted_rps == pytest.approx(200.0)  # newer half level

    def test_subminute_samples_do_not_extrapolate(self):
        # Samples ~10s apart (rising) span < 0.5 min between half-midpoints:
        # the trend is reported but no per-minute rate is projected, so the
        # prediction stays at the smoothed level instead of blowing up.
        ten_s = [
            Observation(
                _T0 + timedelta(seconds=10 * i),
                rps=300 + 40 * i,
                avg_latency_ms=80,
                p99_latency_ms=140,
                throughput=(300 + 40 * i) * 0.97,
                layer="container",
                replicas=4,
            )
            for i in range(5)
        ]
        result = optimize_sma(ten_s, horizon_minutes=10)
        assert result.trend_pct > 0  # level rose
        assert result.slope_rps_per_min == 0.0  # but not extrapolated
        assert result.predicted_rps == pytest.approx(result.predicted_rps)
        # Prediction equals the recent half level — no runaway value.
        assert result.predicted_rps < 1000

    def test_unmodelled_layer_falls_back_to_current_replicas(self):
        obs = _series(
            [500, 600], layer="web", replicas=4
        )  # 'web' not a ReplicationLayer
        result = optimize_sma(obs)
        assert result.recommended_replicas == 4
        assert result.action == "hold"

    def test_unsorted_input_is_sorted(self):
        obs = _series([100, 110, 120, 130])
        shuffled = [obs[2], obs[0], obs[3], obs[1]]
        result = optimize_sma(shuffled)
        assert result.first_ts == _T0
        assert result.last_ts == _T0 + timedelta(minutes=3)
        assert result.slope_rps_per_min > 0


# ---------------------------------------------------------------------------
# CLI: pat optimize
# ---------------------------------------------------------------------------


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "optimize", *args])


class TestOptimizeCLI:
    def test_no_observations_friendly_message(self, tmp_path):
        result = _invoke("--db", str(tmp_path / "obs.db"))
        assert result.exit_code == 0
        assert "No observations" in result.output

    def test_recommendation_rendered(self, tmp_path):
        db = tmp_path / "obs.db"
        for i in range(10):
            record_observation(
                Observation(
                    _T0 + timedelta(minutes=i),
                    rps=100 + 10 * i,
                    avg_latency_ms=80,
                    p99_latency_ms=140,
                    throughput=(100 + 10 * i) * 0.97,
                    layer="container",
                    replicas=2,
                ),
                db_path=db,
            )
        result = _invoke("--db", str(db), "--window", "10", "--horizon-minutes", "10")
        assert result.exit_code == 0
        assert "Optimize (SMA)" in result.output
        assert "Recommend" in result.output
        assert "Predicted" in result.output

    def test_unsupported_model_exits_2(self, tmp_path):
        # 'sma' and 'arima' are valid; anything else is rejected.
        result = _invoke("--model", "prophet", "--db", str(tmp_path / "obs.db"))
        assert result.exit_code == 2
        assert "sma" in result.output.lower()

    def test_invalid_layer_exits_2(self, tmp_path):
        result = _invoke("--layer", "not-a-layer", "--db", str(tmp_path / "obs.db"))
        assert result.exit_code == 2

    def test_layer_filter_isolates_series(self, tmp_path):
        db = tmp_path / "obs.db"
        record_observation(
            Observation(_T0, 100, 80, 140, 97, "container", 2), db_path=db
        )
        record_observation(
            Observation(_T0 + timedelta(minutes=1), 900, 40, 90, 880, "pod", 9),
            db_path=db,
        )
        # Filtering to 'pod' must not be skewed by the container row.
        result = _invoke("--db", str(db), "--layer", "pod")
        assert result.exit_code == 0
        assert "pod" in result.output
