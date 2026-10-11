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

## Amendment — Phase 1c load harness (2026-10-09)

`pat infer-benchmark` drives load against a vLLM engine the operator runs and
records each level through `infer-observe`'s gates.

- **pat never launches engines.** Starting vLLM, downloading models and
  choosing tp stay with the operator (one run per tp); pat only sends
  completion requests, in line with A1.
- **Two submit policies, one loop.** Closed loop (N in flight) produces
  calibration points with a steady batch; open loop (Poisson λ, in-flight
  capped at 512, rejections counted) produces the per-layer SLO-capacity sweep.
  An open sweep stops at the first saturated level. Saturation is judged from
  the client side — completions below 90 % of the arrivals that fed them, a
  queue, rejections, or a preemption/Little's-law refusal — because an
  overloaded engine is steady *at capacity* and the observe gates alone would
  record it and keep sweeping (adversarial review). The arrivals are counted
  over the hold shifted back by the mean latency: comparing with the hold's own
  arrivals leaves the change in in-flight requests as error, about 5–7 % at the
  minimum window and enough to flag a healthy level (found as CI flakiness).
- **Artefacts prevented, not detected.** Lockstep: initial submits are
  staggered and `max_tokens` jittered ±20 % (mean preserved), because
  identical lengths synchronise completions and inject a periodic prefill
  spike no gate sees. Prefix cache: every prompt is fresh seeded random words
  behind a unique nonce, and `infer-observe` refuses windows with a prefix-cache
  hit rate ≥ 1 % or speculative-decoding draft tokens. Length drift: two
  probes (256 and 512 words) separate tokens per word from the fixed nonce/BOS
  overhead, which a single probe folded into the ratio and which refused every
  short-prompt level; a level whose mean prompt misses P by more than 5 % is
  refused. Output length is exact under `ignore_eos`, so a level is refused if
  any completion differs from its requested `max_tokens`, rather than
  comparing a jittered sample mean with O.
- **No GPU time on doomed runs, no lost results.** tp, window and minimum
  requests are validated before any load, and `P + 1.2·O` is checked against
  the engine's `max_model_len`. The report is rewritten after every level and
  kept, with the error, on failure or interrupt.
- **H3 restated.** The sweep records server-side TTFT p99 per level, so H3
  becomes "per-layer SLO capacity (largest λ meeting the SLO) predicted within
  20 %", which implies the crossover. Level spacing ×1.25 resolves ±12 %;
  computing λ_max from a sweep report is the next step.
- **Deferred.** Energy per level (DCGM through the E1a gate), streaming
  client-side TTFT, and the λ_max / λ* computation in `infer-validate`.

## Amendment — Phase 1d capacity validation (2026-10-10)

`pat infer-validate --sweep` compares each layout's predicted λ_max with the
λ_max its open-loop sweep measured (H3). Design reviewed with the advisor
before implementation.

- **Measured λ_max is a bracket.** Per SLO, a level passes when recorded,
  unsaturated and within the SLO; fails when saturated or recorded above it;
  otherwise (refused, not saturated) it carries no information. λ_hi is the
  first failing level and λ_lo the highest passing level below it; passing
  levels above λ_hi are flagged `non_monotone`, never averaged away. The point
  estimate is the geometric midpoint. The metric is not interpolated: the
  failing level is usually saturated with no gated TPOT, and latency is
  convex in λ, so a chord is biased.
- **Error is an interval.** `to_bracket` is 0 inside the bracket, else the
  distance to the nearer edge; `worst` the distance to the farther edge, both
  relative to the measured edge. H3 passes at worst ≤ 20 %, fails (the kill
  criterion) at to-bracket > 25 %, and is inconclusive otherwise, as is any
  non-monotone bracket (the measurement itself is suspect). An unrefined
  ×1.25 bracket passes only a prediction in [λ_lo, 1.2·λ_lo]; within that band
  the verdict holds for every truth in the bracket, but where the prediction
  lands near λ_hi it is inconclusive (adversarial review corrected an earlier
  claim here that unrefined sweeps can never pass).
- **`--refine K` on the sweep.** After the sweep, K geometric bisection levels
  run inside each measured bracket (saturation, and TPOT when
  `--tpot-slo-ms` is given), classified exactly as the validator does; an
  uninformative level ends that bracket's refinement. K = 2 narrows ×1.25 to
  about ±3 % for roughly two extra levels per bracket, so the verdict no
  longer depends on where in a wide bracket the truth sits.
- **Predicted λ_max uses the recommender's predicate** (feasible, ρ ≤ 0.95,
  SLOs met), bisected in log λ to 0.5 %, on the sweep's target P and O (fixed
  before the sweep; the harness already refuses levels off P).
