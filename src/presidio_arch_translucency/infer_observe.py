"""
vLLM calibration windows from Prometheus (ADR-0012, Inference Arc Phase 1b).

`pat infer-observe` turns one steady-state window of a single vLLM engine into
an :class:`~presidio_arch_translucency.infer_calibrate.InferencePoint` for
`pat infer-calibrate`. Metric names are pinned to vLLM 0.31 (released
2026-10-05; ``vllm/v1/metrics/loggers.py``):

  vllm:num_requests_running            gauge      mean running batch
  vllm:num_requests_waiting            gauge      queue (prefill gate)
  vllm:kv_cache_usage_perc             gauge      0..1, live KV footprint
  vllm:inter_token_latency_seconds     histogram  TPOT
  vllm:request_prefill_time_seconds    histogram  prefill per request
  vllm:request_prompt_tokens           histogram  P per request
  vllm:request_generation_tokens       histogram  O per request
  vllm:request_success_total           counter    completed requests
  vllm:num_preemptions_total           counter    steady-state gate

Every series carries ``model_name`` and ``engine`` labels (``engine`` is the
data-parallel rank). Older engines renamed two of these
(``vllm:time_per_output_token_seconds``, ``vllm:gpu_cache_usage_perc``); use
the override flags for them.

All queries are evaluated at one shared instant, so they describe the same
window. A window is refused, never recorded, unless it is steady:

* exactly one engine series across the *whole* window (a restart under a new
  ``instance`` label would otherwise double the batch);
* no counter reset (an in-window restart) and no preemption (KV overflow);
* the running batch never reaches 0 and varies little (coefficient of
  variation below :data:`MAX_BATCH_CV`), which rejects ramps;
* Little's law holds: completions per second agree with
  ``batch / (prefill + O·TPOT)`` within :data:`LITTLE_TOLERANCE` — the single
  check that catches ramps, restarts and double counting together;
* the window spans at least :data:`MIN_WINDOWS_PER_REQUEST` mean request
  latencies, since per-request token means are observed at completion and a
  short window over-samples short requests;
* enough requests completed, and the engine was not idle.

Prefill time is only included when the queue was empty and the batch small,
because under load it is inflated by queueing and decode interference.

Bounded claims: ``tp`` is the operator's statement (vLLM exposes no tp
metric). Mean inter-token latency is weighted by tokens, so steps with larger
batches count more; the model predicts ``t(E[b])``, and the bias
``s1·Var(b)/E[b]`` is what the variation gate bounds.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from presidio_arch_translucency.infer_calibrate import (
    InferenceCalibrationError,
    InferencePoint,
)
from presidio_arch_translucency.prometheus import (
    PrometheusError,
    _has_control_chars,
    _resolve_token,
    instant_query,
    instant_query_vector,
)

VLLM_METRICS_VERSION: Final[str] = "vllm 0.31"
DEFAULT_TPOT_METRIC: Final[str] = "vllm:inter_token_latency_seconds"
DEFAULT_KV_USAGE_METRIC: Final[str] = "vllm:kv_cache_usage_perc"
DEFAULT_WINDOW_S: Final[int] = 120
WINDOW_MIN_S: Final[int] = 30
WINDOW_MAX_S: Final[int] = 3600
DEFAULT_MIN_REQUESTS: Final[int] = 20
DEFAULT_PREFILL_MAX_BATCH: Final[float] = 4.0
#: Mean queue length below which the engine counts as not queueing.
PREFILL_MAX_WAITING: Final[float] = 0.05
#: Running-batch coefficient of variation above which the window is a ramp.
MAX_BATCH_CV: Final[float] = 0.25
#: Allowed relative disagreement between observed and Little's-law throughput.
LITTLE_TOLERANCE: Final[float] = 0.5
#: The window must span this many mean request latencies.
MIN_WINDOWS_PER_REQUEST: Final[float] = 5.0
MAX_LABEL_VALUE_LEN: Final[int] = 256

_METRIC_NAME_CHARS: Final[frozenset[str]] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:"
)


class InferenceObserveError(ValueError):
    """Raised when a window cannot be turned into a trustworthy point."""


@dataclass(frozen=True)
class VllmSelector:
    """Which engine to read: label values matched exactly."""

    model_name: str | None = None
    engine: str | None = "0"

    def __post_init__(self) -> None:
        for name, value in (("model_name", self.model_name), ("engine", self.engine)):
            if value is None:
                continue
            if (
                not isinstance(value, str)
                or not (1 <= len(value) <= MAX_LABEL_VALUE_LEN)
                or _has_control_chars(value)
            ):
                raise InferenceObserveError(
                    f"{name} must be 1–{MAX_LABEL_VALUE_LEN} characters without "
                    "control characters"
                )

    def promql(self) -> str:
        """``{model_name="…",engine="…"}`` with values escaped for PromQL."""
        parts = []
        for name, value in (("model_name", self.model_name), ("engine", self.engine)):
            if value is not None:
                escaped = value.replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'{name}="{escaped}"')
        return "{" + ",".join(parts) + "}"


def _metric_name(value: str, flag: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 200
        or not set(value) <= _METRIC_NAME_CHARS
        or value[0].isdigit()
    ):
        raise InferenceObserveError(f"{flag} must be a Prometheus metric name")
    return value


def _window(window_s: int) -> int:
    if isinstance(window_s, bool) or not isinstance(window_s, int):
        raise InferenceObserveError("window must be whole seconds")
    if not WINDOW_MIN_S <= window_s <= WINDOW_MAX_S:
        raise InferenceObserveError(
            f"window must be between {WINDOW_MIN_S} and {WINDOW_MAX_S} seconds"
        )
    return window_s


def vllm_queries(
    selector: VllmSelector,
    window_s: int,
    tpot_metric: str = DEFAULT_TPOT_METRIC,
    kv_usage_metric: str = DEFAULT_KV_USAGE_METRIC,
) -> dict[str, str]:
    """The PromQL for one window. Each scalar query aggregates to one value."""
    sel = selector.promql()
    w = f"[{_window(window_s)}s]"
    tpot = _metric_name(tpot_metric, "--tpot-metric")
    kv = _metric_name(kv_usage_metric, "--kv-usage-metric")
    running = f"vllm:num_requests_running{sel}"

    def mean_of(histogram: str) -> str:
        return (
            f"sum(rate({histogram}_sum{sel}{w})) / sum(rate({histogram}_count{sel}{w}))"
        )

    return {
        "engines": running,
        "series_over_window": f"count(count_over_time({running}{w}))",
        "batch": f"sum(avg_over_time({running}{w}))",
        "batch_min": f"min(min_over_time({running}{w}))",
        "batch_stddev": f"sum(stddev_over_time({running}{w}))",
        "waiting": f"sum(avg_over_time(vllm:num_requests_waiting{sel}{w}))",
        "kv_usage": f"sum(avg_over_time({kv}{sel}{w}))",
        "tpot_s": mean_of(tpot),
        "prefill_s": mean_of("vllm:request_prefill_time_seconds"),
        "e2e_s": mean_of("vllm:e2e_request_latency_seconds"),
        "prompt_tokens": mean_of("vllm:request_prompt_tokens"),
        "output_tokens": mean_of("vllm:request_generation_tokens"),
        "requests": f"sum(increase(vllm:request_success_total{sel}{w}))",
        "resets": f"sum(resets(vllm:request_success_total{sel}{w}))",
        "preemptions": f"sum(increase(vllm:num_preemptions_total{sel}{w}))",
    }


@dataclass(frozen=True)
class ObservedWindow:
    point: InferencePoint
    window_s: int
    eval_time: float
    requests: float
    waiting: float
    batch_cv: float
    little_ratio: float  # observed completions / Little's-law prediction
    prefill_included: bool
    kv_usage_included: bool


ScalarQuery = Callable[..., "float | None"]
VectorQuery = Callable[..., "list[tuple[dict[str, str], float]]"]

_REQUIRED: Final[tuple[str, ...]] = (
    "series_over_window",
    "batch",
    "batch_min",
    "batch_stddev",
    "waiting",
    "tpot_s",
    "prompt_tokens",
    "output_tokens",
)


def observe_vllm_window(
    base_url: str,
    tp: int,
    selector: VllmSelector | None = None,
    window_s: int = DEFAULT_WINDOW_S,
    min_requests: int = DEFAULT_MIN_REQUESTS,
    prefill_max_batch: float = DEFAULT_PREFILL_MAX_BATCH,
    include_kv_usage: bool = True,
    tpot_metric: str = DEFAULT_TPOT_METRIC,
    kv_usage_metric: str = DEFAULT_KV_USAGE_METRIC,
    scalar_query: ScalarQuery | None = None,
    vector_query: VectorQuery | None = None,
    eval_time: float | None = None,
) -> ObservedWindow:
    """Read one window of one vLLM engine and return a calibration point.

    Fail-closed: raises :class:`InferenceObserveError` rather than returning a
    point that does not describe a steady, single-engine window.
    """
    selector = selector or VllmSelector()
    scalar_query = scalar_query or instant_query
    vector_query = vector_query or instant_query_vector
    if isinstance(min_requests, bool) or not isinstance(min_requests, int):
        raise InferenceObserveError("min_requests must be an integer")
    if not 1 <= min_requests <= 1_000_000:
        raise InferenceObserveError("min_requests must be in [1, 1000000]")
    queries = vllm_queries(selector, window_s, tpot_metric, kv_usage_metric)
    # One evaluation instant for every query: they describe the same window.
    at = time.time() if eval_time is None else eval_time
    try:
        token = _resolve_token(base_url)
        engines = vector_query(base_url, queries["engines"], token=token, eval_time=at)
        if len(engines) != 1:
            found = (
                "no vLLM engine series"
                if not engines
                else f"{len(engines)} engine series"
            )
            raise InferenceObserveError(
                f"{found} match {selector.promql()}; a calibration point describes "
                "exactly one engine (filter with --model-name / --engine)"
            )
        values = {
            name: scalar_query(base_url, query, token=token, eval_time=at)
            for name, query in queries.items()
            if name != "engines"
        }
    except PrometheusError as exc:
        raise InferenceObserveError(str(exc)) from exc

    requests = values["requests"] or 0.0
    if requests < min_requests:
        raise InferenceObserveError(
            f"only {requests:.0f} requests completed in {window_s}s "
            f"(minimum {min_requests}); lengthen the window or raise the load"
        )
    missing = [name for name in _REQUIRED if values[name] is None]
    if missing:
        raise InferenceObserveError(
            f"no data for {', '.join(missing)} in the last {window_s}s; are these "
            f"{VLLM_METRICS_VERSION} metric names?"
        )
    if values["series_over_window"] != 1.0:
        raise InferenceObserveError(
            f"{values['series_over_window']:.0f} engine series appeared during "
            "the window (restart or relabel); not one steady engine"
        )
    if (values["resets"] or 0.0) > 0.0:
        raise InferenceObserveError(
            "a counter reset during the window (engine restart); not steady state"
        )
    preemptions = values["preemptions"] or 0.0
    if preemptions > 0.0:
        raise InferenceObserveError(
            f"{preemptions:g} preemption(s) in the window: the KV cache "
            "overflowed, so this is not a steady-state point"
        )
    batch = values["batch"]
    if batch <= 0.0 or values["batch_min"] <= 0.0:
        raise InferenceObserveError(
            "the running batch reached 0 during the window (idle or ramping)"
        )
    batch_cv = values["batch_stddev"] / batch
    if batch_cv >= MAX_BATCH_CV:
        raise InferenceObserveError(
            f"running batch varied too much (CV {batch_cv:.2f} ≥ {MAX_BATCH_CV}); "
            "hold the load steady for the whole window"
        )

    tpot_s = values["tpot_s"]
    output_tokens = values["output_tokens"]
    prefill_s = values["prefill_s"] or 0.0
    service_s = prefill_s + output_tokens * tpot_s
    little_ratio = (requests / window_s) / (batch / service_s)
    if abs(little_ratio - 1.0) > LITTLE_TOLERANCE:
        raise InferenceObserveError(
            f"Little's law does not hold ({requests:.0f} completions vs "
            f"{batch / service_s * window_s:.0f} implied by batch {batch:.2f} and "
            f"{service_s:.2f}s per request): ramp, restart or double counting"
        )
    e2e_s = values["e2e_s"]
    if e2e_s is not None and window_s < MIN_WINDOWS_PER_REQUEST * e2e_s:
        raise InferenceObserveError(
            f"window {window_s}s is shorter than {MIN_WINDOWS_PER_REQUEST:g}× the "
            f"mean request latency ({e2e_s:.1f}s); token means would over-sample "
            "short requests"
        )

    waiting = values["waiting"]
    prefill_included = (
        values["prefill_s"] is not None
        and waiting < PREFILL_MAX_WAITING
        and batch <= prefill_max_batch
    )
    kv_usage = values["kv_usage"] if include_kv_usage else None
    try:
        point = InferencePoint(
            tp=tp,
            batch=batch,
            prompt_tokens=values["prompt_tokens"],
            output_tokens=output_tokens,
            tpot_ms=tpot_s * 1e3,
            prefill_ms=prefill_s * 1e3 if prefill_included else None,
            kv_usage=kv_usage,
        )
    except InferenceCalibrationError as exc:
        raise InferenceObserveError(f"window is out of domain: {exc}") from exc
    return ObservedWindow(
        point=point,
        window_s=window_s,
        eval_time=at,
        requests=requests,
        waiting=waiting,
        batch_cv=batch_cv,
        little_ratio=little_ratio,
        prefill_included=prefill_included,
        kv_usage_included=kv_usage is not None,
    )
