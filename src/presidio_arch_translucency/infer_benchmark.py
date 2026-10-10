"""
vLLM load harness for calibration points and λ sweeps (Inference Arc Phase 1c).

`pat infer-benchmark` drives load against a vLLM engine the operator already
runs (pat never launches engines, models or GPUs) and records each level with
`pat infer-observe`'s steadiness gates. Two submit policies share one loop:

  closed  — N requests in flight, resubmitted on completion. A steady running
            batch ≈ N: the calibration points `pat infer-calibrate` needs.
  open    — Poisson arrivals at λ req/s (in-flight capped). The per-layer
            SLO-capacity sweep the paper's H3 needs.

Per level: warm up, hold for the window, keep the load on for one scrape lag,
then read the window at the end of the hold (one shared evaluation instant).

Artefacts the harness prevents rather than detects:

* **Lockstep.** Identical lengths make every request finish and resubmit at
  once, putting a periodic prefill spike into TPOT that no gate sees. Initial
  closed-loop submits are staggered and ``max_tokens`` is jittered ±20 %
  around O (mean preserved).
* **Prefix cache.** Every prompt is fresh seeded random words behind a unique
  nonce, so no cached block can be shared; observe also gates on the hit rate.
* **Length drift.** Words are not tokens. Two probes (256 and 512 words)
  measure tokens per word and the fixed overhead (nonce, BOS) separately; a
  level whose achieved mean prompt misses the target by more than 5 % is
  refused. Output length is exact by construction under ``ignore_eos``, so a
  level is refused if any completion differs from its requested
  ``max_tokens`` (truncation by the context limit, or an early stop).
* **Wasted GPU time.** Window, minimum requests and tp are validated before
  any load, and ``P + 1.2·O`` is checked against the engine's
  ``max_model_len``. An open sweep stops at the first saturated level,
  detected from the client side (completions below 90 % of the arrivals
  offered during the hold, a queue, or rejections): an overloaded engine is
  steady at capacity, so the observe gates alone would record it and carry
  on. Comparing with offered arrivals rather than the nominal λ keeps Poisson
  noise out of the verdict.

The API key comes from ``PAT_VLLM_API_KEY`` only and requires https; requests
time out and redirects are refused. Energy is not measured here yet.
"""

from __future__ import annotations

import json
import math
import os
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Final

from presidio_arch_translucency import __version__
from presidio_arch_translucency.infer_calibrate import InferencePoint
from presidio_arch_translucency.infer_observe import (
    DEFAULT_WINDOW_S,
    InferenceObserveError,
    VllmSelector,
    _window,
    observe_vllm_window,
)
from presidio_arch_translucency.inference import SUPPORTED_TP_DEGREES
from presidio_arch_translucency.prometheus import (
    PrometheusError,
    _has_control_chars,
    _resolve_token,
    instant_query,
)

API_KEY_ENV: Final[str] = "PAT_VLLM_API_KEY"  # noqa: S105 -- env var name
_USER_AGENT: Final[str] = f"pat-cli/{__version__}"

MAX_IN_FLIGHT: Final[int] = 512
MAX_CONCURRENCY: Final[int] = 4096
MAX_RATE: Final[float] = 10_000.0
MAX_TOKENS_TARGET: Final[int] = 200_000
OUTPUT_JITTER: Final[float] = 0.2
STAGGER_S: Final[float] = 2.0
DRIFT_TOLERANCE: Final[float] = 0.05
DEFAULT_WARMUP_S: Final[int] = 60
DEFAULT_SCRAPE_LAG_S: Final[int] = 15
DEFAULT_REQUEST_TIMEOUT_S: Final[float] = 600.0
PROBE_WORDS: Final[tuple[int, int]] = (256, 512)
#: Open loop: completions below this fraction of the arrivals offered during
#: the hold mean the engine did not keep up (saturated).
SATURATION_THROUGHPUT_RATIO: Final[float] = 0.9
#: Open loop: a mean queue above this during the hold means saturation.
SATURATION_WAITING: Final[float] = 1.0

_sleep = time.sleep

