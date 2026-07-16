"""Tests for the energy scaling signal (`pat scaler --signal energy`, v0.22.0)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.scaler import (
    ENERGY_METRIC,
    VALID_SIGNALS,
    ScalerError,
    build_keda_scaledobject,
    build_scaler,
    default_query,
)

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


# ── default signal is byte-identical (no regression) ──────────────────────────


def test_energy_metric_matches_exporter_gauge() -> None:
    # Must equal export.py's gauge name exactly.
    assert ENERGY_METRIC == "pat_energy_per_request_joules"


def test_default_signal_keda_unchanged() -> None:
    explicit = build_scaler(
        "keda",
        "web",
        "http://prom:9090",
        "max(pat_predicted_recommended_replicas)",
        signal="replicas",
    )
    legacy = build_keda_scaledobject(
        "web", "http://prom:9090", "max(pat_predicted_recommended_replicas)"
    )
    assert explicit == legacy
    assert 'threshold: "1"' in explicit


def test_valid_signals() -> None:
    assert VALID_SIGNALS == ("replicas", "energy")


# ── energy signal — KEDA ──────────────────────────────────────────────────────


def test_energy_keda_query_and_threshold() -> None:
    query = default_query(ENERGY_METRIC, "container")
    y = build_scaler(
        "keda",
        "web",
        "http://prom:9090",
        query,
        signal="energy",
        energy_budget_j_per_req=0.5,
    )
    assert 'query: "max(pat_energy_per_request_joules{layer=\\"container\\"})"' in y
    assert 'threshold: "0.5"' in y
    # Caveat + emit-only notes present.
    assert "EEI > 1" in y
    assert "A1/E1" in y
    assert "never actuates" in y


def test_energy_adapter_value_and_metric() -> None:
    query = default_query(ENERGY_METRIC)
    y = build_scaler(
        "prometheus-adapter",
        "web",
        "http://prom:9090",
        query,
        metric=ENERGY_METRIC,
        signal="energy",
        energy_budget_j_per_req=0.25,
    )
    assert 'value: "0.25"' in y
    assert "pat_energy_per_request_joules" in y
    assert "kind: HorizontalPodAutoscaler" in y
    assert "EEI > 1" in y


def test_energy_signal_requires_budget() -> None:
    with pytest.raises(ScalerError):
        build_scaler(
            "keda",
            "web",
            "http://prom:9090",
            default_query(ENERGY_METRIC),
            signal="energy",
        )


@pytest.mark.parametrize("bad", [0.0, -1.0, float("inf"), float("nan")])
def test_energy_budget_rejects_nonpositive_or_nonfinite(bad: float) -> None:
    with pytest.raises(ScalerError):
        build_scaler(
            "keda",
            "web",
            "http://prom:9090",
            default_query(ENERGY_METRIC),
            signal="energy",
            energy_budget_j_per_req=bad,
        )


def test_unknown_signal_raises() -> None:
    with pytest.raises(ScalerError):
        build_scaler("keda", "web", "http://prom:9090", "q", signal="bogus")


# ── CLI wiring ────────────────────────────────────────────────────────────────


def test_cli_energy_keda_roundtrip() -> None:
    result = invoke(
        "scaler",
        "-t",
        "web",
        "--prometheus-url",
        "http://prom:9090",
        "--signal",
        "energy",
        "--energy-budget-j-per-req",
        "0.5",
        "-c",
        "container",
    )
    assert result.exit_code == 0
    assert "pat_energy_per_request_joules" in result.output
    assert 'threshold: "0.5"' in result.output
    assert "kind: ScaledObject" in result.output


def test_cli_energy_adapter() -> None:
    result = invoke(
        "scaler",
        "-t",
        "web",
        "--prometheus-url",
        "http://prom:9090",
        "--format",
        "prometheus-adapter",
        "--signal",
        "energy",
        "--energy-budget-j-per-req",
        "1.5",
        "-c",
        "container",
    )
    assert result.exit_code == 0
    assert 'value: "1.5"' in result.output
    assert "pat_energy_per_request_joules" in result.output


def test_cli_energy_signal_missing_budget_errors() -> None:
    result = invoke(
        "scaler",
        "-t",
        "web",
        "--prometheus-url",
        "http://prom:9090",
        "--signal",
        "energy",
    )
    assert result.exit_code == 2
    assert "energy-budget-j-per-req" in result.output.lower()


def test_cli_energy_signal_requires_one_layer() -> None:
    result = invoke(
        "scaler",
        "-t",
        "web",
        "--prometheus-url",
        "http://prom:9090",
        "--signal",
        "energy",
        "--energy-budget-j-per-req",
        "0.5",
    )
    assert result.exit_code == 2
    assert "current-layer" in result.output.lower()


def test_cli_bad_signal_errors() -> None:
    result = invoke(
        "scaler",
        "-t",
        "web",
        "--prometheus-url",
        "http://prom:9090",
        "--signal",
        "bogus",
    )
    assert result.exit_code == 2


def test_cli_default_signal_still_replicas() -> None:
    result = invoke("scaler", "-t", "web", "--prometheus-url", "http://prom:9090")
    assert result.exit_code == 0
    assert "pat_predicted_recommended_replicas" in result.output
    assert 'threshold: "1"' in result.output


def test_cli_energy_budget_nonfinite_rejected() -> None:
    result = invoke(
        "scaler",
        "-t",
        "web",
        "--prometheus-url",
        "http://prom:9090",
        "--signal",
        "energy",
        "--energy-budget-j-per-req",
        "nan",
    )
    assert result.exit_code == 2
