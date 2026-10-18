"""Tests for measured DCGM energy per benchmark level (infer_energy.py)."""

import json

import pytest

from presidio_arch_translucency import infer_benchmark, infer_energy
from presidio_arch_translucency.infer_benchmark import (
    BenchmarkConfig,
    InferenceBenchmarkError,
    LevelReport,
    run_benchmark,
)
from presidio_arch_translucency.infer_energy import (
    ENERGY_METRIC,
    EnergyMeter,
    InferenceEnergyError,
    drift_control,
    parse_energy_gpus,
)
from presidio_arch_translucency.prometheus import PrometheusError

U1 = "GPU-11111111-2222-3333-4444-555555555555"
U2 = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
U3 = "GPU-99999999-8888-7777-6666-555555555555"
WINDOW = 120
PROM = "http://prom.test:9090"


class FakeDcgm:
    """dcgm-exporter plus vLLM token counters behind one fake Prometheus."""

    def __init__(self, uuids, watts=250.0, idle_watts=60.0):
        self.loaded = True
        self.watts = dict.fromkeys(uuids, watts)
        self.idle_watts = dict.fromkeys(uuids, idle_watts)
        self.labels = {
            uuid: {
                "UUID": uuid,
                "gpu": str(i),
                "Hostname": "node-a",
                "modelName": "NVIDIA L40S",
                "DCGM_FI_DRIVER_VERSION": "580.65.06",
            }
            for i, uuid in enumerate(uuids)
        }
        self.extra_series: list[dict] = []
        # Per-UUID overrides of a reading, by query name (None drops it).
        self.override: dict[str, dict[str, float | None]] = {}
        self.tokens = {
            "vllm:generation_tokens_total": 12_000.0,
            "vllm:prompt_tokens_total": 36_000.0,
            "vllm:request_success_total": 40.0,
        }
        self.refuse_at: set[float] = set()
        self.busy_after = False
        self.token_series = 1.0
        self.token_resets = 0.0
        self.scalar_queries: list[str] = []
        self.fail = False
        self.meter = EnergyMeter(
            PROM, (tuple(uuids),), scalar_query=self.scalar, vector_query=self.vector
        )

    def readings(self, uuid):
        watts = self.watts[uuid] if self.loaded else self.idle_watts[uuid]
        values = {
            "energy_mj": watts * WINDOW * 1e3,
            "changes": WINDOW,
            "resets": 0.0,
            "series": 1.0,
            "samples": WINDOW / 5 + 1,  # 5 s scrape
            "power_mean_w": watts,
            "power_max_w": watts * 1.1,
            "util_mean": 80.0 if self.loaded else 0.0,
            "temp_mean_c": 61.0 if self.loaded else 38.0,
            "sm_clock_mean_mhz": 2490.0,
            "sm_clock_min_mhz": 2400.0,
            "power_limit_min_w": 350.0,
            "power_limit_max_w": 350.0,
        }
        values.update(self.override.get(uuid, {}))
        return values

    def vector(self, base_url, query, token=None, eval_time=None):
        if self.fail:
            raise PrometheusError("prometheus down")
        if query.startswith(ENERGY_METRIC):  # the gate: raw series
            return [(labels, 1.0) for labels in self.labels.values()] + [
                (labels, 1.0) for labels in self.extra_series
            ]
        names = {q: n for n, q in self.meter._queries(WINDOW).items()}
        name = names[query]
        out = []
        for uuid in self.watts:
            value = self.readings(uuid).get(name)
            if value is not None:
                out.append(({"UUID": uuid}, value))
        return out

    def scalar(self, base_url, query, token=None, eval_time=None):
        self.scalar_queries.append(query)
        if query.startswith("count(count_over_time("):
            return self.token_series
        if "resets(" in query:
            return self.token_resets
        for metric, value in self.tokens.items():
            if metric in query:
                return value
        raise AssertionError(query)


def gated(uuids=(U1,), **kw):
    fake = FakeDcgm(list(uuids), **kw)
    fake.meter.gate()
    return fake


