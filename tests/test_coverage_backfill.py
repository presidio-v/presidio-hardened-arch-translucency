"""Coverage backfill for offline/error/fallback paths.

Targets the uncovered branches in:
  - security.py     — run_dependency_audit failure modes
  - cloud.py        — cache edge cases, network fetch bodies, parser skips,
                      reserved/spot error paths, builder error paths
  - cloud_gcp.py    — price-format errors, fetch body, PricingError reraise
  - cloud_azure.py  — item-type/price skips, PricingError reraise

All network access is mocked — no live API calls.
"""

from __future__ import annotations

import io
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from presidio_arch_translucency.security import run_dependency_audit

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cache_paths(tmp_path, monkeypatch):
    """Redirect the pricing cache to a temp file."""
    monkeypatch.setattr(
        "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
    )
    monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
    return tmp_path


def _urlopen_cm(payload: bytes) -> MagicMock:
    """A urlopen() return value usable as a context manager yielding *payload*."""
    cm = MagicMock()
    cm.__enter__.return_value = io.BytesIO(payload)
    cm.__exit__.return_value = False
    return cm


_EC2_HEADER = (
    "SKU,OfferTermCode,RateCode,TermType,PriceDescription,EffectiveDate,"
    "StartingRange,EndingRange,Unit,PricePerUnit,Currency,RelatedTo,"
    "LeaseContractLength,PurchaseOption,OfferingClass,Product Family,"
    "serviceCode,Location,Location Type,Instance Type,Current Generation,"
    "vCPU,Memory,Storage,Network Performance,Processor Architecture,"
    "Operating System,Tenancy,Pre Installed S/W,CapacityStatus\n"
)

_EC2_META = "FormatVersion,v1.0\nDisclaimer,info\n\n"


def _ec2_row(
    instance_type: str = "m5.large",
    price: str = "0.096",
    term: str = "OnDemand",
    os_: str = "Linux",
    tenancy: str = "Shared",
    sw: str = "NA",
    cap: str = "Used",
    lease: str = "",
    purchase: str = "",
) -> str:
    """A 30-column EC2 pricing CSV data row."""
    return (
        f"SKU,OTC,RATE,{term},desc,2024-01-01T00:00:00Z,0,Inf,Hrs,{price},USD,,"
        f"{lease},{purchase},,Compute Instance,AmazonEC2,US East (N. Virginia),"
        f"AWS Region,{instance_type},Yes,2,8 GiB,EBS only,Up to 10 Gigabit,x86_64,"
        f"{os_},{tenancy},{sw},{cap}\n"
    )


def _stream(text: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(text.encode()), encoding="utf-8")


# ===========================================================================
# security.py — run_dependency_audit failure modes
# ===========================================================================


class TestRunDependencyAuditBranches:
    @patch("presidio_arch_translucency.security.subprocess.run")
    def test_audit_passes_returns_true(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        assert run_dependency_audit() is True

    @patch("presidio_arch_translucency.security.subprocess.run")
    def test_audit_finds_vulnerabilities_returns_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="CVE-2024-0001 found")
        assert run_dependency_audit() is False

    @patch(
        "presidio_arch_translucency.security.subprocess.run",
        side_effect=FileNotFoundError(),
    )
    def test_pip_audit_not_installed_returns_true(self, mock_run):
        # Graceful skip when the audit tool is absent.
        assert run_dependency_audit() is True

    @patch(
        "presidio_arch_translucency.security.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="pip_audit", timeout=60),
    )
    def test_timeout_skipped_returns_true(self, mock_run):
        assert run_dependency_audit(skip_on_error=True) is True

    @patch(
        "presidio_arch_translucency.security.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="pip_audit", timeout=60),
    )
    def test_timeout_raises_when_not_skipping(self, mock_run):
        with pytest.raises(subprocess.TimeoutExpired):
            run_dependency_audit(skip_on_error=False)

    @patch(
        "presidio_arch_translucency.security.subprocess.run",
        side_effect=RuntimeError("unexpected"),
    )
    def test_generic_error_skipped_returns_true(self, mock_run):
        assert run_dependency_audit(skip_on_error=True) is True

    @patch(
        "presidio_arch_translucency.security.subprocess.run",
        side_effect=RuntimeError("unexpected"),
    )
    def test_generic_error_raises_when_not_skipping(self, mock_run):
        with pytest.raises(RuntimeError):
            run_dependency_audit(skip_on_error=False)


