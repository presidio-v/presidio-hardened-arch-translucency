"""
Per-layout SLO capacity from λ sweeps, measured vs predicted (Inference Arc 1d).

`pat infer-validate --sweep` compares the λ_max a profile predicts for one
layout with the λ_max an open-loop sweep (`pat infer-benchmark --rate`)
measured on it — the paper's H3. Definitions (ADR-0012, 1d amendment):

* **Level classification.** Per SLO a level *passes* when it was recorded,
  not saturated, and meets the SLO; it *fails* when it saturated or was
  recorded above the SLO; otherwise (refused but not saturated) it carries no
  information. TPOT is the window mean (exact, and what the model predicts).
  TTFT is judged only at a histogram bucket edge — the fraction of first
  tokens under the edge, ≥ 0.99 — because ``histogram_quantile`` interpolates
  inside vLLM's coarse buckets and its p99 is an artefact.
* **Measured λ_max is a bracket, never a point.** λ_hi is the first failing
  level, λ_lo the highest passing level below it. Passing levels above λ_hi
  are flagged ``non_monotone``, never averaged away. A sweep that never fails
  gives a lower bound only; one that fails at its first level, an upper bound
  only. The point estimate is the geometric midpoint; the metric is not
  interpolated (the failing level is usually saturated and has no gated TPOT,
  and latency is convex in λ, so a chord is biased).
* **Predicted λ_max** bisects λ on the recommender's own predicate
  (feasible, ρ ≤ 0.95, SLOs met), so the number validated is the number pat
  would act on. ``lambda_sat`` is the model's capacity (ρ = 1).
* **Error is an interval.** ``to_bracket_pct`` is 0 inside [λ_lo, λ_hi], else
  the distance to the nearer edge; ``worst_pct`` the distance to the farther
  edge. H3 passes when the worst case is within 20 %; it fails (the kill
  criterion) when even the nearer edge is more than 25 % away; anything else,
  and any non-monotone bracket, is inconclusive. An unrefined ×1.25 bracket
  passes only a prediction in [λ_lo, 1.2·λ_lo]; ``--refine`` narrows the
  bracket so the verdict no longer depends on where in it the truth sits.
* **What was tested.** The SLO row records which constraint ends the
  predicted range (the SLO, or ρ = 0.95) and why the measured λ_hi failed
  (over the SLO, or saturated). A mismatch is inconclusive; when both are
  utilisation the row validates capacity, not latency, and says so. The
  capacity row (model ρ = 1 against client-side saturation, which trips
  somewhat below ρ = 1) is a mechanism check with its own wording, not H3.

Sweep reports are operator-written, unsigned files: they are checked for
shape and consistency and their SHA-256 is echoed, but not trusted further.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from presidio_arch_translucency.infer_calibrate import (
    InferenceCalibrationError,
    InferenceProfile,
    _label,
    _reject_constant,
    _reject_duplicate_keys,
    read_regular_file,
)
from presidio_arch_translucency.inference import (
    SUPPORTED_TP_DEGREES,
    EngineSpec,
    GpuSpec,
    InferenceWorkload,
    _qualifies,
    evaluate_config,
)

BENCHMARK_SCHEMA: Final[str] = "presidio-hardened/inference-benchmark@1"
MAX_SWEEP_FILE_BYTES: Final[int] = 16 * 1024 * 1024
MAX_SWEEPS: Final[int] = 16
#: TTFT passes at an edge when at least this fraction of first tokens is under it.
TTFT_QUANTILE: Final[float] = 0.99
#: H3: the worst case over the bracket is within this many percent.
H3_PASS_PCT: Final[float] = 20.0
#: Kill criterion: even the nearer bracket edge is further than this.
H3_KILL_PCT: Final[float] = 25.0
#: Predicted λ_max closer than this (percent) are a tie: no predicted order.
RANK_TIE_PCT: Final[float] = 1.0
#: Bisection stops when the predicted bracket is this narrow (ratio).
_BISECT_RATIO: Final[float] = 1.005
_EDGE_TOLERANCE: Final[float] = 1e-9

PASS: Final[str] = "pass"  # noqa: S105 -- a level outcome, not a secret
FAIL: Final[str] = "fail"


# ---------------------------------------------------------------------------
# Level classification (shared with the harness's --refine)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slo:
    tpot_ms: float | None = None
    ttft_ms: float | None = None

    def __post_init__(self) -> None:
        for name in ("tpot_ms", "ttft_ms"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InferenceCalibrationError(f"{name} SLO must be a number")
            if not (math.isfinite(value) and 0.0 < value <= 3_600_000.0):
                raise InferenceCalibrationError(
                    f"{name} SLO must be in (0, 3600000] ms"
                )

    @property
    def given(self) -> bool:
        return self.tpot_ms is not None or self.ttft_ms is not None


Classifier = Callable[[dict], "str | None"]


def classify_saturation(level: dict) -> str | None:
    """Capacity: a level fails by saturating (client-side signals).

    An unsaturated level whose requests failed proves nothing about capacity.
    """
    if level.get("saturated"):
        return FAIL
    return None if level.get("errors") else PASS


def ttft_fraction_at(level: dict, edge_ms: float) -> float | None:
    """Fraction of first tokens under a bucket edge, or None if not an edge."""
    cdf = level.get("ttft_cdf")
    if not isinstance(cdf, dict):
        return None
    for key, fraction in cdf.items():
        try:
            le_ms = float(key) * 1e3
        except ValueError:
            continue
        if math.isclose(le_ms, edge_ms, rel_tol=_EDGE_TOLERANCE):
            return fraction
    return None


def slo_classifier(slo: Slo) -> Classifier:
    """Pass when recorded, unsaturated and within every given SLO."""

    def classify(level: dict) -> str | None:
        if level.get("saturated"):
            return FAIL
        if level.get("verdict") != "recorded":
            return None
        outcomes = []
        if slo.tpot_ms is not None:
            point = level.get("point") or {}
            tpot = point.get("tpot_ms")
            outcomes.append(None if tpot is None else tpot <= slo.tpot_ms)
        if slo.ttft_ms is not None:
            fraction = ttft_fraction_at(level, slo.ttft_ms)
            outcomes.append(None if fraction is None else fraction >= TTFT_QUANTILE)
        if False in outcomes:
            return FAIL
        if None in outcomes:
            return None
        return PASS

    return classify


# ---------------------------------------------------------------------------
# Measured bracket
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bracket:
    lo: float | None  # highest passing level below the first failure
    hi: float | None  # first failing level
    non_monotone: bool  # a level above hi passed
    uninformative_inside: int  # levels strictly inside (lo, hi) that told nothing

    @property
    def resolved(self) -> bool:
        return self.lo is not None and self.hi is not None

    @property
    def estimate(self) -> float | None:
        return math.sqrt(self.lo * self.hi) if self.resolved else None

    def as_dict(self) -> dict:
        return {
            "lambda_lo": self.lo,
            "lambda_hi": self.hi,
            "lambda_hat": self.estimate,
            "resolved": self.resolved,
            "non_monotone": self.non_monotone,
            "uninformative_inside": self.uninformative_inside,
        }


def find_bracket(levels: list[dict], classify: Classifier) -> Bracket:
    """The first-failure bracket over levels sorted by nominal λ."""
    judged = sorted(
        ((float(level["level"]), classify(level)) for level in levels),
        key=lambda item: item[0],
    )
    failures = [lam for lam, outcome in judged if outcome == FAIL]
    hi = failures[0] if failures else None
    below = [
        lam for lam, outcome in judged if outcome == PASS and (hi is None or lam < hi)
    ]
    lo = max(below) if below else None
    non_monotone = hi is not None and any(
        outcome == PASS and lam > hi for lam, outcome in judged
    )
    inside = sum(
        1
        for lam, outcome in judged
        if outcome is None and (lo is None or lam > lo) and (hi is None or lam < hi)
    )
    return Bracket(lo, hi, non_monotone, inside)


# ---------------------------------------------------------------------------
# Predicted capacity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredictedCapacity:
    lambda_slo: float  # highest λ the recommender would accept (0: never)
    lambda_sat: float  # model capacity, ρ = 1
    extrapolated: bool
    binding: str  # what ends the accepted range: "slo" or "utilization"


def predict_capacity(
    profile: InferenceProfile,
    tp: int,
    instances: int,
    prompt_tokens: float,
    output_tokens: float,
    slo: Slo,
) -> PredictedCapacity:
    """Bisect λ on the recommender's predicate for one (tp, n) layout."""
    hw = profile.hardware
    model = hw.model_spec()
    gpu = GpuSpec(
        count=tp * instances,
        memory_gb=hw.gpu_memory_gb,
        bandwidth_gbs=hw.gpu_bandwidth_gbs,
        gpus_per_node=tp,
        memory_utilization=hw.gpu_memory_utilization,
    )
    engine = EngineSpec(max_num_seqs=hw.max_num_seqs, tp_degrees=(tp,))

    def evaluate(lam: float):  # noqa: ANN202
        return evaluate_config(
            tp,
            instances,
            InferenceWorkload(lam, prompt_tokens, output_tokens),
            model,
            gpu,
            engine,
            profile.params,
            ttft_slo_ms=slo.ttft_ms,
            tpot_slo_ms=slo.tpot_ms,
        )

    probe = evaluate(1e-6)
    if not probe.feasible:
        raise InferenceCalibrationError(
            f"tp={tp} n={instances} is infeasible for this profile (weights or "
            "KV cache do not fit); there is no capacity to predict"
        )
    capacity = probe.capacity_rps
    extrapolated = tp > max(p.tp for p in profile.points) or instances >= 2
    if not _qualifies(probe):
        return PredictedCapacity(0.0, capacity, extrapolated, "slo")
    lo, hi = 1e-6, capacity
    while hi / lo > _BISECT_RATIO:
        mid = math.sqrt(lo * hi)
        if _qualifies(evaluate(mid)):
            lo = mid
        else:
            hi = mid
    beyond = evaluate(hi)
    binding = (
        "slo"
        if not beyond.saturated and not beyond.near_saturation and not beyond.slo_ok
        else "utilization"
    )
    return PredictedCapacity(lo, capacity, extrapolated, binding)