#: Short, common words: one or two tokens each in mainstream tokenizers.
_WORDS: Final[tuple[str, ...]] = tuple(
    """time year people way day man thing woman life child world school state
    family student group country problem hand part place case week company
    system program question work government number night point home water room
    mother area money story fact month lot right study book eye job word
    business issue side kind head house service friend father power hour game
    line end member law car city community name president team minute idea kid
    body information back parent face others level office door health person
    art war history party result change morning reason research girl guy moment
    air teacher force education foot boy age policy music market sense nation
    plan college interest death experience effect use class control care field
    development role effort rate heart drug show leader light voice wife police
    mind price report decision son view relationship town road arm difference
    value building action model season society tax director position player
    record paper space ground form event official matter center couple site
    project activity star table need court oil situation cost industry figure
    street image phone data picture practice piece land product doctor wall
    patient worker news test movie north love support technology step baby
    computer type attention film tree source organization hair window evidence
    population site camera""".split()
)


class InferenceBenchmarkError(ValueError):
    """Raised on invalid harness configuration or an unusable endpoint."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201, PLR0913
        raise urllib.error.HTTPError(
            req.full_url, code, "redirects are refused", headers, fp
        )


@dataclass(frozen=True)
class CompletionResult:
    prompt_tokens: int
    completion_tokens: int
    latency_s: float


def _api_key(endpoint: urllib.parse.ParseResult) -> str | None:
    key = os.environ.get(API_KEY_ENV)
    if not key or not key.strip():
        return None
    if _has_control_chars(key):
        raise InferenceBenchmarkError(f"{API_KEY_ENV} contains control characters")
    if endpoint.scheme != "https":
        raise InferenceBenchmarkError(
            f"{API_KEY_ENV} requires an https endpoint; refusing to send an API key "
            "over cleartext HTTP"
        )
    return key.strip()


class VllmClient:
    """Minimal OpenAI-compatible ``/v1/completions`` client (non-streaming)."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        if not isinstance(endpoint, str) or _has_control_chars(endpoint):
            raise InferenceBenchmarkError("endpoint must be a plain URL")
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise InferenceBenchmarkError(
                f"endpoint must be an http(s) URL with a host, got {endpoint!r}"
            )
        if parsed.username is not None or parsed.password is not None:
            raise InferenceBenchmarkError("endpoint must not embed credentials")
        if (
            not isinstance(model, str)
            or not 1 <= len(model) <= 256
            or _has_control_chars(model)
        ):
            raise InferenceBenchmarkError("model must be 1–256 printable characters")
        if not (isinstance(timeout_s, (int, float)) and 1.0 <= timeout_s <= 3600.0):
            raise InferenceBenchmarkError("request timeout must be in [1, 3600] s")
        self.base = endpoint.rstrip("/")
        self.url = self.base + "/v1/completions"
        self.model = model
        self.timeout_s = float(timeout_s)
        self._key = _api_key(parsed)
        self._opener = urllib.request.build_opener(_NoRedirect())

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    def max_model_len(self) -> int | None:
        """The served model's context limit from ``/v1/models``, if reported."""
        req = urllib.request.Request(  # noqa: S310 -- scheme validated
            self.base + "/v1/models", headers=self._headers(), method="GET"
        )
        try:
            with self._opener.open(req, timeout=min(self.timeout_s, 60.0)) as resp:
                payload = json.loads(resp.read(1_048_576))
        except (OSError, ValueError) as exc:
            raise InferenceBenchmarkError(f"model listing failed: {exc}") from exc
        models = payload.get("data") if isinstance(payload, dict) else None
        for entry in models if isinstance(models, list) else []:
            if isinstance(entry, dict) and entry.get("id") == self.model:
                limit = entry.get("max_model_len")
                if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
                    return limit
                return None
        raise InferenceBenchmarkError(
            f"model {self.model!r} is not served at this endpoint"
        )

    def request_body(self, prompt: str, max_tokens: int) -> dict:
        return {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": int(max_tokens),
            "temperature": 0,
            "ignore_eos": True,
            "n": 1,
            "stream": False,
        }

    def complete(self, prompt: str, max_tokens: int) -> CompletionResult:
        data = json.dumps(self.request_body(prompt, max_tokens)).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 -- scheme validated
            self.url, data=data, headers=self._headers(), method="POST"
        )
        started = time.monotonic()
        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read(1_048_576))
        except (OSError, ValueError) as exc:
            raise InferenceBenchmarkError(f"completion request failed: {exc}") from exc
        latency = time.monotonic() - started
        usage = payload.get("usage") if isinstance(payload, dict) else None
        try:
            prompt_tokens = int(usage["prompt_tokens"])
            completion_tokens = int(usage["completion_tokens"])
        except (KeyError, TypeError, ValueError) as exc:
            raise InferenceBenchmarkError(
                "completion response carries no usage token counts"
            ) from exc
        if prompt_tokens < 0 or completion_tokens < 0:
            raise InferenceBenchmarkError("negative usage token counts")
        return CompletionResult(prompt_tokens, completion_tokens, latency)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def make_prompt(words: int, rng: random.Random) -> str:
    """A unique prompt: random nonce, then seeded random words."""
    nonce = f"{rng.getrandbits(64):016x}{rng.getrandbits(64):016x}"
    body = " ".join(rng.choice(_WORDS) for _ in range(max(0, words)))
    return f"{nonce} {body}"


