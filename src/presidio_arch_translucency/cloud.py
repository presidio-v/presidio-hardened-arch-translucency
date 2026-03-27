"""AWS on-demand cloud pricing integration (v0.5.0).

Fetches live EC2 and Fargate on-demand prices from the public AWS Pricing API
(no credentials required).  Results are cached locally for 24 hours to avoid
repeated network round-trips.

Cache location: ~/.pat/pricing-cache.json
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from presidio_arch_translucency.cost import CostParams

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".pat"
CACHE_FILE = CACHE_DIR / "pricing-cache.json"
CACHE_TTL_SECONDS = 86_400  # 24 hours

# ---------------------------------------------------------------------------
# AWS Pricing API endpoints — public, no auth required
# ---------------------------------------------------------------------------

_EC2_CSV_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2"
    "/current/{region}/index.csv"
)
_FARGATE_JSON_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS"
    "/current/index.json"
)

# EC2 packing ratios used to derive per-container / per-pod costs from the
# full-node on-demand price.
_CONTAINERS_PER_NODE = 16
_PODS_PER_NODE = 8

# ---------------------------------------------------------------------------
# AWS region → pricing-data location name (used in ECS JSON)
# ---------------------------------------------------------------------------

_REGION_TO_LOCATION: dict[str, str] = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "ca-central-1": "Canada (Central)",
    "eu-west-1": "Europe (Ireland)",
    "eu-west-2": "Europe (London)",
    "eu-west-3": "Europe (Paris)",
    "eu-central-1": "Europe (Frankfurt)",
    "eu-north-1": "Europe (Stockholm)",
    "eu-south-1": "Europe (Milan)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
    "ap-northeast-2": "Asia Pacific (Seoul)",
    "ap-northeast-3": "Asia Pacific (Osaka)",
    "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-east-1": "Asia Pacific (Hong Kong)",
    "sa-east-1": "South America (Sao Paulo)",
    "me-south-1": "Middle East (Bahrain)",
    "af-south-1": "Africa (Cape Town)",
}


def _region_location(region: str) -> str:
    return _REGION_TO_LOCATION.get(region, region)


# ---------------------------------------------------------------------------
# Local pricing cache
# ---------------------------------------------------------------------------


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _cache_get(key: str) -> Optional[float]:  # noqa: UP045
    """Return cached price if present and within TTL, else None."""
    entry = _load_cache().get(key)
    if entry and time.time() - entry.get("ts", 0) < CACHE_TTL_SECONDS:
        return float(entry["price"])
    return None


def _cache_set(key: str, price: float) -> None:
    cache = _load_cache()
    cache[key] = {"price": price, "ts": time.time()}
    _save_cache(cache)


def _cache_get_stale(key: str) -> Optional[float]:  # noqa: UP045
    """Return cached price regardless of TTL (offline fallback)."""
    entry = _load_cache().get(key)
    return float(entry["price"]) if entry else None


# ---------------------------------------------------------------------------
# EC2 on-demand pricing
# ---------------------------------------------------------------------------


def _parse_ec2_price_from_stream(stream: io.TextIOWrapper, instance_type: str) -> float:
    """Stream-parse an AWS EC2 pricing CSV and return the on-demand Linux price."""
    reader = csv.reader(stream)

    # The CSV has ~5 metadata lines before the real header.
    # The header row starts with the literal field "SKU".
    headers: Optional[list[str]] = None  # noqa: UP045
    for row in reader:
        if row and row[0].strip().strip('"') == "SKU":
            headers = [h.strip().strip('"') for h in row]
            break

    if headers is None:
        raise PricingError("Could not locate column headers in AWS EC2 pricing CSV.")

    try:
        idx = {h: i for i, h in enumerate(headers)}
        it_col = idx["Instance Type"]
        os_col = idx["Operating System"]
        ten_col = idx["Tenancy"]
        sw_col = idx["Pre Installed S/W"]
        cap_col = idx["CapacityStatus"]
        term_col = idx["TermType"]
        price_col = idx["PricePerUnit"]
    except KeyError as exc:
        raise PricingError(
            f"Missing expected column in AWS EC2 pricing CSV: {exc}"
        ) from exc

    max_col = max(it_col, os_col, ten_col, sw_col, cap_col, term_col, price_col)
    for row in reader:
        if len(row) <= max_col:
            continue
        if (
            row[it_col] == instance_type
            and row[os_col] == "Linux"
            and row[ten_col] == "Shared"
            and row[sw_col] == "NA"
            and row[cap_col] == "Used"
            and row[term_col] == "OnDemand"
        ):
            try:
                price = float(row[price_col])
                if price > 0:
                    return price
            except ValueError:
                continue

    raise PricingError(
        f"No on-demand Linux price found for instance type {instance_type!r}. "
        "Verify the instance type and region are valid AWS values."
    )


def _fetch_ec2_price_from_api(region: str, instance_type: str) -> float:
    url = _EC2_CSV_URL.format(region=region)
    req = urllib.request.Request(url, headers={"User-Agent": "pat-cli/0.5.0"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        stream = io.TextIOWrapper(resp, encoding="utf-8", errors="replace")
        return _parse_ec2_price_from_stream(stream, instance_type)


def get_ec2_price(
    region: str,
    instance_type: str,
    no_cache: bool = False,
) -> tuple[float, bool]:
    """
    Return (on-demand Linux price USD/hr, from_cache).

    On network failure falls back to stale cache.  Raises PricingError when
    neither the API nor any cached value is available.
    """
    key = f"ec2:{region}:{instance_type}"
    if not no_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached, True

    try:
        price = _fetch_ec2_price_from_api(region, instance_type)
        _cache_set(key, price)
        return price, False
    except Exception as exc:  # noqa: BLE001
        stale = _cache_get_stale(key)
        if stale is not None:
            return stale, True
        raise PricingError(
            f"Failed to fetch EC2 pricing and no local cache is available: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Fargate on-demand pricing
# ---------------------------------------------------------------------------


def _extract_price_from_terms(od_terms: dict, sku: str) -> Optional[float]:  # noqa: UP045
    for term in od_terms.get(sku, {}).values():
        for dim in term.get("priceDimensions", {}).values():
            try:
                price = float(dim.get("pricePerUnit", {}).get("USD", "0"))
                if price > 0:
                    return price
            except (ValueError, AttributeError):
                continue
    return None


def _parse_fargate_rates(data: dict, location: str) -> tuple[float, float]:
    """Extract (vcpu_rate_per_hr, mem_gb_rate_per_hr) from ECS pricing JSON."""
    vcpu_rate: Optional[float] = None  # noqa: UP045
    mem_rate: Optional[float] = None  # noqa: UP045

    od_terms = data.get("terms", {}).get("OnDemand", {})
    for sku, product in data.get("products", {}).items():
        attrs = product.get("attributes", {})
        if attrs.get("location") != location:
            continue
        usage = attrs.get("usagetype", "")
        if "Fargate-vCPU-Hours" in usage and vcpu_rate is None:
            vcpu_rate = _extract_price_from_terms(od_terms, sku)
        elif "Fargate-GB-Hours" in usage and mem_rate is None:
            mem_rate = _extract_price_from_terms(od_terms, sku)
        if vcpu_rate is not None and mem_rate is not None:
            break

    # Fall back to published us-east-1 defaults if region not found in data
    return (
        vcpu_rate if vcpu_rate is not None else 0.04048,
        mem_rate if mem_rate is not None else 0.004445,
    )


def _fetch_fargate_rates_from_api(region: str) -> tuple[float, float]:
    req = urllib.request.Request(  # noqa: S310
        _FARGATE_JSON_URL, headers={"User-Agent": "pat-cli/0.5.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        data = json.loads(resp.read())
    return _parse_fargate_rates(data, _region_location(region))


def get_fargate_rates(
    region: str,
    no_cache: bool = False,
) -> tuple[tuple[float, float], bool]:
    """
    Return ((vcpu_rate, mem_gb_rate), from_cache).

    Falls back to stale cache on network failure.
    """
    key_v = f"fargate:vcpu:{region}"
    key_m = f"fargate:mem:{region}"
    if not no_cache:
        v = _cache_get(key_v)
        m = _cache_get(key_m)
        if v is not None and m is not None:
            return (v, m), True

    try:
        vcpu_rate, mem_rate = _fetch_fargate_rates_from_api(region)
        _cache_set(key_v, vcpu_rate)
        _cache_set(key_m, mem_rate)
        return (vcpu_rate, mem_rate), False
    except Exception as exc:  # noqa: BLE001
        v = _cache_get_stale(key_v)
        m = _cache_get_stale(key_m)
        if v is not None and m is not None:
            return (v, m), True
        raise PricingError(
            f"Failed to fetch Fargate pricing and no local cache is available: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# CostParams builder
# ---------------------------------------------------------------------------


@dataclass
class CloudPricingResult:
    """CostParams derived from cloud pricing, with source metadata."""

    params: CostParams
    from_cache: bool
    source_description: str


def build_cost_params_from_aws(
    region: str,
    instance_type: Optional[str] = None,  # noqa: UP045
    fargate: bool = False,
    vcpu: Optional[float] = None,  # noqa: UP045
    memory_gb: Optional[float] = None,  # noqa: UP045
    no_cache: bool = False,
) -> CloudPricingResult:
    """
    Build CostParams from live AWS on-demand pricing.

    EC2 mode (instance_type set): all four layers are derived from the instance
    price using packing ratios (containers_per_node=16, pods_per_node=8).

    Fargate mode (fargate=True): container/pod layers from task pricing;
    deployment/node estimated at 4×/8× pod cost (no host node in Fargate).
    """
    if fargate:
        if vcpu is None or memory_gb is None:
            raise PricingError(
                "--vcpu and --memory-gb are required when using --fargate pricing."
            )
        (vcpu_rate, mem_rate), from_cache = get_fargate_rates(region, no_cache=no_cache)
        task_cost = vcpu_rate * vcpu + mem_rate * memory_gb
        params = CostParams(
            cost_per_container_hour=vcpu_rate * (vcpu * 0.25)
            + mem_rate * (memory_gb * 0.25),
            cost_per_pod_hour=task_cost,
            cost_per_deployment_hour=task_cost * 4.0,
            cost_per_node_hour=task_cost * 8.0,
        )
        return CloudPricingResult(
            params=params,
            from_cache=from_cache,
            source_description=(
                f"AWS Fargate on-demand · {region} · "
                f"{vcpu} vCPU / {memory_gb} GB  "
                f"(vCPU ${vcpu_rate:.5f}/hr · mem ${mem_rate:.6f}/GB-hr)"
            ),
        )

    if instance_type:
        node_price, from_cache = get_ec2_price(region, instance_type, no_cache=no_cache)
        params = CostParams(
            cost_per_container_hour=node_price / _CONTAINERS_PER_NODE,
            cost_per_pod_hour=node_price / _PODS_PER_NODE,
            cost_per_deployment_hour=node_price,
            cost_per_node_hour=node_price,
        )
        return CloudPricingResult(
            params=params,
            from_cache=from_cache,
            source_description=(
                f"AWS EC2 on-demand · {region} · {instance_type} "
                f"(${node_price:.4f}/hr  →  "
                f"container ${node_price / _CONTAINERS_PER_NODE:.4f} "
                f"· pod ${node_price / _PODS_PER_NODE:.4f})"
            ),
        )

    raise PricingError(
        "Specify either --instance-type <type> or --fargate for cloud pricing."
    )


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class PricingError(Exception):
    """Raised when cloud pricing cannot be fetched or parsed."""
