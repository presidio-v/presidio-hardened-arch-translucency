# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For the change history of releases prior to 0.7.0, see the Version Registry in
`PRESIDIO-REQ.md`.

## [Unreleased]

### Added

- **`pat annotate` — post recommendations to Grafana (v0.12.0, "Visualize &
  Annotate").** The arc's first **outbound write**: runs the analysis and posts
  an annotation to Grafana's `/api/annotations` so the recommendation shows up as
  a marker on dashboards. Still emit-only in spirit — it writes an informational
  annotation, never infrastructure. The token is read from `PAT_GRAFANA_TOKEN`
  only (never a flag, never logged), HTTPS is required unless `--insecure-http`
  (warned), tags are sanitised, and `--dry-run` previews the payload without
  posting. `--dashboard-uid` scopes to one dashboard; `--tag` adds tags. `urllib`
  only — no new dependency.
- **Grafana provisioning bundle (v0.12.0).** `grafana/provisioning/` adds
  datasource + dashboard-provider configs so `grafana/pat-dashboard.json` loads
  automatically on startup (mount instead of importing by hand). See
  `grafana/README.md`.

- **`pat rules` — Prometheus recording + alerting rules (v0.11.0, "Alert").**
  Emits a declarative Prometheus rule file (YAML) built from the exporter's
  metrics, so the model's signals fire through the existing Prometheus /
  Alertmanager pipeline. Recording rules normalise the metrics
  (`pat:predicted_rps`, `pat:observed_rps`, `pat:demand_growth_ratio`,
  `pat:trend_ratio`); alerts are **PatDemandSurgeForecast**,
  **PatDemandTrendRising**, **PatExporterAbsent** (always),
  **PatLayerTranslucencyMismatch** (`--current-layer`), and
  **PatCostPerRequestOverBudget** (`--cost-budget`). `--demand-surge-ratio`,
  `--trend-threshold`, and `--for` tune the thresholds. Emit-only — `pat`
  produces the YAML and never loads or applies it. The layer name, numeric
  thresholds, and `for:` duration are validated, and every YAML scalar is
  double-quoted/escaped, so the rule file is always valid and cannot smuggle
  content. No new dependencies (hand-rolled YAML, like `hpa_patch`). Second step
  of the monitoring-integration arc.

## [0.10.0] - 2026-06-17

### Added

- **`pat export` — read-only Prometheus exporter (v0.10.0).** Publishes the
  architectural-translucency model's per-layer recommendations for a workload as
  Prometheus gauges on a read-only `GET /metrics` endpoint, for scraping into
  Prometheus/Grafana. Exposes `pat_recommended_replicas`,
  `pat_estimated_throughput_rps`, `pat_response_time_ms`,
  `pat_throughput_gain_ratio`, and `pat_layer_recommended` (per `layer`), plus
  `pat_workload_*` inputs and `pat_build_info`. `--once` prints the exposition
  and exits instead of serving. The server is read-only (only `GET` is
  implemented; other methods return `501`) and binds `127.0.0.1` by default —
  binding a routable interface requires the explicit `--listen-public` opt-in.
  Metric names are fixed and label values escaped. Exposition text is generated
  by hand (Prometheus text format 0.0.4); no new dependencies. First step of the
  monitoring-integration arc ("The Translucency Control Plane").
- **`pat export --predict` — forecast metrics from the observation store
  (v0.10.0 Phase 2).** When `--predict` is set, the exporter also runs an
  `optimize` pass over `~/.pat/observations.db` on every scrape and exposes the
  live forecast: `pat_predicted_rps{model}`,
  `pat_predicted_recommended_replicas{layer}`, `pat_observed_rps`,
  `pat_observed_latency_ms`, `pat_optimize_trend_ratio`,
  `pat_optimize_horizon_minutes`, and `pat_optimize_samples` (reads `0` on an
  empty store). `--model arima` adds 95% CI bounds (`pat_predicted_rps_lower`/
  `_upper` and matching replica bounds); `--window`, `--horizon-minutes`,
  `--predict-layer`, and `--db` tune the pass. SMA is the default (cheap); ARIMA
  refits on each scrape. Turns the exporter into the moving front of the
  observe → predict → visualize loop.
- **`pat export --cost-per-replica-hour` — cost metrics (v0.10.0).** Adds
  per-layer `pat_cost_per_request` and `pat_hourly_cost_usd` gauges from a
  uniform replica cost. (Live cloud pricing stays in `pat cost`; the exporter
  keeps one uniform rate to remain scrape-cheap and network-free.)
- **Official Grafana dashboard (`grafana/pat-dashboard.json`, v0.10.0).** A
  ready-to-import dashboard visualising observed-vs-predicted demand (with the
  ARIMA CI band), recommended replicas per layer, response time, throughput
  gain, and cost-per-request. A test keeps every metric the dashboard queries in
  sync with what the exporter actually emits. Completes the v0.10.0
  monitoring-integration scope (Expose → predict → visualize).

### Fixed

- Command help and version-only exits skip the on-run dependency audit, avoiding
  network work and false CVE warnings while rendering help. Normal command
  execution still runs `pip-audit` unless `--skip-audit` is supplied.

## [0.9.0] - 2026-06-17

### Added

- **`pat observe daemon` — continuous collection (v0.9.0).** `pat observe daemon
  install` writes a platform-native scheduler unit that fires `pat observe` on an
  interval: a launchd LaunchAgent
  (`~/Library/LaunchAgents/eu.presidio-group.pat.observe.plist`) on macOS, or a
  systemd `--user` `.service` + `.timer` (`~/.config/systemd/user/`) on Linux;
  other platforms error. `install` accepts `--prometheus`, `--layer` and
  `--interval` (default 60 s). `daemon uninstall` removes the unit(s) (and
  best-effort `launchctl bootout` on macOS); `daemon status` reports
  loaded/running, installed-but-inactive, or not-installed. This is an opt-in
  convenience on top of cron/launchd — observe stays single-shot (decision D2 is
  extended, not reversed): the scheduler invokes it, it does not become a
  long-running process. No new dependencies (`subprocess` / `pathlib` /
  `shutil`).
- **Per-layer `pat calibrate` (v0.9.0 Phase 1).** `pat calibrate --layer <name>`
  tags an observation set with a service layer and fits per-layer model
  parameters, upserting them into `~/.pat/model.json` under `layers.<name>`
  while preserving the global (pooled) fit and every other layer. `--show-global`
  prints the pooled fit alongside a per-layer fit. `pat analyze`, `what-if`,
  `slo`, and `optimize` accept `--layer` to select the calibrated per-layer
  capacity, falling back to the global fit then the built-in default. The model
  file stays backward-compatible: a pre-v0.9.0 file with no `layers` key resolves
  exactly as before. Delivers the per-layer fitting half of design decision D4. (#37)
- **Docker benchmark mode for `pat calibrate` (v0.9.0).** `pat calibrate
  --benchmark` sweeps a set of replica counts (`--replicas`, default `1 2 4`) on
  the local Docker daemon, load-testing each count with the same Monte Carlo
  workload as `pat demo`, then fits the model to the measured throughput/latency
  instead of user-supplied `--observation` points. `--requests`, `--concurrency`,
  and `--iterations` tune the load; `--layer` writes per-layer parameters as in
  analytical mode. At least two distinct replica counts are required, and
  `--benchmark` is mutually exclusive with `--observation`. Workload containers
  are published to `127.0.0.1` only. Completes the remaining (benchmark) half of
  design decision D4 — no new dependencies (reuses the `demo` Docker harness).
- **Configurable ARIMA order bounds for `pat optimize` (v0.9.0 Phase 3).** The
  AIC order grid is no longer hard-coded: `--max-p`, `--max-d` and `--max-q`
  set the upper bounds of the `p`/`d`/`q` sweep (defaults `3`/`2`/`3`, exactly
  reproducing the previous 4×3×4 = 48-model search). `--auto-diff` replaces the
  `d` sweep with a single differencing order chosen by a dependency-free
  variance heuristic — raw vs. first- vs. second-difference variance — which
  shrinks the search and side-steps guessing `d`; the heuristic's choice is
  capped at `--max-d`. All flags are optional and only affect `--model arima`.

### Security

- **Prometheus bearer auth hardened.** `pat observe --prometheus` no longer reads
  kubeconfig bearer tokens automatically. Bearer auth is now env-only via
  `PAT_PROMETHEUS_TOKEN`, token use requires an HTTPS Prometheus URL, and
  Prometheus URLs/query strings reject control characters.
- **Daemon unit generation hardened.** `pat observe daemon install` validates
  scheduler inputs, rejects control characters, quotes systemd `ExecStart=`
  arguments, escapes systemd `%` specifiers, and writes generated unit files
  owner-only where supported.
- **Audit log context redaction tightened.** Security log context now redacts
  scalar values whose keys look credential-bearing (`token`, `secret`,
  `password`, `key`, `credential`, or `auth`) instead of passing them through.
- **Demo isolation tightened.** `pat demo` publishes Docker ports to
  `127.0.0.1` only. The embedded workload image now runs as an unprivileged user
  and includes a healthcheck.
- **Local store permissions tightened.** Default observation and calibrated
  model stores create `~/.pat` owner-only and chmod store files to `0o600` where
  supported.
- **Security policy refreshed.** `SECURITY.md` now records current supported
  versions, security features, known limitations, and the 2026-06-16 and
  2026-06-17 audit reports.

### Fixed

- **AWS cloud demo pricing result handling.** `pat demo --cloud aws` now handles
  both flat and tiered pricing results and uses the on-demand tier for demo cost
  parameters.
- **Linux observe-daemon timer cadence.** The systemd `--user` timer now uses
  elapsed scheduling (`OnBootSec` / `OnUnitActiveSec`) rather than an invalid
  seconds-field `OnCalendar` expression for default and custom second intervals.

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

[Unreleased]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.10.0...HEAD
[0.10.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.9.0
[0.8.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.8.0
[0.7.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.7.0
