# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For the change history of releases prior to 0.7.0, see the Version Registry in
`PRESIDIO-REQ.md`.

## [Unreleased]

### Changed

- Trove classifier promoted from `Development Status :: 3 - Alpha` to
  `Development Status :: 4 - Beta`, reflecting v0.7.0 maturity. (#24)

### Tests

- Backfilled offline/error/fallback coverage for the security and cloud-pricing
  modules — `security.py`, `cloud.py`, `cloud_gcp.py`, and `cloud_azure.py` are
  now all ≥99% covered. All paths are mocked; no live API calls. (#23)

## [0.7.0] - 2026-06-10

### Added

- **`pat calibrate` (analytical mode).** Fits the per-replica capacity model —
  concurrency (κ) and coordination overhead (β) — to one or more measured
  `rps:latency_ms:replicas` observations via `scipy.optimize.curve_fit`, writes
  the fitted parameters to `~/.pat/model.json`, and prints a per-observation
  prediction/residual table plus overall R² and RMSE. No Docker required. Adds
  `scipy` as a runtime dependency.
- **Envelope warning.** `pat analyze`, `cost`, `what-if`, and `slo` warn on
  stderr when no calibrated model (`.pat-model.json` or `~/.pat/model.json`)
  exists, naming the reference workload envelope (~50–2000 req/s, ~10–250 ms)
  and suggesting `pat calibrate`. The warning is suppressed once a model is
  calibrated.

### Fixed

- **Replica over-provisioning for async workloads.** The capacity model assumed
  serial, single-in-flight replicas (~12 rps/replica), recommending ~64
  container replicas for the 500 rps / 80 ms async reference workload.
  Per-replica capacity now models async concurrency
  (`concurrency × 1000/latency`, default 8 ≈ 100 rps/replica), and the
  recommendation selects the fewest replicas that saturate demand. The reference
  workload now lands at 6 container replicas.
- **Cost-per-request display precision.** Sub-microdollar `Cost/request` values
  no longer truncate to `$0.000000`. The formatter switches to scientific
  notation below `$1e-4` and keeps up to 8 significant figures above it, applied
  across `pat cost`, `pat analyze --show-all`, and `pat demo`.

[Unreleased]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.7.0
