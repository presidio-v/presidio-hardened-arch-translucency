"""
Architectural Translucency — ML training parallelism domain (MVP).

Extends the replication model from serving (container / pod / deployment /
node — see ``model.py``) to distributed **training** parallelism strategies.
The same core question — *at which layer does replication yield the highest
throughput gain with the lowest overhead?* — is answered for training runs:

  data      — data parallelism (DDP): full model replica per device, gradients
              all-reduced each step. Low fixed overhead; coordination cost is
              the gradient synchronisation.
  fsdp      — sharded data parallelism (FSDP / ZeRO-3): parameters, gradients
              and optimizer state sharded across devices. Higher communication
              (all-gather + reduce-scatter) but relaxes the memory constraint.
  tensor    — tensor (model) parallelism: intra-layer sharding with per-layer
              activation all-reduces. Highest coordination cost; practically
              bounded to the intra-node interconnect (NVLink) domain.
  pipeline  — pipeline parallelism: layer stages across devices. Modelled with
              the exact bubble formula ``m / (m + δ − 1)`` for m microbatches
              rather than the α/β form.

Two deliberate departures from the serving model:

* **Memory is a hard constraint, not a soft penalty.** A (strategy, δ) point
  where the per-device memory requirement exceeds device memory is *excluded*,
  not scored down. ``data`` requires the full model per device; the sharded
  strategies require ``model_memory / δ``.
* **Throughput is compute-bound, not demand-bound.** There is no demand cap:
  ω(δ) = baseline · δ · efficiency(δ) in samples/second.

Per-strategy ``overhead_alpha`` / ``overhead_beta`` defaults are MVP
placeholders in the same spirit as the serving defaults and are calibratable
via a ``training`` section in ``.pat-model.json`` / ``~/.pat/model.json``::

    {"training": {"data": {"overhead_alpha": 0.03, "overhead_beta": 0.04}}}

`pat train-calibrate` fits these from bounded recorded step-time logs and stores
a committed per-strategy record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final

from presidio_arch_translucency.model import load_calibrated_model

# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------

VALID_STRATEGIES: Final[tuple[str, ...]] = ("data", "fsdp", "tensor", "pipeline")


class TrainingDomainError(ValueError):
    """Raised on out-of-domain training parameters (fail-closed, no math on junk)."""


class TrainingCalibrationTamperError(ValueError):
    """Raised when a committed ``training[<strategy>]`` record no longer re-hashes.

    Fail-closed (ADR-0010 semantics, matching serving's
    :class:`model.CalibrationTamperError`): a consumer that finds a
    present-but-mismatched training commitment must refuse to use the fitted
    parameters rather than silently falling back to defaults.
    """


def _require_positive_finite(value: float, name: str) -> float:
    """Library-level guard: training workload numbers must be finite and > 0.

    Added for the v0.18.0 third-party audit finding: CLI bounds alone do not
    protect API callers; ``nan``/``inf`` must never reach the model equations.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrainingDomainError(
            f"{name} must be a number, got {type(value).__name__!r}"
        )
    v = float(value)
    if not math.isfinite(v) or v <= 0.0:
        raise TrainingDomainError(f"{name} must be a finite number > 0, got {v!r}")
    return v


def _require_degree(degree: int, name: str, maximum: int) -> int:
    """Guard: parallelism degrees / counts are integers within [1, maximum]."""
    if isinstance(degree, bool) or not isinstance(degree, int):
        raise TrainingDomainError(
            f"{name} must be an integer, got {type(degree).__name__!r}"
        )
    if not (1 <= degree <= maximum):
        raise TrainingDomainError(
            f"{name} must be between 1 and {maximum}, got {degree}"
        )
    return degree


class ParallelismStrategy(str, Enum):
    """Training parallelism strategies ordered by coordination overhead."""

    DATA = "data"
    FSDP = "fsdp"
    TENSOR = "tensor"
    PIPELINE = "pipeline"


