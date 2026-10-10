"""
Inference calibration and validation (ADR-0012, Inference Arc Phase 1).

Fits the inference model's engine parameters from measured vLLM operating
points and stores them as a **committed** named profile under
``model["inference"][<profile>]``. A profile belongs to one (model, GPU)
pair, so consumers use it only when asked by name (``--calibration NAME``),
never implicitly; they fail closed when its commitment no longer re-hashes or
when the hardware they are asked about differs from the profile's.

A point is one steady-state window on a single instance at a fixed tp::

    {"tp": 2, "batch": 24.3, "prompt_tokens": 1000, "output_tokens": 256,
     "tpot_ms": 35.1, "prefill_ms": 60.2, "kv_usage": 0.41}

``batch`` is the mean running batch, ``tpot_ms`` the mean inter-token latency,
``prefill_ms`` (optional) the mean per-request prefill time, measured at low
load, and ``kv_usage`` (optional, 0–1) the mean KV-cache usage. When present,
``kv_usage`` gives the live context per sequence directly (``usage·T/b``)
instead of the ``P + O/2`` estimate.

Decode fit. With ``x = 1/(η·BW)`` and the Phase 0 decode form (α_replica = 0),
each point satisfies, linearly in ``(x, t₀, u = x·W·α_t, v = x·W·β_t)``::

    TPOT_i = x·(W + b_i·c_i·k)/tp_i + t₀ + u·[tp_i > 1] + v·ln tp_i

The linear solution starts a bounded least-squares refinement in natural units
(η, t₀, α_t, β_t). With few tp levels α and β are nearly collinear, so a noisy
point set can push them past physical bounds; the bound then binds and is
reported (``at_bound``) rather than the fit being refused. With only one
distinct tp > 1, β_t is held at its default. Prefill is a one-parameter fit of
``t_pre_i = (1/R_pre)·P_i/(tp_i·eff_i)`` with the fitted α/β.

Bounded claim: η is an *effective* bandwidth (measured inter-token latency
includes chunked-prefill interference), not HBM efficiency. The fit explains
the measured points; ``pat infer-validate`` reports the error on held-out
points, and only held-out *configurations* (other tp, n ≥ 2) test the
crossover prediction.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from presidio_arch_translucency.calibrate import (
    CalibrationError,
    _canonical_bytes,
    _num_str,
    _prepare_model_path,
    _read_existing_model,
    _write_private_json,
    global_model_path,
)
from presidio_arch_translucency.inference import (
    ALPHA_MAX,
    BETA_MAX,
    DEFAULT_ENGINE_PARAMS,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_MAX_NUM_SEQS,
    SUPPORTED_TP_DEGREES,
    EngineParams,
    GpuSpec,
    InferenceDomainError,
    InferenceWorkload,
    ModelSpec,
    ServingStrategy,
    _validate_engine_params,
    decode_step_s,
    kv_tokens_per_instance,
    live_context_tokens,
    prefill_efficiency,
    prefill_s,
)

INFERENCE_COMMITMENT_SCHEMA: Final[str] = (
    "presidio-hardened/inference-calibration-commitment@1"
)
COMMITMENT_KEY: Final[str] = "calibration_commitment"
RECORD_SCHEMA: Final[str] = "presidio-hardened/inference-calibration@1"

MAX_POINTS: Final[int] = 10_000
MAX_POINTS_FILE_BYTES: Final[int] = 5 * 1024 * 1024
MAX_LABEL_LEN: Final[int] = 64
#: Relative tolerance when matching a profile to the hardware being analysed.
HARDWARE_MATCH_TOLERANCE: Final[float] = 0.01
#: Joint coordination bound: keeps prefill efficiency well above its floor
#: across the profile's own tp range.
JOINT_OVERHEAD_MAX: Final[float] = 0.9
#: Slack on the [P, P+O] band a KV-usage-derived live context must fall in
#: (block granularity and prefix sharing move it a little).
KV_CONTEXT_TOLERANCE: Final[float] = 0.1

_POINT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "tp",
        "batch",
        "prompt_tokens",
        "output_tokens",
        "tpot_ms",
        "prefill_ms",
        "kv_usage",
    }
)
_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {"tp", "batch", "prompt_tokens", "output_tokens", "tpot_ms"}
)
_SHORT_KEYS: Final[dict[str, str]] = {
    "prompt": "prompt_tokens",
    "output": "output_tokens",
}
_ETA_MIN: Final[float] = 0.01
_ETA_MAX: Final[float] = 1.0
_T0_MAX_MS: Final[float] = 10_000.0
_GB: Final[float] = 1e9

_PARAM_FIELDS: Final[tuple[str, ...]] = (
    "bandwidth_efficiency",
    "step_overhead_ms",
    "prefill_tokens_per_s_per_gpu",
    "replica_alpha",
    "tensor_alpha",
    "tensor_beta",
)


class InferenceCalibrationError(ValueError):
    """Raised when inference calibration input or fit fails (fail-closed)."""


class InferenceCalibrationTamperError(ValueError):
    """Raised when a stored profile is uncommitted, tampered, or mismatched."""


def _finite(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InferenceCalibrationError(f"{name} must be a number")
    v = float(value)
    if not math.isfinite(v) or not (minimum <= v <= maximum):
        raise InferenceCalibrationError(
            f"{name} must be finite and within [{minimum}, {maximum}], got {v!r}"
        )
    return v


def _label(value: object, name: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not (1 <= len(value) <= MAX_LABEL_LEN)
        or not value.isprintable()
    ):
        raise InferenceCalibrationError(
            f"{name} must be 1–{MAX_LABEL_LEN} printable characters"
        )
    return value


# ---------------------------------------------------------------------------
# Hardware and points
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardwareSpec:
    """What a profile is specific to; every field is bound into the commitment."""

    weights_gb: float
    kv_bytes_per_token: float
    gpu_bandwidth_gbs: float
    gpu_memory_gb: float
    gpu_memory_utilization: float = DEFAULT_GPU_MEMORY_UTILIZATION
    max_num_seqs: int = DEFAULT_MAX_NUM_SEQS
    model_name: str | None = None
    gpu_type: str | None = None
    engine_version: str | None = None

    def __post_init__(self) -> None:
        _finite(self.weights_gb, "weights_gb", 1e-3, 1e5)
        _finite(self.kv_bytes_per_token, "kv_bytes_per_token", 1.0, 1e8)
        _finite(self.gpu_bandwidth_gbs, "gpu_bandwidth_gbs", 1.0, 1e6)
        _finite(self.gpu_memory_gb, "gpu_memory_gb", 1e-3, 1e4)
        _finite(self.gpu_memory_utilization, "gpu_memory_utilization", 0.05, 1.0)
        if isinstance(self.max_num_seqs, bool) or not isinstance(
            self.max_num_seqs, int
        ):
            raise InferenceCalibrationError("max_num_seqs must be an integer")
        if not 1 <= self.max_num_seqs <= 65_536:
            raise InferenceCalibrationError("max_num_seqs must be in [1, 65536]")
        _label(self.model_name, "model_name")
        _label(self.gpu_type, "gpu_type")
        _label(self.engine_version, "engine_version")

    def model_spec(self) -> ModelSpec:
        return ModelSpec(self.weights_gb, self.kv_bytes_per_token)

    def gpu_spec(self, tp: int) -> GpuSpec:
        return GpuSpec(
            count=tp,
            memory_gb=self.gpu_memory_gb,
            bandwidth_gbs=self.gpu_bandwidth_gbs,
            gpus_per_node=max(tp, 1),
            memory_utilization=self.gpu_memory_utilization,
        )

    def as_dict(self) -> dict:
        return {
            "weights_gb": float(self.weights_gb),
            "kv_bytes_per_token": float(self.kv_bytes_per_token),
            "gpu_bandwidth_gbs": float(self.gpu_bandwidth_gbs),
            "gpu_memory_gb": float(self.gpu_memory_gb),
            "gpu_memory_utilization": float(self.gpu_memory_utilization),
            "max_num_seqs": int(self.max_num_seqs),
            "model_name": self.model_name,
            "gpu_type": self.gpu_type,
            "engine_version": self.engine_version,
        }


@dataclass(frozen=True)
class InferencePoint:
    tp: int
    batch: float
    prompt_tokens: float
    output_tokens: float
    tpot_ms: float
    prefill_ms: float | None = None
    kv_usage: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.tp, bool) or not isinstance(self.tp, int):
            raise InferenceCalibrationError("tp must be an integer")
        if self.tp not in SUPPORTED_TP_DEGREES:
            raise InferenceCalibrationError(
                f"tp must be one of {SUPPORTED_TP_DEGREES}, got {self.tp}"
            )
        _finite(self.batch, "batch", 0.0, 65_536.0)
        _finite(self.prompt_tokens, "prompt_tokens", 1.0, 1e7)
        _finite(self.output_tokens, "output_tokens", 1.0, 1e7)
        _finite(self.tpot_ms, "tpot_ms", 1e-3, 1e6)
        if self.prefill_ms is not None:
            _finite(self.prefill_ms, "prefill_ms", 1e-3, 1e7)
        if self.kv_usage is not None:
            _finite(self.kv_usage, "kv_usage", 0.0, 1.0)
            if self.batch <= 0.0:
                raise InferenceCalibrationError("kv_usage needs a batch > 0")

    def as_row(self) -> dict:
        row: dict = {
            "tp": self.tp,
            "batch": float(self.batch),
            "prompt_tokens": float(self.prompt_tokens),
            "output_tokens": float(self.output_tokens),
            "tpot_ms": float(self.tpot_ms),
        }
        if self.prefill_ms is not None:
            row["prefill_ms"] = float(self.prefill_ms)
        if self.kv_usage is not None:
            row["kv_usage"] = float(self.kv_usage)
        return row


def point_from_mapping(data: object) -> InferencePoint:
    """Build a point from a decoded JSON object; unknown keys are rejected."""
    if not isinstance(data, dict):
        raise InferenceCalibrationError("a point must be a JSON object")
    unknown = set(data) - _POINT_KEYS
    if unknown:
        raise InferenceCalibrationError(f"unknown point keys: {sorted(unknown)}")
    missing = _REQUIRED_KEYS - set(data)
    if missing:
        raise InferenceCalibrationError(f"missing point keys: {sorted(missing)}")
    return InferencePoint(
        tp=data["tp"],
        batch=data["batch"],
        prompt_tokens=data["prompt_tokens"],
        output_tokens=data["output_tokens"],
        tpot_ms=data["tpot_ms"],
        prefill_ms=data.get("prefill_ms"),
        kv_usage=data.get("kv_usage"),
    )


def parse_point(raw: str) -> InferencePoint:
    """Parse ``tp=2,batch=24.3,prompt=1000,output=256,tpot_ms=35.1[,...]``."""
    if not isinstance(raw, str) or len(raw) > 512:
        raise InferenceCalibrationError("point must be a string of at most 512 chars")
    data: dict[str, object] = {}
    for part in raw.split(","):
        key, sep, value = part.strip().partition("=")
        key = _SHORT_KEYS.get(key.strip(), key.strip())
        if not sep or not key:
            raise InferenceCalibrationError(f"malformed point field {part!r}")
        if key in data:
            raise InferenceCalibrationError(f"duplicate point field {key!r}")
        try:
            data[key] = int(value) if key == "tp" else float(value)
        except ValueError as exc:
            raise InferenceCalibrationError(f"{key} is not a number") from exc
    return point_from_mapping(data)


def _reject_duplicate_keys(pairs):  # noqa: ANN001, ANN202
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise InferenceCalibrationError("duplicate key in point object")
    return dict(pairs)


def _reject_constant(name: str) -> float:
    raise InferenceCalibrationError(f"non-finite constant {name!r} is not allowed")


def read_regular_file(path: str | Path, max_bytes: int, label: str) -> str:
    """UTF-8 text of a regular, non-symlink file no larger than ``max_bytes``."""
    p = Path(path)
    try:
        if stat.S_ISLNK(p.lstat().st_mode):
            raise InferenceCalibrationError(f"{label} must not be a symbolic link")
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise InferenceCalibrationError(f"{label} is not a regular file")
            raw = fh.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise InferenceCalibrationError(f"{label} exceeds {max_bytes} bytes")
        return raw.decode("utf-8", errors="strict")
    except InferenceCalibrationError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise InferenceCalibrationError(f"{label} could not be read: {exc}") from exc


def load_points_file(path: str | Path) -> list[InferencePoint]:
    """Read JSON-Lines points from a regular, non-symlink file (fail-closed)."""
    text = read_regular_file(path, MAX_POINTS_FILE_BYTES, "points file")
    points: list[InferencePoint] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            data = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_constant,
            )
            points.append(point_from_mapping(data))
        except (ValueError, InferenceCalibrationError) as exc:
            raise InferenceCalibrationError(f"line {number}: {exc}") from exc
        if len(points) > MAX_POINTS:
            raise InferenceCalibrationError(f"more than {MAX_POINTS} points")
    return points


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------


def _strategy(tp: int) -> ServingStrategy:
    # A calibration point is one instance: tp = 1 is a replica, else tensor.
    return ServingStrategy.REPLICA if tp == 1 else ServingStrategy.TENSOR


def _workload(point: InferencePoint) -> InferenceWorkload:
    # λ does not enter the per-step equations; any valid value will do.
    return InferenceWorkload(1.0, point.prompt_tokens, point.output_tokens)


def point_context_tokens(point: InferencePoint, hardware: HardwareSpec) -> float:
    """Live context per sequence: measured ``usage·T/b`` if given, else ``P + O/2``."""
    if point.kv_usage is not None:
        capacity = kv_tokens_per_instance(
            hardware.model_spec(), hardware.gpu_spec(point.tp), point.tp
        )
        if capacity <= 0.0:
            raise InferenceCalibrationError(
                f"tp={point.tp}: weights leave no KV cache on this hardware"
            )
        context = point.kv_usage * capacity / point.batch
        # The prompt is resident for the whole decode and a sequence never
        # exceeds P + O: anything outside that band is a measurement error.
        low = point.prompt_tokens * (1.0 - KV_CONTEXT_TOLERANCE)
        high = (point.prompt_tokens + point.output_tokens) * (
            1.0 + KV_CONTEXT_TOLERANCE
        )
        if not low <= context <= high:
            raise InferenceCalibrationError(
                f"tp={point.tp} b={point.batch:g}: kv_usage {point.kv_usage:g} "
                f"implies {context:.0f} live tokens per sequence, outside "
                f"[P, P+O] = [{point.prompt_tokens:g}, "
                f"{point.prompt_tokens + point.output_tokens:g}]"
            )
        return context
    return live_context_tokens(_workload(point))


@dataclass
class PointPrediction:
    point: InferencePoint
    tpot_ms: float
    prefill_ms: float | None

    @property
    def tpot_error_pct(self) -> float:
        return (self.tpot_ms - self.point.tpot_ms) / self.point.tpot_ms * 100.0

    @property
    def prefill_error_pct(self) -> float | None:
        if self.prefill_ms is None or self.point.prefill_ms is None:
            return None
        return (self.prefill_ms - self.point.prefill_ms) / self.point.prefill_ms * 100.0


def predict_point(
    point: InferencePoint, hardware: HardwareSpec, params: EngineParams
) -> PointPrediction:
    """Predict TPOT (and prefill when measured) for one point under *params*."""
    strategy = _strategy(point.tp)
    workload = _workload(point)
    tpot = decode_step_s(
        point.batch,
        workload,
        hardware.model_spec(),
        hardware.gpu_spec(point.tp),
        point.tp,
        strategy,
        params,
        context_tokens=point_context_tokens(point, hardware),
    )
    prefill = (
        prefill_s(workload, point.tp, strategy, params) * 1e3
        if point.prefill_ms is not None
        else None
    )
    return PointPrediction(point=point, tpot_ms=tpot * 1e3, prefill_ms=prefill)


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


@dataclass
class InferenceCalibrationResult:
    params: EngineParams
    hardware: HardwareSpec
    points: list[InferencePoint]
    fitted: tuple[str, ...]  # parameters estimated from the points
    held: tuple[str, ...]  # parameters left at their defaults
    at_bound: tuple[str, ...]  # fitted, but pinned at a physical bound
    degrees_of_freedom: int  # decode points minus free decode parameters
    r_squared: float | None  # None when degrees_of_freedom < 2
    rmse_ms: float
    predictions: list[PointPrediction] = field(default_factory=list)


def fit_inference_calibration(
    points: list[InferencePoint], hardware: HardwareSpec
) -> InferenceCalibrationResult:
    """Fit η, t₀, α_t, β_t (and R_pre when prefill is measured) from *points*."""
    import numpy as np  # noqa: PLC0415
    from scipy.optimize import least_squares  # noqa: PLC0415

    if not points:
        raise InferenceCalibrationError("at least one point is required")
    single = [p for p in points if p.tp == 1]
    multi = [p for p in points if p.tp > 1]
    if len({p.batch for p in single}) < 2:
        raise InferenceCalibrationError(
            "need ≥2 tp=1 points with distinct mean batch to separate bandwidth "
            "from fixed step cost"
        )
    if not multi:
        raise InferenceCalibrationError("need ≥1 point with tp > 1 to fit α_tensor")
    fit_beta = len({p.tp for p in multi}) >= 2
    free_count = 4 if fit_beta else 3
    if len(points) < free_count + 1:
        raise InferenceCalibrationError(
            f"{free_count} decode parameters need ≥{free_count + 1} points "
            f"(one degree of freedom), got {len(points)}"
        )

    defaults = DEFAULT_ENGINE_PARAMS
    weights = hardware.weights_gb * _GB
    k = hardware.kv_bytes_per_token
    bandwidth = hardware.gpu_bandwidth_gbs * _GB
    beta_default = defaults.tensor_beta
    rows_data = [
        (p.tp, point_context_tokens(p, hardware), p.batch, p.tpot_ms) for p in points
    ]

    # Linear start: TPOT = x·A + t₀ + u·[tp>1] + v·ln tp  (seconds).
    design_rows: list[list[float]] = []
    for tp, context, batch, _ in rows_data:
        a = (weights + batch * context * k) / tp
        flag = 1.0 if tp > 1 else 0.0
        if fit_beta:
            design_rows.append([a, 1.0, flag, math.log(tp)])
        else:
            design_rows.append([a + weights * beta_default * math.log(tp), 1.0, flag])
    design = np.array(design_rows, dtype=float)
    target = np.array([row[3] / 1e3 for row in rows_data], dtype=float)
    scale = np.abs(design).max(axis=0)
    scale[scale == 0.0] = 1.0
    if np.linalg.matrix_rank(design / scale) < design.shape[1]:
        raise InferenceCalibrationError("points do not identify the parameters")
    coeffs, *_ = np.linalg.lstsq(design, target, rcond=None)
    x = float(coeffs[0])
    if not math.isfinite(x) or x <= 0.0:
        raise InferenceCalibrationError(
            "fitted TPOT does not grow with bytes read; the decode form does not "
            "fit these points"
        )
    start = [
        1.0 / (x * bandwidth),
        float(coeffs[1]) * 1e3,
        float(coeffs[2]) / (x * weights),
        float(coeffs[3]) / (x * weights) if fit_beta else beta_default,
    ]

    names = ("bandwidth_efficiency", "step_overhead_ms", "tensor_alpha", "tensor_beta")
    lower = [_ETA_MIN, 0.0, 0.0, 0.0]
    upper = [_ETA_MAX, _T0_MAX_MS, ALPHA_MAX - 1e-9, BETA_MAX - 1e-9]
    free = [0, 1, 2, 3] if fit_beta else [0, 1, 2]

    def theta_of(free_values) -> list[float]:  # noqa: ANN001
        theta = [0.0, 0.0, 0.0, beta_default]
        for i, value in zip(free, free_values, strict=True):
            theta[i] = float(value)
        return theta

    def residuals(free_values):  # noqa: ANN001, ANN202
        eta, t0_ms, alpha_t, beta_t = theta_of(free_values)
        out = []
        for tp, context, batch, tpot_ms in rows_data:
            coord = 0.0 if tp == 1 else alpha_t + beta_t * math.log(tp)
            read = (weights + batch * context * k) / tp + coord * weights
            out.append(read / (eta * bandwidth) * 1e3 + t0_ms - tpot_ms)
        return out

    solution = least_squares(
        residuals,
        [min(max(start[i], lower[i]), upper[i]) for i in free],
        bounds=([lower[i] for i in free], [upper[i] for i in free]),
        method="trf",
        x_scale=[[0.1, 1.0, 0.01, 0.01][i] for i in free],
    )
    if not solution.success:
        raise InferenceCalibrationError(
            f"decode fit did not converge: {solution.message}"
        )
    eta, t0_ms, alpha_t, beta_t = theta_of(solution.x)
    bound_tolerance = (1e-4, 1e-3, 1e-4, 1e-4)  # η, t₀ ms, α, β
    theta = theta_of(solution.x)
    at_bound = tuple(
        names[i]
        for i in free
        if theta[i] - lower[i] <= bound_tolerance[i]
        or upper[i] - theta[i] <= bound_tolerance[i]
    )
    if "bandwidth_efficiency" in at_bound:
        raise InferenceCalibrationError(
            f"fitted bandwidth efficiency η = {eta:.3f} hit its bound "
            f"[{_ETA_MIN}, {_ETA_MAX}]; check --gpu-bandwidth-gbs and the points"
        )
    tp_max = max(p.tp for p in points)
    if alpha_t + beta_t * math.log(tp_max) >= JOINT_OVERHEAD_MAX:
        raise InferenceCalibrationError(
            f"fitted coordination α + β·ln {tp_max} = "
            f"{alpha_t + beta_t * math.log(tp_max):.3f} ≥ {JOINT_OVERHEAD_MAX}; "
            "the points imply tensor parallelism barely works"
        )

    params = replace(
        defaults,
        bandwidth_efficiency=eta,
        step_overhead_ms=t0_ms,
        tensor_alpha=alpha_t,
        tensor_beta=beta_t,
    )
    fitted = list(names[i] for i in free)
    held = ["replica_alpha"] + ([] if fit_beta else ["tensor_beta"])

    with_prefill = [p for p in points if p.prefill_ms is not None]
    if with_prefill:
        z = [
            p.prompt_tokens / (p.tp * prefill_efficiency(_strategy(p.tp), p.tp, params))
            for p in with_prefill
        ]
        t = [p.prefill_ms / 1e3 for p in with_prefill]
        inverse_rate = sum(a * b for a, b in zip(z, t, strict=True)) / sum(
            a * a for a in z
        )
        rate = 1.0 / inverse_rate
        if not math.isfinite(rate) or not 1.0 <= rate <= 1e9:
            raise InferenceCalibrationError("prefill points imply an implausible rate")
        params = replace(params, prefill_tokens_per_s_per_gpu=rate)
        fitted.append("prefill_tokens_per_s_per_gpu")
    else:
        held.append("prefill_tokens_per_s_per_gpu")

    predictions = [predict_point(p, hardware, params) for p in points]
    observed = [p.tpot_ms for p in points]
    predicted = [pr.tpot_ms for pr in predictions]
    ss_res = sum((o - q) ** 2 for o, q in zip(observed, predicted, strict=True))
    dof = len(points) - len(free)
    r_squared: float | None = None
    if dof >= 2:
        mean = sum(observed) / len(observed)
        ss_tot = sum((o - mean) ** 2 for o in observed)
        r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0.0 else 1.0
    return InferenceCalibrationResult(
        params=params,
        hardware=hardware,
        points=list(points),
        fitted=tuple(fitted),
        held=tuple(held),
        at_bound=at_bound,
        degrees_of_freedom=dof,
        r_squared=r_squared,
        rmse_ms=math.sqrt(ss_res / len(points)),
        predictions=predictions,
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@dataclass
class ValidationReport:
    predictions: list[PointPrediction]
    tpot_mape_pct: float
    tpot_max_error_pct: float
    prefill_mape_pct: float | None
    prefill_max_error_pct: float | None
    skipped_folds: int = 0  # leave-one-out folds that could not be fitted
    degraded_folds: int = 0  # folds that fitted fewer parameters; excluded


def _report(
    predictions: list[PointPrediction], skipped: int = 0, degraded: int = 0
) -> ValidationReport:
    if not predictions:
        raise InferenceCalibrationError("no points could be validated")
    tpot = [abs(p.tpot_error_pct) for p in predictions]
    prefill = [abs(e) for p in predictions if (e := p.prefill_error_pct) is not None]
    return ValidationReport(
        predictions=predictions,
        tpot_mape_pct=sum(tpot) / len(tpot),
        tpot_max_error_pct=max(tpot),
        prefill_mape_pct=sum(prefill) / len(prefill) if prefill else None,
        prefill_max_error_pct=max(prefill) if prefill else None,
        skipped_folds=skipped,
        degraded_folds=degraded,
    )


def validate_holdout(
    points: list[InferencePoint], hardware: HardwareSpec, params: EngineParams
) -> ValidationReport:
    """Prediction error of a fitted profile on held-out points."""
    return _report([predict_point(p, hardware, params) for p in points])


def validate_leave_one_out(
    points: list[InferencePoint], hardware: HardwareSpec
) -> ValidationReport:
    """Refit without each point in turn and predict it.

    Only folds that fit every parameter the full point set fits are scored: a
    fold that falls back to a default (e.g. β when its only second tp level
    was the held-out point) would mix placeholder error into the figure. Such
    folds are counted as ``degraded``; folds that cannot be fitted at all as
    ``skipped``.
    """
    full = set(fit_inference_calibration(points, hardware).fitted)
    predictions: list[PointPrediction] = []
    skipped = degraded = 0
    for index, point in enumerate(points):
        rest = points[:index] + points[index + 1 :]
        try:
            fit = fit_inference_calibration(rest, hardware)
        except InferenceCalibrationError:
            skipped += 1
            continue
        if not full <= set(fit.fitted):
            degraded += 1
            continue
        predictions.append(predict_point(point, hardware, fit.params))
    return _report(predictions, skipped, degraded)


# ---------------------------------------------------------------------------
# Committed persistence
# ---------------------------------------------------------------------------


def _canonicalize(value: object) -> object:
    """Floats → shortest round-trip decimal strings (family discipline)."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InferenceCalibrationError("non-finite number in profile")
        return _num_str(value)
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonicalize(item) for item in value]
    raise InferenceCalibrationError(f"unsupported value type {type(value).__name__}")


