"""
Training-calibration from step-time logs (L-TR-1, v0.23.0).

Fits the training-domain efficiency model (see ``training.py``) to a handful of
recorded training runs, one JSON-Lines *step log* per run, then persists the
fitted per-strategy overhead parameters into the ``training`` section of the
calibrated-model store — committed exactly the way ``pat calibrate`` commits a
serving fit (ADR-0010/0011 hash-surface discipline).

Step-log contract (the repo's step-log format)
----------------------------------------------
A step log is **JSON Lines** (one JSON object per line). Each object records a
single optimizer step and MUST contain exactly these keys — unknown keys are
rejected so a typo'd ``"power"`` cannot silently vanish:

    {"step": <int >= 0>,
     "duration_s": <float > 0, finite>,
     "samples": <int >= 1>,
     "power_w": <optional float > 0, finite>}

``power_w`` is the *board / device-group* power draw measured during the step
(a producer's measured claim or a modelled estimate — attributed as such, never
an observation-chain reading; see PRESIDIO-REQ Energy Arc invariant E1a). It is
either present on **every** line of a run or **none** — partial coverage is an
error (no silent averaging over gaps).

Bounds (fail-closed, checked before any parsing):

* file must be a regular file, ``<= 10 MB``, ``<= 100000`` lines;
* read as UTF-8 with ``errors="strict"`` (a decode error fails closed);
* a malformed line raises :class:`StepLogError` naming the 1-based line number.

The run-level aggregate a fit consumes is ``samples_per_second`` =
``total_samples / total_duration_s``; the energy aggregate is the
duration-weighted mean of ``power_w``.

Identifiability note
--------------------
The efficiency model mirrors ``training.scaling_efficiency`` exactly: a single
device carries no overhead (``eff(δ≤1) = 1``), and for the α/β strategies
``eff(δ>1) = 1 − α − β·ln δ`` (pipeline: ``eff(δ>1) = (1−α)·m/(m+δ−1)``). A run
at ``δ=1`` therefore pins ``baseline`` directly (its throughput *is* the
baseline), and the remaining ``δ>1`` points identify ``α`` (and ``β`` with ≥3
degrees). Absent a ``δ=1`` anchor the ``δ>1`` curve only constrains the products
``baseline·(1−α)`` and ``baseline·β``, so the ``baseline``/``α`` split is not
uniquely identified from throughput alone. Such an input therefore fails closed:
every fit requires exactly one distinct ``δ=1`` anchor. The commitment binds the
whole fitted record.
"""

from __future__ import annotations

import json
import math
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from presidio_arch_translucency.calibrate import (
    TRAINING_CALIBRATION_COMMITMENT_SCHEMA,
    TRAINING_COMMITMENT_KEY,
    CalibrationError,
    _prepare_model_path,
    _read_existing_model,
    _write_private_json,
    global_model_path,
    training_commitment_digest_from_fields,
)
from presidio_arch_translucency.training import (
    DEFAULT_MICROBATCHES,
    STRATEGY_PARAMS,
    ParallelismStrategy,
)

#: Hard ingestion bounds (fail-closed before parsing — see module docstring).
MAX_STEP_LOG_BYTES: int = 10 * 1024 * 1024
MAX_STEP_LOG_LINES: int = 100_000
MAX_STEP_ID: int = 10_000_000_000
MAX_SAMPLES_PER_STEP: int = 1_000_000_000_000
MAX_STEP_DURATION_S: float = 86_400.0
MAX_STEP_POWER_W: float = 1_000_000.0
MAX_WATTS_PER_DEVICE: float = 2_000.0

#: Exactly the keys a step-log line may carry (strict — unknown keys rejected).
_STEP_LOG_KEYS: frozenset[str] = frozenset({"step", "duration_s", "samples", "power_w"})

#: Fit bounds mirror the strategy-parameter envelope (training.py / STRATEGY_PARAMS).
_ALPHA_MAX: float = 0.5
_BETA_MAX: float = 0.45


def _unique_object_pairs(pairs):  # noqa: ANN001, ANN202
    """Build a JSON object while refusing duplicate member names."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


class StepLogError(ValueError):
    """Raised on a malformed / out-of-bounds step log (fail-closed ingestion)."""


class TrainingCalibrationError(ValueError):
    """Raised when a training fit cannot run (too few / degenerate runs)."""


@dataclass(frozen=True)
class StepLog:
    """Run-level aggregates parsed from one step log.

    ``mean_power_w`` is the duration-weighted mean device-group power across the run
    when **every** line carried ``power_w``; ``None`` when **no** line did.
    """

    total_samples: int
    total_duration_s: float
    samples_per_second: float
    mean_power_w: float | None
    line_count: int


@dataclass(frozen=True)
class RunSummary:
    """One run's fitted-input summary (degree + measured aggregates)."""

    degree: int
    samples_per_second: float
    duration_s: float
    mean_power_w: float | None


