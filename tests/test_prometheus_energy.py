"""Security and functional tests for direct-hardware energy collection."""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from presidio_arch_translucency.observe import CollectedEnergyObservation
from presidio_arch_translucency.prometheus import (
    DCGM_WATTS_QUERY,
    DEFAULT_REPLICAS_QUERY,
    DEFAULT_RPS_QUERY,
    ENERGY_METER_PRESETS,
    RAPL_WATTS_QUERY,
    PrometheusError,
    fetch_energy_observation,
    instant_query_vector,
)

_URL = "https://prometheus.monitoring.svc:9090"


def _urlopen_cm(payload: dict):
    cm = MagicMock()
    cm.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    cm.__exit__.return_value = False
    return cm


def _vector(series: list[tuple[dict, object]]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [1717243200, str(value)]}
                for labels, value in series
            ],
        },
    }


class TestInstantQueryVector:
    def test_returns_labels_and_values(self):
        payload = _vector([({"path": "package"}, 12.0), ({"path": "dram"}, 3.0)])
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            out = instant_query_vector(_URL, "q")
        assert out == [({"path": "package"}, 12.0), ({"path": "dram"}, 3.0)]

    @pytest.mark.parametrize("value", ["NaN", "nan", "+Inf", "inf", "-inf"])
    def test_non_finite_samples_are_skipped_case_insensitively(self, value):
        payload = _vector([({"path": "bad"}, value), ({"path": "good"}, 5)])
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            assert instant_query_vector(_URL, "q") == [({"path": "good"}, 5.0)]

    def test_empty_vector_returns_empty(self):
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(_vector([]))):
            assert instant_query_vector(_URL, "q") == []

    def test_malformed_sample_raises_controlled_error(self):
        payload = _vector([({"path": "bad"}, "not-a-number")])
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            with pytest.raises(PrometheusError, match="non-numeric"):
                instant_query_vector(_URL, "q")

    def test_error_status_raises(self):
        payload = {"status": "error", "error": "boom"}
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            with pytest.raises(PrometheusError, match="boom"):
                instant_query_vector(_URL, "q")


def _run_meter(meter="rapl", gate_series=None, watts=90.0, window_s=60.0, **kwargs):
    if gate_series is None:
        gate_series = [({"path": "/sys/class/powercap/intel-rapl:0"}, 40.0)]
    if meter == "dcgm" and gate_series == [
        ({"path": "/sys/class/powercap/intel-rapl:0"}, 40.0)
    ]:
        gate_series = [({"gpu": "0"}, 40.0)]
    seconds = int(window_s)
    if meter == "rapl":
        preset_watts = (
            f"sum(increase(node_rapl_package_joules_total[{seconds}s])) / {seconds}"
        )
        preset_gate = (
            f"sum by (path) (increase(node_rapl_package_joules_total[{seconds}s]))"
        )
    else:
        preset_watts = (
            "sum(increase(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"
            f"[{seconds}s])) / 1000 / {seconds}"
        )
        preset_gate = (
            f"sum by (gpu) (increase(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION[{seconds}s]))"
        )
    effective_watts = kwargs.get("watts_query") or preset_watts
    effective_gate = kwargs.get("gate_query") or preset_gate

    def vector(_base_url, query, token=None, timeout=30.0):
        assert query == effective_gate
        return gate_series

    values = {
        effective_watts: watts,
        DEFAULT_RPS_QUERY: kwargs.pop("rps", 100.0),
        DEFAULT_REPLICAS_QUERY: kwargs.pop("replicas", 2.0),
    }

    def scalar(_base_url, query, token=None, timeout=30.0):
        return values[query]

    with (
        patch(
            "presidio_arch_translucency.prometheus.instant_query_vector",
            side_effect=vector,
        ),
        patch(
            "presidio_arch_translucency.prometheus.instant_query", side_effect=scalar
        ),
    ):
        return fetch_energy_observation(
            _URL, "node", meter, window_s=window_s, **kwargs
        )


