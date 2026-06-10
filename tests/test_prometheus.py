"""Tests for the Prometheus observation source (v0.8.0 Phase 3).

All HTTP is mocked — no live Prometheus is contacted.
"""

from __future__ import annotations

import io
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import count_observations, load_observations
from presidio_arch_translucency.prometheus import (
    DEFAULT_AVG_QUERY,
    DEFAULT_P99_QUERY,
    DEFAULT_REPLICAS_QUERY,
    DEFAULT_RPS_QUERY,
    PrometheusError,
    _build_query_url,
    _kubeconfig_path,
    _parse_yaml_subset,
    _resolve_token,
    _token_from_kubeconfig,
    fetch_observation,
    instant_query,
)

runner = CliRunner()

_URL = "http://prometheus.monitoring.svc:9090"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _urlopen_cm(payload: dict) -> MagicMock:
    cm = MagicMock()
    cm.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    cm.__exit__.return_value = False
    return cm


def _vector(value) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": {}, "value": [1717243200, str(value)]}],
        },
    }


def _empty_vector() -> dict:
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


def _scalar(value) -> dict:
    return {
        "status": "success",
        "data": {"resultType": "scalar", "result": [1717243200, str(value)]},
    }


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


class TestBuildQueryURL:
    def test_builds_instant_query_url(self):
        url = _build_query_url("http://prom:9090", "up")
        assert url == "http://prom:9090/api/v1/query?query=up"

    def test_strips_trailing_slash_and_encodes(self):
        url = _build_query_url("http://prom:9090/", "sum(rate(x[1m]))")
        assert url.startswith("http://prom:9090/api/v1/query?query=")
        assert "%28" in url  # '(' encoded — no raw parens leak into the query string

    def test_https_allowed(self):
        assert _build_query_url("https://prom", "up").startswith("https://prom/")

    @pytest.mark.parametrize(
        "bad", ["file:///etc/passwd", "ftp://prom", "prom:9090", ""]
    )
    def test_rejects_non_http_scheme(self, bad):
        with pytest.raises(PrometheusError, match="http"):
            _build_query_url(bad, "up")


# ---------------------------------------------------------------------------
# instant_query — response parsing
# ---------------------------------------------------------------------------