def with_idle(fake):
    fake.loaded = False
    fake.meter.idle = fake.meter.measure_idle(WINDOW, 1.0)
    fake.loaded = True
    return fake


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_groups_per_engine():
    assert parse_energy_gpus([f"{U1},{U2}"], 2, 1) == ((U1, U2),)
    assert parse_energy_gpus([U1, f" {U2} "], 1, 2) == ((U1,), (U2,))


@pytest.mark.parametrize(
    ("groups", "tp", "instances", "match"),
    [
        ([U1], 1, 2, "1 --energy-gpu group"),
        ([U1], 2, 1, "has 2 GPU"),
        (["GPU-123"], 1, 1, "not a GPU UUID"),
        (["MIG-11111111-2222-3333-4444-555555555555"], 1, 1, "MIG"),
        ([f"{U1},{U1.lower().replace('gpu', 'GPU')}"], 2, 1, "named twice"),
        ([U2, U2.upper().replace("GPU-", "GPU-")], 1, 2, "named twice"),
        ([f'{U1}"'], 1, 1, "not a GPU UUID"),
    ],
)
def test_parse_refusals(groups, tp, instances, match):
    with pytest.raises(InferenceEnergyError, match=match):
        parse_energy_gpus(groups, tp, instances)


def test_config_refuses_bad_energy_gpus():
    base = {
        "endpoint": "http://vllm.test",
        "prometheus": PROM,
        "model": "m",
        "tp": 2,
        "prompt_tokens": 900,
        "output_tokens": 300,
        "mode": "open",
        "levels": (1.0,),
    }
    BenchmarkConfig(**base, energy_gpus=(f"{U1},{U2}",))
    with pytest.raises(InferenceBenchmarkError, match="has 2 GPU"):
        BenchmarkConfig(**base, energy_gpus=(U1,))
    with pytest.raises(InferenceBenchmarkError, match="tuple"):
        BenchmarkConfig(**base, energy_gpus=[U1])


# ---------------------------------------------------------------------------
# Gate (E1a, before any load)
# ---------------------------------------------------------------------------


def test_gate_records_labels():
    fake = gated((U1, U2))
    assert fake.meter.labels[U2] == {
        "engine": "0",
        "gpu": "1",
        "host": "node-a",
        "model_name": "NVIDIA L40S",
        "driver": "580.65.06",
    }
    described = fake.meter.describe()
    assert described["hosts"] == 1
    assert described["gpus"] == [[U1, U2]]
    assert described["meter"] == "dcgm"


def _gate_fails(fake, match):
    with pytest.raises(InferenceEnergyError, match=match):
        fake.meter.gate()


def test_gate_refuses_missing_and_duplicate_series():
    fake = FakeDcgm([U1, U2])
    del fake.labels[U2]
    _gate_fails(fake, f"0 {ENERGY_METRIC} series for GPU {U2}")
    fake = FakeDcgm([U1])
    fake.extra_series.append(dict(fake.labels[U1], instance="second-exporter"))
    _gate_fails(fake, "2 .* series")


def test_gate_refuses_estimator_and_unlabelled_series():
    fake = FakeDcgm([U1])
    fake.labels[U1]["components_power_source"] = "Estimator"
    _gate_fails(fake, "estimator tell")
    fake = FakeDcgm([U1])
    fake.labels[U1]["gpu"] = " "
    _gate_fails(fake, "no gpu label")
    fake = FakeDcgm([U1])
    del fake.labels[U1]["Hostname"]
    _gate_fails(fake, "no Hostname")


def test_gate_refuses_split_tp_group_and_mixed_models():
    fake = FakeDcgm([U1, U2])
    fake.labels[U2]["Hostname"] = "node-b"
    _gate_fails(fake, "2 hosts")
    # Replicas may sit on different hosts, but must be one GPU model.
    fake = FakeDcgm([U1, U2])
    fake.meter.groups = ((U1,), (U2,))
    fake.labels[U2]["Hostname"] = "node-b"
    fake.meter.gate()
    assert fake.meter.describe()["hosts"] == 2
    fake.labels[U2]["modelName"] = "NVIDIA H100"
    _gate_fails(fake, "different models")


def test_gate_wraps_prometheus_errors():
    fake = FakeDcgm([U1])
    fake.fail = True
    _gate_fails(fake, "prometheus down")


# ---------------------------------------------------------------------------
# Idle baseline
# ---------------------------------------------------------------------------