def commitment_digest(record: dict) -> str:
    """SHA-256 over the canonical profile content, excluding the commitment."""
    content = {key: value for key, value in record.items() if key != COMMITMENT_KEY}
    envelope = {"schema": INFERENCE_COMMITMENT_SCHEMA, "record": content}
    return hashlib.sha256(_canonical_bytes(_canonicalize(envelope))).hexdigest()


def build_profile_record(name: str, result: InferenceCalibrationResult) -> dict:
    """Serialise a fit; the commitment binds every field, points included."""
    record: dict = {
        "schema": RECORD_SCHEMA,
        "profile": name,
        "calibrated_at": datetime.now(timezone.utc).isoformat(),
        "hardware": result.hardware.as_dict(),
        "params": {name: float(getattr(result.params, name)) for name in _PARAM_FIELDS},
        "fitted": list(result.fitted),
        "held": list(result.held),
        "at_bound": list(result.at_bound),
        "degrees_of_freedom": result.degrees_of_freedom,
        "r_squared": result.r_squared,
        "rmse_ms": float(result.rmse_ms),
        "points": [p.as_row() for p in result.points],
    }
    # Hash what will be read back: a JSON round trip fixes int/float shapes.
    record = json.loads(json.dumps(record))
    record[COMMITMENT_KEY] = {
        "schema": INFERENCE_COMMITMENT_SCHEMA,
        "digest": commitment_digest(record),
    }
    return record