class TestDirectMeterBoundary:
    def test_kepler_is_always_rejected_before_network(self):
        with patch("urllib.request.urlopen") as opened:
            with pytest.raises(
                PrometheusError, match="not accepted as direct measured"
            ):
                fetch_energy_observation(_URL, "node", "kepler")
        opened.assert_not_called()

    @pytest.mark.parametrize("meter", ["rapl", "dcgm"])
    def test_direct_hardware_meters_return_sealed_observation(self, meter):
        obs = _run_meter(meter)
        assert isinstance(obs, CollectedEnergyObservation)
        assert obs.meter == meter
        assert obs.watts == 90.0
        assert obs.joules == 5400.0

    def test_unknown_meter_rejected(self):
        with pytest.raises(PrometheusError, match="unknown meter"):
            fetch_energy_observation(_URL, "node", "manual")

    @pytest.mark.parametrize("meter,label", [("rapl", "path"), ("dcgm", "gpu")])
    def test_empty_or_unidentified_gate_refused(self, meter, label):
        with pytest.raises(PrometheusError, match="no real power source"):
            _run_meter(meter, gate_series=[])
        with pytest.raises(PrometheusError, match="empty/missing"):
            _run_meter(meter, gate_series=[({label: ""}, 1.0), ({}, 2.0)])

    @pytest.mark.parametrize("meter", ["rapl", "dcgm"])
    def test_watts_missing_refused_but_measured_zero_allowed(self, meter):
        with pytest.raises(PrometheusError, match="no series"):
            _run_meter(meter, watts=None)
        assert _run_meter(meter, watts=0.0).joules == 0.0

    def test_missing_replicas_refused(self):
        with pytest.raises(PrometheusError, match="replica"):
            _run_meter(replicas=None)


class TestWindowAndOverrides:
    def test_window_changes_promql_range_and_energy_window_together(self):
        obs = _run_meter(window_s=30.0, watts=100.0)
        assert obs.window_s == 30.0
        assert obs.joules == 3000.0

    @pytest.mark.parametrize("bad", [0, 0.5, 1.5, 3601, float("nan"), float("inf")])
    def test_invalid_window_refused(self, bad):
        with pytest.raises(PrometheusError, match="whole number"):
            fetch_energy_observation(_URL, "node", "rapl", window_s=bad)

    def test_override_is_permanently_marked(self):
        override = "sum(increase(node_rapl_package_joules_total[30s])) / 30"
        obs = _run_meter(watts_query=override)
        assert obs.source == "prometheus-override"

    def test_exact_preset_is_not_marked_override(self):
        obs = _run_meter(
            watts_query=ENERGY_METER_PRESETS["rapl"]["watts"],
            gate_query=ENERGY_METER_PRESETS["rapl"]["gate"],
        )
        assert obs.source == "prometheus"

    def test_pinned_queries_use_matching_increase_window(self):
        assert RAPL_WATTS_QUERY.endswith("[60s])) / 60")
        assert DCGM_WATTS_QUERY.endswith("[60s])) / 1000 / 60")


class TestAuthDiscipline:
    def test_token_from_env_used_as_bearer(self, monkeypatch):
        monkeypatch.setenv("PAT_PROMETHEUS_TOKEN", "envtoken")  # noqa: S105
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            query = req.full_url
            if "sum+by+%28path%29" in query:
                return _urlopen_cm(_vector([({"path": "/rapl/0"}, 12.0)]))
            return _urlopen_cm(_vector([({}, 2.0)]))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_energy_observation(_URL, "node", "rapl")
        assert seen and all(value == "Bearer envtoken" for value in seen)

    def test_env_token_requires_https(self, monkeypatch):
        monkeypatch.setenv("PAT_PROMETHEUS_TOKEN", "envtoken")  # noqa: S105
        with pytest.raises(PrometheusError, match="https"):
            fetch_energy_observation("http://prometheus:9090", "node", "rapl")
