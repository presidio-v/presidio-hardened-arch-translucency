# Architectural Translucency — Knowledge Base

> Maintained alongside `presidio-hardened-arch-translucency`. Last updated: 2026-04-05.
> Current version: v0.5.0 (cloud billing integration — AWS on-demand).

---

## 1. Theory — What Architectural Translucency Is

**Architectural translucency** (Stantchev, 2006) is the ability to monitor and
control non-functional properties — especially performance — **architecture-wide in
a cross-layered way.**

The core insight: the *same* measure (replication) has **different implications on
throughput ω(δ) and response time** when applied at different architectural layers.
Choosing the wrong layer wastes resources or degrades latency even as replicas increase.

The concept is defined formally in:
- Stantchev & Malek (2006) — IEE Proceedings Software [foundational theory]
- Stantchev & Schröpfer (2009) — GPC 2009 Springer [QoS/SLA enforcement]
- Stantchev (ICSI TR) — WebSphere replication measurements [empirical basis]

See `raw/papers/` for summaries of each.

---

## 2. CLI Reference (v0.5.0)

**Package:** `presidio-hardened-arch-translucency`
**CLI entry point:** `pat`
**Install:** `pip install presidio-hardened-arch-translucency`

### Commands

| Command | Purpose |
|---------|---------|
| `pat analyze` | Recommend optimal replication layer for a workload |
| `pat what-if` | Model HPA lag trough during a load spike |
| `pat slo` | Check p99 SLO compliance in steady-state and during trough |
| `pat cost` | Cost-aware replication analysis (with optional AWS live pricing) |
| `pat demo` | Live Docker experiment: measures real throughput/latency across variants |

### Key flags (analyze)

| Flag | Required | Description |
|------|----------|-------------|
| `--requests-per-second` | Yes | Observed workload (req/s) |
| `--avg-latency-ms` | Yes | Current average latency (ms) |
| `--current-layer` | Yes | `container` \| `pod` \| `deployment` \| `node` |
| `--show-all` | No | Show all layers in comparison table |
| `--cost-per-replica-hour` | No | Add cost columns to output |

### Key flags (cost, v0.5.0 cloud mode)

| Flag | Description |
|------|-------------|
| `--cloud aws` | Fetch live AWS on-demand pricing (no credentials needed) |
| `--region` | AWS region (e.g. `us-east-1`) |
| `--instance-type` | EC2 instance type (e.g. `m5.large`) |
| `--fargate` | Use Fargate pricing instead of EC2 |
| `--vcpu`, `--memory-gb` | Fargate resource allocation |
| `--no-cache` | Bypass local pricing cache (`~/.pat/pricing-cache.json`, TTL 24h) |

Full CLI surface captured in `raw/cli-api-v0.5.md`.

---

## 3. Replication Layers

Four layers modelled, each with distinct α (fixed overhead) and β (coordination cost):

| Layer | α | β | Fixed overhead | Coordination |
|-------|---|---|----------------|-------------|
| `container` | 0.02 | 0.01 | 2% | Low (shared kernel) |
| `pod` | 0.05 | 0.02 | 5% | Moderate (shared network namespace) |
| `deployment` | 0.10 | 0.04 | 10% | High (scheduler + network policy) |
| `node` | 0.18 | 0.06 | 18% | Highest (VM/bare-metal startup) |

Source: `raw/replication-model.md`. These are calibrated parameters, not first-principles
derivations — see the raw file for the empirical basis.

**Cross-layer recommendation logic:** maximise `ω(δ)` gain while penalising response-time
increase. The optimal layer is not always the one with the lowest overhead — workload
intensity and current utilisation determine whether the coordination cost of a higher
layer is worth paying.

---

## 4. HPA Lag Model (v0.3.0)

`pat what-if` models the performance trough that occurs between a load spike and the
moment new Kubernetes pods are Ready.

