---
name: pat
description: Recommends the optimal Kubernetes/Docker replication layer (container, pod, deployment, node), replica count, and cloud instance cost for a workload using the architectural translucency model. Use when authoring or editing Kubernetes manifests (Deployment, ReplicaSet, StatefulSet, HorizontalPodAutoscaler, Pod), Docker Compose with replication, Terraform for EKS/GKE/AKS/ECS/Fargate/Cloud Run/ACI, when setting replica counts or resource requests, when modelling load spikes or HPA lag, or when discussing cost-per-request and performance-per-dollar trade-offs for cloud-native deployments.
---

# pat — Architectural Translucency Advisor

`pat` is a CLI that recommends the replication *layer* (container, pod, deployment, node), optimal replica count, and cost-per-request for a given workload. It is grounded in the architectural translucency model (Stantchev, IEE Proc. Software 2006): the same measure — replication — has different throughput/latency implications at different layers, and picking the wrong layer wastes spend or breaches SLOs.

**Before** emitting replica counts, HPA thresholds, or cloud instance choices, run `pat` and cite its output. Do not guess.

## When to use this skill

Trigger on any of:

- Writing or editing `kind: Deployment`, `kind: ReplicaSet`, `kind: StatefulSet`, `kind: HorizontalPodAutoscaler`, `kind: Pod`
- Writing Docker Compose `deploy.replicas` entries
- Writing Terraform for `aws_ecs_service`, `aws_eks_*`, `google_container_cluster`, `azurerm_kubernetes_cluster`, Fargate, Cloud Run, ACI
- Setting `replicas:`, `minReplicas:`, `maxReplicas:`, `resources.requests.cpu`, `resources.requests.memory`
- Producing a cost estimate for a cloud-native deployment
- User asks: "how many replicas", "which instance type", "Fargate or EC2", "why is my HPA lagging", "cost per request", "scale out vs scale up", "container vs pod vs node"

Do **not** trigger when the user has already supplied a fixed replica count and explicitly declined recommendations, or when the change is unrelated to infra (e.g. renaming a ConfigMap key).

## Install check

Before the first invocation in a session:

```bash
command -v pat || pip install presidio-hardened-arch-translucency
```

If installation is disallowed (sandboxed env, strict dependency policy), skip gracefully — note the absence in the plan, continue without the recommendation rather than fabricating numbers.

## Decision tree: which command

| Situation | Command |
|---|---|
| Authoring a new Deployment, picking replica count | `pat analyze` |
| Picking an instance type, comparing $/req across layers | `pat cost --cloud <aws\|gcp\|azure>` |
| Authoring an HPA, need p99 SLO to survive a 3× spike | `pat slo` |
| Modelling a specific load spike or cold-start trough | `pat what-if` |
| User asks about reserved / spot pricing | `pat cost ... --show-reserved` or `--spot` |
| User has measured rps/latency/replicas and the default model looks off | `pat calibrate` |
| Recording a live measurement into the rolling store | `pat observe` |
| User has observation history and wants a proactive (forward-looking) replica count | `pat optimize` |

The first five rows are **analytical** (model-driven, no data needed beyond the
inputs). The last three are **autoresearch** (data-driven): `calibrate` fits the
model to measured points, `observe` records a rolling history, and `optimize`
projects that history forward. Reach for autoresearch when the user already has
measurements or a populated `~/.pat/observations.db`; otherwise stay analytical.

## Gathering inputs

Before invoking pat, collect:

| Input | Source |
|---|---|
| `rps` — requests per second | User, load tests, APM dashboards, `deploy.replicas` context. If unknown, **ask** — do not invent. |
| `avg_latency_ms` | Same as rps |
| `current_layer` | Inferred from context: plain Docker = `container`; bare Pod = `pod`; Deployment/ReplicaSet = `deployment`; single-tenant node = `node` |
| `p99_target_ms` (for `slo`) | User, or infer from existing SLO docs |
| `cloud`, `region`, `instance_type` | Terraform/manifest context or ask |