ORDERED_STRATEGIES: Final[tuple[ParallelismStrategy, ...]] = (
    ParallelismStrategy.DATA,
    ParallelismStrategy.FSDP,
    ParallelismStrategy.TENSOR,
    ParallelismStrategy.PIPELINE,
)


@dataclass(frozen=True)
class StrategyParams:
    overhead_alpha: float  # fixed overhead fraction per device
    overhead_beta: float  # coordination overhead scaling with ln(δ)
    max_degree: int  # practical upper bound for the strategy
    shards_model: bool  # True → per-device memory is model_memory / δ
    description: str


STRATEGY_PARAMS: Final[dict[ParallelismStrategy, StrategyParams]] = {
    ParallelismStrategy.DATA: StrategyParams(
        overhead_alpha=0.02,
        overhead_beta=0.03,
        max_degree=64,
        shards_model=False,
        description=(
            "Data parallelism (DDP): full replica per device, gradient all-reduce"
        ),
    ),
    ParallelismStrategy.FSDP: StrategyParams(
        overhead_alpha=0.04,
        overhead_beta=0.05,
        max_degree=128,
        shards_model=True,
        description=(
            "Sharded data parallelism (FSDP/ZeRO-3): all-gather + reduce-scatter"
        ),
    ),
    ParallelismStrategy.TENSOR: StrategyParams(
        overhead_alpha=0.05,
        overhead_beta=0.12,
        max_degree=8,
        shards_model=True,
        description="Tensor parallelism: per-layer activation all-reduce (intra-node)",
    ),
    ParallelismStrategy.PIPELINE: StrategyParams(
        overhead_alpha=0.03,
        overhead_beta=0.0,  # unused — pipeline uses the exact bubble formula
        max_degree=16,
        shards_model=True,
        description="Pipeline parallelism: stage per device, bubble m/(m+δ-1)",
    ),
}

#: Fraction of device memory usable by model state; the rest is reserved for
#: activations, fragmentation and framework overhead (MVP approximation).
MEMORY_HEADROOM: Final[float] = 0.9

#: Default number of pipeline microbatches (GPipe guidance: m ≳ 4×stages keeps
#: the bubble below ~25%; 8 is a pragmatic MVP default for small stage counts).
DEFAULT_MICROBATCHES: Final[int] = 8
TRAINING_POWER_W_PER_DEVICE_MAX: Final[float] = 2_000.0


# ---------------------------------------------------------------------------
# Calibration overrides (``training`` section of the calibrated model store)
# ---------------------------------------------------------------------------


def _load_training_record(strategy: ParallelismStrategy) -> dict | None:
    """Return the ``training[<strategy>]`` record from the calibrated model, or None."""
    model = load_calibrated_model()
    if not isinstance(model, dict):
        return None
    training = model.get("training")
    if not isinstance(training, dict):
        return None
    record = training.get(strategy.value)
    return record if isinstance(record, dict) else None


def _training_status_or_raise(strategy: ParallelismStrategy, record: dict) -> str:
    """Classify the record's training commitment, raising on tamper (fail-closed).

    ``ok`` / ``legacy`` are returned; ``tampered`` raises
    :class:`TrainingCalibrationTamperError`. Importing the classifier lazily
    keeps the ``training`` <-> ``calibrate`` edge one-directional (train_calibrate
    imports training; training must not import train_calibrate).
    """
    from presidio_arch_translucency.calibrate import (  # noqa: PLC0415
        training_commitment_of,
        training_commitment_status,
    )

    record_strategy = record.get("strategy")
    if record_strategy is not None and record_strategy != strategy.value:
        raise TrainingCalibrationTamperError(
            f"calibrated training record stored under {strategy.value!r} declares "
            f"strategy {record_strategy!r}; refusing a cross-strategy record"
        )
    status = training_commitment_status(record)
    if status == "tampered":
        raise TrainingCalibrationTamperError(
            f"calibrated training parameters for {strategy.value!r} do not match "
            "their stored calibration_commitment "
            f"(expected {training_commitment_of(record)}); the model file was "
            "modified after calibration. Re-run `pat train-calibrate` to produce "
            "a fresh, committed fit."
        )
    return status


