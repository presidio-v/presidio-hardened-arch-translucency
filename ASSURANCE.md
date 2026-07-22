# Security Assurance Case

This document is the assurance case for `presidio-hardened-arch-translucency`: an explicit argument
for why the project's security requirements are met. It has four parts, as
required by the OpenSSF Best Practices silver criterion `assurance_case`:

1. the threat model,
2. the trust boundaries,
3. the argument that secure design principles are applied, and
4. the argument that common implementation weaknesses are countered.

It is a summary that links to the authoritative detail in
[`SECURITY.md`](SECURITY.md) (controls, per-version threat tables, reporting) and
[`ARCHITECTURE.md`](ARCHITECTURE.md) (components, flow, boundaries) for
`presidio-v/presidio-hardened-arch-translucency`.

## 1. Threat model

**Assets.** (a) The integrity of the scaling recommendation and the manifest it
emits — a wrong layer or replica count wastes spend or breaches an SLO. (b) The
integrity of the measured-energy / degradation evidence chain — downstream
consumers (e.g. `presidio-hardened-x402`) act on it. (c) The confidentiality of
any cloud credentials or Prometheus bearer token in the operator's environment.

| Adversary / threat | Mitigating control |
|---|---|
| Malicious or malformed telemetry driving a bad scaling decision | Input sanitisation (finite/bounds checks, layer allowlist), scheme-validated telemetry sources, model-fit guards. The tool **advises only** — a bad recommendation cannot self-apply. |
| Unauthorised cluster mutation | By design `pat` emits manifests to stdout and holds no kube credentials; application is out-of-band and human-reviewed. |
| Evidence tampering / repudiation | Hash-chained observation store; `pat observe verify` with strict tamper exit codes; evidence emission gated on a clean chain walk (fails closed); detached signatures over canonical JSON; `pat` signs only measured values (E1a). |
| Spoofed / misconfigured energy reading | Key-less unsigned readings plus refusal to emit unmeasured figures; downstream verifies the (sidecar-applied) signature before acting. |
| Cloud-credential / bearer-token exposure | Credentials only from env vars (never hardcoded); secure logging redacts secret-like keys; bearer token refused over non-TLS Prometheus. |
| SSRF / redirect to an internal host via an operator-supplied endpoint | URL scheme + host validation, no-redirect handler on the live carbon fetch, TLS certificate verification. |

**Out of scope (documented, not silently assumed):** signing-key custody
(delegated to the signing sidecar / operator by contract); the security of the
Kubernetes cluster and of the `kubectl` / GitOps path that applies emitted
manifests; the correctness of third-party pricing / carbon data; and the recall
limits of the analytical model itself.

## 2. Trust boundaries

These mirror [`ARCHITECTURE.md`](ARCHITECTURE.md#trust-boundaries); names are kept
identical on purpose.

- **CLI / caller args → pat** (input-validation): the primary validation boundary
  — `security.py` sanitisers, the layer allowlist, and RFC 1123 name validation.
- **Telemetry / cloud / carbon endpoint → pat** (input-validation + egress): URL
  scheme/host validation, bearer token refused over non-TLS, no-redirect on the
  live fetch, TLS certificate verification, local static-snapshot backstop on
  remote failure.
- **pat → emitted manifest** (egress): only ints and validated names crossed;
  advisory output, applied out-of-band under human review.
- **pat → evidence record** (egress): canonical JSON over measured values only;
  key custody delegated to the sidecar by contract — `pat` holds no key by
  default.
- **Environment → cloud credentials** (secret handling): env-only, never
  hardcoded, redacted from logs.

## 3. Secure design principles applied

- **Fail-safe defaults / secure by default.** Input validation and the on-run
  `pip-audit` check are on by default; evidence emission fails closed on a broken
  hash chain; `pat` never actuates a cluster; newer controls (energy-evidence
  signing) are opt-in and do not weaken existing defaults.
- **Complete mediation.** Every workload number passes sanitisation before use;
  every telemetry URL is scheme/host-checked; every emitted Kubernetes name is
  RFC 1123-validated; every evidence emission is gated on chain integrity.
- **Least privilege.** The project holds no cluster credentials and no signing key
  by default; AWS credentials are read from the environment only when spot pricing
  is explicitly requested; telemetry access is read-only.
- **Defense in depth.** Independent controls — input bounds, URL/scheme checks,
  no-redirect handling, TLS certificate verification, secret redaction in logs,
  the hash-chained evidence store, and CI SAST — each cover a distinct class.
- **Economy of mechanism.** Cryptography is stdlib `hashlib`/`hmac` plus the
  vetted `cryptography` Ed25519 primitive — no bespoke crypto; manifests are built
  as hand-written strings with no YAML-parser dependency; HTTP uses `urllib` only,
  with no third-party client library, keeping the attack surface small.

## 4. Common implementation weaknesses countered

| Weakness class | How it is countered |
|---|---|
| Improper input validation / injection (CWE-20, CWE-74) | `security.py` sanitisers, RFC 1123 name validation, URL scheme validation; the one subprocess call (`pip-audit`) uses a list argv, never `shell=True`. |
| Memory safety (CWE-119 family) | Python is memory-safe; not applicable at the language level. |
| Cryptographic misuse / weak hash (CWE-327, CWE-916) | SHA-256 + Ed25519 / HMAC-SHA256 via stdlib and `cryptography`; the canonical encoding rejects floats; no MD5/SHA-1 used for security. |
| Hard-coded / exposed secrets (CWE-798, CWE-532) | No hardcoded secrets; credentials read from env vars; secure logging redacts secret-like keys before emission. |
| Cleartext transport / missing certificate validation (CWE-319, CWE-295) | HTTPS endpoints fetched with the default `urllib` TLS certificate verification; a bearer token is refused over non-TLS; no `verify=False`. |
| Server-side request forgery (CWE-918) | URL scheme + host validation and a no-redirect handler on the live fetch. Residual risk (operator-supplied endpoints are trusted operator input) is documented, not hidden. |
| Unsafe deserialization (CWE-502) | JSON only — no `pickle` and no `yaml.load` of untrusted input; emitted manifests are hand-built strings, never parsed back. |
| Vulnerable dependencies (CWE-1104) | Dependabot, a `pip-audit` release gate in CI, and a `uv.lock` drift check. |

These classes are checked continuously by **CodeQL** (`security-extended` +
`security-and-quality`), **bandit via the ruff `S` rule set** (enabled in
`pyproject.toml`, `S101` ignored only for test asserts), and **OpenSSF
Scorecard**, on every push and pull request. In addition, the project has
undergone periodic third-party security audits, recorded in the repository's
`SECURITY-AUDIT-*.md` files (several findings from the v0.18.0 audit are cited in
the source and have been remediated).

## Conclusion

The threats above are each matched to a control; the controls sit at explicit
trust boundaries; the design follows fail-safe, least-privilege, complete-
mediation, defense-in-depth, and economy-of-mechanism principles; and the common
implementation weakness classes are countered by design and checked by automated
analysis. The project's stated security requirements are therefore met, subject
to the documented out-of-scope assumptions.
