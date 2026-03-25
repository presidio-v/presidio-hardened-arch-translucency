# presidio-hardened-arch-translucency

[![PyPI version](https://img.shields.io/pypi/v/presidio-hardened-arch-translucency.svg)](https://pypi.org/project/presidio-hardened-arch-translucency/)
[![Python](https://img.shields.io/pypi/pyversions/presidio-hardened-arch-translucency.svg)](https://pypi.org/project/presidio-hardened-arch-translucency/)
[![GitHub release](https://img.shields.io/github/v/release/presidio-v/presidio-hardened-arch-translucency.svg)](https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.1.0)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> MVP 0.1.0 — Architectural Translucency Analyzer for Docker & Kubernetes

**Architectural translucency** (Stantchev, ~2005) is the ability to monitor and
control non-functional properties — especially performance — **architecture-wide
in a cross-layered way**.  The core insight: the *same* measure (replication) has
**different implications on throughput ω(δ) and response time** when applied at
different layers.

This CLI tool (`pat`) helps you choose the replication layer that gives the
**highest performance gain with the lowest overhead** for your workload.

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

## Live Demonstrator

`pat demo` spins up real Docker containers and measures throughput, latency,
and CPU across three replication variants, then outputs a results table and
a PNG comparison chart.

**Requirements:** Docker daemon running locally.

```bash
# Install with demo extras
pip install "presidio-hardened-arch-translucency[demo]"

# Run the demo (defaults: 4 replicas, 40 requests, 8 concurrent threads)
pat demo

# Custom run
pat demo --replicas 6 --requests 80 --concurrency 12 --output results.png
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
```

---

## Security — Presidio Hardening

This toolkit ships with mandatory Presidio security extensions:

| Feature | Description |
|---|---|
| **Input sanitization** | All workload parameters are bounds-checked and type-validated |
| **Secure logging** | Recommendations logged without sensitive data |
| **CVE/dependency audit** | `pip-audit` check on every run (`--skip-audit` to disable) |
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
  analyze   Analyze workload and recommend the optimal replication layer.

pat analyze Options:
  -r, --requests-per-second FLOAT   Observed workload in req/s  [required]
  -l, --avg-latency-ms FLOAT        Current average latency in ms  [required]
  -c, --current-layer TEXT          Current layer (container|pod|deployment|node)  [required]
  --show-all                        Show all layers in a comparison table
```

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
