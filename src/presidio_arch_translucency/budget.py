"""
Energy/carbon budgeting solver (v0.22.0) — the SEANERGYS dual.

``pat budget`` answers the SEANERGYS objective function in both directions:

  * **Direction 1 — max output within a budget.** Given an energy budget (Wh
    over a window, or a carbon budget converted to Wh via grid intensity), find
    the replication factor δ at each layer that maximises throughput while
    keeping modelled energy ``E(δ) ≤ budget``. "Compute the most you can within
    the watt budget."

  * **Direction 2 — minimum energy for the demand.** Find, per layer, the
    smallest δ that saturates demand (exactly the ``analyze`` sweep), then
    report the modelled energy it costs. Recommend the layer that meets demand
    for the fewest watt-hours. "Less energy for the same output."

The solver lives here (rather than in ``energy.py``, which is kept focused on
the core equations) so the budget optimisation and its carbon overlay have a
home without pushing ``energy.py`` past ~500 lines — an approved module split.

E1a hygiene: every figure produced here is a **modelled estimate** built from
the analytic energy model and a documented/cited grid-intensity figure. Nothing
here is measured, chained, or signed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from presidio_arch_translucency.energy import (
    joules_per_request as _joules_per_request,
)
from presidio_arch_translucency.energy import (
    layer_energy,
    power_watts,
    resolve_energy_params,
)
from presidio_arch_translucency.model import (
    ALL_REPLICATION_LAYERS,
    LAYER_PARAMS,
    ReplicationLayer,
    analyze,
    base_capacity_rps,
    resolve_concurrency,
    throughput,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLayerResult:
    """One layer's budget outcome (Direction 1 or Direction 2)."""

    layer: ReplicationLayer
    replicas: int
    throughput_rps: float
    energy_wh: float  # modelled energy over the window (Wh)
    joules_per_request: Optional[float]  # noqa: UP045
    eei: Optional[float]  # noqa: UP045 — Direction 2 only (None for Direction 1)
    headroom_wh: Optional[float]  # noqa: UP045 — Direction 1 only (budget − used)
    feasible: bool  # Direction 1: even δ=1 fits the budget
    source: str  # "calibrated" | "default"
    grams_per_request: Optional[float]  # noqa: UP045 — set only with --region
    grams_per_window: Optional[float]  # noqa: UP045 — set only with --region


@dataclass(frozen=True)
class BudgetReport:
    """Full ``pat budget`` result for one workload."""

    direction: str  # "max-output" | "min-energy"
    window_h: float
    layers: list[BudgetLayerResult]
    recommended: Optional[ReplicationLayer]  # noqa: UP045 — None if all infeasible
    budget_wh: Optional[float]  # noqa: UP045 — Direction 1 only
    carbon_budget_g: Optional[float]  # noqa: UP045 — carbon-budget mode only
    intensity_g_per_kwh: Optional[float]  # noqa: UP045 — set with --region / carbon
    intensity_source: Optional[str]  # noqa: UP045 — "live"|"cache"|"static"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _energy_terms(
    layer: ReplicationLayer,
    replica_power_watts: float,
    base_cap: float,
    model_layer: Optional[str],  # noqa: UP045
) -> tuple[float, float, float, str]:
    """Resolve ``(idle_w, dyn_j_per_req, β_E, source)`` with a dyn fallback.

    ``resolve_energy_params`` returns ``dyn=None`` only for non-positive
    capacity; the budget sweep needs a concrete number, so a ``None`` dyn is
    treated as zero dynamic cost (idle-only), which cannot happen for a valid
    ``rps > 0`` workload but keeps the solver total.
    """
    idle_w, dyn, beta, source = resolve_energy_params(
        layer, replica_power_watts, base_cap, model_layer
    )
    return idle_w, (dyn if dyn is not None else 0.0), beta, source


