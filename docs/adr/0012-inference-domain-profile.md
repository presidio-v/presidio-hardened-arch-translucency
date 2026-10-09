# ADR-0012: LLM inference serving as a third domain profile, KV capacity as the mechanism

- **Status:** Accepted (2026-10-09) — governs the Inference Arc, Phase 0.
- **Deciders:** Vladimir Stantchev (maintainer)
- **Related:** [ADR-0009](0009-training-domain-profile.md) (domain-profile
  pattern, engine-generalization revisit trigger);
  [ADR-0010](0010-observation-chain-and-calibration-commitment.md)
  (calibration commitments); PRESIDIO-REQ.md "Inference Arc".

## Context

The productization plan of 2026-10-09 targets LLM inference serving: a paper
("Where to Replicate an LLM") whose publishable claim is that a cheap,
calibrated translucency model predicts *where the best replication layer
switches* out of sample, plus a fixed-scope EU inference placement audit and a
signed EU inference benchmark index. All three need an inference profile; `pat`
has none.

Inference exhibits the translucency phenomenon directly. With a GPU budget N,
the same model can be served as N single-GPU replicas, one tensor-parallel (TP)
instance, or replicas of TP groups. Forces:

1. **The mechanism is KV-cache capacity, not only coordination.** A replica
   holds the full weights W on each GPU, leaving `h·M − W` for KV cache; a TP
   group pools `tp·h·M − W`. As `W → h·M` the replica's concurrent-sequence
   budget collapses while TP's does not. This is a hard capacity effect the
   serving model's α/β form cannot express.
2. **Decode is memory-bandwidth bound** at the batch sizes vLLM runs: each step
   reads the weight shard and the live KV. TP divides both across GPUs and pays
   an all-reduce per layer — the α/β·ln(tp) form fits that overhead.
3. **Demand is an input, latency is the SLO.** Operators size against a request
   rate and TTFT/TPOT targets, so the objective is fewest GPUs meeting the SLO,
   not maximum throughput (training's objective).
4. **ADR-0009 named "inference batching" as the third domain** that would
   trigger revisiting engine generalization.

## Decision

Model inference as a **separate domain profile**: `inference.py` (math) and
`inference_cli.py` (`pat infer-analyze`, `pat infer-what-if`), registered in
`cli.py` like `demo`. The serving `ReplicationLayer` enum and the training
module are untouched.

- **Configurations, not strategies, are searched.** Every `(tp, n)` with
  `tp ∈ {1, 2, 4, 8, 16}`, `tp ≤ gpus_per_node`, `tp·n ≤ N`. The label is
  derived: `replica` (tp = 1), `tensor` (n = 1), `hybrid` (both > 1).
- **Equations** (per instance, s̄ = P + O, the conservative full-occupancy
  footprint): KV tokens `T = (tp·h·M − W)/k`; `b_max = min(B_max, ⌊T/s̄⌋)`;
  decode step `t(b) = (W + b·s̄·k)/(tp·η·BW) + (α + β·ln tp)·W/(η·BW) + t₀`
  — coordination is charged against the *unsharded* weight read, so the
  all-reduce cost does not shrink with tp (a multiplicative form made TP=8
  ~5× faster than TP=1 on an 8B model, far above observed vLLM scaling); prefill
  `t_pre = P/(R_pre·tp·(1 − α − β·ln tp))`; service `S(b) = t_pre + O·t(b)`;
  in-flight batch from Little's law `b = λ_i·S(b)` — closed form since S is
  affine in b; TTFT = t_pre + M/M/c wait (c = b_max, Erlang C via the Erlang-B
  recursion); TPOT = t(b_eff).
- **Hard constraints, as in ADR-0009.** Weights that do not fit, or a KV pool
  too small for one sequence, are infeasible — excluded, never scored. A
  configuration with `b_eff ≥ b_max` is saturated: reported with its capacity,
  never recommended. Above ρ = 0.95 a configuration is flagged as near
  saturation and not recommended either: the M/M/c tail grows without bound
  as ρ → 1, and a TPOT-only SLO would otherwise accept it.
- **Objective.** Among feasible configurations with ρ ≤ 0.95 meeting both SLOs,
  the fewest GPUs; ties by lowest TPOT, then TTFT p99. If none qualifies, no
  recommendation; the highest-capacity configuration is shown as best effort.
- **Parameters.** Per strategy α/β (`hybrid` shares `tensor`'s; replica has
  β = 0 since ln 1 = 0) plus three global engine parameters: bandwidth
  efficiency η, per-step overhead t₀, prefill rate R_pre. All are MVP
  placeholders. The KV bytes per token k is architectural and never fitted.
- **No model-file section in Phase 0.** Training read an uncommitted
  `training` section first and had to carry a legacy path once commitments
  arrived. The `inference` section is born with `pat infer-calibrate` (Phase 1)
  *with* an ADR-0010 commitment from its first write. Until then overrides are
  explicit CLI/API arguments only, and every output says "modelled,
  uncalibrated".
- **Engine generalization is deferred again.** The third domain shares the
  α/β form but not the structure (configuration search, KV constraint, queueing
  latency). A generic engine would still churn every serving call site for no
  user-visible gain.

### Options considered and rejected

- **Add `pipeline` and `node` as strategies.** `node` is not a strategy in
  inference: it is the constraint `tp ≤ gpus_per_node`; cross-node replicas
  only add router cost, already in α. Inference pipeline parallelism pipelines
  *requests*, not microbatches, so training's bubble form would be a fake α/β.
  Deferred as L-INF-1 (pipeline) and L-INF-2 (cross-node TP/PP with an
  interconnect parameter).
- **Maximise goodput instead of minimising GPUs.** Demand is an input for an
  operator; maximum goodput is reported as capacity and best effort.

## Consequences

- Easier: one command answers "replicas or TP for this model on these GPUs at
  this load", and the crossover is predicted conditionally on `W/(h·M)` — the
  hypothesis the paper pilot (Phase 1) tests against vLLM.
- Easier: the audit deliverable and the index reuse the same equations.
- Harder: a third profile to document and calibrate.
- Bounded claim: TTFT p99 is M/M/c-derived and ignores prefill–decode
  interference. With a large `max_num_seqs` the queueing wait is near zero
  until ρ approaches 1, so TTFT p99 is mostly prefill time and rarely binds;
  calibration must show whether that holds. TPOT is a mean; $/Mtok counts
  output tokens only. Out of scope: compute-bound decode, chunked
  prefill, prefix caching, speculative decoding, MoE (weights read per step ≠
  W), quantized KV, heterogeneous GPUs, energy (no DCGM path for inference yet;
  E1a stands), EU price catalog. `pat` emits a recommendation only (A1).
- Revisit: (a) `pat infer-calibrate` from vLLM metrics with commitments;
  (b) vLLM metric presets for `pat observe`; (c) L-INF-1/L-INF-2; (d) the mean
  footprint `P + O/2` once calibration shows the conservative s̄ bias.
