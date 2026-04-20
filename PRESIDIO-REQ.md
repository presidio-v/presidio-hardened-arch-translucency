# Presidio-Hardened Toolkit: presidio-hardened-arch-translucency

## Overview
Build a production-ready Python CLI tool named `presidio-hardened-arch-translucency` that implements MVP 0.1.0 of the "architectural translucency" concept (defined 20 years ago by Vladimir Stantchev). It analyzes where replication should be applied (new Docker container vs. Kubernetes Pod vs. multi-node Deployment with replicas) to maximize performance (throughput ω(δ) and response time) for a given workload.

Architectural translucency is the ability to monitor and control non-functional properties (especially performance) architecture-wide in a cross-layered way. It states that the same measure (e.g. replication) has different implications on throughput and response time when applied at different layers. Key layers for Docker/K8s: container level (new container), Pod level, Deployment/ReplicaSet level, or cluster-node level. The goal is to choose the layer that gives the highest performance gain with the lowest overhead.

Target: Docker and Kubernetes deployments in cloud-native environments.

Users run `pat analyze --requests-per-second 500 --avg-latency-ms 80 --current-layer container` and receive a layer recommendation with estimated throughput/response-time improvement.

## Mandatory Presidio Security Extensions
- Input sanitization for all workload parameters (bounds checking, type validation, rejection of malformed inputs)
- Secure logging of replication recommendations (no sensitive data, no secrets in log output)
- On-run CVE/dependency check for Docker/K8s client libraries (pip-audit or safety check on startup)
- Security event logging ("Presidio architectural-translucency recommendation applied")
- Rate-limit / abuse guard on CLI invocations (configurable max calls per session)
- Strict output sanitization: recommendations never echo raw user input without escaping
- Full GitHub security files: SECURITY.md, .github/dependabot.yml, .github/workflows/codeql.yml + pytest workflow

## Technical Requirements
- Python 3.9+
- Modern pyproject.toml + hatchling/uv + Typer CLI
- src/presidio_arch_translucency/ layout
- Simple CLI: `pat analyze --requests-per-second 500 --avg-latency-ms 80 --current-layer container`
- Returns recommendation + estimated throughput/response-time improvement
- Basic simulation model based on the original equations from the papers (ω(δ) = f(ι(δ)), response time = 1/ω)
- 80%+ test coverage with pytest
- README.md with side-by-side examples and clear reference to the architectural translucency concept
- LICENSE = MIT
- Version = 0.1.0

## Workflow Rules (always follow)
1. First create or update PRESIDIO-REQ.md from this template (adapt for the specific toolkit).
2. Manually remove or comment out the final "Deliver the complete working project ready for GitHub publish." line.
3. Implement file-by-file in logical order.
4. After every major section run validation commands (ruff format . && ruff check . --fix && pytest) and fix all issues automatically.
5. When complete, reply exactly: "BUILD COMPLETE – ready for publish"

<!-- Deliver the complete working project ready for GitHub publish. -->

---

# Version Registry & Deliberation Log

Every deliberation about future versions and roadmap is persisted here.

---

## Roadmap Summary

| Version | Theme | Status |
|---|---|---|
| v0.1.0 | MVP — Layer analysis & recommendation | Released |
| v0.2.0 | Refactor & multi-Python CI hardening | Released |
| v0.3.0 | HPA lag model (`pat what-if`, `pat slo`) | Released |
| v0.4.0 | Cost-aware replication analysis (`pat cost`) | Released |
| v0.5.0 | Cloud billing integration — AWS on-demand | Released |
| v0.6.0 | Cloud billing — reserved/spot + GCP + Azure | Released |
| v0.7.0 | Autoresearch — `pat demo` observation + simple moving average | Planned |
| v0.8.0 | Autoresearch — Prometheus integration + ARIMA time-series model | Planned |

---

## v0.5.0 — Cloud Billing Integration (AWS, On-Demand)

**Deliberated:** 2026-03-27

### Scope decision
On-demand pricing only. AWS only. Reserved/Spot and other providers deferred to v0.6.0.

### Goal
Replace manual `--cost-per-X-hour` inputs with live on-demand pricing fetched from the
AWS public Pricing API (no auth required for on-demand rates).

### New CLI surface
```bash
# Auto-fetch AWS on-demand pricing by instance type
pat cost --cloud aws --region us-east-1 --instance-type m5.large \
    --requests-per-second 500 --avg-latency-ms 80

# Fargate/serverless-pod pricing
pat cost --cloud aws --region us-east-1 --fargate \
    --vcpu 0.5 --memory-gb 1 \
    --requests-per-second 500 --avg-latency-ms 80
```