Key parameters:
- `--hpa-poll-s`: HPA polling interval (default 15 s)
- `--pod-startup-s`: pod startup time (default 30 s)
- `--cold-start-s`: additional cold-start time (default 0 s)

Trough window = `hpa-poll-s + pod-startup-s + cold-start-s`

During the trough, throughput drops to the pre-spike level and latency spikes.
`pat slo` uses this model to determine the minimum `HPA minReplicas` that eliminates
a p99 SLO breach during the trough.

**Integration relevance for x402 v0.5.0:** The trough is the trigger event for the SLO
payment broker. When p99 exceeds the threshold during a trough, the broker fires an
x402 payment for a capacity upgrade. The cooldown window (default 5 min) prevents
re-triggering during recovery. See `raw/integration/x402-slo-broker-design.md`.

---

## 5. Cost Model (v0.4.0–v0.5.0)

**ROI score** = throughput-gain-% / cost-per-request (higher = better performance-per-dollar)

**Layer-to-AWS pricing mapping:**

| Layer | EC2 packing ratio | Fargate pricing |
|-------|------------------|-----------------|
| `container` | 1/16 of node | 25% of task price |
| `pod` | 1/8 of node | Full task price |
| `deployment` | Full node | 4× task price |
| `node` | Dedicated node | 8× task price |

Pricing source: AWS public Pricing API (no credentials). Cached at
`~/.pat/pricing-cache.json`, TTL 24 h.

---

## 6. Integration with presidio-hardened-x402 (v0.5.0 of x402)

The planned integration makes `presidio-hardened-arch-translucency` the **SLO
observability signal** for the x402 SLO payment broker.

**Signal flow:**
```
arch-translucency (pat slo / metrics feed)
  → SLO degradation event (p99 > threshold)
    → ArchTranslucencyAdapter (in x402)
      → SLOPaymentBroker
        → HardenedX402Client
          → x402 payment for capacity upgrade
```

**What arch-translucency provides to x402:**
- p99 latency estimates (analytical model) — available today via `pat slo`
- Trough window duration and severity
- Recommended replica count and layer to restore SLO

**What x402 needs arch-translucency to expose (not yet implemented):**
- A stable metrics endpoint or Python API for programmatic SLO queries
- A `SLOEvent` object or dict schema that `ArchTranslucencyAdapter` can consume
- Stable output format across minor versions

See `raw/integration/x402-slo-broker-design.md` for the full interface design and
`design-decisions.md` for which aspects are deliberated and stable.

---

## 7. Roadmap

| Version | Theme | Status |
|---------|-------|--------|
| v0.1.0 | MVP — layer analysis & recommendation | Released |
| v0.2.0 | Multi-Python CI hardening | Released |
| v0.3.0 | HPA lag model (`pat what-if`, `pat slo`) | Released |
| v0.4.0 | Cost-aware analysis (`pat cost`) | Released |
| **v0.5.0** | **Cloud billing — AWS on-demand pricing** | **Current** |
| v0.6.0 | Cloud billing — reserved/spot + GCP + Azure | Planned |
| v0.7.0 | Autoresearch — simple moving average predictions | Planned |
| v0.8.0 | Autoresearch — Prometheus integration + ARIMA | Planned |

Full deliberation in `PRESIDIO-REQ.md`.

---

## 8. Open Questions / Next Investigations

- [ ] What Python API surface should arch-translucency expose for programmatic SLO queries? (needed for x402 ArchTranslucencyAdapter — resolve before x402 v0.5.0 work starts)
- [ ] Are the α/β layer parameters empirically validated for Kubernetes clusters, or calibrated against the Docker demo only? The demo uses local Docker — not production K8s overhead.
- [ ] v0.6.0: GCP and Azure pricing API endpoints — same public/no-auth pattern as AWS?
- [ ] v0.7.0 autoresearch: what is the minimum `pat demo` run count to fit a reliable SMA? Needs a data collection plan before implementation.
