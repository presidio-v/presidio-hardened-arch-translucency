# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| main / 0.24.x | :white_check_mark: |
| 0.23.x  | :white_check_mark: |
| 0.22.x  | :white_check_mark: |
| 0.21.x  | :white_check_mark: |
| 0.20.x  | :white_check_mark: |
| 0.19.x  | :white_check_mark: |
| 0.17.x  | :white_check_mark: |
| 0.8.x   | :white_check_mark: |
| 0.7.x   | :white_check_mark: |
| 0.6.x   | :white_check_mark: |
| < 0.6   | :x:                |

## Reporting a Vulnerability

Please report security vulnerabilities by opening a private GitHub Security Advisory
(via the "Security" tab -> "Report a vulnerability") rather than a public issue.

Include:

- Description of the vulnerability
- Steps to reproduce
- Potential impact
- Suggested fix (if any)

You will receive an acknowledgement within 5 business days. We aim to release a patch
within 30 days of a confirmed vulnerability.

## Security Features

This toolkit ships with the following Presidio security hardening:

| Feature | Description |
|---|---|
| **Input sanitization** | CLI parameters and scheduler inputs are bounds-checked and type-validated before use |
| **Env-only telemetry tokens** | `PAT_PROMETHEUS_TOKEN`, `PAT_GRAFANA_TOKEN`, `PAT_OTLP_TOKEN`, and `PAT_PUSHGATEWAY_TOKEN` are env-only, never logged, reject control characters, and require HTTPS when sent as bearer auth |
| **Private local stores** | Default pricing cache, observation store, calibrated model file, and daemon unit files are owner-only where the platform supports chmod |
| **Demo isolation** | `pat demo` publishes Docker ports to `127.0.0.1` only and runs the embedded workload as an unprivileged user |
| **Secure logging** | Recommendations and audit events redact token-, secret-, password-, key-, credential-, and auth-shaped context fields |
| **CVE/dependency audit** | `pip-audit` check runs on normal command execution (skippable via `--skip-audit`; help/version exits skip network audit) |
| **Security event logging** | Structured audit log entry emitted for every recommendation |
| **Output sanitization** | Rich markup prevents injection via user-supplied layer names |
| **Dependabot** | Automated dependency updates configured in `.github/dependabot.yml` |
| **CodeQL** | Static analysis via `.github/workflows/codeql.yml` |
| **Key-less evidence producer (v0.17.0)** | `evidence_producer` signs runtime-posture degradation evidence, but the `pat` runtime holds **no signing key** — the Ed25519 key lives in a separate signing-bridge sidecar (consumers hold only the public key), preserving the read-only "no secrets to steal" posture |

## Dependency Security

Dependencies are pinned in `uv.lock` and monitored via:

- GitHub Dependabot (automated PRs for updates)
- `pip-audit` on normal CLI command execution
- CI `pip-audit` against both `[audit]` and `[evidence]`, so the optional
  Ed25519 dependency is covered by the release gate
- CodeQL static analysis on every push and weekly schedule
- `lock-drift` CI to ensure `pyproject.toml` and `uv.lock` stay aligned

## Evidence Trust Boundary (v0.17.x / v0.19.x)

`pat evidence-emit` reads either an explicit `--p99-latency-ms` value or the
latest local observation from `~/.pat/observations.db`, then emits an unsigned
Layer-0 reading. The signing-bridge sidecar is expected to run on the same
trusted host boundary, recompute the `content_hash`, and hold the Ed25519 key.
Local observation integrity therefore depends on the host and filesystem
permissions. The default store is created owner-only (`~/.pat` at `0700`,
`observations.db` at `0600`) and this is covered by regression tests.

**`training-run@1` (v0.18.0).** `pat train-evidence-emit` emits an unsigned
Layer-0 training-run record under the same key-less model: `pat` holds no
signing key; the sidecar recomputes the `content_hash` before signing. The
record's inputs come from CLI arguments (not a local store), so the trust
boundary is the invoking process: `run_id` is bounded (≤512 chars) and
control-character free, numeric fields must be finite non-negative integers,
and `parents` / `model_hash` / `dataset_hash` must be lowercase-hex content
hashes — all enforced fail-closed **in the library**
(`build_training_run_reading`), so an alternative caller (sidecar, script)
cannot construct malformed signable content. `parents` entries are *claims*
by the producer; resolving and verifying the referenced payloads is the
consumer's responsibility (presidio-evidence ADR-0002 P4). The security log
records a SHA-256 digest of `run_id`, never the raw value.

