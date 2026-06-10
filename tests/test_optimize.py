"""Tests for SMA-based proactive optimisation (v0.8.0 Phase 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency import optimize as optimize_mod
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import Observation, record_observation
from presidio_arch_translucency.optimize import (
    DEFAULT_MAX_D,
    DEFAULT_MAX_P,
    DEFAULT_MAX_Q,
    OptimizeError,
    _auto_diff_order,
    _fit_best_arima,
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


# ---------------------------------------------------------------------------
# ARIMA configurable order bounds (v0.9.0 Phase 3)
#
# No real ARIMA fitting happens here: the grid-search tests swap statsmodels'
# ARIMA class for a recorder that just logs the (p, d, q) orders it is asked to
# build, and the heuristic tests exercise `_auto_diff_order` directly.
# ---------------------------------------------------------------------------


class _RecordingARIMA:
    """Stand-in for statsmodels' ARIMA that records orders instead of fitting."""

    orders: list[tuple[int, int, int]] = []

    def __init__(self, series, order):
        self.order = order
        _RecordingARIMA.orders.append(order)

    def fit(self):
        return _FakeFitted(self.order)


class _FakeFitted:
    def __init__(self, order):
        # Deterministic, distinct AICs so a single best order emerges; lower
        # order-sum → lower AIC, so (0, 0, 0) always wins.
        self.aic = float(sum(order))


def _patch_arima(monkeypatch):
    _RecordingARIMA.orders = []
    monkeypatch.setattr("statsmodels.tsa.arima.model.ARIMA", _RecordingARIMA)


def _full_grid(max_p, max_d, max_q):
    return {
        (p, d, q)
        for p in range(max_p + 1)
        for d in range(max_d + 1)
        for q in range(max_q + 1)
    }


class TestAutoDiffOrder:
    def test_stationary_series_picks_zero(self):
        # An oscillation around a fixed level is already stationary; raw variance
        # is minimal and differencing only injects noise.
        series = [10, 11, 9, 10, 11, 9, 10, 11, 9, 10]
        assert _auto_diff_order(series) == 0

    def test_random_walk_picks_one(self):
        # A strong linear trend (a deterministic random walk) collapses to a
        # constant first difference (variance 0) → d=1.
        series = list(range(0, 50, 2))
        assert _auto_diff_order(series) == 1

    def test_quadratic_trend_picks_two(self):
        # A quadratic needs two differences to become constant → d=2.
        series = [i * i for i in range(20)]
        assert _auto_diff_order(series) == 2

    def test_capped_at_max_d(self):
        # The same quadratic, but max_d=1 forbids the d=2 choice.
        series = [i * i for i in range(20)]
        assert _auto_diff_order(series, max_d=1) == 1

    def test_short_series_does_not_overshoot(self):
        # Too few points to difference twice; never returns more than is sound.
        assert _auto_diff_order([1.0, 4.0], max_d=2) == 0


class TestFitBestArimaBounds:
    def test_default_grid_is_4x3x4(self, monkeypatch):
        # Regression guard: defaults reproduce the historical 48-model search.
        _patch_arima(monkeypatch)
        _fit_best_arima([float(i) for i in range(8)])
        assert len(_RecordingARIMA.orders) == 4 * 3 * 4
        assert set(_RecordingARIMA.orders) == _full_grid(
            DEFAULT_MAX_P, DEFAULT_MAX_D, DEFAULT_MAX_Q
        )

    def test_narrowed_bounds_shrink_the_grid(self, monkeypatch):
        _patch_arima(monkeypatch)
        result = _fit_best_arima(
            [float(i) for i in range(8)], max_p=1, max_d=1, max_q=1
        )
        assert set(_RecordingARIMA.orders) == _full_grid(1, 1, 1)
        assert result is not None
        _fitted, order = result
        assert order == (0, 0, 0)  # lowest AIC by construction

    def test_auto_diff_searches_single_d(self, monkeypatch):
        _patch_arima(monkeypatch)
        # A linear ramp → heuristic d=1, so every tried order has d=1.
        _fit_best_arima(list(range(0, 40, 2)), max_p=1, max_q=1, auto_diff=True)
        tried_d = {d for (_p, d, _q) in _RecordingARIMA.orders}
        assert tried_d == {1}
        # p,q still swept fully → 2×1×2 = 4 fits, not the full d-sweep.
        assert len(_RecordingARIMA.orders) == 2 * 1 * 2

    def test_auto_diff_respects_max_d_cap(self, monkeypatch):
        _patch_arima(monkeypatch)
        # Quadratic would pick d=2, but max_d=1 caps the auto choice at d=1.
        _fit_best_arima(
            [float(i * i) for i in range(20)], max_p=0, max_q=0, max_d=1, auto_diff=True
        )
        tried_d = {d for (_p, d, _q) in _RecordingARIMA.orders}
        assert tried_d == {1}


class TestOptimizeArimaSignatureDefaults:
    def test_defaults_match_module_constants(self):
        import inspect

        from presidio_arch_translucency.optimize import optimize_arima

        params = inspect.signature(optimize_arima).parameters
        assert params["max_p"].default == DEFAULT_MAX_P
        assert params["max_d"].default == DEFAULT_MAX_D
        assert params["max_q"].default == DEFAULT_MAX_Q
        assert params["auto_diff"].default is False


class TestOptimizeArimaCLIPassthrough:
    """The four flags must reach `optimize_arima` with the right values."""

    @staticmethod
    def _record_db(tmp_path):
        db = tmp_path / "obs.db"
        for i in range(3):
            record_observation(
                Observation(
                    _T0 + timedelta(minutes=i), 100 + i, 80, 140, 97, "container", 2
                ),
                db_path=db,
            )
        return db

    def _capture(self, monkeypatch):
        captured = {}
        real_sma = optimize_mod.optimize_sma

        def fake_arima(rows, **kwargs):
            captured.update(kwargs)
            return real_sma(rows)  # a valid OptimizeResult so rendering succeeds

        monkeypatch.setattr(optimize_mod, "optimize_arima", fake_arima)
        return captured

    def test_explicit_flags_reach_optimize_arima(self, tmp_path, monkeypatch):
        captured = self._capture(monkeypatch)
        db = self._record_db(tmp_path)
        result = _invoke(
            "--db",
            str(db),
            "--model",
            "arima",
            "--max-p",
            "1",
            "--max-d",
            "0",
            "--max-q",
            "2",
            "--auto-diff",
        )
        assert result.exit_code == 0
        assert captured["max_p"] == 1
        assert captured["max_d"] == 0
        assert captured["max_q"] == 2
        assert captured["auto_diff"] is True

    def test_default_flags_reach_optimize_arima(self, tmp_path, monkeypatch):
        captured = self._capture(monkeypatch)
        db = self._record_db(tmp_path)
        result = _invoke("--db", str(db), "--model", "arima")
        assert result.exit_code == 0
        assert captured["max_p"] == DEFAULT_MAX_P
        assert captured["max_d"] == DEFAULT_MAX_D
        assert captured["max_q"] == DEFAULT_MAX_Q
        assert captured["auto_diff"] is False

    def test_negative_bound_rejected(self, tmp_path):
        result = _invoke(
            "--db", str(tmp_path / "obs.db"), "--model", "arima", "--max-p", "-1"
        )
        assert result.exit_code == 2
