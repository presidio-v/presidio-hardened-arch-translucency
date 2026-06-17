# Security Audit -- presidio-hardened-arch-translucency v0.13.0

**Audit date:** 2026-06-17
**Commit audited:** `0691b8e` plus release-prep metadata and token-hardening fixes
**Scope:** v0.11.0 through v0.13.0 monitoring-arc work: `pat rules`, Grafana provisioning, `pat annotate`, ADR-0006, and `pat export --otlp`.

## Summary

No critical or high-severity issue was found. The release adds one outbound write path (`pat annotate`) and one outbound push path (`pat export --otlp`). Both keep credentials env-only and HTTPS-bound by default. The audit found and remediated one low-severity hardening gap: env bearer tokens accepted control characters before constructing `Authorization` headers.

## Findings

| Severity | Finding | Status |
|---|---|---|
| Low | `PAT_GRAFANA_TOKEN`, `PAT_OTLP_TOKEN`, and `PAT_PROMETHEUS_TOKEN` did not reject control characters before header construction. | Fixed |
| Info | OTLP without a token allows HTTP for in-cluster/local collectors. | Accepted; no credentials are sent, and tokened OTLP still requires HTTPS unless `--insecure-http`. |
| Info | Grafana provisioning defaults to `http://prometheus:9090`. | Accepted; this is an in-cluster datasource default and contains no credentials. |

## Remediation

- Grafana, OTLP, and Prometheus env-token helpers now reject control characters before returning a token.
- Regression tests cover control-character rejection for all three env tokens.
- `SECURITY.md` now documents all env-only telemetry tokens and the control-character rejection behavior.

## Verification

- `ruff check .`
- `ruff format --check .`
- `pytest tests/ -x -q --tb=short`
- `python -m pip_audit --progress-spinner=off`
- `pip-audit -r /tmp/presidio-arch-translucency-0.13.0-requirements.txt --progress-spinner=off`
- Focused tests: `tests/test_annotate.py`, `tests/test_otlp.py`, `tests/test_prometheus.py`
- CLI smoke: `pat rules`, `pat annotate --dry-run`, and `pat export --otlp` against a local OTLP/HTTP receiver
- Grafana dashboard JSON parsed successfully

## Residual risk

- Docker demo and benchmark behavior were not part of the v0.13.0 change surface.
- Base-image digest pinning remains a separate supply-chain follow-up already tracked in prior audits.
