# Security Audit -- presidio-hardened-arch-translucency

**Audit date:** 2026-06-16
**Commit audited:** `730072b` (branch `main`)
**Remediation branch:** `codex/security-audit-remediation-2026-06-16`
**Scope:** Current repository sources and documentation, with emphasis on the
v0.8.0/v0.9.0 surfaces added after the 2026-06-03 audit: Prometheus
observation, daemon scheduler installation, demo Docker workload, observation
and calibration stores, packaging metadata, and security documentation.
**Methodology:** Manual source review of the changed Python modules, tests,
Docker artifacts, packaging files, and security documentation; pattern scanning
for token handling, injection sinks, unsafe network exposure, filesystem
permissions, supply-chain drift, and stale security claims. Local dynamic
verification could not be run in this execution environment because both
configured shells (`/bin/zsh` and `/bin/sh`) were unavailable.

---

## Executive summary

The project continues to show strong security intent: no committed secrets were
found, outbound HTTP calls use standard TLS validation, subprocess use remains
argument-list based, and the CLI consistently avoids logging credentials. The
largest issue found in this audit was introduced by the planned v0.9.0
Prometheus auth work: automatically reading a kubeconfig bearer token and
sending it to an arbitrary user-supplied Prometheus URL created a realistic
credential-disclosure path, especially when paired with non-HTTPS URLs.

No critical remote-code-execution issue was identified. The actionable issues
were concentrated around newly added surfaces: Prometheus auth, daemon unit file
generation, local demo Docker exposure, local store permissions, and stale
security documentation.

### Findings at a glance

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| 1 | High | Kubeconfig bearer token could be sent to arbitrary `--prometheus` URL | `src/presidio_arch_translucency/prometheus.py` |
| 2 | Medium | Daemon unit generation allowed unsafe argv/control-character content | `src/presidio_arch_translucency/daemon.py` |
| 3 | Medium | `pat demo` published Docker ports beyond loopback and embedded image ran as root | `src/presidio_arch_translucency/demo.py` |
| 4 | Low | AWS cloud demo consumed tiered pricing result with the wrong shape | `src/presidio_arch_translucency/demo.py` |
| 5 | Low | Observation/model stores were not permission-hardened | `src/presidio_arch_translucency/observe.py`, `src/presidio_arch_translucency/calibrate.py` |
| 6 | Low | Security policy and audit history were stale for v0.8/v0.9 surfaces | `SECURITY.md` |
| 7 | Info | Heavy default dependency surface remains larger than the core CLI needs | `pyproject.toml`, `uv.lock` |
| 8 | Info | Base-image digest pinning remains pending | embedded `_DOCKERFILE`, `demo/Dockerfile` |

---

## Remediation status (updated 2026-06-16)

The following remediation was applied on branch
`codex/security-audit-remediation-2026-06-16`. Focused regression tests were
added or updated for each code-level fix, but local execution of ruff, pytest,
and pip-audit was not possible in this environment because no configured shell
binary was available.

| # | Status | What changed |
|---|--------|--------------|
| 1 | Fixed | `pat observe --prometheus` no longer reads kubeconfig tokens automatically. Bearer auth is now env-only via `PAT_PROMETHEUS_TOKEN`; token use requires an HTTPS Prometheus URL; Prometheus URLs and query strings reject control characters. Tests cover env-only auth, kubeconfig non-use, HTTPS enforcement, and control-character rejection. |
| 2 | Fixed | `pat observe daemon install` now validates Prometheus URLs, layer names, and intervals before rendering units; systemd `ExecStart=` arguments are quoted and `%`-escaped; control characters are rejected; generated unit files are written owner-only where chmod is supported. Tests cover validation, quoting, escaping, and file modes. |
| 3 | Fixed / partial | `pat demo` now publishes Docker ports to `127.0.0.1` only. The embedded image used by `pat demo` now creates an unprivileged `appuser`, runs as that user, and includes a `/health` healthcheck. Base-image digest pinning remains a separate supply-chain task. |
| 4 | Fixed | The cloud demo now normalizes flat and tiered pricing results through `_on_demand_pricing`, using the `on_demand` price from `TieredPricingResult` and preserving the source description. Tests cover the tiered path. |
| 5 | Fixed | Default observation and calibrated-model stores now create `~/.pat` owner-only and chmod the SQLite/model files to `0o600` where supported. Tests cover default store permissions. |
| 6 | Fixed | `SECURITY.md` now reflects current supported versions, Prometheus token handling, private local stores, demo isolation, known limitations, and the manual audit history. |
| 7 | Deferred | Moving Docker, plotting, SciPy, and statsmodels into optional extras would require regenerating `uv.lock`. That should be done in a normal development environment with `uv lock` and full CI verification to avoid lock drift. |
| 8 | Deferred | Digest pinning requires selecting and maintaining verified upstream image digests. The embedded demo image was hardened, but the digest decision is left as a follow-up to avoid pinning an unverified value. |

