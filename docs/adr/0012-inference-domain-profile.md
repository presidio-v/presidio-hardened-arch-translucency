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
- **Equations** (per instance): KV tokens `T = (tp·h·M − W)/k`;
  `b_max = min(B_max, ⌊T/(P + O)⌋)` with the conservative full footprint (a
  sequence must fit at its longest); decode step
  `t(b) = (W + b·c·k)/(tp·η·BW) + (α + β·ln tp)·W/(η·BW) + t₀`, where
  `c = P + O/2` is the mean live context a decode step reads
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
- **Parameters.** Tensor α/β (`hybrid` shares them) plus three global engine
  parameters: bandwidth efficiency η, per-step overhead t₀, prefill rate R_pre.
  α_replica is fixed at 0: it is not identifiable from single-instance data
  (it merges into t₀), and the multi-instance router cost is not modelled. The
  defaults are MVP placeholders. The KV bytes per token k is architectural and
  never fitted.
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
- Revisit: (a) vLLM metric presets for `pat observe`; (b) L-INF-1/L-INF-2;
  (c) router cost for n ≥ 2, which needs multi-instance measurements.

## Amendment — Phase 1a calibration (2026-10-09)

`pat infer-calibrate` fits the engine parameters from measured points and
`pat infer-validate` reports prediction error, so the paper's error metric is
computed by `pat` itself.

- **Point.** One steady-state window on one instance at fixed tp: mean running
  batch, mean inter-token latency, optional mean prefill time (measured at low
  load) and optional mean KV-cache usage, which replaces the `P + O/2` estimate
  with the measured live context `usage·T/b`.
- **Fit.** Linear in `(x = 1/(η·BW), t₀, x·W·α_t, x·W·β_t)`; that solution
  starts a bounded least-squares refinement in natural units. With tp ∈ {2, 4}
  α and β are nearly collinear, so noise can push them past physical bounds;
  the bound then binds and is reported (`at_bound`) instead of refusing the
  fit. On synthetic data with 3% noise, a fit on tp ∈ {1, 2, 4} predicts
  held-out tp = 8 points within ~5% MAPE while α sits at its bound. With one
  distinct tp > 1, β is held. At least one degree of freedom is required; R²
  is reported only with two or more.
- **Named profiles, born committed.** `model["inference"][<profile>]` binds
  the hardware (W, k, BW, M, h, B_max, optional model/GPU/engine labels), the
  parameters, the fit metadata and every point. Consumers use a profile only
  via `--calibration NAME`, fail closed on tamper, and fail closed when the
  analysed W, k, BW or M differs from the profile by more than 1%. Engine
  overrides cannot be combined with a profile. h and B_max are not checked —
  the fitted parameters do not depend on them, so "what if I raise h" is a
  legitimate calibrated question — but differences are reported.
- **Global store only.** Profiles are read from `~/.pat/model.json` with the
  strict reader the write path uses, never from a project-local
  `.pat-model.json` (ADR-0005): a repo's serving calibration must not hide
  them, and a profile shipped inside a repository carries no key and would
  certify itself. The profile name is part of the hashed content, so a record
  copied under another name is refused. The commitment follows the family's
  string-decimal rule: tools that renormalise numbers (`4.0` → `4`) break it.
- **Extrapolation is visible.** With `--calibration`, each configuration is
  flagged `extrapolated` when its tp exceeds the largest calibrated tp or
  n ≥ 2 (router cost is never observed by single-instance points). That is
  exactly the region H3 tests, so it is marked, not hidden.
- **Leave-one-out scores only full folds.** A fold that falls back to a
  default (β when the held-out point was its only second tp level) is
  excluded and counted as degraded, so placeholder error never enters the
  paper's figure.

## Amendment — Phase 1b observation (2026-10-09)

`pat infer-observe` reads one window of one vLLM engine from Prometheus and
prints it as a calibration point.

- **Pinned names.** vLLM 0.31 metric names (`vllm:inter_token_latency_seconds`,
  `vllm:kv_cache_usage_perc`, `vllm:request_prefill_time_seconds`, request
  token histograms, `vllm:request_success_total`, `vllm:num_preemptions_total`).
  The two names older engines used for TPOT and KV usage are reachable through
  validated overrides, never by guessing.
- **Refuse, don't record.** All queries share one evaluation instant. A
  window is refused unless it is steady: exactly one engine series now and
  across the whole window (`model_name` / `engine` labels, escaped into the
  selector), no counter reset, no preemption, a running batch that never
  reached 0 with CV < 0.25, completions within ±50% of Little's law
  `batch / (prefill + O·TPOT)` — the single check that catches ramps, restarts
  and double counting — a window of at least 5 mean request latencies (token
  means are observed at completion and over-sample short requests in short
  windows), and enough completed requests. Preemption-only gating, the first
  draft, let ramp and restart windows through (adversarial review).
- **Prefill only at low load.** Prefill time is included only with an empty
  queue and a small batch; under load it is inflated by queueing and decode
  interference and would bias R_pre.
- **No new store.** The point goes to stdout for a points file; the
  calibration commitment binds it once fitted. A chained inference observation
  store is deferred until the benchmark harness needs it.
- **Bounded claim.** tp is the operator's statement (vLLM exposes none).
  Mean inter-token latency is token-weighted, so larger-batch steps count
  more; with the decode step affine in b the bias is `s1·Var(b)/E[b]`, which
  the CV gate bounds.
- **Bounded claim.** η is an effective bandwidth: measured inter-token latency
  includes chunked-prefill interference. Fitting single-instance points
  validates timing; the crossover prediction (H3) also depends on b_max, router
  cost and queueing, so it needs held-out *configurations* (other tp, n ≥ 2)
  and λ sweeps per layer.