### Layer-to-AWS pricing mapping
| Layer | AWS pricing source |
|---|---|
| `container` | Fargate task (fractional vCPU/memory) or EC2 fraction |
| `pod` | Fargate task (full task unit) |
| `deployment` | EC2 instance (EKS worker node) |
| `node` | EC2 instance (dedicated/standalone) |

### Pricing data source
- AWS Pricing API — public JSON endpoint, no auth needed for on-demand
- EC2: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/<region>/index.json`
- Fargate: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonECS/current/index.json`

### Local price cache
- Path: `~/.pat/pricing-cache.json`
- TTL: 24 hours
- Offline fallback: use last cached prices + emit timestamped warning
- Cache keyed by `(provider, region, instance_type)`

### New flags
| Flag | Description |
|---|---|
| `--cloud aws` | Activate cloud pricing mode |
| `--region` | AWS region (e.g. `us-east-1`) |
| `--instance-type` | EC2 instance type (e.g. `m5.large`) |
| `--fargate` | Use Fargate pricing instead of EC2 |
| `--vcpu` | vCPU allocation (Fargate mode) |
| `--memory-gb` | Memory allocation in GB (Fargate mode) |
| `--no-cache` | Bypass local pricing cache |

### Security
- No AWS credentials required (public pricing API)
- Never accept API keys via CLI flags — env vars only if needed in future versions
- Cache file must not store any auth tokens

---

## v0.6.0 — Cloud Billing: Reserved/Spot + GCP + Azure

**Deliberated:** 2026-03-27

### Scope decision
Extends v0.5.0 with: (1) reserved and spot/preemptible pricing tiers for AWS,
(2) full GCP support, (3) full Azure support. On-demand remains the default.

