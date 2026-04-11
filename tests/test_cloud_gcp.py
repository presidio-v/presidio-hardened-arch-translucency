"""Tests for GCP cloud pricing integration (v0.6.0)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from presidio_arch_translucency.cloud import (
    PricingError,
    TieredPricingResult,
    _cache_set,
)
from presidio_arch_translucency.cloud_gcp import (
    _gcp_price_key,
    _parse_gcp_price,
    build_cost_params_from_gcp,
    get_gcp_instance_price,
)

# ---------------------------------------------------------------------------
# Sample pricelist JSON (minimal subset)
# ---------------------------------------------------------------------------

_GCP_PRICELIST = {
    "gcp_price_list": {
        "CP-COMPUTEENGINE-VMIMAGE-N2-STANDARD-4": {
            "us-central1": 0.1942,
            "us-east1": 0.1942,
            "europe-west1": 0.2330,
        },
        "CP-COMPUTEENGINE-VMIMAGE-PREEMPTIBLE-N2-STANDARD-4": {
            "us-central1": 0.0291,
        },
        "CP-COMPUTEENGINE-VMIMAGE-E2-STANDARD-2": {
            "us": 0.0671,
        },
    }
}


# ---------------------------------------------------------------------------
# Key helper
# ---------------------------------------------------------------------------


def test_gcp_price_key_on_demand():
    assert _gcp_price_key("n2-standard-4") == "CP-COMPUTEENGINE-VMIMAGE-N2-STANDARD-4"


def test_gcp_price_key_preemptible():
    assert (
        _gcp_price_key("n2-standard-4", preemptible=True)
        == "CP-COMPUTEENGINE-VMIMAGE-PREEMPTIBLE-N2-STANDARD-4"
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseGCPPrice:
    def test_parses_known_region(self):
        price = _parse_gcp_price(_GCP_PRICELIST, "us-central1", "n2-standard-4")
        assert price == pytest.approx(0.1942)

    def test_parses_short_region_fallback(self):
        # e2-standard-2 only has "us" in fixture, but "us-central1" should fall back to "us"  # noqa: E501
        price = _parse_gcp_price(_GCP_PRICELIST, "us-central1", "e2-standard-2")
        assert price == pytest.approx(0.0671)

    def test_unknown_machine_type_raises(self):
        with pytest.raises(PricingError, match="not found in pricing data"):
            _parse_gcp_price(_GCP_PRICELIST, "us-central1", "z9-gigantic-99")

    def test_unknown_region_raises(self):
        with pytest.raises(PricingError, match="not found"):
            _parse_gcp_price(_GCP_PRICELIST, "mars-west1", "n2-standard-4")

    def test_preemptible_price(self):
        price = _parse_gcp_price(
            _GCP_PRICELIST, "us-central1", "n2-standard-4", preemptible=True
        )
        assert price == pytest.approx(0.0291)


# ---------------------------------------------------------------------------
# get_gcp_instance_price
# ---------------------------------------------------------------------------


class TestGetGCPInstancePrice:
    def test_returns_price_from_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud_gcp._fetch_gcp_price_from_api",
            return_value=0.1942,
        ):
            price, from_cache = get_gcp_instance_price(
                "us-central1", "n2-standard-4", no_cache=True
            )

        assert price == pytest.approx(0.1942)
        assert from_cache is False

    def test_uses_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        _cache_set("gcp:ondemand:us-central1:n2-standard-4", 0.1942)

        with patch(
            "presidio_arch_translucency.cloud_gcp._fetch_gcp_price_from_api"
        ) as mock_api:
            price, from_cache = get_gcp_instance_price("us-central1", "n2-standard-4")

        mock_api.assert_not_called()
        assert price == pytest.approx(0.1942)
        assert from_cache is True

    def test_falls_back_to_stale_cache_on_error(self, tmp_path, monkeypatch):
        import time

        from presidio_arch_translucency.cloud import CACHE_TTL_SECONDS, _save_cache

        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        old_ts = time.time() - CACHE_TTL_SECONDS - 1
        _save_cache(
            {"gcp:ondemand:us-central1:n2-standard-4": {"price": 0.1942, "ts": old_ts}}
        )  # noqa: E501

        with patch(
            "presidio_arch_translucency.cloud_gcp._fetch_gcp_price_from_api",
            side_effect=OSError("network error"),
        ):
            price, from_cache = get_gcp_instance_price("us-central1", "n2-standard-4")

        assert price == pytest.approx(0.1942)
        assert from_cache is True

    def test_raises_pricing_error_with_no_cache_and_network_failure(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud_gcp._fetch_gcp_price_from_api",
            side_effect=OSError("network error"),
        ):
            with pytest.raises(PricingError, match="no local cache"):
                get_gcp_instance_price("us-central1", "n2-standard-4", no_cache=True)


# ---------------------------------------------------------------------------
# build_cost_params_from_gcp
# ---------------------------------------------------------------------------


class TestBuildCostParamsFromGCP:
    def test_on_demand_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud_gcp.get_gcp_instance_price",
            return_value=(0.1942, False),
        ):
            result = build_cost_params_from_gcp("us-central1", "n2-standard-4")

        assert isinstance(result, TieredPricingResult)
        assert result.on_demand.params.cost_per_node_hour == pytest.approx(0.1942)
        assert result.on_demand.params.cost_per_pod_hour == pytest.approx(0.1942 / 8)
        assert result.on_demand.params.cost_per_container_hour == pytest.approx(
            0.1942 / 16
        )  # noqa: E501
        assert result.spot is None
        assert "GCP on-demand" in result.on_demand.source_description

    def test_preemptible_populates_spot_tier(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        def mock_price(region, machine_type, preemptible=False, no_cache=False):
            return (0.0291, False) if preemptible else (0.1942, False)

        with patch(
            "presidio_arch_translucency.cloud_gcp.get_gcp_instance_price",
            side_effect=mock_price,
        ):
            result = build_cost_params_from_gcp(
                "us-central1", "n2-standard-4", preemptible=True
            )

        assert result.spot is not None
        assert result.spot.params.cost_per_node_hour == pytest.approx(0.0291)
        assert "preemptible" in result.spot.source_description

    def test_preemptible_unavailable_does_not_fail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        def mock_price(region, machine_type, preemptible=False, no_cache=False):
            if preemptible:
                raise PricingError("no preemptible price")
            return (0.1942, False)

        with patch(
            "presidio_arch_translucency.cloud_gcp.get_gcp_instance_price",
            side_effect=mock_price,
        ):
            result = build_cost_params_from_gcp(
                "us-central1", "n2-standard-4", preemptible=True
            )

        assert result.on_demand.params.cost_per_node_hour == pytest.approx(0.1942)
        assert result.spot is None  # silently omitted