def test_idle_baseline_and_busy_gpu():
    fake = with_idle(gated((U1, U2)))
    assert fake.meter.idle[U1]["watts"] == pytest.approx(60.0)
    assert fake.meter.idle[U1]["power_limit_w"] == 350.0
    fake.loaded = False
    fake.override[U2] = {"util_mean": 30.0}
    with pytest.raises(InferenceEnergyError, match="another workload"):
        fake.meter.measure_idle(WINDOW, 1.0)


def test_idle_baseline_refuses_stale_counter():
    fake = gated()
    fake.loaded = False
    fake.override[U1] = {"changes": 4.0}  # 30 s exporter cadence
    with pytest.raises(InferenceEnergyError, match="collect-interval"):
        fake.meter.measure_idle(WINDOW, 1.0)


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------


def _level(fake, steady=True):
    return fake.meter.level(WINDOW, 1.0, '{model_name="m"}', steady=steady)


def test_level_records_joules_per_output_token():
    fake = with_idle(gated((U1, U2)))
    record = _level(fake)
    assert record["verdict"] == "recorded", record["reason"]
    assert record["joules"] == pytest.approx(2 * 250.0 * WINDOW)
    assert record["watts"] == pytest.approx(500.0)
    assert record["j_per_output_token"] == pytest.approx(500.0 * WINDOW / 12_000)
    assert record["j_per_request"] == pytest.approx(500.0 * WINDOW / 40)
    assert record["prompt_tokens"] == 36_000.0
    gpu = record["gpus"][1]
    assert gpu["uuid"] == U2
    assert gpu["host"] == "node-a"
    assert gpu["power_check"] == pytest.approx(0.0)
    assert gpu["temp_mean_c"] == 61.0
    json.dumps(record)  # report-serialisable


def test_level_keeps_energy_of_a_refused_capacity_window():
    record = _level(with_idle(gated()), steady=False)
    assert record["verdict"] == "recorded"
    assert record["steady"] is False


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"changes": 11.0}, "under one per 10s"),
        ({"resets": 1.0}, "reset"),
        ({"series": 2.0}, "2 series"),
        ({"power_mean_w": 200.0}, "disagree by \\+?-20%"),
        ({"power_limit_min_w": 300.0}, "power limit changed"),
        ({"power_limit_min_w": 300.0, "power_limit_max_w": 300.0}, "differs from idle"),
        ({"util_mean": 1.0}, "did not land"),
        ({"energy_mj": 70.0 * WINDOW * 1e3, "power_mean_w": 70.0}, "did not land"),
        ({"energy_mj": 0.0}, "no energy recorded"),
    ],
)
def test_level_energy_refusals(override, match):
    fake = with_idle(gated())
    fake.override[U1] = override
    record = _level(fake)
    assert record["verdict"] == "refused"
    assert record["reason"] is not None
    assert __import__("re").search(match, record["reason"]), record["reason"]


def test_level_refuses_gpus_with_different_power_limits():
    fake = gated((U1, U2))
    fake.override[U2] = {"power_limit_min_w": 300.0, "power_limit_max_w": 300.0}
    record = _level(fake)
    assert "one power limit" in record["reason"]


def test_level_refuses_missing_series_without_raising():
    fake = with_idle(gated((U1, U2)))
    fake.override[U2] = {"energy_mj": None}
    record = _level(fake)
    assert record["verdict"] == "refused"
    assert f"GPU {U2} has no energy_mj" in record["reason"]
    assert record["gpus"] is None
    fake.fail = True
    assert "prometheus down" in _level(fake)["reason"]


def test_level_refuses_without_output_tokens():
    fake = with_idle(gated())
    fake.tokens["vllm:generation_tokens_total"] = 0.0
    record = _level(fake)
    assert record["verdict"] == "refused"
    assert "no output tokens" in record["reason"]
    assert record["joules"] == pytest.approx(250.0 * WINDOW)


def test_level_queries_select_only_the_named_gpus():
    fake = gated((U1, U2))
    queries = fake.meter._queries(WINDOW)
    assert all(f'{{UUID=~"{U1}|{U2}"}}' in q for q in queries.values())
    assert queries["energy_mj"] == (
        f'sum by (UUID) (increase({ENERGY_METRIC}{{UUID=~"{U1}|{U2}"}}[120s]))'
    )