### Reserved & Spot pricing (AWS)
- 1-year and 3-year reserved instance pricing columns (shown with `--show-reserved`)
- Spot pricing from EC2 Spot Price History API (requires env: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`)
- Spot shown with risk annotation: `⚠ spot — interruption risk`

### GCP integration
| Layer | GCP pricing source |
|---|---|
| `container` | Cloud Run per-request / Autopilot pod |
| `pod` | GKE Autopilot pod pricing |
| `deployment` | GKE Standard node |
| `node` | GKE Standard node (dedicated) |

- Public API: GCP Cloud Billing Catalog REST — no auth required
- Preemptible VM pricing shown with `--spot` flag

### Azure integration
| Layer | Azure pricing source |
|---|---|
| `container` | Azure Container Instances per container |
| `pod` | ACI per container group |
| `deployment` | AKS node |
| `node` | AKS node (dedicated) |

- Public API: Azure Retail Prices API (`https://prices.azure.com/api/retail/prices`) — no auth
- Spot: Azure Spot VMs shown with `--spot` flag

### New flags
| Flag | Description |
|---|---|
| `--cloud gcp` / `--cloud azure` | Activate GCP or Azure pricing |
| `--show-reserved` | Add 1yr/3yr reserved columns to output |
| `--spot` | Include spot/preemptible pricing column |

### Cache extension
Spot prices TTL 5 minutes (volatile). Reserved and on-demand TTL 24h.

---

## v0.7.0 — Autoresearch: `pat demo` Observation + Simple Moving Average

**Deliberated:** 2026-03-27

### Scope decision
Observation source: `pat demo` measurements only (Docker-based). No Prometheus in this version.
Prediction model: simple moving average (SMA). ARIMA and Prometheus deferred to v0.8.0.

### `pat calibrate` — Model self-calibration
Fits layer-specific α (fixed overhead) and β (coordination cost) in the intensity formula
`ι(δ) = rps/δ + α·rps + β·rps·ln(δ)` using `scipy.optimize.curve_fit`.

Two modes — both supported from v0.7.0:

**Analytical mode** (no Docker required): user supplies measured throughput/latency at two
or more replica counts from any source (APM, load tests, prior `pat demo` output).
```bash
pat calibrate \
  --layer container \
  --measurements "1:500rps:80ms,2:920rps:45ms,4:1600rps:28ms"
```

**Benchmark mode** (Docker required): runs controlled sweeps across replica counts, then fits.
```bash
pat calibrate --layer container --replicas 1 2 4 8 --duration-s 30 --benchmark
```

Output: fitted α, β, confidence interval, residual error. Persisted to `.pat-model.json`.

### `pat observe` — Rolling measurement collection
Runs `pat demo` at configurable intervals and stores results in a local SQLite database.
```bash
pat observe --interval-minutes 5 --duration 1h
```
Storage: `~/.pat/observations.db`. Schema: timestamp, rps, avg_latency_ms, p99_latency_ms,
throughput, layer, replicas.

### `pat optimize` — SMA-based proactive recommendation
Reads observation history, applies SMA over last N samples (default 10) to smooth noise,
outputs a proactive scaling recommendation.
```bash
pat optimize --window 10
```
Example output:
```
Based on 10 observed samples (SMA):
  Trend:      +12% throughput demand over last 50 min
  Predicted:  ~680 req/s in ~10 min
  Recommend:  Scale container → 5 replicas in ~8 min
```

### Persistence layout
```
.pat-model.json          # project-local fitted α/β params
~/.pat/observations.db   # global rolling measurement store (SQLite)
~/.pat/pricing-cache.json  # from v0.5.0
```

### Findings from 2026-04-20 dogfood (incorporate into v0.7.0 scope)

Two observations surfaced while dogfooding the `pat` Agent Skill against a
realistic K8s scaffolding scenario (500 req/s, 80 ms, AWS m5.large):

1. **Default α/β may be over-aggressive.** `pat analyze -r 500 -l 80 -c container`
   recommends **64 replicas**, implying each replica is modelled at ~12 req/s of
   capacity. That is plausible for a single-threaded Python worker but wrong for
   a typical async service. Action: during `pat calibrate` development, validate
   the current defaults against ≥2 reference workloads (async Python, Go) and
   either (a) ship improved defaults or (b) make `pat analyze` warn when no
   local `.pat-model.json` exists and the workload profile is outside the
   default's validity envelope.

2. **Cost/request display precision is too coarse.** The `pat cost` top panel
   shows `Cost/request: $0.000000` at very low per-request costs — the value is
   truncated to 6 decimals. Action: widen to 7–8 decimals (or switch to
   scientific notation below $1e-6) in the top-panel Cost/request field and
   per-layer table. Tiny fix, should ship with v0.7.0 alongside `pat calibrate`.

---

## v0.8.0 — Autoresearch: Prometheus Integration + ARIMA

**Deliberated:** 2026-03-27

### Scope decision
Extends v0.7.0 with: (1) Prometheus/metrics-server as live observation source,
(2) ARIMA time-series model replacing SMA in `pat optimize`.

### Prometheus integration
```bash
pat observe --prometheus http://prometheus.monitoring.svc:9090 \
    --duration 1h --interval-minutes 1
```
- Queries: `rate(http_requests_total[1m])`, `histogram_quantile(0.99, ...)`, pod count
- Auth: bearer token from env `PAT_PROMETHEUS_TOKEN` or kubeconfig — never a CLI arg
- `pat demo` observation remains available as fallback

### ARIMA model
```bash
pat optimize --model arima --horizon-minutes 15
```
- `statsmodels` ARIMA(p,d,q), order auto-selected by AIC minimisation
- Output includes 95% confidence interval bands
- Automatic fallback to SMA if fewer than 30 samples available

### HPA patch YAML output
```bash
pat optimize --model arima --emit-hpa-patch > hpa-patch.yaml
kubectl apply -f hpa-patch.yaml
```
Emitted YAML sanitized — no secrets, no raw user input echoed.

### New dependencies
- `statsmodels` — ARIMA
- Direct HTTP to Prometheus API (no heavy client library)
- `kubernetes` Python client (optional, for kubeconfig auth)

### Security
- Prometheus token from env `PAT_PROMETHEUS_TOKEN` only
- kubeconfig path validated and never logged
- HPA YAML output sanitized before emission

---

## Cross-cutting decisions

| Decision | Rationale |
|---|---|
| `~/.pat/` as global store | Consistent home for cache, observations, and fitted models |
| `.pat-model.json` as project-local | Fitted params are environment-specific |
| Analytical calibrate mode from v0.7.0 | CI compatibility — no Docker daemon required |
| SMA before ARIMA | Ship fast, validate prediction usefulness before adding complexity |
| On-demand before reserved/spot | Universally available without credentials |
| AWS before GCP/Azure | Largest K8s adoption share; proves the integration pattern first |

## SDLC

These requirements are delivered under the family-wide Presidio SDLC:
<https://github.com/presidio-v/presidio-hardened-docs/blob/main/sdlc/sdlc-report.md>.
