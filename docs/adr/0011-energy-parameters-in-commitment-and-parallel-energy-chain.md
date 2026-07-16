# ADR-0011: Energy parameters in the calibration commitment; parallel energy-observation chain

- **Status:** Accepted (2026-07-14) — founder release gate. §1 is implemented
  in v0.20.0; §2 governs the v0.21.0 implementation.
- **Deciders:** Vladimir Stantchev (maintainer)
- **Related:** [ADR-0010](0010-observation-chain-and-calibration-commitment.md)
  (the commitment and chain disciplines this extends);
  [ADR-0004](0004-calibrate-global-analytical-fit.md) /
  [ADR-0005](0005-model-file-location-project-overrides-global.md) (the fit
  record the energy parameters join); the Energy Arc deliberation in
  `PRESIDIO-REQ.md` (invariant E1).

## Context

v0.20.0 introduces fitted energy parameters (standing power `P_idle`, dynamic
energy `e_dyn`, coordination `β_E`) and v0.21.0 will introduce measured energy
readings. Both need an integrity story, and both have an ADR-0010 precedent
pulling in a specific direction:

1. **Where do fitted energy parameters live?** If they are fitted into the same
   record as κ/β, the v0.19 `calibration_commitment` digest binds them with
   zero new verification code — `pat analyze` already re-hashes the stored
   record and fails closed on mismatch. Stored anywhere else, they are unbound:
   a hand-edited `energy_idle_w` would silently shift every Watts/J-req/EEI
   figure and, from v0.22, budget recommendations.
2. **Where do measured energy readings live?** The `observations` measurement
   schema is test-pinned and its rows are content-hashed into the ADR-0010
   chain; new columns would break the pin and perturb existing record hashes.

Forces: additive-only evolution of *hash surfaces* (a v0.19 commitment must
keep verifying under v0.20+ code, byte-identically); the family float
discipline (no bare floats in hashed content — shortest round-trip decimal
strings); honest legacy handling; invariant E1 (measure/model/evidence, never
actuate).

## Decision

**§1 — Energy parameters join the committed fit record (v0.20.0, implemented).**
`pat calibrate --energy-observation` writes `energy_idle_w`,
`energy_dyn_j_per_req`, `energy_beta`, `energy_r_squared`, `energy_rmse`, and
the energy observation set into the same fit record as κ/β. The committed
content includes the energy fields **only when present in the record**: a
record without them re-hashes byte-identically to the v0.19 scheme, so every
existing committed fit verifies `ok` unchanged; a record with them binds them —
tampering any energy field flips the record to `tampered` and every model
consumer (`pat analyze`, `pat what-if`, `pat slo`) fails closed through the
unchanged ADR-0010 gate. When a named-layer analysis takes its energy fit from
the *global* record (the resolve_concurrency-style fallback), the gate verifies
that record too — the commitment checked is always the one the rendered energy
figures came from. Energy floats are hashed as `repr(float(x))` per the family
string-decimal discipline.

**§2 — Measured energy readings get a parallel chained table (v0.21.0, to
build).** Energy readings are NOT new columns on `observations`. A parallel
`energy_observations` table stores the measurements, hash-chained with the
identical ADR-0010 discipline: `record_hash = SHA-256(canonical({content,
prev_hash, seq}))`, genesis sentinel `"0"*64`, joules/watts encoded as
round-trip decimal strings. `pat observe verify` walks **both** chains and
reports each; exit-code semantics (0 intact / 1 broken / 2 legacy-incomplete,
`--allow-legacy` downgrades only 2) apply per the ADR-0010 rules, with a break
in either chain yielding exit 1.

**§2 amendment (2026-07-13, L-EN-3 spike; corollary E1a):** the chain only ever
receives **measured** watts — *pat never signs a watt it did not measure*.
Concretely, v0.21 measured mode is platform-gated and fail-closed:

- At `pat observe` start, probe for a direct hardware power source (a readable
  node-exporter RAPL counter, or DCGM for accelerators). Absent
  one, exit non-zero with a clear "no power source on this platform" message
  and write **no** rows to `energy_observations`. No estimator fallback, no
  fidelity-warning mode: attributed or synthetic workload joules are not direct
  measurements, and a signed estimate would weaponise exactly the capture-time
  honesty gap ADR-0010 concedes.
- Reject estimator tells explicitly: drop any sample carrying
  `components_power_source="estimator"`, or `cpu_architecture="unknown"` —
  refused at the door, not detected after the fact.
- Pin node-exporter's `node_rapl_package_joules_total` and DCGM's
  `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION`. Kepler is not accepted: current
  releases support a synthetic CPU meter and proportionally attribute node
  energy to workloads, while retaining an indistinguishable metric/zone shape.
- Derive watts with `increase(counter[window]) / window`, using the exact same
  whole-second window stored in the observation.
- Require a process-local collector seal at the public persistence API, and
  require consumers to verify the complete chain from a read-only SQLite
  snapshot. The seal narrows the supported API; it is not remote attestation.
- The analytic model (§1) remains the honest cross-platform answer and is
  always labelled as modelled — it never enters the chain and never becomes an
  `energy-reading@1` (the family enum deliberately has no `"analytic"` meter).

## Consequences

- Easier: fitted energy coefficients become tamper-evident on day one — the
  workshop claim "your energy numbers are bound to the observations that
  produced them" holds from the first fitted watt, and v0.22 budget outputs
  inherit it.
- Easier: v0.21 storage lands as a copy of a proven mechanism rather than a new
  design; `energy-reading@1` (v0.24) can carry the energy-chain head using the
  same head-hash convention ADR-0010 deferred.
- Harder: the committed-content builder now has a conditional branch — the
  byte-identity of the no-energy path is pinned by a regression test and must
  stay pinned (any future committed field must follow the same
  conditional-on-presence rule).
- Harder: two chains to walk in `verify` from v0.21; report rendering must keep
  the per-chain legacy prefixes distinguishable.
- Bounded claim, restated honestly (unchanged from ADR-0010): commitments and
  chains prove *post-hoc rewriting* is detectable; they do not prove readings
  were honest at capture time. External anchoring of chain heads remains the
  v0.24 deliverable.