def _carbon_cols(
    watts: float,
    j_per_req: Optional[float],  # noqa: UP045
    window_h: float,
    intensity_g_per_kwh: Optional[float],  # noqa: UP045
) -> tuple[Optional[float], Optional[float]]:  # noqa: UP045
    """(gCO₂/req, gCO₂/window) for a layer, or (None, None) with no intensity."""
    if intensity_g_per_kwh is None:
        return None, None
    from presidio_arch_translucency.carbon import grams_per_hour, grams_per_request

    g_req = (
        grams_per_request(j_per_req, intensity_g_per_kwh)
        if j_per_req is not None
        else None
    )
    g_window = grams_per_hour(watts, intensity_g_per_kwh) * window_h
    return g_req, g_window


# ---------------------------------------------------------------------------
# Direction 2 — minimum energy meeting demand ("less energy, same output")
# ---------------------------------------------------------------------------


def solve_min_energy(
    requests_per_second: float,
    avg_latency_ms: float,
    replica_power_watts: float,
    model_layer: Optional[str] = None,  # noqa: UP045
    window_h: float = 1.0,
    intensity_g_per_kwh: Optional[float] = None,  # noqa: UP045
    intensity_source: Optional[str] = None,  # noqa: UP045
) -> BudgetReport:
    """Least-energy layer meeting demand, reusing the ``analyze`` sweep exactly.

    For each layer the smallest δ that saturates demand comes straight from
    :func:`model.analyze` (same base-capacity resolution, same optimal-δ
    selection), so the recommendation is consistent with ``pat analyze``. Energy
    is the modelled ``E(δ)`` at that δ over *window_h*; the recommended layer is
    the one meeting demand for the fewest watt-hours (tie → fewest replicas).
    """
    concurrency = resolve_concurrency(model_layer)
    base_cap = base_capacity_rps(requests_per_second, avg_latency_ms, concurrency)
    analysis = analyze(
        requests_per_second=requests_per_second,
        avg_latency_ms=avg_latency_ms,
        current_layer=ALL_REPLICATION_LAYERS[0],
        layer=model_layer,
    )

    results: list[BudgetLayerResult] = []
    for lr in analysis.layers:
        achieved_rps = throughput(
            requests_per_second,
            lr.optimal_replicas,
            lr.layer,
            base_cap,
        )
        feasible = math.isclose(
            achieved_rps,
            requests_per_second,
            rel_tol=1e-9,
            abs_tol=1e-6,
        )
        le = layer_energy(
            lr.layer,
            lr.optimal_replicas,
            requests_per_second,
            base_cap,
            replica_power_watts,
            model_layer,
        )
        energy_wh = le.watts * window_h
        g_req, g_window = _carbon_cols(
            le.watts, le.joules_per_request, window_h, intensity_g_per_kwh
        )
        results.append(
            BudgetLayerResult(
                layer=lr.layer,
                replicas=lr.optimal_replicas,
                throughput_rps=round(achieved_rps, 2),
                energy_wh=energy_wh,
                joules_per_request=le.joules_per_request,
                eei=le.eei,
                headroom_wh=None,
                feasible=feasible,
                source=le.source,
                grams_per_request=g_req,
                grams_per_window=g_window,
            )
        )

    # Recommend lowest energy only among layers that actually satisfy demand.
    # A max-replica ceiling can leave a layer far short of the requested output;
    # that is an infeasible constraint result, not a cheap solution.
    feasible_results = [result for result in results if result.feasible]
    recommended = (
        min(feasible_results, key=lambda r: (r.energy_wh, r.replicas)).layer
        if feasible_results
        else None
    )
    return BudgetReport(
        direction="min-energy",
        window_h=window_h,
        layers=results,
        recommended=recommended,
        budget_wh=None,
        carbon_budget_g=None,
        intensity_g_per_kwh=intensity_g_per_kwh,
        intensity_source=intensity_source,
    )


# ---------------------------------------------------------------------------
# Direction 1 — max output within an energy (or carbon) budget
# ---------------------------------------------------------------------------


