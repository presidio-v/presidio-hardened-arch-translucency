"""Tests for the ``pat-exporter`` Helm chart (v0.16.0, "Package & operate").

The chart is the seventh step of the monitoring-integration arc — cluster-native
packaging of the read-only ``pat export`` endpoint. CI has no Helm binary, so
these tests guard the chart two ways:

  * **Exact sync** — the bundled ``files/pat-rules.yaml`` must equal what
    ``pat rules`` emits, and ``files/pat-dashboard.json`` must equal the official
    ``grafana/pat-dashboard.json``. This is the chart's analogue of
    ``test_grafana.py``: bundled artifacts can never silently drift.
  * **Security & structural invariants** — string/regex assertions that lock the
    hardened posture (non-root, read-only rootfs, dropped caps, no mounted SA
    token) and the emit-only arc invariant A1.

When a ``helm`` binary is present (developer machines), an extra test renders the
chart end-to-end and re-checks the posture on the rendered manifests.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from presidio_arch_translucency.rules import build_rule_groups, render_rules_yaml

REPO = Path(__file__).resolve().parent.parent
CHART = REPO / "charts" / "pat-exporter"
TEMPLATES = CHART / "templates"


def _read(rel: str) -> str:
    return (CHART / rel).read_text(encoding="utf-8")


# ── chart metadata ────────────────────────────────────────────────────────────


def test_chart_yaml_present_and_v2() -> None:
    chart = _read("Chart.yaml")
    assert re.search(r"^apiVersion:\s*v2\s*$", chart, re.MULTILINE)
    assert re.search(r"^name:\s*pat-exporter\s*$", chart, re.MULTILINE)
    assert re.search(r"^type:\s*application\s*$", chart, re.MULTILINE)
    # Chart + app version are kept in lock-step for the 0.16.0 packaging release.
    assert re.search(r"^version:\s*0\.16\.0\s*$", chart, re.MULTILINE)
    assert re.search(r'^appVersion:\s*"0\.16\.0"\s*$', chart, re.MULTILINE)


def test_expected_template_files_exist() -> None:
    expected = {
        "_helpers.tpl",
        "serviceaccount.yaml",
        "deployment.yaml",
        "service.yaml",
        "servicemonitor.yaml",
        "prometheusrule.yaml",
        "dashboard-configmap.yaml",
        "networkpolicy.yaml",
        "NOTES.txt",
    }
    present = {p.name for p in TEMPLATES.iterdir()}
    assert expected <= present, f"missing templates: {expected - present}"


# ── bundled-artifact sync (exact) ─────────────────────────────────────────────


def test_bundled_rules_match_pat_rules_output() -> None:
    """files/pat-rules.yaml must be the verbatim default `pat rules` emission."""
    expected = render_rules_yaml(build_rule_groups())
    assert _read("files/pat-rules.yaml") == expected


def test_bundled_dashboard_matches_official() -> None:
    """files/pat-dashboard.json must equal grafana/pat-dashboard.json byte-for-byte."""
    official = (REPO / "grafana" / "pat-dashboard.json").read_text(encoding="utf-8")
    assert _read("files/pat-dashboard.json") == official


# ── security posture (hardening invariants) ───────────────────────────────────


def test_values_default_security_context_hardened() -> None:
    values = _read("values.yaml")
    for needle in (
        "runAsNonRoot: true",
        "readOnlyRootFilesystem: true",
        "allowPrivilegeEscalation: false",
        "privileged: false",
        "type: RuntimeDefault",
    ):
        assert needle in values, f"values.yaml missing hardening: {needle!r}"
    # Capabilities are fully dropped.
    assert re.search(r"capabilities:\s*\n\s*drop:\s*\n\s*-\s*ALL", values)


def test_service_account_token_never_mounted() -> None:
    """Least privilege: the read-only exporter needs no API token (arc A1)."""
    assert "automount: false" in _read("values.yaml")
    sa = _read("templates/serviceaccount.yaml")
    assert "automountServiceAccountToken: {{ .Values.serviceAccount.automount }}" in sa
    # The Deployment also pins it off explicitly, independent of the SA default.
    assert (
        "automountServiceAccountToken: {{ .Values.serviceAccount.automount }}"
        in _read("templates/deployment.yaml")
    )


def test_deployment_applies_both_security_contexts() -> None:
    dep = _read("templates/deployment.yaml")
    assert "securityContext:\n        {{- toYaml .Values.podSecurityContext" in dep
    assert "securityContext:\n            {{- toYaml .Values.securityContext" in dep


def test_no_rbac_role_in_chart() -> None:
    """Emit-only: the chart grants the exporter no Role/ClusterRole bindings."""
    for tmpl in TEMPLATES.glob("*.yaml"):
        text = tmpl.read_text(encoding="utf-8")
        assert "kind: Role" not in text
        assert "kind: ClusterRole" not in text
        assert "kind: RoleBinding" not in text


# ── exporter wiring (read-only serve) ─────────────────────────────────────────


def test_deployment_runs_readonly_export_server() -> None:
    dep = _read("templates/deployment.yaml")
    # Serves /metrics over a routable interface (required inside a pod) but the
    # endpoint is read-only — there is no apply path.
    assert "- export" in dep
    assert "- --listen-public" in dep
    assert "- --host\n            - 0.0.0.0" in dep
    # Probes hit the read-only liveness path.
    assert "path: /healthz" in _read("values.yaml")


def test_predict_and_cost_are_gated() -> None:
    dep = _read("templates/deployment.yaml")
    assert "{{- if .Values.predict.enabled }}" in dep
    assert "{{- if .Values.costPerReplicaHour }}" in dep
    values = _read("values.yaml")
    # Prediction is off by default (needs a populated observation store).
    assert re.search(r"predict:\s*\n\s*enabled:\s*false", values)


# ── operator/sidecar artifacts gated off by default ───────────────────────────


@pytest.mark.parametrize(
    "tmpl,guard",
    [
        ("servicemonitor.yaml", ".Values.serviceMonitor.enabled"),
        ("prometheusrule.yaml", ".Values.prometheusRule.enabled"),
        ("dashboard-configmap.yaml", ".Values.dashboard.enabled"),
        ("networkpolicy.yaml", ".Values.networkPolicy.enabled"),
    ],
)
def test_optional_objects_are_guarded(tmpl: str, guard: str) -> None:
    text = _read(f"templates/{tmpl}")
    assert f"{{{{- if {guard} -}}}}" in text


def test_optional_objects_default_off() -> None:
    values = _read("values.yaml")
    for block in ("serviceMonitor", "prometheusRule", "dashboard", "networkPolicy"):
        assert re.search(rf"{block}:\s*\n\s*enabled:\s*false", values), (
            f"{block} should default to enabled: false"
        )


def test_servicemonitor_and_rule_use_operator_api() -> None:
    for tmpl in ("servicemonitor.yaml", "prometheusrule.yaml"):
        assert "apiVersion: monitoring.coreos.com/v1" in _read(f"templates/{tmpl}")


def test_prometheusrule_injects_rules_via_files_get() -> None:
    """Rules are injected with .Files.Get so Prometheus {{ $value }} survives."""
    rule = _read("templates/prometheusrule.yaml")
    assert '.Files.Get "files/pat-rules.yaml"' in rule
    # And the bundled file really does carry Prometheus templating that would
    # break if it were run through Helm's templater.
    assert "{{ $value" in _read("files/pat-rules.yaml")


def test_dashboard_configmap_has_sidecar_label() -> None:
    cm = _read("templates/dashboard-configmap.yaml")
    assert "{{ .Values.dashboard.sidecarLabel }}" in cm
    assert '.Files.Get "files/pat-dashboard.json"' in cm
    assert re.search(r"sidecarLabel:\s*grafana_dashboard", _read("values.yaml"))


def test_networkpolicy_restricts_to_metrics_port() -> None:
    np = _read("templates/networkpolicy.yaml")
    assert "kind: NetworkPolicy" in np
    assert "policyTypes:\n    - Ingress" in np
    assert "port: {{ .Values.service.portName }}" in np


# ── full render (only when a helm binary is available) ────────────────────────

_HELM = shutil.which("helm")


@pytest.mark.skipif(_HELM is None, reason="helm binary not installed")
def test_helm_lint_passes() -> None:
    proc = subprocess.run(  # noqa: S603
        [_HELM, "lint", str(CHART)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(_HELM is None, reason="helm binary not installed")
def test_helm_template_renders_hardened_manifests() -> None:
    proc = subprocess.run(  # noqa: S603
        [
            _HELM,
            "template",
            "pat",
            str(CHART),
            "--set",
            "serviceMonitor.enabled=true",
            "--set",
            "prometheusRule.enabled=true",
            "--set",
            "dashboard.enabled=true",
            "--set",
            "networkPolicy.enabled=true",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    # Rendered posture survives templating.
    assert "runAsNonRoot: true" in out
    assert "readOnlyRootFilesystem: true" in out
    assert "automountServiceAccountToken: false" in out
    # All optional objects rendered.
    assert "kind: ServiceMonitor" in out
    assert "kind: PrometheusRule" in out
    assert "kind: NetworkPolicy" in out
    # Prometheus templating passed through untouched (not eaten by Helm).
    assert "{{ $value | humanize }}" in out
