# Security Audit -- presidio-hardened-arch-translucency v0.15.0

**Audit date:** 2026-06-18
**Commit audited:** `86d6f1d` plus release-prep metadata and URL/token-hardening fixes
**Scope:** v0.14.0 through v0.15.0 monitoring-arc work: `pat export --pushgateway`, ADR-0007, and `pat scaler`.

## Summary

No critical or high-severity issue was found. The release adds one outbound metrics push path and one YAML-emitting autoscaler path. Pushgateway tokens remain env-only and HTTPS-bound by default; scaler output remains emit-only and does not contact a cluster or apply changes.

The audit found and remediated three low-severity hardening and integration gaps:

- outbound HTTP endpoint validators accepted URLs with embedded userinfo such as `https://user:pass@example`, which could leak credentials through request targets, emitted YAML, or audit-log host context;
- env token helpers trimmed tokens before checking control characters, so trailing newline/control characters could be silently removed instead of rejected.

## Findings

| Severity | Finding | Status |
|---|---|---|
| Low | Grafana, OTLP, Prometheus, Pushgateway, and scaler Prometheus URLs accepted embedded credentials. | Fixed |
| Low | Env telemetry tokens could be stripped before control-character rejection. | Fixed |
| Low | Grafana datasource provisioning used shell-style env default syntax that Grafana 13 expanded to an empty URL. | Fixed |
| Info | Pushgateway without a token allows HTTP for local or in-cluster gateways. | Accepted; no credentials are sent, and tokened Pushgateway still requires HTTPS unless `--insecure-http`. |
| Info | `pat scaler --format prometheus-adapter` emits a commented adapter-rule example rather than mutating the cluster-wide adapter ConfigMap. | Accepted; emit-only posture is intentional and avoids unsafe config mutation. |

## Remediation

- URL validators now require an http(s) URL with a parsed hostname and reject `username` or `password` components.
- `PAT_PROMETHEUS_TOKEN`, `PAT_GRAFANA_TOKEN`, `PAT_OTLP_TOKEN`, and `PAT_PUSHGATEWAY_TOKEN` are checked for raw control characters before trimming whitespace.
- Regression tests cover embedded credential rejection and trailing control-character token rejection.
- `SECURITY.md` now documents v0.15.x support and `PAT_PUSHGATEWAY_TOKEN` in the env-only telemetry-token set.
- Grafana datasource provisioning now uses a static `http://prometheus:9090` default that loaded correctly in a real Grafana 13.0.2 container.

## Verification

- `ruff check .`
- `ruff format --check .`
- `pytest tests/ -x -q --tb=short`
- `python -m pip_audit --progress-spinner=off`
- `pip-audit -r /tmp/presidio-arch-translucency-0.15.0-requirements.txt --progress-spinner=off`
- Focused tests: `tests/test_pushgateway.py`, `tests/test_scaler.py`, `tests/test_annotate.py`, `tests/test_otlp.py`, and `tests/test_prometheus.py`
- CLI smoke: `pat export --once`, `pat export --pushgateway` against a local HTTP PUT listener, and `pat scaler` in KEDA and Prometheus-Adapter modes
- Docker Grafana smoke: `grafana/grafana-oss:latest` 13.0.2 with repository provisioning mounted; health OK, dashboard `pat-translucency` loaded, datasource `prometheus` URL `http://prometheus:9090`

## Residual risk

- Pushgateway retention and deletion semantics remain an operational responsibility; this tool performs one PUT for the selected job/grouping and exits.
- Docker demo and benchmark behavior were not part of the v0.14.0/v0.15.0 change surface.
- Base-image digest pinning remains a separate supply-chain follow-up already tracked in prior audits.