# ---------------------------------------------------------------------------
# Error interval and verdict
# ---------------------------------------------------------------------------


_MECHANISM_WORDS: Final[dict[str, str]] = {
    "pass": "consistent",
    "fail": "inconsistent",
}


def capacity_error(predicted: float, bracket: Bracket, role: str = "h3") -> dict:
    """Interval-valued error of a prediction against a measured bracket.

    ``role="mechanism"`` words the verdict as a consistency check rather than
    the H3 pass/fail (the capacity row compares two different definitions).
    """
    if not bracket.resolved:
        return {"to_bracket_pct": None, "worst_pct": None, "verdict": "unresolved"}
    lo, hi = bracket.lo, bracket.hi
    if lo <= predicted <= hi:
        to_bracket = 0.0
    elif predicted < lo:
        to_bracket = (lo - predicted) / lo * 100.0
    else:
        to_bracket = (predicted - hi) / hi * 100.0
    worst = max(abs(predicted - lo) / lo, abs(predicted - hi) / hi) * 100.0
    if bracket.non_monotone:
        verdict = "inconclusive"  # the measurement itself is suspect
    elif to_bracket > H3_KILL_PCT:
        verdict = "fail"
    elif worst <= H3_PASS_PCT:
        verdict = "pass"
    else:
        verdict = "inconclusive"
    if role == "mechanism":
        verdict = _MECHANISM_WORDS.get(verdict, verdict)
    return {
        "to_bracket_pct": round(to_bracket, 2),
        "worst_pct": round(worst, 2),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Sweep files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sweep:
    path: str
    sha256: str
    model: str
    tp: int
    instances: int
    prompt_tokens: int
    output_tokens: int
    levels: list[dict]

    @property
    def gpus(self) -> int:
        return self.tp * self.instances

    @property
    def label(self) -> str:
        return f"tp{self.tp}×{self.instances}"


def _positive_int(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InferenceCalibrationError(f"{name} must be an integer")
    if not 1 <= value <= maximum:
        raise InferenceCalibrationError(f"{name} must be in [1, {maximum}]")
    return value


def _level(entry: object, index: int) -> dict:
    if not isinstance(entry, dict):
        raise InferenceCalibrationError(f"level {index} is not an object")
    lam = entry.get("level")
    if (
        isinstance(lam, bool)
        or not isinstance(lam, (int, float))
        or not (math.isfinite(lam) and lam > 0.0)
    ):
        raise InferenceCalibrationError(f"level {index} has no positive rate")
    if entry.get("verdict") not in ("recorded", "refused"):
        raise InferenceCalibrationError(f"level {index} has no verdict")
    if not isinstance(entry.get("saturated"), bool):
        raise InferenceCalibrationError(f"level {index} has no saturation flag")
    point = entry.get("point")
    if point is not None:
        tpot = point.get("tpot_ms") if isinstance(point, dict) else None
        if (
            isinstance(tpot, bool)
            or not isinstance(tpot, (int, float))
            or not (math.isfinite(tpot) and tpot > 0.0)
        ):
            raise InferenceCalibrationError(f"level {index} point has no TPOT")
    errors = entry.get("errors", 0)
    if isinstance(errors, bool) or not isinstance(errors, int) or errors < 0:
        raise InferenceCalibrationError(f"level {index} has a malformed error count")
    cdf = entry.get("ttft_cdf")
    if cdf is not None:
        _check_cdf(cdf, index)
    return entry


def _check_cdf(cdf: object, index: int) -> None:
    """Edges parse, are unique, and the fractions never decrease with them."""
    malformed = InferenceCalibrationError(f"level {index} has a malformed ttft_cdf")
    if not isinstance(cdf, dict):
        raise malformed
    edges: dict[float, float] = {}
    for key, value in cdf.items():
        try:
            edge = float(key)
        except ValueError:
            raise malformed from None
        if (
            math.isnan(edge)
            or edge in edges
            or isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= value <= 1.0
        ):
            raise malformed
        edges[edge] = value
    fractions = [edges[edge] for edge in sorted(edges)]
    if any(
        later < earlier
        for earlier, later in zip(fractions, fractions[1:], strict=False)
    ):
        raise malformed


def load_sweep(path: str | Path) -> Sweep:
    """Read one open-loop sweep report and check its shape (fail-closed)."""
    text = read_regular_file(path, MAX_SWEEP_FILE_BYTES, "sweep report")
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ValueError as exc:
        raise InferenceCalibrationError(f"sweep report is not JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != BENCHMARK_SCHEMA:
        raise InferenceCalibrationError(
            f"sweep report schema is not {BENCHMARK_SCHEMA}"
        )
    if data.get("error") is not None:
        raise InferenceCalibrationError(
            f"sweep ended with an error ({data['error']}); re-run it"
        )
    config = data.get("config")
    if not isinstance(config, dict) or config.get("mode") != "open":
        raise InferenceCalibrationError(
            "sweep report is not an open-loop (--rate) sweep"
        )
    tp = _positive_int(config.get("tp"), "tp", max(SUPPORTED_TP_DEGREES))
    if tp not in SUPPORTED_TP_DEGREES:
        raise InferenceCalibrationError(f"tp must be one of {SUPPORTED_TP_DEGREES}")
    instances = _positive_int(config.get("instances", 1), "instances", 64)
    if instances != 1:
        raise InferenceCalibrationError(
            "replica layouts (instances > 1) are not measurable yet: the harness "
            "and the observe gate read exactly one engine"
        )
    model = config.get("model")
    if model is None or model == "":
        raise InferenceCalibrationError("sweep report has no model")
    _label(model, "sweep model")
    levels = data.get("levels")
    if not isinstance(levels, list) or not levels:
        raise InferenceCalibrationError("sweep report has no levels")
    return Sweep(
        path=str(path),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        model=model,
        tp=tp,
        instances=instances,
        prompt_tokens=_positive_int(
            config.get("prompt_tokens"), "prompt_tokens", 10**6
        ),
        output_tokens=_positive_int(
            config.get("output_tokens"), "output_tokens", 10**6
        ),
        levels=[_level(entry, i) for i, entry in enumerate(levels)],
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _ttft_edges(sweeps: list[Sweep]) -> set[float]:
    edges: set[float] = set()
    for sweep in sweeps:
        for level in sweep.levels:
            for key in level.get("ttft_cdf") or {}:
                try:
                    edge = float(key)
                except ValueError:
                    continue
                if math.isfinite(edge):
                    edges.add(edge * 1e3)
    return edges


def _check_sweeps(profile: InferenceProfile, sweeps: list[Sweep], slo: Slo) -> None:
    if not 1 <= len(sweeps) <= MAX_SWEEPS:
        raise InferenceCalibrationError(f"give 1–{MAX_SWEEPS} sweep reports")
    first = sweeps[0]
    for sweep in sweeps[1:]:
        if (sweep.model, sweep.prompt_tokens, sweep.output_tokens) != (
            first.model,
            first.prompt_tokens,
            first.output_tokens,
        ):
            raise InferenceCalibrationError(
                f"{sweep.path} differs from {first.path} in model, P or O; "
                "layouts are only comparable on one workload"
            )
    expected = profile.hardware.model_name
    if expected is not None and first.model != expected:
        raise InferenceCalibrationError(
            f"sweeps served {first.model!r} but profile {profile.name!r} was "
            f"calibrated on {expected!r}"
        )
    if slo.ttft_ms is not None:
        edges = _ttft_edges(sweeps)
        if not edges:
            raise InferenceCalibrationError(
                "a TTFT SLO needs ttft_cdf in the sweep levels (re-run with a "
                "current pat infer-benchmark)"
            )
        if not any(
            math.isclose(edge, slo.ttft_ms, rel_tol=_EDGE_TOLERANCE) for edge in edges
        ):
            listed = ", ".join(f"{edge:g}" for edge in sorted(edges))
            raise InferenceCalibrationError(
                f"TTFT SLO {slo.ttft_ms:g} ms is not a histogram bucket edge; the "
                f"fraction is only exact at an edge (edges, ms: {listed})"
            )


def _layout(profile: InferenceProfile, sweep: Sweep, slo: Slo) -> dict:
    predicted = predict_capacity(
        profile,
        sweep.tp,
        sweep.instances,
        sweep.prompt_tokens,
        sweep.output_tokens,
        slo,
    )
    sat = find_bracket(sweep.levels, classify_saturation)
    by_level = {float(level["level"]): level for level in sweep.levels}
    row = {
        "sweep": sweep.path,
        "sha256": sweep.sha256,
        "layout": sweep.label,
        "tp": sweep.tp,
        "instances": sweep.instances,
        "gpus": sweep.gpus,
        "levels": len(sweep.levels),
        "extrapolated": predicted.extrapolated,
        "saturation": {
            "role": "mechanism",
            "definition": (
                "model capacity at ρ = 1 vs client-side saturation, which trips "
                "below ρ = 1; expect the measured bracket to sit a few percent low"
            ),
            "measured": sat.as_dict(),
            "predicted": round(predicted.lambda_sat, 4),
            "error": capacity_error(predicted.lambda_sat, sat, role="mechanism"),
        },
        "slo": None,
    }
    if slo.given:
        measured = find_bracket(sweep.levels, slo_classifier(slo))
        cause = None
        if measured.hi is not None:
            cause = "utilization" if by_level[measured.hi]["saturated"] else "slo"
        error = capacity_error(predicted.lambda_slo, measured)
        if cause is not None and cause != predicted.binding:
            error = {
                **error,
                "verdict": "inconclusive",
                "reason": (
                    f"the model ends the range on {predicted.binding} but the "
                    f"sweep first failed on {cause}"
                ),
            }
        row["slo"] = {
            "role": "h3",
            "binding": {"predicted": predicted.binding, "measured": cause},
            "latency_tested": predicted.binding == "slo" and cause == "slo",
            "measured": measured.as_dict(),
            "predicted": round(predicted.lambda_slo, 4),
            "error": error,
        }
    return row


def _ranking(layouts: list[dict], metric: str) -> list[dict]:
    """Per GPU budget: do predicted and measured orders of λ_max agree?"""
    groups: dict[int, list[dict]] = {}
    for row in layouts:
        groups.setdefault(row["gpus"], []).append(row)
    rankings = []
    for gpus, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        values = sorted(r[metric]["predicted"] for r in rows)
        tied = any(
            low <= 0.0 or (high - low) / low * 100.0 < RANK_TIE_PCT
            for low, high in zip(values, values[1:], strict=False)
        )
        predicted = (
            None
            if tied
            else [
                r["layout"] for r in sorted(rows, key=lambda r: -r[metric]["predicted"])
            ]
        )
        brackets = [r[metric]["measured"] for r in rows]
        separated = all(
            b["resolved"] and not b["non_monotone"] for b in brackets
        ) and all(
            a["lambda_hi"] <= b["lambda_lo"] or b["lambda_hi"] <= a["lambda_lo"]
            for i, a in enumerate(brackets)
            for b in brackets[i + 1 :]
        )
        measured = (
            [
                r["layout"]
                for r in sorted(
                    rows, key=lambda r: -r[metric]["measured"]["lambda_hat"]
                )
            ]
            if separated
            else None
        )
        rankings.append(
            {
                "gpus": gpus,
                "metric": metric,
                "predicted": predicted,
                "measured": measured,
                "agree": (
                    None
                    if measured is None or predicted is None
                    else measured == predicted
                ),
                "reason": (
                    f"predictions within {RANK_TIE_PCT:g} %"
                    if predicted is None
                    else "measured brackets overlap or are unresolved"
                    if measured is None
                    else None
                ),
            }
        )
    return rankings


def validate_sweeps(profile: InferenceProfile, sweeps: list[Sweep], slo: Slo) -> dict:
    """Measured vs predicted λ_max per layout, and the ranking per GPU budget."""
    _check_sweeps(profile, sweeps, slo)
    layouts = [_layout(profile, sweep, slo) for sweep in sweeps]
    first = sweeps[0]
    return {
        "workload": {
            "model": first.model,
            "prompt_tokens": first.prompt_tokens,
            "output_tokens": first.output_tokens,
        },
        "slo": {
            "tpot_ms": slo.tpot_ms,
            "ttft_ms": slo.ttft_ms,
            "ttft_quantile": TTFT_QUANTILE,
        },
        "thresholds": {
            "pass_pct": H3_PASS_PCT,
            "kill_pct": H3_KILL_PCT,
            "rank_tie_pct": RANK_TIE_PCT,
        },
        "layouts": layouts,
        "ranking": _ranking(layouts, "slo" if slo.given else "saturation"),
    }