def test_drift_control():
    first = {"verdict": "recorded", "watts": 400.0}
    assert drift_control(first, {"verdict": "recorded", "watts": 410.0}) == {
        "watts_first": 400.0,
        "watts_repeat": 410.0,
        "drift": pytest.approx(0.025),
        "drifted": False,
        "tolerance": 0.05,
    }
    assert drift_control(first, {"verdict": "recorded", "watts": 440.0})["drifted"]
    refused = drift_control(first, {"verdict": "refused", "watts": 440.0})
    assert refused["drift"] is None
    assert refused["drifted"] is None
    assert drift_control(None, None)["watts_first"] is None


# ---------------------------------------------------------------------------
# Harness integration
# ---------------------------------------------------------------------------


class _Client:
    def __init__(self):
        self.calls = 0

    def max_model_len(self):
        self.calls += 1
        return None


def _energy_config(**kw):
    base = {
        "endpoint": "http://vllm.test",
        "prometheus": PROM,
        "model": "m",
        "tp": 1,
        "prompt_tokens": 900,
        "output_tokens": 300,
        "mode": "open",
        "levels": (1.0, 2.0),
        "window_s": WINDOW,
        "scrape_lag_s": 0,
        "energy_gpus": (U1,),
    }
    return BenchmarkConfig(**{**base, **kw})


@pytest.fixture
def scripted(monkeypatch):
    """A run whose levels and probes are fakes and whose energy is real."""
    fake = FakeDcgm([U1])
    events: list[str] = []

    def probe(client, rng):
        events.append("probe")
        return infer_benchmark.PromptCalibration(4 / 3, 11.0)

    def level(config, client, lam, prompts, refine=False, energy=None, **_kw):
        events.append(f"level {lam}")
        assert energy is not None
        fake.loaded = lam not in fake.refuse_at  # unloaded: landing refused
        record = energy.level(config.window_s, 1.0, "{}", steady=True)
        fake.loaded = False
        return LevelReport(
            "open", lam, 1, config.window_s, 40, 0, 40, 0, 0.33, None, None,
            900.0, 300.0, None, "recorded", energy=record,
        )  # fmt: skip

    real_idle = infer_benchmark._idle_window

    def idle(config, meter, level_kwargs):
        events.append("idle")
        fake.loaded = False
        if fake.busy_after and events.count("idle") == 2:
            fake.override[U1] = {"util_mean": 50.0}
        return real_idle(config, meter, level_kwargs)

    monkeypatch.setattr(infer_benchmark, "probe_prompt_calibration", probe)
    monkeypatch.setattr(infer_benchmark, "run_level", level)
    monkeypatch.setattr(infer_benchmark, "_idle_window", idle)
    return fake, events


def test_run_records_idle_levels_drift_and_idle_after(scripted):
    fake, events = scripted
    report = run_benchmark(
        _energy_config(),
        client=[_Client()],
        energy_meter=fake.meter,
        sleep=lambda _s: None,
        wall_clock=lambda: 1.0,
    )
    assert events == ["probe", "idle", "level 1.0", "level 2.0", "level 1.0", "idle"]
    assert [lv.level for lv in report.levels] == [1.0, 2.0]
    energy = report.as_dict()["energy"]
    assert energy["idle_before"][U1]["watts"] == pytest.approx(60.0)
    assert energy["idle_after"][U1]["watts"] == pytest.approx(60.0)
    drift = energy["drift_control"]
    assert drift["level"] == 1.0
    assert drift["drift"] == pytest.approx(0.0)
    assert drift["repeat"]["energy"]["verdict"] == "recorded"
    assert report.levels[0].energy["verdict"] == "recorded"
    assert report.config["energy_gpus"] == (U1,)


def test_run_refuses_before_load_when_the_gate_fails(scripted):
    fake, events = scripted
    del fake.labels[U1]
    with pytest.raises(InferenceBenchmarkError, match="0 .* series"):
        run_benchmark(_energy_config(), client=[_Client()], energy_meter=fake.meter)
    assert events == []


