"""
CLI entry-point for presidio-hardened-arch-translucency.

Usage:
    pat analyze --requests-per-second 500 --avg-latency-ms 80 --current-layer container
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from presidio_arch_translucency import __version__
from presidio_arch_translucency.cost import (
    CostParams,
    build_cost_results,
    cost_per_request,
    format_cost_per_request,
    hourly_cost,
    trough_cost_usd,
)
from presidio_arch_translucency.demo import demo_command
from presidio_arch_translucency.hpa import (
    ScaleEventParams,
    ScaleEventResult,
    save_hpa_plot,
    simulate_scale_event,
)
from presidio_arch_translucency.model import (
    REFERENCE_LATENCY_RANGE_MS,
    REFERENCE_RPS_RANGE,
    VALID_LAYERS,
    ReplicationLayer,
    analyze,
    model_is_calibrated,
)
from presidio_arch_translucency.security import (
    InputValidationError,
    configure_logging,
    log_recommendation,
    log_security_event,
    run_dependency_audit,
    sanitize_latency_ms,
    sanitize_layer,
    sanitize_requests_per_second,
)

app = typer.Typer(
    name="pat",
    help=(
        "Presidio Architectural Translucency analyzer.\n\n"
        "Recommends the optimal Docker/Kubernetes replication layer "
        "(container, pod, deployment, node) for your workload."
    ),
    add_completion=False,
)
app.command("demo")(demo_command)

console = Console()
err_console = Console(stderr=True, style="bold red")
warn_console = Console(stderr=True)


def _envelope_warning_text() -> str:
    """Message shown when running against uncalibrated default parameters."""
    rps_lo, rps_hi = REFERENCE_RPS_RANGE
    lat_lo, lat_hi = REFERENCE_LATENCY_RANGE_MS
    return (
        "[yellow]⚠ No calibrated model found (.pat-model.json or ~/.pat/model.json). "
        "Using default parameters calibrated for async Python services in the "
        f"~{rps_lo:.0f}–{rps_hi:.0f} req/s and ~{lat_lo:.0f}–{lat_hi:.0f} ms latency "
        "envelope. Results may be inaccurate outside this range or for non-async "
        "(single-threaded / CPU-bound) workloads.\n"
        "  Run [bold]pat calibrate[/] with your measured workload to tune the "
        "model.[/]"
    )


def _warn_if_uncalibrated() -> None:
    """Emit the envelope warning to stderr unless a calibrated model exists."""
    if not model_is_calibrated():
        warn_console.print(_envelope_warning_text())


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pat version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(  # noqa: UP045
        None,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Enable debug logging."
    ),
    skip_audit: bool = typer.Option(
        False,
        "--skip-audit",
        help="Skip the on-run CVE dependency audit.",
    ),
) -> None:
    """Presidio Architectural Translucency CLI."""
    configure_logging(verbose=verbose)
    log_security_event("CLI_INVOCATION", {"version": __version__})
    if not skip_audit:
        run_dependency_audit(skip_on_error=True)


@app.command("analyze")
def analyze_cmd(
    requests_per_second: float = typer.Option(
        ...,
        "--requests-per-second",
        "-r",
        help="Observed workload in requests per second.",
        min=0.01,
    ),
    avg_latency_ms: float = typer.Option(
        ...,
        "--avg-latency-ms",
        "-l",
        help="Current average response latency in milliseconds.",
        min=0.1,
    ),
    current_layer: str = typer.Option(
        ...,
        "--current-layer",
        "-c",
        help=f"Current replication layer. One of: {', '.join(VALID_LAYERS)}",
    ),
    show_all: bool = typer.Option(
        False,
        "--show-all",
        help="Show analysis results for all layers, not just the recommendation.",
    ),
    cost_per_replica_hour: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--cost-per-replica-hour",
        help="Uniform cost (USD/replica/hour). Adds cost columns to --show-all table.",
        min=0.0,
    ),
) -> None:
    """
    Analyze workload and recommend the optimal replication layer.

    Applies the architectural translucency model to determine where
    replication (container / pod / deployment / node) yields the highest
    throughput gain with the lowest overhead for the given workload.
    """
    # --- Input sanitization (Presidio security extension) ---
    try:
        rps = sanitize_requests_per_second(requests_per_second)
        lat = sanitize_latency_ms(avg_latency_ms)
        layer_str = sanitize_layer(current_layer, VALID_LAYERS)
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    current = ReplicationLayer(layer_str)
    _warn_if_uncalibrated()

    # --- Run analysis ---
    result = analyze(
        requests_per_second=rps,
        avg_latency_ms=lat,
        current_layer=current,
    )

    # --- Presidio security event logging ---
    best = next(r for r in result.layers if r.layer == result.recommended_layer)
    log_recommendation(
        layer=result.recommended_layer.value,
        replicas=result.recommended_replicas,
        throughput_gain_pct=best.throughput_gain_pct,
    )

    # --- Render output ---
    _render_results(result, show_all=show_all, uniform_cost=cost_per_replica_hour)


def _render_results(
    result: AnalysisResult,  # type: ignore[name-defined]  # noqa: F821
    show_all: bool,
    uniform_cost: Optional[float] = None,  # noqa: UP045
) -> None:
    from presidio_arch_translucency.model import AnalysisResult  # local import for type

    assert isinstance(result, AnalysisResult)  # noqa: S101

    best = next(r for r in result.layers if r.layer == result.recommended_layer)

    # Summary panel
    gain_color = "green" if best.throughput_gain_pct >= 0 else "red"
    rt_color = "red" if best.response_time_change_pct > 10 else "green"

    summary = (
        f"[bold]Recommended layer:[/]  [cyan]{result.recommended_layer.value}[/]\n"
        f"[bold]Optimal replicas:[/]   [cyan]{result.recommended_replicas}[/]\n"
        f"[bold]Throughput gain:[/]    [{gain_color}]"
        f"{best.throughput_gain_pct:+.1f}%[/]\n"
        f"[bold]Response-time Δ:[/]    [{rt_color}]"
        f"{best.response_time_change_pct:+.1f}%[/]\n"
        f"[bold]Est. throughput:[/]    {best.estimated_throughput_rps:.0f} req/s\n"
        f"[bold]Est. response time:[/] {best.estimated_response_time_ms:.1f} ms\n\n"
        f"[dim]{best.description}[/]"
    )

    console.print()
    console.print(
        Panel(
            summary,
            title="[bold blue]Presidio Architectural Translucency — Recommendation[/]",
            border_style="blue",
        )
    )

    if show_all:
        # Build uniform-cost params if requested
        cost_params = None
        if uniform_cost is not None:
            cost_params = CostParams(
                cost_per_container_hour=uniform_cost,
                cost_per_pod_hour=uniform_cost,
                cost_per_deployment_hour=uniform_cost,
                cost_per_node_hour=uniform_cost,
            )

        table = Table(
            title="All Layers Analysis",
            box=box.ROUNDED,
            show_lines=True,
        )
        table.add_column("Layer", style="cyan", no_wrap=True)
        table.add_column("Replicas", justify="right")
        table.add_column("Throughput (req/s)", justify="right")
        table.add_column("Δ Throughput", justify="right")
        table.add_column("Response Time (ms)", justify="right")
        table.add_column("Δ RT", justify="right")
        if cost_params is not None:
            table.add_column("Cost/hr (USD)", justify="right")
            table.add_column("Cost/req (USD)", justify="right")
        table.add_column("Recommended", justify="center")

        for lr in result.layers:
            is_rec = lr.layer == result.recommended_layer
            is_cur = lr.layer == result.current_layer
            label = ""
            if is_rec:
                label += "✓ rec"
            if is_cur:
                label += " (current)" if label else "(current)"

            tp_color = "green" if lr.throughput_gain_pct >= 0 else "red"
            rt_style = "red" if lr.response_time_change_pct > 10 else "green"

            row: list[str] = [
                lr.layer.value,
                str(lr.optimal_replicas),
                f"{lr.estimated_throughput_rps:.0f}",
                f"[{tp_color}]{lr.throughput_gain_pct:+.1f}%[/]",
                f"{lr.estimated_response_time_ms:.1f}",
                f"[{rt_style}]{lr.response_time_change_pct:+.1f}%[/]",
            ]
            if cost_params is not None:
                hc = hourly_cost(lr.layer, lr.optimal_replicas, cost_params)
                cpr = cost_per_request(
                    lr.layer,
                    lr.optimal_replicas,
                    lr.estimated_throughput_rps,
                    cost_params,
                )
                row.append(f"${hc:.4f}")
                row.append(format_cost_per_request(cpr))
            row.append("[bold green]✓[/]" if is_rec else "")
            table.add_row(*row)

        console.print()
        console.print(table)

    console.print(
        f"\n[dim]Baseline: {result.baseline_throughput_rps:.0f} req/s "
        f"@ {result.baseline_response_time_ms:.1f} ms  "
        f"(current layer: {result.current_layer.value})[/]\n"
    )


@app.command("what-if")
def what_if_cmd(
    current_rps: float = typer.Option(
        ...,
        "--current-rps",
        "-r",
        help="Current baseline workload in requests per second.",
        min=0.01,
    ),
    spike_rps: float = typer.Option(
        ...,
        "--spike-rps",
        "-s",
        help="Expected spike demand in requests per second.",
        min=0.01,
    ),
    avg_latency_ms: float = typer.Option(
        ...,
        "--avg-latency-ms",
        "-l",
        help="Current average response latency in milliseconds.",
        min=0.1,
    ),
    current_layer: str = typer.Option(
        ...,
        "--current-layer",
        "-c",
        help=f"Replication layer to model. One of: {', '.join(VALID_LAYERS)}",
    ),
    hpa_poll_s: float = typer.Option(
        15.0, "--hpa-poll-s", help="HPA scrape interval in seconds."
    ),
    pod_startup_s: float = typer.Option(
        30.0, "--pod-startup-s", help="Pod startup + readiness probe time in seconds."
    ),
    cold_start_s: float = typer.Option(
        0.0, "--cold-start-s", help="Additional cold-start warmup in seconds."
    ),
    replicas_before: Optional[int] = typer.Option(  # noqa: UP045
        None,
        "--replicas-before",
        help="Override replica count before spike (default: model-derived).",
    ),
    replicas_after: Optional[int] = typer.Option(  # noqa: UP045
        None,
        "--replicas-after",
        help="Override replica count after scale-out (default: model-derived).",
    ),
    output: Optional[Path] = typer.Option(  # noqa: UP045, B008
        None,
        "--output",
        "-o",
        help="Save time-series plot to this path (e.g. hpa-event.png).",
    ),
    cost_per_req: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--cost-per-request",
        help="USD value per request. Shows estimated trough revenue cost.",
        min=0.0,
    ),
) -> None:
    """
    Model an HPA scale event and show the performance trough.

    Projects throughput, latency, and missed requests during the window
    between a load spike and new pods reaching Ready state.
    """
    try:
        rps = sanitize_requests_per_second(current_rps)
        srps = sanitize_requests_per_second(spike_rps)
        lat = sanitize_latency_ms(avg_latency_ms)
        layer_str = sanitize_layer(current_layer, VALID_LAYERS)
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    if srps <= rps:
        err_console.print("[bold red]--spike-rps must be greater than --current-rps[/]")
        raise typer.Exit(code=2)

    layer = ReplicationLayer(layer_str)
    _warn_if_uncalibrated()
    params = ScaleEventParams(
        hpa_poll_s=hpa_poll_s,
        pod_startup_s=pod_startup_s,
        cold_start_s=cold_start_s,
    )
    result = simulate_scale_event(
        rps_baseline=rps,
        rps_spike=srps,
        avg_latency_ms=lat,
        layer=layer,
        params=params,
        replicas_before=replicas_before,
        replicas_after=replicas_after,
    )
    log_security_event("WHAT_IF_INVOCATION", {"layer": layer_str, "spike_rps": srps})
    _render_what_if(result, cost_per_req=cost_per_req)

    if output is not None:
        save_hpa_plot(result, output)
        console.print(f"[green]Plot saved →[/] {output}\n")


@app.command("slo")
def slo_cmd(
    requests_per_second: float = typer.Option(
        ...,
        "--requests-per-second",
        "-r",
        help="Baseline workload in requests per second.",
        min=0.01,
    ),
    avg_latency_ms: float = typer.Option(
        ...,
        "--avg-latency-ms",
        "-l",
        help="Current average response latency in milliseconds.",
        min=0.1,
    ),
    p99_target_ms: float = typer.Option(
        ...,
        "--p99-target-ms",
        help="SLO target: maximum acceptable p99 latency in milliseconds.",
        min=1.0,
    ),
    spike_multiplier: float = typer.Option(
        3.0,
        "--spike-multiplier",
        help="Load spike factor applied to requests-per-second (default 3×).",
        min=1.01,
    ),
    hpa_poll_s: float = typer.Option(
        15.0, "--hpa-poll-s", help="HPA scrape interval in seconds."
    ),
    pod_startup_s: float = typer.Option(
        30.0, "--pod-startup-s", help="Pod startup + readiness probe time in seconds."
    ),
    cold_start_s: float = typer.Option(
        0.0, "--cold-start-s", help="Additional cold-start warmup in seconds."
    ),
) -> None:
    """
    Check which replication layers meet a p99 latency SLO.

    Evaluates all four layers in steady-state AND during an HPA scale-event
    trough, showing whether your SLO target survives a load spike.
    """
    try:
        rps = sanitize_requests_per_second(requests_per_second)
        lat = sanitize_latency_ms(avg_latency_ms)
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    _warn_if_uncalibrated()
    spike_rps = rps * spike_multiplier
    params = ScaleEventParams(
        hpa_poll_s=hpa_poll_s,
        pod_startup_s=pod_startup_s,
        cold_start_s=cold_start_s,
    )

    results = {
        layer: simulate_scale_event(
            rps_baseline=rps,
            rps_spike=spike_rps,
            avg_latency_ms=lat,
            layer=layer,
            params=params,
        )
        for layer in ReplicationLayer
    }
    log_security_event("SLO_INVOCATION", {"rps": rps, "p99_target_ms": p99_target_ms})
    _render_slo(results, p99_target_ms, rps, spike_rps, params, CostParams())


@app.command("cost")
def cost_cmd(
    requests_per_second: float = typer.Option(
        ...,
        "--requests-per-second",
        "-r",
        help="Observed workload in requests per second.",
        min=0.01,
    ),
    avg_latency_ms: float = typer.Option(
        ...,
        "--avg-latency-ms",
        "-l",
        help="Current average response latency in milliseconds.",
        min=0.1,
    ),
    current_layer: str = typer.Option(
        ...,
        "--current-layer",
        "-c",
        help=f"Current replication layer. One of: {', '.join(VALID_LAYERS)}",
    ),
    cost_per_container_hour: float = typer.Option(
        0.02, "--cost-per-container-hour", help="USD per container replica per hour."
    ),
    cost_per_pod_hour: float = typer.Option(
        0.05, "--cost-per-pod-hour", help="USD per pod replica per hour."
    ),
    cost_per_deployment_hour: float = typer.Option(
        0.10, "--cost-per-deployment-hour", help="USD per deployment replica per hour."
    ),
    cost_per_node_hour: float = typer.Option(
        0.50, "--cost-per-node-hour", help="USD per cluster node per hour."
    ),
    # --- v0.5.0+: live cloud pricing ---
    cloud: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--cloud",
        help="Cloud provider for live pricing. Supported: aws, gcp, azure.",
    ),
    region: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--region",
        help="Cloud region (e.g. us-east-1 / us-central1 / eastus). Required with --cloud.",  # noqa: E501
    ),
    instance_type: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--instance-type",
        help="EC2 instance type (e.g. m5.large). Use with --cloud aws.",
    ),
    fargate: bool = typer.Option(
        False,
        "--fargate",
        help="Use AWS Fargate task pricing instead of EC2. Needs --vcpu/--memory-gb.",
    ),
    vcpu: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--vcpu",
        help="vCPU allocation per Fargate task (e.g. 0.5). Required with --fargate.",
        min=0.25,
    ),
    memory_gb: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--memory-gb",
        help="Memory in GB per Fargate task (e.g. 1.0). Required with --fargate.",
        min=0.5,
    ),
    machine_type: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--machine-type",
        help="GCP machine type (e.g. n2-standard-4). Required with --cloud gcp.",
    ),
    sku_name: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--sku-name",
        help="Azure VM SKU name (e.g. 'D2s v3'). Required with --cloud azure.",
    ),
    # --- v0.6.0: pricing tiers ---
    show_reserved: bool = typer.Option(
        False,
        "--show-reserved",
        help="Show 1yr/3yr reserved pricing tiers (AWS EC2 only).",
    ),
    show_spot: bool = typer.Option(
        False,
        "--spot",
        help="Show spot/preemptible pricing tier (AWS, GCP, Azure).",
    ),
    no_cache: bool = typer.Option(
        False,
        "--no-cache",
        help="Bypass the local pricing cache and fetch fresh prices from the cloud provider.",  # noqa: E501
    ),
) -> None:
    """
    Cross-layer cost analysis: throughput gain vs hourly cost.

    Shows cost/hour, cost/request, and ROI score for every replication layer,
    helping you pick the layer with the best performance-per-dollar.

    Use --cloud with --region to fetch live pricing (no credentials required
    for on-demand rates):

    \b
      AWS EC2:     --cloud aws --region us-east-1 --instance-type m5.large
      AWS Fargate: --cloud aws --region us-east-1 --fargate --vcpu 0.5 --memory-gb 1
      GCP:         --cloud gcp --region us-central1 --machine-type n2-standard-4
      Azure:       --cloud azure --region eastus --sku-name 'D2s v3'

    Add --show-reserved (AWS EC2 only) and/or --spot to see additional pricing tiers.
    Prices are cached locally (~/.pat/pricing-cache.json) for 24 h (5 min for spot).
    """
    try:
        rps = sanitize_requests_per_second(requests_per_second)
        lat = sanitize_latency_ms(avg_latency_ms)
        layer_str = sanitize_layer(current_layer, VALID_LAYERS)
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    current = ReplicationLayer(layer_str)
    _warn_if_uncalibrated()
    result = analyze(requests_per_second=rps, avg_latency_ms=lat, current_layer=current)

    if cloud is not None:
        cloud_lower = cloud.lower()

        # ── Input validation per provider ──────────────────────────────────
        if cloud_lower == "aws":
            if region is None:
                err_console.print(
                    "[bold red]--region is required when using --cloud aws[/]\n"
                    "[dim]Example: --cloud aws --region us-east-1 --instance-type m5.large[/]"  # noqa: E501
                )
                raise typer.Exit(code=2)
            if not fargate and instance_type is None:
                err_console.print(
                    "[bold red]Specify --instance-type or --fargate with --cloud aws[/]\n"  # noqa: E501
                    "[dim]Examples:\n"
                    "  --cloud aws --region us-east-1 --instance-type m5.large\n"
                    "  --cloud aws --region us-east-1 --fargate --vcpu 0.5 --memory-gb 1[/]"  # noqa: E501
                )
                raise typer.Exit(code=2)
            if fargate and show_reserved:
                console.print(
                    "[yellow]Note: --show-reserved is not applicable for Fargate pricing "  # noqa: E501
                    "and will be ignored.[/]"
                )
        elif cloud_lower == "gcp":
            if region is None:
                err_console.print(
                    "[bold red]--region is required when using --cloud gcp[/]\n"
                    "[dim]Example: --cloud gcp --region us-central1 --machine-type n2-standard-4[/]"  # noqa: E501
                )
                raise typer.Exit(code=2)
            if machine_type is None:
                err_console.print(
                    "[bold red]--machine-type is required when using --cloud gcp[/]\n"
                    "[dim]Example: --cloud gcp --region us-central1 --machine-type n2-standard-4[/]"  # noqa: E501
                )
                raise typer.Exit(code=2)
        elif cloud_lower == "azure":
            if region is None:
                err_console.print(
                    "[bold red]--region is required when using --cloud azure[/]\n"
                    "[dim]Example: --cloud azure --region eastus --sku-name 'D2s v3'[/]"
                )
                raise typer.Exit(code=2)
            if sku_name is None:
                err_console.print(
                    "[bold red]--sku-name is required when using --cloud azure[/]\n"
                    "[dim]Example: --cloud azure --region eastus --sku-name 'D2s v3'[/]"
                )
                raise typer.Exit(code=2)
        else:
            err_console.print(
                f"[bold red]Unsupported cloud provider: {cloud!r}. "
                "Supported: aws, gcp, azure[/]"
            )
            raise typer.Exit(code=2)

        from presidio_arch_translucency.cloud import PricingError

        try:
            with console.status(
                "[dim]Fetching cloud pricing "
                "(first run may take 30–60 s, cached afterwards)…[/dim]"
            ):
                if cloud_lower == "aws":
                    from presidio_arch_translucency.cloud import (
                        build_cost_params_from_aws,
                    )

                    tiered = build_cost_params_from_aws(
                        region=region,
                        instance_type=instance_type,
                        fargate=fargate,
                        vcpu=vcpu,
                        memory_gb=memory_gb,
                        no_cache=no_cache,
                        show_reserved=show_reserved,
                        show_spot=show_spot,
                    )
                elif cloud_lower == "gcp":
                    from presidio_arch_translucency.cloud_gcp import (
                        build_cost_params_from_gcp,
                    )

                    tiered = build_cost_params_from_gcp(
                        region=region,
                        machine_type=machine_type,
                        preemptible=show_spot,
                        no_cache=no_cache,
                    )
                else:  # azure
                    from presidio_arch_translucency.cloud_azure import (
                        build_cost_params_from_azure,
                    )

                    tiered = build_cost_params_from_azure(
                        region=region,
                        sku_name=sku_name,
                        spot=show_spot,
                        no_cache=no_cache,
                    )
        except PricingError as exc:
            err_console.print(f"[bold red]Cloud pricing error:[/] {exc}")
            raise typer.Exit(code=2) from exc

        log_security_event("COST_INVOCATION", {"layer": layer_str, "rps": rps})
        _render_tiered_cost(tiered, result)
    else:
        cp = CostParams(
            cost_per_container_hour=cost_per_container_hour,
            cost_per_pod_hour=cost_per_pod_hour,
            cost_per_deployment_hour=cost_per_deployment_hour,
            cost_per_node_hour=cost_per_node_hour,
        )
        cost_results = build_cost_results(result.layers, cp)
        log_security_event("COST_INVOCATION", {"layer": layer_str, "rps": rps})
        _render_cost(cost_results, result, pricing_note=None)


@app.command("calibrate")
def calibrate_cmd(
    observations: list[str] = typer.Option(  # noqa: B008
        ...,
        "--observation",
        "-o",
        help=(
            "Measured operating point as 'rps:latency_ms:replicas' "
            "(e.g. 300:80:5). Repeat for multiple points; >=2 recommended."
        ),
    ),
) -> None:
    """
    Fit the translucency model to measured workload points (analytical mode).

    Supply one or more observed `rps:latency_ms:replicas` triples from your APM,
    load tests, or prior `pat demo` output. The model's per-replica capacity
    (concurrency) and coordination overhead are fitted with
    scipy.optimize.curve_fit and written to ~/.pat/model.json, after which
    `pat analyze` uses your calibrated parameters and stops warning. No Docker
    required.

    \b
      pat calibrate --observation 100:50:2 --observation 300:80:5
    """
    from presidio_arch_translucency.calibrate import (  # noqa: PLC0415
        CalibrationError,
        fit_calibration,
        parse_observation,
        write_model_file,
    )

    try:
        parsed = [parse_observation(raw) for raw in observations]
        result = fit_calibration(parsed)
    except CalibrationError as exc:
        err_console.print(f"[bold red]Calibration error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    path = write_model_file(result)
    log_security_event("CALIBRATE_INVOCATION", {"observations": len(parsed)})
    _render_calibration(result, path)


@app.command("observe")
def observe_cmd(
    layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-c",
        help=f"Replication layer of the measurement. One of: {', '.join(VALID_LAYERS)}",
    ),
    rps: Optional[float] = typer.Option(  # noqa: UP045
        None, "--rps", "-r", help="Measured requests per second.", min=0.0
    ),
    avg_latency_ms: Optional[float] = typer.Option(  # noqa: UP045
        None, "--avg-latency-ms", "-l", help="Measured average latency (ms).", min=0.0
    ),
    p99_latency_ms: Optional[float] = typer.Option(  # noqa: UP045
        None, "--p99-latency-ms", help="Measured p99 latency (ms).", min=0.0
    ),
    throughput: Optional[float] = typer.Option(  # noqa: UP045
        None, "--throughput", help="Measured served throughput (req/s).", min=0.0
    ),
    replicas: Optional[int] = typer.Option(  # noqa: UP045
        None, "--replicas", help="Replica count during the measurement.", min=1
    ),
    list_recent: bool = typer.Option(
        False, "--list", help="List recent observations instead of recording one."
    ),
    limit: int = typer.Option(
        20, "--limit", help="Number of rows to show with --list.", min=1
    ),
    db: Optional[Path] = typer.Option(  # noqa: UP045, B008
        None,
        "--db",
        help="Override the store path (default: ~/.pat/observations.db).",
    ),
) -> None:
    """
    Record one workload observation, or list recent ones (--list).

    Single-shot by design: this records a single measurement and exits. Schedule
    recurring collection externally (cron / launchd / a Kubernetes CronJob).
    The store is source-agnostic — supply numbers measured by any source (APM, a
    load test, prior `pat demo` output); `pat demo` and Prometheus sources are
    wired in later v0.8.0 phases. `pat optimize` reads this store back.
    """
    from presidio_arch_translucency import observe as store  # noqa: PLC0415

    if list_recent:
        layer_filter = layer.strip().lower() if layer else None
        rows = store.latest_observations(limit, db_path=db, layer=layer_filter)
        total = store.count_observations(db_path=db, layer=layer_filter)
        log_security_event("OBSERVE_LIST", {"rows": len(rows)})
        _render_observations(rows, total=total)
        return

    # --- Record mode: all measurement fields are required ---
    required = {
        "--layer": layer,
        "--rps": rps,
        "--avg-latency-ms": avg_latency_ms,
        "--p99-latency-ms": p99_latency_ms,
        "--throughput": throughput,
        "--replicas": replicas,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        err_console.print(
            "[bold red]Recording requires all measurement options:[/] "
            + ", ".join(missing)
            + "\n[dim]Or use --list to view recent observations.[/]"
        )
        raise typer.Exit(code=2)

    try:
        lat = sanitize_latency_ms(avg_latency_ms)
        p99 = sanitize_latency_ms(p99_latency_ms)
        layer_str = sanitize_layer(layer, VALID_LAYERS)
        obs = store.record(
            rps=rps,
            avg_latency_ms=lat,
            p99_latency_ms=p99,
            throughput=throughput,
            layer=layer_str,
            replicas=replicas,
            db_path=db,
        )
    except (InputValidationError, store.ObservationError) as exc:
        err_console.print(f"[bold red]Invalid observation:[/] {exc}")
        raise typer.Exit(code=2) from exc

    total = store.count_observations(db_path=db)
    log_security_event("OBSERVE_RECORD", {"layer": layer_str, "replicas": replicas})
    console.print(
        f"[green]✓ Recorded[/] {obs.layer} observation "
        f"({obs.rps:.0f} req/s, p99 {obs.p99_latency_ms:.0f} ms, "
        f"{obs.replicas} replicas) → {total} total in store.\n"
    )


# ── rendering helpers ─────────────────────────────────────────────────────────


def _render_observations(rows: list, total: int) -> None:
    """Render recent observations from the rolling store."""
    if not rows:
        console.print(
            "\n[dim]No observations recorded yet. "
            "Record one with [bold]pat observe --layer … --rps … …[/], or schedule "
            "collection via cron/launchd.[/]\n"
        )
        return

    table = Table(
        title=f"Recent observations (showing {len(rows)} of {total})",
        box=box.ROUNDED,
        show_lines=False,
    )
    table.add_column("Timestamp (UTC)", style="dim", no_wrap=True)
    table.add_column("Layer", style="cyan")
    table.add_column("req/s", justify="right")
    table.add_column("Avg ms", justify="right")
    table.add_column("p99 ms", justify="right")
    table.add_column("Throughput", justify="right")
    table.add_column("Replicas", justify="right")

    for obs in rows:
        table.add_row(
            obs.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            obs.layer,
            f"{obs.rps:.0f}",
            f"{obs.avg_latency_ms:.0f}",
            f"{obs.p99_latency_ms:.0f}",
            f"{obs.throughput:.0f}",
            str(obs.replicas),
        )

    console.print()
    console.print(table)
    console.print()


def _render_calibration(result: object, path: Path) -> None:
    """Render fitted parameters, per-point predictions, and fit quality."""
    from presidio_arch_translucency.calibrate import (  # local import
        CalibrationResult,
    )

    assert isinstance(result, CalibrationResult)  # noqa: S101

    table = Table(
        title="Calibration fit — observed vs predicted",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Observed rps", justify="right")
    table.add_column("Latency (ms)", justify="right")
    table.add_column("Replicas", justify="right")
    table.add_column("Predicted rps", justify="right")
    table.add_column("Residual", justify="right")

    for obs, pred, resid in zip(
        result.observations, result.predictions, result.residuals
    ):
        resid_color = "green" if abs(resid) < 0.01 * max(obs.rps, 1.0) else "yellow"
        table.add_row(
            f"{obs.rps:.1f}",
            f"{obs.latency_ms:.1f}",
            str(obs.replicas),
            f"{pred:.1f}",
            f"[{resid_color}]{resid:+.2f}[/]",
        )

    r2_color = "green" if result.r_squared >= 0.95 else "yellow"
    body = (
        f"[bold]Concurrency (κ):[/]   [cyan]{result.concurrency:.3f}[/] "
        "req/replica in-flight\n"
        f"[bold]Overhead β:[/]        [cyan]{result.overhead_beta:.4f}[/]\n"
        f"[bold]R²:[/]                [{r2_color}]{result.r_squared:.4f}[/]\n"
        f"[bold]RMSE:[/]              {result.rmse:.4f} req/s\n\n"
        f"[dim]Written to {path}\n"
        f"`pat analyze` will now use these calibrated parameters.[/]"
    )

    console.print()
    console.print(
        Panel(
            body,
            title="[bold blue]Presidio Architectural Translucency — Calibration[/]",
            border_style="blue",
        )
    )
    console.print()
    console.print(table)
    console.print()


def _render_tiered_cost(tiered: object, result: object) -> None:
    """Render on-demand + optional reserved/spot pricing tiers."""
    from presidio_arch_translucency.cloud import TieredPricingResult  # local import

    assert isinstance(tiered, TieredPricingResult)  # noqa: S101

    od = tiered.on_demand
    cache_tag = " [dim](cached)[/dim]" if od.from_cache else ""
    _render_cost(
        build_cost_results(result.layers, od.params),  # type: ignore[union-attr]
        result,
        pricing_note=od.source_description + cache_tag,
    )

    if tiered.reserved_1yr is not None:
        r1 = tiered.reserved_1yr
        cache_tag = " [dim](cached)[/dim]" if r1.from_cache else ""
        console.print("\n[bold blue]1-Year Reserved Pricing[/]")
        _render_cost(
            build_cost_results(result.layers, r1.params),  # type: ignore[union-attr]
            result,
            pricing_note=r1.source_description + cache_tag,
        )

    if tiered.reserved_3yr is not None:
        r3 = tiered.reserved_3yr
        cache_tag = " [dim](cached)[/dim]" if r3.from_cache else ""
        console.print("\n[bold blue]3-Year Reserved Pricing[/]")
        _render_cost(
            build_cost_results(result.layers, r3.params),  # type: ignore[union-attr]
            result,
            pricing_note=r3.source_description + cache_tag,
        )

    if tiered.spot is not None:
        sp = tiered.spot
        cache_tag = " [dim](cached)[/dim]" if sp.from_cache else ""
        console.print(
            "\n[bold yellow]⚠ Spot / Preemptible Pricing — interruption risk[/]"
        )  # noqa: E501
        _render_cost(
            build_cost_results(result.layers, sp.params),  # type: ignore[union-attr]
            result,
            pricing_note=sp.source_description + cache_tag,
        )


def _render_cost(
    cost_results: list,  # type: ignore[type-arg]
    result: object,
    pricing_note: Optional[str] = None,  # noqa: UP045
) -> None:
    from presidio_arch_translucency.model import AnalysisResult  # local import

    assert isinstance(result, AnalysisResult)  # noqa: S101

    best = next(r for r in cost_results if r.is_recommended)

    # Summary panel
    cpr_str = format_cost_per_request(best.cost_per_request_usd)
    body = (
        f"[bold]Best ROI layer:[/]  [cyan]{best.layer.value}[/]\n"
        f"[bold]Replicas:[/]        [cyan]{best.replicas}[/]\n"
        f"[bold]Throughput gain:[/] [green]{best.throughput_gain_pct:+.1f}%[/]\n"
        f"[bold]Cost/hour:[/]       ${best.hourly_cost_usd:.4f}\n"
        f"[bold]Cost/request:[/]    {cpr_str}\n"
        f"[bold]ROI score:[/]       {best.roi_score:.1f}\n\n"
        f"[dim]{best.description}[/]"
    )
    console.print()
    console.print(
        Panel(
            body,
            title="[bold blue]Presidio Architectural Translucency — Cost Analysis[/]",
            border_style="blue",
        )
    )

    table = Table(
        title="All Layers — Cost vs Performance",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Layer", style="cyan", no_wrap=True)
    table.add_column("Replicas", justify="right")
    table.add_column("Throughput (req/s)", justify="right")
    table.add_column("Δ Throughput", justify="right")
    table.add_column("Δ RT", justify="right")
    table.add_column("Cost/hr (USD)", justify="right")
    table.add_column("Cost/req (USD)", justify="right")
    table.add_column("ROI score", justify="right")
    table.add_column("Best ROI", justify="center")

    for cr in cost_results:
        tp_color = "green" if cr.throughput_gain_pct >= 0 else "red"
        rt_color = "red" if cr.response_time_change_pct > 10 else "green"
        is_rec = cr.is_recommended
        is_cur = cr.layer == result.current_layer

        layer_label = cr.layer.value
        if is_cur:
            layer_label += " (current)"

        table.add_row(
            layer_label,
            str(cr.replicas),
            f"{cr.throughput_rps:.0f}",
            f"[{tp_color}]{cr.throughput_gain_pct:+.1f}%[/]",
            f"[{rt_color}]{cr.response_time_change_pct:+.1f}%[/]",
            f"${cr.hourly_cost_usd:.4f}",
            format_cost_per_request(cr.cost_per_request_usd),
            f"{cr.roi_score:.1f}",
            "[bold green]✓[/]" if is_rec else "",
        )

    console.print()
    console.print(table)
    pricing_line = f"\n[dim]Pricing source: {pricing_note}[/]" if pricing_note else ""
    console.print(
        f"\n[dim]Baseline: {result.baseline_throughput_rps:.0f} req/s "
        f"@ {result.baseline_response_time_ms:.1f} ms  "
        f"(current layer: {result.current_layer.value})[/]"
        + pricing_line
        + "\n[dim]ROI score = throughput-gain-% / cost-per-request  "
        "(higher = better performance-per-dollar)[/]\n"
    )


def _render_what_if(
    r: ScaleEventResult,
    cost_per_req: Optional[float] = None,  # noqa: UP045
) -> None:
    spike_x = r.rps_spike / max(r.rps_baseline, 0.01)
    trough_color = "red" if r.trough_throughput_pct < 80 else "yellow"
    steady_ok = r.steady_throughput_rps >= r.rps_spike * 0.98

    trough_cost_line = ""
    if cost_per_req is not None:
        tc = trough_cost_usd(r.missed_requests, cost_per_req)
        trough_cost_line = f"\n  Trough cost   ~${tc:,.2f} revenue impact"

    body = (
        f"[bold]Load:[/]  {r.rps_baseline:.1f} → {r.rps_spike:.1f} req/s"
        f"  ([bold]{spike_x:.1f}×[/])\n"
        f"[bold]Trough window:[/]  {r.trough_duration_s:.0f} s"
        f"  (HPA poll {r.params.hpa_poll_s:.0f} s"
        f"  +  pod startup {r.params.pod_startup_s:.0f} s"
        + (
            f"  +  cold-start {r.params.cold_start_s:.0f} s"
            if r.params.cold_start_s > 0
            else ""
        )
        + f")\n\n"
        f"[bold red]TROUGH[/]  (0 s – {r.trough_duration_s:.0f} s)\n"
        f"  Throughput    [{trough_color}]{r.trough_throughput_rps:.1f} req/s"
        f"  ({r.trough_throughput_pct:.0f} % of demand)[/]\n"
        f"  Avg latency   {r.trough_avg_latency_ms:,.0f} ms\n"
        f"  p99 latency   {r.trough_p99_latency_ms:,.0f} ms\n"
        f"  Missed reqs   ~{r.missed_requests:,}" + trough_cost_line + f"\n\n"
        f"[bold green]STEADY STATE[/]  (after {r.trough_duration_s:.0f} s"
        f"  —  {r.replicas_after} replicas)\n"
        f"  Throughput    {'[green]' if steady_ok else '[yellow]'}"
        f"{r.steady_throughput_rps:.1f} req/s[/]\n"
        f"  Avg latency   {r.steady_avg_latency_ms:,.0f} ms\n"
        f"  p99 latency   {r.steady_p99_latency_ms:,.0f} ms"
    )
    console.print()
    console.print(
        Panel(
            body,
            title=f"[bold blue]HPA Scale Event · {r.layer.value}  "
            f"{r.replicas_before} → {r.replicas_after} replicas[/]",
            border_style="blue",
        )
    )

    table = Table(
        title="Scale-event timeline",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Time", justify="right", style="dim")
    table.add_column("Replicas", justify="right")
    table.add_column("Throughput", justify="right")
    table.add_column("Avg Lat", justify="right")
    table.add_column("p99 Lat", justify="right")
    table.add_column("Phase", justify="center")

    ttr = r.trough_duration_s
    for pt in r.timeline:
        in_trough = pt.t_s < ttr
        phase = "[bold red]TROUGH[/]" if in_trough else "[bold green]STEADY[/]"
        row_style = "on #330000" if in_trough else ""
        table.add_row(
            f"{pt.t_s:.1f} s",
            str(pt.replicas),
            f"{pt.throughput_rps:.1f}",
            f"{pt.avg_latency_ms:,.0f} ms",
            f"{pt.p99_latency_ms:,.0f} ms",
            phase,
            style=row_style,
        )

    console.print()
    console.print(table)
    console.print()


def _render_slo(
    results: dict[ReplicationLayer, ScaleEventResult],
    p99_target: float,
    rps: float,
    spike_rps: float,
    params: ScaleEventParams,
    cost_params: Optional[CostParams] = None,  # noqa: UP045
) -> None:
    console.print()
    console.print(
        f"[bold]SLO:[/] p99 ≤ {p99_target:.0f} ms  |  "
        f"[bold]Workload:[/] {rps:.1f} req/s  |  "
        f"[bold]Spike:[/] {spike_rps / rps:.1f}× → {spike_rps:.1f} req/s\n"
        f"[dim]HPA timing: {params.hpa_poll_s:.0f} s poll + "
        f"{params.pod_startup_s:.0f} s startup"
        f" = {params.time_to_ready_s:.0f} s trough[/]"
    )

    table = Table(box=box.ROUNDED, show_lines=True)
    table.add_column("Layer", style="cyan", no_wrap=True)
    table.add_column("Before", justify="right")
    table.add_column("After", justify="right")
    table.add_column("Steady p99", justify="right")
    table.add_column("Trough p99", justify="right")
    table.add_column("Cost/hr (USD)", justify="right")
    table.add_column("SLO verdict", justify="left")

    best_layer = min(results.values(), key=lambda r: r.steady_p99_latency_ms)
    cp = cost_params if cost_params is not None else CostParams()

    for r in results.values():
        steady_ok = r.steady_p99_latency_ms <= p99_target
        trough_ok = r.trough_p99_latency_ms <= p99_target

        steady_str = (
            f"[green]{r.steady_p99_latency_ms:,.0f} ms ✓[/]"
            if steady_ok
            else f"[red]{r.steady_p99_latency_ms:,.0f} ms ✗[/]"
        )
        trough_str = (
            f"[green]{r.trough_p99_latency_ms:,.0f} ms ✓[/]"
            if trough_ok
            else f"[red]{r.trough_p99_latency_ms:,.0f} ms ✗[/]"
        )

        if steady_ok and trough_ok:
            verdict = "[bold green]Meets SLO ✓[/]"
        elif steady_ok:
            verdict = "[yellow]Steady ✓  Trough ✗[/]"
        elif trough_ok:
            verdict = "[yellow]Trough ✓  Steady ✗[/]"
        else:
            verdict = "[bold red]Fails SLO ✗[/]"

        hc = hourly_cost(r.layer, r.replicas_after, cp)
        cost_str = f"${hc:.4f}"
        if steady_ok:
            cost_str = f"[green]{cost_str}[/]"

        table.add_row(
            r.layer.value,
            str(r.replicas_before),
            str(r.replicas_after),
            steady_str,
            trough_str,
            cost_str,
            verdict,
        )

    console.print()
    console.print(table)

    # Recommendation
    passing = [r for r in results.values() if r.steady_p99_latency_ms <= p99_target]
    if passing:
        best = min(passing, key=lambda r: r.steady_p99_latency_ms)
        cheapest = min(
            passing, key=lambda r: hourly_cost(r.layer, r.replicas_after, cp)
        )
        trough_breach = best.trough_p99_latency_ms > p99_target
        rec_lines = (
            f"[bold]{best.layer.value}[/] meets the steady-state SLO "
            f"(p99 {best.steady_p99_latency_ms:,.0f} ms)."
        )
        if cheapest.layer != best.layer:
            cheapest_hc = hourly_cost(cheapest.layer, cheapest.replicas_after, cp)
            rec_lines += (
                f"\n[cyan]Min-cost option:[/] [bold]{cheapest.layer.value}[/]"
                f" also meets the SLO at ${cheapest_hc:.4f}/hr"
                f" ({cheapest.replicas_after} replicas)."
            )
        if trough_breach:
            rec_lines += (
                f"\n[yellow]Warning:[/] trough p99 = "
                f"{best.trough_p99_latency_ms:,.0f} ms violates SLO during spike.\n"
                f"  Set [bold]HPA minReplicas = {best.replicas_after}[/]"
                f" to pre-provision and eliminate the trough."
            )
        else:
            rec_lines += (
                "\nSLO is met in both steady-state and during the spike trough."
            )
    else:
        rec_lines = (
            f"No layer meets the p99 ≤ {p99_target:.0f} ms SLO at {rps:.1f} req/s.\n"
            f"[bold]{best_layer.layer.value}[/] is closest "
            f"(steady p99 {best_layer.steady_p99_latency_ms:,.0f} ms).\n"
            f"  → Reduce avg latency below "
            f"{p99_target / 1.8:.0f} ms, or\n"
            f"  → Pre-provision [bold]{best_layer.replicas_after}[/] replicas"
            f" and re-evaluate."
        )

    console.print()
    console.print(
        Panel(
            rec_lines,
            title="[bold blue]Recommendation[/]",
            border_style="blue",
        )
    )
    console.print()


if __name__ == "__main__":
    app()
