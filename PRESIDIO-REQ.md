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
| v0.7.0 | Autoresearch — `pat calibrate` + cost/α-β fixes | Released |
| v0.8.0 | Autoresearch — `pat observe`/`pat optimize`, Prometheus source, ARIMA + HPA patch | Released |
| v0.9.0 | Per-layer calibrate, kubeconfig Prometheus auth, configurable ARIMA order | Released |
| v0.10.0 | Monitoring integration — Prometheus exporter + official Grafana dashboard | Deliberated |

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

### Design decisions (locked 2026-06-10)

Context: v0.7.0 shipped `pat calibrate` (analytical mode) plus the cost-precision
and α/β-recalibration fixes, but deferred the rest of the autoresearch scope
(`pat observe`, `pat optimize`). v0.8.0 builds that foundation and then layers
Prometheus + ARIMA on top. The following decisions are locked.

**D1 — Foundation-first sequencing.** Build the autoresearch base before the
advanced models: (1) `pat observe` SQLite store + source-agnostic ingestion,
(2) `pat optimize --model sma` over that store, then (3) Prometheus as an
observation source and (4) ARIMA as an opt-in model on top. This honours the
existing "SMA before ARIMA" cross-cutting decision, and ARIMA's
`<30 samples → SMA` fallback hard-requires SMA to exist first.

**D2 — `pat observe` process model: cron/launchd, single-shot.** `pat observe`
is a single-shot collection script — it takes one measurement (or one Prometheus
scrape), appends it to the store, and exits. It is **not** a daemon and **not** a
foreground polling loop. Users schedule recurring collection externally (cron,
launchd, a Kubernetes CronJob, CI). This keeps the tool stateless, testable, and
crash-safe, and avoids owning a long-running process. The earlier
`--duration/--interval` framing is superseded by this decision.

**D3 — Prometheus auth: env token only for v0.8.0.** Prometheus authentication
uses the bearer token from `PAT_PROMETHEUS_TOKEN` only. kubeconfig-based auth (and
the optional `kubernetes` client dependency) is **deferred** beyond v0.8.0. Tokens
are never accepted as CLI args and never logged.

**D4 — Calibrate: build to the original spec in v0.8.0.** Extend the shipped
calibrate (global concurrency κ + β, analytical-only) toward the original
contract as new work: add **per-layer α/β fitting** via `--layer web/worker/cache/db`
and a **Docker benchmark mode** that runs controlled replica sweeps and fits from
the measured results. The v0.7.0 analytical, global-fit path remains supported.

**D5 — Model file location: project-local overrides global.** Fitted parameters
may live in a project-local `.pat-model.json` (cwd) and/or a global
`~/.pat/model.json`. The loader checks **project-local first and falls back to
global** — project-local overrides global. (This matches the behaviour already in
`model._model_search_paths`; the divergence in earlier notes that implied a single
location is resolved in favour of this two-tier scheme.)

### v0.8.0 Delivery (shipped 2026-06-10)

Released as **v0.8.0** on 2026-06-10. Built foundation-first per D1, one PR per
phase. This record closes the loop between the spec/decisions above and what
actually shipped.

#### What shipped (Phases 1–5)

| Phase | Deliverable | PR | Notes |
|---|---|---|---|
| 1 | `pat observe` — source-agnostic SQLite store | #26 | `~/.pat/observations.db`; record one measurement or `--list` recent; `--db` override; `--source` tag |
| 2 | `pat optimize --model sma` | #27 | SMA over `--window` recent samples, projects `--horizon-minutes` ahead, recommends replicas |
| 3 | Prometheus observation source | #28 | `pat observe --prometheus <url> --layer <layer>`; PromQL matches spec (`sum(rate(http_requests_total[1m]))`, `histogram_quantile(0.99, …)`, `count(up == 1)`); token from `PAT_PROMETHEUS_TOKEN` env only |
| 4 | `pat optimize --model arima` | #29 | `statsmodels` ARIMA, order AIC-minimised over a bounded grid (p,q ∈ [0,3], d ∈ [0,2]); 95% CI bands + replica range; auto-falls back to SMA below 30 samples |
| 5 | `pat optimize --emit-hpa-patch` | #30 | Emits a sanitised `HorizontalPodAutoscaler` to stdout; `minReplicas` = point estimate, `maxReplicas` = ARIMA upper-CI bound when available |

