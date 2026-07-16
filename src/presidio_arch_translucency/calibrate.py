"""
Analytical model calibration -- v0.7.0.

Fits the architectural-translucency per-replica capacity model to a handful of
observed ``(rps, latency_ms, replicas)`` points using
``scipy.optimize.curve_fit``, then persists the fitted parameters so
`pat analyze` stops warning and uses workload-specific defaults.

Calibration model
-----------------
At a replica count chosen to serve demand, the system saturates when

    rps ~= concurrency x (1000 / latency_ms) x replicas x (1 - beta*ln(replicas))

where ``concurrency`` (kappa) is the per-replica async in-flight factor and
``beta`` is the coordination overhead that erodes efficiency as replicas grow.
These are exactly the parameters `pat analyze` consumes via the calibrated-model
file, so a fit here directly tunes the recommendation.

This module is intentionally Docker-free (analytical mode only): observations
come from APM, load tests, or prior ``pat demo`` output.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import warnings
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from presidio_arch_translucency.model_config import (
    DEFAULT_CONCURRENCY,
    DEFAULT_LAYER_NAME,
    GLOBAL_MODEL_RELPATH,
)

#: Calibration-commitment schema tag written into each committed fit record
#: (v0.19.0). A version tag so the re-hash rule can evolve without silently
#: mis-verifying an older commitment.
CALIBRATION_COMMITMENT_SCHEMA = "presidio-hardened/calibration-commitment@1"

#: Key under which the commitment digest and its metadata live in a fit record.
COMMITMENT_KEY = "calibration_commitment"

#: Training-calibration commitment schema (v0.23.0, L-TR-1). A distinct schema
#: tag from the serving commitment so a serving verifier never re-hashes a
#: training record (and vice-versa); the digest key is the same ``COMMITMENT_KEY``.
TRAINING_CALIBRATION_COMMITMENT_SCHEMA = (
    "presidio-hardened/training-calibration-commitment@1"
)
TRAINING_COMMITMENT_KEY = COMMITMENT_KEY

# Default coordination overhead used when a single observation cannot constrain
# beta (one point, two free parameters).
_DEFAULT_BETA: float = 0.02

# Calibration input bounds mirror the public CLI workload envelope while
# leaving room for large benchmark fleets and whole-system power readings.
CALIBRATION_RPS_MAX: float = 1_000_000.0
CALIBRATION_LATENCY_MS_MAX: float = 300_000.0
CALIBRATION_REPLICAS_MAX: int = 10_000
CALIBRATION_WATTS_MAX: float = 1_000_000.0
MODEL_FILE_MAX_BYTES: int = 10 * 1024 * 1024

# Fitted-energy bounds are part of the persisted-record trust contract. Keep
# these public so record consumers validate with the exact same limits.
ENERGY_IDLE_W_MAX: float = 10_000.0
ENERGY_DYN_J_PER_REQ_MAX: float = 1_000.0
ENERGY_BETA_MAX: float = 0.45
_MAX_DESIGN_CONDITION: float = 1.0e10


class CalibrationError(ValueError):
    """Raised when an observation string is malformed or a fit cannot run."""


@dataclass(frozen=True)
class Observation:
    """One measured operating point."""

    rps: float
    latency_ms: float
    replicas: int


@dataclass
class CalibrationResult:
    """Fitted parameters plus per-point predictions and fit quality."""

    concurrency: float  # kappa
    overhead_beta: float  # beta
    r_squared: float
    rmse: float
    observations: list[Observation]
    predictions: list[float]
    residuals: list[float]


@dataclass(frozen=True)
class EnergyObservation:
    """One measured energy operating point (v0.20.0).

    ``watts`` is the *total system* power draw at that operating point — the
    quantity a wattmeter / RAPL / cloud power API reports — not a per-replica
    figure; the fit separates the standing (idle) part from the per-request
    (dynamic) part.
    """

    rps: float
    latency_ms: float
    replicas: int
    watts: float


@dataclass
class EnergyCalibrationResult:
    """Fitted energy parameters plus per-point predictions and fit quality.

    Mirrors :class:`CalibrationResult`. ``energy_idle_w`` is the fitted standing
    power per replica (P_idle), ``energy_dyn_j_per_req`` the dynamic joules per
    request (e_dyn), and ``energy_beta`` (β_E) the ln-δ coordination overhead.
    """

    energy_idle_w: float  # P_idle
    energy_dyn_j_per_req: float  # e_dyn
    energy_beta: float  # beta_E
    r_squared: float
    rmse: float
    observations: list[EnergyObservation]
    predictions: list[float]
    residuals: list[float]


def _validate_observation_values(
    rps: float, latency_ms: float, replicas: int, *, label: str
) -> None:
    if not (math.isfinite(rps) and math.isfinite(latency_ms)):
        raise CalibrationError(
            f"{label} requires finite rps and latency_ms (NaN/inf rejected)."
        )
    if not (
        0 < rps <= CALIBRATION_RPS_MAX
        and 0 < latency_ms <= CALIBRATION_LATENCY_MS_MAX
        and 0 < replicas <= CALIBRATION_REPLICAS_MAX
    ):
        raise CalibrationError(
            f"{label} requires 0 < rps <= {CALIBRATION_RPS_MAX:g}, "
            f"0 < latency_ms <= {CALIBRATION_LATENCY_MS_MAX:g}, and "
            f"0 < replicas <= {CALIBRATION_REPLICAS_MAX}."
        )


def _validate_energy_observation_values(
    point: EnergyObservation, *, label: str
) -> None:
    _validate_observation_values(
        point.rps, point.latency_ms, point.replicas, label=label
    )
    if not math.isfinite(point.watts):
        raise CalibrationError(f"{label} requires finite watts (NaN/inf rejected).")
    if not 0 < point.watts <= CALIBRATION_WATTS_MAX:
        raise CalibrationError(
            f"{label} requires 0 < watts <= {CALIBRATION_WATTS_MAX:g}."
        )


def parse_observation(raw: str) -> Observation:
    """
    Parse a ``rps:latency_ms:replicas`` triple (e.g. ``300:80:5``).

    Raises CalibrationError on malformed input -- note the values are bounded so
    a stray negative or zero cannot poison the fit.
    """
    parts = raw.split(":")
    if len(parts) != 3:
        raise CalibrationError(
            f"Observation {raw!r} must be 'rps:latency_ms:replicas' (e.g. 300:80:5)."
        )
    try:
        rps = float(parts[0])
        latency_ms = float(parts[1])
        replicas = int(parts[2])
    except ValueError as exc:
        raise CalibrationError(f"Observation {raw!r} has non-numeric fields.") from exc
    _validate_observation_values(
        rps, latency_ms, replicas, label=f"Observation {raw!r}"
    )
    return Observation(rps=rps, latency_ms=latency_ms, replicas=replicas)


def predict_rps(latency_ms: float, replicas: float, concurrency: float, beta: float):
    """Model-predicted saturating rps for one operating point (vectorisable)."""
    import numpy as np  # noqa: PLC0415

    eff = 1.0 - beta * np.log(np.maximum(replicas, 1.0))
    return concurrency * (1000.0 / latency_ms) * replicas * eff


def fit_calibration(observations: list[Observation]) -> CalibrationResult:
    """
    Fit ``concurrency`` (kappa) and ``overhead_beta`` (beta) to *observations*.

    Uses ``scipy.optimize.curve_fit`` with bounded parameters.  With a single
    observation beta is fixed at its default and kappa solved directly (a
    1-point fit cannot constrain two parameters).
    """
    if not observations:
        raise CalibrationError("At least one observation is required to calibrate.")
    for index, observation in enumerate(observations, start=1):
        _validate_observation_values(
            observation.rps,
            observation.latency_ms,
            observation.replicas,
            label=f"Observation {index}",
        )

    import numpy as np  # noqa: PLC0415
    from scipy.optimize import curve_fit  # noqa: PLC0415

    latency = np.array([o.latency_ms for o in observations], dtype=float)
    replicas = np.array([float(o.replicas) for o in observations], dtype=float)
    rps = np.array([o.rps for o in observations], dtype=float)

    def _model(x, concurrency, beta):
        lat, rep = x
        return predict_rps(lat, rep, concurrency, beta)

    if len(observations) >= 2:
        popt, _ = curve_fit(
            _model,
            (latency, replicas),
            rps,
            p0=[DEFAULT_CONCURRENCY, _DEFAULT_BETA],
            bounds=([0.1, 0.0], [1000.0, 0.45]),
            maxfev=10000,
        )
        kappa, beta = float(popt[0]), float(popt[1])
    else:
        # One point: hold beta at the default and solve kappa exactly.
        beta = _DEFAULT_BETA
        eff = 1.0 - beta * math.log(max(replicas[0], 1.0))
        kappa = float(rps[0] / ((1000.0 / latency[0]) * replicas[0] * eff))

    preds = predict_rps(latency, replicas, kappa, beta)
    residuals = rps - preds

    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((rps - np.mean(rps)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    rmse = float(np.sqrt(np.mean(residuals**2)))

    return CalibrationResult(
        concurrency=kappa,
        overhead_beta=beta,
        r_squared=r_squared,
        rmse=rmse,
        observations=list(observations),
        predictions=[float(p) for p in preds],
        residuals=[float(r) for r in residuals],
    )


#: Placeholder latency for store-sourced energy fits. The energy model is
#: latency-free (:func:`predict_watts` uses only replicas / rps / watts), but
#: :class:`EnergyObservation` carries a latency field for parity with the
#: ``--energy-observation`` quad and its shared validator requires ``> 0``.
#: Measured-energy store rows have no latency, so this fixed non-zero sentinel
#: satisfies the validator without ever entering the fit math.
ENERGY_STORE_FIT_LATENCY_MS: float = 1.0


def energy_observations_from_store(rows: list) -> list[EnergyObservation]:
    """Map measured-energy store rows to energy-fit inputs (v0.21.0).

    Each *row* is an ``observe.EnergyObservation`` (a chained, measured watt).
    Only ``rps`` / ``replicas`` / ``watts`` drive the energy fit; latency is
    supplied as :data:`ENERGY_STORE_FIT_LATENCY_MS` purely to satisfy the shared
    validator (the energy model does not use it). The resulting objects feed
    :func:`fit_energy_calibration` unchanged, so a store-sourced fit produces a
    record byte-identical in shape to a ``--energy-observation`` fit — the
    commitment binds it exactly the same way.

    Numeric coercion is wrapped: a store row whose ``rps`` / ``replicas`` /
    ``watts`` is non-numeric (e.g. a TEXT value in the DB) raises a clear
    :class:`CalibrationError` rather than surfacing a bare ``ValueError``
    traceback from ``float()`` / ``int()`` (P3).
    """
    mapped: list[EnergyObservation] = []
    for row in rows:
        try:
            point = EnergyObservation(
                rps=float(row.rps),
                latency_ms=ENERGY_STORE_FIT_LATENCY_MS,
                replicas=int(row.replicas),
                watts=float(row.watts),
            )
        except (TypeError, ValueError) as exc:
            raise CalibrationError(
                "energy store row has non-numeric rps/replicas/watts "
                f"(rps={row.rps!r}, replicas={row.replicas!r}, watts={row.watts!r})"
            ) from exc
        mapped.append(point)
    return mapped


def parse_energy_observation(raw: str) -> EnergyObservation:
    """
    Parse an ``rps:latency_ms:replicas:watts`` quad (e.g. ``300:80:5:420``).

    ``watts`` is the *total system* power at that operating point. Raises
    CalibrationError on malformed input; every field is bounded positive so a
    stray zero/negative cannot poison the energy fit (same discipline as
    :func:`parse_observation`).
    """
    parts = raw.split(":")
    if len(parts) != 4:
        raise CalibrationError(
            f"Energy observation {raw!r} must be "
            "'rps:latency_ms:replicas:watts' (e.g. 300:80:5:420)."
        )
    try:
        rps = float(parts[0])
        latency_ms = float(parts[1])
        replicas = int(parts[2])
        watts = float(parts[3])
    except ValueError as exc:
        raise CalibrationError(
            f"Energy observation {raw!r} has non-numeric fields."
        ) from exc
    point = EnergyObservation(
        rps=rps, latency_ms=latency_ms, replicas=replicas, watts=watts
    )
    _validate_energy_observation_values(point, label=f"Energy observation {raw!r}")
    return point


def predict_watts(
    replicas: float, rps: float, p_idle: float, e_dyn: float, beta: float
):
    """Model-predicted total watts for one operating point (vectorisable).

    ``W = P_idle·δ + e_dyn·rps·(1 + β_E·ln δ)`` — the standing draw scales with
    the replica count, the dynamic draw with served load plus a ln-δ term.
    """
    import numpy as np  # noqa: PLC0415

    coordination = 1.0 + beta * np.log(np.maximum(replicas, 1.0))
    return p_idle * replicas + e_dyn * rps * coordination


def fit_energy_calibration(
    points: list[EnergyObservation],
) -> EnergyCalibrationResult:
    """
    Fit ``(P_idle, e_dyn, β_E)`` to measured energy *points*.

    Separating standing from dynamic power needs variation in the replica count:

    * **≥4 unique, identifiable points at ≥2 distinct δ** — fit all three
      parameters with a bounded
      ``scipy.optimize.curve_fit`` (P_idle ∈ [0, 10000] W, e_dyn ∈ [0, 1000]
      J/req, β_E ∈ [0, 0.45]).
    * **2–3 unique points at distinct δ** — the observations cannot estimate
      β_E with covariance, so β_E is held at its default and a two-parameter
      linear least-squares fit solves (P_idle, e_dyn).
    * **1 point (or all δ equal)** — CalibrationError: a single replica count
      cannot separate the standing floor from the per-request cost.
    """
    if not points:
        raise CalibrationError("At least one energy observation is required.")
    for index, point in enumerate(points, start=1):
        _validate_energy_observation_values(point, label=f"Energy observation {index}")
    if len(points) == 1:
        raise CalibrationError(
            "energy calibration needs at least two observations at different "
            "replica counts to separate standing from dynamic power"
        )
    if len({p.replicas for p in points}) < 2:
        raise CalibrationError(
            "energy calibration needs observations at two or more distinct "
            "replica counts to separate standing from dynamic power"
        )
    operating_points = {(p.rps, p.latency_ms, p.replicas, p.watts) for p in points}
    if len(operating_points) != len(points):
        raise CalibrationError(
            "energy calibration requires unique operating points; duplicate "
            "observations do not add identifying information"
        )

    import numpy as np  # noqa: PLC0415

    replicas = np.array([float(p.replicas) for p in points], dtype=float)
    rps = np.array([p.rps for p in points], dtype=float)
    watts = np.array([p.watts for p in points], dtype=float)

    if len(points) <= 3:
        # With fewer than four points covariance for a three-parameter fit is
        # undefined. Hold β_E at the default and solve the two power terms.
        beta = _DEFAULT_BETA
        coeff = np.column_stack(
            (
                replicas,
                rps * (1.0 + beta * np.log(np.maximum(replicas, 1.0))),
            )
        )
        if (
            np.linalg.matrix_rank(coeff) < 2
            or np.linalg.cond(coeff) > _MAX_DESIGN_CONDITION
        ):
            raise CalibrationError(
                "energy observations are degenerate; cannot solve for "
                "standing and dynamic power"
            )
        solution, *_ = np.linalg.lstsq(coeff, watts, rcond=None)
        p_idle, e_dyn = float(solution[0]), float(solution[1])
        if p_idle < 0 or e_dyn < 0:
            raise CalibrationError(
                "energy observations are inconsistent with the model "
                "(solving gives negative standing or dynamic power); supply "
                "more points or re-check the watt readings"
            )
    else:
        from scipy.optimize import OptimizeWarning, curve_fit  # noqa: PLC0415

        # Identifiability of W = a·δ + b·rps + c·rps·lnδ is necessary for the
        # nonlinear (P_idle, e_dyn, β_E) parameterisation too. Normalize columns
        # before conditioning so units do not dominate the test.
        design = np.column_stack(
            (replicas, rps, rps * np.log(np.maximum(replicas, 1.0)))
        )
        norms = np.linalg.norm(design, axis=0)
        if np.any(norms == 0):
            raise CalibrationError("energy observations do not identify all parameters")
        normalized_design = design / norms
        if (
            np.linalg.matrix_rank(normalized_design) < 3
            or np.linalg.cond(normalized_design) > _MAX_DESIGN_CONDITION
        ):
            raise CalibrationError(
                "energy observations are rank-deficient or ill-conditioned; "
                "vary both load and replica count"
            )

        def _model(x, p_idle, e_dyn, beta):
            rep, load = x
            return predict_watts(rep, load, p_idle, e_dyn, beta)

        p0 = [
            min(float(np.min(watts)) * 0.5, ENERGY_IDLE_W_MAX * (1.0 - 1e-9)),
            min(
                float(np.max(watts)) / float(np.max(rps)),
                ENERGY_DYN_J_PER_REQ_MAX * (1.0 - 1e-9),
            ),
            _DEFAULT_BETA,
        ]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", OptimizeWarning)
                popt, _ = curve_fit(
                    _model,
                    (replicas, rps),
                    watts,
                    p0=p0,
                    bounds=(
                        [0.0, 0.0, 0.0],
                        [ENERGY_IDLE_W_MAX, ENERGY_DYN_J_PER_REQ_MAX, ENERGY_BETA_MAX],
                    ),
                    maxfev=10000,
                )
        except (OptimizeWarning, RuntimeError, TypeError, ValueError) as exc:
            raise CalibrationError(
                "energy calibration optimizer could not produce an identifiable fit"
            ) from exc
        p_idle, e_dyn, beta = float(popt[0]), float(popt[1]), float(popt[2])

    if not all(math.isfinite(value) for value in (p_idle, e_dyn, beta)):
        raise CalibrationError("energy calibration produced non-finite parameters")

    preds = predict_watts(replicas, rps, p_idle, e_dyn, beta)
    residuals = watts - preds
    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((watts - np.mean(watts)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    rmse = float(np.sqrt(np.mean(residuals**2)))

    return EnergyCalibrationResult(
        energy_idle_w=p_idle,
        energy_dyn_j_per_req=e_dyn,
        energy_beta=beta,
        r_squared=r_squared,
        rmse=rmse,
        observations=list(points),
        predictions=[float(p) for p in preds],
        residuals=[float(r) for r in residuals],
    )


def global_model_path() -> Path:
    """Resolve ``~/.pat/model.json`` (the global calibrated-model store)."""
    return Path.home() / GLOBAL_MODEL_RELPATH[0] / GLOBAL_MODEL_RELPATH[1]


def _num_str(value: float | int) -> str:
    """Shortest round-trip decimal string for a fitted/observed value.

    The evidence family's canonical profile rejects bare floats (they are not
    portable across encoders); α/β/κ, R²/RMSE and the observed rps/latency are
    naturally floats, so the commitment encodes each as ``repr(float(...))`` --
    the shortest string that round-trips to the same IEEE-754 double -- keeping
    the digest deterministic and lossless. This mirrors the observation-chain
    string-decimal encoding in ``observe.py``.
    """
    return repr(float(value))


def _committed_content(
    result: CalibrationResult,
    energy: EnergyCalibrationResult | None = None,
) -> dict[str, object]:
    """The exact calibration inputs+outputs bound by the commitment digest.

    Inputs: the observation set used (rps/latency as round-trip decimals,
    replicas as ints). Outputs: the fitted per-layer α/β (β = ``overhead_beta``,
    κ = ``concurrency``) and the fit metadata (R², RMSE, point count). This is
    what ``pat analyze`` re-hashes from the model file to detect tampering.

    Energy fields (v0.20.0) are folded in **only when an energy fit is present**:
    when ``energy`` is ``None`` the returned content is byte-for-byte identical
    to the v0.19 scheme, so every commitment written before v0.20 still verifies
    ``ok`` under v0.20 code. When present, the fitted P_idle/e_dyn/β_E, the
    energy fit metadata, and the energy observation set join the committed bytes.
    """
    content: dict[str, object] = {
        "schema": CALIBRATION_COMMITMENT_SCHEMA,
        "concurrency": _num_str(result.concurrency),
        "overhead_beta": _num_str(result.overhead_beta),
        "r_squared": _num_str(result.r_squared),
        "rmse": _num_str(result.rmse),
        "observation_count": len(result.observations),
        "observations": [
            [_num_str(o.rps), _num_str(o.latency_ms), int(o.replicas)]
            for o in result.observations
        ],
    }
    if energy is not None:
        content["energy_idle_w"] = _num_str(energy.energy_idle_w)
        content["energy_dyn_j_per_req"] = _num_str(energy.energy_dyn_j_per_req)
        content["energy_beta"] = _num_str(energy.energy_beta)
        content["energy_r_squared"] = _num_str(energy.r_squared)
        content["energy_rmse"] = _num_str(energy.rmse)
        content["energy_observation_count"] = len(energy.observations)
        content["energy_observations"] = [
            [
                _num_str(o.rps),
                _num_str(o.latency_ms),
                int(o.replicas),
                _num_str(o.watts),
            ]
            for o in energy.observations
        ]
    return content


def _canonical_bytes(payload: object) -> bytes:
    """Family canonical JSON (sorted keys, tight separators); floats already
    string-encoded upstream so the bytes stay deterministic."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def commitment_digest(
    result: CalibrationResult,
    energy: EnergyCalibrationResult | None = None,
) -> str:
    """SHA-256 over the canonical calibration inputs+outputs (lowercase hex).

    When *energy* is given the digest binds the energy fit too; when it is
    ``None`` the digest is identical to the v0.19 throughput-only commitment.
    """
    return hashlib.sha256(
        _canonical_bytes(_committed_content(result, energy))
    ).hexdigest()