**Measured energy (v0.21.0).** `pat observe --energy` records watts scraped
from Prometheus into a second hash chain (`energy_observations`), under
corollary E1a: *pat never signs a watt it did not measure*. The platform gate
is fail-closed — no gated series under the pinned direct-hardware metric names
(node-exporter RAPL or DCGM)
means nothing is written; estimator value-tells are refused (normalized); a
gate/watts **query override is permanently marked in the chained record**
(`source="prometheus-override"`), so an operator-supplied query can never
masquerade as a preset-attested measurement. Bounded claim: the gate proves a
power interface exists at gate time; it does not bind the watts sample to the
gated series, and the chain does not prove capture-time honesty (ADR-0010
bound). Kepler is rejected because its supported synthetic CPU meter and
workload attribution cannot be distinguished by metric/label shape from direct
measurement. The public persistence API requires a process-local collector seal;
this is an API capability, not remote attestation. Export and calibration verify
the full energy chain from a read-only SQLite snapshot before consuming rows.
The token/HTTPS discipline of the serving Prometheus source applies unchanged;
the strict meter enum has no `manual`/`analytic` member.

**Carbon & budget figures (v0.22.0).** `pat budget`, `cost --carbon`, and
`what-if --energy-aware` render **modelled estimates** — they never enter the
observation chains and never become evidence readings (E1/E1a). Grid carbon
intensity resolves live→cache→static: the live Electricity Maps path is
env-token-only (`PAT_CARBON_TOKEN`, HTTPS, never logged, never cached), the
cache (`~/.pat/carbon-cache.json`) is owner-only, and every intensity crossing
a trust boundary is bounds-validated (finite, `0 < v ≤ 2000 gCO₂eq/kWh`) —
poisoned cache entries or malformed live responses are refused and resolution
falls back to the cited static snapshot without failing the command. The
cache is written atomically and rejects symlinks, non-owner permissions,
wrong-owner files, oversized input, non-object JSON, and future timestamps.
Live responses are size-bounded and redirects are refused to prevent forwarding
the env-only token to another origin. The
static table is a documented annual-average snapshot (Ember CC-BY-4.0 /
Google region data, location-based methodology); treat carbon rankings as
placement guidance, not accounting-grade Scope 2 figures.

**Training energy (v0.23.0).** `pat train-calibrate` ingests caller-supplied
step-time logs under a fail-closed contract (size/line bounds, strict keys,
per-line numeric bounds, strictly increasing step ids, descriptor-bound reads,
symlinks and deep-nesting refused); fitted training records carry a
`training-calibration-commitment@1` digest and `train-analyze` /
`train-what-if` fail closed on tamper (legacy no-commitment sections keep
working, reported honestly). Fits require a degree-1 identifiability anchor;
model-file upserts are owner-only and atomic, and consumers bind each committed
record to its strategy map key. `training-run@1` optional `energy_wh` /
`mean_power_w` are **producer-measured claims** under the v0.18 trust
boundary: validated fail-closed in the library (floats/bools rejected by
type, values must round-trip IEEE-754 losslessly, ints and strings canonicalize
to the same string-decimal, negative zero collapses, and jointly supplied energy
and mean total run power must agree with duration — one value, one hash), attributed to the
producer like every other training-run field, never generated by pat.

**`energy-reading@1` (v0.24.0).** `pat energy-evidence-emit` emits an unsigned
Layer-0 measured-energy reading under the same key-less model (sidecar signs).
The figures come **exclusively** from the chained `energy_observations` store
— no CLI flag can supply an energy value (E1a); rows recorded under a query
override (`source="prometheus-override"`) refuse emission; the signed window
uses span-overlap closure so the refusal scans exactly the coverage the
window claims. Overlapping spans (double-counting) and gaps (unmeasured
coverage) are refused; energy and mean power must agree with elapsed time.
Emission is gated on a clean chain walk from an explicit transaction on a
**single read-only snapshot**, and the emitted `energy_chain_head` is the last
*verified* link by construction. Bounded claim, unchanged from ADR-0010:
external anchoring makes post-hoc rewriting externally detectable; it does
not prove readings were honest at capture time. The library builder
(`build_energy_reading`) validates wire shape only and trusts its caller for
measured provenance — the trust boundary sits at the CLI derivation, and
sidecar authors must not treat shape validation as measured-ness.

## Known Limitations (main / v0.24.x)

