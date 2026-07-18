# presidio-hardened-arch-translucency

[![PyPI version](https://img.shields.io/pypi/v/presidio-hardened-arch-translucency.svg)](https://pypi.org/project/presidio-hardened-arch-translucency/)
[![Python](https://img.shields.io/pypi/pyversions/presidio-hardened-arch-translucency.svg)](https://pypi.org/project/presidio-hardened-arch-translucency/)
[![GitHub release](https://img.shields.io/github/v/release/presidio-v/presidio-hardened-arch-translucency.svg)](https://github.com/presidio-v/presidio-hardened-arch-translucency/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> v0.24.1 — Family-vector conformance patch for the Architectural Translucency Analyzer. The nominal Kepler energy vector and energy-bearing training vector are now pinned to the authoritative `presidio-evidence` records while PAT retains its fail-closed refusal to emit Kepler measurements.

**Architectural translucency** (Stantchev, ~2005) is the ability to monitor and
control non-functional properties — especially performance — **architecture-wide
in a cross-layered way**.  The core insight: the *same* measure (replication) has
**different implications on throughput ω(δ) and response time** when applied at
different layers.

This CLI tool (`pat`) helps you choose the replication layer that gives the
**highest performance gain with the lowest overhead** for your workload.

---

## Agent Skill — use `pat` from Claude Code, Cursor, etc.

`pat` ships with an Agent Skill for AI coding assistants. When an assistant
edits a Kubernetes manifest, an HPA, Terraform for EKS/GKE/AKS/ECS/Fargate,
or reasons about replica counts and cost-per-request, the skill triggers the
assistant to invoke `pat` and ground its recommendation in the architectural
translucency model instead of guessing.

### Project-local (auto-discovered in this repo)

| Assistant | Path |
|---|---|
| Claude Code | `.claude/skills/pat/SKILL.md` |
| Cursor | `.cursor/skills/pat/SKILL.md` |

An assistant working in this repo discovers them automatically.

### Personal install (works across all your projects)

```bash
# Claude Code
cp -r .claude/skills/pat ~/.claude/skills/pat

# Cursor
cp -r .cursor/skills/pat ~/.cursor/skills/pat
```

### What the skill does

- Triggers on Kubernetes manifests (Deployment, HPA, StatefulSet, ReplicaSet),
  Docker Compose `deploy.replicas`, and Terraform for ECS/Fargate/EKS/GKE/AKS/ACI
- Runs `pat analyze`, `pat cost`, `pat slo`, or `pat what-if` with inputs
  gathered from code and context
- Injects the recommendation into the assistant's plan or PR description,
  citing the architectural translucency model so reviewers can reproduce it
- **Refuses to fabricate** `rps` or `avg_latency_ms` — asks the user instead

The skill is MIT-licensed (same as `pat` itself) and adds no runtime
dependencies; it's plain Markdown that instructs the assistant to shell out
to the `pat` CLI you already installed.

---

## Replication Layers (Docker/Kubernetes)

| Layer | Description | Fixed Overhead | Coordination Cost |
|---|---|---|---|
| `container` | New Docker container (process-level isolation) | 2% | Low |
| `pod` | Kubernetes Pod (shared network namespace) | 5% | Moderate |
| `deployment` | Kubernetes Deployment/ReplicaSet | 10% | High |
| `node` | Cluster node (full VM/bare-metal) | 18% | Highest |

---

## Installation

Requires **Python ≥ 3.10**.

```bash
pip install presidio-hardened-arch-translucency
```

Or with `uv`:

```bash
uv pip install presidio-hardened-arch-translucency
```

---

## Quick Start

```bash
# Analyze a 500 req/s workload with 80ms avg latency, currently at container level
pat analyze --requests-per-second 500 --avg-latency-ms 80 --current-layer container
```

**Output:**

```
╭──────────── Presidio Architectural Translucency — Recommendation ────────────╮
│ Recommended layer:  container                                                 │
│ Optimal replicas:   4                                                         │
│ Throughput gain:    +45.2%                                                    │
│ Response-time Δ:    -38.1%                                                    │
│ Est. throughput:    500 req/s                                                  │
│ Est. response time: 49.4 ms                                                   │
│                                                                               │
│ New Docker container (process-level isolation, shared kernel)                 │
╰───────────────────────────────────────────────────────────────────────────────╯

Baseline: 714 req/s @ 80.0 ms  (current layer: container)
```

### Show all layers

```bash
pat analyze --requests-per-second 500 --avg-latency-ms 80 \
    --current-layer container --show-all
```

| Layer | Replicas | Throughput | Δ Throughput | Response Time | Δ RT | Recommended |
|---|---|---|---|---|---|---|
| container | 4 | 500 | +45.2% | 49.4 ms | -38.1% | ✓ |
| pod | 3 | 500 | +42.0% | 55.2 ms | -31.0% | |
| deployment | 2 | 500 | +38.1% | 68.3 ms | -14.6% | |
| node | 1 | 357 | 0.0% | 80.0 ms | 0.0% | |

---

## Dynamic Scaling Analysis (v0.3.0)

### `pat what-if` — HPA Lag Model

Projects the performance **trough** that occurs between a load spike and the
moment new Kubernetes pods become Ready. Shows throughput, latency, p99, and
missed requests during the HPA scale-out window.

```bash
pat what-if \
  --current-rps 50 --spike-rps 200 \
  --avg-latency-ms 80 --current-layer container \
  --output hpa-event.png
```

Three stacked panels are saved to `hpa-event.png`:
- **Throughput (req/s)** — actual served vs demand, with trough annotation
- **Avg latency (ms)** — how response time degrades during the trough
- **p99 latency (ms)** — tail behaviour before and after pods are Ready

Optional overrides: `--hpa-poll-s` (default 15 s), `--pod-startup-s`
(default 30 s), `--cold-start-s` (default 0 s), `--replicas-before`,
`--replicas-after`.

---

### `pat slo` — SLO Compliance Check

Checks whether a p99 latency SLO is met in **steady-state** and during an
**HPA trough** across all four replication layers.

```bash
pat slo \
  --requests-per-second 50 \
  --avg-latency-ms 80 \
  --p99-target-ms 500 \
  --spike-multiplier 3.0
```

Output table shows steady p99, trough p99, and SLO verdict per layer.
The recommendation panel advises the minimum `HPA minReplicas` needed to
eliminate the trough breach.

---

## Cost-Aware Analysis (v0.4.0 / v0.5.0)

### `pat cost` — Performance-Per-Dollar Ranking

Cross-layer cost analysis showing hourly cost, cost-per-request, and ROI score
for every replication layer.

```bash
pat cost \
  --requests-per-second 500 \
  --avg-latency-ms 80 \
  --current-layer container \
  --cost-per-container-hour 0.02 \
  --cost-per-pod-hour 0.05 \
  --cost-per-deployment-hour 0.10 \
  --cost-per-node-hour 0.50
```

| Layer | Replicas | Δ Throughput | Δ RT | Cost/hr | Cost/req | ROI score | Best ROI |
|---|---|---|---|---|---|---|---|
| container | 4 | +45.2% | -38.1% | $0.0800 | $0.000044 | 1 027 | ✓ |
| pod | 3 | +42.0% | -31.0% | $0.1500 | $0.000083 | 504 | |
| deployment | 2 | +38.1% | -15% | $0.2000 | $0.000111 | 343 | |
| node | 1 | 0.0% | 0.0% | $0.5000 | $0.000278 | 0 | |

ROI score = throughput-gain-% / cost-per-request (higher = better performance-per-dollar).

### `pat analyze` — Cost columns

Add `--cost-per-replica-hour` to include cost columns in `--show-all` output:

```bash
pat analyze --requests-per-second 500 --avg-latency-ms 80 \
    --current-layer container --show-all --cost-per-replica-hour 0.02
```

### `pat what-if` — Trough revenue impact

Add `--cost-per-request` to see the estimated revenue cost of the HPA trough:

```bash
pat what-if --current-rps 50 --spike-rps 200 --avg-latency-ms 80 \
    --current-layer container --cost-per-request 0.001
```

Output includes:
```
  Missed reqs   ~1 350
  Trough cost   ~$1.35 revenue impact
```

### `pat slo` — Min-cost layer that meets SLO

`pat slo` now shows a **Cost/hr** column and identifies the cheapest layer that
satisfies your p99 target.

---

## Cloud Billing Integration (v0.5.0 / v0.6.0)

Replaces manual `--cost-per-*-hour` flags with **live cloud prices** fetched from
public APIs (no credentials required for on-demand/reserved). Results are cached
locally at `~/.pat/pricing-cache.json`.

### `pat cost --cloud aws` — EC2 on-demand pricing (v0.5.0)

```bash
pat cost \
  --requests-per-second 500 \
  --avg-latency-ms 80 \
  --current-layer container \
  --cloud aws \
  --region us-east-1 \
  --instance-type m5.large
```

Per-layer costs use packing ratios: 16 containers/node, 8 pods/node.

### `pat cost --cloud aws --show-reserved` — Reserved pricing (v0.6.0)

Add `--show-reserved` to render 1-year and 3-year No Upfront reserved pricing
alongside on-demand in separate tables:

```bash
pat cost -r 500 -l 80 -c container \
  --cloud aws --region us-east-1 --instance-type m5.large \
  --show-reserved
```

### `pat cost --cloud aws --spot` — Spot pricing (v0.6.0)

Requires boto3 and AWS credentials (`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`):

```bash
pip install "presidio-hardened-arch-translucency[spot]"

pat cost -r 500 -l 80 -c container \
  --cloud aws --region us-east-1 --instance-type m5.large \
  --spot
```

Spot prices use a **5-minute cache TTL** and are annotated with an interruption-risk warning.

### `pat cost --cloud aws --fargate` — Fargate task pricing (v0.5.0)

```bash
pat cost -r 500 -l 80 -c container \
  --cloud aws --region us-east-1 --fargate --vcpu 0.5 --memory-gb 1
```

### `pat cost --cloud gcp` — GCP Compute Engine pricing (v0.6.0)

Fetches from the public GCP Pricing Calculator JSON (no credentials required):

```bash
pat cost -r 500 -l 80 -c container \
  --cloud gcp --region us-central1 --machine-type n2-standard-4

# Add --spot for preemptible pricing with interruption-risk annotation
pat cost -r 500 -l 80 -c container \
  --cloud gcp --region us-central1 --machine-type n2-standard-4 --spot
```

### `pat cost --cloud azure` — Azure VM pricing (v0.6.0)

Fetches from the official Azure Retail Prices API (no credentials required):

```bash
pat cost -r 500 -l 80 -c container \
  --cloud azure --region eastus --sku-name "D2s v3"

# Add --spot for Azure Spot VM pricing
pat cost -r 500 -l 80 -c container \
  --cloud azure --region eastus --sku-name "D2s v3" --spot
```

### Cache control

| Flag | Effect |
|---|---|
| *(default)* | On-demand/reserved: 24 h cache; spot: 5 min cache |
| `--no-cache` | Force a fresh API fetch (use in CI cost-gate pipelines) |

---

## Autoresearch: calibrate → observe → optimize (v0.7.0 / v0.8.0)

The static commands above (`analyze`, `cost`, `slo`, `what-if`) reason from the
analytical model. The **autoresearch** commands close the loop with *measured*
data: calibrate the model to your workload, record a rolling history of live
measurements, and project demand forward into a proactive scaling
recommendation — optionally emitted as an apply-able HPA manifest.

All three share `~/.pat/`: the fitted model lives at `~/.pat/model.json` (or a
project-local `.pat-model.json`, which takes precedence), and the rolling
observation history lives in a SQLite store at `~/.pat/observations.db`.

### `pat calibrate` — fit the model to measured points (v0.7.0)

Fits the per-replica capacity model (concurrency κ and coordination overhead β)
to two or more measured `rps:latency_ms:replicas` points from your APM, load
tests, or prior `pat demo` runs. Writes `~/.pat/model.json`; afterwards the
static commands use your fitted parameters and stop emitting the envelope
warning. No Docker required.

```bash
pat calibrate --observation 100:50:2 --observation 300:80:5
```

Prints a per-observation prediction/residual table plus overall R² and RMSE.

**Calibration commitment.** Every fit written by `pat calibrate` embeds a
`calibration_commitment` — a SHA-256 over the calibration inputs (the observation
set) and outputs (fitted κ/β, R²/RMSE). `pat analyze`, `pat what-if` and
`pat slo` re-hash the stored parameters and **fail closed** if they no longer
match the commitment: a model file hand-edited after calibration is rejected
rather than silently driving a recommendation. The recommendation output carries the commitment
digest so a recommendation is traceable to the exact parameters that produced
it. A model file written before commitments existed has none; it is reported as
a *legacy* (unbound) throughput fit and used, never rejected — recalibrate to
bind it. Energy fields are never accepted from a legacy record because energy
calibration was introduced together with commitments in v0.20.0. See
[ADR-0010](docs/adr/0010-observation-chain-and-calibration-commitment.md).

#### `pat calibrate --benchmark` — measure the points with Docker (v0.9.0)

Don't have measured points to hand? Let `pat` measure them. Benchmark mode sweeps
a set of replica counts on the local Docker daemon — starting that many copies of
the same Monte Carlo workload `pat demo` uses, load-testing each — then fits the
model to the measured throughput/latency at every count. Requires a running
Docker daemon; the workload containers are published to `127.0.0.1` only.

```bash
# Sweep 1, 2, 4 replicas, measure each, and fit (defaults to 1 2 4)
pat calibrate --benchmark --layer container \
  --replicas 1 --replicas 2 --replicas 4

# Tune the load applied at each replica count
pat calibrate --benchmark --requests 80 --concurrency 16 --iterations 200000
```

At least two distinct replica counts are required (one point cannot constrain
both parameters). Pass `--layer` to write per-layer parameters, exactly as in
analytical mode. `--observation` and `--benchmark` are mutually exclusive.

### `pat observe` — record a rolling measurement history (v0.8.0)

Records a single workload observation into the SQLite store, or lists recent
rows. **Single-shot by design** — it takes one measurement and exits; schedule
recurring collection externally (cron, launchd, a Kubernetes CronJob).

```bash
# Record one measurement (all fields required when recording)
pat observe --layer container \
  --rps 480 --avg-latency-ms 78 --p99-latency-ms 190 \
  --throughput 470 --replicas 4

# List the most recent observations
pat observe --list --limit 20
```

The store is **source-agnostic** — supply numbers from any source. Tag the
origin with `--source` (defaults to `manual`), and override the store path with
`--db`.

#### `pat observe --prometheus` — scrape one sample from Prometheus (v0.8.0)

Scrapes a single sample (rps, p99, replica count) from the Prometheus HTTP API
and records it with `source='prometheus'`. Still single-shot — schedule repeats
externally. Prometheus does not know the replication layer, so `--layer` is
required.

```bash
export PAT_PROMETHEUS_TOKEN=...   # optional bearer token; env only, never a flag
pat observe --prometheus http://prometheus.monitoring.svc:9090 --layer deployment
```

The bearer token is read from `PAT_PROMETHEUS_TOKEN` only — never passed as a
CLI argument and never logged.

#### `pat observe verify` — tamper-evident observation history

Each new observation is linked into a per-store SHA-256 **hash chain**: every
record's hash binds its own contents, its chain position, and the previous
record's hash. `pat observe verify` walks the chain and reports the first break,
detecting any post-hoc edit, deletion, insertion, or reorder of the local
history relative to the chain head.

```bash
pat observe verify                 # verify ~/.pat/observations.db
pat observe verify --db ./obs.db   # verify a specific store
pat observe verify --allow-legacy  # allow only an intact legacy-prefix report
```

The chain is a parallel table — the `observations` measurement schema is
untouched, and `pat observe` records nothing new. Rows recorded *before* chaining
existed carry no link and are reported as an **UNVERIFIABLE legacy prefix**,
never counted as verified coverage. Existing databases keep working unchanged.
Exit codes are machine-distinct: `0` means chain intact with full coverage, `1`
means a chain break was found, and `2` means the chain suffix is intact but
coverage is incomplete because legacy rows remain. `--allow-legacy` downgrades
only exit `2` to `0`; a broken chain still exits `1`.

**What the chain proves — and what it does not.** A clean chain proves the local
history was not rewritten after the fact relative to the chain head. It does
**not** prove the readings were honest at capture time — a source can still
record a false measurement; the chain only makes silent *rewriting* of what was
recorded detectable. (Numeric readings are hashed as shortest round-trip decimal
strings, following the evidence family's float discipline — see
[ADR-0010](docs/adr/0010-observation-chain-and-calibration-commitment.md).) This
realizes the "evidence by cryptography, not by mutable logs" creed of the
Computational Jurisprudence program (Stantchev, arXiv 2026).

### `pat optimize` — proactive scaling recommendation (v0.8.0)

Reads the observation store, projects demand a few minutes ahead, and recommends
the replica count to serve it.

```bash
# Simple moving average over the most-recent samples (default)
pat optimize --model sma --window 10 --horizon-minutes 10

# ARIMA forecast with a 95% confidence interval + replica range
pat optimize --model arima --horizon-minutes 15
```

`--model arima` fits a `statsmodels` ARIMA with a 95% confidence interval and
emits a replica range; it **auto-falls back to SMA** when fewer than 30 samples
are available. Restrict to one layer with `--layer`, or override the store with
`--db`.

#### `pat optimize --emit-hpa-patch` — emit an apply-able HPA (v0.8.0)

Instead of the summary panel, emit a sanitised `HorizontalPodAutoscaler`
manifest to stdout for a target Deployment. `minReplicas` is the point
recommendation; `maxReplicas` is the ARIMA upper-CI bound when available.

```bash
pat optimize --model arima --emit-hpa-patch \
  --target my-api --namespace production > hpa-patch.yaml
kubectl apply -f hpa-patch.yaml
```

The target and namespace are validated as RFC 1123 names; no user input is
echoed raw into the manifest.

### Workflow example — the observe → optimize loop

```bash
# 1. (Once) calibrate the model to a couple of measured operating points
pat calibrate --observation 100:50:2 --observation 300:80:5

# 2. (Recurring, e.g. a */1 * * * * cron entry) record a live sample each minute
pat observe --prometheus http://prometheus:9090 --layer deployment

# 3. After ~30+ samples accumulate, project demand and recommend replicas
pat optimize --model arima --horizon-minutes 15

# 4. When you're ready to act, emit the HPA and apply it
pat optimize --model arima --emit-hpa-patch \
  --target my-api --namespace production | kubectl apply -f -
```

Until ~30 samples exist, step 3 transparently falls back to SMA, so the loop is
useful from the very first observations.

---

## Monitoring integration: Prometheus exporter (v0.10.0)

The first step of the monitoring-integration arc ("The Translucency Control
Plane"). `pat export` publishes the model's per-layer recommendations as
Prometheus metrics on a **read-only** `/metrics` endpoint, so they can be scraped
into Grafana alongside the metrics you already run — `pat` never mutates
infrastructure, it only exposes.

```bash
# Serve metrics on http://127.0.0.1:9847/metrics (Ctrl-C to stop)
pat export --requests-per-second 500 --avg-latency-ms 80 --current-layer container

# Print the exposition once and exit (no server) — handy for CI / a quick look
pat export -r 500 -l 80 -c container --once
```

Exposed gauges (per `layer` where applicable): `pat_recommended_replicas`,
`pat_estimated_throughput_rps`, `pat_response_time_ms`, `pat_throughput_gain_ratio`,
`pat_layer_recommended`, plus `pat_workload_*` inputs and `pat_build_info`.

### Forecast metrics from the observation store (`--predict`)

With `--predict`, the exporter *also* runs an `optimize` pass over your
observation store (`pat observe`) on every scrape and exposes the live forecast —
turning the exporter from a static view into the moving front of the
observe → predict → visualize loop:

```bash
# Expose forecast metrics alongside the analysis (SMA by default)
pat export -r 500 -l 80 -c container --predict

# Use ARIMA (refits each scrape — prefer SMA for frequent intervals)
pat export -r 500 -l 80 -c container --predict --model arima --horizon-minutes 15
```

Adds `pat_predicted_rps{model}`, `pat_predicted_recommended_replicas{layer}`,
`pat_observed_rps`/`pat_observed_latency_ms`, `pat_optimize_trend_ratio`,
`pat_optimize_horizon_minutes`, and `pat_optimize_samples` (reads `0` on an empty
store). With `--model arima` it also exposes 95% CI bounds
(`pat_predicted_rps_lower`/`_upper` and the matching replica bounds). `--window`,
`--predict-layer`, and `--db` tune the SMA window, the observation layer, and the
store path.

Scrape it from Prometheus:

```yaml
scrape_configs:
  - job_name: pat
    static_configs:
      - targets: ["127.0.0.1:9847"]
```

**Security.** The server is read-only (only `GET` is implemented — any other
method returns `501`) and binds `127.0.0.1` by default. Binding a routable
interface requires an explicit opt-in:

```bash
pat export -r 500 -l 80 -c container --host 0.0.0.0 --listen-public
```

Metric names are fixed (never user input) and label values are escaped. Use
`--layer` to expose a per-layer calibrated fit (see `pat calibrate --layer`).

### Cost metrics (`--cost-per-replica-hour`)

Pass a uniform replica cost to add per-layer cost gauges
(`pat_cost_per_request`, `pat_hourly_cost_usd`):

```bash
pat export -r 500 -l 80 -c container --cost-per-replica-hour 0.02
```

(For live cloud pricing across AWS/GCP/Azure, use `pat cost` — the exporter
keeps a single uniform rate to stay scrape-cheap and network-free.)

### Vendor-neutral push over OTLP (`--otlp`, v0.13.0)

Instead of being scraped, the exporter can **push** its metrics once over
OTLP/HTTP+JSON to an OpenTelemetry Collector, which fans out to Datadog, New
Relic, Honeycomb, Grafana Cloud, etc. — so you get pat metrics **without
Prometheus**:

```bash
# One-shot push to a collector (schedule via cron for recurring push)
pat export --otlp http://collector:4318 -r 500 -l 80 -c container --predict
```

Per [ADR-0006](docs/adr/0006-otlp-export-transport.md) this is hand-rolled
OTLP/HTTP+JSON (no `opentelemetry` SDK, no gRPC) — point it at an OTel Collector
(or any OTLP/HTTP endpoint that accepts JSON). An optional bearer token is read
from `PAT_OTLP_TOKEN` only (HTTPS required unless `--insecure-http`);
`--service-name` sets the OTLP `service.name`.

### Ephemeral contexts: Pushgateway (`--pushgateway`, v0.14.0)

Cron jobs, CI, and Kubernetes `Job`/`CronJob`s have no scrape endpoint. Push the
metrics once to a Prometheus **Pushgateway** instead, then exit — Prometheus
scrapes the gateway:

```bash
pat export --pushgateway http://pushgateway:9091 --job pat \
  --grouping instance=ci-7 -r 500 -l 80 -c container
```

This reuses the exporter's Prometheus text output verbatim (zero new
dependencies). `--job` sets the job (default `pat`); repeat `--grouping key=value`
for grouping labels (validated + percent-encoded). An optional token is read from
`PAT_PUSHGATEWAY_TOKEN` only (HTTPS required unless `--insecure-http`). `--otlp`
and `--pushgateway` are mutually exclusive.

### Official Grafana dashboard

A ready-to-import dashboard lives at [`grafana/pat-dashboard.json`](grafana/pat-dashboard.json).
It visualises observed-vs-predicted demand (with the ARIMA CI band), recommended
replicas per layer, response time, throughput gain, and cost-per-request.

Import it via **Grafana → Dashboards → New → Import**, upload the JSON, and pick
your Prometheus data source when prompted. Pair it with `pat export --predict`
(and `--cost-per-replica-hour` for the cost panel) for the full picture.

### Alerting rules (`pat rules`, v0.11.0)

`pat rules` emits a Prometheus rule file (recording + alerting rules) built from
the exporter's metrics, so the model's signals fire through your existing
Alertmanager — `pat` only emits the YAML, it never loads or applies it.

```bash
# Emit recording + alerting rules
pat rules --current-layer container --cost-budget 0.000001 > pat-rules.yml
```

Then reference it from `prometheus.yml` and reload Prometheus:

```yaml
rule_files:
  - pat-rules.yml
```

Alerts: **PatDemandSurgeForecast** (forecast demand outruns observed),
**PatDemandTrendRising** (demand trending up), **PatLayerTranslucencyMismatch**
(running on a layer the model no longer recommends — needs `--current-layer`),
**PatCostPerRequestOverBudget** (needs `--cost-budget`), and **PatExporterAbsent**
(scrape health). Tune with `--demand-surge-ratio`, `--trend-threshold`, and
`--for`. Every emitted scalar is validated/escaped — the rule file can't smuggle
arbitrary content.

### Provisioning & annotations (v0.12.0)

[`grafana/provisioning/`](grafana/provisioning/) holds Grafana file-provisioning
configs (datasource + dashboard provider) so the dashboard loads automatically —
mount it instead of importing by hand (see [`grafana/README.md`](grafana/README.md)).

`pat annotate` posts the current recommendation to Grafana's annotation API so it
shows up as a marker on your dashboards — `pat`'s one **outbound write**, and only
ever an informational annotation, never an infrastructure change:

```bash
export PAT_GRAFANA_TOKEN=...   # editor token; env only, never a flag
pat annotate -r 500 -l 80 -c container --grafana https://grafana.example.com
pat annotate -r 500 -l 80 -c container --grafana https://g --dry-run   # preview only
```

The token is read from `PAT_GRAFANA_TOKEN` only (never a flag, never logged),
HTTPS is required (use `--insecure-http` for localhost dev), and tags are
sanitised. `--dry-run` prints the payload without posting; `--dashboard-uid`
scopes the annotation to one dashboard; `--tag` adds extra tags.

### Close the loop: autoscaling (`pat scaler`, v0.15.0)

The conceptual payoff of the monitoring arc — **translucency-aware autoscaling**.
The exporter publishes `pat_predicted_recommended_replicas` (run `pat export
--predict`, scraped into Prometheus); `pat scaler` emits the declarative glue so
an autoscaler scales a Deployment to **track that forecast**:

```bash
# KEDA ScaledObject (default) — threshold 1, so replicas == pat's prediction
pat scaler -t web --prometheus-url http://prom:9090 -c container > scaledobject.yaml
kubectl apply -f scaledobject.yaml

# Or an HPA on an External metric (Prometheus Adapter path)
pat scaler -t web --prometheus-url http://prom:9090 --format prometheus-adapter
```

`pat` only **emits** the YAML — it never applies or scales anything (apply via
`kubectl`/GitOps). `--min-replicas`/`--max-replicas` bound the range, `--layer`
filters the default query, `--query` overrides it, and target/namespace names are
RFC 1123-validated.

### Cluster-native packaging: Helm chart (`charts/pat-exporter`, v0.16.0)

The arc's final step — **"Package & operate"**: the exporter and its declarative
monitoring artifacts ship as one installable Helm chart.

`v0.17.0` is an evidence/library release, not a chart release. The chart and
default image tag remain `0.16.0`; set `image.tag` only when you have built and
published a matching newer `pat-exporter` image.

```bash
# Minimal install — Deployment + Service + ServiceAccount (works on any cluster)
helm install pat ./charts/pat-exporter -n monitoring --create-namespace \
  --set workload.requestsPerSecond=500 \
  --set workload.avgLatencyMs=80 \
  --set workload.currentLayer=container

# Full stack — add the Prometheus-Operator ServiceMonitor + PrometheusRule and
# the Grafana dashboard (loaded via the Grafana sidecar)
helm install pat ./charts/pat-exporter -n monitoring \
  --set serviceMonitor.enabled=true \
  --set prometheusRule.enabled=true \
  --set dashboard.enabled=true
```

The pod is **hardened by default** (non-root UID 10001, read-only root
filesystem, all capabilities dropped, `seccompProfile: RuntimeDefault`) and the
ServiceAccount mounts **no token** — the read-only exporter needs no Kubernetes
API access. Operator/sidecar objects (`ServiceMonitor`, `PrometheusRule`,
dashboard `ConfigMap`) and the `NetworkPolicy` are off by default; enable the
ones your stack supports. The chart **applies nothing** to the cluster — it only
deploys the read-only exporter and emits declarative artifacts (arc invariant
A1). The container image builds from the repo `Dockerfile`. See
[`charts/pat-exporter/README.md`](charts/pat-exporter/README.md) for all values.

---

## Live Demonstrator

`pat demo` spins up real Docker containers and measures throughput, latency,
and CPU across three replication variants, then outputs:

1. A results table and PNG comparison chart
2. An **HPA Lag Projection** — what happens if load spikes 3× (v0.3.0)
3. A **Cost Analysis** panel — cost/req per variant and best-ROI layer (v0.5.0)

**Requirements:** Docker daemon running locally.

```bash
# Install with demo extras
pip install "presidio-hardened-arch-translucency[demo]"

# Run the demo (defaults: 4 replicas, 40 requests, 8 concurrent threads)
pat demo

# Custom run with cost override
pat demo --replicas 6 --requests 80 --concurrency 12 \
    --cost-per-container-hour 0.05 --output results.png
```

**Variants compared:**

| Variant | Description |
|---|---|
| 1 — Single container | Baseline: one container handles all traffic |
| 2 — N containers (round-robin) | Manual container-level replication, client-side LB |
| 3 — N workers + nginx | Simulated Kubernetes Deployment with nginx reverse proxy |

**Example output:**

```
╭───── Architectural Translucency — Measured Results ──────╮
│ Variant                    Workers  Throughput  Avg Lat   │
│ 1 — Single container            1        8.2    612 ms    │
│ 2 — 4 containers (round-robin)  4       28.7    167 ms ✓  │
│ 3 — nginx LB (4 workers)        5       22.4    213 ms    │
╰──────────────────────────────────────────────────────────╯

Architectural Translucency Insight:
  Manual container replication minimises coordination overhead…

╭──────────── HPA Lag Projection (if load spikes 3×) ─────────────╮
│ TROUGH  (0 s – 45 s)                                             │
│   Throughput    8.2 req/s  (9 % of spike demand)                 │
│   p99 latency   4,896 ms                                         │
│   Missed reqs   ~3,321                                           │
│ STEADY STATE  (after 45 s — 3 replicas)                          │
│   Throughput    24.6 req/s                                        │
│   p99 latency   1,102 ms                                         │
│ → Set HPA minReplicas = 3 to eliminate the trough.               │
╰──────────────────────────────────────────────────────────────────╯

╭────────────────── v0.5.0 Cost Analysis ──────────────────────────╮
│ Best measured variant:  2 — 4 containers (round-robin)           │
│   Cost/req  $0.000077  ·  Cost/hr  $0.0800                       │
│ Analytical best-ROI layer:  container                            │
│   Replicas  4  ·  Throughput gain  +45.2%                        │
│   Cost/req  $0.000044  ·  Cost/hr  $0.0800  ·  ROI score  1027   │
╰──────────────────────────────────────────────────────────────────╯
```

Two PNG files are saved: `demo-results.png` (bar chart) and `demo-results-hpa.png`
(3-panel HPA time-series).

---

## Security — Presidio Hardening

This toolkit ships with mandatory Presidio security extensions:

| Feature | Description |
|---|---|
| **Input sanitization** | All workload parameters are bounds-checked and type-validated |
| **Secure logging** | Recommendations logged without sensitive data |
| **CVE/dependency audit** | `pip-audit` check on normal command execution (`--skip-audit` to disable; help/version exits skip network audit) |
| **Security event logging** | `"Presidio architectural-translucency recommendation applied"` emitted |
| **Output sanitization** | User-supplied values are never echoed raw into output |
| **Dependabot** | Automated dependency updates via `.github/dependabot.yml` |
| **CodeQL** | Static analysis via `.github/workflows/codeql.yml` |

---

## CLI Reference

```
Usage: pat [OPTIONS] COMMAND [ARGS]...

Options:
  -V, --version         Show version and exit.
  -v, --verbose         Enable debug logging.
  --skip-audit          Skip the on-run CVE dependency audit.
  --help                Show this message and exit.

Commands:
  analyze     Analyze workload and recommend the optimal replication layer.
  what-if     Project the HPA scale-out trough for a load spike.
  slo         Check p99 SLO compliance in steady state and during a trough.
  cost        Rank layers by cost-per-request and performance-per-dollar.
  calibrate   Fit the model to measured rps:latency:replicas points.
  observe     Record one workload observation (or --list recent ones).
  optimize    Proactive scaling recommendation from observed history.
  demo        Run the live Docker demonstrator.

pat analyze Options:
  -r, --requests-per-second FLOAT   Observed workload in req/s  [required]
  -l, --avg-latency-ms FLOAT        Current average latency in ms  [required]
  -c, --current-layer TEXT          Current layer (container|pod|deployment|node)  [required]
  --show-all                        Show all layers in a comparison table
```

Run `pat <command> --help` for the full option list of any command.

---

## Theory: Architectural Translucency Model

The model is based on the replication performance equations from Stantchev's work:

**Intensity after replication:**
```
ι(δ) = rps/δ  +  α·rps  +  β·rps·ln(δ)
```

**Throughput:**
```
ω(δ) = min(base_capacity · δ · efficiency(δ), rps)
efficiency(δ) = 1 - α - β·ln(δ)
```

**Response time** (M/M/δ approximation):
```
RT(δ) = avg_latency / (1 - ρ)  +  coordination_overhead
ρ = ι(δ) / base_capacity
```

Where `α` (fixed overhead) and `β` (coordination cost) are layer-specific
parameters calibrated for Docker/Kubernetes realities.

The **cross-layer recommendation** maximises `ω(δ)` gain while penalising
response-time degradation — the central principle of architectural translucency.

---

## Development

```bash
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"

# Format + lint
ruff format . && ruff check . --fix

# Tests with coverage
pytest
```

---

## Signed degradation evidence (v0.17.0, library)

`evidence_producer` turns a degradation reading / `Observation` into a signed
`presidio-hardened/evidence-ref@1` envelope that downstream family consumers
verify fail-closed before acting on it.

**Key-less by design.** `pat` itself holds **no signing key**. It emits an *unsigned*
Layer-0 reading; a separate signing-bridge sidecar holds the Ed25519 key and signs it —
preserving the read-only "no secrets to steal" posture (mirrors `treasury` in evidence
ADR-0001). Consumers hold only the public key.

```bash
# Emit a key-less Layer-0 reading (only when p99 breaches the target), pipe to the sidecar:
pat evidence-emit --p99-target-ms 200 --p99-latency-ms 420 | evidence-bridge-sign
```

```python
# Library API (the sidecar uses observation_to_evidence with its key):
from presidio_arch_translucency.evidence_producer import observation_to_layer0
reading = observation_to_layer0(obs, slo_target_ms=200)   # unsigned, key-less
# sidecar signs → downstream consumers verify the signature before acting.
```

Ed25519 needs the optional `[evidence]` extra and uses a raw 32-byte private
seed encoded as exactly 64 lowercase hex characters; HMAC + the canonical/hash
layer are stdlib.
See [PRESIDIO-REQ.md](PRESIDIO-REQ.md) "Evidence Arc (v0.17.0)".

**Family golden vectors.** The producer's wire formats are pinned in the
normative `presidio-evidence` vector tree (Python + Rust conformance): the
degradation chain by `vectors/slo-reading/` (byte-identical — content hash
*and* deterministic Ed25519 signature — to `build_slo_evidence` output;
`item_id SLO-DEGRADED`, `ledger_ref arch-translucency:obs`) and the training
record by `vectors/training-run/` (pinned in `tests/test_training.py`). If
this producer ever drifts from the family canonical profile, a vector suite
breaks before a consumer does.

---

## ML training parallelism (v0.18.0, Training Arc)

The same translucency question — *at which layer does replication yield the
highest throughput gain with the lowest overhead?* — answered for
**distributed training**. Strategies are the training analog of replication
layers: `data` (DDP), `fsdp` (FSDP/ZeRO-3), `tensor`, `pipeline`
([ADR-0009](docs/adr/0009-training-domain-profile.md) — a separate domain
profile; the serving model is untouched).

Two deliberate departures from the serving domain: **per-device memory is a
hard feasibility constraint** (an oversized DDP replica is impossible, not
suboptimal — infeasible points are excluded, never scored), and **throughput
is compute-bound** (no demand cap). `pipeline` uses the exact bubble formula
`(1 − α)·m/(m+δ−1)`; the α/β strategies are calibratable via a `training`
section of `.pat-model.json`.

```bash
# Cross-strategy recommendation (40 GB model on 24 GB devices → DDP excluded):
pat train-analyze --samples-per-second 120 --model-memory-gb 40 \
  --device-memory-gb 24 --devices 8 --show-all

# One specific configuration, JSON for automation:
pat train-what-if --strategy pipeline --degree 4 -s 100 -m 40 -d 24 --json
```

### Training-run evidence (`training-run@1`) + provenance parents

`pat train-evidence-emit` emits a key-less, unsigned Layer-0 **training-run
record** (run id, strategy, degree, samples/s, duration, devices, optional
model/dataset hashes) for the same signing-bridge sidecar as `evidence-emit`.
The payload may carry `parents` — content hashes of the upstream evidence that
authorized the run (classification, gate decision) — attested *inside* the
signed content, so evidence forms a verifiable provenance DAG that can support
broader operator documentation:

```bash
pat train-evidence-emit --run-id run-2026-07-02-001 --strategy fsdp --degree 8 \
  -s 712 --duration-s 3600 -n 8 \
  --parent <classification content_hash> --parent <gate-decision content_hash> \
  | evidence-bridge-sign
```

All inputs are validated fail-closed (finite numbers only, printable bounded
run ids, lowercase-hex parent hashes); the library enforces the same contract
independently of the CLI so a sidecar can never sign malformed content.

### Training energy (v0.23.0) — "Train the watt"

Rather than hand-editing the `training` section, **calibrate** the α/β overhead
from measured runs. `pat train-calibrate` fits `tp(δ) = baseline · δ · eff(δ)`
from one JSON-Lines **step log** per run and writes a *committed*
`training.<strategy>` record (a post-calibration edit is detected and fails
closed, exactly like `pat calibrate` for serving).

A step log is JSON Lines — one optimizer step per line, carrying exactly these
keys (unknown keys are rejected so a typo'd `"power"` cannot silently vanish;
`power_w` is optional but must be present on **every** line of a run or none):

```jsonl
{"step": 0, "duration_s": 2.01, "samples": 512, "power_w": 430.0}
{"step": 1, "duration_s": 1.98, "samples": 512, "power_w": 428.5}
```

The run aggregate a fit consumes is `samples_per_second = total_samples /
total_duration_s`; the energy aggregate is the duration-weighted mean of
`power_w`. Step ids must be strictly increasing. Files are descriptor-bound,
non-symlink regular files (≤10 MB, ≤100 000 lines, strict UTF-8); numeric fields
are bounded and every malformed line fails closed naming its 1-based number.

```bash
# Fit the DDP overhead from three step logs at δ = 1, 2, 4
# (α/β strategies need ≥3 distinct degrees for the full fit; pipeline needs ≥2):
pat train-calibrate --strategy data \
  --run 1:logs/ddp1.jsonl --run 2:logs/ddp2.jsonl --run 4:logs/ddp4.jsonl
```

Every fit requires a δ=1 run, which uniquely anchors the single-device baseline.
When every run's log carried `power_w`, the fit also records fitted watts per
device. `train-analyze` / `train-what-if` then add a **`Samples/s/W`** column
and, only when every feasible strategy has comparable power data, mark the
energy-best strategy (`⚡ best`) — a marker distinct
from the throughput recommendation. Energy is informational only: it never
changes the recommendation and never excludes a strategy (memory stays the only
hard constraint), and without any fitted power or an explicit
`--device-power-watts N` (1–2000) the table is byte-identical to before.

```bash
# Rank strategies by throughput AND samples/s/W (fitted power, or a placeholder):
pat train-analyze -s 120 -m 40 -d 24 -n 8 --device-power-watts 400 --show-all
```

A `training-run@1` evidence record can also carry the run's energy as
**producer-attributed** figures (a measured claim or modelled estimate, never an
observation-chain reading — Energy Arc invariant E1a). Pass them as an int or a
decimal **string** (bare floats are rejected as non-portable on the wire); both
forms canonicalize to the same string-decimal hash. If both fields are supplied,
mean total run power must agree with energy and duration within 2% or 0.01 Wh.
The fields join the attested content only when given, so a power-free record
hashes byte-identically to a pre-v0.23 one:

```bash
pat train-evidence-emit --run-id run-2026-07-16-001 --strategy fsdp --degree 8 \
  -s 712 --duration-s 3600 -n 8 --energy-wh "420.0" --mean-power-w "420.0" \
  | evidence-bridge-sign
```

A signed `training-run@1` can contribute one provenance record to a provider's
technical documentation. It does not by itself establish compliance: EU AI Act
Article 53 and Annex XI require broader GPAI technical documentation, while
Germany's EnEfG §12 requires qualifying data-centre operators to continuously
measure electrical power and energy demand for essential components within an
energy or environmental management system. The energy here remains attributed
to the producer under the v0.18 trust boundary (E1a), not asserted as a
measurement by this suite.

---

## Energy model (v0.20.0)

The same translucency question — *the same measure (replication) has different
implications at different layers* — answered for **energy**. `pat` models the
power a layer draws serving δ replicas at throughput ω(δ):

```
E(δ):  W(δ) = idle_w·δ  +  dyn_j_per_req·ω(δ)·(1 + β_E·ln δ)
       idle_w        = α_E · replica_power_watts            (standing floor)
       dyn_j_per_req = (1 − α_E) · replica_power_watts / base_capacity_rps
```

Each layer carries an idle-power fraction **α_E** and a ln-δ coordination
overhead **β_E** (a parallel table to the throughput params — the serving model
is untouched, per ADR-0010). The insight is the idle-power **asymmetry**: a
container adds almost no standing draw, while a *node* buys the whole server
idle floor whether or not it does useful work.

| Layer | α_E (idle fraction) | β_E (coordination) | Rationale |
|---|---|---|---|
| container | 0.02 | 0.005 | ~constant ≈2 W standing draw on a shared kernel |
| pod | 0.03 | 0.010 | container + kubelet/pause bookkeeping |
| deployment | 0.05 | 0.020 | + scheduler/etcd share |
| node | 0.36 | 0.030 | full server idle floor (fleet idle ≈36 % of peak, 2023 servers) |

> These are **MVP placeholders** encoding published idle-power ratios
> (Tadesse/Chiasserini/Malandrino 2018; SPECpower_ssj2008), not a measurement of
> your fleet. Calibrate them from real watt readings before trusting the
> absolute joules; the relative container-vs-node story holds regardless.

```bash
# Analyze with the energy columns (Watts, J/req, EEI). --replica-power-watts is
# the per-replica peak power (MVP placeholder ≈15 W):
pat analyze --requests-per-second 500 --avg-latency-ms 80 \
  --current-layer container --show-all --replica-power-watts 15
```

`--show-all` adds **Watts** (W(δ) at the recommended δ), **J/req** (energy per
request) and **EEI** (Energy-Efficiency Index). EEI > 1 means replicating at
that layer buys more throughput than it costs in energy intensity. `pat what-if`
reports estimated power / J-per-req for the evaluated point; `pat slo` adds a
**J/req** column (the frontier's third axis: p99 × $/req × J/req).

```bash
# Fit real energy: rps:latency_ms:replicas:watts (total system watts). Two or
# three unique points hold β_E at its default; four or more identifiable points
# fit β_E too. Use at least two replica counts. The energy fit is written into
# the same model record and bound by the calibration commitment:
pat calibrate --observation 300:80:5 --observation 500:80:6 \
  --energy-observation 300:80:2:180 --energy-observation 500:80:5:300 \
  --energy-observation 700:80:8:420 --energy-observation 400:80:3:240
```

**Invariant E1:** `pat` *measures, models, and evidences* energy — it **never
actuates power**. No DVFS, no power caps, no writes to any infrastructure; the
watt is a number to reason with, not a knob to turn.

---

## Measured energy (v0.21.0)

The energy model above is the honest *modelled* answer. v0.21.0 adds **measured**
watts: readings from a real power meter, stored in a parallel hash-chained table
(`energy_observations`) alongside the serving observation chain. The governing
rule (ADR-0011 §2 amendment, corollary **E1a**) is blunt: **pat never signs a
watt it did not measure** — the chain only ever receives measured watts; the
analytic model never enters it, and there is no `analytic` meter.

```bash
# Scrape one gated watt reading from a real meter and record it (chained).
# --energy-meter is required (no default — an explicit meter is a claim):
pat observe --prometheus https://prometheus.monitoring.svc:9090 \
  --energy --energy-meter rapl --layer node --energy-window-s 60
```

Measured mode is **platform-gated and fail-closed**. Before any watt is recorded,
pat runs the meter's *gate query* and refuses when no real power interface is
present — on a host without one, nothing is written:

```
Measured-energy collection failed: no real power source detected: rapl gate
query 'sum by (path) (increase(node_rapl_package_joules_total[60s]))' returned no
series. This platform exposes no real power interface (readable RAPL zone / DCGM
device); pat refuses to sign an unmeasured watt (ADR-0011 E1a). Nothing was
recorded.
```

The gate proves a real power interface for **every supported meter**: it refuses
unless at least one gate series carries a non-empty identity label for that
interface (rapl→`path`, dcgm→`gpu`). Estimator **value tells** are refused *at
the door*, not detected after the fact: any series carrying
`components_power_source="estimator"` or `cpu_architecture="unknown"` (matched
after `strip()`+`casefold()`, so casing/whitespace variants cannot slip through)
is dropped. Kepler is refused regardless of labels: current Kepler can use a
synthetic CPU meter and proportionally attributes node energy to workloads, so
its metric/zone shape is not proof that a workload watt was directly measured.

| Meter | Pinned metric (supported floor) | Gate label |
|---|---|---|
| `rapl` | `node_rapl_package_joules_total` — node-exporter RAPL package domain | `path` |
| `dcgm` | `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` — DCGM exporter, millijoules (÷1000 → W) | `gpu` |

The preset uses `increase(counter[window]) / window`, where `window` is the
same whole-second `--energy-window-s` stored in the record. This binds the
PromQL measurement interval to the reported joules window.

The gate and watts queries are overridable (`--energy-watts-query`,
`--energy-gate-query`). Overriding **either** away from the meter's pinned preset
forfeits the pinned-metric attestation: the reading is recorded with
`source=prometheus-override` (not `prometheus`) and the CLI warns accordingly.
Because `source` is hash-chained, an overridden reading is permanently
distinguishable from a preset-attested one. Note the honesty bound either way:
the gate and the watts value come from separate instant queries (different scrape
instants, and with overrides potentially different metrics), so the gate proves a
real power interface exists at gate time but does not bind the watts sample to the
gated series — the same capture-time bound ADR-0010 concedes for the chain.

The supported record API accepts only process-local observations sealed by this
collector. That capability prevents ordinary library callers from minting a
clean `source=prometheus` chain from arbitrary values; it is an API boundary,
not cryptographic remote attestation. Calibration and exporter consumers open
the store read-only, verify the complete energy chain in the same database
snapshot, validate every decoded row, and refuse tampered or incomplete data.

`pat observe verify` now walks **both** chains and reports each:

```
── Serving observation chain ──
   Observation chain — verified
── Measured energy chain ──
   Energy observation chain — verified
```

Exit 0 when both are intact and covered, 1 when either is broken, 2 on
incomplete coverage. Fit the energy parameters straight from the chained store
with `pat calibrate --energy-from-store` (mutually exclusive with
`--energy-observation`), and export the latest measured watt per (layer, meter)
as the `pat_measured_power_watts` gauge — labelled *measured*, distinct from the
*modelled* `pat_power_watts`. E1a in one sentence: **an unmeasured watt cannot
enter the store.**

---

## Energy & carbon budgeting (v0.22.0)

The arc's conceptual payoff: the SEANERGYS objective function — *compute the most
you can within an energy budget*, and *spend the least energy for a given output*
— plus carbon awareness. Everything here is a **modelled estimate** (the analytic
energy model × a documented grid-intensity figure); per invariants A1/E1/E1a it
is emit-only, never enters the observation/energy chains, and is never signed.

### `pat budget` — both directions

*Direction 2* (default) — the minimum modelled energy that still meets demand
("less energy, same output"). Reuses the `analyze` sweep and honours the
calibration commitment exactly:

```bash
pat budget -r 500 -l 80
```

```
Minimum energy meeting demand  (window 1 h)
Direction 2 — least watt-hours that saturate the workload
┏━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━┓
┃ Layer      ┃ Replicas ┃ Energy (Wh) ┃ J/req ┃  EEI ┃
┃ container  ┃        6 ┃          76 ┃ 0.152 ┃ 5.04 ┃  ✓ recommended
┃ node       ┃        7 ┃        88.6 ┃ 0.177 ┃ 5.57 ┃
┗━━━━━━━━━━━━┻━━━━━━━━━━┻━━━━━━━━━━━━━┻━━━━━━━┻━━━━━━┛
```

*Direction 1* — maximise throughput while modelled `E(δ) = W(δ)·H` stays within a
watt-hour (or carbon) budget over a window. A layer whose single replica already
exceeds the budget is reported **infeasible** and excluded from the
recommendation:

```bash
pat budget -r 500 -l 80 --energy-budget-wh 40 --window-h 1
pat budget -r 500 -l 80 --carbon-budget-g 3300 --region eu-central-1   # grams → Wh
```

Add `--region` for gCO₂/req and gCO₂/window columns to either direction, and
`--json` for an additive machine-readable result (with the intensity source).

### Grid carbon intensity

Region → gCO₂eq/kWh, resolved as **location-based annual average** — the right
basis for cross-region placement ranking (market-based accounting obscures grid
physics; marginal intensity answers a different, intra-day question). The
embedded table is a **documented MVP-placeholder snapshot** (2023), coherent with
[Ember](https://ember-energy.org/) yearly country averages (CC-BY-4.0) and
Google's `region-carbon-info` 2023 dataset — Nordic regions ≈ 46, Germany
≈ 345, Virginia ≈ 322, Singapore ≈ 369 gCO₂eq/kWh. Set `PAT_CARBON_TOKEN` (env
only, never a flag, never logged) to prefer a live Electricity Maps reading,
cached atomically and owner-only in `~/.pat/carbon-cache.json` (TTL 1 h); redirects
are refused so the token cannot be forwarded to another host, response bodies are
bounded, and a live failure never
fails the command — it warns and falls back to the static snapshot. Every
untrusted intensity (a live response or a cache entry) is bounds-validated —
finite and `0 < v ≤ 2000 gCO₂eq/kWh` — so a malformed or poisoned value is
refused and resolution falls back to the cited static snapshot. Unknown regions
fail closed.

### `pat what-if --energy-aware` — the idle-energy flip

Weighs the standing energy of warm `minReplicas` over the simulated window
against the trough's missed-request revenue loss, with a verdict that flips:

```bash
pat what-if -r 300 -s 900 -l 80 -c node --energy-aware \
  --cost-per-request 0.001 --electricity-cost-per-kwh 0.12 --region eu-central-1
```

```
Idle energy vs trough
STANDING ENERGY  (warm minReplicas over the simulated window)
  Standing E    0.47 Wh  (~$0.0001)
  Standing CO₂  0.17 gCO₂eq @ 345 gCO₂eq/kWh (static 2023 average)
TROUGH
  Trough cost   ~$9.0900 revenue impact
The trough loses more in revenue ($9.0900) than warm replicas cost in
standing energy ($0.0001). Keeping minReplicas warm is justified.
```

Without `--energy-aware`, the output is byte-identical to before.

### `pat cost --carbon` — greenest + cheapest

Adds gCO₂/req and gCO₂/hour columns (modelled watts × resolved intensity) and a
combined **cheapest-greenest** rank: cost/req and gCO₂/req are each min-max
normalised to [0,1] across layers, and the layer with the lowest mean wins.
Requires an explicit `--region`; works on the spot/reserved tiered paths too.

```bash
pat cost -r 500 -l 80 -c container --carbon --region eu-central-1
```

### `pat scaler --signal energy`

Emit-only KEDA / Prometheus-Adapter config that scales OUT when the modelled
`pat_energy_per_request_joules` gauge exceeds a J/req budget:

```bash
pat scaler -t web --prometheus-url http://prom:9090 \
  --signal energy --energy-budget-j-per-req 0.5 -c container
```

```yaml
# ...
#   query: max(pat_energy_per_request_joules{layer="container"})
#   threshold: "0.5"
# Caveat: adding replicas amortises standing power only when the layer's EEI > 1.
# Emit-only (A1/E1): pat models and emits, it never actuates power.
```

J/req (not EEI) is the trigger because it has a natural absolute budget
threshold. The default `--signal replicas` is unchanged.

---

## Sign the watt (v0.24.0)

The measured-energy chain proves the local history was not rewritten *relative
to its own head* — but nothing outside pat had ever seen that head. v0.24.0
closes the loop: it emits the measured energy as a **key-less**
`presidio-hardened/energy-reading@1` record that carries the energy-chain **head
hash**. Once a signing-bridge sidecar signs it and it is published, any later
silent rewrite of the local store no longer reproduces the anchored head — the
rewrite becomes externally detectable. This discharges the anchoring deferral
recorded in ADR-0010.

```bash
# Derive one reading from the measured-energy observations of the last hour
# and print the unsigned record. arch-translucency holds NO signing key.
pat energy-evidence-emit --window-minutes 60
```

```json
{"schema":"presidio-hardened/energy-reading@1","attested_content":{"window_start":"2026-07-18T10:00:00+00:00","window_end":"2026-07-18T10:06:00+00:00","energy_wh":"14.25","mean_power_w":"142.5","meter":"rapl","energy_chain_head":"9f2c…","layer":"node"},"content_hash":"…","source":"presidio-hardened-arch-translucency","source_version":"0.24.0","generated_at":"2026-07-18T10:06:01+00:00"}
```

The figures come **exclusively from the preset-attested store** (corollary
**E1a**: *pat never signs a watt it did not measure*). There is deliberately no
override flag for the energy numbers; a window containing any
`prometheus-override` row is refused, one reading carries one meter and one
layer, and emission is gated on a **clean** energy-chain verify — a broken chain
is never anchored. Selection is the **span-overlap closure** of the requested
window — every measurement whose span `[timestamp − window_s, timestamp]` reaches
into the emitted interval is pulled in and checked — so the signed window covers
exactly the rows that were scanned and no `prometheus-override` sliver can escape
the E1a check. Multiple rows must form one exactly consecutive window: overlap
would double-count energy and a gap would claim unmeasured coverage, so either
condition fails closed. `energy_wh` and `mean_power_w` are also checked against
the elapsed window before hashing. As always, the record is unsigned: pipe it to the signing
bridge, which adds the Ed25519 signature that downstream family consumers verify
fail-closed.

For periodic anchoring, `pat observe verify` gains `--emit-head`: it sends the
normal two-chain report to stderr and, on a clean energy walk with at least one
continuous row span, emits only
an `energy-reading@1` for the full store window. Both emit paths derive their
report, rows, and anchoring head from **one explicit transaction on a read-only
snapshot**, so the
anchored `energy_chain_head` is the last verified link and a row appended after
the snapshot is outside both the figures and the head by construction.

```bash
pat observe verify --emit-head | evidence-bridge-sign
```

The honest bound is unchanged and worth restating plainly: a clean chain plus an
external anchor proves the recorded history **was not rewritten after the fact** —
it does **not** attest that the meter was honest at capture time. Capture-time
honesty remains outside pat's trust boundary by design.

This completes the Energy Arc: **model** the watt (v0.20) → **measure** it
(v0.21) → **budget** it (v0.22) → **train** on it (v0.23) → **sign** it (v0.24).

---

## Roadmap

| Version | Theme |
|---|---|
| v0.1.0 | MVP — layer analysis & recommendation |
| v0.2.0 | Multi-Python CI hardening |
| v0.3.0 | HPA lag model (`pat what-if`, `pat slo`) |
| v0.4.0 | Cost-aware replication analysis (`pat cost`) |
| v0.5.0 | Cloud billing integration — AWS on-demand pricing |
| v0.6.0 | Cloud billing — AWS reserved/spot + GCP + Azure |
| v0.7.0 | Autoresearch — `pat calibrate` + observation store + SMA predictions |
| v0.8.0 | Autoresearch — `pat observe`/`pat optimize`, Prometheus source, ARIMA + HPA patch emitter |
| v0.9.0 | Per-layer + Docker-benchmark `pat calibrate`, ARIMA order bounds, observe daemon, security audit |
| v0.10.0 | Monitoring integration — read-only Prometheus exporter (`pat export`) + Grafana dashboard |
| v0.11.0 | Alerting — `pat rules` emits Prometheus recording + alerting rules |
| v0.12.0 | Visualize & Annotate — Grafana provisioning + `pat annotate` |
| v0.13.0 | Speak OTLP — vendor-neutral `pat export --otlp` (hand-rolled OTLP/HTTP+JSON, ADR-0006) |
| v0.14.0 | Reach ephemeral contexts — `pat export --pushgateway` |
| v0.15.0 | Close the loop — `pat scaler` (KEDA ScaledObject / HPA on pat's forecast) |
| v0.16.0 | Package & operate — `charts/pat-exporter` Helm chart (Deployment + ServiceMonitor + PrometheusRule + Grafana dashboard) + Dockerfile |
| v0.17.0 | Evidence arc · Sign the signal — `evidence_producer` + `pat evidence-emit` (L-EV-3): runtime-posture degradation as signed `evidence-ref@1`, consumed by `presidio-hardened-x402`; key-less daemon, signing in a sidecar |
| v0.18.0 | Training arc · Same question, new domain — ML training parallelism (`pat train-analyze`/`train-what-if`: data / fsdp / tensor / pipeline, memory as hard constraint) + `training-run@1` evidence with provenance parents (`pat train-evidence-emit`) |
| v0.19.0 | Evidence-hardening arc — hash-chained observations (`pat observe verify`) with strict tamper vs legacy exit codes + calibration commitments binding fitted models to their source observations |
| v0.19.1 | Evidence-hardening patch release — publishes ADR-0010 with remediated Actions pins and CodeQL code-scanning alert fixes |
| v0.20.0 | Model the watt — per-layer energy model (α_E/β_E), `pat analyze` Watts/J-per-req/EEI columns, `pat calibrate --energy-observation` fit bound by the calibration commitment; measures/models energy, never actuates (E1) |
| v0.21.0 | Measure the watt — measured-energy store + parallel hash chain (`pat observe --energy`, `verify` walks both chains), platform-gated fail-closed direct hardware presets (RAPL/DCGM; Kepler attribution is refused), verified read-only consumers, `pat calibrate --energy-from-store`, modelled + measured exporter gauges, energy alert rules |
| v0.22.0 | Budget the watt — `pat budget` (max output within a Wh/carbon budget, or least energy for the demand), region carbon intensity (`carbon.py`, live Electricity Maps + static snapshot), `pat what-if --energy-aware` idle-vs-trough flip, `pat cost --carbon` cheapest-greenest rank, `pat scaler --signal energy`; all modelled, never signed (E1a) |
| v0.23.0 | Train the watt — `pat train-calibrate` (L-TR-1): fit training α/β overhead from committed JSON-Lines step logs, `samples/s/W` ranking in `train-analyze`/`train-what-if` with an energy-best marker, `training-run@1` optional producer-attributed energy fields (`--energy-wh`/`--mean-power-w`); training-fit tamper fails closed, energy stays a modelled/producer claim (E1a) |
| **v0.24.0** | **Sign the watt — Energy Arc finale: `pat energy-evidence-emit` + `pat observe verify --emit-head` emit key-less `energy-reading@1` carrying the measured-energy chain head hash (`build_energy_reading`, chain-head accessors); store-only figures, `prometheus-override` rows refused, emission gated on a clean chain walk (E1a); external anchoring discharges the ADR-0010 deferral — post-hoc rewriting becomes externally detectable** |
| **v0.24.1** | **Family-vector conformance patch — pin the nominal Kepler energy payload and energy-bearing training payload to the merged `presidio-evidence` vectors without weakening PAT's audited Kepler emission refusal** |

Full deliberation and feature details: [PRESIDIO-REQ.md](PRESIDIO-REQ.md)

---

## License

MIT — see [LICENSE](LICENSE).

## References

- V. Stantchev, "Effects of Replication on Web Service Performance in WebSphere,"
  Technical Report, ICSI — International Computer Science Institute, Berkeley, CA, USA.
- V. Stantchev, C. Schröpfer, "Negotiating and Enforcing QoS and SLAs in Grid and
  Cloud Computing," in *Advances in Grid and Pervasive Computing* (GPC 2009),
  Lecture Notes in Computer Science, vol. 5529, Springer, 2009.
- V. Stantchev, M. Malek, "Architectural translucency in service-oriented
  architectures," *IEE Proceedings — Software*, vol. 153, no. 1, pp. 31–37, 2006.
  DOI: 10.1049/ip-sen:20050017

---

## SDLC

This repository is developed under the Presidio hardened-family SDLC:
<https://github.com/presidio-v/presidio-hardened-docs/blob/main/sdlc/sdlc-report.md>.
