# ADR-0008: Package the exporter as a static Helm chart, emit-only and RBAC-free

- **Status:** Accepted (2026-06-18). Governs v0.16.0 ("Package & operate"), the
  final step of the monitoring-integration arc.
- **Deciders:** Vladimir Stantchev (maintainer)
- **Related:** PRESIDIO-REQ.md "Monitoring Integration Arc" (arc invariant A1,
  "pat emits, it never applies"); the static `grafana/` provisioning bundle
  (v0.12.0); [ADR-0006](0006-otlp-export-transport.md) /
  [ADR-0007](0007-prometheus-remote-write.md) (dependency-vs-posture tension).

## Context

The arc's seventh step packages the read-only `pat export` endpoint and its
declarative monitoring artifacts (recording/alerting rules, the Grafana
dashboard) as a single cluster-native, installable bundle so `pat` is deployable
as a monitoring component, not only a CLI.

Several forces shape how:

1. **Emit-only is the arc's security spine (A1).** Nothing the project ships may
   hold cluster write credentials or apply changes itself. A packaging artifact
   must not smuggle in a privileged agent.
2. **The exporter needs no Kubernetes API access.** `pat export` computes the
   recommendation from CLI args and serves `/metrics`; it never reads or writes
   cluster state.
3. **`pat rules` output embeds Prometheus's own Go templating** (`{{ $value }}`
   in alert annotations), which collides with Helm's templating.
4. **Target clusters vary.** ServiceMonitor/PrometheusRule are Prometheus
   Operator CRDs; the dashboard ConfigMap assumes a Grafana sidecar; NetworkPolicy
   assumes a controller. A chart that hard-requires these fails on plain clusters.
5. **Prior art in the repo** packages static assets (the `grafana/` bundle), not
   a CLI emitter, for things with no per-invocation logic.

## Decision

We will ship **`charts/pat-exporter`, a static Helm chart** (not a `pat chart`
CLI emitter — charts carry no per-invocation logic, so they live as files,
mirroring `grafana/`), plus a root `Dockerfile` for the exporter image.

- **RBAC-free, token-free.** The chart creates **no** Role/ClusterRole/binding
  and sets `automountServiceAccountToken: false`. Least privilege for a workload
  that needs zero cluster permissions is *zero* permissions.
- **Hardened by default.** Pod `runAsNonRoot` + non-root UID + `seccompProfile:
  RuntimeDefault`; container `readOnlyRootFilesystem`, no privilege escalation,
  all capabilities dropped.
- **Rules injected with `.Files.Get`, not templated.** The verbatim `pat rules`
  output lives in `files/pat-rules.yaml` and is inserted with `.Files.Get` (which
  does not re-template), so Prometheus's `{{ $value }}` annotations survive Helm
  rendering. A test keeps the bundled copy in exact sync with `pat rules`.
- **Operator/sidecar objects default off.** ServiceMonitor, PrometheusRule, the
  dashboard ConfigMap, and NetworkPolicy are opt-in so a bare `helm install`
  works on any cluster; enable the ones a given stack supports.
- **Emit-only preserved.** The chart deploys the read-only exporter and emits
  declarative objects; it applies nothing and the exporter has no mutation path.

## Consequences

- **Easier:** one-command deploy of the whole monitoring surface; the chart is
  universally installable (CRDs/sidecar optional); the hardened posture is the
  default, not an add-on; bundled artifacts cannot drift (sync tests).
- **Harder / trade-offs:**
  - CI has no Helm binary, so render correctness is guarded by exact-sync +
    security-invariant string tests, with a full `helm lint`/`helm template`
    render gated on Helm being present (developer machines). Adding Helm to CI
    would strengthen this and is a possible follow-on.
  - The container image carries the full package (incl. scipy/statsmodels); an
    extras-slimmed image is a possible follow-on.
  - Publishing the image to a registry is out of band (the Dockerfile builds it;
    a CI publish job is future work).
- **Deferred:** the *optional* native Grafana panel plugin (the shipped
  dashboard already covers the visual surface — consistent with the "Grafana
  instead of a dedicated GUI" decision); and, from ADR-0007, a hand-rolled
  remote-write target.