def resolve_strategy_params(strategy: ParallelismStrategy) -> StrategyParams:
    """Per-strategy parameters, honouring a committed calibrated ``training`` section.

    ``.pat-model.json`` / ``~/.pat/model.json`` may carry
    ``{"training": {"<strategy>": {"overhead_alpha": .., "overhead_beta": ..}}}``.
    ADR-0010 semantics (v0.23.0): when the record carries a
    ``calibration_commitment`` that re-hashes clean the fitted values are used;
    a present-but-mismatched commitment **raises**
    :class:`TrainingCalibrationTamperError` (fail-closed, no silent fallback);
    a record with **no** commitment is legacy (hand-written or pre-v0.23) and
    keeps the historical fitted-if-sane-else-defaults behaviour, so a v0.22-era
    model file behaves identically. Missing/malformed values still fall back to
    the defaults (this tunes an estimate, it authorizes nothing).
    """
    defaults = STRATEGY_PARAMS[strategy]
    record = _load_training_record(strategy)
    if record is None:
        return defaults

    _training_status_or_raise(strategy, record)  # fail-closed on tamper

    def _positive_float(key: str, fallback: float) -> float:
        try:
            value = float(record[key])
        except (KeyError, TypeError, ValueError):
            return fallback
        return value if 0.0 <= value < 1.0 else fallback

    return StrategyParams(
        overhead_alpha=_positive_float("overhead_alpha", defaults.overhead_alpha),
        overhead_beta=_positive_float("overhead_beta", defaults.overhead_beta),
        max_degree=defaults.max_degree,
        shards_model=defaults.shards_model,
        description=defaults.description,
    )


def resolve_training_commitment(strategy: ParallelismStrategy) -> dict:
    """Resolve the training-calibration commitment status for *strategy* (fail-closed).

    Returns ``{"status": ..., "digest": ...}`` where ``status`` is ``ok``,
    ``legacy`` (no commitment / hand-written record), or ``uncalibrated`` (no
    training record for the strategy). Raises
    :class:`TrainingCalibrationTamperError` when the record carries a commitment
    its stored parameters no longer match — the tamper signal train-analyze /
    train-what-if honour before rendering (mirrors
    ``model.resolve_calibration_commitment``).
    """
    record = _load_training_record(strategy)
    if record is None:
        return {"status": "uncalibrated", "digest": None}
    status = _training_status_or_raise(strategy, record)
    if status == "legacy":
        return {"status": "legacy", "digest": None}
    from presidio_arch_translucency.calibrate import (  # noqa: PLC0415
        training_commitment_of,
    )

    return {"status": "ok", "digest": training_commitment_of(record)}


def resolve_training_energy(strategy: ParallelismStrategy) -> dict | None:
    """Return committed fitted energy figures for *strategy*, or ``None``.

    ``{"mean_power_w": float, "watts_per_device": float}`` when the (untampered)
    training record carries them, else ``None``. Fails closed on tamper via
    :func:`resolve_strategy_params`' shared gate. Used by the ``samples/s/W``
    energy column in ``train-analyze`` / ``train-what-if``.
    """
    record = _load_training_record(strategy)
    if record is None:
        return None
    _training_status_or_raise(strategy, record)  # fail-closed on tamper
    watts_per_device = record.get("watts_per_device")
    mean_power_w = record.get("mean_power_w")
    if not isinstance(watts_per_device, (int, float)) or isinstance(
        watts_per_device, bool
    ):
        return None
    wpd = float(watts_per_device)
    if not math.isfinite(wpd) or wpd <= 0.0 or wpd > TRAINING_POWER_W_PER_DEVICE_MAX:
        return None
    mpw = None
    if isinstance(mean_power_w, (int, float)) and not isinstance(mean_power_w, bool):
        candidate = float(mean_power_w)
        if math.isfinite(candidate) and candidate > 0.0:
            mpw = candidate
    return {"mean_power_w": mpw, "watts_per_device": wpd}


# ---------------------------------------------------------------------------
# Core equations
# ---------------------------------------------------------------------------


