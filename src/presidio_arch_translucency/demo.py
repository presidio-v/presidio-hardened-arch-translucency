"""
pat demo — Live architectural translucency demonstrator.

Runs a Monte Carlo π workload across three replication variants on the
local Docker daemon and measures throughput, latency, and CPU usage.
Demonstrates the core architectural-translucency insight: the same
replication measure has different performance implications at different layers.

Variants
--------
1 — Single container         (baseline, no replication)
2 — N independent containers (manual container-level replication, round-robin LB)
3 — N workers + nginx        (simulated Deployment-style replication with an LB)
"""

from __future__ import annotations

import concurrent.futures
import io
import itertools
import shutil
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import urlopen

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from rich.table import Table

from presidio_arch_translucency.security import log_security_event

# ── constants ─────────────────────────────────────────────────────────────────

IMAGE_NAME = "pat-demo-workload"
IMAGE_TAG = "0.1.0"
FULL_IMAGE = f"{IMAGE_NAME}:{IMAGE_TAG}"
NETWORK_NAME = "pat-demo-net"
V1_PORT = 18080
V2_BASE_PORT = 18081
V3_LB_PORT = 18090
CONTAINER_PREFIX = "pat-demo"

# ── embedded workload assets (written to tmpdir at build time) ─────────────────

_DOCKERFILE = """\
FROM python:3.12-slim
LABEL description="Monte Carlo pi workload for pat demo"
WORKDIR /app
COPY app.py .
EXPOSE 8080
CMD ["python", "-u", "app.py"]
"""

_APP_PY = """\
import json, os, random, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

def mc_pi(n):
    return 4.0 * sum(
        1 for _ in range(n)
        if random.random()**2 + random.random()**2 <= 1.0
    ) / n

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urlparse(self.path)
        if p.path == '/health':
            self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
        elif p.path == '/compute':
            n = int(parse_qs(p.query).get('n', ['200000'])[0])
            t0 = time.perf_counter()
            pi = mc_pi(n)
            ms = (time.perf_counter() - t0) * 1000
            b = json.dumps({'pi': round(pi,6),'n':n,'ms':round(ms,2)}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(b)))
            self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass

HTTPServer(('0.0.0.0', int(os.environ.get('PORT','8080'))), H).serve_forever()
"""

# ── data model ────────────────────────────────────────────────────────────────


@dataclass
class VariantResult:
    name: str
    description: str
    n_workers: int
    n_lb: int
    throughput_rps: float
    avg_latency_ms: float
    p95_latency_ms: float
    cpu_pct: float
    errors: int


# ── pure helpers (fully testable without Docker) ──────────────────────────────


def nginx_conf(n: int) -> str:
    """Generate nginx upstream config for n workers named pat-demo-v3-{i}."""
    upstreams = "\n".join(
        f"        server {CONTAINER_PREFIX}-v3-{i}:8080;" for i in range(n)
    )
    return (
        "events { worker_connections 1024; }\n"
        "http {\n"
        "    upstream workload {\n"
        f"{upstreams}\n"
        "    }\n"
        "    server {\n"
        "        listen 80;\n"
        "        location /health {\n"
        "            return 200 'ok';\n"
        "            add_header Content-Type text/plain;\n"
        "        }\n"
        "        location / {\n"
        "            proxy_pass http://workload;\n"
        "            proxy_connect_timeout 60s;\n"
        "            proxy_read_timeout    60s;\n"
        "        }\n"
        "    }\n"
        "}\n"
    )


