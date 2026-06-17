"""Tests for the official Grafana dashboard (`grafana/pat-dashboard.json`).

Validates the dashboard JSON and — crucially — keeps it in sync with the
exporter: every ``pat_*`` metric a panel queries must be one the exporter can
actually produce, so the dashboard cannot silently drift from the metrics.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from presidio_arch_translucency.export import (
    build_metrics,
    prediction_metrics_from_result,
)
from presidio_arch_translucency.model import ReplicationLayer
from presidio_arch_translucency.optimize import OptimizeResult

DASHBOARD = Path(__file__).resolve().parent.parent / "grafana" / "pat-dashboard.json"


def _load() -> dict:
    return json.loads(DASHBOARD.read_text(encoding="utf-8"))


def _exporter_metric_universe() -> set[str]:
    """Every metric name the exporter can emit (analysis + cost + prediction+CI)."""
    names = {
        m.name
        for m in build_metrics(
            500.0, 80.0, ReplicationLayer.CONTAINER, cost_per_replica_hour=0.02
        )
    }
    now = datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    arima_result = OptimizeResult(
        layer="container",
        samples=40,
        window_minutes=60.0,
        sma_rps=500.0,
        sma_latency_ms=80.0,
        trend_pct=10.0,
        slope_rps_per_min=2.0,
        horizon_minutes=10.0,
        predicted_rps=540.0,
        current_replicas=4,
        recommended_replicas=6,
        first_ts=now,
        last_ts=now,
        model="arima",
        predicted_rps_lower=500.0,
        predicted_rps_upper=600.0,
        recommended_replicas_lower=5,
        recommended_replicas_upper=8,
        arima_order=(1, 1, 1),
    )
    names |= {m.name for m in prediction_metrics_from_result(arima_result)}
    return names


def _referenced_metrics() -> set[str]:
    dashboard = _load()
    referenced: set[str] = set()
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            referenced |= set(re.findall(r"pat_[a-z0-9_]+", target.get("expr", "")))
    return referenced


def test_dashboard_is_valid_json_with_panels() -> None:
    dashboard = _load()
    assert dashboard["title"]
    assert dashboard["uid"] == "pat-translucency"
    assert isinstance(dashboard["panels"], list)
    assert len(dashboard["panels"]) >= 5


def test_dashboard_is_importable() -> None:
    dashboard = _load()
    # A datasource template variable makes the dashboard importable anywhere.
    var_names = {v["name"] for v in dashboard["templating"]["list"]}
    assert "DS_PROMETHEUS" in var_names
    assert any(inp["name"] == "DS_PROMETHEUS" for inp in dashboard["__inputs"])


def test_dashboard_metrics_are_a_subset_of_exporter() -> None:
    referenced = _referenced_metrics()
    assert referenced, "dashboard references no pat_ metrics"
    unknown = referenced - _exporter_metric_universe()
    assert not unknown, f"dashboard references unknown metrics: {unknown}"


def test_dashboard_covers_core_metrics() -> None:
    referenced = _referenced_metrics()
    assert {
        "pat_recommended_replicas",
        "pat_predicted_rps",
        "pat_cost_per_request",
    } <= referenced
