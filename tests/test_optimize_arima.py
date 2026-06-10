"""Tests for the ARIMA optimisation path (v0.8.0 Phase 4).

Uses deterministic synthetic series so fits are reproducible. No live data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import Observation, record_observation
from presidio_arch_translucency.optimize import (
    MIN_ARIMA_SAMPLES,
    _horizon_steps,
    optimize_arima,
    optimize_sma,
)

runner = CliRunner()

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ramp(n, start=200.0, slope=5.0, layer="container", replicas=4, step_min=1):
    """A deterministic upward ramp with a small alternating wiggle."""
    out = []
    for i in range(n):
        rps = start + slope * i + (6.0 if i % 2 else -6.0)
        out.append(
            Observation(
                timestamp=_T0 + timedelta(minutes=i * step_min),
                rps=float(rps),
                avg_latency_ms=80.0,
                p99_latency_ms=140.0,
                throughput=float(rps) * 0.97,
                layer=layer,
                replicas=replicas,
            )
        )
    return out


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_horizon_steps_from_interval():
    # samples 1 min apart ⇒ 15-min horizon = 15 steps
    t_min = [float(i) for i in range(20)]
    assert _horizon_steps(t_min, 15.0) == 15
    # 30s apart ⇒ 15 min = 30 steps
    t_min_30s = [i * 0.5 for i in range(20)]
    assert _horizon_steps(t_min_30s, 15.0) == 30


def test_horizon_steps_degenerate_interval_returns_one():
    assert _horizon_steps([0.0, 0.0, 0.0], 15.0) == 1


# ---------------------------------------------------------------------------
# optimize_arima — real fits on synthetic data
# ---------------------------------------------------------------------------


class TestOptimizeARIMA:
    def test_fits_and_produces_interval(self):
        result = optimize_arima(_ramp(36), horizon_minutes=15)
        assert result.model == "arima"
        assert result.fallback_reason is None
        assert result.arima_order is not None
        assert len(result.arima_order) == 3
        # 95% CI present and correctly ordered around the point estimate.
        assert result.has_interval
        assert (
            result.predicted_rps_lower
            <= result.predicted_rps
            <= result.predicted_rps_upper
        )

    def test_upward_trend_forecasts_above_mean(self):
        result = optimize_arima(_ramp(40, start=200, slope=6), horizon_minutes=15)
        # A sustained ramp ⇒ the forecast sits above the smoothed window mean.
        assert result.predicted_rps > result.sma_rps

    def test_replica_range_brackets_point(self):
        result = optimize_arima(_ramp(36), horizon_minutes=15)
        assert result.recommended_replicas_lower is not None
        assert result.recommended_replicas_upper is not None
        assert (
            result.recommended_replicas_lower
            <= result.recommended_replicas
            <= result.recommended_replicas_upper
        )

    def test_constant_demand_series_is_robust(self):
        # A flat series makes several ARIMA orders fail/return non-finite AIC;
        # the grid search must skip those and still produce a usable forecast.
        flat = [
            Observation(_T0 + timedelta(minutes=i), 300.0, 80, 140, 291, "container", 5)
            for i in range(32)
        ]
        result = optimize_arima(flat, horizon_minutes=15)
        assert result.model == "arima"
        assert result.predicted_rps == pytest.approx(300.0, abs=50.0)

    # -- fallback: < MIN_ARIMA_SAMPLES ------------------------------------

    def test_falls_back_to_sma_below_threshold(self):
        result = optimize_arima(_ramp(12), horizon_minutes=15)
        assert result.model == "sma"
        assert result.fallback_reason is not None
        assert "30" in result.fallback_reason
        assert not result.has_interval

    def test_fallback_uses_recent_window(self):
        # 25 samples (< 30) ⇒ SMA over the most recent DEFAULT_WINDOW (10).
        result = optimize_arima(_ramp(25), horizon_minutes=15)
        assert result.model == "sma"
        assert result.samples <= 10

    def test_threshold_boundary_uses_arima(self):
        result = optimize_arima(_ramp(MIN_ARIMA_SAMPLES), horizon_minutes=15)
        assert result.model == "arima"

    # -- fallback: no order converges -------------------------------------

    def test_falls_back_when_no_order_converges(self):
        with patch(
            "presidio_arch_translucency.optimize._fit_best_arima", return_value=None
        ):
            result = optimize_arima(_ramp(40), horizon_minutes=15)
        assert result.model == "sma"
        assert "converge" in result.fallback_reason

    # -- regression: SMA path carries no interval -------------------------

    def test_sma_result_has_no_interval(self):
        result = optimize_sma(_ramp(10), horizon_minutes=10)
        assert result.model == "sma"
        assert not result.has_interval
        assert result.predicted_rps_lower is None


# ---------------------------------------------------------------------------
# CLI: pat optimize --model arima
# ---------------------------------------------------------------------------


def _seed(db, n, **kw):
    for obs in _ramp(n, **kw):
        record_observation(obs, db_path=db)


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "optimize", *args])


class TestOptimizeARIMACLI:
    def test_arima_renders_interval(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed(db, 36)
        result = _invoke("--model", "arima", "--horizon-minutes", "15", "--db", str(db))
        assert result.exit_code == 0
        assert "ARIMA(" in result.output
        assert "95% CI" in result.output
        assert "Optimize (ARIMA" in result.output

    def test_arima_below_threshold_falls_back_with_warning(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed(db, 8)
        result = _invoke("--model", "arima", "--db", str(db))
        assert result.exit_code == 0
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "used SMA" in combined
        assert "Optimize (SMA)" in result.output

    def test_unsupported_model_exits_2(self, tmp_path):
        result = _invoke("--model", "prophet", "--db", str(tmp_path / "obs.db"))
        assert result.exit_code == 2
        assert "sma" in result.output.lower()

    def test_arima_empty_store_friendly(self, tmp_path):
        result = _invoke("--model", "arima", "--db", str(tmp_path / "obs.db"))
        assert result.exit_code == 0
        assert "No observations" in result.output