def translucency_insight(results: list[VariantResult]) -> str:
    """
    Derive an architectural-translucency insight from the measured results.
    Returns a human-readable explanation of which layer performed best and why.
    """
    if not results:
        return "No results to analyse."

    best = max(results, key=lambda r: r.throughput_rps)
    worst = min(results, key=lambda r: r.throughput_rps)

    speedup = (
        best.throughput_rps / worst.throughput_rps if worst.throughput_rps > 0 else 1.0
    )
    lat_improvement = (
        (worst.avg_latency_ms - best.avg_latency_ms) / worst.avg_latency_ms * 100
        if worst.avg_latency_ms > 0
        else 0.0
    )

    lines = [
        f"Best layer : {best.name} "
        f"({best.throughput_rps:.1f} req/s, {best.avg_latency_ms:.0f} ms avg)",
        f"Baseline   : {worst.name} "
        f"({worst.throughput_rps:.1f} req/s, {worst.avg_latency_ms:.0f} ms avg)",
        f"Speedup    : {speedup:.2f}× throughput,  "
        f"{lat_improvement:.0f}% latency reduction",
        "",
        "Architectural translucency insight:",
    ]

    if best.name.startswith("2"):
        lines.append(
            "  Manual container replication minimises coordination overhead."
            " Each replica handles an independent slice of the request stream"
            " with no shared routing state — the load is partitioned at the"
            " lowest possible layer, matching the theoretical optimum of"
            " ω(δ) = min(δ × base_capacity × efficiency(δ), demand)."
        )
    elif best.name.startswith("3"):
        lines.append(
            "  The nginx load-balancer layer adds a thin routing tier but"
            " enables a single stable endpoint — closer to a Kubernetes"
            " Service/Deployment model. The scheduling overhead (β·ln δ)"
            " is offset by better connection reuse across replicas."
        )
    else:
        lines.append(
            "  A single container saturated under this workload. Adding"
            " replication at any layer would improve throughput. Try"
            " increasing --replicas or reducing --iterations."
        )

    return "\n".join(lines)


# ── matplotlib plot (uses Agg backend — safe in headless environments) ─────────


