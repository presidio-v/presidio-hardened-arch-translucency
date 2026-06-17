"""Tests for the read-only Prometheus exporter (`pat export`, v0.10.0)."""

import threading
import urllib.error
import urllib.request

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.export import (
    Metric,
    Sample,
    build_metrics,
    build_prediction_metrics,
    build_server,
    handle_request,
    is_loopback_host,
    prediction_metrics_from_result,
    render_exposition,
)
from presidio_arch_translucency.model import ReplicationLayer

_BUILD_SERVER = "presidio_arch_translucency.export.build_server"

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


# ── render_exposition ─────────────────────────────────────────────────────────


def test_render_exposition_basic_shape() -> None:
    text = render_exposition([Metric("pat_x", "an x", [Sample({"layer": "pod"}, 3.0)])])
    assert "# HELP pat_x an x" in text
    assert "# TYPE pat_x gauge" in text
    assert 'pat_x{layer="pod"} 3' in text
    assert text.endswith("\n")


def test_render_exposition_unlabeled_and_integer_formatting() -> None:
    text = render_exposition([Metric("pat_n", "n", [Sample({}, 5.0)])])
    # Whole-number floats render without a trailing .0
    assert "pat_n 5\n" in text


def test_render_exposition_float_value() -> None:
    text = render_exposition([Metric("pat_f", "f", [Sample({}, 0.1)])])
    assert "pat_f 0.1\n" in text


def test_render_exposition_escapes_label_values() -> None:
    text = render_exposition([Metric("pat_e", "h", [Sample({"k": 'a"b\\c'}, 1.0)])])
    assert 'k="a\\"b\\\\c"' in text


@pytest.mark.parametrize(
    "value,expected",
    [(float("nan"), "NaN"), (float("inf"), "+Inf"), (float("-inf"), "-Inf")],
)
def test_format_value_specials(value: float, expected: str) -> None:
    text = render_exposition([Metric("pat_s", "s", [Sample({}, value)])])
    assert f"pat_s {expected}\n" in text


# ── build_metrics ─────────────────────────────────────────────────────────────


def test_build_metrics_names_and_layers() -> None:
    metrics = build_metrics(500.0, 80.0, ReplicationLayer.CONTAINER)
    names = {m.name for m in metrics}
    assert {
        "pat_build_info",
        "pat_recommended_replicas",
        "pat_throughput_gain_ratio",
        "pat_response_time_ms",
        "pat_layer_recommended",
    } <= names
    # Per-layer metrics carry one sample per replication layer.
    replicas = next(m for m in metrics if m.name == "pat_recommended_replicas")
    assert {s.labels["layer"] for s in replicas.samples} == {
        "container",
        "pod",
        "deployment",
        "node",
    }


def test_build_metrics_exactly_one_recommended_layer() -> None:
    metrics = build_metrics(500.0, 80.0, ReplicationLayer.CONTAINER)
    recommended = next(m for m in metrics if m.name == "pat_layer_recommended")
    assert sum(s.value for s in recommended.samples) == 1.0


def test_build_metrics_renders_end_to_end() -> None:
    text = render_exposition(build_metrics(500.0, 80.0, ReplicationLayer.CONTAINER))
    assert 'pat_recommended_replicas{layer="container"}' in text
    assert "pat_build_info" in text


# ── is_loopback_host ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_is_loopback_host_true(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    ["10.0.0.1", "0.0.0.0", "example.com", "192.168.1.2"],  # noqa: S104
)
def test_is_loopback_host_false(host: str) -> None:
    assert is_loopback_host(host) is False


# ── handle_request (pure routing) ─────────────────────────────────────────────


def test_handle_request_metrics_calls_provider() -> None:
    calls: list[int] = []

    def _provider() -> str:
        calls.append(1)
        return "pat_x 1\n"

    status, content_type, body = handle_request("/metrics?foo=bar", _provider)
    assert status == 200
    assert content_type.startswith("text/plain; version=0.0.4")
    assert body == b"pat_x 1\n"
    assert calls == [1]  # query string stripped, provider hit once


def test_handle_request_health_and_root_and_404() -> None:
    assert handle_request("/health", lambda: "")[0] == 200
    assert handle_request("/", lambda: "")[0] == 200
    status, _, body = handle_request("/nope", lambda: "")
    assert status == 404
    assert body == b"not found\n"


# ── build_server (real socket, loopback ephemeral port) ───────────────────────


def test_build_server_serves_and_is_read_only() -> None:
    server = build_server("127.0.0.1", 0, lambda: "pat_marker 1\n")
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/metrics", timeout=5) as resp:  # noqa: S310
            assert resp.status == 200
            assert "pat_marker 1" in resp.read().decode()
            assert resp.headers["Content-Type"].startswith("text/plain; version=0.0.4")

        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/nope", timeout=5)  # noqa: S310
        assert missing.value.code == 404

        # Read-only: only GET is implemented, so POST yields 501.
        post = urllib.request.Request(  # noqa: S310
            f"{base}/metrics", method="POST", data=b"x"
        )
        with pytest.raises(urllib.error.HTTPError) as mutated:
            urllib.request.urlopen(post, timeout=5)  # noqa: S310
        assert mutated.value.code == 501
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ── pat export CLI ────────────────────────────────────────────────────────────


