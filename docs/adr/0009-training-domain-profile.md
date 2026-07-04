# ADR-0009: Extend translucency to ML training as a domain profile, not new layers

- **Status:** Accepted (2026-07-04) — governs the v0.18.0 Training Arc.
- **Deciders:** Vladimir Stantchev (maintainer)
- **Related:** PRESIDIO-REQ.md "Training Arc"; suite strategy deliberation of
  2026-07-02 (persisted in `presidio-projects-overview/analysis/`);
  [ADR-0005](0005-model-file-location-project-overrides-global.md) (model file
  precedence, reused for the `training` calibration section); presidio-evidence
  ADR-0002 (provenance-parents convention, first adopted here).

## Context

Architectural translucency states that the same measure (replication) has
different throughput/response-time implications at different layers. To date
`pat` answers this for the **serving** domain (container / pod / deployment /
node). The suite strategy positions the presidio family as the compliant
implementation environment for organizations adopting AI — and between
"classify the use case" (eai-classificator → ikigov) and "operate it
compliantly" (ikigov gates, x402 payment gating, pat serving posture) there is
a gap: nothing covers the **build/train** phase.

Distributed training exhibits exactly the translucency phenomenon: the same
replication decision applied as data parallelism (DDP), sharded data
parallelism (FSDP/ZeRO-3), tensor parallelism, or pipeline parallelism yields
different throughput and different overhead. The question is how to model it
in `pat` without distorting the serving model.

Forces:

1. **The serving abstraction (α fixed overhead, β·ln(δ) coordination cost,
   per-layer max δ, calibration overrides) transfers cleanly** for data,
   sharded-data, and tensor parallelism — β's semantics change (gradient
   all-reduce, all-gather + reduce-scatter, per-layer activation all-reduce)
   but the functional form holds as an MVP approximation.
2. **Two semantics do NOT transfer.** (a) Serving throughput is demand-capped
   (`min(capacity, rps)`); training is compute-bound with no demand cap.
   (b) Serving treats overhead as a soft penalty in the recommendation score;
   in training, per-device memory is a *feasibility* question — a DDP replica
   that does not fit in device memory is not "worse", it is impossible.
3. **Pipeline parallelism has an exact, well-known overhead form** — the
   bubble fraction `(δ−1)/(m+δ−1)` for m microbatches — which the α/β form can
   only approximate.
4. **Adding training strategies to `ReplicationLayer` would poison the serving
   model:** every serving code path (cost, scaler, exporter, rules) iterates
   the layer enum.

## Decision

We will model training as a **separate domain profile** — a parallel module
(`training.py`) with its own strategy enum (`data`, `fsdp`, `tensor`,
`pipeline`), its own CLI surface (`pat train-analyze`, `pat train-what-if`,
`pat train-evidence-emit`), and the same recommendation objective (most
throughput gain, fewest devices) — rather than extending the serving layer
enum or generalizing the serving engine.

- **α/β reused where honest, exact where cheap.** `data`/`fsdp`/`tensor` use
  `efficiency(δ) = 1 − α − β·ln(δ)`; `pipeline` uses
  `(1 − α) · m/(m + δ − 1)`.
- **Memory is a hard constraint.** Infeasible (strategy, δ) points are
  excluded from the sweep, not scored down. `data` requires the full model
  state per device; sharded strategies require `model/δ`, under a 0.9
  headroom reserve for activations/fragmentation (MVP approximation).
- **Throughput is uncapped** (compute-bound domain).
- **Calibration reuses the ADR-0005 model-file mechanism**: a `training`
  section in `.pat-model.json` / `~/.pat/model.json` overrides per-strategy
  α/β. Fitting from recorded step-time logs (NVML/DCGM ingestion) is deferred.
- **Evidence follows the v0.17.0 key-less pattern**: `training-run@1` Layer-0
  records emitted unsigned, signed by the bridge sidecar. The payload adopts
  the family **provenance-parents convention** (presidio-evidence ADR-0002):
  content hashes of the upstream evidence that authorized the run
  (classification, gate decision) are attested *inside* the signed content.

### Option considered and rejected: generalize the serving engine

A single generic engine with pluggable "domains" (layer set + overhead
function + constraint set) was considered. Rejected for the MVP: it would
churn every serving call site for zero user-visible gain, and the two models
share only ~30 lines of math. Revisit if a third domain appears (e.g.
inference batching or federated learning — `presidio-hardened-fl` is the
natural candidate).

## Consequences

- Easier: the suite story closes over the training phase — a training run
  becomes an optimization decision *and* a compliance artifact (EU AI Act
  Art. 12 record-keeping / GPAI compute documentation) in one step.
- Easier: workshop narrative for regulated verticals (healthcare, Annex III /
  Dec 2027) can demonstrate classify → gate → train → evidence end-to-end.
- Harder: two domain models to calibrate and document; defaults for training
  α/β are MVP placeholders until step-time fitting lands.
- Revisit: (a) step-time log ingestion + fitting; (b) hybrid/3D parallelism as
  strategy composition; (c) a degraded-training-throughput broker tie-in in
  x402 (same pattern as the SLO payment broker — **check provisional patent
  claim breadth before anything public**); (d) engine generalization on the
  third domain.
