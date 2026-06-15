# ADR-0004: `pat calibrate` fits global κ+β analytically (v0.8.0)

* Status: accepted — extended by v0.9.0 per-layer fitting (see Consequences)
* Date: 2026-06-10
* Decision ref: D4 (PRESIDIO-REQ.md, v0.8.0 Design Decisions)

## Context

v0.7.0 shipped `pat calibrate` in analytical mode: it fits a single global
per-replica capacity model — concurrency (κ) and coordination overhead (β) — to
measured `rps:latency_ms:replicas` observations via `scipy.optimize.curve_fit`,
writing the result to the model file. The original calibrate contract envisioned
more: per-layer α/β fitting (`--layer web/worker/cache/db`) and a Docker benchmark
mode that runs controlled replica sweeps and fits from the measured results.

The question for v0.8.0 was how much of that fuller contract to build while the
autoresearch loop (D1) was the sprint's priority.

## Decision

We will keep `pat calibrate` at the shipped scope for v0.8.0 — a single global κ+β
analytical fit — and treat per-layer α/β fitting and Docker benchmark mode as new
work layered on afterward. The v0.7.0 analytical, global-fit path remains the
supported baseline.

## Consequences

- The analytical global fit stays the stable, Docker-free baseline that
  `pat analyze`/`cost`/`what-if`/`slo` rely on.
- Workloads with materially different per-layer characteristics (e.g. a CPU-bound
  worker vs. an async API) cannot be tuned independently in v0.8.0.
- **v0.9.0 extension.** Per-layer fitting shipped: `pat calibrate --layer <name>`
  upserts per-layer parameters under `layers.<name>` in the model file while
  preserving the global fit, and the analysis commands select them with their own
  `--layer`. The model file stays backward-compatible (a file with no `layers` key
  resolves exactly as before). The **Docker `--benchmark` mode remains deferred.**