# ===========================================================================
# cloud.py — cache edge cases
# ===========================================================================


class TestCacheEdgeCases:
    def test_save_cache_tolerates_chmod_failure(self, cache_paths):
        from presidio_arch_translucency.cloud import _cache_set, _load_cache

        # Some filesystems reject chmod; the value must still be cached.
        with patch.object(Path, "chmod", side_effect=OSError("no chmod here")):
            _cache_set("ec2:us-east-1:m5.large", 0.096)
        assert _load_cache()["ec2:us-east-1:m5.large"]["price"] == pytest.approx(0.096)

    def test_cache_get_returns_none_for_malformed_entry(self, cache_paths):
        from presidio_arch_translucency.cloud import _cache_get, _save_cache

        # ts is non-numeric → float() raises → treated as a miss.
        _save_cache({"k": {"ts": "not-a-number", "price": 0.5}})
        assert _cache_get("k") is None

    def test_cache_get_stale_returns_none_when_price_missing(self, cache_paths):
        from presidio_arch_translucency.cloud import _cache_get_stale, _save_cache

        # Entry without a "price" key → KeyError → None.
        _save_cache({"k": {"ts": time.time()}})
        assert _cache_get_stale("k") is None


# ===========================================================================
# cloud.py — EC2 CSV parser skip branches
# ===========================================================================


class TestEC2ParserSkips:
    def test_missing_column_raises(self):
        from presidio_arch_translucency.cloud import (
            PricingError,
            _parse_ec2_all_prices_from_stream,
        )

        header_no_it = _EC2_HEADER.replace("Instance Type,", "")
        csv_text = _EC2_META + header_no_it + _ec2_row()
        with pytest.raises(PricingError, match="Missing expected column"):
            _parse_ec2_all_prices_from_stream(_stream(csv_text), "m5.large")

    def test_short_row_and_bad_and_zero_prices_are_skipped(self):
        from presidio_arch_translucency.cloud import _parse_ec2_all_prices_from_stream

        csv_text = (
            _EC2_META
            + _EC2_HEADER
            + "SHORT,row,only,three\n"  # len <= max_col → skipped
            + _ec2_row(price="not-a-number")  # ValueError on float() → skipped
            + _ec2_row(price="0")  # price <= 0 → skipped
            + _ec2_row(price="0.096")  # the real on-demand price
        )
        od, r1, r3 = _parse_ec2_all_prices_from_stream(_stream(csv_text), "m5.large")
        assert od == pytest.approx(0.096)
        assert r1 is None and r3 is None


# ===========================================================================
# cloud.py — network fetch bodies (mocked urlopen)
# ===========================================================================


