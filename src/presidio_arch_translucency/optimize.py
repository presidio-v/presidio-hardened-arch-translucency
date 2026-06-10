"""
SMA-based proactive optimisation (v0.8.0, Phase 2).

Reads the rolling observation store (:mod:`observe`), smooths the recent demand
with a simple moving average (SMA), estimates the short-term trend, projects
demand a few minutes ahead, and recommends the replica count needed to serve
that predicted demand.

This is the SMA foundation called for by decision **D1** — ARIMA (a later phase)
plugs in as an alternative model and falls back to this SMA path when there are
too few samples.  Pure and deterministic: it takes a list of observations and
returns a result, with no I/O of its own.

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


class OptimizeError(ValueError):
    """Raised when there is nothing to optimise from."""


@dataclass
class OptimizeResult:
    """Outcome of an SMA optimisation pass."""

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

    @property
    def action(self) -> str:
        """``scale-up`` / ``scale-down`` / ``hold`` relative to current replicas."""
        if self.recommended_replicas > self.current_replicas:
            return "scale-up"
        if self.recommended_replicas < self.current_replicas:
            return "scale-down"
        return "hold"


def simple_moving_average(values: Iterable[float]) -> float:
    """Mean of *values* (the simple moving average over the supplied window)."""
    vals = list(values)
    if not vals:
        raise OptimizeError("cannot average an empty series")
    return sum(vals) / len(vals)


def optimize_sma(
    observations: Sequence[Observation],
    horizon_minutes: float = DEFAULT_HORIZON_MINUTES,
) -> OptimizeResult:
    """
    Smooth *observations*, project demand ``horizon_minutes`` ahead, and
    recommend a replica count.  Observations are sorted by timestamp internally;
    pass the window you want smoothed (e.g. the most recent N).
    """
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
        # Only infer a per-minute rate when the window spans enough time;
        # otherwise report the trend but do not extrapolate (slope 0).
        slope = (newer_mean - older_mean) / dt if dt >= _MIN_TREND_SPAN_MINUTES else 0.0
        level = newer_mean
    else:
        # A single sample: no trend can be inferred.
        trend_pct = 0.0
        slope = 0.0
        level = sma_rps

    predicted_rps = max(0.0, level + slope * horizon_minutes)

    layer = obs[-1].layer
    current_replicas = obs[-1].replicas
    try:
        recommended = optimal_replicas_for_rps(
            predicted_rps, sma_latency, ReplicationLayer(layer)
        )
    except (ValueError, KeyError):
        # Layer outside the modelled replication layers — cannot size it.
        recommended = current_replicas

    return OptimizeResult(
        layer=layer,
        samples=n,
        window_minutes=t_min[-1],
        sma_rps=sma_rps,
        sma_latency_ms=sma_latency,
        trend_pct=trend_pct,
        slope_rps_per_min=slope,
        horizon_minutes=horizon_minutes,
        predicted_rps=predicted_rps,
        current_replicas=current_replicas,
        recommended_replicas=recommended,
        first_ts=obs[0].timestamp,
        last_ts=obs[-1].timestamp,
    )