class TestInstantQuery:
    def test_vector_result_returns_value(self):
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(_vector(42.5))):
            assert instant_query(_URL, "up") == pytest.approx(42.5)

    def test_scalar_result_returns_value(self):
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(_scalar(7))):
            assert instant_query(_URL, "scalar(up)") == pytest.approx(7.0)

    def test_empty_vector_returns_none(self):
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(_empty_vector())):
            assert instant_query(_URL, "up") is None

    def test_nan_returns_none(self):
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(_vector("NaN"))):
            assert instant_query(_URL, "x") is None

    def test_error_status_raises(self):
        payload = {"status": "error", "error": "parse error: bad query"}
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            with pytest.raises(PrometheusError, match="parse error"):
                instant_query(_URL, "??")

    def test_non_numeric_value_raises(self):
        with patch(
            "urllib.request.urlopen", return_value=_urlopen_cm(_vector("not-a-num"))
        ):
            with pytest.raises(PrometheusError, match="non-numeric"):
                instant_query(_URL, "x")

    def test_malformed_value_pair_raises(self):
        # A vector element whose value tuple is empty ([] ⇒ no [1] index).
        payload = {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [{"metric": {}, "value": []}],
            },
        }
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            with pytest.raises(PrometheusError, match="malformed"):
                instant_query(_URL, "x")

    def test_unsupported_result_type_raises(self):
        payload = {"status": "success", "data": {"resultType": "matrix", "result": []}}
        with patch("urllib.request.urlopen", return_value=_urlopen_cm(payload)):
            with pytest.raises(PrometheusError, match="resultType"):
                instant_query(_URL, "x")

    def test_transport_error_raises(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            with pytest.raises(PrometheusError, match="failed to query"):
                instant_query(_URL, "up")


# ---------------------------------------------------------------------------
# auth — token from env only
# ---------------------------------------------------------------------------


class TestAuth:
    def test_token_sets_bearer_header(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return _urlopen_cm(_vector(1))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            instant_query(_URL, "up", token="s3cr3t")  # noqa: S106 — test literal
        assert captured["auth"] == "Bearer s3cr3t"

    def test_no_token_no_auth_header(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            return _urlopen_cm(_vector(1))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            instant_query(_URL, "up", token=None)
        assert captured["auth"] is None

    def test_fetch_reads_token_from_env(self, monkeypatch):
        monkeypatch.setenv("PAT_PROMETHEUS_TOKEN", "envtoken")
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            return _urlopen_cm(_vector(3))  # every query returns 3

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_observation(_URL, "pod")
        assert seen and all(h == "Bearer envtoken" for h in seen)


# ---------------------------------------------------------------------------
# fetch_observation
# ---------------------------------------------------------------------------


def _query_router(values: dict):
    """Return an instant_query stand-in mapping each default query to a value."""

    def _fn(base_url, query, token=None, timeout=30.0):
        return values[query]

    return _fn


class TestFetchObservation:
    def test_builds_prometheus_observation(self):
        values = {
            DEFAULT_RPS_QUERY: 500.0,
            DEFAULT_P99_QUERY: 0.14,  # seconds
            DEFAULT_AVG_QUERY: 0.08,  # seconds
            DEFAULT_REPLICAS_QUERY: 6.0,
        }
        with patch(
            "presidio_arch_translucency.prometheus.instant_query",
            side_effect=_query_router(values),
        ):
            obs = fetch_observation(_URL, "container")
        assert obs.source == "prometheus"
        assert obs.layer == "container"
        assert obs.rps == pytest.approx(500.0)
        assert obs.throughput == pytest.approx(500.0)
        assert obs.p99_latency_ms == pytest.approx(140.0)  # 0.14 s → ms
        assert obs.avg_latency_ms == pytest.approx(80.0)  # 0.08 s → ms
        assert obs.replicas == 6
        assert obs.timestamp.tzinfo is not None

    def test_replicas_rounded_to_int(self):
        values = {
            DEFAULT_RPS_QUERY: 100.0,
            DEFAULT_P99_QUERY: 0.1,
            DEFAULT_AVG_QUERY: 0.05,
            DEFAULT_REPLICAS_QUERY: 3.7,
        }
        with patch(
            "presidio_arch_translucency.prometheus.instant_query",
            side_effect=_query_router(values),
        ):
            obs = fetch_observation(_URL, "pod")
        assert obs.replicas == 4

    def test_no_traffic_records_zero_rps(self):
        values = {
            DEFAULT_RPS_QUERY: None,  # no series ⇒ no traffic
            DEFAULT_P99_QUERY: None,
            DEFAULT_AVG_QUERY: None,
            DEFAULT_REPLICAS_QUERY: 2.0,
        }
        with patch(
            "presidio_arch_translucency.prometheus.instant_query",
            side_effect=_query_router(values),
        ):
            obs = fetch_observation(_URL, "pod")
        assert obs.rps == 0.0
        assert obs.avg_latency_ms == 0.0
        assert obs.p99_latency_ms == 0.0
        assert obs.replicas == 2

    def test_missing_replica_count_raises(self):
        values = {
            DEFAULT_RPS_QUERY: 500.0,
            DEFAULT_P99_QUERY: 0.14,
            DEFAULT_AVG_QUERY: 0.08,
            DEFAULT_REPLICAS_QUERY: None,
        }
        with patch(
            "presidio_arch_translucency.prometheus.instant_query",
            side_effect=_query_router(values),
        ):
            with pytest.raises(PrometheusError, match="replica"):
                fetch_observation(_URL, "container")

    def test_zero_replica_count_raises(self):
        values = {
            DEFAULT_RPS_QUERY: 500.0,
            DEFAULT_P99_QUERY: 0.14,
            DEFAULT_AVG_QUERY: 0.08,
            DEFAULT_REPLICAS_QUERY: 0.0,
        }
        with patch(
            "presidio_arch_translucency.prometheus.instant_query",
            side_effect=_query_router(values),
        ):
            with pytest.raises(PrometheusError, match="replica"):
                fetch_observation(_URL, "container")

    def test_end_to_end_against_mocked_http(self):
        # Drive the whole path (fetch → parse) through mocked urlopen, routing
        # each query's HTTP response by the query string in the request URL.
        import urllib.parse

        responses = {
            DEFAULT_RPS_QUERY: _vector(250.0),
            DEFAULT_P99_QUERY: _vector(0.2),
            DEFAULT_AVG_QUERY: _vector(0.09),
            DEFAULT_REPLICAS_QUERY: _vector(5.0),
        }

        def fake_urlopen(req, timeout=None):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)[
                "query"
            ][0]
            return _urlopen_cm(responses[q])

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            obs = fetch_observation(_URL, "deployment")
        assert obs.rps == pytest.approx(250.0)
        assert obs.p99_latency_ms == pytest.approx(200.0)
        assert obs.replicas == 5


# ---------------------------------------------------------------------------
# CLI: pat observe --prometheus
# ---------------------------------------------------------------------------


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "observe", *args])


