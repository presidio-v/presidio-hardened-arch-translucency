# CLI API Surface — v0.5.0
# Captured: 2026-04-05. Immutable — add cli-api-v0.6.md when CLI changes.
# This is the stable contract for the x402 ArchTranslucencyAdapter to depend on.

## Entry point

```
pat [OPTIONS] COMMAND [ARGS]...
```

Global options: `--version / -V`, `--verbose / -v`, `--skip-audit`, `--help`

---

## pat analyze

Recommend optimal replication layer for a workload.

```bash
pat analyze \
  --requests-per-second FLOAT \   # required
  --avg-latency-ms FLOAT \        # required
  --current-layer TEXT \          # required: container|pod|deployment|node
  [--show-all] \                  # compare all layers
  [--cost-per-replica-hour FLOAT] # add cost columns
```

**Output (default):** Rich panel with recommended layer, optimal replicas,
throughput gain %, response-time Δ %, estimated throughput (req/s), estimated RT (ms).

**Output (--show-all):** Table with one row per layer + Recommended column.

---

## pat what-if

Model the HPA trough during a load spike.

```bash
pat what-if \
  --current-rps FLOAT \           # required
  --spike-rps FLOAT \             # required
  --avg-latency-ms FLOAT \        # required
  --current-layer TEXT \          # required
  [--hpa-poll-s INT]   \          # default 15
  [--pod-startup-s INT] \         # default 30
  [--cold-start-s INT] \          # default 0
  [--replicas-before INT] \
  [--replicas-after INT] \
  [--cost-per-request FLOAT] \    # show trough revenue impact
  [--output PATH]                 # save PNG (3-panel time-series)
```

**Output:** Two Rich panels (trough summary + steady state) + optional PNG.

---

## pat slo

SLO compliance check across all layers, steady-state and trough.

```bash
pat slo \
  --requests-per-second FLOAT \   # required
  --avg-latency-ms FLOAT \        # required
  --p99-target-ms FLOAT \         # required
  [--spike-multiplier FLOAT] \    # default 3.0
  [--hpa-poll-s INT] \
  [--pod-startup-s INT] \
  [--cold-start-s INT] \
  [--cost-per-replica-hour FLOAT] # show Cost/hr column
```

**Output:** Table with columns: Layer | Replicas | Steady p99 | Trough p99 | SLO | Cost/hr.
Recommendation panel: min HPA minReplicas to eliminate trough breach.

**Key output field for x402 integration:** `trough_p99_ms` and `slo_breached` per layer.
These are the trigger signal for the SLO payment broker.

---

## pat cost

Cost-aware replication analysis.

```bash
# Manual costs
pat cost \
  --requests-per-second FLOAT \
  --avg-latency-ms FLOAT \
  --current-layer TEXT \
  --cost-per-container-hour FLOAT \
  --cost-per-pod-hour FLOAT \
  --cost-per-deployment-hour FLOAT \
  --cost-per-node-hour FLOAT

# AWS on-demand (v0.5.0)
pat cost \
  --requests-per-second FLOAT \
  --avg-latency-ms FLOAT \
  --current-layer TEXT \
  --cloud aws \
  --region TEXT \               # e.g. us-east-1
  --instance-type TEXT \        # e.g. m5.large
  [--fargate] \
  [--vcpu FLOAT] \
  [--memory-gb FLOAT] \
  [--no-cache]
```

**Output:** Table with: Layer | Replicas | Δ Throughput | Δ RT | Cost/hr | Cost/req | ROI score | Best ROI.

---

## pat demo

Live Docker experiment (requires Docker daemon).

```bash
pip install "presidio-hardened-arch-translucency[demo]"

pat demo \
  [--replicas INT] \              # default 4
  [--requests INT] \              # default 40
  [--concurrency INT] \           # default 8
  [--cost-per-container-hour FLOAT] \
  [--cloud aws] [--region TEXT] [--instance-type TEXT] \
  [--output PATH]                 # save PNG
```

**Output:** Measured results table + HPA projection panel + cost analysis panel.
Saves `demo-results.png` and `demo-results-hpa.png`.

---

## Programmatic access (v0.5.0 — NOT YET AVAILABLE)

No public Python API is exposed at v0.5.0. The x402 ArchTranslucencyAdapter must
use CLI subprocess invocation until a Python API is added.

**Provisional subprocess contract for ArchTranslucencyAdapter:**

```python
import subprocess, json

result = subprocess.run([
    "pat", "slo",
    "--requests-per-second", str(rps),
    "--avg-latency-ms", str(avg_latency_ms),
    "--p99-target-ms", str(p99_target_ms),
    "--spike-multiplier", str(spike_multiplier),
], capture_output=True, text=True)
# Parse Rich panel output — fragile; a JSON output flag is the preferred solution.
```

**Recommended resolution (design-decision D9):** add `--output-json` flag to `pat slo`
before x402 v0.5.0 implementation begins, returning:
```json
{
  "layers": [
    {
      "layer": "container",
      "replicas": 4,
      "steady_p99_ms": 120.5,
      "trough_p99_ms": 892.3,
      "slo_breached": true,
      "cost_per_hour": 0.08
    }
  ],
  "recommended_min_replicas": 3
}
```
