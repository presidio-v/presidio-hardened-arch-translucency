"""Tests for the exporter's energy gauges (v0.21.0).

Three MODELLED gauges (HELP marked "modelled (analytic energy model)") always
present; one MEASURED gauge (HELP "measured (chained energy observation store)")
only when the energy store has readings. Exposition must stay valid.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from presidio_arch_translucency.export import (
    build_metrics,
    measured_energy_metrics,
    render_exposition,
)
from presidio_arch_translucency.model import ReplicationLayer
from presidio_arch_translucency.observe import (
    EnergyObservation,
    _record_energy_observation_unchecked,
)

_MODELLED = {
    "pat_energy_per_request_joules",
    "pat_power_watts",
    "pat_energy_efficiency_index",
}


def _metrics():
    return build_metrics(500.0, 80.0, ReplicationLayer.CONTAINER)


def test_three_modelled_gauges_present():
    names = {m.name for m in _metrics()}
    assert _MODELLED <= names


def test_modelled_help_marks_analytic_model():
    for m in _metrics():
        if m.name in _MODELLED:
            assert "modelled (analytic energy model)" in m.help


def test_power_watts_has_one_sample_per_layer():
    m = next(x for x in _metrics() if x.name == "pat_power_watts")
    layers = {s.labels["layer"] for s in m.samples}
    assert layers == {"container", "pod", "deployment", "node"}


def test_replica_power_watts_scales_power():
    low = next(
        x
        for x in build_metrics(
            500.0, 80.0, ReplicationLayer.NODE, replica_power_watts=10.0
        )
        if x.name == "pat_power_watts"
    )
    high = next(
        x
        for x in build_metrics(
            500.0, 80.0, ReplicationLayer.NODE, replica_power_watts=100.0
        )
        if x.name == "pat_power_watts"
    )
    low_node = next(s.value for s in low.samples if s.labels["layer"] == "node")
    high_node = next(s.value for s in high.samples if s.labels["layer"] == "node")
    assert high_node > low_node


def test_measured_gauge_absent_when_store_empty(tmp_path):
    assert measured_energy_metrics(db_path=tmp_path / "empty.db") == []


def test_measured_gauge_present_with_readings(tmp_path):
    db = tmp_path / "obs.db"
    _record_energy_observation_unchecked(
        EnergyObservation(
            datetime.now(timezone.utc),
            120.0,
            7200.0,
            60.0,
            500.0,
            "node",
            4,
            "rapl",
            "prometheus",
        ),
        db_path=db,
    )
    metrics = measured_energy_metrics(db_path=db)
    assert len(metrics) == 1
    m = metrics[0]
    assert m.name == "pat_measured_power_watts"
    assert "measured (chained energy observation store)" in m.help
    assert m.samples[0].labels == {"layer": "node", "meter": "rapl"}


def test_measured_gauge_refuses_tampered_store(tmp_path):
    db = tmp_path / "obs.db"
    _record_energy_observation_unchecked(
        EnergyObservation(
            datetime.now(timezone.utc),
            120.0,
            7200.0,
            60.0,
            500.0,
            "node",
            4,
            "rapl",
            "prometheus",
        ),
        db_path=db,
    )
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE energy_observations SET watts = 1 WHERE id = 1")
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="not fully verified"):
        measured_energy_metrics(db_path=db)


def test_measured_gauge_latest_per_layer_meter(tmp_path):
    db = tmp_path / "obs.db"
    for w in (100.0, 130.0):  # second is more recent
        _record_energy_observation_unchecked(
            EnergyObservation(
                datetime.now(timezone.utc),
                w,
                w * 60,
                60.0,
                500.0,
                "node",
                4,
                "rapl",
                "prometheus",
            ),
            db_path=db,
        )
    (m,) = measured_energy_metrics(db_path=db)
    assert len(m.samples) == 1  # collapsed to latest per (layer, meter)
    assert m.samples[0].value == 130.0


def test_exposition_contains_energy_gauges(tmp_path):
    db = tmp_path / "obs.db"
    _record_energy_observation_unchecked(
        EnergyObservation(
            datetime.now(timezone.utc),
            120.0,
            7200.0,
            60.0,
            500.0,
            "node",
            4,
            "rapl",
            "prometheus",
        ),
        db_path=db,
    )
    text = render_exposition(_metrics() + measured_energy_metrics(db_path=db))
    assert "# TYPE pat_power_watts gauge" in text
    assert "# TYPE pat_energy_per_request_joules gauge" in text
    assert 'pat_measured_power_watts{layer="node",meter="rapl"}' in text
    assert text.endswith("\n")
