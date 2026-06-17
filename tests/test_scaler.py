"""Tests for the autoscaler emitter (`pat scaler`, v0.15.0)."""

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.scaler import (
    ScalerError,
    build_keda_scaledobject,
    build_prometheus_adapter,
    build_scaler,
    default_query,
)

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


# ── default_query ─────────────────────────────────────────────────────────────


def test_default_query_no_layer() -> None:
    assert default_query() == "max(pat_predicted_recommended_replicas)"


def test_default_query_with_layer() -> None:
    assert (
        default_query("pat_predicted_recommended_replicas", "container")
        == 'max(pat_predicted_recommended_replicas{layer="container"})'
    )


def test_default_query_bad_layer_raises() -> None:
    with pytest.raises(ScalerError):
        default_query("pat_predicted_recommended_replicas", "bogus")


# ── KEDA ScaledObject ─────────────────────────────────────────────────────────


def test_keda_scaledobject_shape() -> None:
    y = build_keda_scaledobject(
        "web",
        "http://prom:9090",
        "max(pat_predicted_recommended_replicas)",
        min_replicas=2,
        max_replicas=8,
        namespace="prod",
    )
    assert "apiVersion: keda.sh/v1alpha1" in y
    assert "kind: ScaledObject" in y
    assert 'name: "web-pat"' in y
    assert 'namespace: "prod"' in y
    assert '    name: "web"' in y  # scaleTargetRef
    assert "  minReplicaCount: 2" in y
    assert "  maxReplicaCount: 8" in y
    assert "type: prometheus" in y
    assert 'serverAddress: "http://prom:9090"' in y
    assert 'threshold: "1"' in y


def test_keda_query_quoted_and_escaped() -> None:
    y = build_keda_scaledobject(
        "web",
        "http://prom:9090",
        'max(pat_predicted_recommended_replicas{layer="container"})',
    )
    assert (
        'query: "max(pat_predicted_recommended_replicas{layer=\\"container\\"})"' in y
    )


def test_keda_rejects_bad_target() -> None:
    with pytest.raises(ScalerError):
        build_keda_scaledobject("Web_App", "http://prom", "q")


def test_keda_rejects_bad_url() -> None:
    with pytest.raises(ScalerError):
        build_keda_scaledobject("web", "ftp://prom", "q")


def test_keda_rejects_control_char_query() -> None:
    with pytest.raises(ScalerError):
        build_keda_scaledobject("web", "http://prom", "q\nbad")


def test_keda_clamps_max_below_min() -> None:
    y = build_keda_scaledobject(
        "web", "http://prom", "q", min_replicas=5, max_replicas=2
    )
    assert "  minReplicaCount: 5" in y
    assert "  maxReplicaCount: 5" in y


def test_keda_min_below_one_raises() -> None:
    with pytest.raises(ScalerError):
        build_keda_scaledobject("web", "http://prom", "q", min_replicas=0)


def test_keda_rejects_control_char_url() -> None:
    with pytest.raises(ScalerError, match="control characters"):
        build_keda_scaledobject("web", "http://prom\n", "q")


def test_keda_rejects_empty_query() -> None:
    with pytest.raises(ScalerError, match="non-empty"):
        build_keda_scaledobject("web", "http://prom", "   ")


# ── Prometheus-Adapter HPA ────────────────────────────────────────────────────


def test_prometheus_adapter_shape() -> None:
    y = build_prometheus_adapter("web", "max(pat_predicted_recommended_replicas)")
    assert "apiVersion: autoscaling/v2" in y
    assert "kind: HorizontalPodAutoscaler" in y
    assert "type: External" in y
    assert 'name: "pat_predicted_recommended_replicas"' in y
    assert "type: Value" in y
    assert 'value: "1"' in y
    # the adapter rule example is present (commented)
    assert "Prometheus Adapter rule" in y
    assert "externalRules" in y


def test_prometheus_adapter_with_namespace() -> None:
    y = build_prometheus_adapter("web", "q", namespace="prod")
    assert 'namespace: "prod"' in y


# ── build_scaler dispatch ─────────────────────────────────────────────────────


def test_build_scaler_dispatch() -> None:
    assert "ScaledObject" in build_scaler("keda", "web", "http://p", "q")
    assert "HorizontalPodAutoscaler" in build_scaler(
        "prometheus-adapter", "web", "http://p", "q"
    )


def test_build_scaler_bad_format_raises() -> None:
    with pytest.raises(ScalerError):
        build_scaler("nope", "web", "http://p", "q")


# ── pat scaler CLI ────────────────────────────────────────────────────────────


def test_scaler_cmd_keda(monkeypatch) -> None:
    result = invoke(
        "scaler", "-t", "web", "--prometheus-url", "http://prom:9090", "-c", "container"
    )
    assert result.exit_code == 0, result.output
    assert "kind: ScaledObject" in result.output
    assert 'layer=\\"container\\"' in result.output


def test_scaler_cmd_prometheus_adapter(monkeypatch) -> None:
    result = invoke(
        "scaler",
        "-t",
        "web",
        "--prometheus-url",
        "http://prom:9090",
        "--format",
        "prometheus-adapter",
    )
    assert result.exit_code == 0, result.output
    assert "kind: HorizontalPodAutoscaler" in result.output


def test_scaler_cmd_custom_query(monkeypatch) -> None:
    result = invoke(
        "scaler",
        "-t",
        "web",
        "--prometheus-url",
        "http://prom:9090",
        "--query",
        "sum(pat_predicted_rps)",
    )
    assert result.exit_code == 0, result.output
    assert "sum(pat_predicted_rps)" in result.output


def test_scaler_cmd_bad_format_exits_2(monkeypatch) -> None:
    result = invoke(
        "scaler", "-t", "web", "--prometheus-url", "http://p", "--format", "nope"
    )
    assert result.exit_code == 2


def test_scaler_cmd_bad_target_exits_2(monkeypatch) -> None:
    result = invoke("scaler", "-t", "Web_App", "--prometheus-url", "http://p")
    assert result.exit_code == 2


def test_scaler_cmd_bad_layer_exits_2(monkeypatch) -> None:
    result = invoke(
        "scaler", "-t", "web", "--prometheus-url", "http://p", "-c", "bogus"
    )
    assert result.exit_code == 2
