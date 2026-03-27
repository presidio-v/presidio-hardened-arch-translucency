"""Tests for cloud pricing integration (v0.5.0)."""

from __future__ import annotations

import io
import time
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.cloud import (
    CACHE_TTL_SECONDS,
    CloudPricingResult,
    PricingError,
    _cache_get,
    _cache_get_stale,
    _cache_set,
    _load_cache,
    _parse_ec2_price_from_stream,
    _parse_fargate_rates,
    _region_location,
    _save_cache,
    build_cost_params_from_aws,
    get_ec2_price,
    get_fargate_rates,
)

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EC2_CSV_HEADER = (
    "SKU,OfferTermCode,RateCode,TermType,PriceDescription,EffectiveDate,"
    "StartingRange,EndingRange,Unit,PricePerUnit,Currency,RelatedTo,"
    "LeaseContractLength,PurchaseOption,OfferingClass,Product Family,"
    "serviceCode,Location,Location Type,Instance Type,Current Generation,"
    "vCPU,Memory,Storage,Network Performance,Processor Architecture,"
    "Operating System,Tenancy,Pre Installed S/W,CapacityStatus\n"
)

_EC2_METADATA = (
    "FormatVersion,v1.0\n"
    'Disclaimer,"This pricing list is for informational purposes only."\n'
    "\n"
    "Publication Date,2024-01-01T00:00:00Z\n"
    "\n"
)


def _make_ec2_csv(
    instance_type: str,
    price: str,
    os: str = "Linux",
    tenancy: str = "Shared",
    sw: str = "NA",
    cap: str = "Used",
    term: str = "OnDemand",
) -> str:
    row = (
        f"SKU001,JRTCKXETXF,SKU001.JRTCKXETXF.6YS6EN2CT7,{term},"
        f"${price} per On Demand Linux {instance_type} Instance Hour,"
        f"2024-01-01T00:00:00Z,0,Inf,Hrs,{price},USD,,,,,"
        f"Compute Instance,AmazonEC2,US East (N. Virginia),AWS Region,"
        f"{instance_type},Yes,2,8 GiB,EBS only,Up to 10 Gigabit,x86_64,"
        f"{os},{tenancy},{sw},{cap}\n"
    )
    return _EC2_METADATA + _EC2_HEADER_LINE + row


_EC2_HEADER_LINE = _EC2_CSV_HEADER

_FARGATE_JSON = {
    "products": {
        "VSKU001": {
            "sku": "VSKU001",
            "attributes": {
                "location": "US East (N. Virginia)",
                "usagetype": "Fargate-vCPU-Hours:perCPU",
            },
        },
        "MSKU001": {
            "sku": "MSKU001",
            "attributes": {
                "location": "US East (N. Virginia)",
                "usagetype": "Fargate-GB-Hours:perGB",
            },
        },
    },
    "terms": {
        "OnDemand": {
            "VSKU001": {
                "VSKU001.TERM": {
                    "priceDimensions": {
                        "VSKU001.TERM.DIM": {"pricePerUnit": {"USD": "0.04048"}}
                    }
                }
            },
            "MSKU001": {
                "MSKU001.TERM": {
                    "priceDimensions": {
                        "MSKU001.TERM.DIM": {"pricePerUnit": {"USD": "0.004445"}}
                    }
                }
            },
        }
    },
}


# ---------------------------------------------------------------------------
# Region mapping
# ---------------------------------------------------------------------------


def test_region_location_known():
    assert _region_location("us-east-1") == "US East (N. Virginia)"
    assert _region_location("eu-west-1") == "Europe (Ireland)"
    assert _region_location("ap-northeast-1") == "Asia Pacific (Tokyo)"


def test_region_location_unknown_returns_raw():
    assert _region_location("xx-unknown-99") == "xx-unknown-99"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