def save_plot(results: list[VariantResult], output: Path) -> None:
    """Save a 3-panel comparison bar chart to *output*."""
    import textwrap  # noqa: PLC0415

    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    names = [textwrap.fill(r.name, width=16) for r in results]
    colours = ["#4C72B0", "#DD8452", "#55A868"]
    best_tp_idx = max(range(len(results)), key=lambda i: results[i].throughput_rps)
    best_lat_idx = min(range(len(results)), key=lambda i: results[i].avg_latency_ms)

    fig, axes = plt.subplots(1, 3, figsize=(13, 5))
    fig.suptitle(
        "Architectural Translucency — Live Demo Results",
        fontsize=13,
        fontweight="bold",
    )

    # panel 0 — throughput
    bars = axes[0].bar(
        names, [r.throughput_rps for r in results], color=colours, edgecolor="white"
    )
    bars[best_tp_idx].set_edgecolor("gold")
    bars[best_tp_idx].set_linewidth(2.5)
    axes[0].set_title("Throughput (req/s)  ↑ higher is better")
    axes[0].set_ylabel("req / s")

    # panel 1 — avg latency
    bars2 = axes[1].bar(
        names, [r.avg_latency_ms for r in results], color=colours, edgecolor="white"
    )
    bars2[best_lat_idx].set_edgecolor("gold")
    bars2[best_lat_idx].set_linewidth(2.5)
    axes[1].set_title("Avg Latency (ms)  ↓ lower is better")
    axes[1].set_ylabel("ms")

    # panel 2 — CPU
    axes[2].bar(names, [r.cpu_pct for r in results], color=colours, edgecolor="white")
    axes[2].set_title("Total CPU (%)  across all containers")
    axes[2].set_ylabel("%")

    for ax in axes:
        ax.tick_params(axis="x", labelsize=8)
        ax.spines[["top", "right"]].set_visible(False)

    fig.tight_layout()
    fig.savefig(output, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ── Docker helpers ─────────────────────────────────────────────────────────────


def _cleanup(client: object) -> None:  # type: ignore[type-arg]
    """Stop and remove all pat-demo containers and the demo network."""
    import docker  # noqa: PLC0415

    assert hasattr(client, "containers")  # noqa: S101
    for c in client.containers.list(all=True):  # type: ignore[union-attr]
        if c.name.startswith(CONTAINER_PREFIX):
            try:
                c.stop(timeout=5)
                c.remove(force=True)
            except docker.errors.APIError:
                pass
    try:
        client.networks.get(NETWORK_NAME).remove()  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001, S110
        pass


def _build_image(client: object, console: Console, force: bool = False) -> None:
    """Build the workload Docker image (skip if already present)."""
    import docker  # noqa: PLC0415

    assert hasattr(client, "images")  # noqa: S101
    if not force:
        try:
            client.images.get(FULL_IMAGE)  # type: ignore[union-attr]
            console.print(
                f"[dim]Image {FULL_IMAGE} already present — skipping build.[/]"
            )
            return
        except docker.errors.ImageNotFound:
            pass

    console.print(f"[cyan]Building {FULL_IMAGE} …[/]")
    tmpdir = Path(tempfile.mkdtemp(prefix="pat-demo-build-"))
    try:
        (tmpdir / "app.py").write_text(_APP_PY)
        (tmpdir / "Dockerfile").write_text(_DOCKERFILE)
        client.images.build(  # type: ignore[union-attr]
            path=str(tmpdir), tag=FULL_IMAGE, rm=True, quiet=False
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    console.print(f"[green]Built {FULL_IMAGE}[/]")


def _ensure_network(client: object) -> None:
    """Create the demo bridge network if it does not exist."""
    import docker  # noqa: PLC0415

    try:
        client.networks.get(NETWORK_NAME)  # type: ignore[union-attr]
    except docker.errors.NotFound:
        client.networks.create(NETWORK_NAME, driver="bridge")  # type: ignore[union-attr]


def _wait_url(url: str, timeout: float = 30.0) -> bool:
    """Poll *url* until HTTP 200 or timeout. Returns True on success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=2) as r:  # noqa: S310
                if r.status == 200:
                    return True
        except (URLError, OSError):
            pass
        time.sleep(0.3)
    return False


def _cpu_sampler(
    client: object,
    container_ids: list[str],
    stop: threading.Event,
) -> float:
    """Background thread: sample total CPU% until stop is set."""
    samples: list[float] = []
    while not stop.is_set():
        total = 0.0
        for cid in container_ids:
            try:
                s = client.containers.get(cid).stats(stream=False)  # type: ignore[union-attr]
                cpu_d = (
                    s["cpu_stats"]["cpu_usage"]["total_usage"]
                    - s["precpu_stats"]["cpu_usage"]["total_usage"]
                )
                sys_d = s["cpu_stats"].get("system_cpu_usage", 0) - s[
                    "precpu_stats"
                ].get("system_cpu_usage", 0)
                n_cpu = s["cpu_stats"].get(
                    "online_cpus",
                    len(s["cpu_stats"]["cpu_usage"].get("percpu_usage", [1])),
                )
                if sys_d > 0:
                    total += (cpu_d / sys_d) * n_cpu * 100.0
            except Exception:  # noqa: BLE001, S110
                pass
        if total > 0:
            samples.append(total)
        time.sleep(0.4)
    return sum(samples) / len(samples) if samples else 0.0


def _load_test(
    urls: list[str],
    n_requests: int,
    concurrency: int,
    iterations: int,
    client: object,
    container_ids: list[str],
    progress: Progress,
    task: TaskID,
) -> tuple[dict[str, float], float]:
    """Run a concurrent load test; returns (metrics_dict, avg_cpu_pct)."""
    url_cycle = itertools.cycle(urls)
    latencies: list[float] = []
    errors = 0

    def _one(url: str) -> Optional[float]:  # noqa: UP045
        try:
            t0 = time.perf_counter()
            with urlopen(  # noqa: S310
                f"{url}/compute?n={iterations}", timeout=120
            ) as r:
                r.read()
            return (time.perf_counter() - t0) * 1000
        except Exception:  # noqa: BLE001
            return None

    stop = threading.Event()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as cpu_ex:
        cpu_fut = cpu_ex.submit(_cpu_sampler, client, container_ids, stop)

        t_start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = [ex.submit(_one, next(url_cycle)) for _ in range(n_requests)]
            for f in concurrent.futures.as_completed(futs):
                v = f.result()
                if v is not None:
                    latencies.append(v)
                else:
                    errors += 1
                progress.advance(task)
        t_total = time.perf_counter() - t_start

        stop.set()
        avg_cpu = cpu_fut.result()

    sl = sorted(latencies)
    n = len(sl)
    p95 = sl[max(0, int(0.95 * n) - 1)] if n else 0.0
    return (
        {
            "throughput_rps": n / t_total if t_total > 0 else 0.0,
            "avg_latency_ms": sum(latencies) / n if n else 0.0,
            "p95_latency_ms": p95,
            "errors": errors,
        },
        avg_cpu,
    )


def _upload_nginx_conf(container: object, conf_text: str) -> None:
    """Inject nginx.conf into a running container via tar archive."""
    data = conf_text.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(name="nginx.conf")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    buf.seek(0)
    container.put_archive("/etc/nginx/", buf)  # type: ignore[union-attr]
    container.exec_run(["nginx", "-s", "reload"])  # type: ignore[union-attr]


# ── per-variant runners ────────────────────────────────────────────────────────


def _run_variant(
    label: str,
    description: str,
    urls: list[str],
    container_ids: list[str],
    n_workers: int,
    n_lb: int,
    n_requests: int,
    concurrency: int,
    iterations: int,
    client: object,
    console: Console,
    progress: Progress,
) -> VariantResult:
    task = progress.add_task(f"[cyan]{label}", total=n_requests)
    metrics, cpu = _load_test(
        urls, n_requests, concurrency, iterations, client, container_ids, progress, task
    )
    return VariantResult(
        name=label,
        description=description,
        n_workers=n_workers,
        n_lb=n_lb,
        throughput_rps=round(metrics["throughput_rps"], 2),
        avg_latency_ms=round(metrics["avg_latency_ms"], 1),
        p95_latency_ms=round(metrics["p95_latency_ms"], 1),
        cpu_pct=round(cpu, 1),
        errors=int(metrics["errors"]),
    )


# ── result rendering ──────────────────────────────────────────────────────────


def _render_table(results: list[VariantResult], console: Console) -> None:
    best_idx = max(range(len(results)), key=lambda i: results[i].throughput_rps)
    table = Table(
        title="Architectural Translucency — Measured Results",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Variant", style="cyan", no_wrap=True)
    table.add_column("Workers", justify="right")
    table.add_column("Throughput\n(req/s)", justify="right")
    table.add_column("Avg Lat\n(ms)", justify="right")
    table.add_column("p95 Lat\n(ms)", justify="right")
    table.add_column("CPU %\n(total)", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Best?", justify="center")

    for i, r in enumerate(results):
        is_best = i == best_idx
        tp_col = "bold green" if is_best else "white"
        table.add_row(
            r.name,
            str(r.n_workers + r.n_lb),
            f"[{tp_col}]{r.throughput_rps:.1f}[/]",
            f"{r.avg_latency_ms:.0f}",
            f"{r.p95_latency_ms:.0f}",
            f"{r.cpu_pct:.0f}",
            str(r.errors),
            "[bold green]✓[/]" if is_best else "",
        )

    console.print()
    console.print(table)


# ── Cost analysis ────────────────────────────────────────────────────────────


def _render_cost_section(
    results: list[VariantResult],
    cost_per_container_hour: float,
    console: Console,
    cost_params: Optional[object] = None,  # noqa: UP045
    pricing_source: Optional[str] = None,  # noqa: UP045
) -> None:
    """
    Show cost/req for each measured variant and the analytical cost recommendation.
    Uses measured throughput — so actual Docker numbers feed the cost model.

    If *cost_params* (a CostParams instance) is provided it is used directly
    (v0.5.0 cloud pricing path).  Otherwise costs are derived from the uniform
    *cost_per_container_hour* scalar (manual / default path).
    """
    from presidio_arch_translucency.cost import (  # noqa: PLC0415
        CostParams,
        build_cost_results,
    )
    from presidio_arch_translucency.model import (  # noqa: PLC0415
        ReplicationLayer,
        analyze,
    )

    best = max(results, key=lambda r: r.throughput_rps)
    if best.throughput_rps <= 0 or best.avg_latency_ms <= 0:
        return

    # Resolve the per-layer CostParams to use for the analytical section
    if cost_params is not None and isinstance(cost_params, CostParams):
        cp = cost_params
        container_cost = cp.cost_per_container_hour
    else:
        container_cost = cost_per_container_hour
        cp = CostParams(
            cost_per_container_hour=container_cost,
            cost_per_pod_hour=container_cost * 2.5,
            cost_per_deployment_hour=container_cost * 5.0,
            cost_per_node_hour=container_cost * 25.0,
        )

    # ── Measured cost per variant ──────────────────────────────────────────────
    table = Table(
        title="Cost Efficiency — Measured Variants",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Variant", style="cyan", no_wrap=True)
    table.add_column("Containers", justify="right")
    table.add_column("Throughput\n(req/s)", justify="right")
    table.add_column("Cost/hr\n(USD)", justify="right")
    table.add_column("Cost/req\n(USD)", justify="right")
    table.add_column("Best ROI?", justify="center")

    cost_rows = []
    for r in results:
        total_containers = r.n_workers + r.n_lb
        hc = round(total_containers * container_cost, 4)
        cpr = hc / (r.throughput_rps * 3600.0) if r.throughput_rps > 0 else float("inf")
        cost_rows.append((r, hc, cpr))

    best_roi_idx = min(range(len(cost_rows)), key=lambda i: cost_rows[i][2])

    for i, (r, hc, cpr) in enumerate(cost_rows):
        is_best = i == best_roi_idx
        cpr_str = f"${cpr:.6f}" if cpr != float("inf") else "—"
        table.add_row(
            r.name,
            str(r.n_workers + r.n_lb),
            f"{r.throughput_rps:.1f}",
            f"${hc:.4f}",
            cpr_str,
            "[bold green]✓[/]" if is_best else "",
        )

    console.print()
    console.print(table)

    # ── Analytical layer recommendation ────────────────────────────────────────
    analysis = analyze(
        best.throughput_rps, best.avg_latency_ms, ReplicationLayer.CONTAINER
    )
    cost_results = build_cost_results(analysis.layers, cp)
    best_analytical = next(r for r in cost_results if r.is_recommended)

    hc_measured = cost_rows[best_roi_idx][1]
    cpr_measured = cost_rows[best_roi_idx][2]

    source_line = (
        f"[dim]Pricing source:[/]  {pricing_source}\n\n"
        if pricing_source
        else f"[dim]Cost assumption:[/]  ${container_cost:.4f} / container / hour\n\n"
    )
    body = (
        source_line + "[bold]Best measured variant:[/]  "
        f"[cyan]{cost_rows[best_roi_idx][0].name}[/]\n"
        f"  Cost/req  ${cpr_measured:.6f}  ·  Cost/hr  ${hc_measured:.4f}\n\n"
        f"[bold]Analytical best-ROI layer:[/]  [cyan]{best_analytical.layer.value}[/]\n"
        f"  Replicas  {best_analytical.replicas}  ·  "
        f"Throughput gain  {best_analytical.throughput_gain_pct:+.1f}%\n"
        f"  Cost/req  ${best_analytical.cost_per_request_usd:.6f}  ·  "
        f"Cost/hr  ${best_analytical.hourly_cost_usd:.4f}\n"
        f"  ROI score  {best_analytical.roi_score:.1f}\n\n"
        "[dim]Run [bold]pat cost --cloud aws[/] with your region and instance type"
        " for a live-priced full breakdown.[/]"
    )
    console.print(
        Panel(
            body,
            title="[bold blue]v0.5.0 Cost Analysis[/]",
            border_style="blue",
        )
    )


# ── HPA lag projection ────────────────────────────────────────────────────────


def _render_hpa_section(
    results: list[VariantResult],
    output: Path,
    spike_multiplier: float,
    console: Console,
) -> None:
    """
    Feed the best measured variant into the HPA lag model and render
    a trough-projection panel + time-series plot.
    """
    from presidio_arch_translucency.hpa import (  # noqa: PLC0415
        save_hpa_plot,
        simulate_scale_event,
    )
    from presidio_arch_translucency.model import ReplicationLayer  # noqa: PLC0415

    best = max(results, key=lambda r: r.throughput_rps)

    if best.throughput_rps <= 0 or best.avg_latency_ms <= 0:
        return  # measurement too noisy to project

    spike_rps = best.throughput_rps * spike_multiplier
    hpa_result = simulate_scale_event(
        rps_baseline=best.throughput_rps,
        rps_spike=spike_rps,
        avg_latency_ms=best.avg_latency_ms,
        layer=ReplicationLayer.CONTAINER,
        replicas_before=max(1, best.n_workers),
    )

    trough_color = "red" if hpa_result.trough_throughput_pct < 50 else "yellow"
    body = (
        f"[dim]Measured baseline:[/]  {best.name}\n"
        f"  {best.throughput_rps:.1f} req/s  ·  "
        f"{best.avg_latency_ms:.0f} ms avg latency\n\n"
        f"[dim]Hypothetical spike:[/]  {spike_multiplier:.0f}× "
        f"→ {spike_rps:.1f} req/s\n\n"
        f"[bold red]TROUGH[/]  (0 s – {hpa_result.trough_duration_s:.0f} s"
        f"  =  HPA poll 15 s  +  pod startup 30 s)\n"
        f"  Throughput    [{trough_color}]"
        f"{hpa_result.trough_throughput_rps:.1f} req/s"
        f"  ({hpa_result.trough_throughput_pct:.0f} % of spike demand)[/]\n"
        f"  p99 latency   {hpa_result.trough_p99_latency_ms:,.0f} ms\n"
        f"  Missed reqs   ~{hpa_result.missed_requests:,}\n\n"
        f"[bold green]STEADY STATE[/]  (after {hpa_result.trough_duration_s:.0f} s"
        f"  —  {hpa_result.replicas_after} replicas)\n"
        f"  Throughput    {hpa_result.steady_throughput_rps:.1f} req/s\n"
        f"  p99 latency   {hpa_result.steady_p99_latency_ms:,.0f} ms\n\n"
        f"[dim]→ Set [bold]HPA minReplicas = {hpa_result.replicas_after}[/]"
        f"[dim] to pre-provision and eliminate the trough.[/]"
    )

    console.print(
        Panel(
            body,
            title=(
                f"[bold magenta]HPA Lag Projection"
                f" (if load spikes {spike_multiplier:.0f}×)[/]"
            ),
            border_style="magenta",
        )
    )

    hpa_output = output.parent / (output.stem + "-hpa" + output.suffix)
    save_hpa_plot(hpa_result, hpa_output)
    console.print(f"[green]HPA plot saved →[/] {hpa_output}\n")


# ── Typer command ─────────────────────────────────────────────────────────────


def demo_command(
    replicas: int = typer.Option(
        4,
        "--replicas",
        "-n",
        help="Number of worker containers for variants 2 & 3.",
        min=2,
    ),
    requests: int = typer.Option(
        40, "--requests", help="Total HTTP requests per variant.", min=10
    ),
    concurrency: int = typer.Option(
        8, "--concurrency", help="Concurrent request threads.", min=1
    ),
    iterations: int = typer.Option(
        200_000, "--iterations", help="Monte Carlo iterations per request."
    ),
    output: Path = typer.Option(  # noqa: B008
        Path("demo-results.png"), "--output", "-o", help="Path for the results plot."
    ),
    force_rebuild: bool = typer.Option(
        False, "--force-rebuild", help="Rebuild the workload Docker image."
    ),
    spike_multiplier: float = typer.Option(
        3.0,
        "--spike-multiplier",
        help="Hypothetical load spike factor for the HPA lag projection.",
    ),
    cost_per_container_hour: float = typer.Option(
        0.02,
        "--cost-per-container-hour",
        help="USD per container per hour (fallback when --cloud is not set).",
    ),
    # ── v0.5.0: live AWS pricing ───────────────────────────────────────────────
    cloud: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--cloud",
        help="Cloud provider for live on-demand pricing (aws).",
    ),
    region: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--region",
        help="Cloud region (e.g. us-east-1). Required with --cloud.",
    ),
    instance_type: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--instance-type",
        help="EC2 instance type (e.g. m5.large). Use with --cloud aws.",
    ),
    fargate: bool = typer.Option(
        False,
        "--fargate",
        help="Use AWS Fargate task pricing. Needs --vcpu/--memory-gb.",
    ),
    vcpu: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--vcpu",
        help="vCPU per Fargate task. Required with --fargate.",
        min=0.25,
    ),
    memory_gb: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--memory-gb",
        help="Memory in GB per Fargate task. Required with --fargate.",
        min=0.5,
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass local pricing cache and fetch fresh prices from AWS.",
    ),
) -> None:
    """
    Live architectural translucency demonstrator.

    Builds a Monte Carlo π workload container and runs it in three
    replication variants to show how throughput and latency change
    depending on where replication is applied.

    Pass --cloud aws with --instance-type or --fargate to use live on-demand
    AWS pricing in the cost analysis section instead of the default estimate.

    Requires a running Docker daemon.
    """
    import docker  # noqa: PLC0415
    import docker.errors  # noqa: PLC0415

    console = Console()
    log_security_event("DEMO_INVOCATION", {"replicas": replicas})

    # ── v0.5.0: resolve cloud pricing before touching Docker ───────────────────
    resolved_cost_params = None
    resolved_pricing_source: Optional[str] = None  # noqa: UP045

    if cloud is not None:
        if cloud.lower() != "aws":
            console.print(
                f"[bold red]Unsupported cloud provider: {cloud!r}. "
                "Only 'aws' is supported in v0.5.0.[/]"
            )
            raise typer.Exit(1)
        if region is None:
            console.print("[bold red]--region is required when using --cloud aws[/]")
            raise typer.Exit(1)
        if not fargate and instance_type is None:
            console.print(
                "[bold red]Specify --instance-type or --fargate with --cloud aws[/]"
            )
            raise typer.Exit(1)

        from presidio_arch_translucency.cloud import (  # noqa: PLC0415
            PricingError,
            build_cost_params_from_aws,
        )

        try:
            with console.status(
                "[dim]Fetching AWS on-demand pricing "
                "(first run may take 30–60 s, cached for 24 h)…[/dim]"
            ):
                pricing = build_cost_params_from_aws(
                    region=region,
                    instance_type=instance_type,
                    fargate=fargate,
                    vcpu=vcpu,
                    memory_gb=memory_gb,
                    no_cache=no_cache,
                )
            resolved_cost_params = pricing.params
            cache_tag = " (cached)" if pricing.from_cache else ""
            resolved_pricing_source = pricing.source_description + cache_tag
            console.print(f"[green]Pricing fetched:[/] {resolved_pricing_source}")
        except PricingError as exc:
            console.print(f"[bold red]Cloud pricing error:[/] {exc}")
            raise typer.Exit(1) from exc

    # ── connect to Docker ──────────────────────────────────────────────────────
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        console.print(f"[bold red]Docker daemon not available:[/] {exc}")
        raise typer.Exit(1) from exc

    console.print(
        Panel(
            f"[bold]Replicas:[/] {replicas}  "
            f"[bold]Requests:[/] {requests}  "
            f"[bold]Concurrency:[/] {concurrency}  "
            f"[bold]Iterations/req:[/] {iterations:,}",
            title="[bold blue]Presidio Architectural Translucency — Live Demo[/]",
            border_style="blue",
        )
    )

    # ── clean up any leftover containers from a previous run ──────────────────
    _cleanup(client)

    # ── build image ───────────────────────────────────────────────────────────
    _build_image(client, console, force=force_rebuild)
    _ensure_network(client)

    results: list[VariantResult] = []

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        # ── Variant 1: single container ────────────────────────────────────
        console.print("\n[bold]Variant 1[/] — Single container")
        c1 = client.containers.run(
            FULL_IMAGE,
            name=f"{CONTAINER_PREFIX}-v1-0",
            ports={"8080/tcp": V1_PORT},
            detach=True,
            network=NETWORK_NAME,
        )
        try:
            if not _wait_url(f"http://localhost:{V1_PORT}/health"):
                console.print("[red]Variant 1 container failed to start.[/]")
                raise typer.Exit(1)
            r1 = _run_variant(
                "1 — Single container",
                "Baseline: one container handles all traffic",
                [f"http://localhost:{V1_PORT}"],
                [c1.id],
                n_workers=1,
                n_lb=0,
                n_requests=requests,
                concurrency=concurrency,
                iterations=iterations,
                client=client,
                console=console,
                progress=progress,
            )
            results.append(r1)
        finally:
            c1.stop(timeout=5)
            c1.remove(force=True)

        # ── Variant 2: N independent containers, round-robin ───────────────
        console.print(f"\n[bold]Variant 2[/] — {replicas} independent containers")
        v2_containers = []
        v2_urls = []
        for i in range(replicas):
            port = V2_BASE_PORT + i
            c = client.containers.run(
                FULL_IMAGE,
                name=f"{CONTAINER_PREFIX}-v2-{i}",
                ports={"8080/tcp": port},
                detach=True,
                network=NETWORK_NAME,
            )
            v2_containers.append(c)
            v2_urls.append(f"http://localhost:{port}")
        try:
            for url in v2_urls:
                if not _wait_url(f"{url}/health"):
                    console.print(f"[red]Container at {url} failed health check.[/]")
                    raise typer.Exit(1)
            r2 = _run_variant(
                f"2 — {replicas} containers (round-robin)",
                f"{replicas} independent containers, client-side round-robin LB",
                v2_urls,
                [c.id for c in v2_containers],
                n_workers=replicas,
                n_lb=0,
                n_requests=requests,
                concurrency=concurrency,
                iterations=iterations,
                client=client,
                console=console,
                progress=progress,
            )
            results.append(r2)
        finally:
            for c in v2_containers:
                c.stop(timeout=5)
                c.remove(force=True)

        # ── Variant 3: N workers + nginx (simulated K8s Deployment) ────────
        console.print(
            f"\n[bold]Variant 3[/] — {replicas} workers + nginx load balancer"
        )
        v3_workers: list[object] = []
        nginx_c = None
        try:
            for i in range(replicas):
                c = client.containers.run(
                    FULL_IMAGE,
                    name=f"{CONTAINER_PREFIX}-v3-{i}",
                    detach=True,
                    network=NETWORK_NAME,
                )
                v3_workers.append(c)

            # Start nginx, then inject config and reload
            nginx_c = client.containers.run(
                "nginx:1.27-alpine",
                name=f"{CONTAINER_PREFIX}-v3-nginx",
                ports={"80/tcp": V3_LB_PORT},
                detach=True,
                network=NETWORK_NAME,
            )
            time.sleep(1)  # let nginx initialise default config
            _upload_nginx_conf(nginx_c, nginx_conf(replicas))

            if not _wait_url(f"http://localhost:{V3_LB_PORT}/health"):
                console.print("[red]nginx failed to start for variant 3.[/]")
                raise typer.Exit(1)

            all_ids = [c.id for c in v3_workers] + [nginx_c.id]  # type: ignore[union-attr]
            r3 = _run_variant(
                f"3 — nginx LB ({replicas} workers)",
                f"{replicas} workers behind nginx reverse proxy (K8s-style)",
                [f"http://localhost:{V3_LB_PORT}"],
                all_ids,
                n_workers=replicas,
                n_lb=1,
                n_requests=requests,
                concurrency=concurrency,
                iterations=iterations,
                client=client,
                console=console,
                progress=progress,
            )
            results.append(r3)
        finally:
            for c in v3_workers:
                try:
                    c.stop(timeout=5)  # type: ignore[union-attr]
                    c.remove(force=True)  # type: ignore[union-attr]
                except Exception:  # noqa: BLE001, S110
                    pass
            if nginx_c:
                try:
                    nginx_c.stop(timeout=5)
                    nginx_c.remove(force=True)
                except Exception:  # noqa: BLE001, S110
                    pass

    # ── cleanup network ────────────────────────────────────────────────────────
    try:
        client.networks.get(NETWORK_NAME).remove()
    except Exception:  # noqa: BLE001, S110
        pass

    # ── output ────────────────────────────────────────────────────────────────
    _render_table(results, console)

    insight = translucency_insight(results)
    console.print()
    console.print(
        Panel(
            insight,
            title="[bold yellow]Architectural Translucency Insight[/]",
            border_style="yellow",
        )
    )

    save_plot(results, output)
    console.print(f"\n[green]Plot saved →[/] {output}\n")

    # ── HPA lag projection using measured results ──────────────────────────────
    _render_hpa_section(results, output, spike_multiplier, console)

    # ── Cost analysis using measured results (v0.5.0) ─────────────────────────
    _render_cost_section(
        results,
        cost_per_container_hour,
        console,
        cost_params=resolved_cost_params,
        pricing_source=resolved_pricing_source,
    )

    log_security_event("DEMO_COMPLETE", {"variants": len(results)})
