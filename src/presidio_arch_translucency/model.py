"""
Architectural Translucency simulation model.

Based on the replication performance equations from Stantchev's work:
  - ω(δ) = throughput as a function of replication factor δ at a given layer
  - ι(δ) = intensity factor (load per replica after replication)
  - response_time = 1 / ω(δ)  (normalised units)

Layer overhead coefficients are calibrated for Docker/Kubernetes realities:
  container  — new container: low isolation overhead, fast spin-up
  pod        — Kubernetes Pod: shared network namespace, moderate overhead
  deployment — Deployment/ReplicaSet: scheduler overhead, full K8s overhead
  node       — cluster node: highest isolation, highest spin-up cost
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Final

# ---------------------------------------------------------------------------
# Layer definitions
# ---------------------------------------------------------------------------

VALID_LAYERS: Final[tuple[str, ...]] = ("container", "pod", "deployment", "node")


class ReplicationLayer(str, Enum):
    """Docker/Kubernetes replication layers ordered by overhead."""

    CONTAINER = "container"
    POD = "pod"
    DEPLOYMENT = "deployment"
    NODE = "node"


# ---------------------------------------------------------------------------
# Layer overhead model parameters
# ---------------------------------------------------------------------------
# overhead_alpha  — per-replica fixed overhead fraction (0..1)
# overhead_beta   — scheduling/coordination cost that grows with δ
# max_replicas    — practical upper bound for the layer
# description     — human-readable label
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerParams:
    overhead_alpha: float  # fixed overhead fraction per replica
    overhead_beta: float  # coordination overhead scaling with δ
    max_replicas: int
    description: str


LAYER_PARAMS: Final[dict[ReplicationLayer, LayerParams]] = {
    ReplicationLayer.CONTAINER: LayerParams(
        overhead_alpha=0.02,
        overhead_beta=0.005,
        max_replicas=64,
        description="New Docker container (process-level isolation, shared kernel)",
    ),
    ReplicationLayer.POD: LayerParams(
        overhead_alpha=0.05,
        overhead_beta=0.01,
        max_replicas=32,
        description="Kubernetes Pod (shared network namespace, kubelet overhead)",
    ),
    ReplicationLayer.DEPLOYMENT: LayerParams(
        overhead_alpha=0.10,
        overhead_beta=0.02,
        max_replicas=20,
        description="Kubernetes Deployment/ReplicaSet (scheduler + etcd overhead)",
    ),
    ReplicationLayer.NODE: LayerParams(
        overhead_alpha=0.18,
        overhead_beta=0.04,
        max_replicas=8,
        description="Cluster node (full VM/bare-metal isolation, highest overhead)",
    ),
}

# ---------------------------------------------------------------------------
# Core equations
# ---------------------------------------------------------------------------


def intensity_after_replication(
    requests_per_second: float,
    delta: int,
    layer: ReplicationLayer,
) -> float:
    """
    ι(δ) — load intensity per replica after replication.

    With ideal load balancing the per-replica load is rps/δ.  Overhead (fixed
    per-replica cost + coordination cost growing with ln(δ)) scales with the
    per-replica load so that doubling replicas halves both useful work and
    overhead for each replica:

    ι(δ) = (rps/δ) * (1 + overhead_alpha + overhead_beta * ln(δ))
    """
    params = LAYER_PARAMS[layer]
    per_replica = requests_per_second / delta
    overhead_factor = (
        1.0 + params.overhead_alpha + params.overhead_beta * math.log(max(delta, 1))
    )
    return per_replica * overhead_factor


def throughput(
    requests_per_second: float,
    delta: int,
    layer: ReplicationLayer,
    base_capacity_rps: float,
) -> float:
    """
    ω(δ) — system throughput after replication at the given layer.

    The system can process min(base_capacity_rps * δ * efficiency, demand).
    Efficiency degrades with overhead:

    efficiency(δ) = 1 - overhead_alpha - overhead_beta * ln(δ)

    ω(δ) = min(base_capacity * δ * efficiency(δ), requests_per_second)
    """
    params = LAYER_PARAMS[layer]
    efficiency = max(
        0.0,
        1.0 - params.overhead_alpha - params.overhead_beta * math.log(max(delta, 1)),
    )
    raw_capacity = base_capacity_rps * delta * efficiency
    return min(raw_capacity, requests_per_second)


def response_time_ms(
    requests_per_second: float,
    delta: int,
    layer: ReplicationLayer,
    avg_latency_ms: float,
    base_capacity_rps: float,
) -> float:
    """
    Estimated response time (ms) at the given layer and replication factor.

    Uses a simplified M/M/δ-inspired approximation:
      utilisation ρ = ι(δ) / base_capacity_rps
      response_time = avg_latency_ms / (1 - ρ)   [clamped to 10x baseline]
    """
    params = LAYER_PARAMS[layer]
    intensity = intensity_after_replication(requests_per_second, delta, layer)
    rho = min(intensity / base_capacity_rps, 0.99)
    # Add per-layer coordination latency
    coordination_ms = (
        params.overhead_alpha * avg_latency_ms * math.log(max(delta, 1) + 1)
    )
    base_rt = avg_latency_ms / (1.0 - rho)
    return base_rt + coordination_ms


# ---------------------------------------------------------------------------
# Layer recommendation
# ---------------------------------------------------------------------------


@dataclass
class LayerResult:
    layer: ReplicationLayer
    optimal_replicas: int
    estimated_throughput_rps: float
    estimated_response_time_ms: float
    throughput_gain_pct: float
    response_time_change_pct: float
    description: str


@dataclass
class AnalysisResult:
    recommended_layer: ReplicationLayer
    recommended_replicas: int
    current_layer: ReplicationLayer
    baseline_throughput_rps: float
    baseline_response_time_ms: float
    layers: list[LayerResult]


def base_capacity_rps(requests_per_second: float, avg_latency_ms: float) -> float:
    """Public alias — single-replica capacity estimate via Little's Law."""
    return _base_capacity(requests_per_second, avg_latency_ms)