def jittered_max_tokens(output_tokens: int, rng: random.Random) -> int:
    """Uniform ±20 % around O, mean preserved, at least 1."""
    low = output_tokens * (1.0 - OUTPUT_JITTER)
    high = output_tokens * (1.0 + OUTPUT_JITTER)
    return max(1, round(rng.uniform(low, high)))


@dataclass(frozen=True)
class PromptCalibration:
    tokens_per_word: float
    overhead_tokens: float  # nonce, BOS and template tokens around the words

    def words_for(self, prompt_tokens: int) -> int:
        return max(
            1, round((prompt_tokens - self.overhead_tokens) / self.tokens_per_word)
        )


def probe_prompt_calibration(
    client: VllmClient, rng: random.Random
) -> PromptCalibration:
    """Two probes separate tokens per word from the fixed per-prompt overhead."""
    short, long = PROBE_WORDS
    t_short = client.complete(make_prompt(short, rng), max_tokens=1).prompt_tokens
    t_long = client.complete(make_prompt(long, rng), max_tokens=1).prompt_tokens
    per_word = (t_long - t_short) / (long - short)
    if not per_word > 0.0:
        raise InferenceBenchmarkError(
            "prompt probes did not grow with length; cannot size prompts"
        )
    return PromptCalibration(per_word, max(0.0, t_short - short * per_word))


# ---------------------------------------------------------------------------
# Load loop
# ---------------------------------------------------------------------------


@dataclass
class _Record:
    started: float
    ended: float
    max_tokens: int
    prompt_tokens: int | None
    completion_tokens: int | None
    error: str | None


