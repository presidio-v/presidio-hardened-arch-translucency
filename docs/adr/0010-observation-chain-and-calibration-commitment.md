# ADR-0010: Tamper-evident observations (hash chain) and calibration commitments

- **Status:** Accepted (2026-07-05) — governs the evidence-hardening pass.
- **Deciders:** Vladimir Stantchev (maintainer)
- **Related:** [ADR-0002](0002-observe-single-shot-process-model.md) (the
  observation store this hardens); [ADR-0004](0004-calibrate-global-analytical-fit.md)
  and [ADR-0005](0005-model-file-location-project-overrides-global.md) (the
  calibrated-model file the commitment binds); the family evidence profile in
  `evidence_producer.py` (canonical JSON, SHA-256 content addressing,
  floats-rejected); the Computational Jurisprudence program (Stantchev,
  arXiv 2026) — "evidence by cryptography, not by mutable logs".

## Context

Two portfolio creed violations lived in this tool. (1) The observation store
(`~/.pat/observations.db`) is mutable SQLite: history can be silently rewritten,
so a recommendation "based on 200 samples" has no defence that those 200 rows
are the ones that were recorded. (2) Calibrated model parameters (κ/β) enter
`pat analyze`'s recommendation with no binding to the observations that produced
them — the model file can be hand-edited and the recommendation shifts with no
trace.

Forces:

1. **Additive only.** Existing databases and model files must keep working; the
   `observations` schema is pinned by tests to an exact column set, and callers
   must not have to migrate.
2. **Float discipline.** The family canonical profile rejects bare floats
   (`evidence_producer._reject_floats`) because they are not portable across
   encoders. But rps / latency / throughput and κ/β/R²/RMSE are naturally
   floats, and the existing rounding convention (`round(p99_latency_ms)`) is
   *lossy* — two distinct observations would collide under a hash of rounded
   values.
3. **Honest legacy handling.** Pre-chain rows and pre-commitment model files
   exist in the wild; verification must report them honestly, not pretend
   coverage and not reject them.
4. **No overclaiming.** A local hash chain proves the history was not rewritten
   after the fact; it cannot prove the readings were honest at capture time.

## Decision

We will make the observation history **tamper-evident** and **bind calibrated
parameters to their inputs**, following the family evidence profile exactly.

- **Hash-chained observations.** A parallel `observation_chain` table (not new
  columns on `observations`, keeping the measurement schema untouched) stores
  one link per chained observation: `record_hash = SHA-256(canonical({content,
  prev_hash, seq}))`, where `content` is the observation's own fields, `seq` is
  the gap-free chain position, and `prev_hash` is the previous link's
  `record_hash` (or a documented genesis sentinel `"0"*64` for the first).
  `pat observe verify` walks the chain in `seq` order and reports the first
  break; binding both `seq` and `prev_hash` makes edit, deletion, insertion, and
  reorder all detectable. `pat observe` measures nothing new; chaining is O(1)
  work appended on insert.
- **Lossless string-decimal encoding.** Every float reading is hashed as
  `repr(float(x))` — the shortest string that round-trips to the same IEEE-754
  double — the repo's string-decimal encoding for numeric readings. This honours
  the "no bare floats in a hash" discipline (the string is portable) without the
  precision loss of rounding.
- **Legacy prefix is UNVERIFIABLE, not verified.** Rows with no chain link
  (recorded before chaining) are counted and reported as an unverifiable legacy
  prefix; a clean report requires full chain coverage. CLI exit codes are
  machine-distinct: `0` = intact and fully covered, `1` = chain broken, `2` =
  intact suffix with incomplete legacy coverage. `--allow-legacy` downgrades only
  exit `2`; a broken chain is never overridden.
- **Calibration commitments.** `pat calibrate` writes a `calibration_commitment`
  = `{schema, digest}` into the fit record, where `digest` is a SHA-256 over the
  canonical calibration inputs (observation set) and outputs (κ, β, R², RMSE,
  point count). `pat analyze` (via `resolve_calibration_commitment`) re-hashes
  the *stored* parameters and **fails closed** when a present commitment no
  longer matches — the model file was edited after calibration. The
  recommendation output carries the commitment digest for provenance.
- **Uncommitted legacy models are reported, not rejected.** A model file written
  before commitments has no `calibration_commitment` key; it is classified
  `legacy` and used, with the output stating the parameters are unbound. A NEW
  calibration always writes the commitment. No opt-in strictness flag is added:
  the repo has no precedent for one, and the fail-closed behaviour is scoped to
  *present-but-mismatched* commitments so existing files never break.

## Consequences

- Easier: the observation history and the calibrated parameters both become
  content-addressed artifacts — the honest path stays O(1) local, and silent
  rewriting of either is detectable without a server or a trusted log.
- Easier: `pat observe verify` and the analyze-time gate give the workshop
  narrative a concrete "your evidence is tamper-evident" demonstration.
- Harder: two more hash surfaces to keep byte-stable; the string-decimal
  encoding must not drift from `observe.py` / `calibrate.py` (pinned by tests).
- Bounded claim (documented in README and PRESIDIO-REQ): the chain proves the
  local history was not rewritten after the fact relative to the chain head; it
  does **not** prove the readings were honest at capture time. A signed,
  externally-anchored chain head (or emitting the chain head as `evidence-ref@1`)
  is the natural next step and is deferred.
