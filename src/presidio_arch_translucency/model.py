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

import json
import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

from presidio_arch_translucency.model_config import (
    DEFAULT_CONCURRENCY,
    DEFAULT_LAYER_NAME,
    GLOBAL_MODEL_RELPATH,
)

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


ALL_REPLICATION_LAYERS: Final[tuple[ReplicationLayer, ...]] = (
    ReplicationLayer.CONTAINER,
    ReplicationLayer.POD,
    ReplicationLayer.DEPLOYMENT,
    ReplicationLayer.NODE,
)


# ---------------------------------------------------------------------------
# Per-replica capacity calibration (v0.7.0)
# ---------------------------------------------------------------------------
# A single replica's serving capacity is modelled as
#
#     per_replica_capacity ≈ concurrency × (1000 / avg_latency_ms)   [rps]
#
# The original model assumed ``concurrency = 1`` — i.e. a single in-flight
# request per replica (Little's Law for a serial worker).  At 80 ms latency
# that is only ~12 rps/replica, which drove `pat analyze` to recommend ~64
# replicas for the 500 rps / 80 ms reference workload (see the 2026-04-20
# dogfood notes in PRESIDIO-REQ.md).  Real async Python services (FastAPI,
# aiohttp, …) keep many requests in flight per worker, so per-replica capacity
# is far higher.  ``DEFAULT_CONCURRENCY = 8`` yields ~100 rps/replica at 80 ms,
# placing the reference workload at a realistic 4–8 replicas.
# Validity envelope the default parameters were calibrated against.  Outside
# this range (or for non-async workloads) the CLI warns and suggests
# `pat calibrate`.
REFERENCE_RPS_RANGE: Final[tuple[float, float]] = (50.0, 2000.0)
REFERENCE_LATENCY_RANGE_MS: Final[tuple[float, float]] = (10.0, 250.0)

# Fitted-parameter persistence (written by `pat calibrate`, v0.7.0).
PROJECT_MODEL_FILENAME: Final[str] = ".pat-model.json"


def _model_search_paths() -> list[Path]:
    """Calibrated-model locations, project-local first then the global store."""
    return [
        Path.cwd() / PROJECT_MODEL_FILENAME,
        Path.home() / GLOBAL_MODEL_RELPATH[0] / GLOBAL_MODEL_RELPATH[1],
    ]


def load_calibrated_model() -> dict | None:
    """
    Return fitted model parameters from ``.pat-model.json`` (cwd) or
    ``~/.pat/model.json``, or ``None`` when neither exists / is readable.

    Project-local parameters take precedence over the global store.
    """
    for path in _model_search_paths():
        try:
            if path.is_file():
                with path.open(encoding="utf-8") as fh:
                    return json.load(fh)
        except (OSError, ValueError):
            continue
    return None


def model_is_calibrated() -> bool:
    """True when a calibrated `.pat-model.json` / `~/.pat/model.json` exists."""
    return load_calibrated_model() is not None


# ---------------------------------------------------------------------------
# Calibration commitments (v0.19.0) -- bind fitted α/β to the observations
# ---------------------------------------------------------------------------


class CalibrationTamperError(ValueError):
    """Raised when a committed fit record no longer re-hashes to its commitment.

    Fail-closed: a consumer that finds a present-but-mismatched commitment must
    refuse to act on the parameters rather than silently using tampered α/β.
    """


def commitment_status(record: object) -> str:
    """Classify a fit record's commitment: ``ok`` / ``tampered`` / ``legacy``.

    ``legacy`` = no commitment stored (a fit written before commitments existed);
    reported honestly, never rejected. ``tampered`` = commitment present but the
    stored parameters do not re-hash to it. ``ok`` = present and matches.
    """
    from presidio_arch_translucency.calibrate import (  # noqa: PLC0415
        COMMITMENT_KEY,
        commitment_of,
        verify_commitment,
    )

    if not isinstance(record, dict) or COMMITMENT_KEY not in record:
        return "legacy"
    if commitment_of(record) is None:
        return "tampered"
    return "ok" if verify_commitment(record) else "tampered"


def active_fit_record(layer: str | None = None) -> dict | None:
    """Return the fit record `pat analyze` would use for *layer*, or ``None``.

    Mirrors :func:`resolve_concurrency`'s selection: a named per-layer fit when
    present, else the global top-level fit. Used to surface / verify the
    commitment that actually drives a recommendation.
    """
    model = load_calibrated_model()
    if not isinstance(model, dict):
        return None
    if layer is not None and layer != DEFAULT_LAYER_NAME:
        layers = model.get("layers")
        if isinstance(layers, dict) and isinstance(layers.get(layer), dict):
            return layers[layer]
    return model if "concurrency" in model else None