class TestFetchBodies:
    def test_fetch_ec2_price_parses_and_seeds_reserved_cache(self, cache_paths):
        from presidio_arch_translucency.cloud import (
            _cache_get,
            _fetch_ec2_price_from_api,
        )

        csv_text = (
            _EC2_META
            + _EC2_HEADER
            + _ec2_row(price="0.096", term="OnDemand")
            + _ec2_row(
                price="0.060", term="Reserved", lease="1yr", purchase="No Upfront"
            )
            + _ec2_row(
                price="0.048", term="Reserved", lease="3yr", purchase="No Upfront"
            )
        )
        with patch(
            "urllib.request.urlopen", return_value=_urlopen_cm(csv_text.encode())
        ):
            price = _fetch_ec2_price_from_api("us-east-1", "m5.large")

        assert price == pytest.approx(0.096)
        # Reserved prices were seeded into the cache as a side-effect.
        assert _cache_get("ec2:reserved1yr:us-east-1:m5.large") == pytest.approx(0.060)
        assert _cache_get("ec2:reserved3yr:us-east-1:m5.large") == pytest.approx(0.048)

    def test_fetch_fargate_rates_parses_json(self, cache_paths):
        import json

        from presidio_arch_translucency.cloud import _fetch_fargate_rates_from_api

        loc = "US East (N. Virginia)"
        fargate = {
            "products": {
                "V": {
                    "attributes": {"location": loc, "usagetype": "Fargate-vCPU-Hours"}
                },
                "M": {"attributes": {"location": loc, "usagetype": "Fargate-GB-Hours"}},
            },
            "terms": {
                "OnDemand": {
                    "V": {
                        "V.T": {
                            "priceDimensions": {
                                "d": {"pricePerUnit": {"USD": "0.04048"}}
                            }
                        }
                    },
                    "M": {
                        "M.T": {
                            "priceDimensions": {
                                "d": {"pricePerUnit": {"USD": "0.004445"}}
                            }
                        }
                    },
                }
            },
        }
        payload = json.dumps(fargate).encode()
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            vcpu, mem = _fetch_fargate_rates_from_api("us-east-1")

        assert vcpu == pytest.approx(0.04048)
        assert mem == pytest.approx(0.004445)

    def test_extract_price_from_terms_skips_non_numeric(self):
        from presidio_arch_translucency.cloud import _extract_price_from_terms

        od_terms = {
            "SKU": {
                "SKU.T": {
                    "priceDimensions": {
                        "SKU.T.D": {"pricePerUnit": {"USD": "not-a-number"}}
                    }
                }
            }
        }
        # Non-numeric USD → ValueError caught → returns None.
        assert _extract_price_from_terms(od_terms, "SKU") is None


# ===========================================================================
# cloud.py — reserved / spot error paths
# ===========================================================================


class TestReservedAndSpotErrors:
    def test_reserved_raises_when_fetch_fails_and_no_cache(self, cache_paths):
        from presidio_arch_translucency.cloud import (
            PricingError,
            get_ec2_reserved_prices,
        )

        with patch(
            "presidio_arch_translucency.cloud._fetch_ec2_price_from_api",
            side_effect=OSError("network down"),
        ):
            with pytest.raises(PricingError, match="no local cache"):
                get_ec2_reserved_prices("us-east-1", "m5.large", no_cache=True)

    def test_spot_raises_when_history_empty(self, cache_paths, monkeypatch):
        from presidio_arch_translucency.cloud import PricingError, get_ec2_spot_price

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_boto3.client.return_value = mock_ec2
        mock_ec2.describe_spot_price_history.return_value = {"SpotPriceHistory": []}

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            with pytest.raises(PricingError, match="No spot price history"):
                get_ec2_spot_price("us-east-1", "m5.large", no_cache=True)

    def test_spot_falls_back_to_stale_on_api_error(self, cache_paths, monkeypatch):
        from presidio_arch_translucency.cloud import (
            SPOT_CACHE_TTL_SECONDS,
            _save_cache,
            get_ec2_spot_price,
        )

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        old_ts = time.time() - SPOT_CACHE_TTL_SECONDS - 10
        _save_cache({"ec2:spot:us-east-1:m5.large": {"price": 0.032, "ts": old_ts}})

        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_boto3.client.return_value = mock_ec2
        mock_ec2.describe_spot_price_history.side_effect = RuntimeError("api blew up")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            price, from_cache = get_ec2_spot_price(
                "us-east-1", "m5.large", no_cache=True
            )

        assert price == pytest.approx(0.032)
        assert from_cache is True

    def test_spot_raises_when_api_error_and_no_cache(self, cache_paths, monkeypatch):
        from presidio_arch_translucency.cloud import PricingError, get_ec2_spot_price

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
        mock_boto3 = MagicMock()
        mock_ec2 = MagicMock()
        mock_boto3.client.return_value = mock_ec2
        mock_ec2.describe_spot_price_history.side_effect = RuntimeError("api blew up")

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            with pytest.raises(PricingError, match="Failed to fetch spot price"):
                get_ec2_spot_price("us-east-1", "m5.large", no_cache=True)

    def test_fargate_falls_back_to_stale_cache_on_error(self, cache_paths):
        from presidio_arch_translucency.cloud import (
            CACHE_TTL_SECONDS,
            _save_cache,
            get_fargate_rates,
        )

        old_ts = time.time() - CACHE_TTL_SECONDS - 10
        _save_cache(
            {
                "fargate:vcpu:us-east-1": {"price": 0.04048, "ts": old_ts},
                "fargate:mem:us-east-1": {"price": 0.004445, "ts": old_ts},
            }
        )
        with patch(
            "presidio_arch_translucency.cloud._fetch_fargate_rates_from_api",
            side_effect=OSError("network down"),
        ):
            (vcpu, mem), from_cache = get_fargate_rates("us-east-1")

        assert vcpu == pytest.approx(0.04048)
        assert mem == pytest.approx(0.004445)
        assert from_cache is True


