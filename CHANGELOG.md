# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

For the change history of releases prior to 0.7.0, see the Version Registry in
`PRESIDIO-REQ.md`.

## [Unreleased]

## [0.19.0] - 2026-07-05

### Added

- **Hash-chained observations (evidence-hardening).** Each new observation is
  linked into a per-store SHA-256 hash chain (parallel `observation_chain`
  table; the `observations` measurement schema is untouched). A new
  `pat observe verify` verb walks the chain and reports the first break,
  detecting any post-hoc row edit, deletion, insertion, or reorder relative to
  the chain head. Rows recorded before chaining existed carry no link and are
  reported as an UNVERIFIABLE **legacy prefix** — never counted as verified.
  Numeric readings (rps / latency / throughput) are hashed as shortest
  round-trip decimal strings, following the family's float discipline
  ([ADR-0010](docs/adr/0010-observation-chain-and-calibration-commitment.md)).
- **Calibration commitments (evidence-hardening).** `pat calibrate` now writes
  a `calibration_commitment` digest into the model file: a SHA-256 over the
  calibration inputs (the observation set) and outputs (fitted κ/β, R²/RMSE).
  `pat analyze` re-hashes the stored parameters and **fails closed** if they no
  longer match the commitment (tamper detection); an uncommitted legacy model
  is reported as such, never rejected, and the recommendation output now carries
  the commitment digest for provenance.

### Notes

- Honest scope: the observation chain proves the local history was not rewritten
  after the fact relative to the chain head; it does **not** prove the readings
  were honest at capture time. Grounded in the Computational Jurisprudence
  program (Stantchev, arXiv 2026) — "evidence by cryptography, not by mutable
  logs".
- Additive only: existing databases and model files keep working; no migration
  is required.

## [0.18.1] - 2026-07-04

### Changed

