"""Tests for the vLLM load harness (infer_benchmark.py), against a fake server."""

import json
import random
import statistics
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency import infer_benchmark
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.infer_benchmark import (
    BenchmarkConfig,
    BenchmarkReport,
    InferenceBenchmarkError,
    LoadGenerator,
    PromptCalibration,
    VllmClient,
    check_context_fits,
    jittered_max_tokens,
    make_prompt,
    probe_prompt_calibration,
    run_benchmark,
    run_level,
    server_ttft_p99_ms,
)
from presidio_arch_translucency.infer_calibrate import InferencePoint
from presidio_arch_translucency.infer_observe import (
    InferenceObserveError,
    ObservedWindow,
    VllmSelector,
)


def fake_tokens(prompt: str) -> int:
    """BOS + a 10-token nonce + words, every third word two tokens."""
    words = prompt.split()[1:]
    return 1 + 10 + sum(2 if i % 3 == 2 else 1 for i in range(len(words)))


class FakeVllm:
    """An OpenAI-compatible server with a non-trivial tokenizer."""

    def __init__(
        self,
        delay_s=0.002,
        fail=False,
        usage=True,
        redirect=False,
        truncate=False,
        max_model_len=8192,
        model="m",
    ):
        self.bodies = []
        self.headers = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, code, payload=None):
                data = json.dumps(payload or {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                entry = {"id": model, "object": "model"}
                if max_model_len is not None:
                    entry["max_model_len"] = max_model_len
                self._send(200, {"data": [entry]})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.bodies.append(body)
                owner.headers.append(dict(self.headers))
                if redirect:
                    self.send_response(302)
                    self.send_header("Location", "http://elsewhere.test/")
                    self.end_headers()
                    return
                if fail:
                    self._send(500)
                    return
                time.sleep(delay_s)
                payload = {"choices": [{"text": "x"}]}
                if usage:
                    completion = body["max_tokens"] - (
                        1 if truncate and body["max_tokens"] > 1 else 0
                    )
                    payload["usage"] = {
                        "prompt_tokens": fake_tokens(body["prompt"]),
                        "completion_tokens": completion,
                    }
                self._send(200, payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(
            target=self.server.serve_forever, args=(0.01,), daemon=True
        ).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def vllm():
    server = FakeVllm()
    yield server
    server.close()


@pytest.fixture
def make_vllm():
    servers = []

    def make(**kw):
        servers.append(FakeVllm(**kw))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def _observed(tp=1, waiting=0.0):
    point = InferencePoint(tp, 3.0, 900.0, 300.0, 30.0)
    return ObservedWindow(point, 120, 1.0, 40.0, waiting, 0.1, 1.0, False, False)


def _fast_sleep(seconds):
    time.sleep(seconds / 2000.0)  # 120 s hold → 60 ms


def _config(url, **kw):
    base = {
        "endpoint": url,
        "prometheus": "http://prom.test:9090",
        "model": "m",
        "tp": 1,
        "prompt_tokens": 900,
        "output_tokens": 300,
        "mode": "closed",
        "levels": (2.0,),
        "warmup_s": 20,
        "scrape_lag_s": 0,
    }
    return BenchmarkConfig(**{**base, **kw})


# Open-loop settings the fake engine sustains even on a slow runner: 100 rps
# over a 400 ms scaled hold after a 100 ms warmup.
_UNSATURATED = {"levels": (100.0,), "level": 100.0, "window_s": 800, "warmup_s": 200}

EXACT = PromptCalibration(tokens_per_word=4 / 3, overhead_tokens=11.0)


def _run(server, observe=None, ttft=None, prompts=EXACT, level=None, **kw):
    config = _config(server.url, **kw)
    return run_level(
        config,
        VllmClient(server.url, "m"),
        level if level is not None else config.levels[0],
        prompts,
        sleep=_fast_sleep,
        observe=observe or (lambda *a, **k: _observed()),
        ttft=ttft or (lambda *a, **k: 412.0),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def test_request_body_pins(vllm):
    client = VllmClient(vllm.url, "meta/llama")
    result = client.complete("nonce hello world", 7)
    assert vllm.bodies[0] == {
        "model": "meta/llama",
        "prompt": "nonce hello world",
        "max_tokens": 7,
        "temperature": 0,
        "ignore_eos": True,
        "n": 1,
        "stream": False,
    }
    assert (result.prompt_tokens, result.completion_tokens) == (13, 7)
    assert "Authorization" not in vllm.headers[0]


def test_client_refuses_redirects_and_errors(make_vllm):
    for server in (
        make_vllm(redirect=True),
        make_vllm(fail=True),
        make_vllm(usage=False),
    ):
        with pytest.raises(InferenceBenchmarkError):
            VllmClient(server.url, "m").complete("a", 1)


@pytest.mark.parametrize(
    ("endpoint", "model"),
    [
        ("ftp://x", "m"),
        ("http://user:pw@x", "m"),
        ("http://", "m"),
        ("http://x\n", "m"),
        ("http://x", ""),
        ("http://x", "a\x00b"),
    ],
)
def test_client_validation(endpoint, model):
    with pytest.raises(InferenceBenchmarkError):
        VllmClient(endpoint, model)


def test_api_key_requires_https(monkeypatch):
    monkeypatch.setenv("PAT_VLLM_API_KEY", "sk-test")
    with pytest.raises(InferenceBenchmarkError, match="https"):
        VllmClient("http://x", "m")
    assert VllmClient("https://x", "m")._key == "sk-test"


def test_api_key_is_sent_as_bearer(vllm):
    client = VllmClient(vllm.url, "m")
    client._key = "sk-test"  # https is enforced at construction; transport is fake
    client.complete("a", 1)
    assert vllm.headers[0]["Authorization"] == "Bearer sk-test"


def test_max_model_len(make_vllm):
    assert VllmClient(make_vllm().url, "m").max_model_len() == 8192
    assert VllmClient(make_vllm(max_model_len=None).url, "m").max_model_len() is None
    with pytest.raises(InferenceBenchmarkError, match="not served"):
        VllmClient(make_vllm(model="other").url, "m").max_model_len()


def test_context_check_refuses_before_load(make_vllm):
    server = make_vllm(max_model_len=1000)
    with pytest.raises(InferenceBenchmarkError, match="max_model_len 1000"):
        check_context_fits(_config(server.url), VllmClient(server.url, "m"))
    assert server.bodies == []  # no completion was sent
    check_context_fits(
        _config(server.url, prompt_tokens=500, output_tokens=300),
        VllmClient(server.url, "m"),
    )


# ---------------------------------------------------------------------------
# Prompts and lengths
# ---------------------------------------------------------------------------


def test_prompts_are_unique_and_sized():
    rng = random.Random(0)  # noqa: S311 -- test
    prompts = {make_prompt(50, rng) for _ in range(500)}
    assert len(prompts) == 500
    assert len({p.split()[0] for p in prompts}) == 500
    assert all(len(p.split()) == 51 for p in prompts)


def test_max_tokens_jitter_preserves_mean():
    rng = random.Random(1)  # noqa: S311 -- test
    draws = [jittered_max_tokens(300, rng) for _ in range(20_000)]
    assert min(draws) >= 240 and max(draws) <= 360
    assert statistics.mean(draws) == pytest.approx(300, rel=0.01)
    assert jittered_max_tokens(1, rng) == 1


def test_two_point_probe_separates_overhead(vllm):
    calibration = probe_prompt_calibration(
        VllmClient(vllm.url, "m"),
        random.Random(0),  # noqa: S311 -- test
    )
    assert calibration.tokens_per_word == pytest.approx(4 / 3, rel=0.01)
    assert calibration.overhead_tokens == pytest.approx(11.0, abs=1.0)
    for target in (50, 900, 2000):
        words = calibration.words_for(target)
        achieved = fake_tokens("nonce " + " ".join(["w"] * words))
        assert abs(achieved - target) / target < 0.05, target


def test_probe_refuses_non_growing_prompts():
    class Flat:
        def complete(self, prompt, max_tokens):
            return infer_benchmark.CompletionResult(100, 1, 0.0)

    with pytest.raises(InferenceBenchmarkError, match="did not grow"):
        probe_prompt_calibration(Flat(), random.Random(0))  # noqa: S311 -- test


# ---------------------------------------------------------------------------
# Load loop
# ---------------------------------------------------------------------------


def test_closed_loop_keeps_n_in_flight_and_staggers(vllm, monkeypatch):
    monkeypatch.setattr(infer_benchmark, "STAGGER_S", 0.2)
    client = VllmClient(vllm.url, "m")
    first_submit = {}
    real_complete = client.complete

    def tracking(prompt, max_tokens):
        first_submit.setdefault(threading.current_thread().name, time.monotonic())
        return real_complete(prompt, max_tokens)

    client.complete = tracking
    load = LoadGenerator(client, "closed", 4, 10, 20, seed="s")
    load.start()
    time.sleep(0.4)
    load.stop()
    firsts = sorted(first_submit.values())
    assert len(firsts) == 4  # exactly N workers
    assert firsts[-1] - firsts[0] >= 0.1  # worker 3 starts ~0.15 s after worker 0
    assert len(load.records) > 20
    assert all(r.error is None for r in load.records)
    assert load.rejected == 0


def test_worker_survives_unexpected_exceptions(vllm):
    client = VllmClient(vllm.url, "m")
    client.complete = lambda *_a: 1 / 0
    load = LoadGenerator(client, "closed", 2, 1, 1, seed="s")
    load.start()
    time.sleep(0.05)
    assert load.workers_alive() == 2
    load.stop()
    assert load.records and all("division" in r.error for r in load.records)


def test_open_loop_poisson_rate(vllm):
    load = LoadGenerator(VllmClient(vllm.url, "m"), "open", 200.0, 5, 5, seed="s")
    load.start()
    time.sleep(1.0)
    load.stop()
    assert 120 <= len(load.records) <= 280


def test_open_loop_caps_in_flight(make_vllm, monkeypatch):
    slow = make_vllm(delay_s=0.5)
    monkeypatch.setattr(infer_benchmark, "MAX_IN_FLIGHT", 3)
    load = LoadGenerator(VllmClient(slow.url, "m"), "open", 200.0, 5, 5, seed="s")
    load.start()
    time.sleep(0.3)
    load.stop()
    assert load.rejected > 0
    assert load.rejected_between(0.0, 0.0) == 0


def test_load_generator_validates_mode(vllm):
    with pytest.raises(InferenceBenchmarkError):
        LoadGenerator(VllmClient(vllm.url, "m"), "burst", 1, 1, 1, seed="s")


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


def test_level_records_point_with_achieved_lengths(vllm):
    seen = {}

    def observe(prometheus, tp, selector, **kw):
        seen.update(kw, selector=selector, tp=tp)
        return _observed()

    result = _run(vllm, observe=observe)
    assert result.verdict == "recorded", result.reason
    assert result.point == _observed().point.as_row()
    assert result.mean_prompt_tokens == pytest.approx(900, rel=0.05)
    assert result.mean_output_tokens == pytest.approx(300, rel=0.1)
    assert result.ttft_p99_ms == 412.0
    assert not result.saturated  # closed loop never reports saturation
    assert seen["selector"] == VllmSelector(model_name="m", engine="0")
    assert seen["window_s"] == 120 and seen["min_requests"] == 20
    assert isinstance(seen["eval_time"], float)


def test_level_refused_by_observe_is_reported_not_raised(vllm):
    def refuse(*_a, **_k):
        raise InferenceObserveError("not steady", code="little")

    result = _run(vllm, observe=refuse)
    assert result.verdict == "refused"
    assert "not steady" in result.reason and result.point is None


def test_level_refused_on_prompt_drift(vllm):
    """A wrong tokens-per-word estimate misses P: the level must be refused."""
    wrong = PromptCalibration(tokens_per_word=2.0, overhead_tokens=11.0)
    result = _run(vllm, prompts=wrong)
    assert result.verdict == "refused"
    assert "prompt tokens" in result.reason
    assert result.point is None


def test_level_refused_on_truncated_completions(make_vllm):
    result = _run(make_vllm(truncate=True))
    assert result.verdict == "refused"
    assert "differ from their requested max_tokens" in result.reason


def test_level_refused_on_request_errors(make_vllm):
    result = _run(make_vllm(fail=True))
    assert result.verdict == "refused"
    assert result.completed == 0 and result.errors > 0


def test_open_level_saturation_from_client_side_signals(vllm, make_vllm):
    # Observe records a steady window, but the engine cannot keep up.
    slow = make_vllm(delay_s=0.3)
    result = _run(slow, mode="open", levels=(1000.0,), level=1000.0)
    assert result.offered > 20 and result.completed < 0.9 * result.offered
    assert result.saturated
    # A fast engine well below its capacity is not saturated. The rate stays
    # far under what the in-process fake serves on a starved CI runner, and
    # the longer hold still collects enough arrivals.
    fine = _run(vllm, mode="open", **_UNSATURATED)
    assert fine.offered > 20 and not fine.saturated, fine
    # A queue during the hold also means saturation, even at matching λ.
    queued = _run(
        vllm,
        mode="open",
        levels=(5.0,),
        level=5.0,
        observe=lambda *a, **k: _observed(waiting=6.0),
    )
    assert queued.saturated

    def preempted(*_a, **_k):
        raise InferenceObserveError("kv overflow", code="preemption")

    assert _run(
        vllm, mode="open", levels=(5.0,), level=5.0, observe=preempted
    ).saturated


def test_server_ttft_query_and_failure_mode():
    calls = []

    def scalar(base_url, query, token=None, eval_time=None):
        calls.append((query, eval_time))
        return 0.25

    sel = VllmSelector(model_name="m")
    assert server_ttft_p99_ms("http://p", sel, 60, 99.0, scalar) == 250.0
    query, at = calls[0]
    assert query.startswith("histogram_quantile(0.99, sum by (le) (rate(")
    assert (
        'vllm:time_to_first_token_seconds_bucket{model_name="m",engine="0"}[60s]'
        in query
    )
    assert at == 99.0

    def broken(*_a, **_k):
        from presidio_arch_translucency.prometheus import PrometheusError

        raise PrometheusError("down")

    assert server_ttft_p99_ms("http://p", sel, 60, 1.0, broken) is None


def test_open_sweep_stops_at_first_saturated_level(vllm):
    levels = []
    queues = iter([0.0, 6.0, 0.0])  # the second level builds a queue
    config = _config(
        vllm.url,
        mode="open",
        levels=(100.0, 110.0, 120.0),
        window_s=_UNSATURATED["window_s"],
        warmup_s=_UNSATURATED["warmup_s"],
    )
    report = run_benchmark(
        config,
        on_level=lambda result, current: levels.append(len(current.levels)),
        sleep=_fast_sleep,
        observe=lambda *a, **k: _observed(waiting=next(queues)),
        ttft=lambda *a, **k: None,
    )
    assert [lv.level for lv in report.levels] == [100.0, 110.0]
    assert report.stopped_early and levels == [1, 2]
    data = report.as_dict()
    assert data["schema"] == "presidio-hardened/inference-benchmark@1"
    assert "endpoint" not in data["config"] and "prometheus" not in data["config"]
    assert data["prompt_calibration"]["tokens_per_word"] == pytest.approx(
        4 / 3, rel=0.01
    )


def test_run_benchmark_fills_the_callers_report_before_failing(vllm):
    report = BenchmarkReport(config={})
    calls = iter([_observed(), RuntimeError("engine vanished")])

    def observe(*_a, **_k):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    with pytest.raises(RuntimeError):
        run_benchmark(
            _config(vllm.url, levels=(2.0, 4.0)),
            report=report,
            sleep=_fast_sleep,
            observe=observe,
            ttft=lambda *a, **k: None,
        )
    assert len(report.levels) == 1 and report.levels[0].verdict == "recorded"


@pytest.mark.parametrize(
    "kw",
    [
        {"levels": ()},
        {"levels": (2.5,)},
        {"mode": "open", "levels": (0.0,)},
        {"mode": "burst"},
        {"prompt_tokens": 0},
        {"output_tokens": True},
        {"warmup_s": -1},
        {"scrape_lag_s": 1000},
        {"tp": 3},
        {"window_s": 10},
        {"min_requests": 0},
        {"engine": "a\nb"},
    ],
)
def test_config_validation(kw):
    with pytest.raises(InferenceBenchmarkError):
        _config("http://x", **kw)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

runner = CliRunner()


def _cli(server, *extra):
    return runner.invoke(
        app,
        [
            "--skip-audit", "infer-benchmark", "--endpoint", server.url,
            "--prometheus", "http://prom.test", "--model", "m", "--tp", "1",
            "-p", "900", "-o", "300", "--warmup-s", "0", "--scrape-lag-s", "0",
            *extra,
        ],
    )  # fmt: skip


@pytest.fixture
def fast_harness(monkeypatch):
    monkeypatch.setattr(infer_benchmark, "_sleep", _fast_sleep)
    monkeypatch.setattr(
        infer_benchmark, "observe_vllm_window", lambda *a, **k: _observed()
    )
    monkeypatch.setattr(infer_benchmark, "server_ttft_p99_ms", lambda *a, **k: None)


def test_cli_records_points_and_writes_report(vllm, tmp_path, fast_harness):
    report = tmp_path / "report.json"
    result = _cli(vllm, "--concurrency", "2,4", "--report", str(report))
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert len(lines) == 2
    data = json.loads(report.read_text())
    assert [lv["verdict"] for lv in data["levels"]] == ["recorded", "recorded"]
    assert data["error"] is None
    assert oct(report.stat().st_mode & 0o777) == "0o600"


def test_cli_keeps_paid_levels_when_a_later_level_fails(
    vllm, tmp_path, fast_harness, monkeypatch
):
    calls = iter([_observed(), InferenceBenchmarkError("engine vanished")])

    def observe(*_a, **_k):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(infer_benchmark, "observe_vllm_window", observe)
    report = tmp_path / "report.json"
    result = _cli(vllm, "--concurrency", "2,4", "--report", str(report))
    assert result.exit_code == 2
    data = json.loads(report.read_text())
    assert len(data["levels"]) == 1
    assert data["stopped_early"] and "engine vanished" in data["error"]


def test_cli_interrupt_keeps_report(vllm, tmp_path, fast_harness, monkeypatch):
    def interrupt(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(infer_benchmark, "observe_vllm_window", interrupt)
    report = tmp_path / "report.json"
    result = _cli(vllm, "--concurrency", "2", "--report", str(report))
    assert result.exit_code == 130
    assert json.loads(report.read_text())["error"] == "interrupted"


def test_cli_refuses_existing_report_before_any_load(vllm, tmp_path):
    report = tmp_path / "report.json"
    report.write_text("keep me")
    result = _cli(vllm, "--concurrency", "2", "--report", str(report))
    assert result.exit_code == 2
    assert report.read_text() == "keep me"
    assert vllm.bodies == []


def test_cli_context_refusal_sends_no_load(make_vllm, tmp_path):
    server = make_vllm(max_model_len=512)
    report = tmp_path / "report.json"
    result = _cli(server, "--concurrency", "2", "--report", str(report))
    assert result.exit_code == 2
    assert server.bodies == []
    assert json.loads(report.read_text())["levels"] == []


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--concurrency", "2", "--rate", "1"],
        ["--concurrency", "two"],
        ["--concurrency", "2.5"],
        ["--concurrency", "2", "--window-s", "5"],
    ],
)
def test_cli_level_errors(vllm, extra):
    assert _cli(vllm, *extra).exit_code == 2
    assert vllm.bodies == []