def _digest_from_record(record: dict) -> str:
    """Re-derive the commitment digest from a *stored* fit record.

    Reconstructs the committed content from the persisted parameters so a
    consumer can detect any post-calibration edit to concurrency / overhead_beta
    / fit metadata / observation set without re-running the fit.
    """
    observations = record.get("observations")
    if not isinstance(observations, list):
        raise CalibrationError("fit record has no observation set to re-hash")
    content: dict[str, object] = {
        "schema": CALIBRATION_COMMITMENT_SCHEMA,
        "concurrency": _num_str(record["concurrency"]),
        "overhead_beta": _num_str(record["overhead_beta"]),
        "r_squared": _num_str(record["r_squared"]),
        "rmse": _num_str(record["rmse"]),
        "observation_count": len(observations),
        "observations": [
            [_num_str(row[0]), _num_str(row[1]), int(row[2])] for row in observations
        ],
    }
    # Energy fields join the re-hash ONLY when the record carries an energy fit
    # (v0.20.0). A record without ``energy_idle_w`` re-hashes to the exact v0.19
    # bytes, so pre-v0.20 commitments keep verifying under v0.20 code.
    if "energy_idle_w" in record:
        energy_observations = record.get("energy_observations")
        if not isinstance(energy_observations, list):
            raise CalibrationError(
                "energy-bearing fit record has no energy observation set to re-hash"
            )
        content["energy_idle_w"] = _num_str(record["energy_idle_w"])
        content["energy_dyn_j_per_req"] = _num_str(record["energy_dyn_j_per_req"])
        content["energy_beta"] = _num_str(record["energy_beta"])
        content["energy_r_squared"] = _num_str(record["energy_r_squared"])
        content["energy_rmse"] = _num_str(record["energy_rmse"])
        content["energy_observation_count"] = len(energy_observations)
        content["energy_observations"] = [
            [_num_str(row[0]), _num_str(row[1]), int(row[2]), _num_str(row[3])]
            for row in energy_observations
        ]
    return hashlib.sha256(_canonical_bytes(content)).hexdigest()


