# Architecture

This document describes the high-level design of `presidio-hardened-arch-translucency`: its
components, how data flows through them, and the trust boundaries the project is
built to enforce. For the security requirements and threat model that motivate
this design, see [SECURITY.md](SECURITY.md) and the assurance case in
[ASSURANCE.md](ASSURANCE.md).

## Overview

`presidio-hardened-arch-translucency` is a single-operator command-line tool
(`pat`) and Python library that recommends the optimal replication *layer*
(container / pod / deployment / node) for a Docker/Kubernetes workload and models
its cost, carbon, and energy. It is largely stateless: the only persistence is a
local, hash-chained SQLite observation store under the operator's config
directory; it holds no cluster credentials. It makes outbound HTTPS calls — to
read-only cloud pricing and carbon-intensity endpoints and to an
operator-supplied Prometheus/OTLP endpoint — using the standard-library `urllib`
with the default TLS certificate verification. Its central design stance: **it
advises but never actuates** — it emits reviewable Kubernetes manifests (HPA /
KEDA / recording rules) to stdout for a human or GitOps pipeline to apply, and it
never signs or emits an energy figure it did not measure (fail-closed; the E1a
invariant).

## Components

| Component | Responsibility |
|---|---|
| `observe.py`, `daemon.py` | Record and read workload observations into the hash-chained local store; maintain the parallel measured-energy chain. |
| `prometheus.py`, `otlp.py`, `pushgateway.py` | Read telemetry from, and export metrics to, monitoring backends. `urllib` only, URL scheme/host validated. |
| `calibrate.py`, `train_calibrate.py`, `training.py` | Fit the analytical model (α/β replication overhead) to measured operating points. |
| `model.py`, `energy.py`, `carbon.py`, `cost.py`, `budget.py` | The translucency model and its energy / carbon / cost / budget extensions. |
| `optimize.py`, `rules.py` | Turn observed history plus a forecast into a scaling recommendation and Prometheus recording/alerting rules. |
| `scaler.py`, `hpa.py`, `hpa_patch.py` | Emit apply-able manifests (KEDA `ScaledObject`, HPA, prometheus-adapter). **Output only — never applies anything to a cluster.** |
| `cloud.py`, `cloud_azure.py`, `cloud_gcp.py` | Read-only cloud pricing lookups (no credentials, except AWS spot pricing via env vars). |
| `evidence_producer.py` | Canonical-JSON + SHA-256 + detached Ed25519 / HMAC-SHA256 signing of degradation / energy / training evidence; key-less by default; signs only measured values. |
| `security.py` | Input sanitisation, secure logging (secret redaction), on-run `pip-audit`. |
| `cli.py`, `__main__.py`, `export.py`, `annotate.py` | The `pat` CLI surface and its emit/annotate subcommands. |

Dependency order (primitives outward): telemetry (`observe` / `prometheus` /
`otlp`) → model (`calibrate` / `energy` / `carbon` / `cost` / `budget`) →
decision (`optimize` / `rules`) → emission (`scaler` / `hpa` / `hpa_patch`) →
evidence (`evidence_producer`), with `security.py` cross-cutting all of them.

## Data / processing flow

A typical `pat observe` / `pat optimize` run moves through the components as a
pipeline:

1. **Validate input** — `security.py` bounds- and type-checks every workload
   number (rejecting `nan`/`inf`/`bool`), allowlists the layer name, and
   RFC 1123-validates any Kubernetes name. Any failing control raises and stops
   the run (**fails closed**).
2. **Gather telemetry** — from the operator-supplied Prometheus/OTLP endpoint or
   the local observation store. Endpoint URLs are scheme/host-validated and
   fetched over TLS with certificate verification; a remote failure falls back to
   a local static snapshot rather than proceeding blind.
3. **Fit / apply the model** — `calibrate`/`model`/`energy` compute the
   layer-aware recommendation.
4. **Produce the recommendation** — `optimize`/`rules`.
5. **Emit** — `scaler`/`hpa`/`hpa_patch` write a reviewable manifest to stdout.
   Nothing is applied to any cluster.

For evidence, `evidence_producer` derives a reading *only* from the measured
chain, gated on a clean hash-chain walk (it refuses to emit if the chain is
broken), and emits it **key-less**; a separate signing sidecar adds the
signature. Load-bearing ordering that is part of the contract: input validation
precedes all use; evidence emission is gated on chain integrity; and `pat` never
emits a figure it did not measure (E1a).

## Trust boundaries

| Boundary | Kind | Control |
|---|---|---|
| **CLI / caller args → pat** | input-validation | `security.py` sanitises rps / latency / domain numbers (rejects `nan`/`inf`/`bool`); `sanitize_layer` allowlists the layer; `hpa_patch` validates names against RFC 1123. |
| **Telemetry / cloud / carbon endpoint → pat** | input-validation + egress | URL scheme validated (`http`/`https` only, host required); a Prometheus bearer token is **refused unless the URL is https** (`prometheus.py`); Prometheus URLs with embedded credentials are rejected (`scaler.py`); the live carbon fetch refuses redirects (`carbon.py`). TLS certificate verification is the `urllib` default — there is no `verify=False` anywhere. On remote failure, a local static snapshot is used. |
| **pat → emitted manifest** | egress | Only integers and already-validated names are interpolated; no secrets or raw input are echoed; the manifest is advisory and applied out-of-band by a human / GitOps. |
| **pat → evidence record** | egress | Canonical JSON over measured values only; signing-key custody is delegated to the sidecar by contract — `pat` holds no signing key by default. |
| **Environment → cloud credentials** | secret handling | Only AWS spot pricing reads `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from the environment; credentials are never hardcoded and are redacted from logs by `security.py`. |
