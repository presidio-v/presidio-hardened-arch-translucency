"""Azure cloud pricing integration (v0.6.0).

Fetches Azure VM prices from the official Azure Retail Prices API.
No credentials required.

API reference:
  https://docs.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices

Query example:
  GET https://prices.azure.com/api/retail/prices?$filter=
    serviceName eq 'Virtual Machines'
    and armRegionName eq 'eastus'
    and skuName eq 'D2s v3'

Spot prices use the SKU suffix " Spot" (e.g. "D2s v3 Spot").
Cache TTL: 24 hours (on-demand), 5 minutes (spot).
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional

from presidio_arch_translucency.cloud import (
    CACHE_TTL_SECONDS,
    SPOT_CACHE_TTL_SECONDS,
    CloudPricingResult,
    PricingError,
    TieredPricingResult,
    _cache_get,
    _cache_get_stale,
    _cache_set,
)
from presidio_arch_translucency.cost import CostParams

# ---------------------------------------------------------------------------
# Azure Retail Prices API
# ---------------------------------------------------------------------------

_AZURE_RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"

# Packing ratios (same as AWS/GCP for cross-provider consistency)
_CONTAINERS_PER_NODE = 16
_PODS_PER_NODE = 8


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_azure_price(data: dict, sku_name: str, region: str, spot: bool) -> float:
    """Extract retail price from Azure Retail Prices API response.

    Defensively verifies that each candidate item actually matches the
    requested SKU/region (when those fields are present) so a broadened or
    tampered response can't yield a price for the wrong instance.
    """
    expected_sku = f"{sku_name} Spot" if spot else sku_name
    for item in data.get("Items", []):
        if item.get("type") not in ("Consumption", "DevTestConsumption"):
            continue
        # Best-effort match: only reject when the field is present and differs.
        item_sku = item.get("skuName")
        if item_sku is not None and item_sku != expected_sku:
            continue
        item_region = item.get("armRegionName")
        if item_region is not None and item_region != region:
            continue
        try:
            price = float(item.get("retailPrice", 0))
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    label = expected_sku
    raise PricingError(
        f"No Azure price found for SKU {label!r} in region {region!r}. "
        "Verify the SKU name and region "
        "(e.g. --sku-name 'D2s v3' --region eastus)."
    )


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def _odata_escape(value: str) -> str:
    """Escape a string literal for an OData ``$filter`` clause.

    OData escapes a single quote by doubling it; doing so prevents a value
    such as ``D2s v3' or skuName eq 'X`` from breaking out of the literal and
    injecting additional filter logic.
    """
    return value.replace("'", "''")


def _fetch_azure_vm_price_from_api(
    region: str, sku_name: str, spot: bool = False
) -> float:
    """Fetch Azure VM retail price via the public Retail Prices REST API."""
    query_sku = f"{sku_name} Spot" if spot else sku_name
    filter_str = (
        f"serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{_odata_escape(region)}' "
        f"and skuName eq '{_odata_escape(query_sku)}'"
    )
    url = (
        f"{_AZURE_RETAIL_PRICES_URL}?{urllib.parse.urlencode({'$filter': filter_str})}"  # noqa: E501
    )
    req = urllib.request.Request(url, headers={"User-Agent": "pat-cli/0.6.0"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        data = json.loads(resp.read())
    return _parse_azure_price(data, sku_name, region, spot)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_azure_vm_price(
    region: str,
    sku_name: str,
    spot: bool = False,
    no_cache: bool = False,
) -> tuple[float, bool]:
    """
    Return (hourly price USD/hr, from_cache).

    Spot prices use a 5-minute TTL; on-demand uses 24 hours.
    Falls back to stale cache on network failure.
    """
    prefix = "azure:spot" if spot else "azure:ondemand"
    key = f"{prefix}:{region}:{sku_name}"
    ttl = SPOT_CACHE_TTL_SECONDS if spot else CACHE_TTL_SECONDS
    if not no_cache:
        cached = _cache_get(key, ttl=ttl)
        if cached is not None:
            return cached, True

    try:
        price = _fetch_azure_vm_price_from_api(region, sku_name, spot)
        _cache_set(key, price)
        return price, False
    except PricingError:
        raise
    except Exception as exc:  # noqa: BLE001
        stale = _cache_get_stale(key)
        if stale is not None:
            return stale, True
        raise PricingError(
            f"Failed to fetch Azure pricing and no local cache is available: {exc}"
        ) from exc


def build_cost_params_from_azure(
    region: str,
    sku_name: str,
    spot: bool = False,
    no_cache: bool = False,
) -> TieredPricingResult:
    """
    Build CostParams from Azure pricing.

    on_demand is always populated with the pay-as-you-go price.
    If spot=True, the spot tier is also populated (when available;
    missing spot data is silently omitted).
    """
    import logging  # noqa: PLC0415

    node_price, from_cache = get_azure_vm_price(
        region, sku_name, spot=False, no_cache=no_cache
    )
    on_demand = CloudPricingResult(
        params=CostParams(
            cost_per_container_hour=node_price / _CONTAINERS_PER_NODE,
            cost_per_pod_hour=node_price / _PODS_PER_NODE,
            cost_per_deployment_hour=node_price,
            cost_per_node_hour=node_price,
        ),
        from_cache=from_cache,
        source_description=(
            f"Azure pay-as-you-go · {region} · {sku_name} (${node_price:.4f}/hr)"
        ),
    )

    spot_result: Optional[CloudPricingResult] = None  # noqa: UP045
    if spot:
        try:
            spot_price, spot_from_cache = get_azure_vm_price(
                region, sku_name, spot=True, no_cache=no_cache
            )
            spot_result = CloudPricingResult(
                params=CostParams(
                    cost_per_container_hour=spot_price / _CONTAINERS_PER_NODE,
                    cost_per_pod_hour=spot_price / _PODS_PER_NODE,
                    cost_per_deployment_hour=spot_price,
                    cost_per_node_hour=spot_price,
                ),
                from_cache=spot_from_cache,
                source_description=(
                    f"Azure Spot · {region} · {sku_name} (${spot_price:.4f}/hr  ⚠ spot)"
                ),
            )
        except PricingError as exc:
            logging.getLogger(__name__).warning(
                "Azure Spot pricing not available: %s", exc
            )

    return TieredPricingResult(on_demand=on_demand, spot=spot_result)
