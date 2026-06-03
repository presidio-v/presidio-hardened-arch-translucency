# Security Audit — presidio-hardened-arch-translucency

**Audit date:** 2026-06-03
**Commit audited:** `9238263` (branch `claude/security-audit-Xz1F1`)
**Scope:** Full repository — Python source (`src/`), demo workload (`demo/`),
CI/CD workflows (`.github/`), packaging (`pyproject.toml`, `uv.lock`), and
supporting documentation.
**Methodology:** Manual source review of every Python module, workflow, and
container artifact; pattern scanning for secrets, injection sinks, and unsafe
calls; review of supply-chain / CI controls. No dynamic testing was performed.

---

## Executive summary

The codebase is a small, well-structured analysis CLI with genuine
security-hardening intent (input sanitization, structured audit logging,
on-run dependency auditing, Dependabot, CodeQL). No secrets, no `eval`/`exec`,
no `shell=True`, and no obviously exploitable remote vulnerability in the core
CLI were found. The `subprocess` usage is safe (argument-list form, no shell).

The findings below are mostly **defense-in-depth and supply-chain hardening**
gaps rather than directly exploitable flaws. The two most material items are:

1. **Unbounded compute in the demo workload server** (`demo/app.py`) — a
   CPU/availability DoS that is reachable by anyone who can reach the demo
   container's port.
2. **CI workflow runs without an explicit least-privilege `permissions`
   block**, on `pull_request`, while executing project code — a token-scope
   and supply-chain concern.

No critical, immediately-exploitable issue was identified in the published
library/CLI itself.

### Findings at a glance

| # | Severity | Finding | Location |
|---|----------|---------|----------|
| 1 | Medium | Unbounded `n` → CPU DoS + unhandled `ValueError` in demo server | `demo/app.py` |
| 2 | Medium | CI workflow lacks explicit least-privilege `permissions:` | `.github/workflows/ci.yml` |
| 3 | Medium | GitHub Actions pinned to mutable tags, not commit SHAs | all `.github/workflows/*` |
| 4 | Low | OData `$filter` injection / unvalidated price match (Azure) | `cloud_azure.py` |
| 5 | Low | Demo container image runs as root; base image tag unpinned | `demo/Dockerfile` |
| 6 | Low | Untrusted/unofficial pricing endpoint feeds cost decisions (GCP) | `cloud_gcp.py` |
| 7 | Low | Unauthenticated local pricing cache trusted without integrity checks | `cloud.py` |
| 8 | Low | `publish.yml` has no environment/approval gate; broad triggers | `.github/workflows/publish.yml` |
| 9 | Info | `assert` used for runtime type validation (stripped under `-O`) | `cli.py`, `demo.py` |
| 10 | Info | Unvalidated `region`/`instance_type` interpolated into request URLs | `cloud.py`, `cloud_gcp.py` |
| 11 | Info | Broad `except Exception: pass` swallows errors | `demo.py` |
| 12 | Info | Stale security documentation / heavy default dependencies | `SECURITY.md`, `pyproject.toml` |

---

## Detailed findings

### 1. Unbounded compute & unhandled exception in demo workload server — Medium

**Location:** `demo/app.py:32-45` (and the embedded copy in `src/.../demo.py:_APP_PY`)

```python
n = int(params.get("n", ["200000"])[0])
...
pi = monte_carlo_pi(n)   # runs a Python loop n times
```

The `/compute` endpoint reads `n` straight from the query string with **no
upper bound**. `monte_carlo_pi(n)` then executes a `range(n)` loop, so a request
such as `GET /compute?n=100000000000` pins a CPU core for a very long time —
a trivial, unauthenticated **CPU/availability DoS** against the demo container.

Additionally, a non-numeric value (`?n=abc`) raises `ValueError` inside
`do_GET`, which is not caught. The request handler aborts without sending a
response, so malformed input degrades availability and the behaviour is
inconsistent with the 404 path.

The server also binds `0.0.0.0` (`# noqa: S104`), so in any environment where
the published port is exposed beyond localhost the workload is reachable.

**Impact:** Denial of service on the demo workload. This is "only" the demo,
but it is the one network-exposed component in the repo and is the image that
`pat demo` builds and runs.

**Recommendation:**
- Clamp `n` to a sane maximum (e.g. `min(int(...), 5_000_000)`), and reject
  non-positive values.
- Wrap parsing in `try/except ValueError` and return HTTP 400 with a short
  body instead of letting the handler raise.
- Keep the `0.0.0.0` bind only inside the throwaway demo container; document
  that the image must not be exposed publicly.

---

### 2. CI workflow runs without explicit least-privilege permissions — Medium

**Location:** `.github/workflows/ci.yml` (no top-level or job-level `permissions:`)

