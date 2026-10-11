"""Tests for vLLM calibration windows from Prometheus (infer_observe.py)."""

import json

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency import infer_observe
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.infer_observe import (
    InferenceObserveError,
    VllmSelector,
    observe_vllm_replicas,
    observe_vllm_window,
    vllm_queries,
)
from presidio_arch_translucency.prometheus import PrometheusError

URL = "http://prom.test:9090"

#: A healthy low-load window: TPOT 30 ms, prefill 60 ms, 900/300 tokens.
#: Completions, batch spread and e2e latency are derived to be consistent
#: with Little's law unless a test overrides them.
HEALTHY = {
    "series_over_window": 1.0,
    "batch": 2.5,
    "waiting": 0.0,
    "kv_usage": 0.012,
    "tpot_s": 0.030,
    "prefill_s": 0.060,
    "prompt_tokens": 900.0,
    "output_tokens": 300.0,
    "resets": 0.0,
    "preemptions": 0.0,
    "prefix_cache_hit_rate": None,
    "spec_decode_drafts": None,
}
EVAL_AT = 1_791_000_000.0


class FakePrometheus:
    """Answers each PromQL query by its key in :func:`vllm_queries`."""

    def __init__(self, values=None, engines=1, selector=None, window_s=120):
        merged = {**HEALTHY, **(values or {})}
        batch = merged["batch"] or 0.0
        service = (merged["prefill_s"] or 0.0) + merged["output_tokens"] * (
            merged["tpot_s"] or 0.0
        )
        derived = {
            "requests": window_s * batch / service if service else 0.0,
            "batch_min": 0.8 * batch,
            "batch_stddev": 0.1 * batch,
            "e2e_s": service,
        }
        self.values = {**derived, **merged}
        self.engines = [({"engine": str(i)}, 1.0) for i in range(engines)]
        self.by_query = {
            query: name
            for name, query in vllm_queries(
                selector or VllmSelector(), window_s
            ).items()
        }
        self.calls = []  # (key, eval_time) in call order
        self.tokens = set()

    def scalar(self, base_url, query, token=None, eval_time=None):
        self.calls.append((self.by_query[query], eval_time))
        self.tokens.add(token)
        return self.values[self.by_query[query]]

    def vector(self, base_url, query, token=None, eval_time=None):
        self.calls.append((self.by_query[query], eval_time))
        assert self.by_query[query] == "engines"
        return self.engines


def _observe(fake, **kw):
    kw.setdefault("eval_time", EVAL_AT)
    return observe_vllm_window(
        URL, 2, scalar_query=fake.scalar, vector_query=fake.vector, **kw
    )


class FakeReplicas:
    """n independent servers, each one ``engine="0"`` under its own instance."""

    RUNNING = 'vllm:num_requests_running{model_name="m"}'

    def __init__(self, per_engine, series=None, over_window=None):
        self.fakes = {
            f"host{i}:8000": FakePrometheus(
                values,
                selector=VllmSelector(
                    model_name="m", engine="0", instance=f"host{i}:8000"
                ),
            )
            for i, values in enumerate(per_engine)
        }
        self.series = series or [
            ({"instance": name, "engine": "0", "job": "vllm"}, 1.0)
            for name in self.fakes
        ]
        self.over_window = (
            float(len(self.fakes)) if over_window is None else over_window
        )
        self.at = set()

    def _fake(self, query):
        return next(f for f in self.fakes.values() if query in f.by_query)

    def scalar(self, base_url, query, token=None, eval_time=None):
        self.at.add(eval_time)
        if query == f"count(count_over_time({self.RUNNING}[120s]))":
            return self.over_window
        return self._fake(query).scalar(base_url, query, token, eval_time)

    def vector(self, base_url, query, token=None, eval_time=None):
        self.at.add(eval_time)
        if query == self.RUNNING:
            return self.series
        return self._fake(query).vector(base_url, query, token, eval_time)


def _replicas(fake, instances=2, **kw):
    kw.setdefault("eval_time", EVAL_AT)
    return observe_vllm_replicas(
        URL,
        1,
        instances,
        model_name="m",
        scalar_query=fake.scalar,
        vector_query=fake.vector,
        **kw,
    )


