"""
Proactive optimisation: SMA (Phase 2) and ARIMA (Phase 4).

Reads the rolling observation store (:mod:`observe`), projects demand a few
minutes ahead, and recommends the replica count needed to serve it.

Two models share one :class:`OptimizeResult`:

* **SMA** (:func:`optimize_sma`) — smooth the window, estimate the short-term
  trend, and extrapolate a point forecast.  Dependency-free and deterministic.
* **ARIMA** (:func:`optimize_arima`) — fit an ``statsmodels`` ARIMA whose order
  is AIC-minimised over a bounded grid (p,q ∈ [0,3], d ∈ [0,2]), forecast with a
  **95% confidence interval**, and report a replica *range* alongside the point
  estimate.  Per the spec it **falls back to SMA when fewer than
  ``MIN_ARIMA_SAMPLES`` (30) observations** are available, flagging that on the
  result via ``fallback_reason``.

``statsmodels`` is imported lazily inside the ARIMA path, so SMA and the rest of
the tool never pay for it.

Method
------
Over the window (oldest→newest):

* ``sma_rps`` / ``sma_latency_ms`` — the smoothed current level (mean).
* **Trend** — split the window in half by count; compare the mean demand of the
  newer half against the older half.  ``trend_pct`` is their relative change;
  ``slope_rps_per_min`` is that change divided by the time between the two
  halves' midpoints.
* **Prediction** — project the recent level forward by ``horizon_minutes`` using
  the slope: ``predicted_rps = newer_mean + slope · horizon`` (clamped ≥ 0).
* **Recommendation** — the minimal replica count that serves ``predicted_rps`` at
  the observed layer and smoothed latency (same primitive as `pat what-if`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from presidio_arch_translucency.hpa import optimal_replicas_for_rps
from presidio_arch_translucency.model import ReplicationLayer
from presidio_arch_translucency.observe import Observation

DEFAULT_WINDOW = 10
DEFAULT_HORIZON_MINUTES = 10.0

# Below this span between the window's two half-midpoints, a per-minute rate
# cannot be inferred reliably (samples are effectively simultaneous), so we do
# not extrapolate a slope — projecting a rate from near-coincident timestamps
# yields absurd predictions.  Real collection (cron/launchd, decision D2) spaces
# samples minutes apart and clears this floor comfortably.
_MIN_TREND_SPAN_MINUTES = 0.5

# ARIMA needs enough history to fit; below this we fall back to SMA (a hard
# requirement from the v0.8.0 spec).
MIN_ARIMA_SAMPLES = 30
# How many recent samples the CLI pulls for an ARIMA run (≈ a few hours at
# minute sampling); more history → better order selection.
ARIMA_DEFAULT_HISTORY = 240
# Bounded order search space (AIC-minimised): p,q ∈ [0,3], d ∈ [0,2].
_ARIMA_P_RANGE = range(0, 4)
_ARIMA_D_RANGE = range(0, 3)
_ARIMA_Q_RANGE = range(0, 4)


class OptimizeError(ValueError):
    """Raised when there is nothing to optimise from."""


@dataclass
class OptimizeResult:
    """Outcome of an optimisation pass (SMA or ARIMA)."""

    layer: str
    samples: int
    window_minutes: float
    sma_rps: float
    sma_latency_ms: float
    trend_pct: float
    slope_rps_per_min: float
    horizon_minutes: float
    predicted_rps: float
    current_replicas: int
    recommended_replicas: int
    first_ts: datetime
    last_ts: datetime
    # Model identity + ARIMA-only fields (None for SMA).
    model: str = "sma"
    predicted_rps_lower: float | None = None  # 95% CI lower
    predicted_rps_upper: float | None = None  # 95% CI upper
    recommended_replicas_lower: int | None = None
    recommended_replicas_upper: int | None = None
    arima_order: tuple[int, int, int] | None = None
    fallback_reason: str | None = None  # set when ARIMA was asked but SMA used

    @property
    def action(self) -> str:
        """``scale-up`` / ``scale-down`` / ``hold`` relative to current replicas."""
        if self.recommended_replicas > self.current_replicas:
            return "scale-up"
        if self.recommended_replicas < self.current_replicas:
            return "scale-down"
        return "hold"

    @property
    def has_interval(self) -> bool:
        """True when a forecast confidence interval is available (ARIMA)."""
        return (
            self.predicted_rps_lower is not None
            and self.predicted_rps_upper is not None
        )


def simple_moving_average(values: Iterable[float]) -> float:
    """Mean of *values* (the simple moving average over the supplied window)."""
    vals = list(values)
    if not vals:
        raise OptimizeError("cannot average an empty series")
    return sum(vals) / len(vals)


@dataclass
class _WindowStats:
    """Shared descriptive statistics over a window of observations."""

    obs: list
    n: int
    rps: list
    t_min: list
    sma_rps: float
    sma_latency: float
    trend_pct: float
    slope: float
    level: float
    layer: str
    current_replicas: int
    first_ts: datetime
    last_ts: datetime


def _window_stats(observations: Sequence[Observation]) -> _WindowStats:
    """Sort, validate, and compute the SMA/trend statistics for a window."""
    obs = sorted(observations, key=lambda o: o.timestamp)
    n = len(obs)
    if n == 0:
        raise OptimizeError("no observations to optimise from")

    rps = [o.rps for o in obs]
    latency = [o.avg_latency_ms for o in obs]
    t0 = obs[0].timestamp
    t_min = [(o.timestamp - t0).total_seconds() / 60.0 for o in obs]

    sma_rps = simple_moving_average(rps)
    sma_latency = simple_moving_average(latency)

    mid = n // 2
    if mid >= 1:
        older_mean = simple_moving_average(rps[:mid])
        newer_mean = simple_moving_average(rps[mid:])
        trend_pct = (
            (newer_mean - older_mean) / older_mean * 100.0 if older_mean > 0 else 0.0
        )
        dt = simple_moving_average(t_min[mid:]) - simple_moving_average(t_min[:mid])
        slope = (newer_mean - older_mean) / dt if dt >= _MIN_TREND_SPAN_MINUTES else 0.0
        level = newer_mean
    else:
        trend_pct = 0.0
        slope = 0.0
        level = sma_rps

    return _WindowStats(
        obs=obs,
        n=n,
        rps=rps,
        t_min=t_min,
        sma_rps=sma_rps,
        sma_latency=sma_latency,
        trend_pct=trend_pct,
        slope=slope,
        level=level,
        layer=obs[-1].layer,
        current_replicas=obs[-1].replicas,
        first_ts=obs[0].timestamp,
        last_ts=obs[-1].timestamp,
    )


def _recommend(rps: float, latency: float, layer: str, current_replicas: int) -> int:
    """Minimal replicas to serve *rps*, or current count for an unmodelled layer."""
    try:
        return optimal_replicas_for_rps(max(0.0, rps), latency, ReplicationLayer(layer))
    except (ValueError, KeyError):
        return current_replicas


def optimize_sma(
    observations: Sequence[Observation],
    horizon_minutes: float = DEFAULT_HORIZON_MINUTES,
) -> OptimizeResult:
    """
    Smooth *observations*, project demand ``horizon_minutes`` ahead, and
    recommend a replica count.  Observations are sorted by timestamp internally;
    pass the window you want smoothed (e.g. the most recent N).
    """
    s = _window_stats(observations)
    predicted_rps = max(0.0, s.level + s.slope * horizon_minutes)
    recommended = _recommend(predicted_rps, s.sma_latency, s.layer, s.current_replicas)
    return OptimizeResult(
        layer=s.layer,
        samples=s.n,
        window_minutes=s.t_min[-1],
        sma_rps=s.sma_rps,
        sma_latency_ms=s.sma_latency,
        trend_pct=s.trend_pct,
        slope_rps_per_min=s.slope,
        horizon_minutes=horizon_minutes,
        predicted_rps=predicted_rps,
        current_replicas=s.current_replicas,
        recommended_replicas=recommended,
        first_ts=s.first_ts,
        last_ts=s.last_ts,
        model="sma",
    )


# ---------------------------------------------------------------------------
# ARIMA
# ---------------------------------------------------------------------------


def _fit_best_arima(series: list[float]):
    """
    Grid-search ARIMA orders and return ``(fitted_result, order)`` with the
    lowest AIC, or ``None`` if no order converges.  statsmodels is imported
    lazily so the rest of the tool never pays for it.
    """
    import math  # noqa: PLC0415
    import warnings  # noqa: PLC0415

    from statsmodels.tsa.arima.model import ARIMA  # noqa: PLC0415

    best = None
    best_aic = None
    best_order = None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # convergence / freq noise
        for p in _ARIMA_P_RANGE:
            for d in _ARIMA_D_RANGE:
                for q in _ARIMA_Q_RANGE:
                    try:
                        fitted = ARIMA(series, order=(p, d, q)).fit()
                    except Exception:  # noqa: BLE001, S112 — unstable orders skipped
                        continue
                    aic = fitted.aic
                    if aic is None or math.isnan(aic):  # skip NaN AIC
                        continue
                    if best_aic is None or aic < best_aic:
                        best, best_aic, best_order = fitted, aic, (p, d, q)
    if best is None:
        return None
    return best, best_order


def _horizon_steps(t_min: list[float], horizon_minutes: float) -> int:
    """Forecast steps = horizon / median sampling interval (≥ 1)."""
    import statistics  # noqa: PLC0415

    diffs = [b - a for a, b in zip(t_min, t_min[1:]) if b - a > 0]
    interval = statistics.median(diffs) if diffs else 0.0
    if interval <= 0:
        return 1
    return max(1, min(10_000, round(horizon_minutes / interval)))


def _arima_forecast(fitted, steps: int) -> tuple[float, float, float]:
    """Return (point, ci_lower, ci_upper) at the final forecast step (95% CI)."""
    import warnings  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        forecast = fitted.get_forecast(steps=steps)
        mean = np.asarray(forecast.predicted_mean, dtype=float)
        ci = np.asarray(forecast.conf_int(alpha=0.05), dtype=float)
    return float(mean[-1]), float(ci[-1, 0]), float(ci[-1, 1])


def optimize_arima(
    observations: Sequence[Observation],
    horizon_minutes: float = DEFAULT_HORIZON_MINUTES,
    min_samples: int = MIN_ARIMA_SAMPLES,
) -> OptimizeResult:
    """
    ARIMA forecast of demand with a 95% confidence interval and a replica range.

    Falls back to SMA (over the most recent ``DEFAULT_WINDOW`` samples) when
    there are fewer than ``min_samples`` observations, or when no ARIMA order
    converges; the returned result carries ``fallback_reason`` in that case.
    """
    s = _window_stats(observations)

    if s.n < min_samples:
        result = optimize_sma(s.obs[-DEFAULT_WINDOW:], horizon_minutes=horizon_minutes)
        result.fallback_reason = (
            f"only {s.n} observation(s) (< {min_samples}); ARIMA needs more "
            "history — used SMA instead."
        )
        return result

    fit = _fit_best_arima(s.rps)
    if fit is None:
        result = optimize_sma(s.obs[-DEFAULT_WINDOW:], horizon_minutes=horizon_minutes)
        result.fallback_reason = (
            "no ARIMA order converged for this series — used SMA instead."
        )
        return result

    fitted, order = fit
    steps = _horizon_steps(s.t_min, horizon_minutes)
    point, lower, upper = _arima_forecast(fitted, steps)
    point, lower, upper = max(0.0, point), max(0.0, lower), max(0.0, upper)

    recommended = _recommend(point, s.sma_latency, s.layer, s.current_replicas)
    rec_lo = _recommend(lower, s.sma_latency, s.layer, s.current_replicas)
    rec_hi = _recommend(upper, s.sma_latency, s.layer, s.current_replicas)

    return OptimizeResult(
        layer=s.layer,
        samples=s.n,
        window_minutes=s.t_min[-1],
        sma_rps=s.sma_rps,
        sma_latency_ms=s.sma_latency,
        trend_pct=s.trend_pct,
        slope_rps_per_min=s.slope,
        horizon_minutes=horizon_minutes,
        predicted_rps=point,
        current_replicas=s.current_replicas,
        recommended_replicas=recommended,
        first_ts=s.first_ts,
        last_ts=s.last_ts,
        model="arima",
        predicted_rps_lower=lower,
        predicted_rps_upper=upper,
        recommended_replicas_lower=min(rec_lo, rec_hi),
        recommended_replicas_upper=max(rec_lo, rec_hi),
        arima_order=order,
    )