def commitment_of(record: object) -> str | None:
    """Return the stored commitment digest of a fit record, or ``None``.

    ``None`` means the record predates commitments (a legacy fit) -- consumers
    report it as uncommitted rather than rejecting it.
    """
    if not isinstance(record, dict):
        return None
    commitment = record.get(COMMITMENT_KEY)
    if (
        isinstance(commitment, dict)
        and commitment.get("schema") == CALIBRATION_COMMITMENT_SCHEMA
    ):
        digest = commitment.get("digest")
        if (
            isinstance(digest, str)
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
        ):
            return digest
    return None


def verify_commitment(record: object) -> bool:
    """Re-hash a stored fit record and compare to its embedded commitment.

    Returns True when the record carries a commitment and the persisted
    parameters re-hash to it (untampered). Returns False when the commitment is
    present but the parameters no longer match it (tampered) -- the fail-closed
    signal. Raises nothing; a *missing* commitment is a separate, legacy case
    (use :func:`commitment_of` to distinguish uncommitted from tampered).
    """
    stored = commitment_of(record)
    if stored is None:
        return False
    try:
        return _digest_from_record(record) == stored  # type: ignore[arg-type]
    except (CalibrationError, KeyError, TypeError, ValueError, IndexError):
        return False


# ---------------------------------------------------------------------------
# Training-calibration commitments (v0.23.0, L-TR-1)
#
# A distinct commitment schema for the ``training[<strategy>]`` fit records that
# ``pat train-calibrate`` writes. The digest binds the fitted α/β/baseline, the
# fit metadata, the per-run observation set, and — conditional on presence — the
# energy aggregate, following the exact _num_str string-decimal discipline of
# the serving commitment. The result-based digest (``…_from_fields``) and the
# record-based re-hash (``_digest_from_training_record``) build the SAME
# canonical content so they must agree; keeping the shared builder here is the
# single source of truth. train_calibrate.py imports the write path from here.
# ---------------------------------------------------------------------------