- Documentation pass: producer docs, docstrings, CLI help, and agent-skill
  files now describe downstream verification generically ("downstream
  family consumers") instead of naming specific consumers.
- ADR-0009 accepted (status update + index row).

### Added

- `test_evidence_producer.py` now pins the family `slo-reading` golden
  vector (content hash and deterministic signature) from
  presidio-evidence v0.2.1, matching the existing `training-run` pin
  (L-EV-7).

## [0.18.0] - 2026-07-02

**Training arc (MVP) · "Same question, new domain".** The architectural
translucency question — *at which layer does replication yield the highest
throughput gain with the lowest overhead?* — extended from serving
(container / pod / deployment / node) to ML **training** parallelism.

### Added

- **`training.py` — training parallelism domain model.** Strategies `data`
  (DDP), `fsdp` (FSDP/ZeRO-3), `tensor`, `pipeline` as the training analog of
  replication layers. `data`/`fsdp`/`tensor` reuse the α/β efficiency form
  `1 − α − β·ln(δ)`; `pipeline` uses the exact bubble formula
  `(1 − α) · m/(m + δ − 1)` for `m` microbatches. Two deliberate departures
  from the serving model: **per-device memory is a hard feasibility
  constraint** (infeasible (strategy, δ) points are excluded, not scored down;
  `data` holds a full replica, sharded strategies hold `model/δ` under a 0.9
  headroom reserve) and **throughput is compute-bound** (no demand cap).
  Per-strategy α/β are calibratable via a `training` section in
  `.pat-model.json` / `~/.pat/model.json`; fitting from step-time logs is
  deferred past the MVP.
- **`pat train-analyze`** — cross-strategy recommendation (most gain, fewest
  devices, memory-feasible only) with a Rich table; **`pat train-what-if`** —
  evaluate one (strategy, degree) point, `--json` for automation.
- **`training-run@1` Layer-0 evidence + provenance-parents convention.**
  `build_training_run_reading()` / **`pat train-evidence-emit`** emit a
  key-less, unsigned training-run record (run id, strategy, degree, samples/s,
  duration, devices, optional `model_hash`/`dataset_hash`) for the
  signing-bridge sidecar — the same pattern as `slo-reading@1`. The payload
  carries **`parents`**: content hashes of upstream evidence (classification,
  gate decision) attested *inside* the signed content, turning family
  envelopes into a verifiable provenance DAG without touching the frozen
  `evidence-ref@1` envelope. Fail-closed validation of parent hashes against
  the family lowercase-hex discipline; floats rejected on the wire (rounded
  upstream). EU AI Act Art. 12 record-keeping / GPAI compute documentation as
  a by-product of the optimization tool.

### Security

Remediation of the 2026-07-02 third-party release-gate audit
(`presidio-third-party-audits/arch-translucency-third-party-security-audit-v0-18-0.md`;
status in `SECURITY-AUDIT-2026-07-02-v0.18.0-remediation.md`):

- **Finite-input validation (audit P1).** New `sanitize_bounded_number`
  rejects `nan`/`inf`/out-of-range on every training CLI number; the library
  independently guards all model math (`TrainingDomainError`) so API callers
  get the same fail-closed behavior. Invalid input → clean exit 2
  (analyze/what-if) or 1 (evidence-emit), never a traceback.
- **`training-run@1` contract enforced in the library (audit P1).**
  `build_training_run_reading` now validates strategy against the domain set,
  rejects control characters / blank / >512-char `run_id`, rejects negative
  or non-integral numerics (no silent truncation — rounding is the caller's
  explicit decision), and wraps conversion failures in
  `EvidenceProducerError`. The security log records a SHA-256 digest of
  `run_id`, never the raw value.
- **`train-what-if` domain guard (audit P2).** `evaluate_strategy` rejects
  `degree` outside `[1, max_degree]` — out-of-domain configurations can no
  longer be reported as feasible.
- **CI `pip-audit` is now blocking on push (audit P2)** — advisory only on
  pull requests, so a release cannot ship with known-vulnerable dependencies.

## [0.17.0] - 2026-06-21

**Evidence arc · "Sign the signal" (L-EV-3).** arch-translucency's runtime-posture
degradation signal becomes authenticatable so downstream family consumers can
verify it fail-closed before acting on it.

### Added

- **`evidence_producer.py` — signed runtime-posture evidence.** Turns a degradation
  reading / `Observation` into a `presidio-hardened/evidence-ref@1` envelope (canonical
  JSON + SHA-256 Layer 0; Ed25519/HMAC detached signature Layer 1). Golden-vector pinned
  to the family wire format; optional `[evidence]` extra (`cryptography`).
  `observation_to_evidence()` maps p99 latency (rounded to an int — the canonical profile
  rejects floats).
- **Key-less Layer-0 emit** — `build_layer0_reading` / `observation_to_layer0` /
  `is_degraded`, plus the **`pat evidence-emit`** CLI command. Emits an *unsigned*
  Layer-0 SLO reading as JSON (from an explicit p99 or the latest stored observation),
  only when the observed p99 breaches the target (`--always` to override). Pipe it to the
  signing-bridge sidecar, which adds the signature — `pat` itself never holds a key.

### Security

- **Key-less posture preserved.** The producer ships as a library primitive and
  `pat evidence-emit` emits unsigned readings only — the `pat` runtime holds **no signing
  key**. Signing runs in a separate bridge sidecar that holds the Ed25519 key (mirrors
  `treasury` in evidence ADR-0001, "no secrets to steal"); x402 holds only the **public**
  key. The bridge re-verifies the reading's `content_hash` before signing. See
  `PRESIDIO-REQ.md` "Evidence Arc (v0.17.0)".
- **Third-party audit remediation.** The optional Ed25519 dependency is now included in
  the CI `pip-audit` release gate, `cryptography` is major-bounded and lock-pinned,
  Ed25519 keys are enforced as raw 32-byte lowercase-hex seeds, and the local
  observation-store trust boundary is documented and regression-tested.
- Pin the audit extra's transitive `msgpack` dependency to `>=1.2.1`, remediating
  GHSA-6v7p-g79w-8964 in `uv.lock`.

## [0.16.0] - 2026-06-20

### Added

- **`charts/pat-exporter` — Helm chart (v0.16.0, "Package & operate").** The
  seventh and final step of the monitoring-integration arc: cluster-native
  packaging of the read-only `pat export` endpoint. `helm install` deploys a
  hardened `pat export` Deployment serving `/metrics`, a Service, and a
  no-privilege ServiceAccount; opt-in flags add a Prometheus-Operator
  `ServiceMonitor`, a `PrometheusRule` (the verbatim `pat rules` recording +
  alerting groups, injected via `.Files.Get` so Prometheus's own `{{ $value }}`
  templating survives Helm rendering), the official Grafana dashboard as a
  sidecar-discovered `ConfigMap`, and a `NetworkPolicy` restricting ingress to
  the metrics port. Operator/sidecar objects default **off** so the chart
  installs on any cluster. **Hardened by default:** `runAsNonRoot`, non-root UID
  10001, `readOnlyRootFilesystem`, all capabilities dropped,
  `seccompProfile: RuntimeDefault`, and `automountServiceAccountToken: false`
  (the exporter holds no Kubernetes API credentials). **Emit-only** — the chart
  applies nothing to the cluster and the exporter has no mutation path,
  preserving arc invariant A1. Adds a root `Dockerfile` (slim, non-root) that
  builds the `pat-exporter` image from the published package. No new runtime
  dependencies; `tests/test_chart.py` keeps the bundled dashboard + rules in
  exact sync with their sources and locks the security posture (with an extra
  full `helm template`/`helm lint` render when a `helm` binary is present).

## [0.15.0] - 2026-06-18

### Added

- **`pat scaler` — translucency-aware autoscaling (v0.15.0, "Close the loop").**
  Emits the declarative glue so an autoscaler scales a Deployment to track pat's
  forecast metric `pat_predicted_recommended_replicas` (from `pat export
  --predict`, scraped into Prometheus). `--format keda` (default) emits a KEDA
  `ScaledObject` with a Prometheus trigger (`threshold: "1"` → replicas == the
  prediction); `--format prometheus-adapter` emits an HPA v2 on an External
  metric (`target.type: Value`, `value: "1"`) plus a commented Prometheus-Adapter
  `externalRules` snippet. `--layer` filters the default query, `--query`
  overrides it, `--min-replicas`/`--max-replicas` bound the range,
  `--namespace`/`--name` set the object. **Emit-only** — prints YAML to stdout;
  `pat` never applies or scales anything (arc invariant A1). Names are RFC 1123
  validated, the URL/query reject control characters and are quoted; hand-rolled
  YAML, no new dependencies. The conceptual payoff of the monitoring arc.
- **`pat export --pushgateway` — Prometheus Pushgateway target (v0.14.0, "Reach
  ephemeral contexts").** Pushes the exporter's metric set once to a Prometheus
  Pushgateway (Prometheus text format) and exits — for cron / CI / Kubernetes
  `Job`/`CronJob` contexts that have no scrape endpoint. `--job` sets the job
  (default `pat`); repeat `--grouping key=value` for grouping labels (validated +
  percent-encoded). Reuses the exporter's existing exposition output verbatim —
  no new dependencies (`urllib` only). Optional bearer token from
  `PAT_PUSHGATEWAY_TOKEN` only (HTTPS required unless `--insecure-http`, control
  characters rejected). `--otlp` and `--pushgateway` are mutually exclusive.
  Prometheus **remote-write** is intentionally deferred (its protobuf + snappy
  wire format would breach the zero-dependency posture — the same tension
  ADR-0006 resolved; Pushgateway already covers the ephemeral-job use case).

### Security

- Outbound Grafana, OTLP, Prometheus, Pushgateway, and scaler Prometheus URLs now reject embedded credentials before request construction, YAML emission, or audit-log host extraction.
- Env telemetry tokens (`PAT_PROMETHEUS_TOKEN`, `PAT_GRAFANA_TOKEN`, `PAT_OTLP_TOKEN`, `PAT_PUSHGATEWAY_TOKEN`) now reject raw control characters before trimming whitespace.

### Fixed

- Grafana datasource provisioning now uses a static Prometheus default URL that Grafana loads correctly; the prior shell-style default expansion could provision an empty datasource URL in Grafana 13.


## [0.13.0] - 2026-06-17

### Added

- **`pat export --otlp` — vendor-neutral OTLP push (v0.13.0, "Speak OTLP").**
  Pushes the exporter's metrics once over OTLP/HTTP+JSON to an OpenTelemetry
  Collector (which fans out to Datadog / New Relic / Honeycomb / Grafana Cloud),
  so pat metrics reach any vendor **without Prometheus**. Single-shot — schedule
  externally for recurring push. Per **ADR-0006** it is hand-rolled OTLP/HTTP+JSON
  (no `opentelemetry` SDK, no `protobuf`, no `grpcio`) targeting a Collector;
  vendor-direct protobuf/gRPC is a non-goal. An optional bearer token is read from
  `PAT_OTLP_TOKEN` only (HTTPS required unless `--insecure-http`); `--service-name`
  sets the OTLP `service.name`; non-finite samples are dropped (no OTLP/JSON
  representation). No new dependencies (`urllib` + `json`). Works with `--predict`
  and `--cost-per-replica-hour`. Third-from-last step of the
  monitoring-integration arc.

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

### Security

- Grafana, OTLP, and Prometheus env bearer tokens now reject control characters before constructing Authorization headers, closing header-injection edge cases while preserving env-only token handling.

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

[Unreleased]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.19.0...HEAD
[0.19.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.18.1...v0.19.0
[0.18.1]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.18.0...v0.18.1
[0.18.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.15.0...v0.16.0
[0.15.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.13.0...v0.15.0
[0.13.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.10.0...v0.13.0
[0.10.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.9.0
[0.8.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.8.0
[0.7.0]: https://github.com/presidio-v/presidio-hardened-arch-translucency/releases/tag/v0.7.0
