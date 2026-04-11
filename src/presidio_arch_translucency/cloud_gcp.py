"""GCP cloud pricing integration (v0.6.0).

Fetches GCP Compute Engine instance prices from the public GCP Pricing
Calculator JSON endpoint.  No credentials or API key required.

Data source (unofficial but widely used):
  https://cloudpricingcalculator.appspot.com/static/data/pricelist.json

Key format in the JSON:
  On-demand:    CP-COMPUTEENGINE-VMIMAGE-{MACHINE_TYPE_UPPER}
  Preemptible:  CP-COMPUTEENGINE-VMIMAGE-PREEMPTIBLE-{MACHINE_TYPE_UPPER}

Example: machine_type="n2-standard-4"
  → "CP-COMPUTEENGINE-VMIMAGE-N2-STANDARD-4": {"us-central1": 0.1942, ...}
"""

from __future__ import annotations

import json
import urllib.request
from typing import Optional

from presidio_arch_translucency.cloud import (
    CACHE_TTL_SECONDS,
    CloudPricingResult,
    PricingError,
    TieredPricingResult,
    _cache_get,
    _cache_get_stale,
    _cache_set,
)
from presidio_arch_translucency.cost import CostParams

# ---------------------------------------------------------------------------
# GCP public pricelist endpoint
# ---------------------------------------------------------------------------

_GCP_PRICELIST_URL = (
    "https://cloudpricingcalculator.appspot.com/static/data/pricelist.json"
)

_GCP_VM_PREFIX = "CP-COMPUTEENGINE-VMIMAGE-"
_GCP_PREEMPTIBLE_PREFIX = "CP-COMPUTEENGINE-VMIMAGE-PREEMPTIBLE-"

# Packing ratios (same as AWS for cross-provider consistency)
_CONTAINERS_PER_NODE = 16
_PODS_PER_NODE = 8


# ---------------------------------------------------------------------------
# Price key helpers
# ---------------------------------------------------------------------------


def _gcp_price_key(machine_type: str, preemptible: bool = False) -> str:
    """Map GCP machine type to its pricing JSON key."""
    suffix = machine_type.upper().replace(".", "-")
    prefix = _GCP_PREEMPTIBLE_PREFIX if preemptible else _GCP_VM_PREFIX
    return prefix + suffix


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_gcp_price(
    data: dict,
    region: str,
    machine_type: str,
    preemptible: bool = False,
) -> float:
    """Extract hourly price from GCP pricelist JSON for a given region + machine type."""  # noqa: E501
    price_list = data.get("gcp_price_list", {})
    key = _gcp_price_key(machine_type, preemptible)

    entry = price_list.get(key)
    if entry is None:
        label = f"preemptible {machine_type}" if preemptible else machine_type
        raise PricingError(
            f"GCP machine type {label!r} not found in pricing data. "
            "Verify the machine type (e.g. 'n2-standard-4', 'e2-standard-2')."
        )

    if isinstance(entry, dict):
        price = entry.get(region)
        if price is None:
            # Some regions use the short form (e.g. "us" for "us-central1")
            short = region.split("-")[0]
            price = entry.get(short)
        if price is None:
            available = ", ".join(k for k in entry if not k.startswith("_"))[:200]
            raise PricingError(
                f"GCP region {region!r} not found for {machine_type!r}. "
                f"Available regions: {available}"
            )
        try:
            return float(price)
        except (TypeError, ValueError) as exc:
            raise PricingError(
                f"Unexpected price format for GCP {machine_type!r}: {price!r}"
            ) from exc

    # Some entries are a direct scalar (global price)
    try:
        return float(entry)
    except (TypeError, ValueError) as exc:
        raise PricingError(
            f"Unexpected GCP pricing entry for {machine_type!r}: {entry!r}"
        ) from exc


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------


def _fetch_gcp_price_from_api(
    region: str, machine_type: str, preemptible: bool = False
) -> float:
    req = urllib.request.Request(  # noqa: S310
        _GCP_PRICELIST_URL, headers={"User-Agent": "pat-cli/0.6.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        data = json.loads(resp.read())
    return _parse_gcp_price(data, region, machine_type, preemptible)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_gcp_instance_price(
    region: str,
    machine_type: str,
    preemptible: bool = False,
    no_cache: bool = False,
) -> tuple[float, bool]:
    """
    Return (hourly price USD/hr, from_cache).

    Falls back to stale cache on network failure.
    """
    prefix = "gcp:preemptible" if preemptible else "gcp:ondemand"
    key = f"{prefix}:{region}:{machine_type}"
    if not no_cache:
        cached = _cache_get(key, ttl=CACHE_TTL_SECONDS)
        if cached is not None:
            return cached, True

    try:
        price = _fetch_gcp_price_from_api(region, machine_type, preemptible)
        _cache_set(key, price)
        return price, False
    except PricingError:
        raise
    except Exception as exc:  # noqa: BLE001
        stale = _cache_get_stale(key)
        if stale is not None:
            return stale, True
        raise PricingError(
            f"Failed to fetch GCP pricing and no local cache is available: {exc}"
        ) from exc


def build_cost_params_from_gcp(
    region: str,
    machine_type: str,
    preemptible: bool = False,
    no_cache: bool = False,
) -> TieredPricingResult:
    """
    Build CostParams from GCP pricing.

    on_demand is always populated with the regular instance price.
    If preemptible=True, spot is also populated with the preemptible price
    (when available; missing preemptible data is silently omitted).
    """
    import logging  # noqa: PLC0415

    node_price, from_cache = get_gcp_instance_price(
        region, machine_type, preemptible=False, no_cache=no_cache
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
            f"GCP on-demand · {region} · {machine_type} (${node_price:.4f}/hr)"
        ),
    )

    spot: Optional[CloudPricingResult] = None  # noqa: UP045
    if preemptible:
        try:
            spot_price, spot_from_cache = get_gcp_instance_price(
                region, machine_type, preemptible=True, no_cache=no_cache
            )
            spot = CloudPricingResult(
                params=CostParams(
                    cost_per_container_hour=spot_price / _CONTAINERS_PER_NODE,
                    cost_per_pod_hour=spot_price / _PODS_PER_NODE,
                    cost_per_deployment_hour=spot_price,
                    cost_per_node_hour=spot_price,
                ),
                from_cache=spot_from_cache,
                source_description=(
                    f"GCP preemptible · {region} · {machine_type} "
                    f"(${spot_price:.4f}/hr  ⚠ preemptible)"
                ),
            )
        except PricingError as exc:
            logging.getLogger(__name__).warning(
                "GCP preemptible pricing not available: %s", exc
            )

    return TieredPricingResult(on_demand=on_demand, spot=spot)
