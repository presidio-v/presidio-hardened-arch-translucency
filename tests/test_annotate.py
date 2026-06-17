"""Tests for the Grafana annotation writer (`pat annotate`, v0.12.0).

The network is always mocked — no Grafana is contacted.
"""

import json
import urllib.error

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.annotate import (
    AnnotateError,
    Annotation,
    build_annotation,
    post_annotation,
    resolve_token,
    token_from_env,
)
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.model import ReplicationLayer, analyze

runner = CliRunner()

_POST = "presidio_arch_translucency.annotate.post_annotation"
_FAKE_TOKEN = "tok"  # noqa: S105 -- test fixture, not a real secret


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def _result():
    return analyze(500.0, 80.0, ReplicationLayer.CONTAINER)


# ── build_annotation ──────────────────────────────────────────────────────────


def test_build_annotation_text_and_tags() -> None:
    ann = build_annotation(_result())
    assert ann.text.startswith("pat recommends ")
    assert "pat" in ann.tags
    assert "pat-recommendation" in ann.tags
    assert any(t.startswith("layer:") for t in ann.tags)


def test_build_annotation_extra_tags_and_uid() -> None:
    ann = build_annotation(_result(), extra_tags=("env:prod",), dashboard_uid="abc")
    assert "env:prod" in ann.tags
    assert ann.dashboard_uid == "abc"


def test_build_annotation_rejects_control_char_tag() -> None:
    with pytest.raises(AnnotateError):
        build_annotation(_result(), extra_tags=("bad\x00tag",))


def test_build_annotation_rejects_empty_tag() -> None:
    with pytest.raises(AnnotateError, match="non-empty"):
        build_annotation(_result(), extra_tags=("   ",))


def test_build_annotation_rejects_overlong_tag() -> None:
    with pytest.raises(AnnotateError, match="exceeds"):
        build_annotation(_result(), extra_tags=("x" * 101,))


# ── Annotation.payload ────────────────────────────────────────────────────────


def test_payload_minimal_omits_optional_fields() -> None:
    body = Annotation(text="t", tags=["pat"]).payload()
    assert body == {"text": "t", "tags": ["pat"]}
    assert "dashboardUID" not in body
    assert "time" not in body


def test_payload_includes_uid_and_time() -> None:
    body = Annotation(text="t", tags=[], dashboard_uid="u", time_ms=123).payload()
    assert body["dashboardUID"] == "u"
    assert body["time"] == 123


# ── token resolution ──────────────────────────────────────────────────────────


def test_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("PAT_GRAFANA_TOKEN", "  secret  ")
    assert token_from_env() == "secret"


def test_token_from_env_blank_is_none(monkeypatch) -> None:
    monkeypatch.setenv("PAT_GRAFANA_TOKEN", "   ")
    assert token_from_env() is None


def test_token_from_env_rejects_control_chars(monkeypatch) -> None:
    monkeypatch.setenv("PAT_GRAFANA_TOKEN", "tok\nInjected: x")
    with pytest.raises(AnnotateError, match="control characters"):
        token_from_env()


def test_resolve_token_missing_raises(monkeypatch) -> None:
    monkeypatch.delenv("PAT_GRAFANA_TOKEN", raising=False)
    with pytest.raises(AnnotateError, match="PAT_GRAFANA_TOKEN"):
        resolve_token()


# ── post_annotation (urlopen mocked) ──────────────────────────────────────────


class _FakeResp:
    def __init__(self, body: bytes = b'{"id": 7}') -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_post_annotation_builds_authorized_post(monkeypatch) -> None:
    captured: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["req"] = req
        return _FakeResp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ann = Annotation(text="hi", tags=["pat"])
    out = post_annotation("https://g.example", ann, token=_FAKE_TOKEN)
    assert out == {"id": 7}
    req = captured["req"]
    assert req.full_url == "https://g.example/api/annotations"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer tok"
    assert json.loads(req.data) == {"text": "hi", "tags": ["pat"]}


def test_post_annotation_refuses_cleartext_http() -> None:
    with pytest.raises(AnnotateError, match="cleartext"):
        post_annotation("http://g.example", Annotation(text="x"), token=_FAKE_TOKEN)


def test_post_annotation_allows_http_with_insecure(monkeypatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResp())
    out = post_annotation(
        "http://localhost:3000",
        Annotation(text="x"),
        token=_FAKE_TOKEN,
        insecure_http=True,
    )
    assert out == {"id": 7}


def test_post_annotation_http_error_raises(monkeypatch) -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(AnnotateError, match="HTTP 403"):
        post_annotation("https://g", Annotation(text="x"), token=_FAKE_TOKEN)


def test_post_annotation_network_error_raises(monkeypatch) -> None:
    def boom(req, timeout=None):  # noqa: ANN001
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(AnnotateError, match="failed to post"):
        post_annotation("https://g", Annotation(text="x"), token=_FAKE_TOKEN)


def test_post_annotation_non_json_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda req, timeout=None: _FakeResp(b"not json")
    )
    assert post_annotation("https://g", Annotation(text="x"), token=_FAKE_TOKEN) == {}


def test_post_annotation_rejects_bad_url() -> None:
    with pytest.raises(AnnotateError):
        post_annotation("ftp://g", Annotation(text="x"), token=_FAKE_TOKEN)


# ── pat annotate CLI ──────────────────────────────────────────────────────────


def test_annotate_dry_run_prints_payload(monkeypatch) -> None:
    monkeypatch.delenv("PAT_GRAFANA_TOKEN", raising=False)
    result = invoke(
        "annotate",
        "--dry-run",
        "--grafana",
        "https://g",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["text"].startswith("pat recommends")
    assert "pat" in payload["tags"]


def test_annotate_missing_token_exits_1(monkeypatch) -> None:
    monkeypatch.delenv("PAT_GRAFANA_TOKEN", raising=False)
    result = invoke(
        "annotate", "--grafana", "https://g", "-r", "500", "-l", "80", "-c", "container"
    )
    assert result.exit_code == 1
    assert "PAT_GRAFANA_TOKEN" in result.output


def test_annotate_posts_with_token(monkeypatch) -> None:
    monkeypatch.setenv("PAT_GRAFANA_TOKEN", "tok")
    monkeypatch.setattr(_POST, lambda *a, **k: {"id": 42})
    result = invoke(
        "annotate",
        "--grafana",
        "https://grafana.example",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
    )
    assert result.exit_code == 0, result.output
    assert "Annotation posted" in result.output
    assert "42" in result.output


def test_annotate_cleartext_http_with_token_exits_1(monkeypatch) -> None:
    monkeypatch.setenv("PAT_GRAFANA_TOKEN", "tok")
    result = invoke(
        "annotate", "--grafana", "http://g", "-r", "500", "-l", "80", "-c", "container"
    )
    assert result.exit_code == 1
    assert "http" in result.output.lower()


def test_annotate_invalid_layer_exits_2(monkeypatch) -> None:
    monkeypatch.setenv("PAT_GRAFANA_TOKEN", "tok")
    result = invoke(
        "annotate", "--grafana", "https://g", "-r", "500", "-l", "80", "-c", "bogus"
    )
    assert result.exit_code == 2
