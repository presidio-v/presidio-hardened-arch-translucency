"""Tests for the HPA manifest emitter (v0.8.0 Phase 5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.hpa_patch import HpaPatchError, build_hpa_patch
from presidio_arch_translucency.observe import Observation, record_observation

runner = CliRunner()

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# build_hpa_patch
# ---------------------------------------------------------------------------


class TestBuildHpaPatch:
    def test_basic_manifest(self):
        out = build_hpa_patch("web", min_replicas=4, max_replicas=8)
        assert "kind: HorizontalPodAutoscaler" in out
        assert "apiVersion: autoscaling/v2" in out
        assert "  name: web" in out
        assert "    kind: Deployment" in out
        assert "    name: web" in out
        assert "  minReplicas: 4" in out
        assert "  maxReplicas: 8" in out
        assert out.endswith("\n")

    def test_namespace_included_when_given(self):
        out = build_hpa_patch("web", 2, 5, namespace="prod")
        assert "  namespace: prod" in out

    def test_namespace_omitted_by_default(self):
        out = build_hpa_patch("web", 2, 5)
        assert "namespace:" not in out

    def test_separate_hpa_name(self):
        out = build_hpa_patch("web", 2, 5, hpa_name="web-hpa")
        assert "  name: web-hpa" in out  # metadata.name
        assert "    name: web" in out  # scaleTargetRef.name

    def test_max_clamped_to_min(self):
        out = build_hpa_patch("web", min_replicas=6, max_replicas=3)
        assert "  minReplicas: 6" in out
        assert "  maxReplicas: 6" in out  # max raised to min

    def test_min_below_one_raises(self):
        with pytest.raises(HpaPatchError, match="min_replicas"):
            build_hpa_patch("web", min_replicas=0, max_replicas=4)

    @pytest.mark.parametrize(
        "bad",
        ["Web", "web_svc", "web.svc", "-web", "web-", "", "a" * 64, "web; rm -rf /"],
    )
    def test_invalid_target_name_raises(self, bad):
        with pytest.raises(HpaPatchError, match="valid Kubernetes name"):
            build_hpa_patch(bad, 2, 5)

    def test_invalid_namespace_raises(self):
        with pytest.raises(HpaPatchError):
            build_hpa_patch("web", 2, 5, namespace="Prod Namespace")

    def test_no_user_input_leaks_unsanitised(self):
        # A name with YAML/shell metacharacters must be rejected, never echoed.
        with pytest.raises(HpaPatchError):
            build_hpa_patch("web\n  evil: true", 2, 5)


# ---------------------------------------------------------------------------
# CLI: pat optimize --emit-hpa-patch
# ---------------------------------------------------------------------------


def _seed(db, n, rps0=200, slope=10, layer="container", replicas=3):
    for i in range(n):
        rps = rps0 + slope * i
        record_observation(
            Observation(
                _T0 + timedelta(minutes=i),
                float(rps),
                80,
                140,
                rps * 0.97,
                layer,
                replicas,
            ),
            db_path=db,
        )


def _invoke(*args):
    return runner.invoke(app, ["--skip-audit", "optimize", *args])


class TestEmitHpaPatchCLI:
    def test_emits_yaml_to_stdout(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed(db, 12)
        result = _invoke("--emit-hpa-patch", "--target", "web", "--db", str(db))
        assert result.exit_code == 0
        assert "kind: HorizontalPodAutoscaler" in result.output
        assert "name: web" in result.output
        assert "minReplicas:" in result.output
        # No Rich recommendation panel when emitting the manifest.
        assert "Optimize (" not in result.output

    def test_requires_target(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed(db, 12)
        result = _invoke("--emit-hpa-patch", "--db", str(db))
        assert result.exit_code == 2
        assert "requires --target" in result.output

    def test_invalid_target_exits_2(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed(db, 12)
        result = _invoke("--emit-hpa-patch", "--target", "Web_Service", "--db", str(db))
        assert result.exit_code == 2
        assert "Cannot emit HPA patch" in result.output

    def test_namespace_flag(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed(db, 12)
        result = _invoke(
            "--emit-hpa-patch",
            "--target",
            "web",
            "--namespace",
            "prod",
            "--db",
            str(db),
        )
        assert result.exit_code == 0
        assert "namespace: prod" in result.output

    def test_empty_store_emits_nothing(self, tmp_path):
        result = _invoke(
            "--emit-hpa-patch", "--target", "web", "--db", str(tmp_path / "obs.db")
        )
        assert result.exit_code == 0
        assert "kind: HorizontalPodAutoscaler" not in result.output
        assert "No observations" in result.output