# ===========================================================================
# cloud.py — build_cost_params_from_aws error/tier branches
# ===========================================================================


class TestBuildAwsTierErrors:
    def test_show_reserved_swallows_pricing_error(self, cache_paths):
        from presidio_arch_translucency.cloud import (
            PricingError,
            build_cost_params_from_aws,
        )

        with (
            patch(
                "presidio_arch_translucency.cloud.get_ec2_price",
                return_value=(0.096, False),
            ),
            patch(
                "presidio_arch_translucency.cloud.get_ec2_reserved_prices",
                side_effect=PricingError("no reserved"),
            ),
        ):
            result = build_cost_params_from_aws(
                region="us-east-1", instance_type="m5.large", show_reserved=True
            )
        assert result.reserved_1yr is None
        assert result.reserved_3yr is None

    def test_show_spot_populates_tier(self, cache_paths):
        from presidio_arch_translucency.cloud import build_cost_params_from_aws

        with (
            patch(
                "presidio_arch_translucency.cloud.get_ec2_price",
                return_value=(0.096, False),
            ),
            patch(
                "presidio_arch_translucency.cloud.get_ec2_spot_price",
                return_value=(0.03, False),
            ),
        ):
            result = build_cost_params_from_aws(
                region="us-east-1", instance_type="m5.large", show_spot=True
            )
        assert result.spot is not None
        assert result.spot.params.cost_per_node_hour == pytest.approx(0.03)
        assert "Spot" in result.spot.source_description

    def test_show_spot_swallows_pricing_error(self, cache_paths):
        from presidio_arch_translucency.cloud import (
            PricingError,
            build_cost_params_from_aws,
        )

        with (
            patch(
                "presidio_arch_translucency.cloud.get_ec2_price",
                return_value=(0.096, False),
            ),
            patch(
                "presidio_arch_translucency.cloud.get_ec2_spot_price",
                side_effect=PricingError("no spot"),
            ),
        ):
            result = build_cost_params_from_aws(
                region="us-east-1", instance_type="m5.large", show_spot=True
            )
        assert result.spot is None


# ===========================================================================
# cloud_gcp.py — price-format errors, fetch body, PricingError reraise
# ===========================================================================


