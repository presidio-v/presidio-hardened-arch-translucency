# ADR-0006: OTLP export transport — hand-rolled OTLP/HTTP+JSON over the OpenTelemetry SDK

- **Status:** Accepted (2026-06-17). Governs the implementation of v0.13.0
  ("Speak OTLP") in the monitoring-integration arc.
- **Deciders:** Vladimir Stantchev (maintainer)
- **Related:** PRESIDIO-REQ.md "Monitoring Integration Arc" (v0.13.0);
  [ADR-0003](0003-prometheus-auth-env-token.md) (env-token auth precedent).

## Context

v0.13.0 of the arc ("Speak OTLP") adds `pat export --otlp <endpoint>` so the
exporter can emit pat's metrics over OTLP — letting Datadog, New Relic,
Honeycomb, Grafana Cloud, and any OpenTelemetry Collector ingest pat data
**without Prometheus**. This makes "Grafana **etc.**" truly *etc.*

The decision is **how** to speak OTLP, and it runs straight into the project's
defining constraint. Every wire format `pat` produces so far is **hand-rolled
with no client library** — Prometheus text exposition 0.0.4 (`export.py`),
Prometheus rules YAML (`rules.py`), Kubernetes HPA YAML (`hpa_patch.py`), and
Grafana annotation JSON (`annotate.py`) all use only `urllib` + `json` + string
building. The current runtime dependency set is small and deliberate (`typer`,
`rich`, `docker`, `matplotlib`, `scipy`, `statsmodels`), and the "hardened"
posture treats every new dependency as added CVE/`uv.lock`/Dependabot surface.

OTLP is a heavier protocol than anything we've emitted:

- **Transports:** OTLP/gRPC (protobuf over gRPC/HTTP2) or OTLP/HTTP (protobuf
  *or* JSON over plain HTTP, `POST {endpoint}/v1/metrics`).
- **Payload:** a nested `ExportMetricsServiceRequest`
  (`resourceMetrics → scopeMetrics → metrics → gauge → dataPoints`), with
  documented JSON-encoding rules (64-bit ints as strings, `timeUnixNano` as a
  string, attribute values wrapped as `{"stringValue": …}` / `{"asDouble": …}`).
- **Recommended architecture:** apps emit to an OpenTelemetry **Collector**,
  which fans out to vendors. The Collector's OTLP/HTTP receiver accepts both
  protobuf and **JSON**.

## Considered options

### Option A — Official OpenTelemetry SDK

`opentelemetry-sdk` + `opentelemetry-exporter-otlp-proto-http` (and/or
`-grpc`).

- **Pros:** canonical and maintained; correct protobuf/JSON encoding, resource
  attributes, retries/backoff; supports gRPC *and* HTTP; works against
  vendor-direct endpoints that only accept protobuf; future-proof as OTLP
  evolves.
- **Cons:** a **large dependency tree** (`opentelemetry-api`, `-sdk`, `-proto`,
  exporter packages, and `protobuf`; `grpcio` is a heavy C-extension if gRPC is
  included). This is the single biggest expansion of the dependency/CVE surface
  in the project's history and breaks the hand-rolled-everything pattern that
  defines the codebase.

### Option B — Hand-rolled OTLP/HTTP + JSON (Collector-targeted) — **chosen**

Build the `ExportMetricsServiceRequest` JSON by hand and `POST` it to
`{endpoint}/v1/metrics` with `urllib`, exactly as `annotate.py` posts to
Grafana.

- **Pros:** **zero new dependencies**; consistent with the four formats we
  already hand-roll; small and fully auditable; reuses the existing
  `Metric`/`Sample` structures the exporter already builds; same `urllib` +
  `S310`-checked-scheme + env-token patterns we already trust.
- **Cons:** **JSON-only, HTTP-only** — no gRPC, and it targets endpoints that
  accept OTLP/JSON (the OpenTelemetry Collector does; some *vendor-direct*
  endpoints accept only protobuf). We own the encoding correctness (the JSON
  rules above) and any future OTLP drift.

### Option C — Hand-rolled OTLP/HTTP + protobuf

Hand-roll the HTTP transport but encode protobuf (vendored generated `*_pb2`
stubs, or manual wire encoding).

- **Pros:** canonical wire format accepted everywhere; HTTP only (no `grpcio`).
- **Cons:** still needs a `protobuf` runtime dependency (or extremely
  error-prone manual wire encoding); the worst of both worlds — added dependency
  *and* hand-rolled complexity. Rejected.

### Option D — Defer v0.13.0

Cut a release of the merged-but-unreleased v0.11.0–v0.12.0 (and run a security
pass on the new authenticated outbound writer) before taking on OTLP.

- Not mutually exclusive with A/B; a sequencing choice, not a transport choice.

## Decision

**We will implement v0.13.0 as Option B: hand-rolled OTLP/HTTP + JSON, targeting
an OpenTelemetry Collector (or any OTLP/HTTP+JSON endpoint).** `pat export --otlp
<endpoint>` builds the `ExportMetricsServiceRequest` JSON from the exporter's
existing metric set and POSTs it with `urllib`, reusing the auth/scheme/token
conventions from `annotate.py` and `prometheus.py` (env-only token, HTTPS
enforced when a token is present).

Rationale: the project's through-line is a **minimal, hand-rolled, hardened**
dependency posture — it is the reason `pat` has shipped four wire formats with
zero client libraries. OTLP/HTTP+JSON is a *documented* encoding that the
OpenTelemetry Collector (the recommended ingestion point, which then fans out to
every vendor) accepts. Choosing B keeps that posture intact and the change
auditable, at the explicit, bounded cost of "Collector-targeted, JSON-only."
Correctness/compatibility genuinely favor Option A, but not enough to justify the
largest dependency expansion in the project's history when the Collector path
covers the stated goal (vendor-neutral ingestion).

## Consequences

- **Easier:** no new dependencies; `uv.lock`/Dependabot/CVE surface unchanged;
  the OTLP encoder is a small, testable pure function (mirrors `render_exposition`),
  and the POST path reuses patterns already covered by tests and prior CodeQL
  passes.
- **Harder / trade-offs:**
  - **JSON-only, HTTP-only.** No gRPC. Documented in `--otlp` help and the
    README: point it at an OTel Collector (or a JSON-accepting OTLP endpoint).
  - We own OTLP/JSON encoding correctness — mitigated by unit tests that assert
    the payload shape against the spec, and (dev-time) validation against a local
    Collector.
  - If real users need **vendor-direct protobuf** or **gRPC**, that is the
    trigger to revisit and add Option A as an optional extra
    (`pip install 'presidio-hardened-arch-translucency[otlp]'`) without changing
    the default zero-dependency install.
- **Revisit triggers:** (1) a user needs a protobuf-only/gRPC endpoint;
  (2) OTLP/JSON encoding proves too brittle to maintain; (3) the OTel project
  deprecates OTLP/JSON. Any of these warrants a superseding ADR adding the SDK as
  an opt-in extra.
- **Sequencing (Option D):** independent of the transport choice — recommend
  cutting v0.11.0–v0.12.0 as a release (with a security pass on `pat annotate`)
  before or alongside v0.13.0, but it does not block this decision.