def scaling_efficiency(
    strategy: ParallelismStrategy,
    degree: int,
    microbatches: int = DEFAULT_MICROBATCHES,
    params: StrategyParams | None = None,
) -> float:
    """Efficiency(δ) ∈ [0, 1] — fraction of ideal linear speed-up retained.

    α/β strategies: ``1 − α − β·ln(δ)`` (the serving-model form).
    Pipeline: exact bubble formula ``(1 − α) · m / (m + δ − 1)``.
    """
    if params is None:
        params = resolve_strategy_params(strategy)
    if degree <= 1:
        return 1.0
    if strategy is ParallelismStrategy.PIPELINE:
        bubble_efficiency = microbatches / (microbatches + degree - 1)
        return max(0.0, (1.0 - params.overhead_alpha) * bubble_efficiency)
    return max(
        0.0,
        1.0 - params.overhead_alpha - params.overhead_beta * math.log(degree),
    )


def training_throughput(
    baseline_samples_per_second: float,
    strategy: ParallelismStrategy,
    degree: int,
    microbatches: int = DEFAULT_MICROBATCHES,
    params: StrategyParams | None = None,
) -> float:
    """ω(δ) — samples/second at parallelism degree δ (compute-bound, no cap)."""
    eff = scaling_efficiency(strategy, degree, microbatches, params)
    return baseline_samples_per_second * degree * eff


def per_device_memory_gb(
    strategy: ParallelismStrategy,
    degree: int,
    model_memory_gb: float,
) -> float:
    """Per-device model-state memory requirement (GB) at degree δ.

    ``data`` holds a full replica; sharded strategies hold ``model / δ``.
    Activation memory is covered by the :data:`MEMORY_HEADROOM` reserve (MVP).
    """
    params = STRATEGY_PARAMS[strategy]
    if params.shards_model and degree > 1:
        return model_memory_gb / degree
    return model_memory_gb


def memory_feasible(
    strategy: ParallelismStrategy,
    degree: int,
    model_memory_gb: float,
    device_memory_gb: float,
) -> bool:
    """Hard constraint: model state must fit within the headroom-adjusted device."""
    required = per_device_memory_gb(strategy, degree, model_memory_gb)
    return required <= device_memory_gb * MEMORY_HEADROOM


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@dataclass
class StrategyResult:
    strategy: ParallelismStrategy
    optimal_degree: int  # 0 when no feasible degree exists
    estimated_samples_per_second: float
    scaling_efficiency_pct: float
    per_device_memory_gb: float
    feasible: bool
    throughput_gain_pct: float
    description: str


@dataclass
class TrainingAnalysisResult:
    recommended_strategy: ParallelismStrategy | None  # None → nothing feasible
    recommended_degree: int
    baseline_samples_per_second: float
    device_count: int
    microbatches: int
    strategies: list[StrategyResult]


def evaluate_strategy(
    strategy: ParallelismStrategy,
    degree: int,
    baseline_samples_per_second: float,
    model_memory_gb: float,
    device_memory_gb: float,
    microbatches: int = DEFAULT_MICROBATCHES,
) -> StrategyResult:
    """Evaluate a single (strategy, δ) point — the ``train-what-if`` primitive.

    Fail-closed on out-of-domain input: ``degree`` must lie within
    ``[1, max_degree]`` for the strategy (audit finding: what-if must not
    report out-of-domain configurations as feasible), and workload numbers
    must be finite and positive.
    """
    params = resolve_strategy_params(strategy)
    _require_degree(degree, "degree", params.max_degree)
    _require_degree(microbatches, "microbatches", 4096)
    baseline_samples_per_second = _require_positive_finite(
        baseline_samples_per_second, "baseline_samples_per_second"
    )
    model_memory_gb = _require_positive_finite(model_memory_gb, "model_memory_gb")
    device_memory_gb = _require_positive_finite(device_memory_gb, "device_memory_gb")
    eff = scaling_efficiency(strategy, degree, microbatches, params)
    tp = training_throughput(
        baseline_samples_per_second, strategy, degree, microbatches, params
    )
    memory = per_device_memory_gb(strategy, degree, model_memory_gb)
    feasible = memory_feasible(strategy, degree, model_memory_gb, device_memory_gb)
    gain = (
        (tp - baseline_samples_per_second)
        / max(baseline_samples_per_second, 1e-9)
        * 100.0
    )
    return StrategyResult(
        strategy=strategy,
        optimal_degree=degree,
        estimated_samples_per_second=round(tp, 2),
        scaling_efficiency_pct=round(eff * 100.0, 1),
        per_device_memory_gb=round(memory, 2),
        feasible=feasible,
        throughput_gain_pct=round(gain, 1),
        description=STRATEGY_PARAMS[strategy].description,
    )


