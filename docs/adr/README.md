# Architecture Decision Records

This directory records the significant architecture decisions for
`presidio-hardened-arch-translucency`.

## Convention

We use [MADR](https://adr.github.io/madr/) (Markdown Any Decision Records),
lightly trimmed. Each record is a Markdown file named
`NNNN-short-title.md` (zero-padded sequence number) and contains:

- **Title** — `# ADR-NNNN: <decision>`
- **Status** — `proposed` | `accepted` | `superseded` | `deprecated`. When a
  later ADR extends or reverses an earlier one, the status line links to it.
- **Context** — the forces at play: the problem, constraints, and prior state.
- **Decision** — what we decided, stated in the active voice ("We will …").
- **Consequences** — what becomes easier or harder as a result, including
  follow-up work and known trade-offs.

ADRs are immutable once accepted: to change a decision, add a new ADR that
supersedes or extends the old one, and update the old one's status line to point
at it. Do not rewrite history.

## Index

| ADR | Decision | Status |
|-----|----------|--------|
| [0001](0001-foundation-first-delivery-order.md) | Foundation-first delivery order (observe → optimize → calibrate → prometheus → hpa) | Accepted |
| [0002](0002-observe-single-shot-process-model.md) | `pat observe` is single-shot (cron/launchd), not a daemon | Accepted — extended by v0.9.0 daemon mode |
| [0003](0003-prometheus-auth-env-token.md) | Prometheus auth via env token only | Accepted — extended by v0.9.0 kubeconfig auth |
| [0004](0004-calibrate-global-analytical-fit.md) | `pat calibrate` fits global κ+β analytically | Accepted — extended by v0.9.0 per-layer fitting |
| [0005](0005-model-file-location-project-overrides-global.md) | Model file at `~/.pat/model.json`; project-local `.pat-model.json` overrides global | Accepted |
| [0006](0006-otlp-export-transport.md) | OTLP export via hand-rolled OTLP/HTTP+JSON (Collector-targeted), not the OpenTelemetry SDK | Accepted — governs v0.13.0 |
| [0007](0007-prometheus-remote-write.md) | Prometheus remote-write via hand-rolled v1 (protobuf + literals-only snappy), zero deps | Accepted |
| [0008](0008-helm-chart-packaging.md) | Package the exporter as a static Helm chart — emit-only, RBAC-free, rules via `.Files.Get` | Accepted — governs v0.16.0 |

ADRs 0001–0005 (D1–D5) were locked on 2026-06-10 alongside the v0.8.0
"autoresearch" release and are backfilled here for traceability. The canonical
decision log remains the "v0.8.0 Design Decisions" section of `PRESIDIO-REQ.md`;
these ADRs restate it in the standard format.
