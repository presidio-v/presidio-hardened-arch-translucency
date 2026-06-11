# ADR-0001: Foundation-first delivery order for the autoresearch loop

* Status: accepted
* Date: 2026-06-10
* Decision ref: D1 (PRESIDIO-REQ.md, v0.8.0 Design Decisions)

## Context

v0.7.0 shipped `pat calibrate` (analytical mode) but deferred the rest of the
"autoresearch" scope — the `pat observe` → `pat optimize` loop that records live
workload measurements and turns them into proactive scaling recommendations.
v0.8.0 had to build that scope across several moving parts: a persistence store,
a smoothing forecaster, a live metrics source, an advanced time-series model, and
an HPA manifest emitter.

These parts have hard dependencies on one another. ARIMA's documented
`< 30 samples → SMA` fallback cannot exist before SMA exists. SMA cannot run
before there is a store to read. A Prometheus source is only useful once the
store can ingest from any source. Sequencing was therefore a real decision, not a
matter of taste.

## Decision

We will build the autoresearch base before the advanced models, one PR per phase,
in this order:

1. `pat observe` — SQLite store + source-agnostic ingestion
2. `pat optimize --model sma` — simple moving average over the store
3. Prometheus as an observation source
4. `pat optimize --model arima` — opt-in ARIMA on top, with SMA fallback
5. `pat optimize --emit-hpa-patch` — HPA manifest emitter

This honours the pre-existing cross-cutting "SMA before ARIMA" decision and
respects the fallback dependency above.

## Consequences

- Each phase ships behind a green test suite and a real, independently useful CLI
  surface — `pat observe` is useful before `optimize` exists; SMA is useful before
  ARIMA does.
- The ARIMA fallback is implementable because SMA is already present.
- Calibrate's per-layer/Docker extension (D4) was sequenced *after* the base and
  consequently slipped out of the v0.8.0 sprint into v0.9.0 — an accepted cost of
  prioritising the loop's foundation.
- Reviewers get small, phase-scoped PRs instead of one large drop.
