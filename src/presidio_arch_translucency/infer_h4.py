"""
H4: do the energy-optimal and the cost-optimal layout diverge? (ADR-0012, 1h)

Measured only: pat has no inference energy model, so H4 compares what the
open-loop sweeps measured (1g) with what the price catalog charges (1f), at a
common arrival rate λ* registered before the sweeps. The pre-registered
statement:

    At λ*, the qualifying layout with the least measured energy per output
    token is not among the qualifying layouts with the lowest price per hour
    (within :data:`COST_TIE_PCT`), and its advantage exceeds the tie margin.

``diverge`` passes; ``coincide`` and ``cost_tied`` fail (a cost optimiser
that breaks ties on energy loses nothing); anything else is inconclusive.

A layout qualifies at λ* when its sweep has a non-refine level at exactly λ*
that meets the SLO, with recorded, steady energy, a measured output-token
rate within :data:`THROUGHPUT_TOLERANCE` of λ*·O, and a drift control that
did not flag. Cost compares euros per hour, which at a common λ* is the price
per token over the nominal token rate λ*·O; measured tokens would let counter
noise break exact price ties. Energy per token stays measured.

Bounded claims, all in the result: joules cover GPU boards, prices whole
instances, so layouts on different host counts never yield ``diverge``
(``host_scope``) and the host draw that would flip the verdict is reported;
at a low λ* every layout idles and the fewest GPUs win on both axes, so each
layout reports its utilisation of the measured λ_max and an idle-dominated
flag, and the verdict is repeated at every common level.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Final

from presidio_arch_translucency.infer_calibrate import InferenceCalibrationError
from presidio_arch_translucency.infer_energy import (
    LANDING_MIN_RISE,
    LANDING_MIN_RISE_W,
    POWER_LIMIT_EPSILON_W,
)
from presidio_arch_translucency.infer_prices import gpu_family_key, gpu_model_key
from presidio_arch_translucency.infer_sweep import (
    PASS,
    Slo,
    Sweep,
    find_bracket,
    slo_classifier,
)

#: The energy-optimal layout must beat the runner-up by at least this much,
#: widened by both sweeps' measured drift.
ENERGY_TIE_PCT: Final[float] = 5.0
#: Prices per hour this close count as equal.
COST_TIE_PCT: Final[float] = 0.5
#: Measured output tokens per second must be this close to λ*·O.
THROUGHPUT_TOLERANCE: Final[float] = 0.05
#: Mean GPU temperatures further apart than this (°C) are worth a warning.
TEMPERATURE_SPREAD_C: Final[float] = 10.0
#: Power above idle below this multiple of the landing rise: idle-dominated.
IDLE_DOMINATED_FACTOR: Final[float] = 2.0
_LEVEL_TOLERANCE: Final[float] = 1e-9

STATEMENT: Final[str] = (
    "At λ*, the qualifying layout with the least measured energy per output "
    "token is not among the qualifying layouts with the lowest price per hour, "
    "and its advantage exceeds the tie margin."
)

#: (tp, instances) → (EUR per hour or None, price detail for the report).
PriceFn = Callable[[int, int], "tuple[float | None, dict | None]"]


def _same(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=_LEVEL_TOLERANCE)


def _level_at(sweep: Sweep, lam: float) -> dict | None:
    for level in sweep.levels:
        if not level.get("refine") and _same(float(level["level"]), lam):
            return level
    return None


def common_levels(sweeps: list[Sweep]) -> list[float]:
    """Non-refine levels present in every sweep (the shared ladder)."""
    first = sorted(
        {float(lv["level"]) for lv in sweeps[0].levels if not lv.get("refine")}
    )
    return [lam for lam in first if all(_level_at(s, lam) for s in sweeps)]


def _model_names(sweep: Sweep) -> set[str]:
    labels = (sweep.energy or {}).get("labels") or {}
    return {str(entry.get("model_name")) for entry in labels.values()}


def _check_comparable(sweeps: list[Sweep], gpu_type: str | None) -> list[str]:
    """Refuse sweeps that measured different hardware; return warnings."""
    measured = [s for s in sweeps if s.energy is not None]
    models = set().union(*(_model_names(s) for s in measured)) if measured else set()
    keys = {gpu_model_key(name) for name in models}
    if len(keys) > 1:
        raise InferenceCalibrationError(
            f"the sweeps measured different GPU models ({sorted(models)}); H4 "
            "compares layouts of one GPU"
        )
    # DCGM spells out the variant ("NVIDIA H100 80GB HBM3"), profiles and
    # catalogs name the family; memory and bandwidth pin the variant there.
    families = {gpu_family_key(name) for name in models}
    if gpu_type is not None and families and families != {gpu_family_key(gpu_type)}:
        raise InferenceCalibrationError(
            f"the sweeps measured {sorted(models)}, the calibration is for {gpu_type!r}"
        )
    limits = sorted(
        {
            entry.get("power_limit_w")
            for s in measured
            for entry in ((s.energy or {}).get("idle_before") or {}).values()
        },
        key=lambda v: (v is None, v or 0.0),
    )
    known = [v for v in limits if v is not None]
    if (known and None in limits) or (
        known and max(known) - min(known) > POWER_LIMIT_EPSILON_W
    ):
        raise InferenceCalibrationError(
            f"the sweeps ran at different GPU power limits ({limits} W); the "
            "energy per token is not comparable"
        )
    warnings = []
    drivers = {
        entry.get("driver")
        for s in measured
        for entry in ((s.energy or {}).get("labels") or {}).values()
        if entry.get("driver") is not None
    }
    if len(drivers) > 1:
        warnings.append(f"the sweeps ran on different drivers ({sorted(drivers)})")
    return warnings


def _idle_watts(sweep: Sweep) -> tuple[float | None, float | None]:
    """Total idle watts of the layout and its idle-dominated threshold."""
    idle = (sweep.energy or {}).get("idle_before")
    if not idle:
        return None, None
    total = sum(entry["watts"] for entry in idle.values())
    rise = sum(
        max(LANDING_MIN_RISE_W, LANDING_MIN_RISE * entry["watts"])
        for entry in idle.values()
    )
    return total, IDLE_DOMINATED_FACTOR * rise


def _row(
    sweep: Sweep,
    lam: float,
    slo: Slo,
    price: PriceFn | None,
) -> dict:
    level = _level_at(sweep, lam)
    energy = (level or {}).get("energy")
    drift = (sweep.energy or {}).get("drift_control")
    reasons: list[str] = []
    outcome = None if level is None else slo_classifier(slo)(level)
    if level is None:
        reasons.append(f"no level at λ = {lam:g}")
    elif outcome is None:
        reasons.append("no SLO verdict at λ* (level refused, not saturated)")
    elif outcome != PASS:
        reasons.append("does not meet the SLO at λ*")
    if energy is None:
        reasons.append("no measured energy")
    elif energy["verdict"] != "recorded":
        reasons.append(f"energy refused: {energy.get('reason')}")
    elif not energy["steady"]:
        reasons.append("capacity window refused")
    rate = None
    if energy is not None and energy["verdict"] == "recorded":
        rate = energy["output_tokens"] / energy["window_s"]
        nominal = lam * sweep.output_tokens
        if abs(rate / nominal - 1.0) > THROUGHPUT_TOLERANCE:
            reasons.append(
                f"throughput_miss: {rate:.1f} output tokens/s against "
                f"λ*·O = {nominal:.1f}"
            )
    if isinstance(drift, dict) and drift.get("drifted"):
        reasons.append(f"drift control flagged {drift.get('drift'):+.1%}")
    eur_per_hour, price_detail = (None, None)
    if price is not None:
        eur_per_hour, price_detail = price(sweep.tp, sweep.instances)
        if eur_per_hour is None:
            reasons.append("no price for this layout")
    recorded = energy is not None and energy["verdict"] == "recorded"
    idle, idle_threshold = _idle_watts(sweep)
    watts = energy["watts"] if recorded else None
    over_idle = None if watts is None or idle is None else watts - idle
    temps = [
        gpu["temp_mean_c"]
        for gpu in (energy or {}).get("gpus") or []
        if gpu.get("temp_mean_c") is not None
    ]
    # λ* over the measured SLO capacity: the bracket midpoint, or, when the
    # sweep never failed, the highest passing level, which bounds the ratio
    # from above (the true capacity is higher).
    bracket = find_bracket(sweep.levels, slo_classifier(slo))
    capacity = bracket.estimate if bracket.resolved else bracket.lo
    return {
        "layout": sweep.label,
        "sweep": sweep.path,
        "tp": sweep.tp,
        "instances": sweep.instances,
        "gpus": sweep.gpus,
        "hosts": (sweep.energy or {}).get("hosts"),
        "qualifies": not reasons,
        "reasons": reasons,
        "j_per_output_token": energy["j_per_output_token"] if recorded else None,
        "watts": watts,
        "watts_over_idle": over_idle,
        "idle_dominated": (
            None if over_idle is None else over_idle < (idle_threshold or 0.0)
        ),
        "output_tokens_per_s": rate,
        "joules": energy["joules"] if recorded else None,
        "output_tokens": energy["output_tokens"] if recorded else None,
        "window_s": energy["window_s"] if recorded else None,
        "temp_mean_c": sum(temps) / len(temps) if temps else None,
        "lambda_over_capacity": None if not capacity else lam / capacity,
        "lambda_over_capacity_bound": (
            None if not capacity else ("estimate" if bracket.resolved else "at_most")
        ),
        "drift": drift.get("drift") if isinstance(drift, dict) else None,
        "drift_checked": isinstance(drift, dict) and drift.get("drift") is not None,
        "eur_per_hour": eur_per_hour,
        "eur_per_mtok": (
            None
            if eur_per_hour is None
            else eur_per_hour / (lam * sweep.output_tokens * 3600.0) * 1e6
        ),
        "price": price_detail,
    }


def _host_slope(row: dict) -> float:
    """Joules per output token that one watt of draw per host adds."""
    return row["hosts"] * row["window_s"] / row["output_tokens"]


def _host_watts_to_flip(e: dict, threats: list[dict]) -> float | None:
    """Least host draw per host (W) at which a threat overtakes e on energy.

    Solves J_e + s_e·P = J_r + s_r·P with s = hosts·window/tokens for each
    layout r on fewer hosts than e: arithmetic on measured quantities, not
    an energy model. None when no such layout exists.
    """
    flips = [
        (r["j_per_output_token"] - e["j_per_output_token"])
        / (_host_slope(e) - _host_slope(r))
        for r in threats
        if _host_slope(e) > _host_slope(r)
    ]
    return min(flips) if flips else None


def _verdict(rows: list[dict], priced: bool) -> dict:
    """The H4 outcome over the qualifying layouts at one λ.

    Every other layout within the tie margin of the least-energy layout e is
    a "close" contender: noise could make it the optimum. ``diverge`` needs no
    close contender and e outside the cost set; a fail needs e in the cost set
    and every close contender in it too (the optimum is cheapest either way).
    Host draw, which joules omit, can only help layouts on fewer hosts than
    e; a verdict that such a layout could overturn is ``host_scope``.
    """
    qualifying = [r for r in rows if r["qualifies"]]
    result: dict = {
        "verdict": "inconclusive",
        "h4": None,
        "reason": None,
        "qualifying": len(qualifying),
        "energy_optimal": None,
        "cost_optimal": None,
        "energy_resolved": None,
        "close": None,
        "tie_margin_pct": None,
        "energy_saving": None,
        "cost_premium": None,
        "tie_energy_spread": None,
        "host_watts_to_flip": None,
    }
    if len(qualifying) < 2:
        result["reason"] = "fewer than two qualifying layouts"
        return result
    e = min(qualifying, key=lambda r: r["j_per_output_token"])
    others = [r for r in qualifying if r is not e]

    def margin(r: dict) -> float:
        drift = abs(e["drift"] or 0.0) + abs(r["drift"] or 0.0)
        return max(ENERGY_TIE_PCT, drift * 100.0)

    def gap(r: dict) -> float:
        return (r["j_per_output_token"] / e["j_per_output_token"] - 1.0) * 100.0

    close = [r for r in others if gap(r) <= margin(r)]
    result.update(
        energy_optimal=e["layout"],
        energy_resolved=not close,
        close=[r["layout"] for r in close],
        tie_margin_pct=max(margin(r) for r in others),
    )
    if not priced:
        result["reason"] = "no_prices"
        return result
    cheapest = min(r["eur_per_hour"] for r in qualifying)
    cost_set = [
        r
        for r in qualifying
        if (r["eur_per_hour"] / cheapest - 1.0) * 100.0 <= COST_TIE_PCT
    ]
    # The conservative comparison: the cheapest layout with the least energy.
    c = min(cost_set, key=lambda r: r["j_per_output_token"])
    result.update(
        cost_optimal=[r["layout"] for r in cost_set],
        energy_saving=1.0 - e["j_per_output_token"] / c["j_per_output_token"],
        cost_premium=e["eur_per_hour"] / c["eur_per_hour"] - 1.0,
    )
    if len(cost_set) > 1:
        joules = [r["j_per_output_token"] for r in cost_set]
        result["tie_energy_spread"] = max(joules) / min(joules) - 1.0
    fewer_hosts = [r for r in others if r["hosts"] < e["hosts"]]
    if e in cost_set:
        if any(r not in cost_set for r in close):
            result["reason"] = "energy_unresolved"
            return result
        threats = [r for r in fewer_hosts if r not in cost_set]
        verdict = "coincide" if len(cost_set) == 1 else "cost_tied"
    else:
        if close:
            result["reason"] = "energy_unresolved"
            return result
        threats = fewer_hosts
        verdict = "diverge"
    if threats:
        result["reason"] = "host_scope"
        result["host_watts_to_flip"] = _host_watts_to_flip(e, threats)
        return result
    result["verdict"] = verdict
    result["h4"] = "pass" if verdict == "diverge" else "fail"
    return result


def analyze_h4(
    sweeps: list[Sweep],
    slo: Slo,
    lambda_star: float,
    gpu_type: str | None = None,
    price: PriceFn | None = None,
) -> dict:
    """The H4 verdict at λ*, and at every other level the sweeps share."""
    if not slo.given:
        raise InferenceCalibrationError(
            "--energy-at compares layouts that meet an SLO; give --tpot-slo-ms "
            "or --ttft-slo-ms"
        )
    if not (math.isfinite(lambda_star) and lambda_star > 0.0):
        raise InferenceCalibrationError("--energy-at must be a positive rate")
    ladder = common_levels(sweeps)
    if not any(_same(lam, lambda_star) for lam in ladder):
        listed = ", ".join(f"{lam:g}" for lam in ladder) or "none"
        raise InferenceCalibrationError(
            f"λ* = {lambda_star:g} req/s is not a level of every sweep (shared "
            f"levels: {listed}); sweep every layout on one pre-registered ladder"
        )
    warnings = _check_comparable(sweeps, gpu_type)
    priced = price is not None

    def at(lam: float) -> tuple[list[dict], dict]:
        rows = [_row(sweep, lam, slo, price) for sweep in sweeps]
        return rows, _verdict(rows, priced)

    rows, headline = at(lambda_star)
    temps = [r["temp_mean_c"] for r in rows if r["qualifies"] and r["temp_mean_c"]]
    if temps and max(temps) - min(temps) > TEMPERATURE_SPREAD_C:
        warnings.append(
            f"mean GPU temperatures at λ* differ by {max(temps) - min(temps):.0f} °C"
        )
    if any(r["qualifies"] and not r["drift_checked"] for r in rows):
        warnings.append("a qualifying layout has no drift control")
    if any(r["qualifies"] and r["idle_dominated"] for r in rows):
        warnings.append(
            "λ* is idle-dominated for some layout: fewest GPUs tends to win both"
        )
    other = []
    for lam in ladder:
        if _same(lam, lambda_star):
            continue
        _, verdict = at(lam)
        if verdict["qualifying"] >= 2:
            other.append({"lambda": lam, **verdict})
    return {
        "statement": STATEMENT,
        "lambda_star": lambda_star,
        "thresholds": {
            "energy_tie_pct": ENERGY_TIE_PCT,
            "cost_tie_pct": COST_TIE_PCT,
            "throughput_tolerance": THROUGHPUT_TOLERANCE,
        },
        "cost_basis": (
            "EUR per hour; at a common λ* this ranks EUR per nominal output "
            "token (λ*·O), while J per output token is measured"
        ),
        "scope": "GPU board energy only; prices cover whole instances",
        **headline,
        "layouts": rows,
        "ladder": other,
        "warnings": warnings,
    }