def test_replicas_pool_per_instance_at_one_instant():
    fake = FakeReplicas(
        [{"batch": 2.4, "tpot_s": 0.030}, {"batch": 2.6, "tpot_s": 0.032}]
    )
    observed = _replicas(fake)
    assert fake.at == {EVAL_AT}
    assert observed.instances == 2
    assert observed.point.batch == pytest.approx(2.5)
    # Token-weighted: the busier engine completed more requests.
    assert 31.0 < observed.point.tpot_ms < 32.0
    assert observed.point.prefill_ms == pytest.approx(60.0)
    assert observed.imbalance == pytest.approx(0.04)
    assert observed.requests == pytest.approx(
        sum(f.values["requests"] for f in fake.fakes.values())
    )
    engines = observed.raw["engines"]
    assert [e["instance"] for e in engines] == ["host0:8000", "host1:8000"]
    assert all(e["reason"] is None for e in engines)
    assert observed.raw["batch"] == pytest.approx(2.5)
    assert observed.raw["requests"] == pytest.approx(observed.requests)


def test_replica_waiting_is_the_worst_engine():
    fake = FakeReplicas([{"waiting": 0.0}, {"waiting": 0.4}])
    observed = _replicas(fake)
    assert observed.waiting == pytest.approx(0.4)
    assert not observed.prefill_included and observed.point.prefill_ms is None


@pytest.mark.parametrize(
    ("fake", "match"),
    [
        (FakeReplicas([{}]), "expected exactly 2 replicas"),
        (FakeReplicas([{}, {}, {}]), "expected exactly 2 replicas"),
        (
            FakeReplicas(
                [{}, {}],
                series=[({"instance": "a", "engine": "0"}, 1.0)] * 2,
            ),
            "1 distinct",
        ),
        (FakeReplicas([{}, {}], over_window=3.0), "appeared during the window"),
    ],
)
def test_replica_set_must_be_exactly_n_engines(fake, match):
    with pytest.raises(InferenceObserveError, match=match):
        _replicas(fake)


def test_replicas_with_and_without_instance_labels_are_read_not_crashed():
    fake = FakeReplicas([{}, {}])
    fake.series = [
        ({"engine": "0"}, 1.0),
        ({"instance": "host1:8000", "engine": "0"}, 1.0),
    ]
    # The unlabelled engine cannot be read on its own: a refusal, not a crash.
    with pytest.raises(InferenceObserveError, match="instance label"):
        _replicas(fake)


def test_imbalance_limit_scales_with_replicas():
    assert infer_observe.replica_imbalance_limit(2) == pytest.approx(0.25)
    assert infer_observe.replica_imbalance_limit(8) == pytest.approx(0.25 * 3**0.5)
    four = FakeReplicas(
        [{"batch": 2.0}, {"batch": 2.0}, {"batch": 2.0}, {"batch": 2.8}]
    )
    four.over_window = 4.0
    # 0.8/2.2 − 1 ≈ 27 %: refused for two engines, within the 35 % for four.
    assert _replicas(four, instances=4).imbalance == pytest.approx(2.8 / 2.2 - 1)


def test_one_overloaded_replica_refuses_the_window_with_its_code():
    fake = FakeReplicas([{"resets": 1.0}, {"preemptions": 2.0}])
    with pytest.raises(InferenceObserveError) as info:
        _replicas(fake)
    assert info.value.code == "preemption"
    assert "host0:8000" in str(info.value) and "host1:8000" in str(info.value)
    engines = info.value.raw["engines"]
    assert engines[0]["reason"] and engines[1]["reason"]
    assert info.value.raw["preemptions"] == 2.0


def test_unbalanced_replicas_are_refused():
    fake = FakeReplicas([{"batch": 2.0}, {"batch": 3.5}])
    with pytest.raises(InferenceObserveError, match="not split evenly") as info:
        _replicas(fake)
    assert info.value.code == "imbalance"
    assert info.value.raw["imbalance"] == pytest.approx(1.5 / 5.5)