def resolve_calibration_commitment(layer: str | None = None) -> dict:
    """Resolve the calibration-commitment status for the active fit (fail-closed).

    Returns ``{"status": ..., "digest": ...}`` describing the commitment on the
    fit record `pat analyze` uses for *layer*. ``status`` is one of ``ok``,
    ``legacy``, or ``uncalibrated`` (no model file / no usable fit). Raises
    :class:`CalibrationTamperError` when the active record carries a commitment
    that its stored parameters no longer match — the tamper signal every model
    consumer honours before acting.
    """
    record = active_fit_record(layer)
    if record is None:
        return {"status": "uncalibrated", "digest": None}

    status = commitment_status(record)
    if status == "tampered":
        from presidio_arch_translucency.calibrate import commitment_of  # noqa: PLC0415

        raise CalibrationTamperError(
            "calibrated model parameters do not match their stored "
            "calibration_commitment "
            f"(expected {commitment_of(record)}); the model file was modified "
            "after calibration. Re-run `pat calibrate` to produce a fresh, "
            "committed fit."
        )
    if status == "legacy":
        return {"status": "legacy", "digest": None}

    from presidio_arch_translucency.calibrate import commitment_of  # noqa: PLC0415

    return {"status": "ok", "digest": commitment_of(record)}


# Reserved layer name meaning "the global/top-level pooled fit" rather than a
# named per-layer fit (v0.9.0).  ``pat calibrate`` with no ``--layer`` (or
# ``--layer default``) writes the top-level parameters; named layers live under
# ``model["layers"][name]``.
def _concurrency_from_record(record: object) -> float | None:
    """Extract a positive ``concurrency`` from a fit record, or ``None``."""
    if not isinstance(record, dict):
        return None
    try:
        value = float(record["concurrency"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def resolve_concurrency(layer: str | None = None) -> float:
    """
    Concurrency from a calibrated model if present, else the default.

    When *layer* names a per-layer fit (``model["layers"][layer]``, v0.9.0) that
    record's ``concurrency`` is preferred; otherwise resolution falls back to the
    global top-level ``concurrency``, then to :data:`DEFAULT_CONCURRENCY`.  The
    reserved name ``"default"`` and ``None`` both mean "use the global fit".
    """
    model = load_calibrated_model()
    if model is None:
        return DEFAULT_CONCURRENCY

    if layer is not None and layer != DEFAULT_LAYER_NAME:
        layers = model.get("layers")
        if isinstance(layers, dict):
            value = _concurrency_from_record(layers.get(layer))
            if value is not None:
                return value

    value = _concurrency_from_record(model)
    if value is not None:
        return value
    return DEFAULT_CONCURRENCY


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


def base_capacity_rps(
    requests_per_second: float,
    avg_latency_ms: float,
    concurrency: float = DEFAULT_CONCURRENCY,
) -> float:
    """Public alias — single-replica capacity estimate (concurrency-aware)."""
    return _base_capacity(requests_per_second, avg_latency_ms, concurrency)


def _base_capacity(
    requests_per_second: float,
    avg_latency_ms: float,
    concurrency: float = DEFAULT_CONCURRENCY,
) -> float:
    """
    Estimate single-replica capacity from observed latency and concurrency.

    A replica serving ``concurrency`` requests in flight at ``avg_latency_ms``
    each sustains ``concurrency × 1000 / avg_latency_ms`` rps (Little's Law for
    a server with ``concurrency`` in-flight slots).  ``concurrency = 1`` is the
    old serial assumption; the async default (8) reflects real FastAPI/aiohttp
    workers.  We still cap relative to observed demand to avoid wild
    extrapolation at very low load.
    """
    littles_capacity = concurrency * 1000.0 / max(avg_latency_ms, 1.0)
    # Assume current utilisation is ~70% (typical production headroom target)
    utilisation_estimate = 0.70
    demand_implied_capacity = requests_per_second / utilisation_estimate
    return min(littles_capacity, demand_implied_capacity)


def analyze(
    requests_per_second: float,
    avg_latency_ms: float,
    current_layer: ReplicationLayer,
    concurrency: float | None = None,
    layer: str | None = None,
) -> AnalysisResult:
    """
    Core architectural translucency analysis.

    For each layer, sweep replication factors δ = 1..max_replicas and find the
    smallest δ that saturates demand (maximises throughput).  ``concurrency``
    sets per-replica capacity; when ``None`` it is resolved from a calibrated
    ``.pat-model.json`` / ``~/.pat/model.json`` — honouring the per-layer fit
    named by ``layer`` when present (v0.9.0) — or falls back to the async
    default.  Returns a full AnalysisResult with a cross-layer recommendation.
    """
    if concurrency is None:
        concurrency = resolve_concurrency(layer)

    base_cap = _base_capacity(requests_per_second, avg_latency_ms, concurrency)
    baseline_rps = min(base_cap, requests_per_second)
    baseline_rt = avg_latency_ms  # single replica, no replication

    layer_results: list[LayerResult] = []

    for layer in ALL_REPLICATION_LAYERS:
        params = LAYER_PARAMS[layer]
        best_delta = 1
        best_tp = baseline_rps
        best_rt = baseline_rt

        for delta in range(1, params.max_replicas + 1):
            tp = throughput(requests_per_second, delta, layer, base_cap)
            rt = response_time_ms(
                requests_per_second, delta, layer, avg_latency_ms, base_cap
            )
            # Optimisation objective: maximise throughput, preferring the
            # FEWEST replicas that achieve it.  Adding replicas past demand
            # saturation only shaves idle-queue latency at extra cost, so we
            # only adopt a larger δ when it strictly improves throughput.
            if tp > best_tp + 1e-9:
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