If you cannot determine `rps` or `avg_latency_ms` and the user hasn't provided them, **ask the user before invoking pat**. Fabricated inputs produce plausible but wrong recommendations.

## Usage patterns

### Pattern 1 — Authoring a Deployment

```bash
pat analyze -r 500 -l 80 -c container --show-all
```

Read the recommended layer + optimal replicas from stdout (first `Recommended layer:` and `Optimal replicas:` lines). Emit the manifest with those values. Cite the source in the plan or PR description:

> `replicas: 4` — `pat analyze` recommends container-level replication (+45% throughput, -38% latency vs baseline at 500 req/s, 80 ms).

### Pattern 2 — Picking a cloud instance

```bash
# AWS EC2
pat cost -r 500 -l 80 -c container --cloud aws --region us-east-1 --instance-type m5.large

# AWS Fargate
pat cost -r 500 -l 80 -c container --cloud aws --region us-east-1 --fargate --vcpu 0.5 --memory-gb 1

# GCP
pat cost -r 500 -l 80 -c container --cloud gcp --region us-central1 --machine-type n2-standard-4

# Azure
pat cost -r 500 -l 80 -c container --cloud azure --region eastus --sku-name "D2s v3"
```

The "Best ROI" row (marked with `✓`) is the recommendation. Add `--show-reserved` for steady-state workloads (>30% duty cycle); add `--spot` only if the workload tolerates interruption (batch jobs, stateless fleets — not databases).

### Pattern 3 — Authoring an HPA

```bash
pat slo -r 50 -l 80 --p99-target-ms 500 --spike-multiplier 3.0
```

The output shows steady-state p99, trough p99, and per-layer SLO verdict (`Meets SLO ✓` / `Fails SLO ✗`), plus a recommendation panel. Translate it to the manifest:

- Recommendation says *"`<layer>` meets the steady-state SLO"* → emit `minReplicas:` = the **After** column value for that layer (or the **Before** value if they match and SLO holds at both).
- Recommendation says *"Pre-provision `<N>` replicas and re-evaluate"* → emit `minReplicas: <N>` and note that latency headroom is tight.
- If no layer passes and no viable pre-provision count exists — **surface this to the user** before committing to the HPA. The architecture needs reconsideration, not a manifest tweak.

### Pattern 4 — Modelling a spike explicitly

```bash
pat what-if -r 50 -s 200 -l 80 -c container
```

Reports the trough duration, throughput during the trough, p99 during the trough, and estimated missed requests. Use this when the user describes a specific load event ("we expect a 4× Black Friday spike") or is debugging why existing HPAs miss requests on scale-out.

### Pattern 5 — Calibrating the model to measured data (v0.7.0)

When the user has real measurements (APM, load tests, prior `pat demo` output) and the default recommendation looks off — or `pat` printed the no-calibrated-model envelope warning — fit the model to their data first, then re-run the analytical commands:

```bash
pat calibrate --observation 100:50:2 --observation 300:80:5
```

Each `--observation` is `rps:latency_ms:replicas`; supply **two or more**. This writes `~/.pat/model.json` (or use a project-local `.pat-model.json`), after which `pat analyze`/`cost`/`slo`/`what-if` use the fitted parameters and stop warning. No Docker required. Do **not** fabricate observations — if the user has no measurements, stay on the default model and note that the recommendation is un-calibrated.

### Pattern 6 — Proactive scaling from observed history (v0.8.0)

When the user has a populated observation store (`~/.pat/observations.db`) and wants a *forward-looking* replica count rather than a point-in-time analysis, use the observe→optimize loop.