@pytest.mark.parametrize("instances", [1, 9, True, 2.0])
def test_replica_count_bounds(instances):
    with pytest.raises(InferenceObserveError, match="instances"):
        _replicas(FakeReplicas([{}, {}]), instances=instances)


def test_replica_transport_errors_become_refusals():
    def broken(*_a, **_k):
        raise PrometheusError("down")

    with pytest.raises(InferenceObserveError, match="down"):
        observe_vllm_replicas(
            URL, 1, 2, scalar_query=broken, vector_query=broken, eval_time=EVAL_AT
        )


def test_instance_label_is_escaped():
    selector = VllmSelector(model_name="m", engine="0", instance='h"1\\')
    assert selector.promql() == '{model_name="m",engine="0",instance="h\\"1\\\\"}'


def test_healthy_low_load_window_includes_prefill_and_kv_usage():
    observed = _observe(FakePrometheus())
    p = observed.point
    assert (p.tp, p.batch, p.prompt_tokens, p.output_tokens) == (2, 2.5, 900, 300)
    assert p.tpot_ms == pytest.approx(30.0)
    assert p.prefill_ms == pytest.approx(60.0)
    assert p.kv_usage == pytest.approx(0.012)
    assert observed.prefill_included and observed.kv_usage_included
    assert observed.little_ratio == pytest.approx(1.0)
    assert observed.batch_cv == pytest.approx(0.1)


def test_engine_checked_first_and_all_queries_share_one_instant():
    fake = FakePrometheus()
    _observe(fake)
    assert fake.calls[0][0] == "engines"
    assert {at for _, at in fake.calls} == {EVAL_AT}
    assert len(fake.calls) == len(vllm_queries(VllmSelector(), 120))


def test_default_eval_time_is_now_and_shared(monkeypatch):
    monkeypatch.setattr(infer_observe.time, "time", lambda: 1234.5)
    fake = FakePrometheus()
    observed = observe_vllm_window(
        URL, 2, scalar_query=fake.scalar, vector_query=fake.vector
    )
    assert observed.eval_time == 1234.5
    assert {at for _, at in fake.calls} == {1234.5}


@pytest.mark.parametrize(
    "values", [{"waiting": 0.4}, {"batch": 24.0}, {"prefill_s": None}]
)
def test_prefill_omitted_under_load_or_when_absent(values):
    observed = _observe(FakePrometheus(values))
    assert observed.point.prefill_ms is None
    assert not observed.prefill_included


def test_kv_usage_can_be_omitted():
    observed = _observe(FakePrometheus(), include_kv_usage=False)
    assert observed.point.kv_usage is None and not observed.kv_usage_included


@pytest.mark.parametrize(
    ("fake", "match"),
    [
        (FakePrometheus(engines=0), "no vLLM engine"),
        (FakePrometheus(engines=2), "2 engine series"),
        (FakePrometheus({"preemptions": 3.0}), "preemption"),
        (FakePrometheus({"preemptions": 0.3}), "preemption"),
        (FakePrometheus({"requests": 5.0}), "only 5 requests"),
        (FakePrometheus({"requests": None}), "only 0 requests"),
        (FakePrometheus({"tpot_s": None, "requests": 0.0}), "only 0 requests"),
        (FakePrometheus({"batch": 0.0, "requests": 40.0}), "reached 0"),
        (FakePrometheus({"batch_min": 0.0}), "reached 0"),
        (FakePrometheus({"batch_stddev": 1.0}), "varied too much"),
        (FakePrometheus({"series_over_window": 2.0}), "2 engine series appeared"),
        (FakePrometheus({"resets": 1.0}), "counter reset"),
        (FakePrometheus({"requests": 60.0}), "Little's law"),
        (FakePrometheus({"batch": 8.0, "requests": 40.0}), "Little's law"),
        (FakePrometheus({"e2e_s": 30.0}), "shorter than 5"),
        (FakePrometheus({"prefix_cache_hit_rate": 0.4}), "prefix cache hit rate"),
        (FakePrometheus({"spec_decode_drafts": 12.0}), "speculative decoding"),
        (FakePrometheus({"tpot_s": None, "requests": 40.0}), "no data for tpot_s"),
        (FakePrometheus({"prompt_tokens": None}), "prompt_tokens"),
        (FakePrometheus({"kv_usage": 1.7}), "out of domain"),
    ],
)
def test_untrustworthy_windows_are_refused(fake, match):
    with pytest.raises(InferenceObserveError, match=match):
        _observe(fake)


