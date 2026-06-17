"""Tests for OTLP/HTTP+JSON export (`pat export --otlp`, v0.13.0, ADR-0006).

The network is always mocked — no collector is contacted.
"""

import json
import urllib.error

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.export import Metric, Sample
from presidio_arch_translucency.otlp import (
    OtlpError,
    build_otlp_payload,
    metrics_url,
    post_otlp,
    resolve_token,
)

runner = CliRunner()

_POST = "presidio_arch_translucency.otlp.post_otlp"
_FAKE_TOKEN = "tok"  # noqa: S105 -- test fixture, not a real secret


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


# ── metrics_url ───────────────────────────────────────────────────────────────


def test_metrics_url_appends_path() -> None:
    assert metrics_url("http://c:4318") == "http://c:4318/v1/metrics"
    assert metrics_url("http://c:4318/") == "http://c:4318/v1/metrics"


def test_metrics_url_idempotent() -> None:
    assert metrics_url("https://c/v1/metrics") == "https://c/v1/metrics"


@pytest.mark.parametrize("bad", ["ftp://c", "not-a-url", "http://"])
def test_metrics_url_rejects_bad(bad: str) -> None:
    with pytest.raises(OtlpError):
        metrics_url(bad)


def test_metrics_url_rejects_control_chars() -> None:
    with pytest.raises(OtlpError):
        metrics_url("http://c\n/v1/metrics")


# ── build_otlp_payload ────────────────────────────────────────────────────────


def test_build_payload_structure() -> None:
    m = Metric("pat_x", "help x", [Sample({"layer": "container"}, 6.0)])
    payload = build_otlp_payload([m], service_name="pat", timestamp_ns=123)
    rm = payload["resourceMetrics"][0]
    attrs = {a["key"]: a["value"]["stringValue"] for a in rm["resource"]["attributes"]}
    assert attrs["service.name"] == "pat"
    assert "service.version" in attrs
    sm = rm["scopeMetrics"][0]
    assert sm["scope"]["name"] == "presidio_arch_translucency"
    met = sm["metrics"][0]
    assert met["name"] == "pat_x"
    dp = met["gauge"]["dataPoints"][0]
    assert dp["asDouble"] == 6.0
    assert dp["timeUnixNano"] == "123"
    assert dp["attributes"] == [{"key": "layer", "value": {"stringValue": "container"}}]


def test_build_payload_drops_non_finite_points() -> None:
    m = Metric("pat_c", "c", [Sample({}, float("inf")), Sample({}, 2.0)])
    payload = build_otlp_payload([m], timestamp_ns=1)
    points = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"][
        "dataPoints"
    ]
    assert [p["asDouble"] for p in points] == [2.0]


def test_build_payload_omits_metric_with_no_finite_points() -> None:
    m = Metric("pat_inf", "i", [Sample({}, float("nan"))])
    payload = build_otlp_payload([m], timestamp_ns=1)
    assert payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"] == []


def test_build_payload_rejects_control_char_service_name() -> None:
    with pytest.raises(OtlpError):
        build_otlp_payload([], service_name="bad\x00")


def test_build_payload_is_json_serialisable() -> None:
    m = Metric("pat_x", "x", [Sample({"layer": "pod"}, 3.0)])
    json.dumps(build_otlp_payload([m]))  # must not raise


# ── resolve_token ─────────────────────────────────────────────────────────────


def test_resolve_token_none_without_env(monkeypatch) -> None:
    monkeypatch.delenv("PAT_OTLP_TOKEN", raising=False)
    assert resolve_token("http://c") is None


def test_resolve_token_requires_https(monkeypatch) -> None:
    monkeypatch.setenv("PAT_OTLP_TOKEN", "tok")
    with pytest.raises(OtlpError, match="https"):
        resolve_token("http://c")


def test_resolve_token_https_ok(monkeypatch) -> None:
    monkeypatch.setenv("PAT_OTLP_TOKEN", "tok")
    assert resolve_token("https://c") == "tok"


def test_resolve_token_rejects_control_chars(monkeypatch) -> None:
    monkeypatch.setenv("PAT_OTLP_TOKEN", "tok\rInjected: x")
    with pytest.raises(OtlpError, match="control characters"):
        resolve_token("https://c")


def test_resolve_token_insecure_allows_http(monkeypatch) -> None:
    monkeypatch.setenv("PAT_OTLP_TOKEN", "tok")
    assert resolve_token("http://c", insecure_http=True) == "tok"


# ── post_otlp (urlopen mocked) ────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_post_otlp_builds_request(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["req"] = req
        return _FakeResp(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status = post_otlp("https://c:4318", {"resourceMetrics": []}, token=_FAKE_TOKEN)
    assert status == 200
    req = captured["req"]
    assert req.full_url == "https://c:4318/v1/metrics"
    assert req.get_method() == "POST"
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Authorization") == "Bearer tok"
    assert json.loads(req.data) == {"resourceMetrics": []}


def test_post_otlp_no_token_omits_auth(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["req"] = req
        return _FakeResp(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    post_otlp("http://c:4318", {"resourceMetrics": []})
    assert captured["req"].get_header("Authorization") is None


def test_post_otlp_http_error_raises(monkeypatch) -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError("u", 422, "Unprocessable", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(OtlpError, match="HTTP 422"):
        post_otlp("https://c", {})


def test_post_otlp_network_error_raises(monkeypatch) -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise OSError("refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(OtlpError, match="failed to push"):
        post_otlp("https://c", {})


# ── pat export --otlp CLI ─────────────────────────────────────────────────────


def test_export_otlp_pushes(monkeypatch) -> None:
    captured: dict = {}

    def fake_post(endpoint, payload, **kwargs):  # noqa: ANN001
        captured["payload"] = payload
        return 200

    monkeypatch.setattr(_POST, fake_post)
    monkeypatch.delenv("PAT_OTLP_TOKEN", raising=False)
    result = invoke(
        "export",
        "--otlp",
        "http://collector:4318",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 0, result.output
    assert "Pushed OTLP metrics to http://collector:4318/v1/metrics" in result.output
    names = {
        met["name"]
        for met in captured["payload"]["resourceMetrics"][0]["scopeMetrics"][0][
            "metrics"
        ]
    }
    assert "pat_recommended_replicas" in names


def test_export_otlp_bad_url_exits_2(monkeypatch) -> None:
    monkeypatch.delenv("PAT_OTLP_TOKEN", raising=False)
    result = invoke(
        "export", "--otlp", "ftp://c", "-r", "500", "-l", "80", "-c", "container"
    )
    assert result.exit_code == 2


def test_export_otlp_token_over_http_exits_1(monkeypatch) -> None:
    monkeypatch.setenv("PAT_OTLP_TOKEN", "tok")
    result = invoke(
        "export",
        "--otlp",
        "http://c:4318",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 1
    assert "https" in result.output.lower()
