# Replication Model — Mathematical Specification
# Source: model.py + Stantchev 2006 IEE Proceedings
# Captured: 2026-04-05 at v0.5.0. Immutable — add a new file if model changes.

## Variables

| Symbol | Meaning |
|--------|---------|
| δ | Number of replicas |
| rps | Requests per second (workload intensity input) |
| α | Fixed overhead fraction for a layer (constant per layer) |
| β | Coordination cost coefficient (constant per layer) |
| base_capacity | Throughput capacity of a single replica (derived from rps + avg_latency) |

## Layer parameters

| Layer | α | β |
|-------|---|---|
| container | 0.02 | 0.01 |
| pod | 0.05 | 0.02 |
| deployment | 0.10 | 0.04 |
| node | 0.18 | 0.06 |

**Calibration basis:** Docker demo measurements + Stantchev ICSI TR WebSphere replication
data. Not empirically validated against production Kubernetes clusters. See design-decision D5.

## Equations

### Intensity after replication

```
ι(δ) = rps/δ  +  α·rps  +  β·rps·ln(δ)
```

Interpretation: per-replica load = divided workload + fixed coordination overhead +
logarithmically growing synchronisation cost.

### Efficiency

```
efficiency(δ) = 1 - α - β·ln(δ)
```

Efficiency degrades as replicas increase (coordination cost grows). For large δ,
efficiency can reach zero — adding more replicas past this point reduces throughput.

### Throughput

```
ω(δ) = min(base_capacity · δ · efficiency(δ), rps)
```

Capped at rps: you cannot serve more than what arrives.

`base_capacity` is derived from the input workload:
```
base_capacity = rps / (1 - utilisation)
utilisation = rps · avg_latency_s  (Little's Law, capped at 0.95)
```

### Response time (M/M/δ approximation)

```
ρ = ι(δ) / base_capacity                         # per-replica utilisation
RT(δ) = avg_latency / (1 - ρ)  +  coordination_overhead
coordination_overhead = α · avg_latency + β · avg_latency · ln(δ)
```

### Optimal replica count

For each layer, search δ ∈ [1, 32] for the value that maximises:
```
score(δ) = throughput_gain(δ) - response_time_penalty(δ)
```

where `throughput_gain = (ω(δ) - rps) / rps` and `response_time_penalty` penalises
layers where RT(δ) > avg_latency (i.e. replication made latency worse).

## HPA lag model (hpa.py)

```
trough_window_s = hpa_poll_s + pod_startup_s + cold_start_s
trough_throughput = pre_spike_throughput           # existing replicas only
trough_p99_ms = response_time_estimate · 3         # p99 ≈ 3× avg during overload
missed_requests = (spike_rps - trough_throughput) · trough_window_s
```

## Known limitations

1. M/M/δ approximation assumes Poisson arrivals and exponential service times —
   appropriate for steady-state estimation, not bursty real-world traffic.
2. Layer parameters (α, β) are not validated against production Kubernetes. The Docker
   demo uses a single-host loopback — coordination overhead is underestimated.
3. base_capacity is derived from the user-provided avg_latency, not measured. The model
   is only as good as the inputs.
4. Response time formula diverges as ρ → 1. Utilisation is capped at 0.95 to avoid
   infinity, but real systems degrade before hitting this cap.