def validate_profile_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not (1 <= len(name) <= MAX_LABEL_LEN)
        or name.startswith(".")
        or not all(ch.isascii() and (ch.isalnum() or ch in "-_.") for ch in name)
    ):
        raise InferenceCalibrationError(
            "profile name must be 1–64 ASCII letters, digits, '-', '_' or '.', "
            "not starting with '.'"
        )
    return name


def write_inference_profile(name: str, result: InferenceCalibrationResult) -> Path:
    """Upsert ``model["inference"][name]``; every other section is preserved."""
    name = validate_profile_name(name)
    path = global_model_path()
    try:
        _prepare_model_path(path)
        payload = _read_existing_model(path, strict=True)
        section = payload.get("inference")
        if section is not None and not isinstance(section, dict):
            raise InferenceCalibrationError(
                "existing model inference section must be a JSON object"
            )
        section = dict(section or {})
        section[name] = build_profile_record(name, result)
        payload["inference"] = section
        _write_private_json(path, payload)
    except (CalibrationError, OSError) as exc:
        raise InferenceCalibrationError(f"profile could not be stored: {exc}") from exc
    return path


@dataclass(frozen=True)
class InferenceProfile:
    name: str
    params: EngineParams
    hardware: HardwareSpec
    points: tuple[InferencePoint, ...]
    digest: str


