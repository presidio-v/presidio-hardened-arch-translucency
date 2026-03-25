"""
Cost-aware replication analysis — v0.4.0.

Adds a cost dimension to the architectural translucency model.  For each
replication layer the module computes:
  - hourly_cost     : replicas × cost-per-replica-hour
  - cost_per_request: hourly cost / (throughput × 3600)
  - roi_score       : throughput-gain-% / cost_per_request  (higher = better ROI)

Default costs (USD/replica-hour) are cloud-typical estimates; callers should
override via CostParams for their actual environment.
"""

from __future__ import annotations

from dataclasses import dataclass

from presidio_arch_translucency.model import ReplicationLayer

# ---------------------------------------------------------------------------
# Cost parameters
# ---------------------------------------------------------------------------

# Approximate USD/replica-hour defaults — container ≈ small Fargate task,
# pod ≈ with kubelet/kube-proxy overhead, deployment ≈ with scheduler churn,
# node ≈ small EC2/GCE instance.
_DEFAULT_COST: dict[ReplicationLayer, float] = {
    ReplicationLayer.CONTAINER: 0.02,
    ReplicationLayer.POD: 0.05,
    ReplicationLayer.DEPLOYMENT: 0.10,
    ReplicationLayer.NODE: 0.50,
}


@dataclass
class CostParams:
    """Per-layer replica costs in USD per replica-hour."""

    cost_per_container_hour: float = 0.02
    cost_per_pod_hour: float = 0.05
    cost_per_deployment_hour: float = 0.10
    cost_per_node_hour: float = 0.50

    def for_layer(self, layer: ReplicationLayer) -> float:
        """Return cost-per-replica-hour for *layer*."""
        return {
            ReplicationLayer.CONTAINER: self.cost_per_container_hour,
            ReplicationLayer.POD: self.cost_per_pod_hour,
            ReplicationLayer.DEPLOYMENT: self.cost_per_deployment_hour,
            ReplicationLayer.NODE: self.cost_per_node_hour,
        }[layer]


# ---------------------------------------------------------------------------
# Core cost functions
# ---------------------------------------------------------------------------


def hourly_cost(layer: ReplicationLayer, replicas: int, params: CostParams) -> float:
    """Total USD/hour for *replicas* at *layer*."""
    return params.for_layer(layer) * replicas


def cost_per_request(
    layer: ReplicationLayer,
    replicas: int,
    throughput_rps: float,
    params: CostParams,
) -> float:
    """
    USD per request served.

    cost_per_request = hourly_cost / (throughput_rps × 3600)
    Returns inf when throughput is zero.
    """
    if throughput_rps <= 0:
        return float("inf")
    return hourly_cost(layer, replicas, params) / (throughput_rps * 3600.0)


def trough_cost_usd(missed_requests: int, cost_per_req: float) -> float:
    """
    Estimated revenue cost of the HPA trough window.

    Proxy metric: missed_requests × cost_per_request.
    Returns 0.0 when cost_per_req is infinite (zero-throughput edge case).
    """
    if cost_per_req == float("inf"):
        return 0.0
    return missed_requests * cost_per_req


# ---------------------------------------------------------------------------
# Per-layer cost result
# ---------------------------------------------------------------------------


@dataclass
class CostResult:
    """Cost analysis for a single replication layer."""

    layer: ReplicationLayer
    replicas: int
    throughput_rps: float
    throughput_gain_pct: float
    response_time_change_pct: float
    hourly_cost_usd: float
    cost_per_request_usd: float
    roi_score: float
    description: str
    is_recommended: bool = False


def build_cost_results(
    analysis_layers: list,  # list[LayerResult] — avoid circular import
    params: CostParams,
) -> list[CostResult]:
    """
    Compute CostResult for every layer in *analysis_layers*.

    *analysis_layers* is the `.layers` attribute of an AnalysisResult.
    The layer with the best ROI score is flagged `is_recommended`.
    """
    results: list[CostResult] = []
    for lr in analysis_layers:
        hc = hourly_cost(lr.layer, lr.optimal_replicas, params)
        cpr = cost_per_request(
            lr.layer, lr.optimal_replicas, lr.estimated_throughput_rps, params
        )
        # ROI: throughput gain per unit cost — avoid division by zero
        roi = lr.throughput_gain_pct / cpr if cpr > 0 and cpr != float("inf") else 0.0
        results.append(
            CostResult(
                layer=lr.layer,
                replicas=lr.optimal_replicas,
                throughput_rps=lr.estimated_throughput_rps,
                throughput_gain_pct=lr.throughput_gain_pct,
                response_time_change_pct=lr.response_time_change_pct,
                hourly_cost_usd=round(hc, 4),
                cost_per_request_usd=round(cpr, 8),
                roi_score=round(roi, 2),
                description=lr.description,
            )
        )

    if results:
        best = max(results, key=lambda r: r.roi_score)
        best.is_recommended = True

    return results
