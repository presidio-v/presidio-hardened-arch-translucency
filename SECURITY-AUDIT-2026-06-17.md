# Security Audit -- presidio-hardened-arch-translucency

**Audit date:** 2026-06-17
**Commit audited:** `47d0c10` (branch `main`)
**Remediation branch:** `codex/release-0.9.0-audit`
**Scope:** Current repository sources and documentation after the v0.9.0 feature
merge, with emphasis on release-readiness, daemon scheduling, audit logging,
Docker benchmark/demo behavior, Prometheus token handling, local file
permissions, CI/security configuration, and the release records in
`CHANGELOG.md`, `SECURITY.md`, and `PRESIDIO-REQ.md`.
**Methodology:** Manual source review of the changed Python modules, tests,
Docker harness, packaging metadata, CI workflows, Dependabot configuration,
security policy, changelog, and v0.9.0 registry record; pattern scanning for
credential handling, command/unit injection, unsafe network exposure, local file
permissions, stale release claims, and verification gaps. Local dynamic
verification could not be run in this execution environment because the process
runner cannot create `/bin/zsh`, `/bin/sh`, `/bin/bash`, or other attempted shell
paths. As a result, ruff, pytest, pip-audit, `uv lock`, the Docker daemon smoke
test, and local signed tagging remain unverified here.

---

## Executive summary

The v0.9.0 feature work is merged and the major hardening from the 2026-06-16
audit is present: Prometheus auth is env-only and HTTPS-bound, Docker demo ports
are loopback-bound, the embedded demo image runs as a non-root user, scheduler
unit rendering rejects control characters, and default local stores are
permission-hardened.

This audit found no critical remote-code-execution issue and no committed
secrets. Two actionable code issues were found and remediated on the release
branch: Linux systemd timers used an invalid calendar expression for the default
60-second interval and arbitrary second intervals, and audit log context did not
actually redact scalar values whose keys looked credential-bearing. Release
records were also updated by moving the v0.9.0 changelog content under a dated
release heading and refreshing `SECURITY.md`.

The release cannot be honestly cut from this runtime. The requested real Docker
smoke test and signed tag both require a working local process runner and local
signing infrastructure. In addition, `pyproject.toml`, `__init__.py`, and the
editable package entry in `uv.lock` still advertise `0.8.0`; changing that
properly requires regenerating `uv.lock` with `uv lock` and running the full
verification suite.

### Findings at a glance

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| 1 | Medium | systemd timer cadence used an invalid seconds-field `OnCalendar` expression | `src/presidio_arch_translucency/daemon.py` |
| 2 | Low | Audit log context allowed secret-shaped scalar values through | `src/presidio_arch_translucency/security.py` |
| 3 | Low | v0.9.0 changelog and security policy still described unreleased/prerelease state | `CHANGELOG.md`, `SECURITY.md` |
| 4 | Low | Package metadata still advertises `0.8.0` while release records target `v0.9.0` | `pyproject.toml`, `src/presidio_arch_translucency/__init__.py`, `uv.lock` |
| 5 | Info | Local Docker smoke test and local signed release tag remain blocked by unavailable process runner | local environment |
| 6 | Info | Base-image digest pinning remains pending from the prior audit | embedded `_DOCKERFILE`, `demo/Dockerfile` |

---

## Remediation status (updated 2026-06-17)

The following remediation was applied on branch `codex/release-0.9.0-audit`.
Local execution of ruff, pytest, pip-audit, Docker smoke tests, `uv lock`, and
git signing was not possible in this environment because no configured shell
binary could be created.

| # | Status | What changed |
|---|--------|--------------|
| 1 | Fixed | `render_systemd_timer()` now uses elapsed scheduling with `OnBootSec=<interval>s` and `OnUnitActiveSec=<interval>s` instead of relying on the invalid `OnCalendar=*:*:00/<interval>` seconds-field expression. |
| 2 | Fixed | `_sanitize_log_context()` now redacts scalar values whose keys contain `token`, `secret`, `password`, `key`, `credential`, or `auth`, while continuing to drop non-scalar values. A focused regression test covers token/password-shaped keys. |
| 3 | Fixed | `CHANGELOG.md` now has a dated `## [0.9.0] - 2026-06-17` section and `[Unreleased]` now compares `v0.9.0...HEAD`. `SECURITY.md` now lists `main / 0.9.x`, updates the known-limitations heading, and links this audit. |
| 4 | Blocked | The package-version bump was not applied because `uv.lock` contains the editable package version and must be regenerated with `uv lock`; the local process runner is unavailable here. Changing `pyproject.toml` without lock regeneration would likely fail the `lock-drift` CI job. |
| 5 | Blocked | The requested real end-to-end Docker smoke test and signed tag were not run because local commands cannot start in this runtime. These must be run on the local workstation once shell/process execution is restored. |
| 6 | Deferred | Digest pinning still requires selecting and maintaining verified upstream image digests. This remains a separate supply-chain task from the release-cut audit. |

---

## Detailed findings

### 1. Invalid systemd timer cadence -- Medium

**Location:** `src/presidio_arch_translucency/daemon.py`

The Linux observe daemon rendered timers as `OnCalendar=*:*:00/<interval>`. That
format places the step in the seconds field. It is not suitable for the default
`60` second interval because valid seconds are `0..59`, and it also does not
support arbitrary second intervals consistently.