@dataclass
class TrainingCalibrationResult:
    """Fitted training-overhead parameters plus fit quality and per-run data.

    Mirrors :class:`calibrate.CalibrationResult`. ``mean_power_w`` /
    ``watts_per_device`` are populated only when every run carried power.
    """

    strategy: str
    baseline_samples_per_second: float
    overhead_alpha: float
    overhead_beta: float
    r_squared: float
    rmse: float
    runs: list[RunSummary]
    predictions: list[float]
    residuals: list[float]
    mean_power_w: float | None = None
    watts_per_device: float | None = None
    microbatches: int = DEFAULT_MICROBATCHES
    #: True when the fit is exactly determined (distinct degrees == free
    #: parameters): R² is then trivially 1.0 and RMSE ≈ 0 by construction, so
    #: neither says anything about fit quality. Renderers must not present a
    #: saturated fit's R² as evidence of a good model.
    saturated: bool = False


# ---------------------------------------------------------------------------
# Step-log ingestion
# ---------------------------------------------------------------------------


def _require_int(
    obj: dict, key: str, *, minimum: int, maximum: int, line_no: int
) -> int:
    if key not in obj:
        raise StepLogError(f"line {line_no}: missing required key {key!r}")
    value = obj[key]
    # JSON booleans are ints in Python; a bool is never a valid count.
    if isinstance(value, bool) or not isinstance(value, int):
        raise StepLogError(
            f"line {line_no}: {key!r} must be an integer, got {type(value).__name__!r}"
        )
    if value < minimum:
        raise StepLogError(f"line {line_no}: {key!r} must be >= {minimum}, got {value}")
    if value > maximum:
        raise StepLogError(f"line {line_no}: {key!r} must be <= {maximum}, got {value}")
    return value


def _require_positive_finite(
    obj: dict, key: str, line_no: int, *, required: bool, maximum: float
) -> float | None:
    if key not in obj:
        if required:
            raise StepLogError(f"line {line_no}: missing required key {key!r}")
        return None
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StepLogError(
            f"line {line_no}: {key!r} must be a number, got {type(value).__name__!r}"
        )
    v = float(value)
    if not math.isfinite(v) or v <= 0.0:
        raise StepLogError(
            f"line {line_no}: {key!r} must be a finite number > 0, got {v!r}"
        )
    if v > maximum:
        raise StepLogError(f"line {line_no}: {key!r} must be <= {maximum:g}, got {v!r}")
    return v


