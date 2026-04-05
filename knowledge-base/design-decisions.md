# Design Decisions Registry — presidio-hardened-arch-translucency

Plays the role of `claims.md` for a software project: deliberated choices,
especially those the x402 v0.5.0 integration will depend on.

**Status values:** `stable` | `provisional` | `superseded` | `under-review`

A `stable` decision is safe to depend on in x402 integration code.
A `provisional` decision may change — do not hardcode in x402 before it is resolved.

---

## Mathematical Model

| ID | Decision | Rationale | Status |
|----|----------|-----------|--------|
| D1 | Layer parameters (α, β) are fixed constants per layer, not workload-adaptive | Simplicity; avoids need for live profiling at query time. Revisit at v0.7.0 autoresearch. | stable |
| D2 | Intensity equation: `ι(δ) = rps/δ + α·rps + β·rps·ln(δ)` | From Stantchev 2006 foundational equations. Not modified since v0.1.0. | stable |
| D3 | Throughput: `ω(δ) = min(base_capacity·δ·efficiency(δ), rps)` | M/M/δ queue approximation. Good for recommendation; not a simulation. | stable |
| D4 | Response time uses M/M/δ approximation + coordination overhead | Approximation is sufficient for layer comparison; not accurate for absolute latency SLOs. | stable |
| D5 | `container` α=0.02, β=0.01; `pod` α=0.05, β=0.02; `deployment` α=0.10, β=0.04; `node` α=0.18, β=0.06 | Calibrated against Docker demo measurements + Stantchev ICSI TR WebSphere data. Not re-calibrated for K8s. | provisional |

---

## CLI and API Surface

| ID | Decision | Rationale | Status |
|----|----------|-----------|--------|
| D6 | CLI entry point: `pat` | Short, memorable. Stands for "presidio architectural translucency". | stable |
| D7 | `--requests-per-second` and `--avg-latency-ms` are required for all analysis commands | Minimum inputs to compute intensity. No defaults — garbage in, garbage out. | stable |
| D8 | `--current-layer` is required for `analyze` and `what-if`; optional for `cost` | Needed to compute baseline; `cost` can compare all layers without a baseline. | stable |
| D9 | No programmatic Python API exposed yet — CLI only | Scope decision for v0.1.0–v0.5.0. A Python API is needed for x402 ArchTranslucencyAdapter. **Blocking issue for x402 v0.5.0 integration.** | provisional |
| D10 | AWS pricing via public API, no credentials required | Privacy and ease-of-use. Fargate and EC2 on-demand only. Reserved/Spot deferred to v0.6.0. | stable |
| D11 | Pricing cache at `~/.pat/pricing-cache.json`, TTL 24 h | Avoids hammering AWS API. Offline fallback: use last cached value + warn. | stable |

---

## HPA Lag Model

| ID | Decision | Rationale | Status |
|----|----------|-----------|--------|
| D12 | HPA poll interval default: 15 s | Kubernetes default HPA sync period. Overridable via `--hpa-poll-s`. | stable |
| D13 | Pod startup default: 30 s | Conservative estimate for a typical FastAPI/Node container. Overridable. | stable |
| D14 | Trough throughput = pre-spike throughput (not zero) | Existing replicas continue serving during scale-out window. | stable |
| D15 | `pat slo` recommends minimum `HPA minReplicas` to avoid trough breach | The recommendation is the smallest replica count where trough p99 ≤ target. | stable |

---

## Integration with x402 (v0.5.0 of x402)

| ID | Decision | Rationale | Status |
|----|----------|-----------|--------|
| D16 | arch-translucency provides the SLO observability signal; x402 provides the payment mechanism | Clean separation of concerns. Neither project depends on the other's internals. | stable |
| D17 | The adapter (`ArchTranslucencyAdapter`) lives in the x402 codebase, not here | x402 owns the payment integration; arch-translucency stays payment-agnostic. | stable |
| D18 | Trigger event: p99 latency estimate from `pat slo` output exceeds threshold | `pat slo` is the existing SLO check surface. x402 adapter calls it programmatically (CLI subprocess or future Python API). | provisional |
| D19 | `SLOEvent` schema: `{timestamp, layer, p99_ms, trough_p99_ms, recommended_replicas, slo_breached: bool}` | Minimal schema; stable enough for adapter prototype. May extend at v0.5.0. | provisional |
| D20 | Cooldown between SLO-triggered payments: 300 s (5 min) default | Prevents spending drain during recovery oscillations. Configurable in `SLOPaymentPolicy`. | provisional |

---

## Lint notes

- **D5** (layer parameters): marked `provisional` — calibrated against Docker demo, not production K8s. If v0.7.0 autoresearch produces new measurements, update parameters and re-mark. The x402 SLO broker's trigger threshold should account for this uncertainty.
- **D9** (no Python API): blocking for x402 v0.5.0 integration. Must be resolved — either expose a Python API or formalise the CLI subprocess contract — before x402 v0.5.0 implementation starts.
- **D18, D19, D20**: all `provisional` — finalise before writing `ArchTranslucencyAdapter` in x402.
