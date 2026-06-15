# ADR-0003: Prometheus auth via env token only (v0.8.0)

* Status: accepted — extended by v0.9.0 kubeconfig auth (see Consequences)
* Date: 2026-06-10
* Decision ref: D3 (PRESIDIO-REQ.md, v0.8.0 Design Decisions)

## Context

`pat observe --prometheus <url>` scrapes a sample from the Prometheus HTTP API.
Most production Prometheus endpoints sit behind authentication. Two broad options
existed for v0.8.0: a simple bearer token supplied out-of-band, or kubeconfig-based
auth that resolves a token from the active Kubernetes context (which also implies
an optional `kubernetes` client dependency and significantly more surface area).

Credential handling is a security-sensitive concern: tokens must never land in
shell history, process listings, or logs.

## Decision

We will authenticate to Prometheus in v0.8.0 using a bearer token from the
`PAT_PROMETHEUS_TOKEN` environment variable only. Tokens are never accepted as CLI
arguments and never logged. kubeconfig-based auth and the optional `kubernetes`
dependency are deferred beyond v0.8.0.

## Consequences

- The smallest possible credential surface ships first: one env var, no new
  dependency, no token in argv.
- Clusters that front Prometheus behind the kube-apiserver proxy are not yet
  served by automatic auth in v0.8.0; users must extract a token manually.
- **v0.9.0 extension.** Kubeconfig bearer-token auth was added as a follow-on:
  when `PAT_PROMETHEUS_TOKEN` is unset, the token is resolved from the active
  kubeconfig context (resolution order env → kubeconfig → unauthenticated), parsed
  with a dependency-free YAML-subset reader (no `pyyaml`, no `kubernetes` client).
  The env-token-first rule and the never-logged/never-in-argv guarantees of this
  ADR are preserved.