---

## Detailed findings

### 1. Kubeconfig bearer token leakage to arbitrary Prometheus URL -- High

**Location:** `src/presidio_arch_translucency/prometheus.py`

The v0.9.0 Prometheus auth path attempted to improve Kubernetes usability by
falling back from `PAT_PROMETHEUS_TOKEN` to the active kubeconfig context. That
meant a command such as `pat observe --prometheus <url>` could automatically
read a cluster bearer token and attach it to a user-supplied URL. Because the
URL was not restricted to HTTPS and was not tied to the kube-apiserver host,
this created a credential disclosure path to an arbitrary endpoint.

**Impact:** A local user or automation job could unintentionally exfiltrate a
cluster bearer token to a non-cluster Prometheus URL, including cleartext HTTP.
This is the most material issue in this audit because it crosses a credential
boundary.

**Recommendation:** Keep bearer tokens explicit and environment-only, require
HTTPS whenever a bearer token is present, and reject control characters in URLs
and query strings.

**Status:** Fixed. Kubeconfig fallback was removed, HTTPS is required when
`PAT_PROMETHEUS_TOKEN` is used, control characters are rejected, and tests were
added.

---

### 2. Daemon unit argv/control-character injection -- Medium

**Location:** `src/presidio_arch_translucency/daemon.py`

The daemon installer rendered scheduler units from user-controlled values such
as `--prometheus` and `--layer`. The systemd `ExecStart=` line was produced by
joining argv with spaces, which is not equivalent to systemd argv quoting. A
value containing whitespace, `%` specifiers, or control characters could alter
how systemd parsed the intended command or produce malformed unit content.

**Impact:** This is local-only and requires the user to install a daemon unit,
but it can produce surprising unit behavior and weakens the trust boundary
between CLI input and scheduler configuration.

**Recommendation:** Validate scheduler inputs before rendering, reject control
characters, quote systemd argv according to systemd expectations, escape `%`,
and write generated unit files with owner-only permissions.

**Status:** Fixed. Input validation, systemd quoting, `%` escaping, control-
character rejection, and owner-only writes were implemented with focused tests.

---

### 3. Demo Docker exposure and embedded image hardening -- Medium

**Location:** `src/presidio_arch_translucency/demo.py`

The previous audit hardened `demo/Dockerfile`, but `pat demo` builds an embedded
Dockerfile from `demo.py`. That embedded Dockerfile still ran as root and did
not include the same hardening. The Docker SDK port mappings also published
container ports through Docker's default host binding behavior instead of
explicitly binding to loopback.

**Impact:** `pat demo` is intended for local experimentation, but CPU-bound demo
workloads should not be reachable from other hosts. Running the embedded
workload as root increases blast radius if the demo endpoint is exposed.

**Recommendation:** Bind all published demo ports to `127.0.0.1`, run the
embedded image as a non-root user, and include a healthcheck.

**Status:** Fixed / partial. Loopback binding, non-root runtime, and healthcheck
were added to the embedded demo path. Base-image digest pinning remains pending.

---

### 4. AWS cloud demo tiered-pricing result mismatch -- Low

**Location:** `src/presidio_arch_translucency/demo.py`