class TestGCPBackfill:
    def test_parse_dict_entry_non_numeric_price_raises(self):
        from presidio_arch_translucency.cloud import PricingError
        from presidio_arch_translucency.cloud_gcp import _parse_gcp_price

        data = {
            "gcp_price_list": {
                "CP-COMPUTEENGINE-VMIMAGE-N2-STANDARD-4": {"us-central1": "free"}
            }
        }
        with pytest.raises(PricingError, match="Unexpected price format"):
            _parse_gcp_price(data, "us-central1", "n2-standard-4")

    def test_parse_scalar_entry_returns_price(self):
        from presidio_arch_translucency.cloud_gcp import _parse_gcp_price

        data = {"gcp_price_list": {"CP-COMPUTEENGINE-VMIMAGE-N2-STANDARD-4": 0.25}}
        assert _parse_gcp_price(data, "us-central1", "n2-standard-4") == pytest.approx(
            0.25
        )

    def test_parse_scalar_entry_non_numeric_raises(self):
        from presidio_arch_translucency.cloud import PricingError
        from presidio_arch_translucency.cloud_gcp import _parse_gcp_price

        data = {"gcp_price_list": {"CP-COMPUTEENGINE-VMIMAGE-N2-STANDARD-4": "n/a"}}
        with pytest.raises(PricingError, match="Unexpected GCP pricing entry"):
            _parse_gcp_price(data, "us-central1", "n2-standard-4")

    def test_fetch_gcp_price_parses_json(self):
        from presidio_arch_translucency.cloud_gcp import _fetch_gcp_price_from_api

        payload = (
            b'{"gcp_price_list": {"CP-COMPUTEENGINE-VMIMAGE-N2-STANDARD-4":'
            b' {"us-central1": 0.1942}}}'
        )
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            price = _fetch_gcp_price_from_api("us-central1", "n2-standard-4")
        assert price == pytest.approx(0.1942)

    def test_pricing_error_propagates_not_wrapped(self, cache_paths):
        from presidio_arch_translucency.cloud import PricingError
        from presidio_arch_translucency.cloud_gcp import get_gcp_instance_price

        with patch(
            "presidio_arch_translucency.cloud_gcp._fetch_gcp_price_from_api",
            side_effect=PricingError("machine type not found"),
        ):
            with pytest.raises(PricingError, match="machine type not found"):
                get_gcp_instance_price("us-central1", "z9-bogus-99", no_cache=True)


# ===========================================================================
# cloud_azure.py — item-type/price skips, PricingError reraise
# ===========================================================================


class TestAzureBackfill:
    def test_parse_skips_non_consumption_item(self):
        from presidio_arch_translucency.cloud_azure import _parse_azure_price

        data = {
            "Items": [
                {"type": "Reservation", "skuName": "D2s v3", "retailPrice": 99.0},
                {
                    "type": "Consumption",
                    "skuName": "D2s v3",
                    "armRegionName": "eastus",
                    "retailPrice": 0.096,
                },
            ]
        }
        price = _parse_azure_price(data, "D2s v3", "eastus", spot=False)
        assert price == pytest.approx(0.096)

    def test_parse_skips_non_numeric_price(self):
        from presidio_arch_translucency.cloud_azure import _parse_azure_price

        data = {
            "Items": [
                {
                    "type": "Consumption",
                    "skuName": "D2s v3",
                    "armRegionName": "eastus",
                    "retailPrice": "free",
                },
                {
                    "type": "Consumption",
                    "skuName": "D2s v3",
                    "armRegionName": "eastus",
                    "retailPrice": 0.096,
                },
            ]
        }
        price = _parse_azure_price(data, "D2s v3", "eastus", spot=False)
        assert price == pytest.approx(0.096)

    def test_pricing_error_propagates_not_wrapped(self, cache_paths):
        from presidio_arch_translucency.cloud import PricingError
        from presidio_arch_translucency.cloud_azure import get_azure_vm_price

        with patch(
            "presidio_arch_translucency.cloud_azure._fetch_azure_vm_price_from_api",
            side_effect=PricingError("SKU not found"),
        ):
            with pytest.raises(PricingError, match="SKU not found"):
                get_azure_vm_price("eastus", "Z99 bogus", no_cache=True)