def test_export_once_prints_exposition(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke("export", "--once", "-r", "500", "-l", "80", "-c", "container")
    assert result.exit_code == 0, result.output
    assert "# TYPE pat_recommended_replicas gauge" in result.output
    assert 'pat_recommended_replicas{layer="container"}' in result.output


def test_export_rejects_non_loopback_without_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke(
        "export",
        "--once",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
        "--host",
        "10.0.0.1",
    )
    assert result.exit_code == 2
    assert "listen-public" in result.output


def test_export_invalid_layer_exits_2(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke("export", "--once", "-r", "500", "-l", "80", "-c", "bogus")
    assert result.exit_code == 2


# ── prediction metrics (Phase 2) ──────────────────────────────────────────────


def _result(**overrides):
    """Build an optimize.OptimizeResult with sensible defaults for tests."""
    from datetime import datetime, timezone

    from presidio_arch_translucency.optimize import OptimizeResult

    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    base = {
        "layer": "container",
        "samples": 12,
        "window_minutes": 55.0,
        "sma_rps": 480.0,
        "sma_latency_ms": 78.0,
        "trend_pct": 12.0,
        "slope_rps_per_min": 4.0,
        "horizon_minutes": 10.0,
        "predicted_rps": 540.0,
        "current_replicas": 4,
        "recommended_replicas": 6,
        "first_ts": now,
        "last_ts": now,
    }
    base.update(overrides)
    return OptimizeResult(**base)


def test_prediction_metrics_sma_result() -> None:
    metrics = prediction_metrics_from_result(_result(model="sma"))
    names = {m.name for m in metrics}
    assert {
        "pat_optimize_samples",
        "pat_observed_rps",
        "pat_predicted_rps",
        "pat_predicted_recommended_replicas",
        "pat_optimize_trend_ratio",
    } <= names
    # No CI metrics for a plain SMA result.
    assert "pat_predicted_rps_lower" not in names
    predicted = next(m for m in metrics if m.name == "pat_predicted_rps")
    assert predicted.samples[0].labels == {"model": "sma"}
    assert predicted.samples[0].value == 540.0


def test_prediction_metrics_arima_result_has_ci() -> None:
    metrics = prediction_metrics_from_result(
        _result(
            model="arima",
            predicted_rps_lower=500.0,
            predicted_rps_upper=600.0,
            recommended_replicas_lower=5,
            recommended_replicas_upper=8,
        )
    )
    names = {m.name for m in metrics}
    assert {
        "pat_predicted_rps_lower",
        "pat_predicted_rps_upper",
        "pat_predicted_recommended_replicas_lower",
        "pat_predicted_recommended_replicas_upper",
    } <= names


def _obs(ts_minute: int, rps: float):
    from datetime import datetime, timezone

    from presidio_arch_translucency.observe import Observation

    return Observation(
        timestamp=datetime(2026, 6, 17, 12, ts_minute, tzinfo=timezone.utc),
        rps=rps,
        avg_latency_ms=80.0,
        p99_latency_ms=160.0,
        throughput=rps,
        layer="container",
        replicas=4,
    )


def test_build_prediction_metrics_empty_emits_zero_samples() -> None:
    metrics = build_prediction_metrics([])
    assert len(metrics) == 1
    assert metrics[0].name == "pat_optimize_samples"
    assert metrics[0].samples[0].value == 0.0


def test_build_prediction_metrics_sma_over_observations() -> None:
    obs = [_obs(m, 400.0 + 10.0 * m) for m in range(0, 20, 2)]
    metrics = build_prediction_metrics(obs, model="sma", horizon_minutes=10.0)
    names = {m.name for m in metrics}
    assert "pat_predicted_rps" in names
    samples = next(m for m in metrics if m.name == "pat_optimize_samples")
    assert samples.samples[0].value == float(len(obs))
    predicted = next(m for m in metrics if m.name == "pat_predicted_rps")
    assert predicted.samples[0].labels == {"model": "sma"}


# ── pat export --predict CLI ──────────────────────────────────────────────────


def test_export_once_predict_with_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    from presidio_arch_translucency import observe

    db = tmp_path / "obs.db"
    for m in range(0, 20, 2):
        observe.record(
            rps=400.0 + 10.0 * m,
            avg_latency_ms=80.0,
            p99_latency_ms=160.0,
            throughput=400.0 + 10.0 * m,
            layer="container",
            replicas=4,
            db_path=db,
        )
    result = invoke(
        "export",
        "--once",
        "--predict",
        "--db",
        str(db),
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 0, result.output
    assert 'pat_predicted_rps{model="sma"}' in result.output
    assert "pat_optimize_samples 10" in result.output


def test_export_predict_empty_store_zero_samples(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke(
        "export",
        "--once",
        "--predict",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 0, result.output
    assert "pat_optimize_samples 0" in result.output
    assert "pat_predicted_rps{" not in result.output


def test_export_predict_unknown_model_exits_2(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke(
        "export",
        "--once",
        "--predict",
        "--model",
        "bogus",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 2
    assert "model" in result.output.lower()


class _FakeServer:
    def __init__(self) -> None:
        self.served = False
        self.closed = False

    def serve_forever(self) -> None:
        self.served = True
        raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed = True


def test_export_serve_mode_runs_and_closes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = _FakeServer()
    monkeypatch.setattr(_BUILD_SERVER, lambda *a, **k: fake)
    result = invoke("export", "-r", "500", "-l", "80", "-c", "container")
    assert result.exit_code == 0, result.output
    assert fake.served and fake.closed
    assert "serving read-only metrics" in result.output


def test_export_serve_public_warns(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    fake = _FakeServer()
    monkeypatch.setattr(_BUILD_SERVER, lambda *a, **k: fake)
    result = invoke(
        "export",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
        "--host",
        "10.0.0.1",
        "--listen-public",
    )
    assert result.exit_code == 0, result.output
    assert fake.served and fake.closed