`cloud.build_cost_params_from_aws()` can return `TieredPricingResult`, but the
cloud demo read `.params`, `.from_cache`, and `.source_description` directly on
the returned object. That shape matches a flat pricing result, not the tiered
wrapper.

**Impact:** The `pat demo --cloud aws` path could fail or silently skip the
intended pricing source, reducing trust in demo output and any follow-on cost
comparison.

**Recommendation:** Normalize cloud pricing results before use and explicitly
choose the on-demand tier when a tiered result is returned.

**Status:** Fixed. `_on_demand_pricing` handles both result shapes and tests
cover the tiered path.

---

### 5. Observation/model store permissions -- Low

**Location:** `src/presidio_arch_translucency/observe.py`,
`src/presidio_arch_translucency/calibrate.py`

Default local stores under `~/.pat` were created with the process umask. These
files can contain workload measurements, service-layer names, and fitted
capacity assumptions. They are not credentials, but they can reveal sensitive
operational characteristics.

**Impact:** On permissive systems or shared-home configurations, other local
users could read observations or model data.

**Recommendation:** Create the default store directory owner-only and chmod
store files to `0o600` where supported. Preserve custom path behavior but still
make the created file owner-only when possible.

**Status:** Fixed. Default stores now harden `~/.pat` and the database/model
files. Tests cover the default-path behavior.

---

### 6. Stale security documentation -- Low

**Location:** `SECURITY.md`

`SECURITY.md` did not describe the new observation, daemon, Prometheus auth, or
demo hardening posture, and it did not link the current manual audit result.

**Impact:** Users and maintainers could rely on incomplete security claims,
especially around token handling and local demo exposure.

**Recommendation:** Keep supported versions, security features, known
limitations, and audit links current with each security-relevant feature.

**Status:** Fixed. The policy now documents current behavior and links this
report plus the 2026-06-03 report.

---

### 7. Heavy default dependency surface -- Info

**Location:** `pyproject.toml`, `uv.lock`

The default install still includes dependencies that are only needed by specific
paths: Docker for demos, matplotlib for plotting, SciPy for calibration, and
statsmodels for ARIMA. This increases the default CVE and supply-chain surface
for users who only need the core analysis CLI.

**Recommendation:** Move feature-specific dependencies into optional extras
such as `[demo]`, `[plot]`, `[calibrate]`, and `[forecast]`, then regenerate
`uv.lock` and run the full verification suite.

**Status:** Deferred. This was not changed in this remediation because the lock
file must be regenerated in a functioning development environment.

---

### 8. Base-image digest pinning pending -- Info

**Location:** embedded `_DOCKERFILE`, `demo/Dockerfile`

The demo images still use the mutable `python:3.12-slim` tag. The embedded image
is now non-root and healthchecked, but mutable base tags are not fully
reproducible.

**Recommendation:** Pin the base image to a verified digest and establish a
process for digest refreshes.

**Status:** Deferred. A verified digest was not selected during this audit.

---

## Positive observations

- No committed secrets, API keys, or tokens were found.
- No `eval`, `exec`, unsafe YAML loading, pickle loading, or `shell=True` use was
  found in the reviewed code.
- Prometheus bearer auth is now explicit, env-only, and HTTPS-bound.
- Scheduler generation now treats user input as structured argv rather than raw
  unit text.
- Local observation/model artifacts are now owner-only by default.
- The Docker demo is now local-only by host binding and runs the embedded
  workload as an unprivileged user.
- Security documentation now records the current limitations instead of relying
  on stale release-era notes.

---

## Verification notes

Local verification was attempted but could not run because the execution
environment had no usable `/bin/zsh` or `/bin/sh`, so `ruff`, `pytest`, `uv`, and
`pip-audit` could not be executed here. Focused tests were added for the
security-sensitive changes and should be run by CI or a normal development
workstation with:

```bash
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check . && .venv/bin/python -m pytest tests/ -x -q --tb=short
```

A direct push to `main` was rejected by branch protection because required
status checks must pass first. The remediation was therefore pushed to branch
`codex/security-audit-remediation-2026-06-16` for review/merge.

---

*This report reflects a point-in-time manual review and does not replace
automated SAST/DAST or a penetration test.*
