"""
Architectural Translucency — LLM inference serving domain (Phase 0, uncalibrated).

Extends the replication model to **LLM inference serving** (vLLM-style engines),
answering the same question as the serving and training domains — *at which
layer does replication yield the most capacity for the least overhead?* — for
a fixed GPU budget N. A configuration is ``(tp, n)``: ``n`` engine instances,
each tensor-parallel over ``tp`` GPUs, with ``tp · n ≤ N``. The strategy label is
derived, not chosen (ADR-0012):

  replica   — ``tp = 1``: independent single-GPU instances behind a router.
  tensor    — ``n = 1``: one instance sharded over ``tp`` GPUs.
  hybrid    — ``tp > 1`` and ``n > 1``: replicas of tensor-parallel groups.

The mechanism that makes the layer matter is **KV-cache capacity**. A replica
holds the full weights W on every GPU, leaving ``h·M − W`` for KV cache; a
TP group holds ``W/tp`` per GPU and pools ``tp·h·M − W``. As ``W → h·M`` the
replica's concurrent-sequence budget collapses while TP's does not, so TP can
win despite its all-reduce overhead. Equations (per instance):

  KV tokens        T      = (tp·h·M − W) / k
  max batch        b_max  = min(B_max, ⌊T / (P + O)⌋)
  decode step      t(b)   = (W + b·(P+O)·k) / (tp·η·BW) + (α + β·ln tp)·W/(η·BW) + t₀
  prefill          t_pre  = P / (R_pre · tp · (1 − α − β·ln tp))
  service time     S(b)   = t_pre + O · t(b)
  in-flight batch  b_eff  = λ_i · S(b_eff)     (Little; closed form, S affine in b)
  TTFT             t_pre + Erlang-C wait, M/M/c with c = b_max, μ = 1/S(b_eff)
  TPOT             t(b_eff)

Two departures from the serving model, as in training (ADR-0009): weights that
do not fit (``W/tp > h·M``) or leave no room for one sequence (``b_max = 0``)
are **infeasible** — excluded, not scored down; and a configuration whose
offered load reaches capacity (ρ ≥ 1) is reported as **saturated** and never
recommended; one above :data:`NEAR_SATURATION_RHO` is flagged and not
recommended either (its queueing tail is unbounded in practice).

The coordination term is charged against the *unsharded* weight-read time, so
the all-reduce cost does not shrink with tp: sharding divides the bytes each
GPU reads, not the synchronisation it waits on.

Every figure this module produces is **modelled, not measured**. The α/β and
hardware-efficiency defaults are MVP placeholders; Phase 1 (`pat
infer-calibrate`) fits them from vLLM metrics and stores them with an ADR-0010
calibration commitment. Until then no model-file section is read — overrides
are explicit arguments only. Out of scope (ADR-0012): pipeline and cross-node
parallelism, compute-bound decode, chunked prefill, prefix caching,
speculative decoding, MoE, quantized KV, heterogeneous GPUs, energy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final

# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------

VALID_SERVING_STRATEGIES: Final[tuple[str, ...]] = ("replica", "tensor", "hybrid")

#: Tensor-parallel degrees vLLM supports in practice (attention heads divide
#: evenly). Cross-node TP is out of scope, so ``tp`` is also capped by the
#: GPUs per node.
SUPPORTED_TP_DEGREES: Final[tuple[int, ...]] = (1, 2, 4, 8, 16)
DEFAULT_TP_DEGREES: Final[tuple[int, ...]] = (1, 2, 4, 8)

DEFAULT_GPUS_PER_NODE: Final[int] = 8
DEFAULT_GPU_MEMORY_UTILIZATION: Final[float] = 0.9  # vLLM default
DEFAULT_MAX_NUM_SEQS: Final[int] = 256  # vLLM default

MAX_GPUS: Final[int] = 4096
MAX_NUM_SEQS_LIMIT: Final[int] = 65_536
MAX_TOKENS: Final[float] = 10_000_000.0

#: Utilization above which a configuration is flagged ``near_saturation`` and
#: excluded from recommendation: the M/M/c tail grows without bound as ρ → 1.
NEAR_SATURATION_RHO: Final[float] = 0.95


class InferenceDomainError(ValueError):
    """Raised on out-of-domain inference parameters (fail-closed, no math on junk)."""


class ServingStrategy(str, Enum):
    """Inference replication strategies (labels derived from ``(tp, n)``)."""

    REPLICA = "replica"
    TENSOR = "tensor"
    HYBRID = "hybrid"


def strategy_for(tp: int, instances: int) -> ServingStrategy:
    """Derive the strategy label for a ``(tp, n)`` configuration."""
    if tp == 1:
        return ServingStrategy.REPLICA
    if instances == 1:
        return ServingStrategy.TENSOR
    return ServingStrategy.HYBRID


@dataclass(frozen=True)
class OverheadParams:
    overhead_alpha: float  # fixed per-instance overhead fraction (router, scheduler)
    overhead_beta: float  # all-reduce overhead scaling with ln(tp)


#: MVP placeholders, uncalibrated. ``hybrid`` shares the tensor record: its
#: extra cost over pure TP is routing, already inside α.
OVERHEAD_PARAMS: Final[dict[ServingStrategy, OverheadParams]] = {
    ServingStrategy.REPLICA: OverheadParams(overhead_alpha=0.02, overhead_beta=0.0),
    ServingStrategy.TENSOR: OverheadParams(overhead_alpha=0.05, overhead_beta=0.10),
    ServingStrategy.HYBRID: OverheadParams(overhead_alpha=0.05, overhead_beta=0.10),
}


@dataclass(frozen=True)
class EngineParams:
    """Hardware/engine efficiency parameters (global, calibratable in Phase 1)."""

    bandwidth_efficiency: float = 0.6  # achieved fraction of spec HBM bandwidth
    step_overhead_ms: float = 2.0  # t₀: scheduler, sampling, kernel launch
    prefill_tokens_per_s_per_gpu: float = 20_000.0  # R_pre


DEFAULT_ENGINE_PARAMS: Final[EngineParams] = EngineParams()


# ---------------------------------------------------------------------------
# Validation (fail-closed; the CLI bounds alone do not protect API callers)
# ---------------------------------------------------------------------------


def _require_bounded(value: float, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InferenceDomainError(
            f"{name} must be a number, got {type(value).__name__!r}"
        )
    v = float(value)
    if not math.isfinite(v) or not (minimum <= v <= maximum):
        raise InferenceDomainError(
            f"{name} must be a finite number in [{minimum}, {maximum}], got {v!r}"
        )
    return v


def _require_positive(value: float, name: str, maximum: float) -> float:
    v = _require_bounded(value, name, 0.0, maximum)
    if v <= 0.0:
        raise InferenceDomainError(f"{name} must be > 0, got {v!r}")
    return v


def _require_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InferenceDomainError(
            f"{name} must be an integer, got {type(value).__name__!r}"
        )
    if not (minimum <= value <= maximum):
        raise InferenceDomainError(
            f"{name} must be between {minimum} and {maximum}, got {value}"
        )
    return value


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InferenceWorkload:
    requests_per_second: float  # λ, mean arrival rate (Poisson assumption)
    prompt_tokens: float  # P, mean prompt length
    output_tokens: float  # O, mean generated tokens

    def __post_init__(self) -> None:
        _require_positive(self.requests_per_second, "requests_per_second", 1e6)
        _require_positive(self.prompt_tokens, "prompt_tokens", MAX_TOKENS)
        _require_positive(self.output_tokens, "output_tokens", MAX_TOKENS)


@dataclass(frozen=True)
class ModelSpec:
    weights_gb: float  # W at the deployed dtype
    kv_bytes_per_token: float  # k = 2 · layers · kv_heads · head_dim · dtype_bytes

    def __post_init__(self) -> None:
        _require_positive(self.weights_gb, "weights_gb", 1e5)
        _require_positive(self.kv_bytes_per_token, "kv_bytes_per_token", 1e8)


@dataclass(frozen=True)
class GpuSpec:
    count: int  # N, the GPU budget
    memory_gb: float  # M per GPU
    bandwidth_gbs: float  # spec HBM bandwidth per GPU (GB/s)
    gpus_per_node: int = DEFAULT_GPUS_PER_NODE
    memory_utilization: float = DEFAULT_GPU_MEMORY_UTILIZATION  # h

    def __post_init__(self) -> None:
        _require_int(self.count, "gpus", 1, MAX_GPUS)
        _require_positive(self.memory_gb, "gpu_memory_gb", 1e4)
        _require_positive(self.bandwidth_gbs, "gpu_bandwidth_gbs", 1e6)
        _require_int(self.gpus_per_node, "gpus_per_node", 1, 64)
        _require_bounded(self.memory_utilization, "gpu_memory_utilization", 0.05, 1.0)


@dataclass(frozen=True)
class EngineSpec:
    max_num_seqs: int = DEFAULT_MAX_NUM_SEQS  # B_max
    tp_degrees: tuple[int, ...] = DEFAULT_TP_DEGREES

    def __post_init__(self) -> None:
        _require_int(self.max_num_seqs, "max_num_seqs", 1, MAX_NUM_SEQS_LIMIT)
        if not self.tp_degrees:
            raise InferenceDomainError("tp_degrees must not be empty")
        for tp in self.tp_degrees:
            _require_int(tp, "tp degree", 1, max(SUPPORTED_TP_DEGREES))
            if tp not in SUPPORTED_TP_DEGREES:
                raise InferenceDomainError(
                    f"tp degree must be one of {SUPPORTED_TP_DEGREES}, got {tp}"
                )


def _validate_engine_params(params: EngineParams) -> EngineParams:
    _require_bounded(params.bandwidth_efficiency, "bandwidth_efficiency", 0.01, 1.0)
    _require_bounded(params.step_overhead_ms, "step_overhead_ms", 0.0, 10_000.0)
    _require_positive(
        params.prefill_tokens_per_s_per_gpu, "prefill_tokens_per_s_per_gpu", 1e9
    )
    return params


# ---------------------------------------------------------------------------
# Core equations
# ---------------------------------------------------------------------------

_GB: Final[float] = 1e9


def coordination_overhead(strategy: ServingStrategy, tp: int) -> float:
    """``α + β·ln tp`` — coordination cost as a fraction of one unsharded read."""
    p = OVERHEAD_PARAMS[strategy]
    return p.overhead_alpha + p.overhead_beta * math.log(tp)


def prefill_efficiency(strategy: ServingStrategy, tp: int) -> float:
    """``1 − α − β·ln tp`` clamped at a small floor (the serving-model form)."""
    p = OVERHEAD_PARAMS[strategy]
    return max(1e-3, 1.0 - p.overhead_alpha - p.overhead_beta * math.log(tp))


def kv_tokens_per_instance(model: ModelSpec, gpu: GpuSpec, tp: int) -> float:
    """``T = (tp·h·M − W) / k`` — KV-cache token capacity; ≤ 0 means no room."""
    pool_bytes = (tp * gpu.memory_utilization * gpu.memory_gb - model.weights_gb) * _GB
    return pool_bytes / model.kv_bytes_per_token


def weights_fit(model: ModelSpec, gpu: GpuSpec, tp: int) -> bool:
    """Hard constraint: the per-GPU weight shard fits in the usable memory."""
    return model.weights_gb / tp <= gpu.memory_utilization * gpu.memory_gb


def max_batch(
    workload: InferenceWorkload,
    model: ModelSpec,
    gpu: GpuSpec,
    engine: EngineSpec,
    tp: int,
) -> int:
    """``b_max = min(B_max, ⌊T / (P + O)⌋)`` — concurrent sequences per instance.

    Uses the full-occupancy footprint ``P + O`` per sequence (conservative).
    """
    if not weights_fit(model, gpu, tp):
        return 0
    tokens = kv_tokens_per_instance(model, gpu, tp)
    per_seq = workload.prompt_tokens + workload.output_tokens
    return max(0, min(engine.max_num_seqs, math.floor(tokens / per_seq)))


def decode_step_s(
    batch: float,
    workload: InferenceWorkload,
    model: ModelSpec,
    gpu: GpuSpec,
    tp: int,
    strategy: ServingStrategy,
    params: EngineParams,
) -> float:
    """``t(b)`` — bandwidth-bound decode step.

    Weights and live KV are read in parallel across the tp shards; the
    coordination term ``(α + β·ln tp)·W/(η·BW)`` is charged against the
    unsharded weight-read time so it does not shrink with tp.
    """
    per_seq_kv = (workload.prompt_tokens + workload.output_tokens) * (
        model.kv_bytes_per_token
    )
    bytes_read = model.weights_gb * _GB + batch * per_seq_kv
    gpu_bw = params.bandwidth_efficiency * gpu.bandwidth_gbs * _GB
    coordination = coordination_overhead(strategy, tp) * model.weights_gb * _GB
    return (
        bytes_read / (tp * gpu_bw)
        + coordination / gpu_bw
        + params.step_overhead_ms / 1000.0
    )


def prefill_s(
    workload: InferenceWorkload,
    tp: int,
    strategy: ServingStrategy,
    params: EngineParams,
) -> float:
    """``t_pre = P / (R_pre · tp · (1 − α − β·ln tp))``."""
    rate = params.prefill_tokens_per_s_per_gpu * tp * prefill_efficiency(strategy, tp)
    return workload.prompt_tokens / rate


def erlang_c(servers: int, offered_load: float) -> float:
    """P(an arrival waits) in M/M/c (Erlang C), via the stable Erlang-B recursion.

    ``offered_load`` is ``a = λ/μ``; requires ``a < servers`` (caller guarantees).
    """
    b = 1.0
    for k in range(1, servers + 1):
        b = offered_load * b / (k + offered_load * b)
    return servers * b / (servers - offered_load * (1.0 - b))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


@dataclass
class ConfigResult:
    tp: int
    instances: int
    strategy: ServingStrategy
    gpus: int
    feasible: bool  # weights fit and at least one sequence fits the KV cache
    saturated: bool  # offered load ≥ capacity (ρ ≥ 1); never recommended
    near_saturation: bool  # ρ > NEAR_SATURATION_RHO; never recommended
    max_batch: int
    effective_batch: float
    utilization: float  # ρ
    capacity_rps: float  # n · b_max / S(b_max)
    served_rps: float  # min(λ, capacity)
    output_tokens_per_s: float
    ttft_mean_ms: float | None  # None when saturated or infeasible
    ttft_p99_ms: float | None
    tpot_ms: float | None
    slo_ok: bool
    cost_per_hour: float | None
    cost_per_million_output_tokens: float | None


@dataclass
class InferenceAnalysis:
    recommended: ConfigResult | None  # None → no config meets the SLO at demand
    best_effort: ConfigResult | None  # highest capacity, shown when none qualifies
    baseline: ConfigResult | None  # tp=1, n=N (all replicas), when feasible
    configs: list[ConfigResult]
    workload: InferenceWorkload
    gpu_budget: int


def _infeasible(tp: int, instances: int, b_max: int) -> ConfigResult:
    return ConfigResult(
        tp=tp,
        instances=instances,
        strategy=strategy_for(tp, instances),
        gpus=tp * instances,
        feasible=False,
        saturated=False,
        near_saturation=False,
        max_batch=b_max,
        effective_batch=0.0,
        utilization=0.0,
        capacity_rps=0.0,
        served_rps=0.0,
        output_tokens_per_s=0.0,
        ttft_mean_ms=None,
        ttft_p99_ms=None,
        tpot_ms=None,
        slo_ok=False,
        cost_per_hour=None,
        cost_per_million_output_tokens=None,
    )


def evaluate_config(
    tp: int,
    instances: int,
    workload: InferenceWorkload,
    model: ModelSpec,
    gpu: GpuSpec,
    engine: EngineSpec | None = None,
    params: EngineParams | None = None,
    ttft_slo_ms: float | None = None,
    tpot_slo_ms: float | None = None,
    cost_per_gpu_hour: float | None = None,
) -> ConfigResult:
    """Evaluate one ``(tp, n)`` configuration — the ``infer-what-if`` primitive.

    Fail-closed on out-of-domain input: ``tp`` must be a supported degree no
    larger than ``gpus_per_node`` and ``tp · n`` must fit the GPU budget.
    """
    engine = engine or EngineSpec()
    params = _validate_engine_params(params or DEFAULT_ENGINE_PARAMS)
    _require_int(tp, "tp", 1, max(SUPPORTED_TP_DEGREES))
    if tp not in SUPPORTED_TP_DEGREES:
        raise InferenceDomainError(
            f"tp must be one of {SUPPORTED_TP_DEGREES}, got {tp}"
        )
    if tp > gpu.gpus_per_node:
        raise InferenceDomainError(
            f"tp={tp} exceeds gpus_per_node={gpu.gpus_per_node}; "
            "cross-node tensor parallelism is out of scope (ADR-0012)"
        )
    _require_int(instances, "instances", 1, MAX_GPUS)
    if tp * instances > gpu.count:
        raise InferenceDomainError(
            f"tp·instances = {tp * instances} exceeds the GPU budget {gpu.count}"
        )
    if ttft_slo_ms is not None:
        _require_positive(ttft_slo_ms, "ttft_slo_ms", 3_600_000.0)
    if tpot_slo_ms is not None:
        _require_positive(tpot_slo_ms, "tpot_slo_ms", 3_600_000.0)
    if cost_per_gpu_hour is not None:
        _require_positive(cost_per_gpu_hour, "cost_per_gpu_hour", 1e4)

    b_max = max_batch(workload, model, gpu, engine, tp)
    if b_max < 1:
        return _infeasible(tp, instances, b_max)

    strategy = strategy_for(tp, instances)
    lam_i = workload.requests_per_second / instances
    o = workload.output_tokens
    t_pre = prefill_s(workload, tp, strategy, params)

    # S(b) = s0 + s1·b (affine): Little's law b = λ_i·S(b) has a closed form.
    t_step_0 = decode_step_s(0.0, workload, model, gpu, tp, strategy, params)
    t_step_1 = decode_step_s(1.0, workload, model, gpu, tp, strategy, params)
    s0 = t_pre + o * t_step_0
    s1 = o * (t_step_1 - t_step_0)

    service_at_max = s0 + s1 * b_max
    capacity_i = b_max / service_at_max
    capacity = capacity_i * instances

    denom = 1.0 - lam_i * s1
    b_eff = lam_i * s0 / denom if denom > 0.0 else math.inf
    saturated = b_eff >= b_max or lam_i >= capacity_i
    cost_per_hour = (
        cost_per_gpu_hour * tp * instances if cost_per_gpu_hour is not None else None
    )

    if saturated:
        served = capacity
        tokens_per_s = served * o
        return ConfigResult(
            tp=tp,
            instances=instances,
            strategy=strategy,
            gpus=tp * instances,
            feasible=True,
            saturated=True,
            near_saturation=True,
            max_batch=b_max,
            effective_batch=float(b_max),
            utilization=1.0,
            capacity_rps=round(capacity, 4),
            served_rps=round(served, 4),
            output_tokens_per_s=round(tokens_per_s, 2),
            ttft_mean_ms=None,
            ttft_p99_ms=None,
            tpot_ms=round(
                decode_step_s(b_max, workload, model, gpu, tp, strategy, params) * 1e3,
                3,
            ),
            slo_ok=False,
            cost_per_hour=cost_per_hour,
            cost_per_million_output_tokens=_cost_per_mtok(cost_per_hour, tokens_per_s),
        )

    service = s0 + s1 * b_eff
    mu = 1.0 / service
    c_wait = erlang_c(b_max, lam_i / mu)
    excess = b_max * mu - lam_i
    wait_mean = c_wait / excess
    wait_p99 = math.log(100.0 * c_wait) / excess if c_wait > 0.01 else 0.0
    ttft_mean = (t_pre + wait_mean) * 1e3
    ttft_p99 = (t_pre + wait_p99) * 1e3
    tpot = decode_step_s(b_eff, workload, model, gpu, tp, strategy, params) * 1e3
    tokens_per_s = workload.requests_per_second * o
    utilization = lam_i / capacity_i
    slo_ok = (ttft_slo_ms is None or ttft_p99 <= ttft_slo_ms) and (
        tpot_slo_ms is None or tpot <= tpot_slo_ms
    )
    return ConfigResult(
        tp=tp,
        instances=instances,
        strategy=strategy,
        gpus=tp * instances,
        feasible=True,
        saturated=False,
        near_saturation=utilization > NEAR_SATURATION_RHO,
        max_batch=b_max,
        effective_batch=round(b_eff, 3),
        utilization=round(utilization, 4),
        capacity_rps=round(capacity, 4),
        served_rps=round(workload.requests_per_second, 4),
        output_tokens_per_s=round(tokens_per_s, 2),
        ttft_mean_ms=round(ttft_mean, 3),
        ttft_p99_ms=round(ttft_p99, 3),
        tpot_ms=round(tpot, 3),
        slo_ok=slo_ok,
        cost_per_hour=cost_per_hour,
        cost_per_million_output_tokens=_cost_per_mtok(cost_per_hour, tokens_per_s),
    )


def _cost_per_mtok(cost_per_hour: float | None, tokens_per_s: float) -> float | None:
    """Cost per million *output* tokens (prompt tokens are not counted)."""
    if cost_per_hour is None or tokens_per_s <= 0.0:
        return None
    return round(cost_per_hour / (tokens_per_s * 3600.0) * 1e6, 6)


def _qualifies(c: ConfigResult) -> bool:
    return c.feasible and not c.saturated and not c.near_saturation and c.slo_ok


def analyze_inference(
    workload: InferenceWorkload,
    model: ModelSpec,
    gpu: GpuSpec,
    engine: EngineSpec | None = None,
    params: EngineParams | None = None,
    ttft_slo_ms: float | None = None,
    tpot_slo_ms: float | None = None,
    cost_per_gpu_hour: float | None = None,
) -> InferenceAnalysis:
    """Sweep every ``(tp, n)`` with ``tp · n ≤ N`` and recommend one.

    Objective (ADR-0012): among feasible configurations below
    :data:`NEAR_SATURATION_RHO` meeting
    both SLOs at the given demand, the **fewest GPUs**; ties broken by lowest
    TPOT, then lowest TTFT p99. When nothing qualifies, ``recommended`` is
    ``None`` and ``best_effort`` is the highest-capacity feasible config.
    """
    engine = engine or EngineSpec()
    configs: list[ConfigResult] = []
    for tp in sorted(set(engine.tp_degrees)):
        if tp > gpu.gpus_per_node or tp > gpu.count:
            continue
        for n in range(1, gpu.count // tp + 1):
            configs.append(
                evaluate_config(
                    tp,
                    n,
                    workload,
                    model,
                    gpu,
                    engine,
                    params,
                    ttft_slo_ms,
                    tpot_slo_ms,
                    cost_per_gpu_hour,
                )
            )

    qualifying = [c for c in configs if _qualifies(c)]
    recommended = (
        min(qualifying, key=lambda c: (c.gpus, c.tpot_ms, c.ttft_p99_ms))
        if qualifying
        else None
    )
    feasible = [c for c in configs if c.feasible]
    best_effort = (
        max(feasible, key=lambda c: (c.capacity_rps, -c.gpus)) if feasible else None
    )
    baseline = next(
        (c for c in configs if c.tp == 1 and c.instances == gpu.count and c.feasible),
        None,
    )
    return InferenceAnalysis(
        recommended=recommended,
        best_effort=best_effort,
        baseline=baseline,
        configs=configs,
        workload=workload,
        gpu_budget=gpu.count,
    )


def representative_configs(analysis: InferenceAnalysis) -> list[ConfigResult]:
    """One row per tp degree: the fewest-GPU qualifying config, else the largest n.

    This is the cross-layer view: the same budget spent at each tp degree.
    """
    rows: list[ConfigResult] = []
    for tp in sorted({c.tp for c in analysis.configs}):
        at_tp = [c for c in analysis.configs if c.tp == tp]
        ok = [c for c in at_tp if _qualifies(c)]
        rows.append(
            min(ok, key=lambda c: c.gpus)
            if ok
            else max(at_tp, key=lambda c: c.instances)
        )
    return rows
