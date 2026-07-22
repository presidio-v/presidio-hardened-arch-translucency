# Stability & semver guarantees — presidio-hardened-arch-translucency

For downstream integrators depending on this project.

## What is the public API

The public API is the **`pat` command-line interface** — the `pat` console
script (`presidio_arch_translucency.cli:app`) and its documented commands and
flags, as listed in the README's CLI Reference and each command's `--help`. The
package exposes `__version__` and `__author__` from
`presidio_arch_translucency.__init__`; there is deliberately **no** curated
`__all__`, so importable module internals are *not* part of the public API — with
one exception: the `evidence_producer` helpers used by the signing sidecar
(`observation_to_evidence`, the `build_*_reading` constructors, `sign_evidence`,
`canonical_bytes`, `sha256_hex`) together with the wire ids and
`SIGNING_ALGORITHMS`, whose canonical byte-output is a stability contract (see
below). Everything else — module internals and underscore-prefixed names — is
internal and may change without notice.

## Versioning rules (semver, pre-1.0 profile)

- **Patch (0.x.Y):** bug fixes, security fixes, dependency floor bumps. No API
  change, no behaviour change except the fixed defect. Safe to auto-upgrade; this
  is the channel security releases ship on.
- **Minor (0.X.0):** additive API (new exports, new optional parameters with
  defaults, new optional extras). Existing code keeps working, including the
  documented public behaviour. Deprecations are announced here (docstring +
  CHANGELOG) at least one minor before any change.
- **Major (1.0.0+):** the only place deprecated surface may be removed.

**Pin guidance for integrators:** pin `presidio_arch_translucency` to the current minor
in production and run the verification step (below) in your CI on every upgrade.

## Behavioural guarantees (stronger than API stability)

These are security invariants, not just interfaces; weakening any of them is
treated as a breaking change regardless of which version component moves.

- **Fail-closed on malformed input** — the input sanitisers raise rather than
  proceed on out-of-range, non-finite, or wrong-type workload values.
- **Never actuates** — `pat` emits reviewable manifests to stdout and never
  applies anything to a cluster; it holds no kube credentials.
- **Never emits an unmeasured figure (E1a)** — energy/evidence readings are
  derived only from the measured store; there is no override for the figures.
- **Evidence integrity** — emission is refused on a broken observation hash chain;
  `pat observe verify` reports tampering with distinct exit codes.
- **Byte-stable evidence output** — the canonical-JSON encoding of an evidence
  record for given input is stable within a wire-id major version (the encoder
  rejects floats to keep this deterministic).
- **No token over cleartext** — a Prometheus bearer token is never sent over a
  non-TLS URL.
- **Secrets never logged** — secret-like context keys are redacted before any log
  line is emitted.

## Verifying an installation

There is no single bundled self-test command. To confirm an installation:

- `pat --version` prints the installed version and exits `0` — confirms the
  console entry point resolves.
- `pat observe verify` walks the observation hash chain and returns a distinct
  non-zero exit code on tampering — confirms the evidence-integrity guarantee
  against a populated store.
- Running the test suite (`pip install -e ".[dev]" && pytest`) exercises the
  guarantees end-to-end; a clean run (exit `0`, coverage floor met) is the
  recommended pre-upgrade smoke test in a consumer's CI.

## Schema/wire stability

The project emits versioned evidence records — `evidence-ref@1`,
`slo-reading@1`, `training-run@1`, and `energy-reading@1` — whose version is
carried in the `@1` wire-id suffix. Within a wire-id major version, fields are
**additive-only**; the canonical-JSON encoding and the resulting content hash are
byte-stable and pinned to the merged `presidio-evidence` family conformance
vectors. Forward/backward compatibility is handled by bumping the wire-id suffix
(never by silently reshaping an existing record). The emitted Kubernetes
manifests (HPA, KEDA `ScaledObject`, prometheus-adapter) track the upstream
Kubernetes API versions they target (`autoscaling/v2`, `apps/v1`).

## Security response

See [SECURITY.md](SECURITY.md). Security fixes ship as patch releases on the
latest minor; any minimum-safe dependency floors are bumped in the same release.