def _training_committed_content(
    *,
    strategy: str,
    overhead_alpha: float,
    overhead_beta: float,
    baseline_samples_per_second: float,
    r_squared: float,
    rmse: float,
    calibrated_at: str,
    microbatches: int,
    runs: list,
    mean_power_w: float | int | None,
    watts_per_device: float | int | None,
) -> dict[str, object]:
    """Canonical committed content for a training fit (all numerics string-decimal).

    ``runs`` rows are ``[degree, samples_per_second, duration_s, power_or_None]``.
    Energy keys join **only** when ``mean_power_w`` is present, so a power-free
    record re-hashes byte-identically to the no-energy scheme (ADR-0011).
    """
    content: dict[str, object] = {
        "schema": TRAINING_CALIBRATION_COMMITMENT_SCHEMA,
        "strategy": strategy,
        "overhead_alpha": _num_str(overhead_alpha),
        "overhead_beta": _num_str(overhead_beta),
        "baseline_samples_per_second": _num_str(baseline_samples_per_second),
        "r_squared": _num_str(r_squared),
        "rmse": _num_str(rmse),
        "calibrated_at": calibrated_at,
        "microbatches": int(microbatches),
        "run_count": len(runs),
        "runs": [
            [
                int(row[0]),
                _num_str(row[1]),
                _num_str(row[2]),
                (None if row[3] is None else _num_str(row[3])),
            ]
            for row in runs
        ],
    }
    if mean_power_w is not None:
        content["mean_power_w"] = _num_str(mean_power_w)
        content["watts_per_device"] = _num_str(watts_per_device)
    return content