All security commitments from the spec held: the Prometheus token is read from
`PAT_PROMETHEUS_TOKEN` only (never a CLI arg, never logged), and the emitted HPA
manifest is sanitised (target/namespace validated as RFC 1123 names, no raw user
input echoed).

#### Deviations from the original 2026-03-27 spec

1. **`pat observe` process model — single-shot, not a daemon.** The original
   spec framed collection as `pat observe --duration 1h --interval-minutes 1`
   (a long-running poller). Per **D2** (locked 2026-06-10), `pat observe` ships
   as a single-shot command — one measurement (or one Prometheus scrape) per
   invocation, scheduled externally via cron/launchd/CronJob. The
   `--duration`/`--interval` flags were not implemented.

2. **D4 (extended `pat calibrate`) deferred — not delivered in v0.8.0.** D4
   called for extending calibrate toward the original contract in v0.8.0:
   **per-layer α/β fitting** (`--layer web/worker/cache/db`) and a **Docker
   benchmark mode** (`--benchmark`) that runs controlled replica sweeps. Neither
   shipped. The v0.7.0 analytical, global-fit path (`--observation
   rps:latency:replicas`, fitting global concurrency κ + β) remains the only
   calibrate mode. **Carried to v0.9.0.** Rationale: the observe→optimize loop
   (D1's higher-priority autoresearch base) consumed the v0.8.0 sprint, and the
   shipped analytical calibrate is sufficient to seed the model for `pat
   optimize`.

3. **HPA emitter requires `--target`.** The spec sketched `pat optimize
   --model arima --emit-hpa-patch > hpa-patch.yaml` with no target. The shipped
   command requires `--target <deployment>` (and accepts `--namespace`) because
   a valid `HorizontalPodAutoscaler` must name its `scaleTargetRef`. This is a
   refinement, not a reduction, of scope.

4. **kubeconfig auth + optional `kubernetes` client — deferred (as planned).**
   Per **D3**, Prometheus auth is env-token-only for v0.8.0; kubeconfig-based
   auth and the optional `kubernetes` dependency remain deferred beyond v0.8.0.

#### Python 3.9 deprecation (out-of-band, #32)

Not part of the original autoresearch scope. During the sprint, GitHub
Dependabot flagged **19 vulnerability alerts** on the default branch, all in
transitive dependencies whose patched releases require Python ≥ 3.10
(`requests`, `urllib3`, `filelock`, and others pinned by `matplotlib`/`pillow`
on 3.9). Resolving them required dropping Python 3.9. `requires-python` is now
`>=3.10`; the CI matrix, trove classifiers, and `ruff target-version` are
3.10–3.12; `uv.lock` was regenerated with all 19 alerts cleared. (A stale
"Test (Python 3.9)" required status check in branch protection was also removed
so PRs could merge.)

#### Carried to v0.9.0 planning

- **D4 calibrate extension** — per-layer α/β fitting and Docker benchmark mode
  (the only unshipped locked decision from this cycle).
- Optional kubeconfig auth for Prometheus (D3 follow-on).

---

## v0.10.0 — Monitoring Integration: Prometheus Exporter + Grafana Dashboard

**Deliberated:** 2026-06-17

### Roadmap fork considered

Two future directions were weighed: **(a) adding a GUI** vs. **(b) integrating with
standard monitoring tools (Grafana et al.)**. Direction (b) was chosen.

### Rationale

1. **Completes a half-built loop.** `pat` already *ingests* from Prometheus
   (`pat observe --prometheus`) and *emits* Kubernetes-native artifacts
   (`pat optimize --emit-hpa-patch`). Exposing its recommendations and
   predictions *back* as scrapeable metrics closes the observe → predict →
   visualize circuit using infrastructure teams already run.
2. **It is the thesis.** Architectural translucency is "monitor and control
   non-functional properties architecture-wide." Grafana is the surface where
   that monitoring is consumed — this is the concept's natural home, not a
   bolt-on.
3. **Preserves the hardened posture.** A read-only Prometheus exporter adds no
   auth system, no write paths, and no new injection surface. A standalone GUI
   (web or desktop) would mean auth, sessions, CSRF/XSS surface, frontend CVE
   exposure, and cross-OS packaging — the opposite of the Presidio security
   mandate every prior version has guarded.
4. **Near-false dichotomy.** A Grafana dashboard *is* the visual interface a
   "GUI" would provide, but delivered through infra users already operate and
   secure. It captures the bulk of the GUI value at a fraction of the cost and
   zero new attack surface.
5. **Small, shippable increment.** Reuses existing `model`/`cost`/`optimize`
   code; the new code is metric exposition + a dashboard. Fits a single release.

A dedicated GUI is **deferred** — revisit only if real users hit a wall the
dashboard cannot cover, and even then prefer a thin read-only web view over a
stateful app.

### Scope decision

Exposition model: **Prometheus exporter** (pull-based `/metrics` endpoint),
chosen over a Grafana JSON datasource or a dashboard-only deliverable. Pull-based
exposition is idiomatic for the cloud-native stack, stateless, and adds no write
paths. An official Grafana dashboard JSON ships alongside it.

### New CLI surface

```bash
# Expose pat's recommendations/predictions as Prometheus metrics
pat export --port 9847 \
    --requests-per-second 500 --avg-latency-ms 80 --current-layer container
```

### Proposed metrics (illustrative — finalize at build time)

| Metric | Type | Labels |
|---|---|---|
| `pat_recommended_replicas` | gauge | `layer` |
| `pat_predicted_rps` | gauge | `model` (sma/arima) |
| `pat_predicted_rps_upper` / `_lower` | gauge | `model` (CI bands) |
| `pat_throughput_gain_ratio` | gauge | `layer` |
| `pat_cost_per_request` | gauge | `layer`, `cloud`, `region` |
| `pat_response_time_ms` | gauge | `layer` |

### Deliverables

- `pat export` command exposing the metrics above over HTTP `/metrics`.
- Official Grafana dashboard JSON (committed to the repo, e.g. `grafana/`)
  built on those metrics.
- README section: wiring `pat export` into a Prometheus scrape config and
  importing the dashboard.

### Security

- **Read-only.** The exporter serves metrics only — no mutation endpoints.
- Bind to `127.0.0.1` by default; explicit opt-in flag required to bind a
  routable interface.
- No secrets, no raw user input, and no auth tokens in exposed metric labels or
  values (output sanitization rules from prior versions carry over).
- No new authenticated surface introduced in v0.10.0.

### Deferred

- Dedicated standalone GUI (web/desktop) — revisit only on demonstrated need.
- Grafana JSON datasource and push/remote-write exposition models.

---

## Cross-cutting decisions

| Decision | Rationale |
|---|---|
| `~/.pat/` as global store | Consistent home for cache, observations, and fitted models |
| `.pat-model.json` (project-local) overrides `~/.pat/model.json` (global) | Fitted params are environment-specific; project-local wins, global is the fallback (see D5) |
| Analytical calibrate mode from v0.7.0 | CI compatibility — no Docker daemon required |
| SMA before ARIMA | Ship fast, validate prediction usefulness before adding complexity |
| On-demand before reserved/spot | Universally available without credentials |
| AWS before GCP/Azure | Largest K8s adoption share; proves the integration pattern first |
| Grafana integration before a dedicated GUI | A dashboard delivers the visual-interface value through infra users already run, at lower cost and zero new attack surface |
| Read-only Prometheus exporter over a web app | Pull-based exposition is idiomatic and adds no auth/write surface, preserving the hardened posture |

## SDLC

These requirements are delivered under the family-wide Presidio SDLC:
<https://github.com/presidio-v/presidio-hardened-docs/blob/main/sdlc/sdlc-report.md>.