`codeql.yml` and `publish.yml` both declare scoped `permissions:` blocks, but
**`ci.yml` declares none**. It therefore inherits the repository/organisation
default `GITHUB_TOKEN` scope, which can be read/write. `ci.yml` triggers on
`pull_request` and then executes project code (`uv pip install -e ".[dev]"`,
`pytest`, `ruff`). Combined with a write-capable token this is a classic
supply-chain exposure: code from a pull request runs in a job whose token may
be able to mutate the repository.

The git history even contains a commit titled *"ci: add contents: read
permission so checkout can access the repo"*, suggesting permissions were
tightened elsewhere but not on `ci.yml`.

**Recommendation:** Add an explicit least-privilege block to `ci.yml`:

```yaml
permissions:
  contents: read
```

Set any broader scope per-job only where strictly required.

---

### 3. GitHub Actions pinned to mutable tags rather than commit SHAs — Medium

**Location:** all of `.github/workflows/*.yml`

Actions are referenced by mutable major-version tags:
`actions/checkout@v4`, `actions/setup-python@v5`, `astral-sh/setup-uv@v3`,
`codecov/codecov-action@v4`, `github/codeql-action/*@v4`. A tag can be moved by
the upstream owner (or an attacker who compromises it) to point at malicious
code, which would then run in CI with whatever token scope is available
(see finding #2).

This is notable because the project's own SDLC explicitly advertises a
**pinned-dependency** supply-chain control (see `SECURITY.md` and the
`lock-drift` CI job), yet that rigor is not applied to third-party Actions.

**Recommendation:** Pin every third-party action to a full commit SHA with a
trailing version comment, and let Dependabot (already configured for
`github-actions`) bump the SHAs:

```yaml
uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11 # v4.1.1
```

---

### 4. OData `$filter` injection and unvalidated price match (Azure) — Low

**Location:** `cloud_azure.py:54-93`

The Azure retail-price query is built by string interpolation of user-supplied
values into an OData `$filter`:

```python
filter_str = (
    f"serviceName eq 'Virtual Machines' "
    f"and armRegionName eq '{region}' "
    f"and skuName eq '{query_sku}'"
)
```

A `--sku-name` (or `--region`) containing a single quote breaks out of the
literal and can alter the filter clause (OData filter injection). Because the
endpoint is public and unauthenticated, the security impact is limited to
returning incorrect/attacker-shaped pricing data rather than data exfiltration.

Compounding this, `_parse_azure_price` returns the **first** `Consumption`
item with `price > 0` **without verifying** it matches the requested `skuName`
/ `armRegionName`. If the (injected or broadened) query returns multiple SKUs,
the tool silently reports a price for the wrong instance — a correctness and
trust issue feeding downstream cost recommendations.

**Recommendation:**
- Validate/escape `region` and `sku_name` (allowlist region codes; reject or
  percent-handle quotes). At minimum reject values containing `'`.
- In `_parse_azure_price`, confirm the returned item's `armSkuName`/`skuName`
  and `armRegionName` match the request before accepting the price.

---

### 5. Demo container runs as root with an unpinned base image — Low

**Location:** `demo/Dockerfile`, and embedded `_DOCKERFILE` in `demo.py`

```dockerfile
FROM python:3.12-slim
...
CMD ["python", "-u", "app.py"]
```

- No `USER` directive — the workload runs as **root** inside the container,
  violating least-privilege and increasing blast radius if the process is
  compromised (see finding #1).
- `python:3.12-slim` is a **mutable tag**; builds are not reproducible and
  could silently pull a different/compromised base image. No digest pin.
- No `HEALTHCHECK` and no dependency minimisation.

**Recommendation:** Add a non-root `USER`, pin the base image by digest
(`python:3.12-slim@sha256:...`), and add a `HEALTHCHECK` hitting `/health`.

---

### 6. Untrusted / unofficial pricing endpoint feeds cost decisions (GCP) — Low

**Location:** `cloud_gcp.py:38-40`

```python
_GCP_PRICELIST_URL = (
    "https://cloudpricingcalculator.appspot.com/static/data/pricelist.json"
)
```

This is an **unofficial, third-party** data source (acknowledged in the module
docstring). It is not an authoritative Google endpoint, can change format or
be taken down without notice, and its contents are trusted verbatim to drive
cost/ROI recommendations. There is no schema validation or signature check on
the downloaded JSON.

**Recommendation:** Prefer an official Google Cloud Billing Catalog API source,
or clearly label GCP figures as best-effort/unofficial in output. Validate the
parsed structure before use.

---

### 7. Local pricing cache trusted without integrity controls — Low

**Location:** `cloud.py:89-120`

`~/.pat/pricing-cache.json` is read and written with default umask
permissions, and `_load_cache()` trusts whatever JSON it finds (falling back to
`{}` only on parse error). A local attacker (or a shared-home environment) who
can write the cache file can poison the prices the tool reports, including via
the **stale-cache fallback** that is used silently whenever a network fetch
fails. Cached values are floats, so there is no code-execution path, but the
output integrity guarantee is weak.

**Recommendation:** Create the cache directory/file with restrictive perms
(`0o600` / `0o700`), validate cached entries (numeric, sane range, expected
keys), and surface when a stale cache value is being used rather than silently
substituting it.

---

### 8. Publish workflow lacks an approval gate and uses broad triggers — Low

**Location:** `.github/workflows/publish.yml`

The workflow correctly uses OIDC trusted publishing (`id-token: write`,
`contents: read`), which is good. However:

- It triggers on both `release: published` **and** `workflow_dispatch`, so any
  actor with write access can manually publish to PyPI at will.
- There is no GitHub **`environment:`** protection rule (required reviewers /
  wait timer) gating the publish job.

**Recommendation:** Bind the publish job to a protected `environment` (e.g.
`pypi`) with required reviewers, and consider restricting `workflow_dispatch`
or limiting publishing to tagged releases only.

---

### 9. `assert` used for runtime validation — Info

**Location:** `cli.py:175, 698, 748`; `demo.py:261, 279`

Several runtime guards use `assert isinstance(...)`. Python's `assert`
statements are **removed when the interpreter runs with `-O`** (CWE-617), so
these checks silently disappear in optimised deployments. Here they guard
internal invariants rather than untrusted input, so impact is low, but they
should not be relied on for validation.

**Recommendation:** Replace validation asserts with explicit `if ... raise`
checks (the `# noqa: S101` markers indicate Bandit already flags these).

---

### 10. Unvalidated path/host components interpolated into request URLs — Info

**Location:** `cloud.py:37-40` (`_EC2_CSV_URL.format(region=region)`),
`cloud_gcp.py`, `cloud_azure.py`

`region` and `instance_type` are interpolated into request URLs/paths without
validation or encoding. The target **host is hard-coded** to the provider's
domain, and the requests are made by the user's own CLI on their own behalf, so
this is not a cross-trust-boundary SSRF. It is noted because a malformed
`--region` could still produce surprising path traversal within the provider
host or confusing errors.

**Recommendation:** Validate `region` against the known
`_REGION_TO_LOCATION` allowlist (already present) before use, and URL-encode
interpolated path segments.

---

### 11. Broad exception swallowing — Info

**Location:** `demo.py` (`_cleanup`, container teardown, network removal,
`_cpu_sampler`) and `cloud.py`/`cloud_*.py` `except Exception` fallbacks.

Multiple `except Exception: pass` / `# noqa: BLE001, S110` blocks hide all
errors during cleanup and metric sampling. While acceptable in best-effort
teardown paths, blanket suppression can mask genuine failures (e.g. containers
left running, leaking resources).

**Recommendation:** Narrow the caught exception types and log at debug level
rather than silently passing.

---

### 12. Documentation drift & heavy default dependencies — Info

- `SECURITY.md` still headlines **"Known Limitations (v0.4.0)"** while the
  package is at `0.6.0`; keep the security policy version-current so users can
  trust it.
- The core CLI declares `docker>=6.0.0` and `matplotlib>=3.7.0` as **required**
  runtime dependencies even though only the `demo`/plotting paths need them.
  This enlarges the dependency attack surface and CVE exposure for users who
  only run analysis commands. Consider moving them to optional extras
  (e.g. `[demo]`, `[plot]`).

---

## Positive observations

- **No secrets** committed; no `eval`/`exec`, `pickle`, `yaml.load`, or
  `shell=True` anywhere in the tree.
- `subprocess.run` (in `run_dependency_audit`) uses the **argument-list form
  with no shell** and a timeout — safe invocation.
- Genuine input sanitization with bounds/type checks
  (`sanitize_requests_per_second`, `sanitize_latency_ms`, `sanitize_layer`)
  plus a dedicated `test_security.py` suite.
- Structured audit logging deliberately avoids echoing raw user strings and
  truncates context keys.
- Supply-chain controls already in place: Dependabot (pip + actions), CodeQL
  (`security-extended`), a `lock-drift` job enforcing `uv.lock`, and an on-run
  `pip-audit` check.
- TLS certificate validation is left at Python defaults (enabled) for all
  outbound `urlopen` calls.

---

## Prioritised remediation plan

1. **Now (Medium):** Clamp/validate `n` and handle `ValueError` in
   `demo/app.py` (#1). Add `permissions: contents: read` to `ci.yml` (#2).
2. **Short term (Medium/Low):** Pin Actions to commit SHAs (#3); add a
   protected environment to `publish.yml` (#8); run the demo container as a
   non-root user with a pinned base image (#5).
3. **Hardening (Low/Info):** Escape/validate Azure filter inputs and verify the
   matched SKU (#4); validate cache entries and tighten cache file permissions
   (#7); validate `region` against the existing allowlist (#10); replace
   validation `assert`s (#9); refresh `SECURITY.md` and slim default
   dependencies (#12).

---

*This report reflects a point-in-time manual review and does not replace
automated SAST/DAST or a penetration test.*