def training_commitment_digest_from_fields(
    *,
    strategy: str,
    overhead_alpha: float,
    overhead_beta: float,
    baseline_samples_per_second: float,
    r_squared: float,
    rmse: float,
    calibrated_at: str,
    microbatches: int,
    runs: list,
    mean_power_w: float | int | None = None,
    watts_per_device: float | int | None = None,
) -> str:
    """SHA-256 over the canonical committed training content (lowercase hex)."""
    return hashlib.sha256(
        _canonical_bytes(
            _training_committed_content(
                strategy=strategy,
                overhead_alpha=overhead_alpha,
                overhead_beta=overhead_beta,
                baseline_samples_per_second=baseline_samples_per_second,
                r_squared=r_squared,
                rmse=rmse,
                calibrated_at=calibrated_at,
                microbatches=microbatches,
                runs=runs,
                mean_power_w=mean_power_w,
                watts_per_device=watts_per_device,
            )
        )
    ).hexdigest()


def _digest_from_training_record(record: dict) -> str:
    """Re-derive a training commitment digest from a *stored* training record.

    Reconstructs the committed content from the persisted fields so a consumer
    can detect any post-calibration edit (α/β/baseline/fit metadata/runs/energy)
    without re-running the fit. Energy keys join the re-hash only when the record
    carries ``mean_power_w`` — a power-free record re-hashes to the no-energy
    bytes.
    """
    runs = record.get("runs")
    if not isinstance(runs, list):
        raise CalibrationError("training fit record has no run set to re-hash")
    mean_power = record["mean_power_w"] if "mean_power_w" in record else None
    watts_per_device = record["watts_per_device"] if "mean_power_w" in record else None
    return training_commitment_digest_from_fields(
        strategy=record["strategy"],
        overhead_alpha=record["overhead_alpha"],
        overhead_beta=record["overhead_beta"],
        baseline_samples_per_second=record["baseline_samples_per_second"],
        r_squared=record["r_squared"],
        rmse=record["rmse"],
        calibrated_at=record["calibrated_at"],
        microbatches=record["microbatches"],
        runs=runs,
        mean_power_w=mean_power,
        watts_per_device=watts_per_device,
    )