def test_run_refuses_a_busy_idle_baseline(scripted):
    fake, events = scripted
    fake.override[U1] = {"util_mean": 50.0}
    with pytest.raises(InferenceBenchmarkError, match="idle baseline refused"):
        run_benchmark(
            _energy_config(),
            client=[_Client()],
            energy_meter=fake.meter,
            sleep=lambda _s: None,
            wall_clock=lambda: 1.0,
        )
    assert events == ["probe", "idle"]


def test_run_without_energy_has_no_energy_section(scripted, monkeypatch):
    _fake, events = scripted

    def level(config, client, lam, prompts, refine=False, **kw):
        assert "energy" not in kw
        events.append(f"level {lam}")
        return LevelReport(
            "open", lam, 1, 120, 40, 0, 40, 0, 0.33, None, None, 900.0, 300.0,
            None, "recorded",
        )  # fmt: skip

    monkeypatch.setattr(infer_benchmark, "run_level", level)
    report = run_benchmark(_energy_config(energy_gpus=()), client=[_Client()])
    assert events == ["probe", "level 1.0", "level 2.0"]
    assert report.as_dict()["energy"] is None


def test_default_meter_uses_module_queries(monkeypatch):
    fake = FakeDcgm([U1])
    monkeypatch.setattr(infer_energy, "instant_query_vector", fake.vector)
    meter = EnergyMeter(PROM, ((U1,),))
    meter.gate()
    assert meter.labels[U1]["gpu"] == "0"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*extra):
    from typer.testing import CliRunner  # noqa: PLC0415

    from presidio_arch_translucency.cli import app  # noqa: PLC0415

    return CliRunner().invoke(
        app,
        [
            "--skip-audit", "infer-benchmark", "--endpoint", "http://vllm.test",
            "--prometheus", PROM, "--model", "m", "--tp", "1", "-p", "900",
            "-o", "300", "--warmup-s", "0", "--scrape-lag-s", "0",
            "--window-s", str(WINDOW), "--rate", "1,2", *extra,
        ],
    )  # fmt: skip


def test_cli_prints_energy_and_writes_it_to_the_report(scripted, monkeypatch, tmp_path):
    fake, _events = scripted
    monkeypatch.setattr(infer_energy, "instant_query_vector", fake.vector)
    monkeypatch.setattr(infer_energy, "instant_query", fake.scalar)
    monkeypatch.setattr(infer_benchmark, "check_context_fits", lambda *a: None)
    monkeypatch.setattr(infer_benchmark, "_sleep", lambda _s: None)
    report = tmp_path / "report.json"
    result = _cli("--energy-gpu", U1, "--report", str(report))
    assert result.exit_code == 0, result.output
    assert "J/output token" in result.output
    assert "Idle before: 60 W on 1 GPU(s), 1 host(s)" in result.output
    assert "Drift control: first level repeated at +0.0% watts" in result.output
    data = json.loads(report.read_text())
    assert data["energy"]["idle_after"][U1]["watts"] == pytest.approx(60.0)
    assert data["levels"][0]["energy"]["verdict"] == "recorded"


def test_cli_prints_energy_refusals(scripted, monkeypatch):
    fake, _events = scripted
    fake.override[U1] = {"changes": 3.0}
    monkeypatch.setattr(infer_energy, "instant_query_vector", fake.vector)
    monkeypatch.setattr(infer_energy, "instant_query", fake.scalar)
    monkeypatch.setattr(infer_benchmark, "check_context_fits", lambda *a: None)
    monkeypatch.setattr(infer_benchmark, "_sleep", lambda _s: None)
    result = _cli("--energy-gpu", U1)
    assert result.exit_code == 2
    assert "idle baseline refused" in result.output


def test_cli_refuses_bad_energy_gpus_before_load(scripted):
    _fake, events = scripted
    result = _cli("--energy-gpu", "MIG-11111111-2222-3333-4444-555555555555")
    assert result.exit_code == 2
    assert "MIG" in result.output
    assert events == []


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------


def test_level_refuses_token_counters_from_other_servers():
    fake = with_idle(gated())
    fake.token_series = 2.0  # a second server of the model on this Prometheus
    record = _level(fake, steady=False)
    assert record["verdict"] == "refused"
    assert "2 vLLM token counter series for 1 engine" in record["reason"]
    fake.token_series = 2.0
    assert (
        fake.meter.level(WINDOW, 1.0, "{}", steady=True, engines=2)["verdict"]
        == "recorded"
    )
    fake.token_series, fake.token_resets = 1.0, 1.0
    assert "reset" in _level(fake)["reason"]


