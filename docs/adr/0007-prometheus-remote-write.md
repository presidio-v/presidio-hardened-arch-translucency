# ADR-0007: Prometheus remote-write — hand-rolled v1 (protobuf + literals-only snappy)

- **Status:** Accepted (2026-06-17). Governs a future remote-write target
  (a v0.14.x / v0.16.x slot — not v0.15.0).
- **Deciders:** Vladimir Stantchev (maintainer)
- **Related:** [ADR-0006](0006-otlp-export-transport.md) (OTLP transport — same
  dependency-vs-posture tension); PRESIDIO-REQ.md "Monitoring Integration Arc"
  (v0.14.0 deferred remote-write).

## Context

v0.14.0 ("Reach ephemeral contexts") shipped a **Pushgateway** target and
**deferred Prometheus remote-write**, citing remote-write's wire format
(protobuf inside snappy compression) as a dependency that would breach the
zero-client-dependency posture — the same tension ADR-0006 resolved for OTLP.

This ADR revisits that deferral, because two facts change the calculus:

1. **Remote-write has a unique reach.** Pushgateway pushes to a *gateway* that
   Prometheus scrapes; OTLP pushes to an *OTel Collector*. Neither cleanly
   covers **direct push to a managed Prometheus-compatible TSDB** — Grafana
   Cloud, Mimir, Cortex, Thanos Receive, VictoriaMetrics, or a Prometheus with
   `--web.enable-remote-write-receiver`. Those expose a *remote-write* endpoint
   (`/api/v1/write`) and often nothing else. This is a common managed-Prometheus
   scenario `pat` cannot currently target without extra infrastructure.

2. **The dependency objection is weaker than v0.14.0 assumed.** Remote-write v1
   is `POST /api/v1/write`, body = **snappy-compressed protobuf** `WriteRequest`,
   headers `Content-Type: application/x-protobuf`, `Content-Encoding: snappy`,
   `X-Prometheus-Remote-Write-Version: 0.1.0`. Both layers are hand-rollable:
   - **Protobuf:** the `WriteRequest` proto is tiny and frozen —
     `WriteRequest{ repeated TimeSeries }`,
     `TimeSeries{ repeated Label{name,value}, repeated Sample{double value, int64 ts} }`.
     Encoding is varints + length-delimited fields + one `fixed64` (IEEE-754
     double); ~40 lines, no library.
   - **Snappy:** the format permits a **literals-only** stream (preamble varint +
     one literal element covering all bytes). A compliant decoder (Prometheus
     uses `golang/snappy`) decodes it fine — so we emit *valid* snappy with **no
     compression library and ~3 bytes overhead** (proven:
     `varint(len) + literal-header + raw`). pat's payloads are a few dozen
     series, so the lost compression is irrelevant.

So remote-write v1 can be **hand-rolled at zero dependency cost**, consistent
with the five wire formats `pat` already emits by hand (Prometheus text, rules
YAML, HPA YAML, Grafana JSON, OTLP JSON).

## Considered options

### Option A — Hand-roll remote-write v1 (protobuf + literals-only snappy) — **chosen**

- **Pros:** unlocks direct push to Grafana Cloud / Mimir / Cortex / Thanos /
  VictoriaMetrics / receiver-enabled Prometheus; **zero new dependencies**;
  consistent with the hand-rolled posture; auditable (encode-only, no parsing of
  untrusted input → no parser attack surface).
- **Cons:** the **first *binary* format** `pat` hand-rolls — higher correctness
  risk than the text formats. Mitigated by: round-trip tests (a tiny in-test
  protobuf/snappy decoder), a real-receiver smoke (`prometheus
  --web.enable-remote-write-receiver`), and scoping to **remote-write v1 only**
  (universally supported; skip the 2.0 symbol-table complexity).

### Option B — Add it as an opt-in extra (`[remote-write]`)

Depend on `protobuf` + a snappy library, gated behind
`pip install '...[remote-write]'`.

- **Pros:** uses maintained encoders; lower correctness risk.
- **Cons:** a dependency tree (incl. a snappy C-extension) for a capability the
  literals-only trick makes free; inconsistent with Option A now that hand-rolling
  is demonstrably cheap. Only worth it if hand-rolled correctness proves
  unsustainable.

### Option C — Do not add remote-write

Rely on Pushgateway (ephemeral → Prometheus) and OTLP (→ Collector → anywhere).

- **Pros:** zero new surface; `pat` is an *advisory, single-shot* tool, and an
  OTel Collector can remote-write onward, so direct remote-write is arguably
  redundant.
- **Cons:** leaves the common "push straight to Grafana Cloud / Mimir without
  running a gateway or collector" case uncovered.

## Decision

**Option A — hand-roll Prometheus remote-write v1**, scoped to v1, with a
hand-rolled protobuf encoder and a literals-only snappy framer (zero
dependencies). This corrects the v0.14.0 deferral: the snappy/protobuf
"dependency" that justified deferring is avoidable, and remote-write uniquely
reaches managed Prometheus-compatible backends.

Surface (for the implementing version): `pat export --remote-write <url>` —
single-shot, builds the same metric set, encodes each metric/sample as a
`TimeSeries` (with a `__name__` label, labels sorted, millisecond timestamps),
snappy-frames the protobuf, and POSTs it. Auth supports **bearer** (env
`PAT_REMOTE_WRITE_TOKEN`) **and HTTP basic** (env `PAT_REMOTE_WRITE_USER` /
`PAT_REMOTE_WRITE_PASSWORD` — Grafana Cloud uses `instance-id` + API token);
HTTPS required when credentials are sent (unless `--insecure-http`). Mutually
exclusive with `--otlp` / `--pushgateway`.

This is **not** v0.15.0 — v0.15.0 remains "Close the loop." Remote-write lands as
its own follow-on (a v0.14.x or v0.16.x slot, maintainer's choice).

## Consequences

- **Easier:** direct managed-Prometheus push with no new dependencies; reuses
  the metric-building path; same env-token/HTTPS security model as the other
  push targets.
- **Harder / trade-offs:** a hand-rolled binary encoder to maintain — mitigated
  by round-trip tests, a real-receiver smoke, and v1-only scope. Remote-write
  **2.0** (string-interning symbol table) is explicitly out of scope; revisit via
  a superseding ADR if a backend requires 2.0.
- **Revisit triggers:** (1) a target requires remote-write 2.0; (2) the
  hand-rolled protobuf proves too brittle to maintain → fall back to Option B
  (`[remote-write]` extra); (3) demand never materialises → the code is small and
  isolated in a `remotewrite` module, cheap to keep or drop.
- **Relationship to ADR-0006:** consistent — both keep the zero-dependency
  posture. ADR-0006 chose hand-rolled JSON because the OTLP *SDK* was the heavy
  option; here the analogous heavy option (protobuf+snappy libs) is likewise
  avoided, and the literals-only snappy insight is what makes the binary case
  tractable by hand.
