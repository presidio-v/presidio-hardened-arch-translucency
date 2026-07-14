"""
Architectural Translucency energy model (v0.20.0) — "Model the watt".

Extends the replication model from throughput/latency (see ``model.py``) to the
*energy* an architecture spends serving a workload. The same translucency
question — *the same measure (replication) has different implications at
different layers* — has a sharp energy answer:

  A new **container** on a shared kernel adds almost no standing draw: its
  power is nearly all dynamic, proportional to the requests it serves. A new
  **node** buys the *entire* server idle floor whether or not it does useful
  work — a 2023 two-socket server idles at ~36 % of its peak power
  (SPECpower_ssj2008 fleet trend). That idle-power asymmetry between the cheap
  inner layers and the expensive outer layer *is* the translucency insight for
  energy: replicating at the node layer to shave a latency tail can cost far
  more joules-per-request than the same relief bought a container at a time,
  precisely when the fleet is underutilised and the idle floor dominates.

Parameterisation (mirrors the v0.18 training α/β framing, and ADR-0010's
"parallel structure" precedent: we do **not** add fields to ``LayerParams`` —
its field set may be test-pinned — but define a parallel per-layer table):

  α_E  (``energy_alpha``)  — idle / standing power as a fraction of a replica's
                            peak power. The layer's floor.
  β_E  (``energy_beta``)   — coordination-energy overhead that grows with ln δ:
                            more replicas cost a little extra to keep in step.

The defaults below are **MVP placeholders**, documented as such, pending
measured calibration via ``pat calibrate --energy-observation``. They encode the
published idle-power ratios (Tadesse/Chiasserini/Malandrino 2018 for the
container/pod/deployment bookkeeping deltas; the SPECpower_ssj2008 server idle
trend for the node floor), not a measurement of any particular fleet.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from presidio_arch_translucency.model import (
    ReplicationLayer,
    load_calibrated_model,
    throughput,
)
from presidio_arch_translucency.model_config import DEFAULT_LAYER_NAME

# ---------------------------------------------------------------------------
# Per-layer energy parameters (parallel to LAYER_PARAMS, per ADR-0010)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnergyParams:
    energy_alpha: float  # α_E — idle/standing power fraction of per-replica peak power
    energy_beta: float  # β_E — coordination energy overhead, ln-scaled with δ


#: MVP-placeholder per-layer energy coefficients. These are *placeholders* in
#: the same sense as the v0.18 training α/β defaults: they encode published
#: idle-power ratios, not a measurement of your fleet. Calibrate them from real
#: watt readings with ``pat calibrate --energy-observation`` before trusting the
#: absolute joules; the *relative* container-vs-node story holds regardless.
ENERGY_PARAMS: Final[dict[ReplicationLayer, EnergyParams]] = {
    # A container on a shared kernel adds ~constant ≈2 W standing draw
    # regardless of count (Tadesse/Chiasserini/Malandrino 2018).
    ReplicationLayer.CONTAINER: EnergyParams(energy_alpha=0.02, energy_beta=0.005),
    # Container + kubelet/pause bookkeeping.
    ReplicationLayer.POD: EnergyParams(energy_alpha=0.03, energy_beta=0.010),
    # + scheduler/etcd share.
    ReplicationLayer.DEPLOYMENT: EnergyParams(energy_alpha=0.05, energy_beta=0.020),
    # A new node buys the full server idle floor: fleet idle ≈36 % of peak for
    # 2023 servers (SPECpower_ssj2008 trend).
    ReplicationLayer.NODE: EnergyParams(energy_alpha=0.36, energy_beta=0.030),
}

#: Per-replica peak power (watts). MVP placeholder: a ~700 W peak 2-socket 2023
#: server split across ~48 usable vCPU slices ≈ 15 W per 1-vCPU-class replica
#: slice. Override on the CLI with ``--replica-power-watts``.
DEFAULT_REPLICA_POWER_WATTS: Final[float] = 15.0


# ---------------------------------------------------------------------------
# Core equations (unit-tested)
# ---------------------------------------------------------------------------


def idle_watts_per_replica(energy_alpha: float, replica_power_watts: float) -> float:
    """Standing draw of one replica: the idle floor the layer cannot avoid.

    Derived from the fraction parameterisation — a replica whose peak power is
    ``replica_power_watts`` spends ``α_E`` of it just existing.
    """
    return energy_alpha * replica_power_watts


def dyn_joules_per_request(
    energy_alpha: float,
    replica_power_watts: float,
    base_capacity_rps: float,
) -> float:
    """Dynamic energy cost of one served request (joules).

    The non-idle fraction ``(1 − α_E)`` of a replica's peak power buys its
    request-serving capacity ``base_capacity_rps`` (the same single-replica
    capacity ``analyze`` computes), so each request costs
    ``(1 − α_E)·P_peak / capacity`` joules of dynamic energy.
    """
    return (1.0 - energy_alpha) * replica_power_watts / base_capacity_rps


def power_watts(
    delta: int,
    omega_rps: float,
    idle_w: float,
    dyn_j_per_req: float,
    energy_beta: float,
) -> float:
    """W(δ) — total power (watts) drawn by δ replicas serving ω(δ) requests/s.

    ``W(δ) = idle_w·δ + dyn_j_per_req·ω(δ)·(1 + β_E·ln δ)``. The idle term grows
    linearly with the replica count (every replica pays its floor); the dynamic
    term scales with useful throughput and carries a mild ln-δ coordination
    penalty so that a single replica (δ=1 → ln δ=0) suffers no coordination
    overhead at all.
    """
    coordination = 1.0 + energy_beta * math.log(max(delta, 1))
    return idle_w * delta + dyn_j_per_req * omega_rps * coordination


def joules_per_request(watts: float, omega_rps: float) -> float | None:
    """J/req — energy intensity: watts spent per request served.

    Returns ``None`` (rendered as "—") when throughput collapses to zero, since
    energy-per-request is undefined with no requests to divide by — the honest
    answer, not an infinity that would poison a comparison.
    """
    if omega_rps <= 0:
        return None
    return watts / omega_rps


def eei(tp_ratio: float, jreq_ratio: float) -> float | None:
    """Energy-Efficiency Index — dimensionless throughput-per-energy-cost ratio.

    ``EEI = (ω(δ_best)/ω(1)) / (J·req(δ_best) / J·req(1))``. EEI > 1 means
    replicating at this layer buys *more* throughput than it costs in energy
    intensity (a good trade); EEI < 1 means the joules-per-request got worse
    faster than throughput improved. Returns ``None`` when the intensity ratio
    is undefined (division guard).
    """
    if jreq_ratio <= 0:
        return None
    return tp_ratio / jreq_ratio


# ---------------------------------------------------------------------------
# Parameter resolution (calibrated fit → default, mirrors resolve_concurrency)
# ---------------------------------------------------------------------------


def _energy_fit_from_record(record: object) -> tuple[float, float, float] | None:
    """Extract fitted ``(idle_w, dyn_j_per_req, β_E)`` from a fit record, or None.

    A record carries an energy fit only when ``pat calibrate
    --energy-observation`` wrote one; otherwise the keys are absent and the
    caller falls back per :func:`resolve_energy_params`.
    """
    if not isinstance(record, dict):
        return None
    from presidio_arch_translucency.calibrate import (  # noqa: PLC0415
        ENERGY_BETA_MAX,
        ENERGY_DYN_J_PER_REQ_MAX,
        ENERGY_IDLE_W_MAX,
        verify_commitment,
    )

    # Energy fields were introduced with commitments in v0.20.0. There is no
    # legitimate legacy energy record, so uncommitted/malformed records must
    # fall back to clearly-labelled defaults instead of driving recommendations.
    if not verify_commitment(record):
        return None
    try:
        idle = float(record["energy_idle_w"])
        dyn = float(record["energy_dyn_j_per_req"])
        beta = float(record["energy_beta"])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (idle, dyn, beta)):
        return None
    if not (
        0.0 <= idle <= ENERGY_IDLE_W_MAX
        and 0.0 <= dyn <= ENERGY_DYN_J_PER_REQ_MAX
        and 0.0 <= beta <= ENERGY_BETA_MAX
    ):
        return None
    return (idle, dyn, beta)


def _resolve_energy_fit(
    model_layer: str | None,
) -> tuple[tuple[float, float, float] | None, str]:
    """Resolve the energy fit and the record *scope* it came from.

    Mirrors :func:`resolve_concurrency`'s per-layer-then-global selection and
    returns ``(fit, scope)`` where ``scope`` is ``"layer"`` (a named per-layer
    record supplied the energy fit), ``"global"`` (resolution fell back to the
    top-level record's energy fit), or ``"default"`` (no calibrated energy fit
    applies). Shared by :func:`resolve_energy_params` and
    :func:`resolve_energy_fit_scope` so the two never disagree about which
    record actually drives the energy figures.
    """
    model = load_calibrated_model()
    if isinstance(model, dict):
        if model_layer is not None and model_layer != DEFAULT_LAYER_NAME:
            layers = model.get("layers")
            if isinstance(layers, dict):
                layer_record = layers.get(model_layer)
                fitted = _energy_fit_from_record(layer_record)
                if fitted is not None:
                    return fitted, "layer"
                if isinstance(layer_record, dict) and "energy_idle_w" in layer_record:
                    return None, "layer"
        fitted = _energy_fit_from_record(model)
        if fitted is not None:
            return fitted, "global"
        if "energy_idle_w" in model:
            return None, "global"
    return None, "default"


def resolve_energy_fit_scope(model_layer: str | None = None) -> str:
    """Report which record scope supplies the energy fit for *model_layer*.

    Returns ``"layer"`` | ``"global"`` | ``"default"`` (see
    :func:`_resolve_energy_fit`). Exposed so a caller that gated only the named
    layer's calibration commitment can detect a fall-through to the **global**
    record's energy fit and gate that record's commitment too — the tamper
    signal every model consumer honours before acting.
    """
    return _resolve_energy_fit(model_layer)[1]


def resolve_energy_params(
    layer: ReplicationLayer,
    replica_power_watts: float,
    base_capacity_rps: float,
    model_layer: str | None = None,
) -> tuple[float, float | None, float, str]:
    """Resolve ``(idle_w, dyn_j_per_req, β_E, source)`` for *layer*.

    Prefers a calibrated energy fit when one is present in the active model file
    — mirroring :func:`resolve_concurrency`'s per-layer-then-global resolution:
    a named per-layer fit's energy keys win, else the global fit's, else the
    per-layer MVP defaults. When calibrated, ``dyn_j_per_req`` comes straight
    from the record (no re-derivation); ``source`` is ``"calibrated"``.
    Otherwise the idle floor and dynamic cost are derived from the layer's
    default α_E / β_E and the supplied peak power / capacity; ``source`` is
    ``"default"``. ``dyn_j_per_req`` is ``None`` only when capacity is
    non-positive (nothing to serve, cannot derive a per-request cost).
    """
    fitted, _scope = _resolve_energy_fit(model_layer)
    if fitted is not None:
        idle, dyn, beta = fitted
        return (idle, dyn, beta, "calibrated")

    params = ENERGY_PARAMS[layer]
    idle_w = idle_watts_per_replica(params.energy_alpha, replica_power_watts)
    dyn = (
        dyn_joules_per_request(
            params.energy_alpha, replica_power_watts, base_capacity_rps
        )
        if base_capacity_rps > 0
        else None
    )
    return (idle_w, dyn, params.energy_beta, "default")


# ---------------------------------------------------------------------------
# Per-layer energy summary (consumed by the CLI render layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayerEnergy:
    """Energy picture for one layer at its recommended replication factor."""

    layer: ReplicationLayer
    replicas: int
    watts: float  # W(δ_best)
    joules_per_request: float | None  # J/req at δ_best, None when ω→0
    eei: float | None  # EEI vs δ=1, None when undefined
    source: str  # "calibrated" | "default"


def layer_energy(
    layer: ReplicationLayer,
    delta_best: int,
    requests_per_second: float,
    base_capacity_rps: float,
    replica_power_watts: float,
    model_layer: str | None = None,
) -> LayerEnergy:
    """Energy metrics for *layer* at δ=*delta_best*, relative to its δ=1 baseline.

    Computes W(δ_best), J/req(δ_best) and the EEI (throughput gain vs energy-
    intensity change from δ=1 to δ_best) from the same throughput curve the
    recommendation uses, so the energy columns are consistent with the
    performance columns. Pure function of the analysis inputs — the render layer
    calls it once per layer.
    """
    idle_w, dyn, beta, source = resolve_energy_params(
        layer, replica_power_watts, base_capacity_rps, model_layer
    )
    omega_best = throughput(requests_per_second, delta_best, layer, base_capacity_rps)

    # No derivable dynamic cost (non-positive capacity): report the idle floor
    # only and leave the ratios undefined rather than inventing a J/req.
    if dyn is None:
        return LayerEnergy(
            layer=layer,
            replicas=delta_best,
            watts=idle_w * max(delta_best, 0),
            joules_per_request=None,
            eei=None,
            source=source,
        )

    omega_one = throughput(requests_per_second, 1, layer, base_capacity_rps)
    watts_best = power_watts(delta_best, omega_best, idle_w, dyn, beta)
    watts_one = power_watts(1, omega_one, idle_w, dyn, beta)
    jreq_best = joules_per_request(watts_best, omega_best)
    jreq_one = joules_per_request(watts_one, omega_one)

    eei_value: float | None = None
    if jreq_best is not None and jreq_one is not None and omega_one > 0:
        eei_value = eei(omega_best / omega_one, jreq_best / jreq_one)

    return LayerEnergy(
        layer=layer,
        replicas=delta_best,
        watts=watts_best,
        joules_per_request=jreq_best,
        eei=eei_value,
        source=source,
    )