def test_raw_readings_travel_with_points_and_refusals():
    observed = _observe(FakePrometheus())
    assert observed.raw["tpot_ms"] == pytest.approx(30.0)
    assert observed.raw["achieved_rps"] == pytest.approx(observed.requests / 120)
    # A refused window still says where the engine was, ungated.
    with pytest.raises(InferenceObserveError) as refused:
        _observe(FakePrometheus({"preemptions": 3.0, "batch": 3.0}))
    assert refused.value.code == "preemption"
    assert refused.value.raw["tpot_ms"] == pytest.approx(30.0)
    assert refused.value.raw["batch"] == 3.0
    assert refused.value.raw["preemptions"] == 3.0
    # Refusals before any reading carry none.
    with pytest.raises(InferenceObserveError) as no_engine:
        _observe(FakePrometheus(engines=0))
    assert no_engine.value.raw is None
    with pytest.raises(InferenceObserveError) as empty:
        _observe(FakePrometheus({"requests": None, "tpot_s": None}))
    assert empty.value.raw["achieved_rps"] is None
    assert empty.value.raw["tpot_ms"] is None


def test_cache_and_spec_decode_counters_at_zero_pass():
    observed = _observe(
        FakePrometheus({"prefix_cache_hit_rate": 0.002, "spec_decode_drafts": 0.0})
    )
    assert observed.point.tp == 2


def test_ramp_that_only_preemption_gating_would_accept_is_refused():
    """Load starts mid-window: mean batch halves, TPOT stays at full load."""
    ramp = FakePrometheus(
        {"batch": 12.0, "batch_min": 0.0, "batch_stddev": 12.0, "requests": 40.0}
    )
    with pytest.raises(InferenceObserveError):
        _observe(ramp)


def test_transport_errors_become_refusals():
    def broken(*_args, **_kw):
        raise PrometheusError("connection refused")

    with pytest.raises(InferenceObserveError, match="connection refused"):
        observe_vllm_window(URL, 1, scalar_query=broken, vector_query=broken)


def test_eval_time_reaches_the_query_url():
    from presidio_arch_translucency.prometheus import _build_query_url

    url = _build_query_url(URL, "up", 1791000000.5)
    assert "time=1791000000.5" in url
    assert "time=" not in _build_query_url(URL, "up")
    with pytest.raises(PrometheusError, match="evaluation time"):
        _build_query_url(URL, "up", float("nan"))


def test_queries_pin_vllm_031_names_and_window():
    q = vllm_queries(VllmSelector(model_name="meta/llama", engine="0"), 90)
    sel = '{model_name="meta/llama",engine="0"}'
    assert q["tpot_s"] == (
        f"sum(rate(vllm:inter_token_latency_seconds_sum{sel}[90s])) / "
        f"sum(rate(vllm:inter_token_latency_seconds_count{sel}[90s]))"
    )
    assert q["kv_usage"] == f"sum(avg_over_time(vllm:kv_cache_usage_perc{sel}[90s]))"
    assert q["requests"] == f"sum(increase(vllm:request_success_total{sel}[90s]))"
    assert "vllm:num_preemptions_total" in q["preemptions"]


def test_label_values_are_escaped():
    sel = VllmSelector(model_name='evil"} or vector(1) #\\', engine=None)
    assert sel.promql() == '{model_name="evil\\"} or vector(1) #\\\\"}'


@pytest.mark.parametrize(
    "kw",
    [
        {"model_name": "a\nb"},
        {"model_name": ""},
        {"engine": "x" * 300},
    ],
)
def test_selector_rejects_bad_label_values(kw):
    with pytest.raises(InferenceObserveError):
        VllmSelector(**kw)


@pytest.mark.parametrize("name", ["vllm:x{a}", "1bad", "", "a b", "x)" * 2])
def test_metric_overrides_must_be_metric_names(name):
    with pytest.raises(InferenceObserveError):
        vllm_queries(VllmSelector(), 120, tpot_metric=name)