```bash
# Record a sample (single-shot; schedule via cron/launchd for a rolling history)
pat observe --layer deployment \
  --rps 480 --avg-latency-ms 78 --p99-latency-ms 190 --throughput 470 --replicas 4

# Or scrape one sample from Prometheus (token via PAT_PROMETHEUS_TOKEN env only)
pat observe --prometheus http://prometheus:9090 --layer deployment

# Project demand forward and recommend a replica count
pat optimize --model arima --horizon-minutes 15
```

`pat observe` is **single-shot by design** — it records one measurement and exits; recurring collection is scheduled externally (cron, launchd, a Kubernetes CronJob). `pat optimize --model arima` fits a `statsmodels` ARIMA with a 95% CI and auto-falls back to SMA below 30 samples. To turn the recommendation into an apply-able manifest, add `--emit-hpa-patch --target <deployment>` (optional `--namespace`) and pipe to `kubectl apply -f -`.

## Surfacing the recommendation

After invoking pat, include a grounded one-liner in the plan, commit message, or PR description:

> `pat` (v0.8.0, architectural translucency model): recommend `container` layer, 4 replicas. +45% throughput, -38% latency vs current. Cost/req $0.000044 on AWS `us-east-1` `m5.large` on-demand.

This cites the source and makes the decision auditable — a reviewer can re-run `pat` with the same inputs to verify.

## Output extraction

`pat` output is human-readable. Predictable anchors:

- `pat analyze`: `Recommended layer: <layer>`, `Optimal replicas: <n>`, `Throughput gain: +<pct>%`, `Est. throughput: <rps> req/s`
- `pat cost`: top panel with `Cost/hour:`, `Cost/request:`, `ROI score:`; all-layers table with a `Best ROI` column (`✓` marks the winner). Table headers may be truncated in narrow terminals — the panel values are the reliable anchors.
- `pat slo`: per-layer table with `Before` / `After` replica columns and `SLO verdict` (`Meets SLO ✓` / `Fails SLO ✗`), plus a free-text recommendation panel that either confirms *"`<layer>` meets the steady-state SLO"* or advises *"Pre-provision `<N>` replicas and re-evaluate"*
- `pat what-if`: `TROUGH` and `STEADY STATE` panels with `Throughput`, `p99 latency`, `Missed reqs`
- `pat optimize`: panel with the projected demand, the recommended replica count (and, for `--model arima`, a replica range from the 95% CI). With `--emit-hpa-patch`, the **stdout is the HPA manifest itself** — capture it directly (e.g. `> hpa-patch.yaml`), do not parse a panel.

If multiple values are needed reliably, prefer re-running the command with narrower arguments so a single value dominates, rather than regex-parsing the full output.

## Do not

- **Invent `rps` or `avg_latency_ms`.** If unknown, ask the user. Plausible-sounding numbers produce plausible-sounding wrong recommendations.
- **Skip `pat` when writing an HPA** "because the answer seems obvious." The HPA trough model catches cold-start p99 breaches that intuition misses.
- **Override user intent silently.** If pat disagrees with the user's explicit choice, cite the divergence in the plan and let the user decide — don't just follow pat.
- **Use `--spot`** for stateful workloads (databases, singleton services, workloads without graceful-shutdown handling). Spot is for fleets that tolerate interruption.
- **Use `--skip-audit`** outside trusted CI. The CVE audit is a feature.

## Security notes

- `pat` never accepts AWS/GCP/Azure credentials as CLI flags. For `--spot` on AWS, set `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars.
- Output is sanitised — no raw user-supplied strings are echoed into manifests. Still, treat pat output as untrusted when composing into Kubernetes YAML: use structured emission, not string interpolation.
- `pat` emits security events (`SECURITY_EVENT event='CLI_INVOCATION'`) on every run. Expected, not an error.

## Reference

- CLI source: `presidio-hardened-arch-translucency` on PyPI, MIT licensed
- Model: Stantchev & Malek, "Architectural translucency in service-oriented architectures," IEE Proc. Software 153(1), 2006. DOI: 10.1049/ip-sen:20050017
- Project README covers full CLI reference and the intensity / throughput / response-time equations