def test_level_records_without_a_power_limit_metric():
    fake = gated((U1, U2))
    for uuid in (U1, U2):
        fake.override[uuid] = {"power_limit_min_w": None, "power_limit_max_w": None}
    with_idle(fake)
    record = _level(fake)
    assert record["verdict"] == "recorded", record["reason"]
    assert record["gpus"][0]["power_limit_w"] is None
    fake.override[U2] = {}  # one GPU reports a limit, the other does not
    assert "one power limit" in _level(fake)["reason"]


def test_cadence_reason_names_the_scrape_interval():
    fake = with_idle(gated())
    fake.override[U1] = {"changes": 7.0, "samples": 9.0}  # 15 s scrape
    reason = _level(fake)["reason"]
    assert "(9 samples)" in reason
    assert "Prometheus scrape interval too long" in reason
    fake.override[U1] = {"changes": 3.0}
    assert "--collect-interval" in _level(fake)["reason"]


def test_gate_message_mentions_mig():
    fake = FakeDcgm([U1])
    fake.extra_series.append(dict(fake.labels[U1], GPU_I_ID="1"))
    _gate_fails(fake, "MIG off")


def test_drift_repeats_the_first_clean_level(scripted):
    fake, events = scripted
    fake.refuse_at = {1.0}
    report = run_benchmark(
        _energy_config(),
        client=[_Client()],
        energy_meter=fake.meter,
        sleep=lambda _s: None,
        wall_clock=lambda: 1.0,
    )
    assert events[-2:] == ["level 2.0", "idle"]
    drift = report.energy["drift_control"]
    assert drift["level"] == 2.0
    assert drift["drift"] == pytest.approx(0.0)


def test_real_run_level_hands_the_window_to_the_meter():
    from presidio_arch_translucency.infer_benchmark import (  # noqa: PLC0415
        VllmClient,
        run_level,
    )
    from presidio_arch_translucency.infer_observe import (  # noqa: PLC0415
        InferenceObserveError,
    )
    from tests.test_infer_benchmark import (  # noqa: PLC0415
        EXACT,
        FakeVllm,
        _config,
        _fast_sleep,
        _observed,
    )

    server = FakeVllm()
    try:
        fake = with_idle(gated())
        config = _config(server.url, energy_gpus=(U1,))

        def run(observe):
            return run_level(
                config,
                VllmClient(server.url, "m"),
                2.0,
                EXACT,
                sleep=_fast_sleep,
                wall_clock=lambda: 1234.0,
                observe=observe,
                ttft=lambda *a, **k: None,
                ttft_cdf=lambda *a, **k: None,
                energy=fake.meter,
            )

        level = run(lambda *a, **k: _observed())
        assert level.verdict == "recorded"
        assert level.energy["verdict"] == "recorded"
        assert level.energy["steady"] is True
        assert level.energy["eval_time"] == 1234.0
        assert any(
            'vllm:generation_tokens_total{model_name="m",engine="0"}[120s]' in q
            for q in fake.scalar_queries
        )

        def refuse(*_a, **_k):
            raise InferenceObserveError("ramp", code="refused")

        level = run(refuse)
        assert level.verdict == "refused"
        assert level.energy["steady"] is False
        assert level.energy["verdict"] == "recorded"
    finally:
        server.close()


def test_cli_prints_an_idle_after_refusal(scripted, monkeypatch):
    fake, _events = scripted
    fake.busy_after = True
    monkeypatch.setattr(infer_energy, "instant_query_vector", fake.vector)
    monkeypatch.setattr(infer_energy, "instant_query", fake.scalar)
    monkeypatch.setattr(infer_benchmark, "check_context_fits", lambda *a: None)
    monkeypatch.setattr(infer_benchmark, "_sleep", lambda _s: None)
    result = _cli("--energy-gpu", U1)
    assert result.exit_code == 0, result.output
    assert "Idle after: idle baseline refused" in result.output
    assert "another workload" in result.output