class TestPricingCache:
    def test_save_and_load(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        _save_cache({"key": {"price": 0.096, "ts": time.time()}})
        loaded = _load_cache()
        assert "key" in loaded
        assert loaded["key"]["price"] == pytest.approx(0.096)

    def test_cache_get_hit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        _cache_set("ec2:us-east-1:m5.large", 0.096)
        result = _cache_get("ec2:us-east-1:m5.large")
        assert result == pytest.approx(0.096)

    def test_cache_get_miss_when_expired(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        old_ts = time.time() - CACHE_TTL_SECONDS - 1
        _save_cache({"ec2:us-east-1:m5.large": {"price": 0.096, "ts": old_ts}})
        assert _cache_get("ec2:us-east-1:m5.large") is None

    def test_cache_get_stale_ignores_ttl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        old_ts = time.time() - CACHE_TTL_SECONDS - 3600
        _save_cache({"ec2:us-east-1:m5.large": {"price": 0.096, "ts": old_ts}})
        assert _cache_get_stale("ec2:us-east-1:m5.large") == pytest.approx(0.096)

    def test_cache_get_returns_none_for_missing_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        assert _cache_get("nonexistent:key") is None
        assert _cache_get_stale("nonexistent:key") is None

    def test_load_cache_handles_corrupt_file(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("{INVALID JSON")
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_FILE", cache_file)
        assert _load_cache() == {}


# ---------------------------------------------------------------------------
# EC2 CSV parsing
# ---------------------------------------------------------------------------


class TestParseEC2CSV:
    def _stream(self, csv_text: str) -> io.TextIOWrapper:
        return io.TextIOWrapper(io.BytesIO(csv_text.encode()), encoding="utf-8")

    def test_parses_valid_row(self):
        csv_text = _make_ec2_csv("m5.large", "0.0960000000")
        stream = self._stream(csv_text)
        price = _parse_ec2_price_from_stream(stream, "m5.large")
        assert price == pytest.approx(0.096)

    def test_skips_reserved_term(self):
        # OnDemand row missing — only a Reserved row present
        row = (
            "SKU002,4NA7Y494T4,SKU002.4NA7Y494T4.6YS6EN2CT7,Reserved,"
            "$0.060 per Reserved Linux m5.large Instance Hour,"
            "2024-01-01T00:00:00Z,0,Inf,Hrs,0.0600000000,USD,,,1yr,All Upfront,,"
            "Compute Instance,AmazonEC2,US East (N. Virginia),AWS Region,"
            "m5.large,Yes,2,8 GiB,EBS only,Up to 10 Gigabit,x86_64,"
            "Linux,Shared,NA,Used\n"
        )
        csv_text = _EC2_METADATA + _EC2_HEADER_LINE + row
        stream = self._stream(csv_text)
        with pytest.raises(PricingError, match="No on-demand Linux price"):
            _parse_ec2_price_from_stream(stream, "m5.large")

    def test_skips_windows_os(self):
        csv_text = _make_ec2_csv("m5.large", "0.192", os="Windows")
        # Append a Linux row with correct price after the Windows row
        linux_row = (
            "SKU002,JRTCKXETXF,SKU002.JRTCKXETXF.6YS6EN2CT7,OnDemand,"
            "$0.096 per On Demand Linux m5.large Instance Hour,"
            "2024-01-01T00:00:00Z,0,Inf,Hrs,0.0960000000,USD,,,,,"
            "Compute Instance,AmazonEC2,US East (N. Virginia),AWS Region,"
            "m5.large,Yes,2,8 GiB,EBS only,Up to 10 Gigabit,x86_64,"
            "Linux,Shared,NA,Used\n"
        )
        stream = self._stream(csv_text + linux_row)
        price = _parse_ec2_price_from_stream(stream, "m5.large")
        assert price == pytest.approx(0.096)

    def test_instance_not_found_raises(self):
        csv_text = _make_ec2_csv("m5.large", "0.096")
        stream = self._stream(csv_text)
        with pytest.raises(PricingError, match="No on-demand Linux price"):
            _parse_ec2_price_from_stream(stream, "p4d.24xlarge")

    def test_no_header_raises(self):
        stream = self._stream("FormatVersion,v1.0\nDisclaimer,foo\n")
        with pytest.raises(PricingError, match="column headers"):
            _parse_ec2_price_from_stream(stream, "m5.large")


# ---------------------------------------------------------------------------
# Fargate JSON parsing
# ---------------------------------------------------------------------------


class TestParseFargateRates:
    def test_parses_known_region(self):
        vcpu_rate, mem_rate = _parse_fargate_rates(
            _FARGATE_JSON, "US East (N. Virginia)"
        )
        assert vcpu_rate == pytest.approx(0.04048)
        assert mem_rate == pytest.approx(0.004445)

    def test_unknown_region_returns_defaults(self):
        vcpu_rate, mem_rate = _parse_fargate_rates(_FARGATE_JSON, "Narnia (North)")
        assert vcpu_rate == pytest.approx(0.04048)
        assert mem_rate == pytest.approx(0.004445)

    def test_empty_data_returns_defaults(self):
        vcpu_rate, mem_rate = _parse_fargate_rates({}, "US East (N. Virginia)")
        assert vcpu_rate == pytest.approx(0.04048)
        assert mem_rate == pytest.approx(0.004445)


# ---------------------------------------------------------------------------
# get_ec2_price — with mocked HTTP
# ---------------------------------------------------------------------------


class TestGetEC2Price:
    def test_returns_price_from_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud._fetch_ec2_price_from_api",
            return_value=0.096,
        ):
            price, from_cache = get_ec2_price("us-east-1", "m5.large", no_cache=True)

        assert price == pytest.approx(0.096)
        assert from_cache is False

    def test_caches_result_after_api_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud._fetch_ec2_price_from_api",
            return_value=0.096,
        ):
            get_ec2_price("us-east-1", "m5.large", no_cache=True)
            # Second call: API should not be invoked again
            with patch(
                "presidio_arch_translucency.cloud._fetch_ec2_price_from_api"
            ) as mock_api:
                price, from_cache = get_ec2_price("us-east-1", "m5.large")

        mock_api.assert_not_called()
        assert price == pytest.approx(0.096)
        assert from_cache is True

    def test_uses_cache_without_api_call(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        _cache_set("ec2:us-east-1:m5.large", 0.096)

        with patch(
            "presidio_arch_translucency.cloud._fetch_ec2_price_from_api"
        ) as mock_api:
            price, from_cache = get_ec2_price("us-east-1", "m5.large")

        mock_api.assert_not_called()
        assert price == pytest.approx(0.096)
        assert from_cache is True

    def test_falls_back_to_stale_cache_on_network_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        old_ts = time.time() - CACHE_TTL_SECONDS - 1
        _save_cache({"ec2:us-east-1:m5.large": {"price": 0.096, "ts": old_ts}})

        with patch(
            "presidio_arch_translucency.cloud._fetch_ec2_price_from_api",
            side_effect=OSError("connection refused"),
        ):
            price, from_cache = get_ec2_price("us-east-1", "m5.large")

        assert price == pytest.approx(0.096)
        assert from_cache is True

    def test_raises_pricing_error_with_no_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud._fetch_ec2_price_from_api",
            side_effect=OSError("network down"),
        ):
            with pytest.raises(PricingError, match="no local cache"):
                get_ec2_price("us-east-1", "m5.large", no_cache=True)


# ---------------------------------------------------------------------------
# get_fargate_rates — with mocked HTTP
# ---------------------------------------------------------------------------


class TestGetFargateRates:
    def test_returns_rates_from_api(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud._fetch_fargate_rates_from_api",
            return_value=(0.04048, 0.004445),
        ):
            (vcpu_rate, mem_rate), from_cache = get_fargate_rates(
                "us-east-1", no_cache=True
            )

        assert vcpu_rate == pytest.approx(0.04048)
        assert mem_rate == pytest.approx(0.004445)
        assert from_cache is False

    def test_uses_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)
        _cache_set("fargate:vcpu:us-east-1", 0.04048)
        _cache_set("fargate:mem:us-east-1", 0.004445)

        with patch(
            "presidio_arch_translucency.cloud._fetch_fargate_rates_from_api"
        ) as mock_api:
            (vcpu_rate, mem_rate), from_cache = get_fargate_rates("us-east-1")

        mock_api.assert_not_called()
        assert from_cache is True

    def test_raises_when_no_cache_and_network_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud._fetch_fargate_rates_from_api",
            side_effect=OSError("network down"),
        ):
            with pytest.raises(PricingError, match="no local cache"):
                get_fargate_rates("us-east-1", no_cache=True)


