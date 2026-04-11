"""Tests for Azure cloud pricing integration (v0.6.0)."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from presidio_arch_translucency.cloud import (
    CACHE_TTL_SECONDS,
    PricingError,
    TieredPricingResult,
    _cache_set,
    _save_cache,
)
from presidio_arch_translucency.cloud_azure import (
    _parse_azure_price,
    build_cost_params_from_azure,
    get_azure_vm_price,
)

# ---------------------------------------------------------------------------
# Sample Azure Retail Prices API response
# ---------------------------------------------------------------------------

_AZURE_RESPONSE_D2SV3 = {
    "Items": [
        {
            "retailPrice": 0.096,
            "unitPrice": 0.096,
            "skuName": "D2s v3",
            "serviceName": "Virtual Machines",
            "armRegionName": "eastus",
            "type": "Consumption",
        }
    ],
    "NextPageLink": None,
}

_AZURE_RESPONSE_D2SV3_SPOT = {
    "Items": [
        {
            "retailPrice": 0.0192,
            "unitPrice": 0.0192,
            "skuName": "D2s v3 Spot",
            "serviceName": "Virtual Machines",
            "armRegionName": "eastus",
            "type": "Consumption",
        }
    ],
    "NextPageLink": None,
}

_AZURE_RESPONSE_EMPTY = {"Items": [], "NextPageLink": None}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseAzurePrice:
    def test_parses_consumption_item(self):
        price = _parse_azure_price(_AZURE_RESPONSE_D2SV3, "D2s v3", "eastus", False)
        assert price == pytest.approx(0.096)

    def test_parses_spot_item(self):
        price = _parse_azure_price(_AZURE_RESPONSE_D2SV3_SPOT, "D2s v3", "eastus", True)
        assert price == pytest.approx(0.0192)

    def test_empty_response_raises(self):
        with pytest.raises(PricingError, match="No Azure price found"):
            _parse_azure_price(_AZURE_RESPONSE_EMPTY, "D2s v3", "eastus", False)

    def test_skips_zero_price_items(self):
        data = {
            "Items": [
                {"retailPrice": 0.0, "type": "Consumption"},
                {"retailPrice": 0.096, "type": "Consumption"},
            ]
        }
        price = _parse_azure_price(data, "D2s v3", "eastus", False)
        assert price == pytest.approx(0.096)


# ---------------------------------------------------------------------------
# get_azure_vm_price
# ---------------------------------------------------------------------------


class TestGetAzureVMPrice:
    def test_returns_price_from_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud_azure._fetch_azure_vm_price_from_api",
            return_value=0.096,
        ):
            price, from_cache = get_azure_vm_price("eastus", "D2s v3", no_cache=True)

        assert price == pytest.approx(0.096)
        assert from_cache is False

    def test_uses_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        _cache_set("azure:ondemand:eastus:D2s v3", 0.096)

        with patch(
            "presidio_arch_translucency.cloud_azure._fetch_azure_vm_price_from_api"
        ) as mock_api:
            price, from_cache = get_azure_vm_price("eastus", "D2s v3")

        mock_api.assert_not_called()
        assert price == pytest.approx(0.096)
        assert from_cache is True

    def test_spot_uses_short_ttl(self, tmp_path, monkeypatch):

        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        _cache_set("azure:spot:eastus:D2s v3", 0.0192)

        price, from_cache = get_azure_vm_price("eastus", "D2s v3", spot=True)
        assert price == pytest.approx(0.0192)
        assert from_cache is True

    def test_spot_cache_misses_after_5_min(self, tmp_path, monkeypatch):
        from presidio_arch_translucency.cloud import SPOT_CACHE_TTL_SECONDS

        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        old_ts = time.time() - SPOT_CACHE_TTL_SECONDS - 1
        _save_cache({"azure:spot:eastus:D2s v3": {"price": 0.0192, "ts": old_ts}})

        with patch(
            "presidio_arch_translucency.cloud_azure._fetch_azure_vm_price_from_api",
            return_value=0.021,
        ):
            price, from_cache = get_azure_vm_price("eastus", "D2s v3", spot=True)

        assert price == pytest.approx(0.021)
        assert from_cache is False  # fetched fresh

    def test_falls_back_to_stale_cache_on_network_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        old_ts = time.time() - CACHE_TTL_SECONDS - 1
        _save_cache({"azure:ondemand:eastus:D2s v3": {"price": 0.096, "ts": old_ts}})

        with patch(
            "presidio_arch_translucency.cloud_azure._fetch_azure_vm_price_from_api",
            side_effect=OSError("network error"),
        ):
            price, from_cache = get_azure_vm_price("eastus", "D2s v3")

        assert price == pytest.approx(0.096)
        assert from_cache is True

    def test_raises_when_no_cache_and_api_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud_azure._fetch_azure_vm_price_from_api",
            side_effect=OSError("network error"),
        ):
            with pytest.raises(PricingError, match="no local cache"):
                get_azure_vm_price("eastus", "D2s v3", no_cache=True)


# ---------------------------------------------------------------------------
# build_cost_params_from_azure
# ---------------------------------------------------------------------------


class TestBuildCostParamsFromAzure:
    def test_on_demand_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud_azure.get_azure_vm_price",
            return_value=(0.096, False),
        ):
            result = build_cost_params_from_azure("eastus", "D2s v3")

        assert isinstance(result, TieredPricingResult)
        assert result.on_demand.params.cost_per_node_hour == pytest.approx(0.096)
        assert result.on_demand.params.cost_per_pod_hour == pytest.approx(0.096 / 8)
        assert result.on_demand.params.cost_per_container_hour == pytest.approx(
            0.096 / 16
        )  # noqa: E501
        assert result.spot is None
        assert "Azure pay-as-you-go" in result.on_demand.source_description

    def test_spot_populates_spot_tier(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        def mock_price(region, sku_name, spot=False, no_cache=False):
            return (0.0192, False) if spot else (0.096, False)

        with patch(
            "presidio_arch_translucency.cloud_azure.get_azure_vm_price",
            side_effect=mock_price,
        ):
            result = build_cost_params_from_azure("eastus", "D2s v3", spot=True)

        assert result.spot is not None
        assert result.spot.params.cost_per_node_hour == pytest.approx(0.0192)
        assert "Spot" in result.spot.source_description

    def test_spot_unavailable_does_not_fail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        def mock_price(region, sku_name, spot=False, no_cache=False):
            if spot:
                raise PricingError("no spot price")
            return (0.096, False)

        with patch(
            "presidio_arch_translucency.cloud_azure.get_azure_vm_price",
            side_effect=mock_price,
        ):
            result = build_cost_params_from_azure("eastus", "D2s v3", spot=True)

        assert result.on_demand.params.cost_per_node_hour == pytest.approx(0.096)
        assert result.spot is None  # silently omitted