def training_commitment_of(record: object) -> str | None:
    """Return a training record's stored commitment digest, or ``None`` (legacy).

    ``None`` means the record carries no *training* commitment — either a
    hand-written / pre-v0.23 ``training[<strategy>]`` section, or a malformed /
    wrong-schema commitment (reported as uncommitted, distinguished from tamper
    by :func:`verify_training_commitment`).
    """
    if not isinstance(record, dict):
        return None
    commitment = record.get(TRAINING_COMMITMENT_KEY)
    if (
        isinstance(commitment, dict)
        and commitment.get("schema") == TRAINING_CALIBRATION_COMMITMENT_SCHEMA
    ):
        digest = commitment.get("digest")
        if (
            isinstance(digest, str)
            and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
        ):
            return digest
    return None


def verify_training_commitment(record: object) -> bool:
    """Re-hash a stored training record and compare to its embedded commitment.

    True when the record carries a training commitment and re-hashes to it;
    False when present-but-mismatched (the fail-closed tamper signal). A missing
    commitment is the separate legacy case (use :func:`training_commitment_of`).
    """
    stored = training_commitment_of(record)
    if stored is None:
        return False
    try:
        return _digest_from_training_record(record) == stored  # type: ignore[arg-type]
    except (CalibrationError, KeyError, TypeError, ValueError, IndexError):
        return False