def test_metric_overrides_reach_the_queries():
    q = vllm_queries(
        VllmSelector(),
        120,
        tpot_metric="vllm:time_per_output_token_seconds",
        kv_usage_metric="vllm:gpu_cache_usage_perc",
    )
    assert "vllm:time_per_output_token_seconds_sum" in q["tpot_s"]
    assert "vllm:gpu_cache_usage_perc" in q["kv_usage"]


@pytest.mark.parametrize("window", [10, 7200, True, 60.5])
def test_window_bounds(window):
    with pytest.raises(InferenceObserveError, match="window"):
        vllm_queries(VllmSelector(), window)


@pytest.mark.parametrize("minimum", [0, True, 2_000_000])
def test_min_requests_bounds(minimum):
    with pytest.raises(InferenceObserveError, match="min_requests"):
        _observe(FakePrometheus(), min_requests=minimum)


def test_token_requires_https(monkeypatch):
    monkeypatch.setenv("PAT_PROMETHEUS_TOKEN", "secret")
    with pytest.raises(InferenceObserveError, match="https"):
        _observe(FakePrometheus())
    fake = FakePrometheus()
    observe_vllm_window(
        "https://prom.test",
        1,
        scalar_query=fake.scalar,
        vector_query=fake.vector,
        eval_time=EVAL_AT,
    )
    assert fake.tokens == {"secret"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

runner = CliRunner()


def _patch(monkeypatch, fake):
    monkeypatch.setattr(infer_observe, "instant_query", fake.scalar)
    monkeypatch.setattr(infer_observe, "instant_query_vector", fake.vector)


def test_cli_prints_one_point_line(monkeypatch):
    _patch(monkeypatch, FakePrometheus())
    result = runner.invoke(
        app, ["--skip-audit", "infer-observe", "--prometheus", URL, "--tp", "2"]
    )
    assert result.exit_code == 0, result.output
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "tp": 2,
        "batch": 2.5,
        "prompt_tokens": 900.0,
        "output_tokens": 300.0,
        "tpot_ms": 30.0,
        "prefill_ms": 60.0,
        "kv_usage": 0.012,
    }


def test_cli_refusal_exits_2(monkeypatch):
    _patch(monkeypatch, FakePrometheus({"preemptions": 1.0}))
    result = runner.invoke(
        app, ["--skip-audit", "infer-observe", "--prometheus", URL, "--tp", "1"]
    )
    assert result.exit_code == 2
    assert "preemption" in result.output


def test_cli_rejects_unsupported_tp(monkeypatch):
    _patch(monkeypatch, FakePrometheus())
    result = runner.invoke(
        app, ["--skip-audit", "infer-observe", "--prometheus", URL, "--tp", "3"]
    )
    assert result.exit_code == 2


def test_cli_observed_points_feed_calibration(monkeypatch, tmp_path):
    """infer-observe output is a valid infer-calibrate points file line."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    lines = []
    for tp, batch, tpot in (
        (1, 2.0, 33.0),
        (1, 40.0, 42.0),
        (2, 3.0, 21.0),
        (2, 60.0, 27.0),
        (4, 3.5, 16.0),
    ):  # noqa: E501
        values = {"batch": batch, "tpot_s": tpot / 1e3, "kv_usage": None}
        _patch(monkeypatch, FakePrometheus(values))
        result = runner.invoke(
            app,
            ["--skip-audit", "infer-observe", "--prometheus", URL, "--tp", str(tp)],
        )
        assert result.exit_code == 0, result.output
        lines += [line for line in result.stdout.splitlines() if line.startswith("{")]
    points = tmp_path / "points.jsonl"
    points.write_text("\n".join(lines) + "\n")
    calibrated = runner.invoke(
        app,
        [
            "--skip-audit", "infer-calibrate", "--profile", "obs", "--points-file",
            str(points), "-w", "16", "-k", "131072", "-b", "864", "-m", "48",
            "--dry-run", "--json",
        ],
    )  # fmt: skip
    assert calibrated.exit_code == 0, calibrated.output
    assert len(json.loads(calibrated.stdout)["points"]) == 5
