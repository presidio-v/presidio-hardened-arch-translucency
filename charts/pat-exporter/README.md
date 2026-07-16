# pat-exporter Helm chart

Cluster-native packaging of the read-only architectural-translucency exporter
(`pat export`). Chart 0.23.0 ships the exporter, measured/modelled energy panels,
and its declarative monitoring artifacts as one installable bundle.

**Emit-only.** The chart deploys a read-only `/metrics` endpoint and declarative
objects (ServiceMonitor, PrometheusRule, dashboard ConfigMap). The exporter
holds **no** Kubernetes API credentials (`automountServiceAccountToken: false`)
and applies nothing — preserving arc invariant **A1** ("pat emits, it never
applies").

## What it deploys

| Object | Default | Purpose |
|---|---|---|
| Deployment | on | Runs `pat export` serving read-only `/metrics` (hardened pod). |
| Service | on | ClusterIP exposing the `metrics` port. |
| ServiceAccount | on | No token mounted; the exporter needs no API access. |
| ServiceMonitor | off | Prometheus-Operator scrape target. |
| PrometheusRule | off | Recording + alerting rules (verbatim `pat rules`). |
| Dashboard ConfigMap | off | Official Grafana dashboard via the Grafana sidecar. |
| NetworkPolicy | off | Restricts ingress to the metrics port. |

The Operator-dependent objects (ServiceMonitor, PrometheusRule) and the sidecar
dashboard are **off by default** so the chart installs cleanly on any cluster;
enable the ones your stack supports.

## Install

```bash
# From a checkout of the repo:
helm install pat ./charts/pat-exporter \
  --namespace monitoring --create-namespace \
  --set workload.requestsPerSecond=500 \
  --set workload.avgLatencyMs=80 \
  --set workload.currentLayer=container

# With Prometheus Operator + Grafana sidecar:
helm install pat ./charts/pat-exporter -n monitoring \
  --set serviceMonitor.enabled=true \
  --set prometheusRule.enabled=true \
  --set dashboard.enabled=true
```

The container image is built from the checked-out source with a digest-pinned
base image:

```bash
docker build --build-arg VERSION=0.23.0 -t ghcr.io/presidio-v/pat-exporter:0.23.0 .
```

## Security posture

- **Pod**: `runAsNonRoot`, non-root UID 10001, `seccompProfile: RuntimeDefault`.
- **Container**: `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`,
  all Linux capabilities dropped.
- **RBAC**: ServiceAccount with no roles and **no mounted token**.
- **Network**: optional NetworkPolicy closes every port except `metrics`.
- The exporter binds `0.0.0.0` inside the pod (required to be scraped) but
  serves only `GET /metrics` and `/healthz` — there is no mutation path.
- The default deployment creates no observation database and works with a
  read-only root filesystem. An optional existing PVC is mounted read-only;
  energy rows are consumed only after full chain verification.

## Key values

See [`values.yaml`](values.yaml) for the full list. Most-used:

| Value | Default | Notes |
|---|---|---|
| `workload.requestsPerSecond` | `500` | `pat export -r` |
| `workload.avgLatencyMs` | `80` | `pat export -l` |
| `workload.currentLayer` | `container` | `pat export -c` |
| `predict.enabled` | `false` | Needs a mounted observation store. |
| `observationStore.enabled` | `false` | Mount an existing PVC read-only. |
| `observationStore.existingClaim` | `""` | Required when the store is enabled. |
| `costPerReplicaHour` | `""` | Adds cost gauges when set. |
| `serviceMonitor.enabled` | `false` | Prometheus Operator. |
| `prometheusRule.enabled` | `false` | Prometheus Operator. |
| `dashboard.enabled` | `false` | Grafana sidecar. |
| `networkPolicy.enabled` | `false` | Needs a NetworkPolicy controller. |

## Bundled artifacts

- [`files/pat-rules.yaml`](files/pat-rules.yaml) — verbatim `pat rules` output,
  wrapped into a PrometheusRule. Regenerate with `pat rules > files/pat-rules.yaml`.
- [`files/pat-dashboard.json`](files/pat-dashboard.json) — copy of
  `grafana/pat-dashboard.json` (kept in sync by `tests/test_chart.py`).
