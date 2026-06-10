# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For the change history of releases prior to 0.7.0, see the Version Registry in
`PRESIDIO-REQ.md`.

## [Unreleased]

### Added

- **Per-layer `pat calibrate` (v0.9.0 Phase 1).** `pat calibrate --layer <name>`
  tags an observation set with a service layer and fits per-layer model
  parameters, upserting them into `~/.pat/model.json` under `layers.<name>`
  while preserving the global (pooled) fit and every other layer. `--show-global`
  prints the pooled fit alongside a per-layer fit. `pat analyze`, `what-if`,
  `slo`, and `optimize` accept `--layer` to select the calibrated per-layer
  capacity, falling back to the global fit then the built-in default. The model
  file stays backward-compatible: a pre-v0.9.0 file with no `layers` key resolves
  exactly as before. Delivers the per-layer fitting half of design decision D4
  (the Docker `--benchmark` mode remains deferred). (#37)

## [0.8.0] - 2026-06-10

The **autoresearch** release: an observe→optimize loop that records live
workload measurements and turns them into proactive scaling recommendations,
plus an apply-able HPA manifest. Built foundation-first per the v0.8.0 design
decisions (D1–D5 in `PRESIDIO-REQ.md`).

### Added

- **`pat observe` — source-agnostic observation store (Phase 1).** Records a
  single workload measurement (`--layer`, `--rps`, `--avg-latency-ms`,
  `--p99-latency-ms`, `--throughput`, `--replicas`) into a local SQLite store at
  `~/.pat/observations.db`, or lists recent rows with `--list`. Single-shot by
  design — schedule recurring collection externally (cron / launchd /
  Kubernetes CronJob). `--db` overrides the store path. (#26)
- **`pat optimize --model sma` — proactive scaling recommendation (Phase 2).**
  Reads the observation store, smooths the most-recent samples with a simple
  moving average (`--window`, default 10), projects demand `--horizon-minutes`
  ahead, and recommends the replica count to serve it. (#27)
- **Prometheus observation source (Phase 3).** `pat observe --prometheus <url>
  --layer <layer>` scrapes one sample (rps, p99, replica count) from the
  Prometheus HTTP API and records it with `source='prometheus'`. Single-shot;
  bearer token read from `PAT_PROMETHEUS_TOKEN` only — never a CLI argument,
  never logged. (#28)
- **`pat optimize --model arima` — ARIMA time-series forecast (Phase 4).** Fits
  a `statsmodels` ARIMA model with a 95% confidence interval and emits a replica
  range alongside the point recommendation. Automatically falls back to SMA when
  fewer than 30 samples are available, with a stderr notice. Adds `statsmodels`
  as a runtime dependency. (#29)
- **`pat optimize --emit-hpa-patch` — HPA manifest emitter (Phase 5).** Emits an
  apply-able `HorizontalPodAutoscaler` manifest to stdout for a `--target`
  Deployment (optional `--namespace`). `minReplicas` is the point
  recommendation; `maxReplicas` is the ARIMA upper-CI bound when available. The
  manifest is sanitised — target/namespace are validated as RFC 1123 names, no
  user input is echoed raw. (#30)

### Changed

- **Dropped Python 3.9; minimum supported version is now 3.10.** The CI matrix,
  trove classifiers, and `ruff target-version` are 3.10–3.12. This was required
  to pull patched releases of several transitive dependencies (see Security).
  (#32)
- Trove classifier promoted from `Development Status :: 3 - Alpha` to
  `Development Status :: 4 - Beta`, reflecting v0.7.0 maturity. (#24)

### Security

- **Resolved all 19 open Dependabot vulnerability alerts** on the default
  branch by dropping Python 3.9 and bumping the affected transitive
  dependencies to their patched versions in `uv.lock`. (#32)

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

[Unreleased]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.8.0
[0.7.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.7.0
