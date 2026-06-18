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
| v0.9.0 | Per-layer + benchmark calibrate, ARIMA order bounds, observe daemon, security audit | Released |
| v0.10.0 | Monitoring arc · Expose — Prometheus exporter + official Grafana dashboard | Released |
| v0.11.0 | Monitoring arc · Alert — `pat rules` recording + alerting rules | Released in v0.13.0 |
| v0.12.0 | Monitoring arc · Visualize & Annotate — Grafana provisioning + `pat annotate` | Released in v0.13.0 |
| v0.13.0 | Monitoring arc · Speak OTLP — vendor-neutral `pat export --otlp` | Released |
| v0.14.0 | Monitoring arc · Reach ephemeral — Pushgateway target (remote-write deferred) | Complete (included in v0.15.0 release) |
| v0.15.0 | Monitoring arc · Close the loop — `pat scaler` (KEDA / HPA on the forecast) | Complete (released) |
| v0.16.0 | Monitoring arc · Package & operate — Helm chart + Grafana panel plugin | Planned |

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

## v0.9.0 — Calibrate Depth, Prediction Tuning, Observe Daemon + Security Audit

**Deliberated:** 2026-03-27 (carried from v0.8.0) · **Backfilled write-up:** 2026-06-17

### Status

Merged to the default branch but **not yet cut as a formal release** — the work
sits under `[Unreleased]` in `CHANGELOG.md` with no `Release v0.9.0` commit or
tag. This section is a backfilled delivery record reconstructed from the merged
PRs (#37–#43) and the changelog, since no fuller v0.9.0 write-up existed at the
time the v0.10.0 deliberation was logged.

### Scope decision

v0.9.0 picks up the locked-but-unshipped work carried out of the v0.8.0 cycle —
the **D4 calibrate extension** and the **D3 follow-on (kubeconfig Prometheus
auth)** — and adds prediction-model tuning, an opt-in observe scheduler, an ADR
backfill, and a full security-audit hardening pass.

### What shipped

| Theme | Deliverable | PR | Notes |
|---|---|---|---|
| Calibrate (D4, per-layer half) | Per-layer `pat calibrate --layer <name>` | #37 | Fits per-layer params into `~/.pat/model.json` under `layers.<name>`, preserving the global pooled fit and other layers. `--show-global` prints both. `analyze`/`what-if`/`slo`/`optimize` take `--layer` (per-layer → global → built-in default). Model file stays backward-compatible (no `layers` key → resolves as before). |
| Prometheus auth (D3 follow-on) | kubeconfig bearer-token auth for `pat observe` | #38 | **Subsequently reverted — see deviations.** |
| Prediction tuning | Configurable ARIMA order bounds + `--auto-diff` | #39 | `--max-p/--max-d/--max-q` set the AIC sweep bounds (defaults 3/2/3 reproduce the prior 4×3×4 = 48-model search). `--auto-diff` replaces the `d` sweep with a dependency-free variance heuristic (raw vs. 1st- vs. 2nd-difference), capped at `--max-d`. Flags affect `--model arima` only. |
| Observe scheduler (extends D2) | `pat observe daemon install/uninstall/status` | #40 | Writes a platform-native scheduler unit — launchd LaunchAgent on macOS, systemd `--user` `.service`+`.timer` on Linux; other platforms error. Accepts `--prometheus`, `--layer`, `--interval` (default 60 s). **Observe stays single-shot** — the scheduler invokes it; it does not become a daemon. No new dependencies. |
| Documentation | Backfilled ADRs 0001–0005 | #41 | Records the v0.8.0 design decisions D1–D5 as ADRs under `docs/adr/`. |
| Security | 2026-06-16 audit hardening pass | #43 | See Security below; documented in `SECURITY-AUDIT-2026-06-16.md`. |
| CI | Bump `codecov/codecov-action` 4.6.0 → 7.0.0 | — | Routine maintenance. |

### Security (audit hardening, #43)

- **Prometheus bearer auth tightened to env-only.** `pat observe --prometheus`
  no longer auto-reads kubeconfig tokens; bearer auth is env-only via
  `PAT_PROMETHEUS_TOKEN`, token use requires an **HTTPS** Prometheus URL, and
  URLs/query strings reject control characters.
- **Daemon unit generation hardened.** Scheduler inputs validated, control
  characters rejected, systemd `ExecStart=` arguments quoted and `%` specifiers
  escaped, generated unit files written owner-only where supported.
- **Demo isolation tightened.** `pat demo` publishes Docker ports to `127.0.0.1`
  only; the embedded workload image runs as an unprivileged user with a
  healthcheck.
- **Local store permissions tightened.** `~/.pat` created owner-only;
  observation and model store files chmod'd `0o600` where supported.
- **Security policy refreshed.** `SECURITY.md` updated with supported versions,
  features, known limitations, and a reference to the 2026-06-16 audit.

### Deviations from plan

1. **Kubeconfig Prometheus auth (D3 follow-on) was added then reverted — net
   not delivered.** Phase 2 (#38) implemented kubeconfig bearer-token auth, but
   the 2026-06-16 security audit (#43) reverted it: auth is now **env-only via
   `PAT_PROMETHEUS_TOKEN`**, requires HTTPS, and rejects control characters.
   Auto-reading tokens from kubeconfig was judged too broad an ambient-credential
   surface for the hardened posture. Net v0.9.0 state matches the original v0.8.0
   **D3** decision (env-token-only). The roadmap-summary one-liner deliberately
   omits "kubeconfig auth" for this reason.
2. **D4 fully delivered.** Per-layer α/β fitting shipped first (#37); the
   **Docker `--benchmark` mode** (controlled replica sweeps that measure the
   operating points and fit from them) landed subsequently in a new `benchmark`
   module that reuses the `demo` Docker harness and feeds the existing analytical
   fitter. Both axes of D4 — per-layer fitting and Docker benchmark — are now
   discharged. (`calibrate` stays Docker-free; all Docker orchestration lives in
   `benchmark`.)
3. **Not formally released.** Work is merged under `[Unreleased]`; cutting a
   tagged v0.9.0 release is outstanding.

### Carried forward

- **Cut a tagged v0.9.0 release** — move `[Unreleased]` entries under a dated
  `[0.9.0]` heading and tag. (Now the only open item for this cycle: with the
  Docker `--benchmark` calibrate mode delivered, every locked decision from the
  v0.8.0/v0.9.0 cycles is discharged.)

---

## Monitoring Integration Arc (v0.10.0 → v0.16.0) — "The Translucency Control Plane"

**Deliberated:** 2026-06-17

The monitoring-integration direction chosen for v0.10.0 is the first step of a
deliberate seven-version arc. `pat` graduates from a CLI advisor into a
monitoring-native control plane: it publishes its model as metrics, alerts on
translucency mismatches, visualizes them, speaks every monitoring dialect, and
ultimately feeds autoscaling — without ever mutating infrastructure directly.

This arc is **directional, not a rigid commitment.** Each version still gets its
own dated deliberation entry (and may re-scope) when it is built. Recorded here
so the through-line is legible and individual versions don't drift.

### Governing invariant (locked 2026-06-17)

**`pat` emits, it never applies.** Across the entire arc, `pat` only *exposes*
metrics and *emits* declarative artifacts (rules, dashboards, scaler configs). It
**never holds write credentials to the cluster and never applies changes
itself** — humans / GitOps / operators apply what it emits. This extends the
existing v0.8 HPA-emitter pattern (`pat optimize --emit-hpa-patch | kubectl
apply`) and is the security spine that keeps "monitoring integration" from
becoming "a daemon with cluster admin." Any future proposal to let `pat` apply
directly must reopen this decision explicitly.

### The seven steps

| Ver | Title | Deliverable | Security step |
|---|---|---|---|
| v0.10.0 | **Expose** | Read-only Prometheus exporter (`pat export` → `/metrics`) + official Grafana dashboard JSON | localhost-default bind, read-only, no new surface |
| v0.11.0 | **Alert** | `pat rules --emit prometheus` → recording + alerting rules from the model (predicted demand > capacity within horizon; cost/req > budget; layer translucency mismatch); Alertmanager routing docs | Declarative YAML, still read-only |
| v0.12.0 | **Visualize & Annotate** | Grafana provisioning bundle (datasource + dashboard provisioning), per-concern dashboard library (forecast / cost / per-layer), and `pat annotate --grafana <url>` pushing annotations when a recommendation fires | *First outbound write* — env token only, HTTPS-only, explicit opt-in |
| v0.13.0 | **Speak OTLP** | `pat export --otlp <endpoint>` — vendor-neutral exposition so Datadog / New Relic / Honeycomb / Grafana Cloud ingest `pat` data without Prometheus | OTLP auth headers from env only |
| v0.14.0 | **Reach ephemeral contexts** | Prometheus remote-write + Pushgateway targets so single-shot `observe`/`optimize` runs in cron / CI / CronJob can land metrics where no scrape endpoint exists | Complements D2 single-shot model; env-only auth |
| v0.15.0 | **Close the loop (emit, don't apply)** | `pat optimize --emit-keda-scaledobject` / `--emit-prometheus-adapter` so HPA scales on `pat`'s predicted-demand metric (exposed since v0.10) instead of lagging CPU | Emit-only, sanitized YAML; GitOps applies |
| v0.16.0 | **Package & operate** | `charts/pat-exporter` Helm chart (Deployment + ServiceMonitor for Prometheus Operator; rules + dashboards as ConfigMaps via Grafana sidecar); optional small Grafana panel plugin rendering the layer recommendation natively | Least-privilege RBAC + NetworkPolicy; exporter read-only |

### Three movements

1. **Expose & Alert** (v0.10–v0.11) — publish the model, make it actionable in
   the existing alerting pipeline. Purely declarative; no outbound writes yet.
2. **Visualize & Generalize** (v0.12–v0.14) — richer Grafana surface, the first
   (gated) outbound write, then vendor-neutral and ephemeral-context reach.
3. **Control & Package** (v0.15–v0.16) — feed autoscaling from the predicted
   metric, then ship the whole thing as a cluster-native bundle.

### Ordering rationale

- **Metrics before everything** (v0.10) — every later step consumes the exposed
  model.
- **Alerts before dashboards-at-scale** (v0.11 before v0.12) — alerting is higher
  operational value and stays declarative, so the first write-path (annotations)
  is deferred until v0.12 where there is a concrete payoff.
- **OTLP before remote-write** (v0.13 before v0.14) — vendor-neutrality is more
  broadly useful than the ephemeral-context niche; remote-write/pushgateway is a
  targeted complement to the single-shot model.
- **Control last** (v0.15) — closing the loop only makes sense once the
  predicted-demand metric has existed long enough to trust; packaging (v0.16)
  then caps the arc with cluster-native deployment.

### Decisions (locked 2026-06-17)

- **A1 — Emit-only invariant.** As above; the security spine of the arc.
- **A2 — Seven-version arc.** Full arc retained rather than the 6-version
  compression (folding remote-write into OTLP). Ephemeral-context support earns
  its own version (v0.14) because the single-shot model (D2) is a first-class
  constraint worth a dedicated, well-tested target.
- **A3 — Closed-loop autoscaling is in scope (v0.15)** — but strictly as
  *emit-only* KEDA / Prometheus-Adapter config, consistent with A1. `pat` never
  calls the Kubernetes API to scale.

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

### Delivery — Phase 1 (2026-06-17)

Shipped the read-only Prometheus exporter foundation:

- **`pat export`** — new `export` module + CLI command. Serves the analytical
  per-layer recommendation for a workload as Prometheus gauges on `GET /metrics`
  (`pat_recommended_replicas`, `pat_estimated_throughput_rps`,
  `pat_response_time_ms`, `pat_throughput_gain_ratio`, `pat_layer_recommended`,
  plus `pat_workload_*` and `pat_build_info`). `--once` renders the exposition
  and exits.
- **Security per the exposition decision:** read-only (only `GET`; other methods
  → `501`), binds `127.0.0.1` by default, `--listen-public` required to bind a
  routable host, fixed metric names, escaped label values. Exposition text
  hand-rolled (text format 0.0.4) — no new dependencies, consistent with the
  hardened posture.

### Delivery — Phase 2 (2026-06-17)

Shipped the prediction metrics, making the exporter reflect the live
observe→predict loop:

- **`pat export --predict`** runs an `optimize` pass over the observation store
  on every scrape and exposes `pat_predicted_rps{model}`,
  `pat_predicted_recommended_replicas{layer}`, `pat_observed_rps`,
  `pat_observed_latency_ms`, `pat_optimize_trend_ratio`,
  `pat_optimize_horizon_minutes`, and `pat_optimize_samples` (reads `0` on an
  empty store). `--model arima` adds 95% CI bounds. `--window`,
  `--horizon-minutes`, `--predict-layer`, and `--db` tune the pass.
- A pure `prediction_metrics_from_result` (testable with a fabricated result,
  no model fit) is split from the store-reading `build_prediction_metrics`.
- SMA is the cheap default; ARIMA refits each scrape (warned at startup).

### Delivery — Phase 3 + cost (2026-06-17): v0.10.0 scope complete

- **Cost metrics.** `pat export --cost-per-replica-hour` adds per-layer
  `pat_cost_per_request` and `pat_hourly_cost_usd` gauges from a uniform replica
  cost. Live cloud pricing (AWS/GCP/Azure) stays in `pat cost` — the exporter
  keeps one uniform rate to remain scrape-cheap and network-free. (The arc's
  `cloud`/`region` labels are therefore deferred to a future cloud-priced
  exporter mode if demanded; not blocking v0.10.0.)
- **Official Grafana dashboard.** `grafana/pat-dashboard.json` — importable
  (datasource template var), visualising observed-vs-predicted demand with the
  ARIMA CI band, recommended replicas per layer, response time, throughput gain,
  and cost-per-request. A test (`tests/test_grafana.py`) asserts every metric the
  dashboard queries is one the exporter emits, so the two cannot drift.

With Expose (Phase 1), predict (Phase 2), cost, and the dashboard shipped, the
**v0.10.0 "Expose" milestone of the monitoring-integration arc is complete.**
Next on the arc: v0.11.0 — Alert (`pat rules` recording + alerting rules).

---

## v0.11.0 — Alert: `pat rules` (delivered 2026-06-17)

Second step of the monitoring-integration arc. `pat rules` emits a Prometheus
rule file (recording + alerting rules) derived from the v0.10.0 exporter's
metrics, so the model's signals become actionable inside the existing
Prometheus / Alertmanager pipeline. **Emit-only** (arc invariant A1): `pat`
produces declarative YAML and never loads, applies, or reloads anything.

### What shipped

- **`pat rules`** — new `rules` module + CLI command, emits to stdout.
- **Recording rules** (`pat.recording`): `pat:predicted_rps`, `pat:observed_rps`,
  `pat:demand_growth_ratio` (predicted/observed, `clamp_min` guarded),
  `pat:trend_ratio`.
- **Alerting rules** (`pat.alerts`): `PatDemandSurgeForecast`,
  `PatDemandTrendRising`, `PatExporterAbsent` (always); `PatLayerTranslucencyMismatch`
  (when `--current-layer` given); `PatCostPerRequestOverBudget` (when
  `--cost-budget` given — the cost metric exists only under the exporter's
  `--cost-per-replica-hour`).
- **Tuning flags:** `--demand-surge-ratio`, `--trend-threshold`, `--for`.

### Security

- Only validated values reach the YAML: layer (one of the four known layers),
  numeric thresholds (rendered as numbers), and a `for:` duration validated
  against `\d+[smhdw]`. Every string scalar is double-quoted with `\` and `"`
  escaped, so the emitted rule file is always valid YAML and cannot smuggle
  content. Hand-rolled (no PyYAML dependency), matching `hpa_patch`. Verified by
  round-tripping the output through a YAML parser (escaped exprs parse back to
  the exact PromQL).

### Deviation from the arc sketch

The arc listed `pat rules --emit prometheus`. Prometheus is the only target, so
the redundant `--emit` flag was dropped — `pat rules` emits a Prometheus rule
file directly. (If a second target is ever added, a `--format` flag can be
introduced then.)

**Next on the arc:** v0.12.0 — Visualize & Annotate (Grafana provisioning +
`pat annotate`).

---

## v0.12.0 — Visualize & Annotate (delivered 2026-06-17)

Third step of the monitoring-integration arc, and the point where the arc takes
its **first outbound write** — gated and security-hardened.

### What shipped

- **Grafana provisioning bundle** — `grafana/provisioning/datasources/` and
  `grafana/provisioning/dashboards/` configs so `grafana/pat-dashboard.json`
  loads automatically (datasource URL honours `PROMETHEUS_URL`). `grafana/README.md`
  documents the mount paths + a docker example. The existing multi-panel
  dashboard already covers the forecast / cost / per-layer concerns, so it is
  loaded as-is rather than split into separate boards.
- **`pat annotate`** — new `annotate` module + CLI command. Runs the analysis and
  posts an annotation to Grafana's `/api/annotations` marking the recommendation.

### First-outbound-write security (the notable part)

This is the only place `pat` writes outward. It mirrors the Prometheus source
hardening (decision D3) and the export exposure guard:

- **Informational only.** It posts a Grafana annotation (a dashboard marker),
  never an infrastructure change — arc invariant A1 holds (`pat` informs/emits,
  it does not mutate infra).
- **Token from `PAT_GRAFANA_TOKEN` only** — required, never a CLI arg, never
  logged. The security event logs the Grafana host, not the URL or token.
- **HTTPS required** when sending the token; `--insecure-http` is an explicit,
  warned opt-out for localhost.
- URL + tags reject control characters; the annotation text is pat-generated.
- `--dry-run` previews the payload with no token and no network.
- `urllib` only — no new dependency.

### Deviation from the arc sketch

The arc mentioned a "per-concern dashboard library (forecast / cost / per-layer)".
The shipped `pat-dashboard.json` already presents those three concerns as panel
groups in one board, so v0.12.0 provisions that single board rather than
authoring three. Splitting into separate boards remains an easy drop-in later
(the provider loads every dashboard JSON in the mounted directory).

**Next on the arc:** v0.13.0 — Speak OTLP (vendor-neutral `pat export --otlp`).

---

## v0.13.0 — Speak OTLP (delivered 2026-06-17)

Fourth step of the monitoring-integration arc — vendor-neutrality. The exporter
can now **push** its metrics over OTLP to an OpenTelemetry Collector, which fans
out to any vendor (Datadog / New Relic / Honeycomb / Grafana Cloud), so pat data
reaches them **without Prometheus**.

### What shipped

- **`pat export --otlp <endpoint>`** — a new `otlp` module + an OTLP push mode on
  the export command. Single-shot: builds the metric set (including `--predict`
  forecasts and `--cost-per-replica-hour` cost gauges), encodes it as an OTLP
  `ExportMetricsServiceRequest` (JSON), and POSTs it to `<endpoint>/v1/metrics`,
  then exits — schedule externally (cron) for recurring push, consistent with
  the single-shot ethos (D2). `--service-name` sets the OTLP `service.name`.

### Transport decision — see ADR-0006

Per **[ADR-0006](../docs/adr/0006-otlp-export-transport.md)** the transport is
**hand-rolled OTLP/HTTP+JSON, Collector-targeted** — no `opentelemetry` SDK, no
`protobuf`, no `grpcio`. This preserves the zero-client-dependency hardened
posture (the fifth wire format `pat` emits by hand, after Prometheus text, rules
YAML, HPA YAML, and Grafana JSON). Bounded trade-off: JSON/HTTP-only, no
vendor-direct protobuf/gRPC — that remains a future opt-in `[otlp]` extra (the
ADR's revisit trigger).

### Security

Mirrors the Prometheus source / annotate writer: optional bearer token from
`PAT_OTLP_TOKEN` only (collectors are usually unauthenticated in-cluster), HTTPS
required when a token is sent (unless `--insecure-http`, warned), URL +
`service.name` reject control characters, non-finite samples dropped (no
OTLP/JSON form), `urllib` only. End-to-end validated against a real local HTTP
listener (valid OTLP/JSON received at `/v1/metrics`).

**Next on the arc:** v0.14.0 — Reach ephemeral contexts (Prometheus remote-write
+ Pushgateway targets).

---

## v0.14.0 — Reach ephemeral contexts (delivered 2026-06-17)

Fifth step of the monitoring-integration arc. Single-shot / batch jobs (cron, CI,
Kubernetes `Job`/`CronJob`) have no scrape endpoint; this version lets them push.

### What shipped

- **`pat export --pushgateway <url> --job <job>`** — a new `pushgateway` module +
  push mode on the export command. Builds the metric set (incl. `--predict` /
  `--cost-per-replica-hour`), renders the **existing Prometheus text exposition**,
  and PUTs it to `<url>/metrics/job/<job>{/<label>/<value>}`, then exits.
  `--grouping key=value` (repeatable) adds grouping labels; `--otlp` and
  `--pushgateway` are mutually exclusive.
- Reuses the exporter's exposition output verbatim — **zero new dependencies**
  (`urllib` only). Security mirrors `otlp.py`: optional bearer token from
  `PAT_PUSHGATEWAY_TOKEN` only (HTTPS-when-token, control chars rejected on token,
  URL, job, and labels; path segments percent-encoded). End-to-end validated
  against a real local PUT listener.

### Scope decision — remote-write deferred

The arc sketched "remote-write **+** Pushgateway." Only **Pushgateway** shipped;
**Prometheus remote-write is deferred**. Remote-write's wire format is protobuf
encoded inside snappy compression — adding it means a `protobuf` dependency (and
snappy), which is exactly the zero-client-dependency tension **ADR-0006** already
resolved against for OTLP. Pushgateway is Prometheus's *native* answer for
ephemeral/batch jobs and fully covers the version's "reach ephemeral contexts"
goal, so it is sufficient on its own. Remote-write remains a future opt-in extra
if a concrete need (e.g. Grafana Cloud / Mimir direct ingest without a gateway)
arises — at which point it warrants its own ADR following the ADR-0006 pattern.

**Next on the arc:** v0.15.0 — Close the loop (emit KEDA ScaledObject /
Prometheus-Adapter configs, emit-only).

---

## v0.15.0 — Close the loop (delivered 2026-06-17)

Sixth step of the monitoring-integration arc, and its **conceptual payoff**:
translucency-aware autoscaling. The exporter already publishes
`pat_predicted_recommended_replicas` (v0.10.0 `--predict`); this version emits the
declarative glue so an autoscaler scales a Deployment to *track that forecast* —
the model's prediction becomes the scaling signal, while `pat` still never
touches infrastructure.

### What shipped

- **`pat scaler`** — a new `scaler` module + CLI command, emit-only (YAML to
  stdout).
- **`--format keda`** (default) — a KEDA `ScaledObject` with a Prometheus trigger.
  `threshold: "1"` makes KEDA's `desiredReplicas = ceil(query / 1)`, so the
  Deployment's replica count equals pat's predicted recommendation.
- **`--format prometheus-adapter`** — an HPA v2 on an External metric
  (`target.type: Value`, `value: "1"` → the same identity) plus a commented
  Prometheus-Adapter `externalRules` snippet registering the metric.
- `--layer` filters the default query (`max(pat_predicted_recommended_replicas
  {layer=…})`), `--query` overrides it, `--min/-max-replicas` bound the range,
  `--namespace`/`--name` set the object.

### Security & posture

Names RFC 1123-validated; Prometheus URL + PromQL query reject control characters
and are double-quoted/escaped in the YAML; **emit-only** (arc invariant A1) — no
apply, no cluster credentials. Hand-rolled YAML, no new dependencies (like
`rules` / `hpa_patch`). Both formats validated by parsing the emitted YAML (KEDA
threshold and the escaped query round-trip exactly; HPA External `Value` target).

### Scope note — Prometheus-Adapter ConfigMap

The arc said "KEDA ScaledObject / Prometheus-Adapter configs." KEDA ships as a
fully appliable `ScaledObject`. For the adapter path, the **HPA** is fully
appliable, but the literal adapter `externalRules` are emitted as a *commented
example* rather than a standalone ConfigMap: Prometheus Adapter reads one
cluster-wide config, so a per-app rule must be merged into it, not applied. This
is honest about how the adapter works; a generated, mergeable rules ConfigMap
could be a follow-on if demanded.

**Next on the arc:** v0.16.0 — Package & operate (Helm chart + optional Grafana
panel plugin). Also outstanding from ADR-0007: a hand-rolled remote-write target
(its own slot).

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
| Grafana instead of a dedicated GUI | A dashboard delivers the visual-interface value through infra users already run, at lower cost and zero new attack surface; a standalone GUI is deferred indefinitely, not merely sequenced later |
| Read-only Prometheus exporter over a web app | Pull-based exposition is idiomatic and adds no auth/write surface, preserving the hardened posture |

## SDLC

These requirements are delivered under the family-wide Presidio SDLC:
<https://github.com/presidio-v/presidio-hardened-docs/blob/main/sdlc/sdlc-report.md>.
