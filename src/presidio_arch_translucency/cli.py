"""
CLI entry-point for presidio-hardened-arch-translucency.

Usage:
    pat analyze --requests-per-second 500 --avg-latency-ms 80 --current-layer container
"""

from __future__ import annotations

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from presidio_arch_translucency import __version__
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

console = Console()
err_console = Console(stderr=True, style="bold red")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"pat version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool | None = typer.Option(
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


if __name__ == "__main__":
    app()
