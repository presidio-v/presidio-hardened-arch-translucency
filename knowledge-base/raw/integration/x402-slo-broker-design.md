# x402 SLO Broker — Integration Design
# Version: 0.1 (provisional) | Date: 2026-04-05
# Immutable — add a new dated file for each design revision.
# Cross-reference: x402 knowledge-base/raw/integration/ (to be created at x402 v0.5.0 kickoff)

## What this is

The design for how `presidio-hardened-arch-translucency` (PAT) provides the SLO
observability signal to `presidio-hardened-x402`'s SLO payment broker.

Specified here rather than only in x402 PRESIDIO-REQ.md because PAT must make
concrete API decisions (D9, D18, D19) before x402 can implement the adapter.

---

## Signal flow

```
[Workload] → PAT (pat slo model)
                ↓ SLOEvent
         ArchTranslucencyAdapter (in x402 codebase)
                ↓ SLOTrigger
         SLOPaymentBroker (in x402 codebase)
                ↓ payment decision
         HardenedX402Client
                ↓ x402 payment
         [Compute provider / capacity upgrade endpoint]
```

---

## PAT's responsibilities (arch-translucency side)

1. Provide a stable, machine-readable output from `pat slo` — either a `--output-json`
   flag (preferred) or a documented subprocess + text-parse protocol (fallback).

2. Expose (eventually) a Python API:
   ```python
   from presidio_arch_translucency import SLOChecker
   checker = SLOChecker(rps=200, avg_latency_ms=80, p99_target_ms=500)
   event = checker.check(spike_multiplier=3.0)
   # event.trough_p99_ms, event.slo_breached, event.recommended_replicas
   ```

3. Keep the SLO output schema stable across minor versions once formalised.

---

## x402's responsibilities (x402 side)

`ArchTranslucencyAdapter` — lives in `presidio_x402/arch_translucency_adapter.py`:

```python
class ArchTranslucencyAdapter:
    def __init__(self, rps: float, avg_latency_ms: float,
                 p99_target_ms: float, spike_multiplier: float = 3.0): ...

    def check_slo(self) -> SLOEvent:
        """Call PAT (CLI or Python API) and return a structured SLOEvent."""
        ...
```

`SLOEvent` schema (provisional — see design-decision D19):
```python
@dataclass
class SLOEvent:
    timestamp: datetime
    layer: str           # container | pod | deployment | node
    p99_ms: float        # steady-state p99
    trough_p99_ms: float # p99 during HPA trough
    slo_breached: bool
    recommended_replicas: int
```

`SLOPaymentBroker` — lives in `presidio_x402/slo_broker.py`:

```python
broker = SLOPaymentBroker(
    client=HardenedX402Client(payment_signer=signer, policy={...}),
    slo_source=ArchTranslucencyAdapter(rps=200, avg_latency_ms=80, p99_target_ms=500),
    slo_policy=SLOPaymentPolicy(
        latency_threshold_ms=200,
        max_per_slo_event_usd=0.50,
        cooldown_seconds=300,
        max_daily_slo_usd=10.00,
    ),
)
```

---

## Blocking issues before x402 v0.5.0 implementation

1. **D9**: PAT has no Python API and no `--output-json` flag. x402 cannot implement
   `ArchTranslucencyAdapter` without one of these. **Must be resolved first.**

2. **D18/D19**: `SLOEvent` schema is provisional. Must be agreed and stabilised
   between both codebases before the adapter is written.

3. **Compute provider availability**: At least one x402-enabled compute provider must
   offer capacity tier upgrades via 402 responses for the end-to-end flow to work.
   This may require building a prototype provider as part of x402 v0.5.0.

---

## What does NOT need to be decided yet

- Whether PAT exposes a full metrics stream (Prometheus-style) or just point-in-time
  SLO checks. The broker polls on a schedule; a stream is v0.8.0 territory.
- Multi-provider bidding. Single-provider first; auction later (x402 v0.6.0).
- The extended PII filter entity types for provisioning metadata. These are x402-internal.