def parse_step_log(path: str | Path) -> StepLog:
    """Parse a JSON-Lines step log into run-level aggregates (fail-closed).

    Enforces the step-log contract documented in the module docstring: size and
    line-count bounds are checked *before* parsing, unknown keys are rejected,
    and a malformed line raises :class:`StepLogError` naming its 1-based number.
    Partial ``power_w`` coverage is an error — power is present on every line or
    none.
    """
    p = Path(path)
    try:
        # Reject links explicitly, then bind type/size/read to one descriptor.
        # O_NOFOLLOW closes the lstat/open swap window on supported platforms.
        if stat.S_ISLNK(p.lstat().st_mode):
            raise StepLogError(f"step log {str(p)!r} must not be a symbolic link")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(p, flags)
        with os.fdopen(fd, "rb") as fh:
            info = os.fstat(fh.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise StepLogError(f"step log {str(p)!r} is not a regular file")
            raw_bytes = fh.read(MAX_STEP_LOG_BYTES + 1)
        if len(raw_bytes) > MAX_STEP_LOG_BYTES:
            raise StepLogError(f"step log exceeds {MAX_STEP_LOG_BYTES} bytes; refusing")
        text = raw_bytes.decode("utf-8", errors="strict")
    except StepLogError:
        raise
    except FileNotFoundError as exc:
        raise StepLogError(f"step log {str(p)!r} is not a regular file") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise StepLogError(f"step log could not be read as UTF-8: {exc}") from exc

    total_samples = 0
    total_duration = 0.0
    weighted_power = 0.0
    power_lines = 0
    line_count = 0
    previous_step: int | None = None

    for line_no, raw in enumerate(text.splitlines(), start=1):
        if line_no > MAX_STEP_LOG_LINES:
            raise StepLogError(f"step log exceeds {MAX_STEP_LOG_LINES} lines; refusing")
        if not raw.strip():
            continue  # tolerate blank / trailing-newline lines
        try:
            obj = json.loads(raw, object_pairs_hook=_unique_object_pairs)
        except ValueError as exc:
            raise StepLogError(f"line {line_no}: not valid JSON ({exc})") from exc
        except RecursionError as exc:
            # json.loads raises RecursionError (a RuntimeError, NOT a
            # ValueError) on pathologically nested input; without this catch a
            # crafted line inside the size bounds would escape the fail-closed
            # ingestion contract as a traceback instead of a StepLogError.
            raise StepLogError(f"line {line_no}: JSON too deeply nested") from exc
        if not isinstance(obj, dict):
            raise StepLogError(f"line {line_no}: each line must be a JSON object")
        unknown = set(obj) - _STEP_LOG_KEYS
        if unknown:
            raise StepLogError(
                f"line {line_no}: unknown key(s) {sorted(unknown)!r}; allowed: "
                f"{sorted(_STEP_LOG_KEYS)!r}"
            )
        step = _require_int(
            obj, "step", minimum=0, maximum=MAX_STEP_ID, line_no=line_no
        )
        if previous_step is not None and step <= previous_step:
            raise StepLogError(
                f"line {line_no}: 'step' must be strictly increasing "
                f"(previous {previous_step}, got {step})"
            )
        previous_step = step
        samples = _require_int(
            obj,
            "samples",
            minimum=1,
            maximum=MAX_SAMPLES_PER_STEP,
            line_no=line_no,
        )
        duration = _require_positive_finite(
            obj,
            "duration_s",
            line_no,
            required=True,
            maximum=MAX_STEP_DURATION_S,
        )
        power = _require_positive_finite(
            obj,
            "power_w",
            line_no,
            required=False,
            maximum=MAX_STEP_POWER_W,
        )

        line_count += 1
        total_samples += samples
        total_duration += duration  # type: ignore[operator]
        if not math.isfinite(total_duration):
            raise StepLogError(f"line {line_no}: duration aggregate overflow")
        if power is not None:
            power_lines += 1
            weighted_power += power * duration  # type: ignore[operator]
            if not math.isfinite(weighted_power):
                raise StepLogError(f"line {line_no}: power aggregate overflow")

    if line_count == 0:
        raise StepLogError("step log has no data lines")
    if power_lines not in (0, line_count):
        raise StepLogError(
            "partial power_w coverage: power is present on "
            f"{power_lines}/{line_count} lines; supply it on every line or none"
        )
    samples_per_second = total_samples / total_duration
    mean_power = weighted_power / total_duration if power_lines == line_count else None
    if not math.isfinite(samples_per_second) or (
        mean_power is not None and not math.isfinite(mean_power)
    ):
        raise StepLogError("step log aggregates are not finite")
    return StepLog(
        total_samples=total_samples,
        total_duration_s=total_duration,
        samples_per_second=samples_per_second,
        mean_power_w=mean_power,
        line_count=line_count,
    )


# ---------------------------------------------------------------------------
# Fit
# ---------------------------------------------------------------------------


def _fit_quality(observed, predicted):  # noqa: ANN001, ANN202
    import numpy as np  # noqa: PLC0415

    obs = np.asarray(observed, dtype=float)
    pred = np.asarray(predicted, dtype=float)
    residuals = obs - pred
    ss_res = float(np.sum(residuals**2))
    ss_tot = float(np.sum((obs - np.mean(obs)) ** 2))
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    rmse = float(np.sqrt(np.mean(residuals**2)))
    return r_squared, rmse, [float(r) for r in residuals]


def _aggregate_energy(runs: list[RunSummary]) -> tuple[float | None, float | None]:
    """Per-strategy energy aggregate across runs (MVP; see module docstring).

    ``mean_power_w`` is the duration-weighted mean device power over the runs;
    ``watts_per_device`` is the duration-weighted mean of each run's
    ``power_w / degree``. Both are
    returned only when **every** run carried power — partial coverage is caught
    earlier by :func:`fit_training_calibration`.
    """
    if any(r.mean_power_w is None for r in runs):
        return None, None
    total_dur = sum(r.duration_s for r in runs)
    mean_power = sum(r.mean_power_w * r.duration_s for r in runs) / total_dur  # type: ignore[operator]
    watts_per_device = (
        sum((r.mean_power_w / r.degree) * r.duration_s for r in runs) / total_dur  # type: ignore[operator]
    )
    if not all(math.isfinite(v) and v > 0 for v in (mean_power, watts_per_device)):
        raise TrainingCalibrationError("training power aggregates are not finite")
    if watts_per_device > MAX_WATTS_PER_DEVICE:
        raise TrainingCalibrationError(
            f"fitted watts per device exceeds {MAX_WATTS_PER_DEVICE:g} W"
        )
    return mean_power, watts_per_device


def fit_training_calibration(
    strategy: ParallelismStrategy,
    runs: list[tuple[int, StepLog]],
    microbatches: int = DEFAULT_MICROBATCHES,
) -> TrainingCalibrationResult:
    """Fit the efficiency model to per-degree step-log aggregates.

    *runs* is a list of ``(degree, StepLog)`` at **distinct** degrees. The model
    is ``tp(δ) = baseline · δ · eff(δ)`` (see module docstring). Requirements:

    Every calibration MUST include a degree-1 anchor; without it baseline and
    α are not separately identifiable. α/β strategies: ``>= 3`` distinct degrees fit
      (baseline, α, β); exactly ``2`` hold β at the strategy default and solve
      (baseline, α); ``< 2`` is an error.
    * pipeline: ``>= 2`` distinct degrees fit (baseline, α); ``1`` is an error.

    Energy: when every run carried ``power_w`` the per-strategy ``mean_power_w``
    and ``watts_per_device`` are aggregated; when none did, no energy figures;
    partial coverage is an error (no silent averaging over gaps).
    """
    if not runs:
        raise TrainingCalibrationError("at least one run is required to calibrate")
    if isinstance(strategy, str):
        strategy = ParallelismStrategy(strategy)
    if isinstance(microbatches, bool) or not isinstance(microbatches, int):
        raise TrainingCalibrationError("microbatches must be an integer")
    if not (1 <= microbatches <= 4096):
        raise TrainingCalibrationError("microbatches must be between 1 and 4096")

    degrees = [d for d, _ in runs]
    if len(set(degrees)) != len(degrees):
        raise TrainingCalibrationError("runs must have distinct parallelism degrees")
    if 1 not in degrees:
        raise TrainingCalibrationError(
            "training calibration requires a degree-1 anchor to identify baseline"
        )
    defaults = STRATEGY_PARAMS[strategy]
    for degree in degrees:
        if isinstance(degree, bool) or not isinstance(degree, int):
            raise TrainingCalibrationError("run degree must be an integer")
        if not (1 <= degree <= defaults.max_degree):
            raise TrainingCalibrationError(
                f"degree {degree} out of range for {strategy.value} "
                f"(1..{defaults.max_degree})"
            )

    # Partial power coverage across runs is rejected (no silent gaps), mirroring
    # the within-run rule in parse_step_log.
    powered = [log.mean_power_w is not None for _, log in runs]
    if any(powered) and not all(powered):
        raise TrainingCalibrationError(
            "partial power coverage across runs: supply power on every run or none"
        )

    summaries = [
        RunSummary(
            degree=degree,
            samples_per_second=log.samples_per_second,
            duration_s=log.total_duration_s,
            mean_power_w=log.mean_power_w,
        )
        for degree, log in runs
    ]
    for summary in summaries:
        if (
            not math.isfinite(summary.samples_per_second)
            or summary.samples_per_second <= 0
        ):
            raise TrainingCalibrationError("run throughput must be finite and > 0")
        if not math.isfinite(summary.duration_s) or summary.duration_s <= 0:
            raise TrainingCalibrationError("run duration must be finite and > 0")
        if summary.mean_power_w is not None and (
            not math.isfinite(summary.mean_power_w) or summary.mean_power_w <= 0
        ):
            raise TrainingCalibrationError("run power must be finite and > 0")
        if (
            summary.mean_power_w is not None
            and summary.mean_power_w / summary.degree > MAX_WATTS_PER_DEVICE
        ):
            raise TrainingCalibrationError(
                f"run power exceeds {MAX_WATTS_PER_DEVICE:g} W per device"
            )
    # Fit against the throughput observed at each degree.
    order = sorted(range(len(summaries)), key=lambda i: summaries[i].degree)
    summaries = [summaries[i] for i in order]

    import numpy as np  # noqa: PLC0415
    from scipy.optimize import curve_fit  # noqa: PLC0415

    deg = np.array([s.degree for s in summaries], dtype=float)
    tp = np.array([s.samples_per_second for s in summaries], dtype=float)

    is_pipeline = strategy is ParallelismStrategy.PIPELINE
    n_distinct = len(set(degrees))

    if is_pipeline:
        if n_distinct < 2:
            raise TrainingCalibrationError(
                "pipeline calibration needs >= 2 distinct degrees"
            )

        def _model(x, baseline, alpha):  # noqa: ANN001, ANN202
            bubble = microbatches / (microbatches + x - 1.0)
            eff = np.where(x <= 1.0, 1.0, (1.0 - alpha) * bubble)
            return baseline * x * eff

        baseline0 = float(tp[np.where(deg == 1.0)[0][0]])
        try:
            popt, _ = curve_fit(
                _model,
                deg,
                tp,
                p0=[baseline0, defaults.overhead_alpha],
                bounds=([1e-9, 0.0], [np.inf, _ALPHA_MAX]),
                maxfev=20000,
            )
        except (RuntimeError, ValueError, FloatingPointError) as exc:
            raise TrainingCalibrationError(f"training fit failed: {exc}") from exc
        baseline, alpha = float(popt[0]), float(popt[1])
        beta = 0.0  # unused for pipeline; store what the model uses
        preds = _model(deg, baseline, alpha)
    else:
        if n_distinct < 2:
            raise TrainingCalibrationError(
                f"{strategy.value} calibration needs >= 2 distinct degrees"
            )

        baseline0 = float(tp[np.where(deg == 1.0)[0][0]])

        if n_distinct == 2:
            # Two points cannot constrain β; hold it at the strategy default and
            # solve (baseline, α).
            beta = defaults.overhead_beta

            def _model2(x, baseline, alpha):  # noqa: ANN001, ANN202
                # Mirror scaling_efficiency exactly: eff(δ<=1) = 1.0 (no overhead
                # at a single device), so the δ=1 throughput pins ``baseline``.
                eff = np.where(
                    x <= 1.0,
                    1.0,
                    np.maximum(0.0, 1.0 - alpha - beta * np.log(np.maximum(x, 1.0))),
                )
                return baseline * x * eff

            try:
                popt, _ = curve_fit(
                    _model2,
                    deg,
                    tp,
                    p0=[baseline0, defaults.overhead_alpha],
                    bounds=([1e-9, 0.0], [np.inf, _ALPHA_MAX]),
                    maxfev=20000,
                )
            except (RuntimeError, ValueError, FloatingPointError) as exc:
                raise TrainingCalibrationError(f"training fit failed: {exc}") from exc
            baseline, alpha = float(popt[0]), float(popt[1])
            preds = _model2(deg, baseline, alpha)
        else:

            def _model3(x, baseline, alpha, beta):  # noqa: ANN001, ANN202
                # Mirror scaling_efficiency exactly: eff(δ<=1) = 1.0 (no overhead
                # at a single device), so the δ=1 throughput pins ``baseline``.
                eff = np.where(
                    x <= 1.0,
                    1.0,
                    np.maximum(0.0, 1.0 - alpha - beta * np.log(np.maximum(x, 1.0))),
                )
                return baseline * x * eff

            try:
                popt, _ = curve_fit(
                    _model3,
                    deg,
                    tp,
                    p0=[baseline0, defaults.overhead_alpha, defaults.overhead_beta],
                    bounds=([1e-9, 0.0, 0.0], [np.inf, _ALPHA_MAX, _BETA_MAX]),
                    maxfev=20000,
                )
            except (RuntimeError, ValueError, FloatingPointError) as exc:
                raise TrainingCalibrationError(f"training fit failed: {exc}") from exc
            baseline, alpha, beta = float(popt[0]), float(popt[1]), float(popt[2])
            preds = _model3(deg, baseline, alpha, beta)

    if not all(math.isfinite(v) for v in (baseline, alpha, beta)):
        raise TrainingCalibrationError(
            "training calibration produced non-finite params"
        )

    r_squared, rmse, residuals = _fit_quality(tp, preds)
    if not all(math.isfinite(v) for v in (r_squared, rmse, *residuals)):
        raise TrainingCalibrationError("training calibration quality is not finite")
    mean_power, watts_per_device = _aggregate_energy(summaries)

    # Exactly-determined systems (2 points / 2 params, or 3 points / 3 params)
    # interpolate their observations perfectly: R² = 1.0 and RMSE ≈ 0 carry no
    # quality information. Flag it so the CLI can annotate honestly.
    free_params = 2 if (is_pipeline or n_distinct == 2) else 3
    saturated = n_distinct == free_params

    return TrainingCalibrationResult(
        strategy=strategy.value,
        baseline_samples_per_second=baseline,
        overhead_alpha=alpha,
        overhead_beta=beta,
        r_squared=r_squared,
        rmse=rmse,
        runs=summaries,
        predictions=[float(p) for p in preds],
        residuals=residuals,
        mean_power_w=mean_power,
        watts_per_device=watts_per_device,
        microbatches=microbatches,
        saturated=saturated,
    )


# ---------------------------------------------------------------------------
# Committed persistence (mirrors calibrate.write_model_file / _fit_record)
# ---------------------------------------------------------------------------


def _training_fit_record(result: TrainingCalibrationResult) -> dict:
    """Serialise a training fit to the on-disk ``training[<strategy>]`` record.

    A NEW training calibration always writes the commitment (v0.23.0): the
    digest binds the fitted α/β/baseline and the per-run observation set that
    produced them, so a consumer can detect a post-calibration edit and fail
    closed. The energy figures, when present, are bound too (conditional on
    presence — a record with no power re-hashes byte-identically to the
    no-energy scheme).
    """
    runs = [
        [r.degree, r.samples_per_second, r.duration_s, r.mean_power_w]
        for r in result.runs
    ]
    calibrated_at = datetime.now(timezone.utc).isoformat()
    record: dict = {
        "strategy": result.strategy,
        "overhead_alpha": result.overhead_alpha,
        "overhead_beta": result.overhead_beta,
        "baseline_samples_per_second": result.baseline_samples_per_second,
        "r_squared": result.r_squared,
        "rmse": result.rmse,
        "calibrated_at": calibrated_at,
        "microbatches": result.microbatches,
        "runs": runs,
    }
    if result.mean_power_w is not None and result.watts_per_device is not None:
        record["mean_power_w"] = result.mean_power_w
        record["watts_per_device"] = result.watts_per_device
    record[TRAINING_COMMITMENT_KEY] = {
        "schema": TRAINING_CALIBRATION_COMMITMENT_SCHEMA,
        "digest": training_commitment_digest_from_fields(
            strategy=result.strategy,
            overhead_alpha=result.overhead_alpha,
            overhead_beta=result.overhead_beta,
            baseline_samples_per_second=result.baseline_samples_per_second,
            r_squared=result.r_squared,
            rmse=result.rmse,
            calibrated_at=calibrated_at,
            microbatches=result.microbatches,
            runs=runs,
            mean_power_w=result.mean_power_w,
            watts_per_device=result.watts_per_device,
        ),
    }
    return record


def write_training_fit(
    strategy: ParallelismStrategy | str, result: TrainingCalibrationResult
) -> Path:
    """Upsert ``model["training"][strategy]`` with the committed fit record.

    Every other model-file section (the global serving fit, per-layer records,
    other strategies' training records) is preserved untouched — the write is a
    surgical upsert, mirroring ``calibrate.write_model_file``'s per-layer path.

    Deliberate scope note: this path does NOT verify a pre-existing commitment
    on the record it replaces. A tampered record for the target strategy is
    simply superseded by the fresh committed fit — the fit consumed only the
    caller's step logs and ``STRATEGY_PARAMS`` defaults (never
    ``resolve_strategy_params``), so no tampered value can influence the new
    parameters. Consumers (``train-analyze`` / ``train-what-if``) remain the
    fail-closed gate.
    """
    if isinstance(strategy, ParallelismStrategy):
        strategy_key = strategy.value
    else:
        strategy_key = ParallelismStrategy(strategy).value
    path = global_model_path()
    try:
        _prepare_model_path(path)
        payload = _read_existing_model(path, strict=True)
        training = payload.get("training")
        if training is not None and not isinstance(training, dict):
            raise TrainingCalibrationError(
                "existing model training section must be a JSON object"
            )
        if not isinstance(training, dict):
            training = {}
        training[strategy_key] = _training_fit_record(result)
        payload["training"] = training
        _write_private_json(path, payload)
    except (CalibrationError, OSError) as exc:
        raise TrainingCalibrationError(
            f"training fit could not be stored: {exc}"
        ) from exc
    return path