class TestObservePrometheusCLI:
    def test_scrape_records_observation(self, tmp_path):
        from presidio_arch_translucency.observe import Observation, utcnow

        db = tmp_path / "obs.db"
        fake = Observation(utcnow(), 500, 80, 140, 500, "container", 6, "prometheus")
        with patch(
            "presidio_arch_translucency.prometheus.fetch_observation",
            return_value=fake,
        ):
            result = _invoke(
                "--prometheus", _URL, "--layer", "container", "--db", str(db)
            )
        assert result.exit_code == 0
        assert "Scraped" in result.output
        assert count_observations(db_path=db, source="prometheus") == 1
        (stored,) = load_observations(db_path=db)
        assert stored.source == "prometheus"

    def test_requires_layer(self, tmp_path):
        result = _invoke("--prometheus", _URL, "--db", str(tmp_path / "obs.db"))
        assert result.exit_code == 2
        assert "requires --layer" in result.output

    def test_prometheus_error_exits_2(self, tmp_path):
        with patch(
            "presidio_arch_translucency.prometheus.fetch_observation",
            side_effect=PrometheusError("connection refused"),
        ):
            result = _invoke(
                "--prometheus", _URL, "--layer", "pod", "--db", str(tmp_path / "obs.db")
            )
        assert result.exit_code == 2
        assert "Prometheus collection failed" in result.output

    def test_invalid_layer_exits_2(self, tmp_path):
        result = _invoke(
            "--prometheus", _URL, "--layer", "bogus", "--db", str(tmp_path / "obs.db")
        )
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# kubeconfig auth (D3 follow-on)
# ---------------------------------------------------------------------------


_KUBECONFIG_WITH_AUTH = """\
apiVersion: v1
kind: Config
current-context: prod
clusters:
- cluster:
    server: https://10.0.0.1:6443
  name: prod-cluster
contexts:
- context:
    cluster: prod-cluster
    user: prod-user
  name: prod
- name: staging
  context:
    cluster: staging-cluster
    user: staging-user
users:
- name: prod-user
  user:
    token: kube-prod-token-XYZ
- name: staging-user
  user:
    token: "kube-staging-token-QRS"
"""

# A current user that authenticates with a client cert, not a token.
_KUBECONFIG_CERT_ONLY = """\
apiVersion: v1
kind: Config
current-context: prod
contexts:
- context:
    cluster: prod-cluster
    user: prod-user
  name: prod
users:
- name: prod-user
  user:
    client-certificate-data: QkxBSAo=
    client-key-data: QkxBSAo=
"""


def _write_kubeconfig(tmp_path, text: str):
    path = tmp_path / "kubeconfig"
    path.write_text(text, encoding="utf-8")
    return path


class TestParseYamlSubset:
    def test_parses_token_for_current_context(self):
        cfg = _parse_yaml_subset(_KUBECONFIG_WITH_AUTH)
        assert cfg["current-context"] == "prod"
        assert [c["name"] for c in cfg["contexts"]] == ["prod", "staging"]
        # key order within the staging item is swapped (name before context)
        assert cfg["contexts"][1]["context"]["user"] == "staging-user"
        assert cfg["users"][0]["user"]["token"] == "kube-prod-token-XYZ"  # noqa: S105

    def test_unquotes_quoted_scalar(self):
        cfg = _parse_yaml_subset(_KUBECONFIG_WITH_AUTH)
        assert cfg["users"][1]["user"]["token"] == "kube-staging-token-QRS"  # noqa: S105

    def test_ignores_comments_and_blank_lines(self):
        text = "# a comment\n\ncurrent-context: x\n\n# trailing\n"
        assert _parse_yaml_subset(text) == {"current-context": "x"}

    def test_empty_text_returns_empty_mapping(self):
        assert _parse_yaml_subset("   \n\n") == {}

    def test_key_with_no_value_is_none(self):
        # A bare "key:" with no nested block resolves to None, not a crash.
        assert _parse_yaml_subset("a:\nb: 1") == {"a": None, "b": "1"}

    def test_top_level_sequence_parses_to_list(self):
        assert _parse_yaml_subset("- x: 1\n- x: 2") == [{"x": "1"}, {"x": "2"}]


