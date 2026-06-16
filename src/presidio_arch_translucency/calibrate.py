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

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from presidio_arch_translucency.model import (
    DEFAULT_CONCURRENCY,
    DEFAULT_LAYER_NAME,
    GLOBAL_MODEL_RELPATH,
)

# Default coordination overhead used when a single observation cannot constrain
# beta (one point, two free parameters).
_DEFAULT_BETA: float = 0.02


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
    if rps <= 0 or latency_ms <= 0 or replicas <= 0:
        raise CalibrationError(
            f"Observation {raw!r} requires positive rps, latency_ms, and replicas."
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


def global_model_path() -> Path:
    """Resolve ``~/.pat/model.json`` (the global calibrated-model store)."""
    return Path.home() / GLOBAL_MODEL_RELPATH[0] / GLOBAL_MODEL_RELPATH[1]


def _fit_record(result: CalibrationResult) -> dict:
    """Serialise a fit to the on-disk record shape (`pat analyze` reads these)."""
    return {
        "concurrency": result.concurrency,
        "overhead_beta": result.overhead_beta,
        "r_squared": result.r_squared,
        "rmse": result.rmse,
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "observations": [
            [o.rps, o.latency_ms, o.replicas] for o in result.observations
        ],
    }


def _read_existing_model(path: Path) -> dict:
    """Return the current global model file as a dict, or ``{}`` if absent/bad."""
    try:
        if path.is_file():
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return {}


def _prepare_model_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass


def _write_private_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def write_model_file(result: CalibrationResult, layer: str | None = None) -> Path:
    """
    Persist *result* to ``~/.pat/model.json`` (creating ``~/.pat/`` as needed)
    and return the path.  The ``concurrency`` key is what `pat analyze` reads.

    When *layer* is ``None`` or the reserved ``"default"``, the fit is written to
    the top-level (global pooled) parameters -- the v0.7.0/v0.8.0 behaviour.  When
    *layer* names a service layer, the fit is upserted into
    ``model["layers"][layer]`` (v0.9.0); the global parameters and every other
    layer are preserved untouched.
    """
    path = global_model_path()
    _prepare_model_path(path)
    record = _fit_record(result)

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
