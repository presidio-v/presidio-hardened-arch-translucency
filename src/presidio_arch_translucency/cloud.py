"""Cloud pricing integration (v0.6.0).

Fetches live EC2 and Fargate on-demand, reserved, and spot prices from
the AWS Pricing API.  Results are cached locally to avoid repeated
network round-trips.

Cache location: ~/.pat/pricing-cache.json
TTL: 24 hours (on-demand / reserved), 5 minutes (spot)
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from presidio_arch_translucency.cost import CostParams

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".pat"
CACHE_FILE = CACHE_DIR / "pricing-cache.json"
CACHE_TTL_SECONDS = 86_400  # 24 hours (on-demand and reserved)
SPOT_CACHE_TTL_SECONDS = 300  # 5 minutes (spot prices are volatile)

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
    # Restrict to the owner: the cache drives cost output, so another local
    # user must not be able to read or poison it.
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE_FILE.write_text(json.dumps(cache, indent=2))
    try:
        CACHE_FILE.chmod(0o600)
    except OSError:
        pass


def _cache_get(key: str, ttl: int = CACHE_TTL_SECONDS) -> Optional[float]:  # noqa: UP045
    """Return cached price if present, valid, and within TTL, else None."""
    entry = _load_cache().get(key)
    if not isinstance(entry, dict):
        return None
    try:
        if time.time() - float(entry.get("ts", 0)) < ttl:
            return float(entry["price"])
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _cache_set(key: str, price: float) -> None:
    cache = _load_cache()
    cache[key] = {"price": price, "ts": time.time()}
    _save_cache(cache)


def _cache_get_stale(key: str) -> Optional[float]:  # noqa: UP045
    """Return cached price regardless of TTL (offline fallback)."""
    entry = _load_cache().get(key)
    if not isinstance(entry, dict):
        return None
    try:
        return float(entry["price"])
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# EC2 on-demand pricing
# ---------------------------------------------------------------------------


def _parse_ec2_all_prices_from_stream(
    stream: io.TextIOWrapper,
    instance_type: str,
) -> tuple[float, Optional[float], Optional[float]]:  # noqa: UP045
    """
    Single-pass parse of an AWS EC2 pricing CSV.

    Returns (on_demand, reserved_1yr_no_upfront, reserved_3yr_no_upfront).
    Reserved values may be None if not found in the CSV.
    Raises PricingError if on-demand price is not found.
    """
    reader = csv.reader(stream)

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
        lease_col = idx["LeaseContractLength"]
        po_col = idx["PurchaseOption"]
    except KeyError as exc:
        raise PricingError(
            f"Missing expected column in AWS EC2 pricing CSV: {exc}"
        ) from exc

    max_col = max(
        it_col, os_col, ten_col, sw_col, cap_col, term_col, price_col, lease_col, po_col
    )  # noqa: E501

    on_demand: Optional[float] = None  # noqa: UP045
    reserved_1yr: Optional[float] = None  # noqa: UP045
    reserved_3yr: Optional[float] = None  # noqa: UP045

    for row in reader:
        if len(row) <= max_col:
            continue
        if not (
            row[it_col] == instance_type
            and row[os_col] == "Linux"
            and row[ten_col] == "Shared"
            and row[sw_col] == "NA"
            and row[cap_col] == "Used"
        ):
            continue
        term = row[term_col]
        try:
            price = float(row[price_col])
        except ValueError:
            continue
        if price <= 0:
            continue

        if term == "OnDemand" and on_demand is None:
            on_demand = price
        elif term == "Reserved" and row[po_col] == "No Upfront":
            lease = row[lease_col]
            if lease == "1yr" and reserved_1yr is None:
                reserved_1yr = price
            elif lease == "3yr" and reserved_3yr is None:
                reserved_3yr = price

        if (
            on_demand is not None
            and reserved_1yr is not None
            and reserved_3yr is not None
        ):  # noqa: E501
            break  # early exit once all three are found

    if on_demand is None:
        raise PricingError(
            f"No on-demand Linux price found for instance type {instance_type!r}. "
            "Verify the instance type and region are valid AWS values."
        )

    return on_demand, reserved_1yr, reserved_3yr


def _parse_ec2_price_from_stream(stream: io.TextIOWrapper, instance_type: str) -> float:
    """Stream-parse an AWS EC2 pricing CSV and return the on-demand Linux price."""
    on_demand, _, _ = _parse_ec2_all_prices_from_stream(stream, instance_type)
    return on_demand


def _fetch_ec2_price_from_api(region: str, instance_type: str) -> float:
    """Fetch on-demand price from API. Opportunistically seeds reserved price cache."""
    # Encode the region so a malformed value cannot alter the request path.
    url = _EC2_CSV_URL.format(region=urllib.parse.quote(region, safe=""))
    req = urllib.request.Request(url, headers={"User-Agent": "pat-cli/0.6.0"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
        stream = io.TextIOWrapper(resp, encoding="utf-8", errors="replace")
        on_demand, reserved_1yr, reserved_3yr = _parse_ec2_all_prices_from_stream(
            stream, instance_type
        )
    # Seed reserved cache at no extra network cost
    if reserved_1yr is not None:
        _cache_set(f"ec2:reserved1yr:{region}:{instance_type}", reserved_1yr)
    if reserved_3yr is not None:
        _cache_set(f"ec2:reserved3yr:{region}:{instance_type}", reserved_3yr)
    return on_demand


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
        _FARGATE_JSON_URL, headers={"User-Agent": "pat-cli/0.6.0"}
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
# Error type  (defined before builders so builders can raise it)
# ---------------------------------------------------------------------------


class PricingError(Exception):
    """Raised when cloud pricing cannot be fetched or parsed."""


# ---------------------------------------------------------------------------
# AWS reserved pricing
# ---------------------------------------------------------------------------


def get_ec2_reserved_prices(
    region: str,
    instance_type: str,
    no_cache: bool = False,
) -> tuple[tuple[float, float], bool]:
    """
    Return ((reserved_1yr_no_upfront, reserved_3yr_no_upfront), from_cache).

    Checks the cache first (seeded opportunistically by get_ec2_price).
    On cache miss fetches the EC2 CSV, which also refreshes the on-demand cache.
    Raises PricingError if reserved pricing is not available for this instance.
    """
    key1 = f"ec2:reserved1yr:{region}:{instance_type}"
    key3 = f"ec2:reserved3yr:{region}:{instance_type}"
    if not no_cache:
        r1 = _cache_get(key1)
        r3 = _cache_get(key3)
        if r1 is not None and r3 is not None:
            return (r1, r3), True

    try:
        # _fetch_ec2_price_from_api seeds reserved cache as a side-effect
        _fetch_ec2_price_from_api(region, instance_type)
    except Exception as exc:  # noqa: BLE001
        r1 = _cache_get_stale(key1)
        r3 = _cache_get_stale(key3)
        if r1 is not None and r3 is not None:
            return (r1, r3), True
        raise PricingError(
            f"Failed to fetch EC2 reserved pricing and no local cache is available: {exc}"  # noqa: E501
        ) from exc

    r1 = _cache_get(key1)
    r3 = _cache_get(key3)
    if r1 is not None and r3 is not None:
        return (r1, r3), False

    raise PricingError(
        f"No reserved (No Upfront) pricing found for {instance_type!r} in {region}. "
        "Reserved pricing may not be available for this instance/region."
    )


# ---------------------------------------------------------------------------
# AWS spot pricing (requires boto3 + AWS credentials)
# ---------------------------------------------------------------------------


def get_ec2_spot_price(
    region: str,
    instance_type: str,
    no_cache: bool = False,
) -> tuple[float, bool]:
    """
    Return (current spot price USD/hr, from_cache).

    Requires boto3 installed and AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars.
    Cache TTL: 5 minutes (spot prices are volatile).
    """
    import os  # noqa: PLC0415

    key = f"ec2:spot:{region}:{instance_type}"
    if not no_cache:
        cached = _cache_get(key, ttl=SPOT_CACHE_TTL_SECONDS)
        if cached is not None:
            return cached, True

    if not os.environ.get("AWS_ACCESS_KEY_ID") or not os.environ.get(
        "AWS_SECRET_ACCESS_KEY"
    ):
        raise PricingError(
            "AWS credentials are required for spot pricing. "
            "Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables."
        )

    try:
        import boto3  # noqa: PLC0415
    except ImportError as exc:
        raise PricingError(
            "boto3 is required for spot pricing. "
            "Install with: pip install 'presidio-hardened-arch-translucency[spot]'"
        ) from exc

    try:
        ec2 = boto3.client("ec2", region_name=region)
        response = ec2.describe_spot_price_history(
            InstanceTypes=[instance_type],
            ProductDescriptions=["Linux/UNIX"],
            MaxResults=1,
        )
        prices = response.get("SpotPriceHistory", [])
        if not prices:
            raise PricingError(
                f"No spot price history found for {instance_type!r} in {region}."
            )
        price = float(prices[0]["SpotPrice"])
        _cache_set(key, price)
        return price, False
    except PricingError:
        raise
    except Exception as exc:  # noqa: BLE001
        stale = _cache_get_stale(key)
        if stale is not None:
            return stale, True
        raise PricingError(
            f"Failed to fetch spot price for {instance_type!r} in {region}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# CostParams builders
# ---------------------------------------------------------------------------


@dataclass
class CloudPricingResult:
    """CostParams derived from cloud pricing, with source metadata."""

    params: CostParams
    from_cache: bool
    source_description: str


@dataclass
class TieredPricingResult:
    """Multi-tier pricing result (on-demand, reserved, spot) for a single provider."""

    on_demand: CloudPricingResult
    reserved_1yr: Optional[CloudPricingResult] = field(default=None)  # noqa: UP045
    reserved_3yr: Optional[CloudPricingResult] = field(default=None)  # noqa: UP045
    spot: Optional[CloudPricingResult] = field(default=None)  # noqa: UP045


def _ec2_params(node_price: float) -> CostParams:
    return CostParams(
        cost_per_container_hour=node_price / _CONTAINERS_PER_NODE,
        cost_per_pod_hour=node_price / _PODS_PER_NODE,
        cost_per_deployment_hour=node_price,
        cost_per_node_hour=node_price,
    )


def build_cost_params_from_aws(
    region: str,
    instance_type: Optional[str] = None,  # noqa: UP045
    fargate: bool = False,
    vcpu: Optional[float] = None,  # noqa: UP045
    memory_gb: Optional[float] = None,  # noqa: UP045
    no_cache: bool = False,
    show_reserved: bool = False,
    show_spot: bool = False,
) -> TieredPricingResult:
    """
    Build tiered CostParams from live AWS pricing.

    EC2 mode (instance_type set): derives all four layers from the instance
    price using packing ratios (containers_per_node=16, pods_per_node=8).
    Optionally includes reserved (--show-reserved) and spot (--spot) tiers.

    Fargate mode (fargate=True): container/pod layers from task pricing;
    deployment/node estimated at 4×/8× pod cost.
    Reserved and spot pricing are not applicable in Fargate mode.
    """
    import logging  # noqa: PLC0415

    log = logging.getLogger(__name__)

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
        on_demand = CloudPricingResult(
            params=params,
            from_cache=from_cache,
            source_description=(
                f"AWS Fargate on-demand · {region} · "
                f"{vcpu} vCPU / {memory_gb} GB  "
                f"(vCPU ${vcpu_rate:.5f}/hr · mem ${mem_rate:.6f}/GB-hr)"
            ),
        )
        return TieredPricingResult(on_demand=on_demand)

    if instance_type:
        node_price, from_cache = get_ec2_price(region, instance_type, no_cache=no_cache)
        on_demand = CloudPricingResult(
            params=_ec2_params(node_price),
            from_cache=from_cache,
            source_description=(
                f"AWS EC2 on-demand · {region} · {instance_type} "
                f"(${node_price:.4f}/hr  →  "
                f"container ${node_price / _CONTAINERS_PER_NODE:.4f} "
                f"· pod ${node_price / _PODS_PER_NODE:.4f})"
            ),
        )

        r1_result: Optional[CloudPricingResult] = None  # noqa: UP045
        r3_result: Optional[CloudPricingResult] = None  # noqa: UP045
        if show_reserved:
            try:
                (r1_price, r3_price), res_from_cache = get_ec2_reserved_prices(
                    region, instance_type, no_cache=no_cache
                )
                r1_result = CloudPricingResult(
                    params=_ec2_params(r1_price),
                    from_cache=res_from_cache,
                    source_description=(
                        f"AWS EC2 1yr Reserved (No Upfront) · {region} · {instance_type} "  # noqa: E501
                        f"(${r1_price:.4f}/hr)"
                    ),
                )
                r3_result = CloudPricingResult(
                    params=_ec2_params(r3_price),
                    from_cache=res_from_cache,
                    source_description=(
                        f"AWS EC2 3yr Reserved (No Upfront) · {region} · {instance_type} "  # noqa: E501
                        f"(${r3_price:.4f}/hr)"
                    ),
                )
            except PricingError as exc:
                log.warning("Reserved pricing not available: %s", exc)

        spot_result: Optional[CloudPricingResult] = None  # noqa: UP045
        if show_spot:
            try:
                spot_price, spot_from_cache = get_ec2_spot_price(
                    region, instance_type, no_cache=no_cache
                )
                spot_result = CloudPricingResult(
                    params=_ec2_params(spot_price),
                    from_cache=spot_from_cache,
                    source_description=(
                        f"AWS EC2 Spot · {region} · {instance_type} "
                        f"(${spot_price:.4f}/hr  ⚠ spot — interruption risk)"
                    ),
                )
            except PricingError as exc:
                log.warning("Spot pricing not available: %s", exc)

        return TieredPricingResult(
            on_demand=on_demand,
            reserved_1yr=r1_result,
            reserved_3yr=r3_result,
            spot=spot_result,
        )

    raise PricingError(
        "Specify either --instance-type <type> or --fargate for cloud pricing."
    )