- **Say what was tested.** The SLO row records which constraint ends the
  predicted range (the SLO, or ρ = 0.95) and why the measured λ_hi failed
  (recorded over the SLO, or saturated). A mismatch is inconclusive; when both
  are utilisation, the row is marked `latency_tested: false`, because a pass
  then validates capacity with a 5 % offset, not the latency model.
- **The capacity row is a mechanism check, not H3.** It compares the model's
  capacity (ρ = 1) with client-side saturation, which trips somewhat below
  ρ = 1 depending on window and burstiness, and words its verdict
  consistent / inconsistent so it is never read as the kill criterion.
- **TTFT only on bucket edges.** vLLM's TTFT histogram buckets are coarse and
  `histogram_quantile` interpolates inside them, so the reported p99 can be
  off by up to 2×. Every level now records the cumulative TTFT fraction at
  each edge (`ttft_cdf`), and a TTFT SLO is accepted only on an edge, judged
  as fraction ≥ 0.99 — exact, no interpolation. TPOT (the window mean, which
  is what the model predicts) is the primary SLO for the pilot. SLOs are
  pre-registered after calibration and before the sweeps, and chosen to bind
  inside the swept range for at least one layout.
- **Raw readings on refused levels.** A refused window keeps its ungated
  readings (`observed`: TPOT, batch, queue, completions) next to the reason,
  never as a point, so the failing level of a bracket still says where the
  engine was.
- **λ\* replaced by a ranking at equal budget.** With a fixed budget the
  switching rate is trivially the smaller layout's λ_max. The claim becomes
  the order of λ_max between layouts with the same GPU count, predicted vs
  measured, reported as agreeing only when the measured brackets do not
  overlap and are monotone; predictions within 1 % are a tie and give no
  predicted order. The calibrated model puts tp2×1 and tp1×2 within about 2 %
  of each other for an 8B model on L40S, so the ranking needs refined
  brackets and may be decided by the SLO rather than the KV cache. Until 1e,
  sweeps of replica layouts (`instances` > 1) are refused, so the ranking is
  implemented but not yet reachable from measured data (lifted in 1e).
- **Sweep reports are unsigned operator files.** The validator checks schema,
  open-loop mode, a clean end (no `error`), level shape (unique,
  non-decreasing TTFT edges; printable model label), one workload (model,
  P, O) across sweeps and the profile's model name when set, and echoes each
  file's SHA-256 with the profile commitment. Signing belongs to the Phase 2
  bundle.
- **Deferred.** Layouts with n ≥ 2 replicas need `instances` in the harness
  and an observe gate for exactly n engine series (vLLM data parallelism);
  until then a sweep is one engine and `instances` must be 1. Energy per
  level (DCGM via E1a) and the dated EU price catalog follow.

## Amendment — Phase 1e replica layouts (2026-10-10)

Replica layouts (n ≥ 2 engines of tp GPUs) become measurable, so the λ_max
ranking at equal GPU budget (I10) can be tested. Design reviewed with the
advisor before implementation.

- **Topology: independent servers, random client-side routing.** The model
  splits λ evenly and treats each instance as its own M/M/c. Uniformly random
  routing splits a Poisson stream into n independent Poisson streams of λ/n,
  so with one endpoint per replica (`--endpoint` repeated) the measured object
  is the modelled one. Round robin or join-shortest-queue, which vLLM's
  data-parallel balancer approximates, smooths arrivals and lowers the batch
  variance — unmodelled advantages that could decide a ranking the model
  puts within 2 %. One endpoint fronting n engines (`--instances n`) is still
  read, but the report records `routing: "server"` and the validator marks
  those rows as outside H3. Whether vLLM 0.31 still steps dense-model
  data-parallel ranks in lockstep is unverified; that is a second reason the
  pilot uses independent servers.
- **Per-engine gates, all or nothing.** Engines are told apart by
  `(instance, engine)`. Exactly n series must be present at the evaluation
  instant and over the whole window, and each engine passes every
  single-engine gate on its own at the shared instant (`--min-requests` is per
  engine). Any refusal refuses the level and carries the first overload code
  (`little`, `preemption`), so an overloaded replica still reads as
  saturation.
- **Balance gate.** Engine batches must agree with their mean within 25 % for
  two replicas, scaled by √(log₂ n) because the largest of n noisy
  deviations grows like √(ln n) (about 43 % at n = 8). A level refused for
  imbalance (`refusal_code: "imbalance"`) carries no capacity information
  unless a hard signal (a queue on any engine, rejected arrivals, an
  overload refusal) still shows saturation.
- **Endpoint faults are not capacity.** Under client routing, failures on
  some endpoints only mark the level `refusal_code: "endpoint"`; the missing
  completions are kept out of the throughput signal, so a dead replica cannot
  cap the bracket at whatever level it died (adversarial review found this
  case classified as saturation).
