"""
HPA lag model — temporal throughput and latency during a Kubernetes scale event.

Models the performance trough that occurs between a load spike and the moment
new pods become Ready, capturing:
  - HPA scrape interval   (default 15 s — Kubernetes upstream default)
  - Pod startup + readiness probe time (default 30 s)
  - Optional cold-start warmup  (JVM, cache hydration, etc.)

The trough metrics are derived from the architectural-translucency steady-state
model applied to the *pre-scale* replica count under *post-spike* demand —
the worst case the system faces before HPA relief arrives.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from presidio_arch_translucency.model import (
    LAYER_PARAMS,
    ReplicationLayer,
    base_capacity_rps,
    response_time_ms,
    throughput,
)

# ── HPA / pod timing defaults ─────────────────────────────────────────────────

DEFAULT_HPA_POLL_S: float = 15.0
DEFAULT_POD_STARTUP_S: float = 30.0
DEFAULT_COLD_START_S: float = 0.0


# ── data model ────────────────────────────────────────────────────────────────


@dataclass
class ScaleEventParams:
    """Timing parameters for a Kubernetes HPA scale event."""

    hpa_poll_s: float = DEFAULT_HPA_POLL_S
    pod_startup_s: float = DEFAULT_POD_STARTUP_S
    cold_start_s: float = DEFAULT_COLD_START_S

    @property
    def time_to_ready_s(self) -> float:
        """Seconds from spike to new pods reaching Ready state."""
        return self.hpa_poll_s + self.pod_startup_s + self.cold_start_s


@dataclass
class TimePoint:
    """System state snapshot at one point during a scale event."""

    t_s: float
    replicas: int
    throughput_rps: float
    avg_latency_ms: float
    p99_latency_ms: float
    demand_rps: float
    overloaded: bool


@dataclass
class ScaleEventResult:
    """Full result of an HPA scale-event simulation."""

    layer: ReplicationLayer
    replicas_before: int
    replicas_after: int
    rps_baseline: float
    rps_spike: float
    params: ScaleEventParams

    # Trough window (δ_before replicas under rps_spike demand)
    trough_duration_s: float
    trough_throughput_rps: float
    trough_throughput_pct: float  # % of spike demand actually served
    trough_avg_latency_ms: float
    trough_p99_latency_ms: float
    missed_requests: int  # requests dropped/queued during the trough

    # Steady-state once δ_after replicas are Ready
    steady_throughput_rps: float
    steady_avg_latency_ms: float
    steady_p99_latency_ms: float

    # Sampled timeline
    timeline: list[TimePoint] = field(default_factory=list)


# ── internal helpers ──────────────────────────────────────────────────────────


def _utilization(rps: float, replicas: int, avg_latency_ms: float) -> float:
    """ρ = λ / (c × μ).  μ = 1000 / avg_latency_ms (Little's Law)."""
    if replicas <= 0 or avg_latency_ms <= 0:
        return 1.0
    service_rate = 1000.0 / avg_latency_ms
    return rps / (replicas * service_rate)


def _p99_multiplier(utilization: float) -> float:
    """
    p99 / avg-latency ratio — M/M/c queue-theory approximation.
    Higher utilization produces a heavier tail.
    """
    if utilization >= 1.0:
        return 15.0
    if utilization >= 0.9:
        return 8.0
    if utilization >= 0.7:
        return 4.0
    if utilization >= 0.5:
        return 2.5
    return 1.8


# ── public helpers ────────────────────────────────────────────────────────────


def optimal_replicas_for_rps(
    rps: float,
    avg_latency_ms: float,
    layer: ReplicationLayer,
) -> int:
    """
    Minimum replica count at *layer* that achieves ≥ 98 % of *rps* throughput.
    Capped at the layer's max_replicas ceiling.
    """
    base_cap = base_capacity_rps(rps, avg_latency_ms)
    params = LAYER_PARAMS[layer]
    for delta in range(1, params.max_replicas + 1):
        if throughput(rps, delta, layer, base_cap) >= rps * 0.98:
            return delta
    return params.max_replicas


# ── timeline builder ──────────────────────────────────────────────────────────


def _build_timeline(
    rps_spike: float,
    avg_latency_ms: float,
    layer: ReplicationLayer,
    replicas_before: int,
    replicas_after: int,
    params: ScaleEventParams,
) -> list[TimePoint]:
    base_cap = base_capacity_rps(rps_spike, avg_latency_ms)
    ttr = params.time_to_ready_s
    raw_times = [
        0.0,
        params.hpa_poll_s,
        params.hpa_poll_s + params.pod_startup_s * 0.5,
        ttr - 0.1,
        ttr,
        ttr + 10.0,
        ttr + 30.0,
        ttr + 60.0,
    ]
    points: list[TimePoint] = []
    for t in sorted(set(max(0.0, round(v, 1)) for v in raw_times)):
        in_trough = t < ttr
        reps = replicas_before if in_trough else replicas_after
        tp = throughput(rps_spike, reps, layer, base_cap)
        rt = response_time_ms(rps_spike, reps, layer, avg_latency_ms, base_cap)
        util = _utilization(rps_spike, reps, avg_latency_ms)
        p99 = rt * _p99_multiplier(util)
        points.append(
            TimePoint(
                t_s=t,
                replicas=reps,
                throughput_rps=round(tp, 1),
                avg_latency_ms=round(rt, 1),
                p99_latency_ms=round(p99, 1),
                demand_rps=rps_spike,
                overloaded=util >= 1.0,
            )
        )
    return points


# ── visualization ────────────────────────────────────────────────────────────


def save_hpa_plot(result: ScaleEventResult, output: object) -> None:
    """
    Save a time-series HPA lag plot to *output*.

    Three panels (stacked):
      1. Throughput (req/s) — actual vs demand
      2. Avg latency (ms)
      3. p99 latency (ms)

    The trough window is shaded red; the steady-state zone is shaded green.
    Vertical annotations mark the HPA poll boundary and the pod-ready event.
    """
    import textwrap  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    from pathlib import Path  # noqa: PLC0415

    import matplotlib.patches as mpatches  # noqa: PLC0415
    import matplotlib.pyplot as plt  # noqa: PLC0415

    output_path = Path(str(output))

    tl = result.timeline
    times = [p.t_s for p in tl]
    tps = [p.throughput_rps for p in tl]
    avgs = [p.avg_latency_ms for p in tl]
    p99s = [p.p99_latency_ms for p in tl]
    ttr = result.trough_duration_s
    poll = result.params.hpa_poll_s

    fig, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    title = (
        f"HPA Scale Event — {result.layer.value} layer  "
        f"({result.replicas_before} → {result.replicas_after} replicas)\n"
        f"Load: {result.rps_baseline:.0f} → {result.rps_spike:.0f} req/s  "
        f"({result.rps_spike / max(result.rps_baseline, 1):.1f}×)  |  "
        f"Trough window: {ttr:.0f} s"
    )
    fig.suptitle(title, fontsize=11, fontweight="bold")

    x_max = max(times) if times else ttr + 60

    for ax in axes:
        # Trough shading
        ax.axvspan(0, ttr, color="#ffcccc", alpha=0.45, label="Trough")
        # Steady-state shading
        ax.axvspan(ttr, x_max, color="#ccffcc", alpha=0.30, label="Steady state")
        # HPA poll boundary
        ax.axvline(poll, color="#cc4444", linewidth=1.2, linestyle="--", alpha=0.8)
        # Pods-ready boundary
        ax.axvline(ttr, color="#228822", linewidth=1.5, linestyle="-", alpha=0.9)
        ax.spines[["top", "right"]].set_visible(False)

    # Panel 0 — throughput
    axes[0].plot(times, tps, color="#2255aa", linewidth=2.2, marker="o", markersize=4)
    axes[0].axhline(
        result.rps_spike,
        color="#888888",
        linewidth=1,
        linestyle=":",
        label=f"Demand {result.rps_spike:.0f} req/s",
    )
    axes[0].set_ylabel("Throughput\n(req/s)")
    axes[0].set_ylim(bottom=0)
    axes[0].legend(fontsize=7, loc="lower right")

    # Panel 1 — avg latency
    axes[1].plot(times, avgs, color="#dd7722", linewidth=2.2, marker="o", markersize=4)
    axes[1].set_ylabel("Avg Latency\n(ms)")
    axes[1].set_ylim(bottom=0)

    # Panel 2 — p99 latency
    axes[2].plot(times, p99s, color="#882288", linewidth=2.2, marker="o", markersize=4)
    axes[2].set_ylabel("p99 Latency\n(ms)")
    axes[2].set_ylim(bottom=0)
    axes[2].set_xlabel("Time (s)")

    # Annotations on first panel
    axes[0].annotate(
        "HPA detects\noverload",
        xy=(poll, axes[0].get_ylim()[1] * 0.85),
        fontsize=7,
        color="#cc4444",
        ha="center",
    )
    axes[0].annotate(
        "Pods Ready",
        xy=(ttr, axes[0].get_ylim()[1] * 0.85),
        fontsize=7,
        color="#228822",
        ha="center",
    )

    # Trough summary annotation on throughput panel
    summary = textwrap.fill(
        f"Trough: {result.trough_throughput_pct:.0f}% served, "
        f"{result.missed_requests:,} missed reqs",
        width=32,
    )
    axes[0].text(
        ttr * 0.5,
        max(tps) * 0.35 if max(tps) > 0 else 1,
        summary,
        fontsize=7.5,
        color="#880000",
        ha="center",
        va="center",
        bbox={"boxstyle": "round,pad=0.3", "facecolor": "#fff0f0", "alpha": 0.8},
    )

    # Legend patches
    legend_patches = [
        mpatches.Patch(color="#ffcccc", alpha=0.7, label="Trough window"),
        mpatches.Patch(color="#ccffcc", alpha=0.6, label="Steady-state"),
    ]
    fig.legend(
        handles=legend_patches,
        loc="lower center",
        ncol=2,
        fontsize=8,
        framealpha=0.6,
    )

    fig.tight_layout(rect=[0, 0.04, 1, 1])
    fig.savefig(output_path, dpi=130, bbox_inches="tight")
    plt.close(fig)


# ── main simulation ───────────────────────────────────────────────────────────


def simulate_scale_event(
    rps_baseline: float,
    rps_spike: float,
    avg_latency_ms: float,
    layer: ReplicationLayer,
    params: ScaleEventParams | None = None,
    replicas_before: int | None = None,
    replicas_after: int | None = None,
) -> ScaleEventResult:
    """
    Simulate an HPA scale event when load rises from *rps_baseline* to *rps_spike*.

    *replicas_before* / *replicas_after* default to the architectural-
    translucency optimal replica counts for each load level.
    """
    if params is None:
        params = ScaleEventParams()
    if replicas_before is None:
        replicas_before = max(
            1, optimal_replicas_for_rps(rps_baseline, avg_latency_ms, layer)
        )
    if replicas_after is None:
        replicas_after = optimal_replicas_for_rps(rps_spike, avg_latency_ms, layer)

    base_cap = base_capacity_rps(rps_spike, avg_latency_ms)

    # Trough: δ_before replicas under rps_spike demand
    trough_tp = throughput(rps_spike, replicas_before, layer, base_cap)
    trough_rt = response_time_ms(
        rps_spike, replicas_before, layer, avg_latency_ms, base_cap
    )
    trough_util = _utilization(rps_spike, replicas_before, avg_latency_ms)
    trough_p99 = trough_rt * _p99_multiplier(trough_util)
    trough_pct = trough_tp / rps_spike * 100.0 if rps_spike > 0 else 100.0
    missed = int(max(0.0, rps_spike - trough_tp) * params.time_to_ready_s)

    # Steady-state: δ_after replicas under rps_spike demand
    steady_tp = throughput(rps_spike, replicas_after, layer, base_cap)
    steady_rt = response_time_ms(
        rps_spike, replicas_after, layer, avg_latency_ms, base_cap
    )
    steady_util = _utilization(rps_spike, replicas_after, avg_latency_ms)
    steady_p99 = steady_rt * _p99_multiplier(steady_util)

    return ScaleEventResult(
        layer=layer,
        replicas_before=replicas_before,
        replicas_after=replicas_after,
        rps_baseline=rps_baseline,
        rps_spike=rps_spike,
        params=params,
        trough_duration_s=round(params.time_to_ready_s, 1),
        trough_throughput_rps=round(trough_tp, 1),
        trough_throughput_pct=round(trough_pct, 1),
        trough_avg_latency_ms=round(trough_rt, 1),
        trough_p99_latency_ms=round(trough_p99, 1),
        missed_requests=missed,
        steady_throughput_rps=round(steady_tp, 1),
        steady_avg_latency_ms=round(steady_rt, 1),
        steady_p99_latency_ms=round(steady_p99, 1),
        timeline=_build_timeline(
            rps_spike,
            avg_latency_ms,
            layer,
            replicas_before,
            replicas_after,
            params,
        ),
    )