# ---------------------------------------------------------------------------
# build_cost_params_from_aws
# ---------------------------------------------------------------------------


class TestBuildCostParams:
    def test_ec2_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud.get_ec2_price",
            return_value=(0.096, False),
        ):
            result = build_cost_params_from_aws(
                region="us-east-1", instance_type="m5.large"
            )

        assert isinstance(result, CloudPricingResult)
        assert result.params.cost_per_node_hour == pytest.approx(0.096)
        assert result.params.cost_per_deployment_hour == pytest.approx(0.096)
        assert result.params.cost_per_pod_hour == pytest.approx(0.096 / 8)
        assert result.params.cost_per_container_hour == pytest.approx(0.096 / 16)
        assert result.from_cache is False
        assert "m5.large" in result.source_description

    def test_fargate_mode(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.cloud.CACHE_FILE", tmp_path / "cache.json"
        )
        monkeypatch.setattr("presidio_arch_translucency.cloud.CACHE_DIR", tmp_path)

        with patch(
            "presidio_arch_translucency.cloud.get_fargate_rates",
            return_value=((0.04048, 0.004445), True),
        ):
            result = build_cost_params_from_aws(
                region="us-east-1", fargate=True, vcpu=0.5, memory_gb=1.0
            )

        task_cost = 0.04048 * 0.5 + 0.004445 * 1.0
        assert result.params.cost_per_pod_hour == pytest.approx(task_cost)
        assert result.params.cost_per_container_hour == pytest.approx(
            0.04048 * 0.125 + 0.004445 * 0.25
        )
        assert result.params.cost_per_deployment_hour == pytest.approx(task_cost * 4)
        assert result.params.cost_per_node_hour == pytest.approx(task_cost * 8)
        assert result.from_cache is True
        assert "Fargate" in result.source_description

    def test_fargate_requires_vcpu_and_memory(self):
        with pytest.raises(PricingError, match="--vcpu"):
            build_cost_params_from_aws(region="us-east-1", fargate=True)

    def test_no_mode_raises(self):
        with pytest.raises(PricingError, match="--instance-type"):
            build_cost_params_from_aws(region="us-east-1")


