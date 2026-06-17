"""Tests for the Prometheus Pushgateway target (`pat export --pushgateway`, v0.14.0).

The network is always mocked — no gateway is contacted.
"""

import urllib.error

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.pushgateway import (
    PushgatewayError,
    parse_grouping,
    push,
    pushgateway_url,
    resolve_token,
)

runner = CliRunner()

_PUSH = "presidio_arch_translucency.pushgateway.push"
_FAKE_TOKEN = "tok"  # noqa: S105 -- test fixture, not a real secret


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


# ── parse_grouping ────────────────────────────────────────────────────────────


def test_parse_grouping_ok() -> None:
    assert parse_grouping(["instance=ci-7", "env=prod"]) == {
        "instance": "ci-7",
        "env": "prod",
    }


def test_parse_grouping_rejects_missing_equals() -> None:
    with pytest.raises(PushgatewayError, match="key=value"):
        parse_grouping(["bogus"])


def test_parse_grouping_rejects_empty_key() -> None:
    with pytest.raises(PushgatewayError, match="empty key"):
        parse_grouping(["=v"])


# ── pushgateway_url ───────────────────────────────────────────────────────────


def test_pushgateway_url_basic() -> None:
    assert pushgateway_url("http://pg:9091", "pat") == "http://pg:9091/metrics/job/pat"


def test_pushgateway_url_with_grouping() -> None:
    url = pushgateway_url("http://pg:9091/", "pat", {"instance": "ci-7"})
    assert url == "http://pg:9091/metrics/job/pat/instance/ci-7"


def test_pushgateway_url_percent_encodes() -> None:
    url = pushgateway_url("http://pg", "a b")
    assert url == "http://pg/metrics/job/a%20b"


def test_pushgateway_url_rejects_slash_in_job() -> None:
    with pytest.raises(PushgatewayError, match="'/'"):
        pushgateway_url("http://pg", "a/b")


def test_pushgateway_url_rejects_control_chars() -> None:
    with pytest.raises(PushgatewayError):
        pushgateway_url("http://pg", "a\nb")


def test_pushgateway_url_rejects_bad_base() -> None:
    with pytest.raises(PushgatewayError):
        pushgateway_url("ftp://pg", "pat")


def test_pushgateway_url_rejects_empty_grouping_value() -> None:
    with pytest.raises(PushgatewayError, match="non-empty"):
        pushgateway_url("http://pg", "pat", {"instance": ""})


# ── resolve_token ─────────────────────────────────────────────────────────────


def test_resolve_token_none_without_env(monkeypatch) -> None:
    monkeypatch.delenv("PAT_PUSHGATEWAY_TOKEN", raising=False)
    assert resolve_token("http://pg") is None


def test_resolve_token_requires_https(monkeypatch) -> None:
    monkeypatch.setenv("PAT_PUSHGATEWAY_TOKEN", "tok")
    with pytest.raises(PushgatewayError, match="https"):
        resolve_token("http://pg")


def test_resolve_token_https_ok(monkeypatch) -> None:
    monkeypatch.setenv("PAT_PUSHGATEWAY_TOKEN", "tok")
    assert resolve_token("https://pg") == "tok"


def test_resolve_token_insecure_allows_http(monkeypatch) -> None:
    monkeypatch.setenv("PAT_PUSHGATEWAY_TOKEN", "tok")
    assert resolve_token("http://pg", insecure_http=True) == "tok"


def test_resolve_token_rejects_control_char_token(monkeypatch) -> None:
    monkeypatch.setenv("PAT_PUSHGATEWAY_TOKEN", "to\x01k")
    with pytest.raises(PushgatewayError, match="control characters"):
        resolve_token("https://pg")


# ── push (urlopen mocked) ─────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_push_builds_put_request(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["req"] = req
        return _FakeResp(200)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    status = push("https://pg:9091", "pat", "pat_x 1\n", token=_FAKE_TOKEN)
    assert status == 200
    req = captured["req"]
    assert req.full_url == "https://pg:9091/metrics/job/pat"
    assert req.get_method() == "PUT"
    assert req.get_header("Content-type").startswith("text/plain; version=0.0.4")
    assert req.get_header("Authorization") == "Bearer tok"
    assert req.data == b"pat_x 1\n"


def test_push_http_error_raises(monkeypatch) -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError("u", 400, "Bad", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(PushgatewayError, match="HTTP 400"):
        push("https://pg", "pat", "x")


def test_push_network_error_raises(monkeypatch) -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise OSError("refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(PushgatewayError, match="failed to push"):
        push("https://pg", "pat", "x")


# ── pat export --pushgateway CLI ──────────────────────────────────────────────


def test_export_pushgateway_pushes(monkeypatch) -> None:
    captured: dict = {}

    def fake_push(base, job, exposition, **kwargs):  # noqa: ANN001
        captured["exposition"] = exposition
        captured["job"] = job
        return 200

    monkeypatch.setattr(_PUSH, fake_push)
    monkeypatch.delenv("PAT_PUSHGATEWAY_TOKEN", raising=False)
    result = invoke(
        "export",
        "--pushgateway",
        "http://pg:9091",
        "--job",
        "pat",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 0, result.output
    assert "Pushed metrics to http://pg:9091/metrics/job/pat" in result.output
    assert "pat_recommended_replicas" in captured["exposition"]


def test_export_pushgateway_with_grouping(monkeypatch) -> None:
    monkeypatch.setattr(_PUSH, lambda *a, **k: 200)
    monkeypatch.delenv("PAT_PUSHGATEWAY_TOKEN", raising=False)
    result = invoke(
        "export",
        "--pushgateway",
        "http://pg:9091",
        "--grouping",
        "instance=ci-7",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 0, result.output
    assert "/metrics/job/pat/instance/ci-7" in result.output


def test_export_pushgateway_bad_grouping_exits_2(monkeypatch) -> None:
    monkeypatch.delenv("PAT_PUSHGATEWAY_TOKEN", raising=False)
    result = invoke(
        "export",
        "--pushgateway",
        "http://pg:9091",
        "--grouping",
        "bogus",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 2


def test_export_pushgateway_and_otlp_mutually_exclusive(monkeypatch) -> None:
    monkeypatch.delenv("PAT_PUSHGATEWAY_TOKEN", raising=False)
    result = invoke(
        "export",
        "--pushgateway",
        "http://pg:9091",
        "--otlp",
        "http://c:4318",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_export_pushgateway_token_over_http_exits_1(monkeypatch) -> None:
    monkeypatch.setenv("PAT_PUSHGATEWAY_TOKEN", "tok")
    result = invoke(
        "export",
        "--pushgateway",
        "http://pg:9091",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 1
    assert "https" in result.output.lower()