**Impact:** A user installing `pat observe daemon install` on Linux could receive
a malformed or ineffective systemd user timer, breaking the advertised continuous
collection workflow.

**Recommendation:** Use monotonic elapsed-time systemd timer directives for
second-based cadence.

**Status:** Fixed. The timer now uses `OnBootSec=<interval>s` and
`OnUnitActiveSec=<interval>s` with the existing `Persistent=true` and unit
binding.

---

### 2. Secret-shaped audit context values were not redacted -- Low

**Location:** `src/presidio_arch_translucency/security.py`

`_sanitize_log_context()` documented that it stripped context values that looked
like secrets, but the implementation only filtered by scalar type. Future callers
that accidentally supplied `api_token`, `password`, `secret`, `auth_header`, or
similar keys could have those values emitted into the audit logger.

**Impact:** No current reviewed call path was found passing bearer tokens into
this helper, so this is preventive hardening rather than an observed leak. It
still matters because the helper is the shared logging boundary.

**Recommendation:** Redact values when the context key is credential-shaped and
keep dropping non-scalar values.

**Status:** Fixed. Credential-shaped keys are redacted to `[REDACTED]`; tests
cover dynamically constructed token/password keys.

---

### 3. Release records still described unreleased/prerelease state -- Low

**Location:** `CHANGELOG.md`, `SECURITY.md`

The v0.9.0 feature set was merged to `main`, but the changelog still kept the
release entries under `[Unreleased]`, and the security policy listed
`main / 0.9.0 prerelease` plus a `main / v0.8.x` limitations heading.

**Impact:** Users and maintainers could not tell which behavior belonged to the
cut release, and release automation would not have a dated changelog section for
`v0.9.0`.

**Recommendation:** Move the v0.9.0 content under a dated release heading,
refresh changelog comparison links, and update security policy support/audit
history.

**Status:** Fixed. The changelog now has `## [0.9.0] - 2026-06-17` and
`SECURITY.md` links this report.

---

### 4. Package metadata remains at 0.8.0 -- Low

**Location:** `pyproject.toml`, `src/presidio_arch_translucency/__init__.py`,
`uv.lock`

The release records target `v0.9.0`, but the package metadata and module
`__version__` still say `0.8.0`. The lock file also contains an editable package
entry for `presidio-hardened-arch-translucency` at `0.8.0`.

**Impact:** If a distribution artifact is built from the tag without updating
these files, installed package metadata and runtime `__version__` will disagree
with the release tag.

**Recommendation:** Update `pyproject.toml` and `__init__.py` to `0.9.0`, run
`uv lock` to refresh the editable package entry, then run the full Python
verification suite and dependency audit.

**Status:** Blocked in this runtime. The files were not changed because doing so
without regenerating `uv.lock` would create lock drift. This should be completed
locally before signing/tagging.

---

### 5. Required local Docker smoke and signed tag blocked -- Info

**Location:** local environment

The requested release gate includes a real end-to-end smoke test against the
local Docker daemon and a signed release tag using local signing infrastructure.
Those operations cannot be performed through the GitHub contents API and require
a working local command runner.

**Impact:** The release cannot be cut with the requested assurance from this
runtime.

**Recommendation:** Restore local process execution, then run the Docker smoke
against the daemon, the full Python checks, `pip-audit`, and `git tag -s v0.9.0`
(or the repository's configured signing command) before pushing the tag.

**Status:** Blocked. No tag was created.

---

### 6. Base-image digest pinning pending -- Info

**Location:** embedded `_DOCKERFILE`, `demo/Dockerfile`

The demo images still use mutable upstream base tags. The image is now
loopback-bound and non-root, but mutable base tags are not fully reproducible.

**Recommendation:** Pin the base image to a verified digest and establish a
refresh process.

**Status:** Deferred, unchanged from the 2026-06-16 audit.

---

## Positive observations

- No committed secrets, API keys, or tokens were found in the reviewed files.
- No `eval`, `exec`, unsafe YAML loading, pickle loading, or `shell=True` use was
  found in the reviewed code.
- Prometheus bearer auth remains explicit, env-only, and HTTPS-bound.
- Docker demo and benchmark workloads publish host ports to `127.0.0.1` only.
- The embedded demo workload runs as an unprivileged user and has a healthcheck.
- Default observation/model stores are owner-only where the platform supports
  chmod.
- Dependabot is active and the Codecov action is pinned to the v7.0.0 commit
  merged immediately before the v0.9.0 release-prep branch.

---

## Verification notes

Local verification was attempted but could not run because the execution
environment had no usable shell/process runner. Attempts to start `/bin/zsh`,
`/bin/sh`, `/bin/bash`, `/usr/bin/zsh`, and elevated `git status` all failed
before command execution with a no-such-file process creation error. Therefore,
the following required commands remain to be run on the local workstation:

```bash
.venv/bin/python -m ruff check . && .venv/bin/python -m ruff format --check . && .venv/bin/python -m pytest tests/ -x -q --tb=short
pip-audit
uv lock --locked
pat calibrate --benchmark --layer container --replicas 1 --replicas 2 --requests 10 --concurrency 2 --iterations 1000
```

After package metadata is bumped and all checks/smoke tests pass, cut the signed
release tag locally, for example with the repository's configured signing
identity:

```bash
git tag -s v0.9.0 -m "Release v0.9.0"
git push origin v0.9.0
```

---

*This report reflects a point-in-time manual review and does not replace
automated SAST/DAST or a penetration test.*