def solve_energy_budget(
    requests_per_second: float,
    avg_latency_ms: float,
    replica_power_watts: float,
    budget_wh: float,
    window_h: float,
    model_layer: Optional[str] = None,  # noqa: UP045
    intensity_g_per_kwh: Optional[float] = None,  # noqa: UP045
    intensity_source: Optional[str] = None,  # noqa: UP045
    carbon_budget_g: Optional[float] = None,  # noqa: UP045
) -> BudgetReport:
    """Max-throughput δ per layer subject to ``E(δ) ≤ budget_wh`` over *window_h*.

    Sweeps δ = 1..max_replicas at each layer, keeps the feasible δ (energy over
    the window within budget) with the highest throughput — tie → fewest
    replicas. A layer whose δ=1 already exceeds the budget is reported
    ``feasible=False`` and excluded from the recommendation (the v0.18
    memory-hard-constraint precedent). The recommended layer maximises achieved
    throughput; ties break to fewest replicas, then lowest Wh.
    """
    concurrency = resolve_concurrency(model_layer)
    base_cap = base_capacity_rps(requests_per_second, avg_latency_ms, concurrency)

    results: list[BudgetLayerResult] = []
    for layer in ALL_REPLICATION_LAYERS:
        idle_w, dyn, beta, source = _energy_terms(
            layer, replica_power_watts, base_cap, model_layer
        )
        params = LAYER_PARAMS[layer]

        best: Optional[tuple[int, float, float]] = None  # noqa: UP045
        for delta in range(1, params.max_replicas + 1):
            omega = throughput(requests_per_second, delta, layer, base_cap)
            watts = power_watts(delta, omega, idle_w, dyn, beta)
            energy_wh = watts * window_h
            if energy_wh <= budget_wh:
                # Prefer higher throughput; tie → fewer replicas (first wins).
                if best is None or omega > best[1] + 1e-9:
                    best = (delta, omega, watts)

        if best is None:
            # Even a single replica exceeds the budget: infeasible layer.
            omega1 = throughput(requests_per_second, 1, layer, base_cap)
            watts1 = power_watts(1, omega1, idle_w, dyn, beta)
            energy1 = watts1 * window_h
            jreq1 = _joules_per_request(watts1, omega1)
            g_req, g_window = _carbon_cols(watts1, jreq1, window_h, intensity_g_per_kwh)
            results.append(
                BudgetLayerResult(
                    layer=layer,
                    replicas=1,
                    throughput_rps=round(omega1, 2),
                    energy_wh=energy1,
                    joules_per_request=jreq1,
                    eei=None,
                    headroom_wh=budget_wh - energy1,
                    feasible=False,
                    source=source,
                    grams_per_request=g_req,
                    grams_per_window=g_window,
                )
            )
            continue

        delta, omega, watts = best
        energy_wh = watts * window_h
        jreq = _joules_per_request(watts, omega)
        g_req, g_window = _carbon_cols(watts, jreq, window_h, intensity_g_per_kwh)
        results.append(
            BudgetLayerResult(
                layer=layer,
                replicas=delta,
                throughput_rps=round(omega, 2),
                energy_wh=energy_wh,
                joules_per_request=jreq,
                eei=None,
                headroom_wh=budget_wh - energy_wh,
                feasible=True,
                source=source,
                grams_per_request=g_req,
                grams_per_window=g_window,
            )
        )

    feasible = [r for r in results if r.feasible]
    recommended: Optional[ReplicationLayer] = None  # noqa: UP045
    if feasible:
        # Highest throughput; tie → fewest replicas, then lowest Wh.
        recommended = max(
            feasible,
            key=lambda r: (r.throughput_rps, -r.replicas, -r.energy_wh),
        ).layer

    return BudgetReport(
        direction="max-output",
        window_h=window_h,
        layers=results,
        recommended=recommended,
        budget_wh=budget_wh,
        carbon_budget_g=carbon_budget_g,
        intensity_g_per_kwh=intensity_g_per_kwh,
        intensity_source=intensity_source,
    )