class TestKubeconfigPath:
    def test_kubeconfig_env_first_entry_wins(self, tmp_path, monkeypatch):
        first = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        missing = tmp_path / "second"
        monkeypatch.setenv("KUBECONFIG", f"{first}{os.pathsep}{missing}")
        assert _kubeconfig_path() == first

    def test_kubeconfig_env_missing_first_file_returns_none(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("KUBECONFIG", str(tmp_path / "nope"))
        assert _kubeconfig_path() is None

    def test_kubeconfig_env_empty_first_entry_returns_none(self, monkeypatch):
        # e.g. KUBECONFIG=":/some/other" — the (empty) first entry wins → None.
        monkeypatch.setenv("KUBECONFIG", f"{os.pathsep}/some/other")
        assert _kubeconfig_path() is None

    def test_default_path_used_when_env_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KUBECONFIG", raising=False)
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setattr(
            "presidio_arch_translucency.prometheus._DEFAULT_KUBECONFIG", cfg
        )
        assert _kubeconfig_path() == cfg

    def test_default_missing_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KUBECONFIG", raising=False)
        monkeypatch.setattr(
            "presidio_arch_translucency.prometheus._DEFAULT_KUBECONFIG",
            tmp_path / "absent",
        )
        assert _kubeconfig_path() is None


class TestTokenFromKubeconfig:
    def test_reads_active_context_token(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig() == "kube-prod-token-XYZ"

    def test_context_override_selects_other_context(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig("staging") == "kube-staging-token-QRS"

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KUBECONFIG", str(tmp_path / "absent"))
        assert _token_from_kubeconfig() is None

    def test_cert_only_user_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_CERT_ONLY)
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig() is None

    def test_unknown_override_context_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig("does-not-exist") is None

    def test_malformed_kubeconfig_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, "current-context: prod\ncontexts: notalist\n")
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig() is None

    def test_no_kubeconfig_path_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            "presidio_arch_translucency.prometheus._kubeconfig_path", lambda: None
        )
        assert _token_from_kubeconfig() is None

    def test_config_not_a_mapping_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, "- a: 1\n- b: 2\n")  # top-level sequence
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig() is None

    def test_no_current_context_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, "apiVersion: v1\nkind: Config\n")
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig() is None

    def test_context_without_inner_mapping_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(
            tmp_path, "current-context: prod\ncontexts:\n- name: prod\n  context:\n"
        )
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig() is None

    def test_user_referenced_but_absent_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(
            tmp_path,
            "current-context: prod\n"
            "contexts:\n- name: prod\n  context:\n    user: ghost\n"
            "users:\n- name: someone-else\n  user:\n    token: t\n",
        )
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        assert _token_from_kubeconfig() is None

    def test_unreadable_file_returns_none(self, tmp_path, monkeypatch):
        # _kubeconfig_path gates on is_file, so force the OSError path directly.
        a_dir = tmp_path / "as-dir"
        a_dir.mkdir()
        monkeypatch.setattr(
            "presidio_arch_translucency.prometheus._kubeconfig_path", lambda: a_dir
        )
        assert _token_from_kubeconfig() is None

    def test_parse_failure_returns_none(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setenv("KUBECONFIG", str(cfg))

        def boom(_text):
            raise RuntimeError("parser blew up")

        monkeypatch.setattr(
            "presidio_arch_translucency.prometheus._parse_yaml_subset", boom
        )
        assert _token_from_kubeconfig() is None


class TestResolveToken:
    def test_env_token_takes_priority_over_kubeconfig(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        monkeypatch.setenv("PAT_PROMETHEUS_TOKEN", "env-wins")
        assert _resolve_token(_URL) == "env-wins"

    def test_falls_back_to_kubeconfig_when_env_absent(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        monkeypatch.delenv("PAT_PROMETHEUS_TOKEN", raising=False)
        assert _resolve_token(_URL) == "kube-prod-token-XYZ"

    def test_kubeconfig_context_env_override(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        monkeypatch.delenv("PAT_PROMETHEUS_TOKEN", raising=False)
        monkeypatch.setenv("PAT_KUBECONFIG_CONTEXT", "staging")
        assert _resolve_token(_URL) == "kube-staging-token-QRS"

    def test_no_token_anywhere_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PAT_PROMETHEUS_TOKEN", raising=False)
        monkeypatch.setenv("KUBECONFIG", str(tmp_path / "absent"))
        assert _resolve_token(_URL) is None

    def test_fetch_observation_uses_kubeconfig_token(self, tmp_path, monkeypatch):
        cfg = _write_kubeconfig(tmp_path, _KUBECONFIG_WITH_AUTH)
        monkeypatch.setenv("KUBECONFIG", str(cfg))
        monkeypatch.delenv("PAT_PROMETHEUS_TOKEN", raising=False)
        seen = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.get_header("Authorization"))
            return _urlopen_cm(_vector(4))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fetch_observation(_URL, "pod")
        assert seen and all(h == "Bearer kube-prod-token-XYZ" for h in seen)