- The simulation model uses calibrated coefficients, not live telemetry.
  Production use should be validated against actual cluster metrics.
- The v0.20.0 energy model is analytic: per-layer α_E/β_E defaults are
  documented MVP placeholders (literature-derived, not measured), and
  `--replica-power-watts` / `--energy-observation` values are caller-supplied.
  All energy inputs are bounds-checked, and fitted energy parameters are bound
  by the calibration commitment (tampered coefficients fail closed; legacy
  records cannot supply energy coefficients; ADR-0011),
  but the *fidelity* of energy figures is only as good as the supplied watts —
  measured-mode integrity arrives with the v0.21 chained `energy_observations`
  store. Per invariant E1, pat never actuates power (no DVFS, no capping), so
  the energy surface adds no write path to infrastructure.
- `pip-audit` requires a network connection; it gracefully skips when offline.
- GCP pricing is sourced from an unofficial third-party pricelist endpoint and
  should be treated as a best-effort estimate.
- `pat demo` is for local demonstration only. It binds published ports to
  loopback, but the workload is CPU-bound and must not be exposed through a
  reverse proxy or public Docker host.
- Container images use a digest-pinned Python base, build the checked-out release
  source, and are published to GHCR with multi-architecture provenance.

Manual security audit history:

- v0.24.1 patch release gate (2026-07-18) -- authoritative nominal Kepler and
  energy-bearing training family-vector hashes independently verified; PAT's
  audited Kepler emission refusal remains fail-closed; no runtime trust-boundary
  change.
- v0.24.0 full functionality/security release gate (2026-07-18) -- all findings
  remediated: explicit SQLite read transaction, family cross-field consistency,
  overlap/gap refusal, strict RFC3339, validated snapshot rows, pipe-pure JSON,
  read-only head accessors, bounded closure complexity, merged family
  golden-vector pin, and synchronized release metadata.
- v0.23.0 full functionality/security release gate (2026-07-17) -- all findings
  remediated: identifiable/model-consistent fits, descriptor-bound bounded log
  ingestion, atomic scoped commitments, honest energy ranking, canonical and
  internally consistent energy evidence, legal-claim correction, and release
  metadata drift.
- v0.22.0 full functionality/security release gate (2026-07-16) -- all findings
  remediated: infeasible recommendations, commitment bypasses, ambiguous energy
  scaling, carbon-cache poisoning/permissions/atomicity, redirect and response
  bounds, invalid ranking values, and release metadata drift.
- v0.21.0 full functionality/security release gate (2026-07-16) -- all eleven
  third-party findings remediated: direct-hardware-only measurement, sealed
  collection API, read-only verified consumers, synchronized windows/lockfile,
  hardened Helm persistence, and attested container publication.
- v0.20.0 full functionality/security release gate (2026-07-14) -- all eight
  third-party findings remediated: locked dependency audit, Pillow 12.3.0,
  bounded/identifiable energy fits, committed-only energy coefficients, and
  strict commitment-schema validation.
- v0.19.1 release gate -- remediates the open CodeQL code-scanning alert set
  before publishing the patch release.
- [`SECURITY-AUDIT-2026-07-02-v0.18.0-remediation.md`](SECURITY-AUDIT-2026-07-02-v0.18.0-remediation.md) -- v0.18.0 third-party release audit (Codex, `presidio-third-party-audits`) remediation status.
- [`SECURITY-AUDIT-2026-06-21-v0.17.0.md`](SECURITY-AUDIT-2026-06-21-v0.17.0.md) -- v0.17.0 third-party release audit and remediation status.
- [`SECURITY-AUDIT-2026-06-17-v0.13.0.md`](SECURITY-AUDIT-2026-06-17-v0.13.0.md) -- v0.13.0 release-cut audit and remediation status.
- [`SECURITY-AUDIT-2026-06-17.md`](SECURITY-AUDIT-2026-06-17.md) -- v0.9.0 release-cut audit and remediation status.
- [`SECURITY-AUDIT-2026-06-16.md`](SECURITY-AUDIT-2026-06-16.md) -- v0.9.0 hardening audit and remediation status.
- [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md) -- 2026-06-03 audit and remediation status.

## Software Development Lifecycle

This repository is developed under the Presidio hardened-family SDLC. The public report
-- scope, standards mapping, threat-model gates, and supply-chain controls -- is at
<https://github.com/presidio-v/presidio-hardened-docs/blob/main/sdlc/sdlc-report.md>.
