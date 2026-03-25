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
from presidio_arch_translucency.demo import demo_command
from presidio_arch_translucency.hpa import (
    ScaleEventParams,
    ScaleEventResult,
    save_hpa_plot,
    simulate_scale_event,
)
from presidio_arch_translucency.model import (
    VALID_LAYERS,
    ReplicationLayer,
    analyze,
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
    _render_results(result, show_all=show_all)


def _render_results(result: AnalysisResult, show_all: bool) -> None:  # type: ignore[name-defined]  # noqa: F821
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

            table.add_row(
                lr.layer.value,
                str(lr.optimal_replicas),
                f"{lr.estimated_throughput_rps:.0f}",
                f"[{tp_color}]{lr.throughput_gain_pct:+.1f}%[/]",
                f"{lr.estimated_response_time_ms:.1f}",
                f"[{rt_style}]{lr.response_time_change_pct:+.1f}%[/]",
                "[bold green]✓[/]" if is_rec else "",
            )

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
    _render_what_if(result)

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
    _render_slo(results, p99_target_ms, rps, spike_rps, params)


# ── rendering helpers ─────────────────────────────────────────────────────────


def _render_what_if(r: ScaleEventResult) -> None:
    spike_x = r.rps_spike / max(r.rps_baseline, 0.01)
    trough_color = "red" if r.trough_throughput_pct < 80 else "yellow"
    steady_ok = r.steady_throughput_rps >= r.rps_spike * 0.98

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
        f"  Missed reqs   ~{r.missed_requests:,}\n\n"
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
    table.add_column("SLO verdict", justify="left")

    best_layer = min(results.values(), key=lambda r: r.steady_p99_latency_ms)

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

        table.add_row(
            r.layer.value,
            str(r.replicas_before),
            str(r.replicas_after),
            steady_str,
            trough_str,
            verdict,
        )

    console.print()
    console.print(table)

    # Recommendation
    passing = [r for r in results.values() if r.steady_p99_latency_ms <= p99_target]
    if passing:
        best = min(passing, key=lambda r: r.steady_p99_latency_ms)
        trough_breach = best.trough_p99_latency_ms > p99_target
        rec_lines = (
            f"[bold]{best.layer.value}[/] meets the steady-state SLO "
            f"(p99 {best.steady_p99_latency_ms:,.0f} ms)."
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