- **Pooled per-instance point.** Mean batch, token-weighted TPOT and
  request-weighted lengths: what the model's `t(b_eff)` at λ/n predicts.
  Per-engine readings travel in `observed.engines`; the worst engine is
  visible, not judged. TTFT pools every engine of the model, which under
  random routing is the per-instance distribution.
- **Saturation: any engine.** A queue on any engine saturates the level, next
  to the global client-side signals, also when the window was refused for
  another reason (its ungated readings keep the queue). The in-flight cap
  scales with the number of endpoints, so it does not reject arrivals below a
  replica layout's capacity. This biases measured λ_max slightly low,
  in the same direction as the existing client-side trip; the capacity row's
  definition says so for replicas.
- **Validation only.** Replica sweeps are open loop only, never print
  calibration points, and their point rows carry `instances`, which
  calibration refuses as an unknown key. `extrapolated` stays true for
  n ≥ 2, so replica layouts test the router and host cost (α_replica = 0)
  out of sample rather than fit it.
- **Before load.** Every endpoint's context is checked and every endpoint
  sizes the same probe prompts; disagreement (different model or tokenizer
  behind the replicas) is refused, as are `--engine` with replicas (every
  engine of the model is read) and series that mix present and missing
  `instance` labels.
- **Risks.** Two vLLM frontends on one host contend for CPU, raising the
  effective per-step overhead against a single-engine calibration; a replica
  miss may be real physics that α_replica = 0 omits, so host CPU belongs in
  the run notes. Near capacity, random-routing noise can queue one engine
  first, a few percent below the even-split capacity; TPOT remains the
  primary SLO.
- **Deferred to 1f.** DCGM energy per level through the E1a gate and the dated
  EU GPU price catalog (1f took the catalog; energy moved to 1g).

## Amendment — Phase 1f price catalogs (2026-10-11)

H4 (energy-optimal vs cost-optimal layout) needs a price per layout that is
not simply proportional to GPUs. Design reviewed with the advisor before
implementation; DCGM energy per level moves to 1g.

- **No prices in the package.** pat ships the schema
  (`presidio-hardened/gpu-price-catalog@1`), the loader, the pricing rule and
  a `--template` skeleton without prices. Fixtures use provider `example` on
  `example.invalid`. List prices move monthly and per tier; keeping catalogs
  fresh sits on the closed side of the open-core line, and a stale shipped
  price would end up in someone's audit.
- **Strict, decimal, EUR.** Unknown keys, duplicate keys and non-finite
  constants are refused; currency is EUR and VAT excluded by schema; prices
  are decimal strings bounded to 0.05–500 EUR per GPU-hour after normalising
  basis (instance or GPU) and unit (hour or minute). Every price carries an
  https source URL (never fetched) and a UTC retrieval time.
- **Committed and dated.** `pat infer-prices FILE --commit OUT` writes a
  SHA-256 over the canonical content; loading fails closed on any change and
  never re-blesses a committed file. A catalog is one retrieval campaign:
  every price retrieved on `as_of` or up to 30 days before it and never in
  the future, no `as_of` more than a day ahead. At use,
  older than 30 days warns and is marked stale, older than 90 days is refused
  with no override. Output cites name, `as_of`, digest, selection and every
  price used with its source.
- **One family, matched hardware.** Provider, region, GPU and tier must
  resolve to exactly one family (ambiguity lists the candidates, never picks
  the cheapest). The priced GPU must match the analysed memory and bandwidth
  within 1 % and the calibration's `gpu_type`, and `--gpus-per-node` must
  cover the largest instance sold. `--price-catalog` and `--cost-per-gpu-hour`
  are mutually exclusive; output without a catalog is unchanged.
- **Instance packing.** A layout (tp, n) is priced on one SKU of the family:
  among SKUs holding a TP group, ⌊g/tp⌋ engines per instance, ⌈n/⌊g/tp⌋⌉
  instances, at least the committed tier's minimum, cheapest SKU wins; a
  layout no SKU holds has no price. Replicas may share an instance, which
  holds for VMs but not every bare-metal offer. The recommendation stays the
  fewest GPUs meeting the SLO; `cheapest` reports the lowest-cost qualifying
  layout beside it, so the two can be compared (H4).
- **Review fixes.** Adversarial review found catalog labels reaching Rich
  markup in refusal messages (escaped now), whole-euro prices cited as
  `1E+1` (plain decimals now), a hardware gate that checked only one SKU per
  family (declared bandwidth now splits families and every SKU is checked),
  future retrieval times, and options silently ignored by
  `infer-prices --commit` / `--template` (refused now).
- **Bounded.** Hourly prices only (no egress, storage or network); committed
  and spot tiers carry term and interruption semantics the hourly model
  ignores, so the tier is always printed. The digest makes an audit
  reproducible, not correct.