def _base_capacity(requests_per_second: float, avg_latency_ms: float) -> float:
    """
    Estimate single-replica capacity from observed latency.

    Little's Law: capacity ≈ 1000 / avg_latency_ms  (requests/sec per replica)
    We also cap it relative to observed demand to avoid wild extrapolation.
    """
    littles_capacity = 1000.0 / max(avg_latency_ms, 1.0)
    # Assume current utilisation is ~70% (typical production headroom target)
    utilisation_estimate = 0.70
    demand_implied_capacity = requests_per_second / utilisation_estimate
    return min(littles_capacity, demand_implied_capacity)


def analyze(
    requests_per_second: float,
    avg_latency_ms: float,
    current_layer: ReplicationLayer,
) -> AnalysisResult:
    """
    Core architectural translucency analysis.

    For each layer, sweep replication factors δ = 1..max_replicas and find
    the δ that maximises throughput while minimising response time.
    Returns a full AnalysisResult with a cross-layer recommendation.
    """
    base_cap = _base_capacity(requests_per_second, avg_latency_ms)
    baseline_rps = min(base_cap, requests_per_second)
    baseline_rt = avg_latency_ms  # single replica, no replication

    layer_results: list[LayerResult] = []

    for layer in ReplicationLayer:
        params = LAYER_PARAMS[layer]
        best_delta = 1
        best_tp = baseline_rps
        best_rt = baseline_rt

        for delta in range(1, params.max_replicas + 1):
            tp = throughput(requests_per_second, delta, layer, base_cap)
            rt = response_time_ms(
                requests_per_second, delta, layer, avg_latency_ms, base_cap
            )
            # Optimisation objective: maximise throughput, then minimise RT
            if tp > best_tp or (tp >= best_tp and rt < best_rt):
                best_tp = tp
                best_rt = rt
                best_delta = delta

        tp_gain = (best_tp - baseline_rps) / max(baseline_rps, 1.0) * 100.0
        rt_change = (best_rt - baseline_rt) / max(baseline_rt, 1.0) * 100.0

        layer_results.append(
            LayerResult(
                layer=layer,
                optimal_replicas=best_delta,
                estimated_throughput_rps=round(best_tp, 2),
                estimated_response_time_ms=round(best_rt, 2),
                throughput_gain_pct=round(tp_gain, 1),
                response_time_change_pct=round(rt_change, 1),
                description=params.description,
            )
        )

    # Recommend the layer with the best (throughput_gain - abs(rt_overhead)) score
    def score(r: LayerResult) -> float:
        return r.throughput_gain_pct - max(r.response_time_change_pct, 0.0)

    best = max(layer_results, key=score)

    return AnalysisResult(
        recommended_layer=best.layer,
        recommended_replicas=best.optimal_replicas,
        current_layer=current_layer,
        baseline_throughput_rps=round(baseline_rps, 2),
        baseline_response_time_ms=round(baseline_rt, 2),
        layers=layer_results,
    )
