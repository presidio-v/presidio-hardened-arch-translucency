# Knowledge Base Schema — presidio-hardened-arch-translucency

<!--
Read this at the start of any session that touches the knowledge base.
Adapted from the x402 knowledge base pattern (Karpathy LLM-wiki, 2026-04-05).
Key difference from x402: no live on-chain data source. Ingest triggers are
CLI version updates, benchmark runs, and integration design changes — not Dune queries.
-->

## Directory layout

```
knowledge-base/
  SCHEMA.md                     ← this file; read first
  arch-translucency.md          ← single-file wiki; primary reference
  design-decisions.md           ← deliberated choices the x402 integration depends on
  log.md                        ← append-only ingest/update record
  raw/                          ← immutable source captures (never edit)
    replication-model.md        ← mathematical model, equations, layer parameters
    cli-api-v0.5.md             ← CLI surface at current version (stable contract)
    papers/
      stantchev-2006-iee.md    ← IEE Proceedings 2006 (foundational theory)
      stantchev-2009-gpc.md    ← GPC 2009 (QoS/SLA in grid/cloud)
    benchmarks/
      README.md
      [date]-[scenario].md     ← pat demo / pat what-if result snapshots
    integration/
      x402-slo-broker-design.md ← planned interface for x402 v0.5.0 integration
```

---

## The three layers

**raw/** is immutable. Never edit existing files — add a new dated version when something changes.

- New CLI version → add `raw/cli-api-vX.Y.md`; cross-reference from wiki
- New benchmark run → add `raw/benchmarks/YYYY-MM-DD-<scenario>.md`
- Integration design change → add new version of `raw/integration/x402-slo-broker-design.md` with date

**arch-translucency.md** is the wiki. Update when new CLI features land, when the mathematical model is revised, or when integration design solidifies.

**design-decisions.md** plays the role of `claims.md` for a software project: it records deliberated choices — especially those the x402 integration will depend on — with their rationale and stability status.

---

## Automated crawlers

A weekly crawler appends new information to this knowledge base between sessions.
**Always read `log.md` first** — crawler entries appear there and may have added new
benchmark captures, flagged CLI changes, or updated the integration design.
Integrate crawler-added content via the ingest workflow before using it for analysis.
Check `design-decisions.md` for any rows the crawler has marked `under-review`.

---

## Operations

### Ingest — new CLI version

1. Add `raw/cli-api-vX.Y.md` capturing the new CLI surface
2. Update `arch-translucency.md` §2 (CLI reference) and §4 (roadmap)
3. Check `design-decisions.md` — does any new feature change a decision marked `stable`?
4. Append to `log.md`

### Ingest — new benchmark result

1. Add `raw/benchmarks/YYYY-MM-DD-<scenario>.md` with full output
2. Update `arch-translucency.md` §3 (empirical parameters) if the result revises a model parameter
3. Append to `log.md`

### Ingest — integration design update (x402 v0.5.0)

1. Add `raw/integration/x402-slo-broker-design-YYYY-MM-DD.md`
2. Update `design-decisions.md` interface rows
3. Cross-reference: update x402 knowledge base `raw/integration/` too
4. Append to `log.md`

### Query

Read `arch-translucency.md` for the current state. For model details read `raw/replication-model.md`. For integration contracts read `raw/integration/x402-slo-broker-design.md`.

### Lint — run before any x402 integration work begins

- **CLI contract**: does `raw/cli-api-v0.5.md` match the current `pyproject.toml` version?
- **Model parameters**: are layer α/β values in `raw/replication-model.md` still the values coded in `model.py`? Grep to verify.
- **Integration interface**: is the `SLOTrigger` / `ArchTranslucencyAdapter` interface in `raw/integration/` still consistent with the current `hpa.py` and `slo` command output format?
- **Stale decisions**: any row in `design-decisions.md` with status `provisional` older than 6 months should be reviewed before x402 v0.5.0 work starts.

### Log entry format

```
## [YYYY-MM-DD] <operation> | <title>
<1-3 sentences: what changed, what triggered it, any notable finding.>
Files updated: arch-translucency.md §N, design-decisions.md
```

---

## Conventions

- Version numbers: match `pyproject.toml` exactly
- Layer names: lowercase (`container`, `pod`, `deployment`, `node`) in code; title case in prose
- Parameters: α (alpha, fixed overhead), β (beta, coordination cost) — consistent throughout
- Equations: use the same variable names as in `model.py` — `intensity`, `throughput`, `response_time`
- Cross-project references: prefix with project name, e.g. `[x402 knowledge-base §5]`