def analyze_training(
    baseline_samples_per_second: float,
    model_memory_gb: float,
    device_memory_gb: float,
    device_count: int,
    microbatches: int = DEFAULT_MICROBATCHES,
) -> TrainingAnalysisResult:
    """
    Core training-domain analysis.

    For each strategy, sweep δ = 1..min(max_degree, device_count) over the
    *feasible* region and keep the smallest δ that strictly maximises
    throughput (mirrors the serving objective: most gain, fewest devices).
    Strategies with no feasible δ are reported with ``feasible=False`` and
    excluded from the recommendation.

    Fail-closed on out-of-domain input (finite/positive workload numbers,
    integer bounds) — see :class:`TrainingDomainError`.
    """
    baseline_samples_per_second = _require_positive_finite(
        baseline_samples_per_second, "baseline_samples_per_second"
    )
    model_memory_gb = _require_positive_finite(model_memory_gb, "model_memory_gb")
    device_memory_gb = _require_positive_finite(device_memory_gb, "device_memory_gb")
    device_count = _require_degree(device_count, "device_count", 1_000_000)
    microbatches = _require_degree(microbatches, "microbatches", 4096)

    strategy_results: list[StrategyResult] = []

    for strategy in ORDERED_STRATEGIES:
        params = resolve_strategy_params(strategy)
        max_delta = min(params.max_degree, device_count)

        best: StrategyResult | None = None
        for delta in range(1, max_delta + 1):
            if not memory_feasible(strategy, delta, model_memory_gb, device_memory_gb):
                continue
            candidate = evaluate_strategy(
                strategy,
                delta,
                baseline_samples_per_second,
                model_memory_gb,
                device_memory_gb,
                microbatches,
            )
            if (
                best is None
                or candidate.estimated_samples_per_second
                > best.estimated_samples_per_second + 1e-9
            ):
                best = candidate

        if best is None:
            # No feasible degree — report the δ=1 memory picture for diagnosis.
            strategy_results.append(
                StrategyResult(
                    strategy=strategy,
                    optimal_degree=0,
                    estimated_samples_per_second=0.0,
                    scaling_efficiency_pct=0.0,
                    per_device_memory_gb=round(
                        per_device_memory_gb(strategy, 1, model_memory_gb), 2
                    ),
                    feasible=False,
                    throughput_gain_pct=0.0,
                    description=params.description,
                )
            )
        else:
            strategy_results.append(best)

    feasible_results = [r for r in strategy_results if r.feasible]
    if not feasible_results:
        return TrainingAnalysisResult(
            recommended_strategy=None,
            recommended_degree=0,
            baseline_samples_per_second=round(baseline_samples_per_second, 2),
            device_count=device_count,
            microbatches=microbatches,
            strategies=strategy_results,
        )

    def score(r: StrategyResult) -> tuple[float, int]:
        # Highest gain wins; ties broken towards the FEWEST devices.
        return (r.throughput_gain_pct, -r.optimal_degree)

    winner = max(feasible_results, key=score)
    return TrainingAnalysisResult(
        recommended_strategy=winner.strategy,
        recommended_degree=winner.optimal_degree,
        baseline_samples_per_second=round(baseline_samples_per_second, 2),
        device_count=device_count,
        microbatches=microbatches,
        strategies=strategy_results,
    )