def training_commitment_status(record: object) -> str:
    """Classify a training record's commitment: ``ok`` / ``tampered`` / ``legacy``.

    ``legacy`` = no training commitment stored (hand-written or pre-v0.23);
    reported honestly, never rejected. ``tampered`` = a commitment is present but
    the stored parameters do not re-hash to it (includes a wrong-schema or
    malformed-digest commitment). ``ok`` = present and matches.
    """
    if not isinstance(record, dict) or TRAINING_COMMITMENT_KEY not in record:
        return "legacy"
    if training_commitment_of(record) is None:
        return "tampered"
    return "ok" if verify_training_commitment(record) else "tampered"


def _fit_record(
    result: CalibrationResult,
    energy: EnergyCalibrationResult | None = None,
) -> dict:
    """Serialise a fit to the on-disk record shape (`pat analyze` reads these).

    A NEW calibration always writes the commitment (v0.19.0): the digest binds
    the fitted α/β and the observation set that produced them, so a consumer can
    detect a post-calibration edit and fail closed. Legacy records written
    before commitments simply lack the key and are reported as uncommitted.

    When *energy* is present (v0.20.0) the fitted P_idle/e_dyn/β_E, energy fit
    metadata, and energy observation set are written into the SAME record, and
    the commitment digest binds them alongside the throughput fit. Omitting
    ``energy`` reproduces the exact v0.19 record + commitment.
    """
    record: dict = {
        "concurrency": result.concurrency,
        "overhead_beta": result.overhead_beta,
        "r_squared": result.r_squared,
        "rmse": result.rmse,
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "observations": [
            [o.rps, o.latency_ms, o.replicas] for o in result.observations
        ],
    }
    if energy is not None:
        record["energy_idle_w"] = energy.energy_idle_w
        record["energy_dyn_j_per_req"] = energy.energy_dyn_j_per_req
        record["energy_beta"] = energy.energy_beta
        record["energy_r_squared"] = energy.r_squared
        record["energy_rmse"] = energy.rmse
        record["energy_observations"] = [
            [o.rps, o.latency_ms, o.replicas, o.watts] for o in energy.observations
        ]
    record[COMMITMENT_KEY] = {
        "schema": CALIBRATION_COMMITMENT_SCHEMA,
        "digest": commitment_digest(result, energy),
    }
    return record