class LoadGenerator:
    """Runs one level's load until stopped; closed or open loop."""

    def __init__(
        self,
        client: VllmClient,
        mode: str,
        level: float,
        prompt_words: int,
        output_tokens: int,
        seed: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if mode not in ("closed", "open"):
            raise InferenceBenchmarkError("mode must be 'closed' or 'open'")
        self.client = client
        self.mode = mode
        self.level = level
        self.prompt_words = prompt_words
        self.output_tokens = output_tokens
        self.seed = seed
        self.clock = clock
        self.records: list[_Record] = []
        self.rejections: list[float] = []  # clock times of refused arrivals
        self.arrivals: list[float] = []  # clock times of every open-loop arrival
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pool: ThreadPoolExecutor | None = None
        self._in_flight = 0

    @property
    def rejected(self) -> int:
        with self._lock:
            return len(self.rejections)

    def rejected_between(self, start: float, end: float) -> int:
        with self._lock:
            return sum(1 for t in self.rejections if start <= t <= end)

    def arrived_between(self, start: float, end: float) -> int:
        with self._lock:
            return sum(1 for t in self.arrivals if start <= t <= end)

    def workers_alive(self) -> int:
        return sum(1 for thread in self._threads if thread.is_alive())

    def _one(self, rng: random.Random) -> None:
        # Each rng belongs to one worker or one request: no sharing, no lock.
        prompt = make_prompt(self.prompt_words, rng)
        max_tokens = jittered_max_tokens(self.output_tokens, rng)
        started = self.clock()
        try:
            result = self.client.complete(prompt, max_tokens)
            record = _Record(
                started,
                self.clock(),
                max_tokens,
                result.prompt_tokens,
                result.completion_tokens,
                None,
            )
        except Exception as exc:  # noqa: BLE001 -- a worker must never die silently
            record = _Record(
                started, self.clock(), max_tokens, None, None, str(exc)[:200]
            )
        with self._lock:
            self.records.append(record)

    def _closed_worker(self, index: int, count: int) -> None:
        rng = random.Random(f"{self.seed}:closed:{index}")  # noqa: S311 -- load shape
        if self._stop.wait(STAGGER_S * index / max(count, 1)):
            return
        while not self._stop.is_set():
            self._one(rng)

    def _open_dispatcher(self) -> None:
        arrivals = random.Random(f"{self.seed}:arrivals")  # noqa: S311 -- load shape
        prompts = random.Random(f"{self.seed}:prompts")  # noqa: S311 -- load shape
        assert self._pool is not None
        while not self._stop.wait(arrivals.expovariate(self.level)):
            with self._lock:
                self.arrivals.append(self.clock())
                if self._in_flight >= MAX_IN_FLIGHT:
                    self.rejections.append(self.clock())
                    continue
                self._in_flight += 1
                request_rng = random.Random(prompts.getrandbits(64))  # noqa: S311
            self._pool.submit(self._open_task, request_rng)

    def _open_task(self, rng: random.Random) -> None:
        try:
            self._one(rng)
        finally:
            with self._lock:
                self._in_flight -= 1

    def start(self) -> None:
        if self.mode == "closed":
            count = int(self.level)
            self._threads = [
                threading.Thread(
                    target=self._closed_worker, args=(i, count), daemon=True
                )
                for i in range(count)
            ]
        else:
            self._pool = ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT)
            self._threads = [
                threading.Thread(target=self._open_dispatcher, daemon=True)
            ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Stop submitting and drop queued work; in-flight requests finish.

        Bounded by the request timeout: the next level must start on an
        engine that is no longer serving this one.
        """
        self._stop.set()
        for thread in self._threads:
            thread.join()
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)

    def completed_between(self, start: float, end: float) -> list[_Record]:
        with self._lock:
            return [r for r in self.records if start <= r.ended <= end]


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


@dataclass
class LevelReport:
    mode: str
    level: float
    tp: int
    window_s: int
    completed: int
    errors: int
    offered: int  # open-loop arrivals during the hold (0 in closed loop)
    rejected: int  # open-loop arrivals refused by the in-flight cap, during the hold
    achieved_rps: float
    e2e_p50_ms: float | None
    e2e_p99_ms: float | None
    mean_prompt_tokens: float | None
    mean_output_tokens: float | None
    ttft_p99_ms: float | None  # server histogram over the window
    verdict: str  # "recorded" or "refused"
    reason: str | None = None
    point: dict | None = None
    saturated: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


def server_ttft_p99_ms(
    prometheus: str,
    selector: VllmSelector,
    window_s: int,
    eval_time: float,
    scalar_query: Callable[..., float | None] | None = None,
) -> float | None:
    """TTFT p99 from vLLM's histogram over the window (server side)."""
    scalar_query = scalar_query or instant_query
    query = (
        "histogram_quantile(0.99, sum by (le) (rate("
        f"vllm:time_to_first_token_seconds_bucket{selector.promql()}"
        f"[{window_s}s])))"
    )
    try:
        value = scalar_query(
            prometheus, query, token=_resolve_token(prometheus), eval_time=eval_time
        )
    except PrometheusError:
        return None
    return None if value is None else value * 1e3


@dataclass(frozen=True)
class BenchmarkConfig:
    endpoint: str
    prometheus: str
    model: str
    tp: int
    prompt_tokens: int
    output_tokens: int
    mode: str
    levels: tuple[float, ...]
    window_s: int = DEFAULT_WINDOW_S
    warmup_s: int = DEFAULT_WARMUP_S
    scrape_lag_s: int = DEFAULT_SCRAPE_LAG_S
    min_requests: int = 20
    engine: str = "0"
    seed: int = 0
    stop_on_saturation: bool = True

    def __post_init__(self) -> None:
        # Everything observe would refuse is refused here, before any load.
        if isinstance(self.tp, bool) or self.tp not in SUPPORTED_TP_DEGREES:
            raise InferenceBenchmarkError(f"tp must be one of {SUPPORTED_TP_DEGREES}")
        try:
            _window(self.window_s)
            VllmSelector(model_name=self.model, engine=self.engine)
        except InferenceObserveError as exc:
            raise InferenceBenchmarkError(str(exc)) from exc
        if isinstance(self.min_requests, bool) or not isinstance(
            self.min_requests, int
        ):
            raise InferenceBenchmarkError("min_requests must be an integer")
        if not 1 <= self.min_requests <= 1_000_000:
            raise InferenceBenchmarkError("min_requests must be in [1, 1000000]")
        for name in ("prompt_tokens", "output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InferenceBenchmarkError(f"{name} must be an integer")
            if not 1 <= value <= MAX_TOKENS_TARGET:
                raise InferenceBenchmarkError(
                    f"{name} must be in [1, {MAX_TOKENS_TARGET}]"
                )
        if self.mode not in ("closed", "open"):
            raise InferenceBenchmarkError("mode must be 'closed' or 'open'")
        if not self.levels:
            raise InferenceBenchmarkError("give at least one level")
        for level in self.levels:
            if self.mode == "closed":
                if not float(level).is_integer() or not 1 <= level <= MAX_CONCURRENCY:
                    raise InferenceBenchmarkError(
                        f"concurrency levels must be integers in [1, {MAX_CONCURRENCY}]"
                    )
            elif not (math.isfinite(level) and 0.0 < level <= MAX_RATE):
                raise InferenceBenchmarkError(
                    f"rate levels must be in (0, {MAX_RATE}] req/s"
                )
        for name, low, high in (
            ("warmup_s", 0, 3600),
            ("scrape_lag_s", 0, 300),
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise InferenceBenchmarkError(f"{name} must be whole seconds")
            if not low <= value <= high:
                raise InferenceBenchmarkError(f"{name} must be in [{low}, {high}]")

    def longest_request_tokens(self) -> int:
        return self.prompt_tokens + math.ceil(self.output_tokens * (1 + OUTPUT_JITTER))


def check_context_fits(config: BenchmarkConfig, client: VllmClient) -> None:
    """Refuse before any load when P + 1.2·O exceeds the engine's context."""
    limit = client.max_model_len()
    if limit is not None and config.longest_request_tokens() > limit:
        raise InferenceBenchmarkError(
            f"P + 1.2·O = {config.longest_request_tokens()} tokens exceeds the "
            f"engine's max_model_len {limit}"
        )


def run_level(
    config: BenchmarkConfig,
    client: VllmClient,
    level: float,
    prompts: PromptCalibration,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    wall_clock: Callable[[], float] | None = None,
    observe: Callable[..., object] | None = None,
    ttft: Callable[..., float | None] | None = None,
) -> LevelReport:
    """Warm up, hold, read the window, verify lengths; never raise on refusal."""
    # Resolved at call time so tests can substitute module attributes.
    sleep = sleep or _sleep
    clock = clock or time.monotonic
    wall_clock = wall_clock or time.time
    observe = observe or observe_vllm_window
    ttft = ttft or server_ttft_p99_ms

    selector = VllmSelector(model_name=config.model, engine=config.engine)
    load = LoadGenerator(
        client,
        config.mode,
        level,
        prompts.words_for(config.prompt_tokens),
        config.output_tokens,
        seed=f"{config.seed}:{level!r}",
        clock=clock,
    )
    observed = None
    refusal: InferenceObserveError | None = None
    load.start()
    try:
        sleep(config.warmup_s)
        hold_start = clock()
        sleep(config.window_s)
        hold_end = clock()
        eval_time = wall_clock()
        workers_alive = load.workers_alive()
        # Keep the load on until Prometheus has scraped the end of the hold.
        sleep(config.scrape_lag_s)
        try:
            observed = observe(
                config.prometheus,
                config.tp,
                selector,
                window_s=config.window_s,
                min_requests=config.min_requests,
                eval_time=eval_time,
            )
        except InferenceObserveError as exc:
            refusal = exc
        ttft_p99 = ttft(config.prometheus, selector, config.window_s, eval_time)
    finally:
        load.stop()

    held = load.completed_between(hold_start, hold_end)
    ok = [r for r in held if r.error is None]
    errors = len(held) - len(ok)
    rejected = load.rejected_between(hold_start, hold_end)
    latencies = [(r.ended - r.started) * 1e3 for r in ok]
    mean_prompt = sum(r.prompt_tokens for r in ok) / len(ok) if ok else None
    mean_output = sum(r.completion_tokens for r in ok) / len(ok) if ok else None
    achieved_rps = len(ok) / max(hold_end - hold_start, 1e-9)
    offered = load.arrived_between(hold_start, hold_end)

    reasons: list[str] = []
    if refusal is not None:
        reasons.append(str(refusal))
    if config.mode == "closed" and workers_alive < int(level):
        reasons.append(f"only {workers_alive} of {int(level)} workers were running")
    if errors:
        reasons.append(f"{errors} request(s) failed during the hold")
    if mean_prompt is None:
        reasons.append("no request completed during the hold")
    elif abs(mean_prompt - config.prompt_tokens) / config.prompt_tokens > (
        DRIFT_TOLERANCE
    ):
        reasons.append(
            f"achieved mean prompt tokens {mean_prompt:.0f} misses the target "
            f"{config.prompt_tokens} by more than {DRIFT_TOLERANCE:.0%}"
        )
    truncated = sum(1 for r in ok if r.completion_tokens != r.max_tokens)
    if truncated:
        reasons.append(
            f"{truncated} completion(s) differ from their requested max_tokens "
            "(context limit or early stop)"
        )

    saturated = False
    if config.mode == "open":
        waiting = getattr(observed, "waiting", 0.0)
        saturated = (
            rejected > 0
            or (offered > 0 and len(ok) < SATURATION_THROUGHPUT_RATIO * offered)
            or waiting > SATURATION_WAITING
            or (refusal is not None and refusal.code in ("little", "preemption"))
        )

    point = getattr(observed, "point", None)
    return LevelReport(
        mode=config.mode,
        level=level,
        tp=config.tp,
        window_s=config.window_s,
        completed=len(ok),
        errors=errors,
        offered=offered,
        rejected=rejected,
        achieved_rps=achieved_rps,
        e2e_p50_ms=_percentile(latencies, 0.5),
        e2e_p99_ms=_percentile(latencies, 0.99),
        mean_prompt_tokens=mean_prompt,
        mean_output_tokens=mean_output,
        ttft_p99_ms=ttft_p99,
        verdict="refused" if reasons else "recorded",
        reason="; ".join(reasons) or None,
        point=(
            point.as_row()
            if not reasons and isinstance(point, InferencePoint)
            else None
        ),
        saturated=saturated,
    )


@dataclass
class BenchmarkReport:
    config: dict
    prompts: PromptCalibration | None = None
    levels: list[LevelReport] = field(default_factory=list)
    stopped_early: bool = False
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "schema": "presidio-hardened/inference-benchmark@1",
            "config": self.config,
            "prompt_calibration": asdict(self.prompts) if self.prompts else None,
            "stopped_early": self.stopped_early,
            "error": self.error,
            "levels": [level.as_dict() for level in self.levels],
        }


def run_benchmark(
    config: BenchmarkConfig,
    client: VllmClient | None = None,
    on_level: Callable[[LevelReport, BenchmarkReport], None] | None = None,
    report: BenchmarkReport | None = None,
    **level_kwargs: object,
) -> BenchmarkReport:
    """Check the context, probe prompt sizing, then run every level in order.

    ``report`` (optional) is filled in place, so a caller still holds the
    completed levels if a later one raises. In open mode a saturated level
    ends the sweep (higher λ only saturates harder) unless
    ``stop_on_saturation`` is False.
    """
    client = client or VllmClient(config.endpoint, config.model)
    if report is None:
        report = BenchmarkReport(config={})
    report.config = {
        key: value
        for key, value in asdict(config).items()
        if key not in ("endpoint", "prometheus")
    }
    check_context_fits(config, client)
    probe_rng = random.Random(f"{config.seed}:probe")  # noqa: S311 -- load shape
    report.prompts = probe_prompt_calibration(client, probe_rng)
    for level in config.levels:
        result = run_level(config, client, level, report.prompts, **level_kwargs)
        report.levels.append(result)
        if on_level is not None:
            on_level(result, report)
        if config.mode == "open" and result.saturated and config.stop_on_saturation:
            report.stopped_early = level != config.levels[-1]
            break
    return report