# ---------------------------------------------------------------------------
# CLI integration — pat cost --cloud aws
# ---------------------------------------------------------------------------


class TestCLICloudCost:
    def _mock_pricing(self, node_price: float = 0.096):
        from presidio_arch_translucency.cloud import CloudPricingResult
        from presidio_arch_translucency.cost import CostParams

        result = CloudPricingResult(
            params=CostParams(
                cost_per_container_hour=node_price / 16,
                cost_per_pod_hour=node_price / 8,
                cost_per_deployment_hour=node_price,
                cost_per_node_hour=node_price,
            ),
            from_cache=False,
            source_description=(
                f"AWS EC2 on-demand · us-east-1 · m5.large (${node_price:.4f}/hr)"
            ),
        )
        return result

    def test_cloud_aws_ec2_mode(self):
        with patch(
            "presidio_arch_translucency.cloud.build_cost_params_from_aws",
            return_value=self._mock_pricing(),
        ):
            result = runner.invoke(
                app,
                [
                    "--skip-audit",
                    "cost",
                    "--requests-per-second",
                    "500",
                    "--avg-latency-ms",
                    "80",
                    "--current-layer",
                    "container",
                    "--cloud",
                    "aws",
                    "--region",
                    "us-east-1",
                    "--instance-type",
                    "m5.large",
                ],
            )
        assert result.exit_code == 0
        assert "AWS EC2 on-demand" in result.output

    def test_cloud_aws_fargate_mode(self):
        with patch(
            "presidio_arch_translucency.cloud.build_cost_params_from_aws",
            return_value=self._mock_pricing(0.025),
        ):
            result = runner.invoke(
                app,
                [
                    "--skip-audit",
                    "cost",
                    "--requests-per-second",
                    "100",
                    "--avg-latency-ms",
                    "50",
                    "--current-layer",
                    "pod",
                    "--cloud",
                    "aws",
                    "--region",
                    "us-east-1",
                    "--fargate",
                    "--vcpu",
                    "0.5",
                    "--memory-gb",
                    "1",
                ],
            )
        assert result.exit_code == 0

    def test_cloud_aws_missing_region(self):
        result = runner.invoke(
            app,
            [
                "--skip-audit",
                "cost",
                "--requests-per-second",
                "100",
                "--avg-latency-ms",
                "50",
                "--current-layer",
                "container",
                "--cloud",
                "aws",
                "--instance-type",
                "m5.large",
            ],
        )
        assert result.exit_code == 2

    def test_cloud_aws_missing_instance_or_fargate(self):
        result = runner.invoke(
            app,
            [
                "--skip-audit",
                "cost",
                "--requests-per-second",
                "100",
                "--avg-latency-ms",
                "50",
                "--current-layer",
                "container",
                "--cloud",
                "aws",
                "--region",
                "us-east-1",
            ],
        )
        assert result.exit_code == 2

    def test_unsupported_cloud_provider(self):
        result = runner.invoke(
            app,
            [
                "--skip-audit",
                "cost",
                "--requests-per-second",
                "100",
                "--avg-latency-ms",
                "50",
                "--current-layer",
                "container",
                "--cloud",
                "gcp",
                "--region",
                "us-central1",
                "--instance-type",
                "n2-standard-2",
            ],
        )
        assert result.exit_code == 2
        assert "gcp" in result.output.lower() or "gcp" in (result.stderr or "").lower()

    def test_pricing_error_shown_to_user(self):
        with patch(
            "presidio_arch_translucency.cloud.build_cost_params_from_aws",
            side_effect=Exception("network down"),
        ):
            # This path goes through PricingError — simulate via import patch
            from presidio_arch_translucency.cloud import PricingError as PE

            with patch(
                "presidio_arch_translucency.cloud.build_cost_params_from_aws",
                side_effect=PE("test error"),
            ):
                result = runner.invoke(
                    app,
                    [
                        "--skip-audit",
                        "cost",
                        "--requests-per-second",
                        "100",
                        "--avg-latency-ms",
                        "50",
                        "--current-layer",
                        "container",
                        "--cloud",
                        "aws",
                        "--region",
                        "us-east-1",
                        "--instance-type",
                        "m5.large",
                    ],
                )
        assert result.exit_code == 2

    def test_no_cloud_flag_uses_manual_costs(self):
        result = runner.invoke(
            app,
            [
                "--skip-audit",
                "cost",
                "--requests-per-second",
                "500",
                "--avg-latency-ms",
                "80",
                "--current-layer",
                "container",
                "--cost-per-container-hour",
                "0.03",
            ],
        )
        assert result.exit_code == 0
        assert "Pricing source" not in result.output