def load_inference_profile(name: str) -> InferenceProfile:
    """Load and verify a committed profile (fail-closed on absence or tamper)."""
    name = validate_profile_name(name)
    # Profiles live in the global store only, read with the same strict reader
    # the write path uses. A project-local .pat-model.json (ADR-0005) must not
    # shadow them, nor ship a self-certifying profile inside a repository.
    try:
        model = _read_existing_model(global_model_path(), strict=True)
    except CalibrationError as exc:
        raise InferenceCalibrationError(f"model store unreadable: {exc}") from exc
    section = model.get("inference")
    record = section.get(name) if isinstance(section, dict) else None
    if not isinstance(record, dict):
        raise InferenceCalibrationError(
            f"no inference calibration profile {name!r}; run "
            f"`pat infer-calibrate --profile {name}` first"
        )
    commitment = record.get(COMMITMENT_KEY)
    digest = commitment.get("digest") if isinstance(commitment, dict) else None
    if (
        not isinstance(commitment, dict)
        or commitment.get("schema") != INFERENCE_COMMITMENT_SCHEMA
        or not isinstance(digest, str)
    ):
        raise InferenceCalibrationTamperError(
            f"inference profile {name!r} carries no valid calibration commitment"
        )
    try:
        recomputed = commitment_digest(record)
    except InferenceCalibrationError as exc:
        raise InferenceCalibrationTamperError(
            f"inference profile {name!r} cannot be re-hashed: {exc}"
        ) from exc
    if recomputed != digest:
        raise InferenceCalibrationTamperError(
            f"inference profile {name!r} does not match its calibration "
            "commitment; the model file was modified after calibration. Re-run "
            "`pat infer-calibrate`."
        )
    if record.get("profile") != name:
        raise InferenceCalibrationTamperError(
            f"inference profile stored under {name!r} was calibrated as "
            f"{record.get('profile')!r}; refusing a renamed profile"
        )
    try:
        params = _validate_engine_params(
            EngineParams(**{f: float(record["params"][f]) for f in _PARAM_FIELDS})
        )
        hardware = HardwareSpec(**record["hardware"])
        points = tuple(point_from_mapping(row) for row in record["points"])
    except (
        KeyError,
        TypeError,
        ValueError,
        InferenceDomainError,
        InferenceCalibrationError,
    ) as exc:
        raise InferenceCalibrationTamperError(
            f"inference profile {name!r} is committed but malformed: {exc}"
        ) from exc
    return InferenceProfile(
        name=name, params=params, hardware=hardware, points=points, digest=digest
    )


def require_hardware_match(
    profile: InferenceProfile,
    weights_gb: float,
    kv_bytes_per_token: float,
    gpu_bandwidth_gbs: float,
    gpu_memory_gb: float,
) -> None:
    """Fail closed when the analysed hardware differs from the profile's (>1%)."""
    expected = profile.hardware
    for name, given, stored in (
        ("model weights (GB)", weights_gb, expected.weights_gb),
        ("KV bytes per token", kv_bytes_per_token, expected.kv_bytes_per_token),
        ("GPU bandwidth (GB/s)", gpu_bandwidth_gbs, expected.gpu_bandwidth_gbs),
        ("GPU memory (GB)", gpu_memory_gb, expected.gpu_memory_gb),
    ):
        if abs(given - stored) > HARDWARE_MATCH_TOLERANCE * abs(stored):
            raise InferenceCalibrationTamperError(
                f"calibration profile {profile.name!r} was fitted for {name} = "
                f"{stored:g}, not {given:g}; a fit does not transfer across "
                "models or GPUs. Calibrate a profile for this hardware."
            )