def _read_existing_model(path: Path, *, strict: bool = False) -> dict:
    """Return the current global model file as a dict.

    Serving calibration retains the historical ``{}`` fallback for a malformed
    file. Surgical callers may pass ``strict=True`` to prevent an unrelated,
    damaged model from being silently replaced.
    """
    try:
        if strict:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags)
            except FileNotFoundError:
                return {}
            with os.fdopen(fd, "rb") as fh:
                info = os.fstat(fh.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise CalibrationError("existing model must be a regular file")
                raw = fh.read(MODEL_FILE_MAX_BYTES + 1)
            if len(raw) > MODEL_FILE_MAX_BYTES:
                raise CalibrationError(
                    f"existing model exceeds {MODEL_FILE_MAX_BYTES} bytes"
                )
            data = json.loads(raw.decode("utf-8", errors="strict"))
            if not isinstance(data, dict):
                raise CalibrationError("existing model root must be a JSON object")
            return data
        if path.is_symlink():
            return {}
        if path.is_file():
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except CalibrationError:
        raise
    except (OSError, ValueError) as exc:
        if strict:
            raise CalibrationError(f"existing model could not be read: {exc}") from exc
        return {}
    return {}


def _prepare_model_path(path: Path) -> None:
    if path.parent.is_symlink():
        raise CalibrationError("model directory must not be a symbolic link")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise CalibrationError("model directory must be a real directory")
    if path.is_symlink():
        raise CalibrationError("model path must not be a symbolic link")
    with suppress(OSError):
        path.parent.chmod(0o700)


def _write_private_json(path: Path, payload: dict) -> None:
    """Atomically replace *path* with owner-only canonical JSON."""
    if path.is_symlink():
        raise CalibrationError("model path must not be a symbolic link")
    encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
        temporary = None
        with suppress(OSError):
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            with suppress(OSError):
                os.unlink(temporary)


def write_model_file(
    result: CalibrationResult,
    layer: str | None = None,
    energy: EnergyCalibrationResult | None = None,
) -> Path:
    """
    Persist *result* to ``~/.pat/model.json`` (creating ``~/.pat/`` as needed)
    and return the path.  The ``concurrency`` key is what `pat analyze` reads.

    When *layer* is ``None`` or the reserved ``"default"``, the fit is written to
    the top-level (global pooled) parameters -- the v0.7.0/v0.8.0 behaviour.  When
    *layer* names a service layer, the fit is upserted into
    ``model["layers"][layer]`` (v0.9.0); the global parameters and every other
    layer are preserved untouched.

    An optional *energy* fit (v0.20.0) is written into the SAME record as the
    throughput fit — an energy calibration therefore always accompanies a
    throughput fit and their joint commitment binds both observation sets.
    """
    path = global_model_path()
    _prepare_model_path(path)
    record = _fit_record(result, energy)

    if layer is None or layer == DEFAULT_LAYER_NAME:
        # Global write: keep any previously-fitted per-layer records intact.
        existing = _read_existing_model(path)
        payload = record
        if isinstance(existing.get("layers"), dict):
            payload["layers"] = existing["layers"]
    else:
        # Per-layer upsert: preserve global params and other layers.
        payload = _read_existing_model(path)
        layers = payload.get("layers")
        if not isinstance(layers, dict):
            layers = {}
        layers[layer] = record
        payload["layers"] = layers

    _write_private_json(path, payload)
    return path
