"""
CLI entry-point for presidio-hardened-arch-translucency.

Usage:
    pat analyze --requests-per-second 500 --avg-latency-ms 80 --current-layer container
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.markup import escape
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
from presidio_arch_translucency.energy import (
    DEFAULT_REPLICA_POWER_WATTS,
    LayerEnergy,
    layer_energy,
    resolve_energy_fit_scope,
    resolve_energy_params,
)
from presidio_arch_translucency.hpa import (
    ScaleEventParams,
    ScaleEventResult,
    save_hpa_plot,
    simulate_scale_event,
)
from presidio_arch_translucency.model import (
    ALL_REPLICATION_LAYERS,
    DEFAULT_LAYER_NAME,
    REFERENCE_LATENCY_RANGE_MS,
    REFERENCE_RPS_RANGE,
    VALID_LAYERS,
    CalibrationTamperError,
    ReplicationLayer,
    analyze,
    base_capacity_rps,
    model_is_calibrated,
    resolve_calibration_commitment,
    resolve_concurrency,
)
from presidio_arch_translucency.observe import VALID_METERS
from presidio_arch_translucency.security import (
    InputValidationError,
    configure_logging,
    log_recommendation,
    log_security_event,
    run_dependency_audit,
    sanitize_bounded_number,
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


def _resolve_commitment_or_exit(model_layer: Optional[str]) -> dict:  # noqa: UP045
    """Resolve the active fit's calibration commitment, failing closed on tamper.

    Returns the commitment status dict for inclusion in the recommendation
    artifact. On a present-but-mismatched commitment (the model file was edited
    after calibration) prints the tamper error and exits non-zero rather than
    acting on tampered α/β. A legacy (uncommitted) fit is reported, not rejected.
    """
    try:
        return resolve_calibration_commitment(model_layer)
    except CalibrationTamperError as exc:
        err_console.print(f"[bold red]Calibration tamper detected:[/] {exc}")
        raise typer.Exit(code=2) from exc


def _gate_commitment_with_energy(model_layer: Optional[str]) -> dict:  # noqa: UP045
    """Gate the calibration commitment for *model_layer*, plus any cross-scope
    energy fit (v0.20.0, P2-1).

    ``--layer <named>`` gates only that record's commitment, but
    :func:`resolve_energy_params` can fall back to the **global** record's
    energy fit — so a tampered global ``energy_idle_w`` would otherwise render
    as "calibrated" Watts/J-per-req/EEI with no error. When the energy fit is
    read from a scope other than the gated one, verify that record's commitment
    too (global scope = ``model_layer`` ``None``/``default``) and fail closed
    identically. Returns the gated record's commitment status dict.
    """
    commitment = _resolve_commitment_or_exit(model_layer)
    if (
        model_layer not in (None, DEFAULT_LAYER_NAME)
        and resolve_energy_fit_scope(model_layer) == "global"
    ):
        _resolve_commitment_or_exit(None)
    return commitment


def _is_help_invocation() -> bool:
    """Return True when Typer is rendering help without executing a command."""
    return any(arg in {"--help", "-h"} for arg in sys.argv[1:])


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
    if not skip_audit and not _is_help_invocation():
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
    model_layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-L",
        help=(
            "Service-layer label whose calibrated parameters to use "
            "(see `pat calibrate --layer`). Falls back to the global fit."
        ),
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
    replica_power_watts: float = typer.Option(
        DEFAULT_REPLICA_POWER_WATTS,
        "--replica-power-watts",
        help=(
            "Per-replica peak power in watts for the energy columns "
            f"(MVP placeholder ≈{DEFAULT_REPLICA_POWER_WATTS:.0f} W; "
            "calibrate with `pat calibrate --energy-observation`)."
        ),
        min=0.1,
        max=10000.0,
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
        # Reject nan/inf that Typer's numeric range does not (house style).
        watts = sanitize_bounded_number(
            replica_power_watts, "replica_power_watts", 0.1, 10000.0
        )
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    current = ReplicationLayer(layer_str)
    _warn_if_uncalibrated()

    # --- Calibration-commitment gate (v0.19.0): fail closed if the model
    #     file's stored parameters no longer match their commitment. Also gates
    #     a cross-scope (global) energy fit the named layer falls back to
    #     (v0.20.0, P2-1). ---
    commitment = _gate_commitment_with_energy(model_layer)

    # --- Run analysis ---
    result = analyze(
        requests_per_second=rps,
        avg_latency_ms=lat,
        current_layer=current,
        layer=model_layer,
    )

    # --- Energy model (v0.20.0): compute W(δ)/J-per-req/EEI per layer from the
    #     same throughput curve the recommendation uses. ---
    base_cap = base_capacity_rps(rps, lat, resolve_concurrency(model_layer))
    energies = {
        lr.layer: layer_energy(
            lr.layer,
            lr.optimal_replicas,
            rps,
            base_cap,
            watts,
            model_layer,
        )
        for lr in result.layers
    }

    # --- Presidio security event logging ---
    best = next(r for r in result.layers if r.layer == result.recommended_layer)
    log_recommendation(
        layer=result.recommended_layer.value,
        replicas=result.recommended_replicas,
        throughput_gain_pct=best.throughput_gain_pct,
    )

    # --- Render output ---
    _render_results(
        result,
        show_all=show_all,
        uniform_cost=cost_per_replica_hour,
        commitment=commitment,
        energies=energies,
    )


@app.command("export")
def export_cmd(
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
    model_layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-L",
        help=(
            "Service-layer label whose calibrated parameters to use "
            "(see `pat calibrate --layer`). Falls back to the global fit."
        ),
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help=(
            "Address to bind. Defaults to loopback; a non-loopback host "
            "requires --listen-public."
        ),
    ),
    port: int = typer.Option(
        9847, "--port", help="TCP port to serve /metrics on.", min=1, max=65535
    ),
    listen_public: bool = typer.Option(
        False,
        "--listen-public",
        help="Explicitly allow binding a non-loopback (routable) host.",
    ),
    once: bool = typer.Option(
        False,
        "--once",
        help="Print the /metrics exposition once to stdout and exit (no server).",
    ),
    predict: bool = typer.Option(
        False,
        "--predict",
        help=(
            "Also expose forecast metrics derived from the observation store "
            "(`pat observe`): predicted demand and the replicas to serve it."
        ),
    ),
    model: str = typer.Option(
        "sma",
        "--model",
        help="Prediction model when --predict is set: 'sma' (cheap) or 'arima'.",
    ),
    window: int = typer.Option(
        10,
        "--window",
        help="Observations to smooth for the SMA prediction (--predict).",
        min=1,
    ),
    horizon_minutes: float = typer.Option(
        10.0,
        "--horizon-minutes",
        help="Forecast horizon in minutes (--predict).",
        min=0.0,
    ),
    predict_layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--predict-layer",
        help=(
            "Observation layer to forecast from (--predict). "
            "Defaults to --current-layer."
        ),
    ),
    db: Optional[Path] = typer.Option(  # noqa: UP045, B008
        None,
        "--db",
        help="Observation store path (--predict). Defaults to ~/.pat/observations.db.",
    ),
    cost_per_replica_hour: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--cost-per-replica-hour",
        help=(
            "Uniform replica cost (USD/replica/hour). Adds per-layer "
            "pat_cost_per_request and pat_hourly_cost_usd gauges."
        ),
        min=0.0,
    ),
    replica_power_watts: float = typer.Option(
        DEFAULT_REPLICA_POWER_WATTS,
        "--replica-power-watts",
        help=(
            "Per-replica peak power in watts for the MODELLED energy gauges "
            f"(MVP placeholder ≈{DEFAULT_REPLICA_POWER_WATTS:.0f} W; same as "
            "`pat analyze`)."
        ),
        min=0.1,
        max=10000.0,
    ),
    otlp: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--otlp",
        help=(
            "Push metrics once over OTLP/HTTP+JSON to this collector endpoint "
            "(e.g. http://collector:4318), then exit, instead of serving."
        ),
    ),
    service_name: str = typer.Option(
        "pat",
        "--service-name",
        help="OTLP resource service.name (--otlp).",
    ),
    pushgateway: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--pushgateway",
        help=(
            "Push metrics once (Prometheus text) to this Pushgateway, then exit "
            "(e.g. http://pushgateway:9091). For cron/CI/Job contexts."
        ),
    ),
    job: str = typer.Option(
        "pat", "--job", help="Pushgateway job name (--pushgateway)."
    ),
    grouping: Optional[list[str]] = typer.Option(  # noqa: UP045, B008
        None,
        "--grouping",
        help="Pushgateway grouping label key=value (repeatable; --pushgateway).",
    ),
    insecure_http: bool = typer.Option(
        False,
        "--insecure-http",
        help="Allow sending an OTLP/Pushgateway token over cleartext HTTP (dev).",
    ),
    timeout: float = typer.Option(
        10.0, "--timeout", help="OTLP/Pushgateway push HTTP timeout (s).", min=0.1
    ),
) -> None:
    """
    Serve or push architectural-translucency metrics.

    Default: a read-only Prometheus endpoint exposing the per-layer
    recommendation as gauges on GET /metrics (binds 127.0.0.1; --listen-public
    for a routable interface). With --predict it also exposes forecast metrics
    from the observation store. --once prints the exposition and exits. --otlp
    pushes the metrics once over OTLP/HTTP+JSON to an OpenTelemetry collector
    (vendor-neutral) and exits — schedule it externally for recurring push.
    """
    from presidio_arch_translucency.export import (  # noqa: PLC0415
        METRICS_PATH,
        build_metrics,
        build_prediction_metrics,
        build_server,
        is_loopback_host,
        render_exposition,
    )

    # --- Input sanitization (Presidio security extension) ---
    try:
        rps = sanitize_requests_per_second(requests_per_second)
        lat = sanitize_latency_ms(avg_latency_ms)
        layer_str = sanitize_layer(current_layer, VALID_LAYERS)
        watts = sanitize_bounded_number(
            replica_power_watts, "replica_power_watts", 0.1, 10000.0
        )
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    current = ReplicationLayer(layer_str)
    _gate_commitment_with_energy(model_layer)

    model_name = model.strip().lower()
    if predict and model_name not in ("sma", "arima"):
        err_console.print(
            f"[bold red]Unknown --model {model!r}.[/] Use 'sma' or 'arima'."
        )
        raise typer.Exit(code=2)

    # --- Exposure guard: loopback unless explicitly opted out ---
    if not is_loopback_host(host) and not listen_public:
        err_console.print(
            f"[bold red]Refusing to bind non-loopback host {host!r} without "
            "--listen-public.[/] The exporter is read-only, but its metrics "
            "would be reachable off-host. Re-run with --listen-public to confirm."
        )
        raise typer.Exit(code=2)

    _warn_if_uncalibrated()

    obs_layer = (predict_layer or layer_str).strip()

    def _prediction_metrics() -> list:
        from presidio_arch_translucency.observe import (  # noqa: PLC0415
            latest_observations,
        )
        from presidio_arch_translucency.optimize import (  # noqa: PLC0415
            ARIMA_DEFAULT_HISTORY,
        )

        pull = ARIMA_DEFAULT_HISTORY if model_name == "arima" else window
        observations = latest_observations(pull, db_path=db, layer=obs_layer)
        return build_prediction_metrics(
            observations, model=model_name, horizon_minutes=horizon_minutes
        )

    def _build_all_metrics() -> list:
        from presidio_arch_translucency.export import (  # noqa: PLC0415
            measured_energy_metrics,
        )

        metrics = build_metrics(
            rps,
            lat,
            current,
            layer=model_layer,
            cost_per_replica_hour=cost_per_replica_hour,
            replica_power_watts=watts,
        )
        # Measured power gauges appear only when the energy store has readings.
        metrics = metrics + measured_energy_metrics(db_path=db)
        if predict:
            metrics = metrics + _prediction_metrics()
        return metrics

    def _provider() -> str:
        return render_exposition(_build_all_metrics())

    predict_mode = model_name if predict else "off"

    if otlp and pushgateway:
        err_console.print(
            "[bold red]--otlp and --pushgateway are mutually exclusive.[/]"
        )
        raise typer.Exit(code=2)

    if pushgateway:
        from presidio_arch_translucency.pushgateway import (  # noqa: PLC0415
            PushgatewayError,
            parse_grouping,
            pushgateway_url,
        )
        from presidio_arch_translucency.pushgateway import (  # noqa: PLC0415
            push as pg_push,
        )
        from presidio_arch_translucency.pushgateway import (
            resolve_token as pg_resolve_token,
        )

        try:
            grouping_map = parse_grouping(grouping or [])
            target = pushgateway_url(pushgateway, job, grouping_map)
        except PushgatewayError as exc:
            err_console.print(f"[bold red]Pushgateway error:[/] {exc}")
            raise typer.Exit(code=2) from exc

        try:
            pg_token = pg_resolve_token(pushgateway, insecure_http=insecure_http)
            if insecure_http and pg_token:
                warn_console.print(
                    "[yellow]⚠ --insecure-http: sending the Pushgateway token over "
                    "cleartext HTTP. Use only for localhost development.[/]"
                )
            pg_push(
                pushgateway,
                job,
                _provider(),
                grouping=grouping_map,
                token=pg_token,
                timeout=timeout,
            )
        except PushgatewayError as exc:
            err_console.print(f"[bold red]Pushgateway error:[/] {exc}")
            raise typer.Exit(code=1) from exc

        host = pushgateway.split("://", 1)[-1].split("/", 1)[0]
        log_security_event(
            "PUSHGATEWAY_PUSH",
            {"pushgateway_host": host, "job": job, "predict": predict_mode},
        )
        console.print(f"[green]Pushed metrics[/] to {target}")
        return

    if otlp:
        from presidio_arch_translucency.otlp import (  # noqa: PLC0415
            OtlpError,
            build_otlp_payload,
            metrics_url,
            post_otlp,
            resolve_token,
        )

        try:
            target = metrics_url(otlp)
        except OtlpError as exc:
            err_console.print(f"[bold red]OTLP error:[/] {exc}")
            raise typer.Exit(code=2) from exc

        payload = build_otlp_payload(_build_all_metrics(), service_name=service_name)
        try:
            token = resolve_token(otlp, insecure_http=insecure_http)
            if insecure_http and token:
                warn_console.print(
                    "[yellow]⚠ --insecure-http: sending the OTLP token over "
                    "cleartext HTTP. Use only for localhost development.[/]"
                )
            post_otlp(
                otlp,
                payload,
                token=token,
                timeout=timeout,
                insecure_http=insecure_http,
            )
        except OtlpError as exc:
            err_console.print(f"[bold red]OTLP error:[/] {exc}")
            raise typer.Exit(code=1) from exc

        host = otlp.split("://", 1)[-1].split("/", 1)[0]
        log_security_event(
            "OTLP_PUSH",
            {"otlp_host": host, "layer": layer_str, "predict": predict_mode},
        )
        console.print(f"[green]Pushed OTLP metrics[/] to {target}")
        return

    if once:
        log_security_event(
            "EXPORT_RENDER",
            {"layer": layer_str, "mode": "once", "predict": predict_mode},
        )
        typer.echo(_provider(), nl=False)
        return

    log_security_event(
        "EXPORT_SERVE",
        {"host": host, "port": port, "layer": layer_str, "predict": predict_mode},
    )
    server = build_server(host, port, _provider)
    if not is_loopback_host(host):
        warn_console.print(
            f"[yellow]⚠ Serving metrics on routable {host}:{port} — read-only, "
            "no secrets exposed, but reachable off-host.[/]"
        )
    if predict and model_name == "arima":
        warn_console.print(
            "[yellow]⚠ --model arima refits on every scrape; for frequent "
            "scrape intervals prefer --model sma.[/]"
        )
    console.print(
        f"[green]pat exporter[/] serving read-only metrics on "
        f"http://{host}:{port}{METRICS_PATH}  (Ctrl-C to stop)"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        console.print("\n[dim]Shutting down exporter…[/]")
    finally:
        server.server_close()


@app.command("rules")
def rules_cmd(
    current_layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--current-layer",
        "-c",
        help=(
            "Layer you run, for the translucency-mismatch alert. "
            f"One of: {', '.join(VALID_LAYERS)}. Omit to skip that alert."
        ),
    ),
    cost_budget: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--cost-budget",
        help=(
            "USD/request budget for the cost alert (needs the exporter run with "
            "--cost-per-replica-hour). Omit to skip the cost alert."
        ),
        min=0.0,
    ),
    energy_budget: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--energy-budget",
        help=(
            "J/request budget for the energy alert (fires on the MODELLED "
            "pat_energy_per_request_joules gauge). Also enables the idle-energy "
            "alert. Omit to skip the over-budget alert."
        ),
    ),
    energy: bool = typer.Option(
        False,
        "--energy",
        help=(
            "Include the idle-energy-waste alert (PatIdleEnergyWaste) even without "
            "--energy-budget."
        ),
    ),
    surge_ratio: float = typer.Option(
        1.2,
        "--demand-surge-ratio",
        help="Forecast/observed demand ratio that fires the surge alert.",
        min=1.0,
    ),
    trend_threshold: float = typer.Option(
        0.2,
        "--trend-threshold",
        help="Demand trend ratio that fires the trend alert (0.2 = +20%).",
        min=0.0,
    ),
    for_duration: str = typer.Option(
        "10m",
        "--for",
        help="Prometheus `for:` duration on the demand/cost/mismatch alerts.",
    ),
) -> None:
    """
    Emit Prometheus recording + alerting rules from the pat metrics.

    Produces a declarative rule file (YAML) on stdout — reference it from
    prometheus.yml `rule_files:` so the model's signals fire through your
    existing Alertmanager. `pat` only emits; it never loads or applies anything.

    \b
      pat rules > pat-rules.yml
      pat rules -c container --cost-budget 0.000001 > pat-rules.yml
    """
    from presidio_arch_translucency.rules import (  # noqa: PLC0415
        RuleError,
        build_rule_groups,
        render_rules_yaml,
    )

    layer_str: Optional[str] = None  # noqa: UP045
    if current_layer is not None:
        try:
            layer_str = sanitize_layer(current_layer, VALID_LAYERS)
        except InputValidationError as exc:
            err_console.print(f"[bold red]Input validation error:[/] {exc}")
            raise typer.Exit(code=2) from exc

    # Reject nan/inf that Typer's range does not (house style, mirrors analyze).
    if energy_budget is not None:
        try:
            energy_budget = sanitize_bounded_number(
                energy_budget, "energy_budget", 0.0, 1.0e12
            )
        except InputValidationError as exc:
            err_console.print(f"[bold red]Input validation error:[/] {exc}")
            raise typer.Exit(code=2) from exc

    try:
        groups = build_rule_groups(
            current_layer=layer_str,
            cost_budget=cost_budget,
            surge_ratio=surge_ratio,
            trend_threshold=trend_threshold,
            for_duration=for_duration,
            energy_budget=energy_budget,
            energy=energy,
        )
    except RuleError as exc:
        err_console.print(f"[bold red]Rules error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "RULES_EMIT",
        {
            "layer": layer_str or "none",
            "cost_alert": cost_budget is not None,
            "energy_alert": energy_budget is not None or energy,
        },
    )
    typer.echo(render_rules_yaml(groups), nl=False)


@app.command("annotate")
def annotate_cmd(
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
    grafana: str = typer.Option(
        ...,
        "--grafana",
        help="Grafana base URL (https). Posts to <url>/api/annotations.",
    ),
    model_layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-L",
        help="Service-layer label whose calibrated parameters to use.",
    ),
    dashboard_uid: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--dashboard-uid",
        help="Attach the annotation to a specific dashboard (UID). Default: org-wide.",
    ),
    tags: Optional[list[str]] = typer.Option(  # noqa: UP045, B008
        None,
        "--tag",
        help="Extra annotation tag (repeatable).",
    ),
    insecure_http: bool = typer.Option(
        False,
        "--insecure-http",
        help="Allow posting the token over cleartext HTTP (localhost dev only).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the annotation payload and exit without posting (no token).",
    ),
    timeout: float = typer.Option(
        10.0, "--timeout", help="HTTP timeout in seconds.", min=0.1
    ),
) -> None:
    """
    Post the current recommendation to Grafana as an annotation.

    Runs the architectural-translucency analysis and posts a marker to Grafana's
    annotations API so the recommendation appears on your dashboards. This is
    pat's one outbound write — an informational annotation, never an
    infrastructure change. The Grafana token is read from PAT_GRAFANA_TOKEN only
    (never a flag); HTTPS is required unless --insecure-http. Use --dry-run to
    preview the payload without posting.
    """
    from presidio_arch_translucency.annotate import (  # noqa: PLC0415
        AnnotateError,
        build_annotation,
        post_annotation,
        resolve_token,
    )

    try:
        rps = sanitize_requests_per_second(requests_per_second)
        lat = sanitize_latency_ms(avg_latency_ms)
        layer_str = sanitize_layer(current_layer, VALID_LAYERS)
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    current = ReplicationLayer(layer_str)
    _resolve_commitment_or_exit(model_layer)
    result = analyze(
        requests_per_second=rps,
        avg_latency_ms=lat,
        current_layer=current,
        layer=model_layer,
    )

    try:
        annotation = build_annotation(
            result, extra_tags=tuple(tags or ()), dashboard_uid=dashboard_uid
        )
    except AnnotateError as exc:
        err_console.print(f"[bold red]Annotation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    if dry_run:
        log_security_event("ANNOTATE_DRYRUN", {"layer": result.recommended_layer.value})
        typer.echo(json.dumps(annotation.payload(), indent=2))
        return

    try:
        token = resolve_token()
        if insecure_http:
            warn_console.print(
                "[yellow]⚠ --insecure-http: sending the Grafana token over "
                "cleartext HTTP. Use only for localhost development.[/]"
            )
        response = post_annotation(
            grafana,
            annotation,
            token=token,
            timeout=timeout,
            insecure_http=insecure_http,
        )
    except AnnotateError as exc:
        err_console.print(f"[bold red]Annotation error:[/] {exc}")
        raise typer.Exit(code=1) from exc

    host = grafana.split("://", 1)[-1].split("/", 1)[0]
    log_security_event(
        "ANNOTATE_POST",
        {
            "grafana_host": host,
            "dashboard_scoped": dashboard_uid is not None,
            "layer": result.recommended_layer.value,
        },
    )
    ann_id = response.get("id")
    console.print(
        f"[green]Annotation posted[/] to {host}"
        + (f" (id {ann_id})" if ann_id is not None else "")
    )


@app.command("scaler")
def scaler_cmd(
    target: str = typer.Option(
        ..., "--target", "-t", help="Deployment to scale (RFC 1123 name)."
    ),
    prometheus_url: str = typer.Option(
        ...,
        "--prometheus-url",
        help="Prometheus URL the autoscaler queries (e.g. http://prom:9090).",
    ),
    fmt: str = typer.Option(
        "keda",
        "--format",
        help="Emit format: 'keda' (ScaledObject) or 'prometheus-adapter' (HPA).",
    ),
    namespace: Optional[str] = typer.Option(  # noqa: UP045
        None, "--namespace", "-n", help="Target namespace (RFC 1123 name)."
    ),
    name: Optional[str] = typer.Option(  # noqa: UP045
        None, "--name", help="Name of the emitted object (default <target>-pat)."
    ),
    layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-c",
        help=(
            "Filter the default query to one layer "
            f"({', '.join(VALID_LAYERS)}). Ignored when --query is given."
        ),
    ),
    query: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--query",
        help=(
            "Override the PromQL query "
            "(default: max(pat_predicted_recommended_replicas))."
        ),
    ),
    min_replicas: int = typer.Option(
        1, "--min-replicas", help="Minimum replica count.", min=1
    ),
    max_replicas: int = typer.Option(
        10, "--max-replicas", help="Maximum replica count.", min=1
    ),
    signal: str = typer.Option(
        "replicas",
        "--signal",
        help=(
            "Scaling signal: 'replicas' (track pat's forecast, default) or "
            "'energy' (scale on modelled J/req vs a budget)."
        ),
    ),
    energy_budget_j_per_req: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--energy-budget-j-per-req",
        help=(
            "Energy budget in joules per request (required with --signal energy). "
            "Scale OUT when modelled J/req exceeds this."
        ),
    ),
) -> None:
    """
    Emit autoscaler config that scales a Deployment on a pat signal.

    Closes the loop: the exporter publishes the pat gauges (run `pat export
    --predict`, scraped into Prometheus); this emits a KEDA ScaledObject
    (default) or a Prometheus-Adapter HPA that scales --target. Emit-only —
    prints YAML to stdout; `pat` never applies or scales anything.

    --signal replicas (default) tracks pat_predicted_recommended_replicas.
    --signal energy scales on pat_energy_per_request_joules vs
    --energy-budget-j-per-req (more replicas amortise standing power only when
    the layer's EEI > 1 — see the generated YAML comment).

    \b
      pat scaler -t web --prometheus-url http://prom:9090 -c container
      pat scaler -t web --prometheus-url http://prom:9090 --format prometheus-adapter
      pat scaler -t web --prometheus-url http://prom:9090 \\
          --signal energy --energy-budget-j-per-req 0.5 -c container
    """
    from presidio_arch_translucency.scaler import (  # noqa: PLC0415
        DEFAULT_METRIC,
        ENERGY_METRIC,
        VALID_FORMATS,
        VALID_SIGNALS,
        ScalerError,
        build_scaler,
        default_query,
    )

    if fmt not in VALID_FORMATS:
        err_console.print(
            f"[bold red]Unknown --format {fmt!r}.[/] Use one of: "
            f"{', '.join(VALID_FORMATS)}."
        )
        raise typer.Exit(code=2)
    if signal not in VALID_SIGNALS:
        err_console.print(
            f"[bold red]Unknown --signal {signal!r}.[/] Use one of: "
            f"{', '.join(VALID_SIGNALS)}."
        )
        raise typer.Exit(code=2)

    if signal == "energy" and energy_budget_j_per_req is not None:
        try:
            energy_budget_j_per_req = sanitize_bounded_number(
                energy_budget_j_per_req, "energy_budget_j_per_req", 1e-9, 1e9
            )
        except InputValidationError as exc:
            err_console.print(f"[bold red]Input validation error:[/] {exc}")
            raise typer.Exit(code=2) from exc

    if signal == "energy" and energy_budget_j_per_req is not None and layer is None:
        err_console.print(
            "[bold red]--signal energy requires --current-layer/-c[/] so the "
            "autoscaler targets one deployment-specific energy series."
        )
        raise typer.Exit(code=2)

    active_metric = ENERGY_METRIC if signal == "energy" else DEFAULT_METRIC

    try:
        effective_query = query if query else default_query(active_metric, layer)
        yaml = build_scaler(
            fmt,
            target,
            prometheus_url,
            effective_query,
            metric=active_metric,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            namespace=namespace,
            name=name,
            signal=signal,
            energy_budget_j_per_req=energy_budget_j_per_req,
        )
    except ScalerError as exc:
        err_console.print(f"[bold red]Scaler error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "SCALER_EMIT",
        {"format": fmt, "target": target, "layer": layer or "all", "signal": signal},
    )
    typer.echo(yaml, nl=False)


def _fmt_watts(value: float) -> str:
    """Watts to one decimal (the energy table / panel convention)."""
    return f"{value:.1f}"


def _fmt_jreq(value: Optional[float]) -> str:  # noqa: UP045
    """Joules-per-request to 3 significant figures, "—" when undefined (ω→0)."""
    return "—" if value is None else f"{value:.3g}"


def _fmt_eei(value: Optional[float]) -> str:  # noqa: UP045
    """EEI to 2 decimals, "—" when undefined (division guard)."""
    return "—" if value is None else f"{value:.2f}"


def _energy_note(source: Optional[str]) -> str:  # noqa: UP045
    """Provenance line for the energy figures: MVP defaults vs calibrated."""
    if source == "calibrated":
        return (
            "[dim]energy model: calibrated (fitted from --energy-observation "
            "watt readings)[/]"
        )
    return "[dim]energy model: MVP defaults — calibrate with --energy-observation[/]"


def _fmt_wh(value: float) -> str:
    """Watt-hours to 3 significant figures (budget table convention)."""
    return f"{value:.3g}"


def _fmt_grams(value: Optional[float]) -> str:  # noqa: UP045
    """gCO₂eq to 3 significant figures, "—" when undefined."""
    return "—" if value is None else f"{value:.3g}"


def _render_results(
    result: AnalysisResult,  # type: ignore[name-defined]  # noqa: F821
    show_all: bool,
    uniform_cost: Optional[float] = None,  # noqa: UP045
    commitment: Optional[dict] = None,  # noqa: UP045
    energies: Optional[dict] = None,  # noqa: UP045
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
        f"[bold]Est. response time:[/] {best.estimated_response_time_ms:.1f} ms"
    )

    best_energy: Optional[LayerEnergy] = None  # noqa: UP045
    if energies is not None:
        best_energy = energies.get(result.recommended_layer)
    if best_energy is not None:
        summary += (
            f"\n[bold]Est. power:[/]         {_fmt_watts(best_energy.watts)} W  ·  "
            f"J/req {_fmt_jreq(best_energy.joules_per_request)}  ·  "
            f"EEI {_fmt_eei(best_energy.eei)}\n"
            f"{_energy_note(best_energy.source)}"
        )
    summary += f"\n\n[dim]{best.description}[/]"

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
        if energies is not None:
            table.add_column("Watts", justify="right")
            table.add_column("J/req", justify="right")
            table.add_column("EEI", justify="right")
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
            if energies is not None:
                le = energies.get(lr.layer)
                if le is None:
                    row.extend(["—", "—", "—"])
                else:
                    eei_color = (
                        "green" if le.eei is not None and le.eei > 1.0 else "yellow"
                    )
                    row.append(_fmt_watts(le.watts))
                    row.append(_fmt_jreq(le.joules_per_request))
                    row.append(f"[{eei_color}]{_fmt_eei(le.eei)}[/]")
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
        # The energy columns widen the all-layers table past the 80-column
        # width Rich assumes for non-TTY / captured output (pipes, CI logs, test
        # capture); widen only that case so no header truncates. A real terminal
        # keeps its own detected width — never force-wrap an 80/120-col TTY.
        table_console = console
        if not console.is_terminal:
            table_console = Console(width=160)
        table_console.print(table)

    console.print(
        f"\n[dim]Baseline: {result.baseline_throughput_rps:.0f} req/s "
        f"@ {result.baseline_response_time_ms:.1f} ms  "
        f"(current layer: {result.current_layer.value})[/]"
    )
    console.print(_commitment_line(commitment) + "\n")


def _commitment_line(commitment: Optional[dict]) -> str:  # noqa: UP045
    """One-line calibration-commitment provenance for a recommendation artifact.

    ``ok`` shows the bound digest (the recommendation is tied to the exact fitted
    α/β and their observation set). ``legacy`` states the fit predates
    commitments. ``uncalibrated`` states no calibrated model drove the result.
    """
    status = (commitment or {}).get("status", "uncalibrated")
    if status == "ok":
        digest = (commitment or {}).get("digest") or ""
        return (
            f"[dim]Calibration commitment: [green]{digest[:16]}…[/] "
            "(parameters bound to their observation set)[/]"
        )
    if status == "legacy":
        return (
            "[dim]Calibration commitment: [yellow]none (legacy fit)[/] — "
            "these parameters predate commitments and are not bound to an "
            "observation set. Recalibrate to bind them.[/]"
        )
    return "[dim]Calibration commitment: n/a (uncalibrated — default parameters)[/]"


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
    model_layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-L",
        help=(
            "Service-layer label whose calibrated parameters to use "
            "(see `pat calibrate --layer`). Falls back to the global fit."
        ),
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
    replica_power_watts: float = typer.Option(
        DEFAULT_REPLICA_POWER_WATTS,
        "--replica-power-watts",
        help=(
            "Per-replica peak power in watts for the energy estimate "
            f"(MVP placeholder ≈{DEFAULT_REPLICA_POWER_WATTS:.0f} W)."
        ),
        min=0.1,
        max=10000.0,
    ),
    energy_aware: bool = typer.Option(
        False,
        "--energy-aware",
        help=(
            "Add an idle-energy-vs-trough section: standing energy of warm "
            "minReplicas vs the trough's revenue loss."
        ),
    ),
    electricity_cost_per_kwh: float = typer.Option(
        0.12,
        "--electricity-cost-per-kwh",
        help=(
            "Electricity price in USD/kWh for --energy-aware "
            "(placeholder average; set your tariff)."
        ),
        min=0.001,
        max=10.0,
    ),
    region: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--region",
        help="Cloud region for a gCO₂ line in the --energy-aware section.",
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
        watts = sanitize_bounded_number(
            replica_power_watts, "replica_power_watts", 0.1, 10000.0
        )
        elec_cost = sanitize_bounded_number(
            electricity_cost_per_kwh, "electricity_cost_per_kwh", 0.001, 10.0
        )
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    if srps <= rps:
        err_console.print("[bold red]--spike-rps must be greater than --current-rps[/]")
        raise typer.Exit(code=2)

    layer = ReplicationLayer(layer_str)
    _warn_if_uncalibrated()
    # --- Calibration-commitment gate (v0.20.0, P2-2): what-if now consumes
    #     calibrated κ and energy, so it honours the tamper signal analyze does
    #     (incl. the P2-1 cross-scope energy rule). ---
    _gate_commitment_with_energy(model_layer)
    params = ScaleEventParams(
        hpa_poll_s=hpa_poll_s,
        pod_startup_s=pod_startup_s,
        cold_start_s=cold_start_s,
    )
    concurrency = resolve_concurrency(model_layer)
    result = simulate_scale_event(
        rps_baseline=rps,
        rps_spike=srps,
        avg_latency_ms=lat,
        layer=layer,
        params=params,
        replicas_before=replicas_before,
        replicas_after=replicas_after,
        concurrency=concurrency,
    )
    # Energy at the evaluated (steady-state) operating point: δ_after replicas
    # serving the spike demand.
    base_cap = base_capacity_rps(srps, lat, concurrency)
    energy = layer_energy(
        layer, result.replicas_after, srps, base_cap, watts, model_layer
    )

    energy_aware_data: dict | None = None
    if energy_aware:
        energy_aware_data = _build_energy_aware(
            result,
            layer,
            watts,
            base_cap,
            model_layer,
            elec_cost,
            cost_per_req,
            region,
        )
    log_security_event("WHAT_IF_INVOCATION", {"layer": layer_str, "spike_rps": srps})
    _render_what_if(
        result,
        cost_per_req=cost_per_req,
        energy=energy,
        energy_aware=energy_aware_data,
    )

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
    model_layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-L",
        help=(
            "Service-layer label whose calibrated parameters to use "
            "(see `pat calibrate --layer`). Falls back to the global fit."
        ),
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
    # --- Calibration-commitment gate (v0.20.0, P2-2): slo now consumes
    #     calibrated κ and energy (J/req), so it honours the tamper signal
    #     analyze does (incl. the P2-1 cross-scope energy rule). ---
    _gate_commitment_with_energy(model_layer)
    spike_rps = rps * spike_multiplier
    params = ScaleEventParams(
        hpa_poll_s=hpa_poll_s,
        pod_startup_s=pod_startup_s,
        cold_start_s=cold_start_s,
    )

    concurrency = resolve_concurrency(model_layer)
    results = {
        layer: simulate_scale_event(
            rps_baseline=rps,
            rps_spike=spike_rps,
            avg_latency_ms=lat,
            layer=layer,
            params=params,
            concurrency=concurrency,
        )
        for layer in ALL_REPLICATION_LAYERS
    }
    # Energy intensity (J/req) as the frontier's third axis: p99 × $/req × J/req.
    # Uses the MVP-default per-replica peak power (slo carries no power flag).
    base_cap = base_capacity_rps(spike_rps, lat, concurrency)
    jreq_by_layer = {
        layer: layer_energy(
            layer,
            res.replicas_after,
            spike_rps,
            base_cap,
            DEFAULT_REPLICA_POWER_WATTS,
            model_layer,
        ).joules_per_request
        for layer, res in results.items()
    }
    log_security_event("SLO_INVOCATION", {"rps": rps, "p99_target_ms": p99_target_ms})
    _render_slo(
        results,
        p99_target_ms,
        rps,
        spike_rps,
        params,
        CostParams(),
        jreq_by_layer=jreq_by_layer,
    )


@app.command("budget")
def budget_cmd(
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
    model_layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-L",
        help=(
            "Service-layer label whose calibrated parameters to use "
            "(see `pat calibrate --layer`). Falls back to the global fit."
        ),
    ),
    replica_power_watts: float = typer.Option(
        DEFAULT_REPLICA_POWER_WATTS,
        "--replica-power-watts",
        help=(
            "Per-replica peak power in watts for the energy figures "
            f"(MVP placeholder ≈{DEFAULT_REPLICA_POWER_WATTS:.0f} W)."
        ),
        min=0.1,
        max=10000.0,
    ),
    region: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--region",
        help=(
            "Cloud region for carbon columns (e.g. us-east-1 / europe-north1 / "
            "eastus). Adds gCO₂/req and gCO₂/window."
        ),
    ),
    energy_budget_wh: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--energy-budget-wh",
        help=(
            "Energy budget in watt-hours over --window-h. Direction 1: maximise "
            "throughput within the budget."
        ),
    ),
    window_h: float = typer.Option(
        1.0,
        "--window-h",
        help="Budget window in hours (Direction 1). Bounds 0.01–8760.",
    ),
    carbon_budget_g: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--carbon-budget-g",
        help=(
            "Carbon budget in gCO₂eq (requires --region; mutually exclusive with "
            "--energy-budget-wh). Converted to a Wh budget via grid intensity."
        ),
    ),
    json_out: bool = typer.Option(
        False, "--json", help="Emit the budget result as JSON instead of a table."
    ),
) -> None:
    """
    Budget the watt: the SEANERGYS objective in both directions.

    Direction 1 (--energy-budget-wh or --carbon-budget-g): maximise throughput
    per layer while modelled energy stays within the budget over --window-h.

    Direction 2 (default): the minimum modelled energy meeting demand — the
    least-watt-hour layer that saturates the workload ("less energy, same
    output").

    Add --region for gCO₂/req and gCO₂/window columns. All figures are MODELLED
    estimates (analytic energy model × cited grid intensity); nothing here is
    measured, chained, or signed (E1a).

    \b
      pat budget -r 500 -l 80 --energy-budget-wh 40 --window-h 1
      pat budget -r 500 -l 80 --region europe-north1
    """
    from presidio_arch_translucency.budget import (  # noqa: PLC0415
        solve_energy_budget,
        solve_min_energy,
    )
    from presidio_arch_translucency.carbon import CarbonError, resolve_carbon_intensity

    try:
        rps = sanitize_requests_per_second(requests_per_second)
        lat = sanitize_latency_ms(avg_latency_ms)
        watts = sanitize_bounded_number(
            replica_power_watts, "replica_power_watts", 0.1, 10000.0
        )
        win = sanitize_bounded_number(window_h, "window_h", 0.01, 8760.0)
        if energy_budget_wh is not None:
            energy_budget_wh = sanitize_bounded_number(
                energy_budget_wh, "energy_budget_wh", 1e-9, 1e12
            )
        if carbon_budget_g is not None:
            carbon_budget_g = sanitize_bounded_number(
                carbon_budget_g, "carbon_budget_g", 1e-9, 1e15
            )
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    # --- Mutual exclusion + carbon-budget prerequisites ---
    if energy_budget_wh is not None and carbon_budget_g is not None:
        err_console.print(
            "[bold red]--energy-budget-wh and --carbon-budget-g are mutually "
            "exclusive.[/]"
        )
        raise typer.Exit(code=2)
    if carbon_budget_g is not None and region is None:
        err_console.print(
            "[bold red]--carbon-budget-g requires --region[/] "
            "(to convert grams → Wh via grid intensity)."
        )
        raise typer.Exit(code=2)

    _warn_if_uncalibrated()
    # Same tamper gate analyze uses (incl. the cross-scope energy rule).
    commitment = _gate_commitment_with_energy(model_layer)

    # --- Carbon intensity (for columns and/or carbon-budget conversion) ---
    intensity: float | None = None
    intensity_source: str | None = None
    if region is not None:
        try:
            intensity, intensity_source = resolve_carbon_intensity(region)
        except CarbonError as exc:
            # escape(): the message embeds the user-supplied --region, so
            # neutralise any Rich markup (e.g. "[blink]") in it — the region
            # renders as literal text, never as console styling.
            err_console.print(f"[bold red]Carbon error:[/] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc

    # --- Solve ---
    if carbon_budget_g is not None:
        # grams → Wh: budget_wh = grams / intensity(gCO₂/kWh) × 1000 (Wh/kWh).
        # Belt-and-suspenders: resolve_carbon_intensity is now bounds-validated
        # and never returns ≤ 0, but guard the division anyway so any future
        # regression surfaces as a clear CarbonError (exit 2), never a raw
        # ZeroDivisionError traceback.
        if intensity is None or intensity <= 0:
            err_console.print(
                "[bold red]Carbon error:[/] resolved grid intensity is not "
                "positive; cannot convert a carbon budget to energy."
            )
            raise typer.Exit(code=2)
        budget_wh = carbon_budget_g / intensity * 1000.0
        report = solve_energy_budget(
            rps,
            lat,
            watts,
            budget_wh=budget_wh,
            window_h=win,
            model_layer=model_layer,
            intensity_g_per_kwh=intensity,
            intensity_source=intensity_source,
            carbon_budget_g=carbon_budget_g,
        )
    elif energy_budget_wh is not None:
        report = solve_energy_budget(
            rps,
            lat,
            watts,
            budget_wh=energy_budget_wh,
            window_h=win,
            model_layer=model_layer,
            intensity_g_per_kwh=intensity,
            intensity_source=intensity_source,
        )
    else:
        report = solve_min_energy(
            rps,
            lat,
            watts,
            model_layer=model_layer,
            window_h=win,
            intensity_g_per_kwh=intensity,
            intensity_source=intensity_source,
        )

    log_security_event(
        "BUDGET_INVOCATION",
        {"direction": report.direction, "rps": rps, "region": region or "none"},
    )

    if json_out:
        typer.echo(json.dumps(_budget_json(report, commitment), indent=2))
        return
    _render_budget(report, commitment)


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
    carbon: bool = typer.Option(
        False,
        "--carbon",
        help=(
            "Add gCO₂/req and gCO₂/hour columns and a cheapest-greenest rank "
            "(needs --region; modelled from grid intensity)."
        ),
    ),
    replica_power_watts: float = typer.Option(
        DEFAULT_REPLICA_POWER_WATTS,
        "--replica-power-watts",
        help=(
            "Per-replica peak power in watts for the --carbon columns "
            f"(MVP placeholder ≈{DEFAULT_REPLICA_POWER_WATTS:.0f} W)."
        ),
        min=0.1,
        max=10000.0,
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
    _gate_commitment_with_energy(None)
    result = analyze(requests_per_second=rps, avg_latency_ms=lat, current_layer=current)

    # --- Carbon context (v0.22.0): modelled gCO₂ columns + cheapest-greenest
    #     rank. Requires an explicit --region (fail closed on an unknown one). ---
    carbon_ctx: dict | None = None
    if carbon:
        if region is None:
            err_console.print(
                "[bold red]--carbon requires --region[/] "
                "(carbon intensity is resolved per region)."
            )
            raise typer.Exit(code=2)
        try:
            watts_c = sanitize_bounded_number(
                replica_power_watts, "replica_power_watts", 0.1, 10000.0
            )
        except InputValidationError as exc:
            err_console.print(f"[bold red]Input validation error:[/] {exc}")
            raise typer.Exit(code=2) from exc
        from presidio_arch_translucency.carbon import (  # noqa: PLC0415
            CarbonError,
            grams_per_hour,
            grams_per_request,
            resolve_carbon_intensity,
        )

        try:
            intensity_c, source_c = resolve_carbon_intensity(region)
        except CarbonError as exc:
            # escape(): the message embeds the user-supplied --region, so
            # neutralise any Rich markup (e.g. "[blink]") in it — the region
            # renders as literal text, never as console styling.
            err_console.print(f"[bold red]Carbon error:[/] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc
        base_cap_c = base_capacity_rps(rps, lat, resolve_concurrency(None))
        grams_req: dict = {}
        grams_hr: dict = {}
        for lr in result.layers:
            le = layer_energy(
                lr.layer, lr.optimal_replicas, rps, base_cap_c, watts_c, None
            )
            grams_req[lr.layer] = (
                grams_per_request(le.joules_per_request, intensity_c)
                if le.joules_per_request is not None
                else None
            )
            grams_hr[lr.layer] = grams_per_hour(le.watts, intensity_c)
        carbon_ctx = {
            "grams_req": grams_req,
            "grams_hour": grams_hr,
            "intensity": intensity_c,
            "source": source_c,
        }

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
        _render_tiered_cost(tiered, result, carbon=carbon_ctx)
    else:
        cp = CostParams(
            cost_per_container_hour=cost_per_container_hour,
            cost_per_pod_hour=cost_per_pod_hour,
            cost_per_deployment_hour=cost_per_deployment_hour,
            cost_per_node_hour=cost_per_node_hour,
        )
        cost_results = build_cost_results(result.layers, cp)
        log_security_event("COST_INVOCATION", {"layer": layer_str, "rps": rps})
        _render_cost(cost_results, result, pricing_note=None, carbon=carbon_ctx)


@app.command("calibrate")
def calibrate_cmd(
    observations: Optional[list[str]] = typer.Option(  # noqa: B008, UP045
        None,
        "--observation",
        "-o",
        help=(
            "Measured operating point as 'rps:latency_ms:replicas' "
            "(e.g. 300:80:5). Repeat for multiple points; >=2 recommended. "
            "Omit when using --benchmark."
        ),
    ),
    energy_observations: Optional[list[str]] = typer.Option(  # noqa: B008, UP045
        None,
        "--energy-observation",
        help=(
            "Measured energy point as 'rps:latency_ms:replicas:watts' (total "
            "system watts, e.g. 300:80:5:420). Repeat; 2–3 unique points hold "
            "β_E at its default, while >=4 identifiable points fit β_E too. "
            "At least two replica counts are required. "
            "Fits P_idle/e_dyn/β_E into the SAME fit record — supply a "
            "throughput fit too (--observation or --benchmark)."
        ),
    ),
    energy_from_store: bool = typer.Option(
        False,
        "--energy-from-store",
        help=(
            "Fit the energy parameters from the chained measured-energy store "
            "(populated by `pat observe --energy`) instead of --energy-observation "
            "quads. Filtered to --layer when a named layer is given. Still needs a "
            "throughput fit (--observation/--benchmark). Mutually exclusive with "
            "--energy-observation."
        ),
    ),
    layer: str = typer.Option(
        DEFAULT_LAYER_NAME,
        "--layer",
        "-L",
        help=(
            "Service-layer label for this observation set (e.g. 'api', 'worker'). "
            "Fits per-layer parameters into model.json under layers.<name>; run "
            "again with a different --layer to add more. Default fits the global "
            "(pooled) parameters."
        ),
    ),
    show_global: bool = typer.Option(
        False,
        "--show-global",
        help="Also show the global (pooled) fit alongside a per-layer fit.",
    ),
    benchmark: bool = typer.Option(
        False,
        "--benchmark",
        help=(
            "Docker mode: sweep --replicas, measure throughput/latency at each "
            "count, and fit from the measured points instead of --observation. "
            "Requires a running Docker daemon."
        ),
    ),
    replicas: Optional[list[int]] = typer.Option(  # noqa: B008, UP045
        None,
        "--replicas",
        help=(
            "Replica counts to sweep in --benchmark mode (repeat the flag, "
            "e.g. --replicas 1 --replicas 2 --replicas 4). Default: 1 2 4."
        ),
    ),
    requests: int = typer.Option(
        40, "--requests", help="HTTP requests per replica count (--benchmark).", min=10
    ),
    concurrency: int = typer.Option(
        8, "--concurrency", help="Concurrent request threads (--benchmark).", min=1
    ),
    iterations: int = typer.Option(
        200_000,
        "--iterations",
        help="Monte Carlo iterations per request (--benchmark).",
    ),
) -> None:
    """
    Fit the translucency model to measured workload points.

    Analytical mode (default, no Docker): supply one or more observed
    `rps:latency_ms:replicas` triples from your APM, load tests, or prior
    `pat demo` output. Benchmark mode (`--benchmark`, Docker required): sweep
    `--replicas`, measure throughput/latency at each replica count on the local
    Docker daemon, and fit from those measurements.

    Either way the fitted per-replica capacity (concurrency) and coordination
    overhead are written to ~/.pat/model.json, after which `pat analyze` uses
    your calibrated parameters and stops warning. Tag the fit with --layer to
    write per-layer parameters; `pat analyze`, `what-if`, `slo`, and `optimize`
    then select them with their own --layer.

    \b
      pat calibrate --observation 100:50:2 --observation 300:80:5
      pat calibrate --layer api --observation 200:40:3 --observation 600:55:8
      pat calibrate --benchmark --layer container --replicas 1 --replicas 2 --replicas 4
    """
    from presidio_arch_translucency.calibrate import (  # noqa: PLC0415
        CalibrationError,
        energy_observations_from_store,
        fit_calibration,
        fit_energy_calibration,
        parse_energy_observation,
        parse_observation,
        write_model_file,
    )

    layer_name = layer.strip() or DEFAULT_LAYER_NAME
    is_named_layer = layer_name != DEFAULT_LAYER_NAME
    observations = observations or []
    energy_observations = energy_observations or []

    # Mutual exclusion (v0.21.0): the energy fit takes its inputs from EITHER
    # explicit --energy-observation quads OR the measured store, never both.
    if energy_observations and energy_from_store:
        err_console.print(
            "[bold red]Calibration error:[/] --energy-observation and "
            "--energy-from-store are mutually exclusive; pick one energy source."
        )
        raise typer.Exit(code=2)

    if benchmark:
        from presidio_arch_translucency.benchmark import (  # noqa: PLC0415
            BenchmarkError,
            parse_replica_sweep,
        )

        if observations:
            err_console.print(
                "[bold red]Calibration error:[/] --benchmark measures its own "
                "observations; drop --observation (or omit --benchmark)."
            )
            raise typer.Exit(code=2)
        try:
            sweep = parse_replica_sweep(replicas or [1, 2, 4])
        except BenchmarkError as exc:
            err_console.print(f"[bold red]Calibration error:[/] {exc}")
            raise typer.Exit(code=2) from exc
        try:
            result = _run_benchmark_calibration(
                sweep,
                requests=requests,
                concurrency=concurrency,
                iterations=iterations,
            )
        except BenchmarkError as exc:
            err_console.print(f"[bold red]Benchmark error:[/] {exc}")
            raise typer.Exit(code=1) from exc
        n_points = len(result.observations)
    else:
        if not observations:
            err_console.print(
                "[bold red]Calibration error:[/] supply at least one "
                "--observation, or use --benchmark to measure points with Docker."
            )
            raise typer.Exit(code=2)
        try:
            parsed = [parse_observation(raw) for raw in observations]
            result = fit_calibration(parsed)
        except CalibrationError as exc:
            err_console.print(f"[bold red]Calibration error:[/] {exc}")
            raise typer.Exit(code=2) from exc
        n_points = len(parsed)

    # Energy fit (v0.20.0): fitted into the SAME record as the throughput fit.
    # Energy inputs come from --energy-observation quads (v0.20) or, new in
    # v0.21.0, the chained measured-energy store (--energy-from-store).
    energy_result = None
    if energy_observations:
        try:
            energy_points = [
                parse_energy_observation(raw) for raw in energy_observations
            ]
            energy_result = fit_energy_calibration(energy_points)
        except CalibrationError as exc:
            err_console.print(f"[bold red]Energy calibration error:[/] {exc}")
            raise typer.Exit(code=2) from exc
    elif energy_from_store:
        from presidio_arch_translucency.observe import (  # noqa: PLC0415
            load_verified_energy_observations,
        )

        store_layer = layer_name if is_named_layer else None
        try:
            rows = load_verified_energy_observations(layer=store_layer)
        except ValueError as exc:
            err_console.print(f"[bold red]Energy calibration error:[/] {exc}")
            raise typer.Exit(code=2) from exc
        if not rows:
            scope = f" for layer {store_layer!r}" if store_layer else ""
            err_console.print(
                "[bold red]Energy calibration error:[/] no measured energy "
                f"observations in the store{scope}. Record some first with "
                "[bold]pat observe --prometheus URL --energy --energy-meter …[/]."
            )
            raise typer.Exit(code=2)
        try:
            energy_result = fit_energy_calibration(energy_observations_from_store(rows))
        except CalibrationError as exc:
            err_console.print(f"[bold red]Energy calibration error:[/] {exc}")
            raise typer.Exit(code=2) from exc

    path = write_model_file(
        result,
        layer=layer_name if is_named_layer else None,
        energy=energy_result,
    )
    log_security_event(
        "CALIBRATE_INVOCATION",
        {
            "observations": n_points,
            "energy_observations": len(energy_observations),
            "layer": layer_name,
            "mode": "benchmark" if benchmark else "analytical",
        },
    )

    global_record = None
    if show_global and is_named_layer:
        from presidio_arch_translucency.model import (  # noqa: PLC0415
            load_calibrated_model,
        )

        model = load_calibrated_model()
        if isinstance(model, dict) and "concurrency" in model:
            global_record = model

    _render_calibration(
        result,
        path,
        layer=layer_name if is_named_layer else None,
        global_record=global_record,
        energy=energy_result,
    )


observe_app = typer.Typer(
    name="observe",
    help=(
        "Record one workload observation, list recent ones (--list), or manage "
        "the background collection daemon (`pat observe daemon …`)."
    ),
    add_completion=False,
    invoke_without_command=True,
)
app.add_typer(observe_app, name="observe")


@observe_app.callback(invoke_without_command=True)
def observe_cmd(
    ctx: typer.Context,
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
    source: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--source",
        help=(
            "Measurement origin (manual/demo/prometheus/…). "
            "Defaults to 'manual' when recording; filters the list when given."
        ),
    ),
    prometheus: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--prometheus",
        help=(
            "Prometheus base URL (e.g. http://prometheus:9090). Scrapes one "
            "sample and records it (source='prometheus'). Needs --layer. "
            "Single-shot — schedule repeats via cron/launchd. "
            "Token from PAT_PROMETHEUS_TOKEN env only."
        ),
    ),
    list_recent: bool = typer.Option(
        False, "--list", help="List recent observations instead of recording one."
    ),
    limit: int = typer.Option(
        20, "--limit", help="Number of rows to show with --list.", min=1
    ),
    energy: bool = typer.Option(
        False,
        "--energy",
        help=(
            "Measured-energy mode: scrape one watt reading from a real power "
            "meter via --prometheus and record it to the chained energy store "
            "(needs --energy-meter and --layer). With --list, list energy "
            "observations instead of serving ones."
        ),
    ),
    energy_meter: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--energy-meter",
        help=(
            "Power meter to read (required with --energy). One of: "
            f"{', '.join(VALID_METERS)}. An explicit meter is a claim — there is "
            "no default; the analytic model is not a meter (ADR-0011 E1a)."
        ),
    ),
    energy_window_s: float = typer.Option(
        60.0,
        "--energy-window-s",
        help="Seconds the measured watts integrate over for the joules figure.",
        min=1.0,
        max=3600.0,
    ),
    energy_watts_query: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--energy-watts-query",
        help="Override the meter's default watts PromQL query (--energy).",
    ),
    energy_gate_query: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--energy-gate-query",
        help=(
            "Override the meter's default power-source gate PromQL query "
            "(--energy). The gate proves a real power interface exists (E1a)."
        ),
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
    recurring collection externally (cron / launchd / a Kubernetes CronJob), or
    let `pat observe daemon install` write the scheduler unit for you.
    The store is source-agnostic — supply numbers measured by any source (APM, a
    load test, prior `pat demo` output), or scrape one sample from Prometheus
    with --prometheus. `pat optimize` reads this store back.
    """
    # A subcommand (e.g. `daemon …`) handles its own logic; the callback only
    # runs the record/list flow when invoked bare (`pat observe …`).
    if ctx.invoked_subcommand is not None:
        return

    from presidio_arch_translucency import observe as store  # noqa: PLC0415

    # Measured-energy mode (v0.21.0) is a distinct additive surface; without
    # --energy the behaviour below is byte-identical to today.
    if energy:
        _observe_energy(
            prometheus=prometheus,
            layer=layer,
            meter=energy_meter,
            window_s=energy_window_s,
            watts_query=energy_watts_query,
            gate_query=energy_gate_query,
            list_recent=list_recent,
            limit=limit,
            db=db,
        )
        return

    if list_recent:
        layer_filter = layer.strip().lower() if layer else None
        rows = store.latest_observations(
            limit, db_path=db, layer=layer_filter, source=source
        )
        total = store.count_observations(db_path=db, layer=layer_filter, source=source)
        log_security_event("OBSERVE_LIST", {"rows": len(rows)})
        _render_observations(rows, total=total)
        return

    if prometheus is not None:
        _observe_from_prometheus(prometheus, layer, db)
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
            source=source or "manual",
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


def _observe_from_prometheus(url: str, layer: str | None, db: Path | None) -> None:
    """Scrape one sample from Prometheus and record it (single-shot)."""
    from presidio_arch_translucency import observe as store  # noqa: PLC0415
    from presidio_arch_translucency.prometheus import (  # noqa: PLC0415
        PrometheusError,
        fetch_observation,
    )

    if not layer:
        err_console.print(
            "[bold red]--prometheus requires --layer[/] "
            f"(one of: {', '.join(VALID_LAYERS)}).\n"
            "[dim]Prometheus does not know the replication layer — tag it.[/]"
        )
        raise typer.Exit(code=2)
    try:
        layer_str = sanitize_layer(layer, VALID_LAYERS)
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    try:
        obs = fetch_observation(url, layer_str)
        store.record_observation(obs, db_path=db)
    except (PrometheusError, store.ObservationError) as exc:
        err_console.print(f"[bold red]Prometheus collection failed:[/] {exc}")
        raise typer.Exit(code=2) from exc

    total = store.count_observations(db_path=db)
    log_security_event("OBSERVE_RECORD", {"layer": layer_str, "source": "prometheus"})
    console.print(
        f"[green]✓ Scraped[/] {obs.layer} observation from Prometheus "
        f"({obs.rps:.0f} req/s, p99 {obs.p99_latency_ms:.0f} ms, "
        f"{obs.replicas} replicas) → {total} total in store.\n"
    )


def _observe_energy(
    *,
    prometheus: str | None,
    layer: str | None,
    meter: str | None,
    window_s: float,
    watts_query: str | None,
    gate_query: str | None,
    list_recent: bool,
    limit: int,
    db: Path | None,
) -> None:
    """Measured-energy mode: list the energy store, or scrape+record one watt.

    Recording is single-shot and E1a-gated: the platform must prove a real power
    source before any watt is written (see
    :func:`prometheus.fetch_energy_observation`). On any refusal NOTHING is
    written and the command exits non-zero.
    """
    from presidio_arch_translucency import observe as store  # noqa: PLC0415

    # Validate the meter if supplied: a filter when listing, a claim when
    # recording. Never a default — an unmeasured meter cannot enter the store.
    meter_str: Optional[str] = None  # noqa: UP045
    if meter is not None:
        meter_str = meter.strip().lower()
        if meter_str not in VALID_METERS:
            err_console.print(
                f"[bold red]Invalid --energy-meter {meter!r}.[/] "
                f"One of: {', '.join(VALID_METERS)} (measured meters only)."
            )
            raise typer.Exit(code=2)

    layer_filter = layer.strip().lower() if layer else None

    if list_recent:
        rows = store.load_energy_observations(
            db_path=db, layer=layer_filter, meter=meter_str, limit=limit
        )
        total = store.count_energy_observations(
            db_path=db, layer=layer_filter, meter=meter_str
        )
        log_security_event("OBSERVE_ENERGY_LIST", {"rows": len(rows)})
        _render_energy_observations(rows, total=total)
        return

    # --- Record mode: a measured single-shot fetch from a real power meter. ---
    if prometheus is None:
        err_console.print(
            "[bold red]--energy records a measured watt and needs a source.[/] "
            "Supply --prometheus URL (the meter is scraped from Prometheus). "
            "Nothing was recorded."
        )
        raise typer.Exit(code=2)
    if meter_str is None:
        err_console.print(
            "[bold red]--energy requires --energy-meter[/] "
            f"(one of: {', '.join(VALID_METERS)}). An explicit meter is a claim — "
            "there is no default. Nothing was recorded."
        )
        raise typer.Exit(code=2)
    if not layer:
        err_console.print(
            "[bold red]--energy requires --layer[/] "
            f"(one of: {', '.join(VALID_LAYERS)}).\n"
            "[dim]Prometheus does not know the replication layer — tag it.[/]"
        )
        raise typer.Exit(code=2)
    try:
        layer_str = sanitize_layer(layer, VALID_LAYERS)
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    from presidio_arch_translucency.prometheus import (  # noqa: PLC0415
        PrometheusError,
        fetch_energy_observation,
    )

    try:
        eobs = fetch_energy_observation(
            prometheus,
            layer_str,
            meter_str,
            window_s=window_s,
            watts_query=watts_query,
            gate_query=gate_query,
        )
        store.record_energy_observation(eobs, db_path=db)
    except (PrometheusError, store.EnergyObservationError) as exc:
        err_console.print(f"[bold red]Measured-energy collection failed:[/] {exc}")
        raise typer.Exit(code=2) from exc

    total = store.count_energy_observations(db_path=db)
    log_security_event(
        "OBSERVE_ENERGY_RECORD",
        {"layer": layer_str, "meter": meter_str, "watts": round(eobs.watts, 3)},
    )
    # Override marking (P1-1): warn clearly when a query override forfeited the
    # pinned-metric attestation — the reading is permanently marked in the chain.
    if eobs.source == "prometheus-override":
        warn_console.print(
            "[yellow]⚠ query override active — reading recorded with "
            "source=prometheus-override; the pinned-metric attestation does not "
            "apply.[/]"
        )
    console.print(
        f"[green]✓ Measured[/] {eobs.layer} energy via [magenta]{eobs.meter}[/] "
        f"([cyan]{eobs.watts:.1f} W[/] over {eobs.window_s:.0f}s = "
        f"{eobs.joules:.0f} J, {eobs.replicas} replicas) → "
        f"chain seq {total - 1}, {total} total in energy store.\n"
    )


@observe_app.command("verify")
def observe_verify_cmd(
    db: Optional[Path] = typer.Option(  # noqa: UP045, B008
        None,
        "--db",
        help="Observation store to verify (default: ~/.pat/observations.db).",
    ),
    allow_legacy: bool = typer.Option(
        False,
        "--allow-legacy",
        help="Exit 0 when the chain is intact but legacy unchained rows remain.",
    ),
) -> None:
    """Verify BOTH hash chains (serving + measured energy) and report each.

    Walks the per-observation serving chain (v0.19.0) and the parallel
    measured-energy chain (v0.21.0, ADR-0011 §2), detecting any post-hoc edit,
    insertion, deletion, or reorder relative to each chain head. Serving rows
    recorded before chaining existed are an UNVERIFIABLE legacy prefix; the
    energy table is new in v0.21.0, so it has no legacy prefix by construction.

    Honest scope: a clean chain proves the local history was not rewritten after
    the fact; it does NOT prove the readings were honest when captured. Exits 0
    when both chains are intact and fully covered, 1 when EITHER is broken, and 2
    when a chain suffix is intact but coverage is incomplete. ``--allow-legacy``
    downgrades only the incomplete-coverage exit to 0; a broken chain still
    exits 1.
    """
    from presidio_arch_translucency import observe as store  # noqa: PLC0415

    serving, energy = store.verify_all_chains(db_path=db)
    log_security_event(
        "OBSERVE_VERIFY",
        {
            "serving_total": serving.total,
            "serving_verified": serving.verified,
            "serving_legacy": serving.legacy_count,
            "serving_ok": serving.ok,
            "energy_total": energy.total,
            "energy_verified": energy.verified,
            "energy_ok": energy.ok,
            "allow_legacy": allow_legacy,
        },
    )
    console.print("\n[bold blue]── Serving observation chain ──[/]")
    _render_chain_report(serving, label="Observation")
    console.print("[bold blue]── Measured energy chain ──[/]")
    _render_chain_report(energy, label="Energy observation")

    # A break in EITHER chain is exit 1 (ADR-0011 §2). Only then consider legacy.
    if serving.broken_obs_id is not None or energy.broken_obs_id is not None:
        raise typer.Exit(code=1)
    # Neither broken: incomplete coverage (only the serving chain can be legacy)
    # is exit 2 unless --allow-legacy downgrades it.
    incomplete = not serving.ok or not energy.ok
    if incomplete and not allow_legacy:
        raise typer.Exit(code=2)


def _render_chain_report(report: object, *, label: str = "Observation") -> None:
    """Render a ChainReport: coverage, legacy prefix, and the first break.

    ``label`` names the record kind so the serving and energy sections render
    with distinct, self-describing titles (ADR-0011 §2: "report rendering must
    keep the per-chain legacy prefixes distinguishable").
    """
    from presidio_arch_translucency.observe import ChainReport  # noqa: PLC0415

    assert isinstance(report, ChainReport)  # noqa: S101

    noun = label.lower()
    if report.total == 0:
        console.print(f"\n[dim]No {noun}s recorded yet — nothing to verify.[/]\n")
        return

    lines = [
        f"[bold]Records:[/]         {report.total}",
        f"[bold]Chained:[/]        {report.chained}",
        f"[bold]Verified:[/]       {report.verified}",
    ]
    if report.legacy_count:
        lines.append(
            f"[bold]Legacy (pre-chain):[/] [yellow]{report.legacy_count} "
            "UNVERIFIABLE[/]"
        )

    if report.broken_obs_id is not None:
        lines.append(
            f"\n[bold red]✗ Chain broken[/] at {noun} id "
            f"{report.broken_obs_id} (seq {report.broken_seq}):\n"
            f"  {report.break_reason}"
        )
        border = "red"
        title = f"[bold red]{label} chain — BROKEN[/]"
    elif report.legacy_count:
        lines.append(
            "\n[yellow]⚠ Chain intact over the chained suffix, but a legacy "
            "prefix predates chaining and cannot be verified.[/]\n"
            "[dim]The chain proves the chained history was not rewritten after "
            "the fact; it does not attest the readings were honest at "
            "capture.[/]"
        )
        border = "yellow"
        title = f"[bold yellow]{label} chain — PARTIAL (legacy prefix)[/]"
    else:
        lines.append(
            "\n[green]✓ Chain intact[/] — no post-hoc edit, insertion, "
            "deletion, or reorder detected.\n"
            "[dim]Proves the local history was not rewritten after the fact "
            "relative to the chain head; not that the readings were honest at "
            "capture.[/]"
        )
        border = "green"
        title = f"[bold blue]{label} chain — verified[/]"

    console.print()
    console.print(Panel("\n".join(lines), title=title, border_style=border))
    console.print()


# ── observe daemon: continuous collection via launchd / systemd ───────────────

daemon_app = typer.Typer(
    name="daemon",
    help=(
        "Run `pat observe` continuously via a launchd (macOS) or systemd (Linux) "
        "unit. Single-shot still applies — the scheduler fires observe on an "
        "interval; it does not become a long-running process."
    ),
    add_completion=False,
)
observe_app.add_typer(daemon_app, name="daemon")


@daemon_app.command("install")
def daemon_install_cmd(
    prometheus: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--prometheus",
        help="Prometheus base URL the scheduled `pat observe` should scrape.",
    ),
    layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        help=f"Replication layer to tag (required for --prometheus). "
        f"One of: {', '.join(VALID_LAYERS)}",
    ),
    interval: int = typer.Option(
        60,
        "--interval",
        help="Seconds between scheduled `pat observe` runs.",
        min=1,
    ),
) -> None:
    """Write the launchd plist (macOS) or systemd unit(s) (Linux)."""
    from presidio_arch_translucency.daemon import DaemonError, install  # noqa: PLC0415

    try:
        result = install(prometheus=prometheus, layer=layer, interval=interval)
    except DaemonError as exc:
        err_console.print(f"[bold red]Cannot install daemon:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "OBSERVE_DAEMON_INSTALL",
        {"platform": result.platform, "interval": interval},
    )
    console.print(
        f"[green]✓ Installed[/] pat observe daemon ({result.platform}, "
        f"every {interval}s):"
    )
    for path in result.paths:
        console.print(f"    {path}")
    if result.reload_hint:
        console.print(f"\n[dim]Activate it with:[/]\n    {result.reload_hint}\n")


@daemon_app.command("uninstall")
def daemon_uninstall_cmd() -> None:
    """Remove the daemon's launchd plist / systemd unit(s)."""
    from presidio_arch_translucency.daemon import (  # noqa: PLC0415
        DaemonError,
        uninstall,
    )

    try:
        removed = uninstall()
    except DaemonError as exc:
        err_console.print(f"[bold red]Cannot uninstall daemon:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event("OBSERVE_DAEMON_UNINSTALL", {"removed": len(removed)})
    if not removed:
        console.print("[dim]No daemon unit files found — nothing to remove.[/]\n")
        return
    console.print("[green]✓ Removed[/] pat observe daemon:")
    for path in removed:
        console.print(f"    {path}")
    console.print()


@daemon_app.command("status")
def daemon_status_cmd() -> None:
    """Show whether the daemon is installed and loaded/running."""
    from presidio_arch_translucency.daemon import DaemonError, status  # noqa: PLC0415

    try:
        result = status()
    except DaemonError as exc:
        err_console.print(f"[bold red]Cannot read daemon status:[/] {exc}")
        raise typer.Exit(code=2) from exc

    if not result.installed:
        console.print(
            "[yellow]pat observe daemon is not installed.[/] "
            "Install it with [bold]pat observe daemon install[/].\n"
        )
        return
    color = "green" if result.loaded else "yellow"
    console.print(
        f"pat observe daemon ({result.platform}): [{color}]{result.detail}[/]\n"
    )


@app.command("optimize")
def optimize_cmd(
    model: str = typer.Option(
        "sma",
        "--model",
        help="Prediction model: 'sma' (default) or 'arima' (auto-falls back to "
        "sma below 30 samples).",
    ),
    window: int = typer.Option(
        10, "--window", help="Number of most-recent samples to smooth (sma).", min=1
    ),
    horizon_minutes: float = typer.Option(
        10.0, "--horizon-minutes", help="How far ahead to project demand.", min=0.0
    ),
    max_p: int = typer.Option(
        3, "--max-p", help="Max AR order p in the ARIMA AIC grid.", min=0
    ),
    max_d: int = typer.Option(
        2,
        "--max-d",
        help="Max differencing order d in the ARIMA AIC grid (ignored with "
        "--auto-diff, which caps the chosen d at this value).",
        min=0,
    ),
    max_q: int = typer.Option(
        3, "--max-q", help="Max MA order q in the ARIMA AIC grid.", min=0
    ),
    auto_diff: bool = typer.Option(
        False,
        "--auto-diff",
        help="Auto-select the ARIMA differencing order d via a variance "
        "heuristic instead of sweeping d (faster; capped at --max-d).",
    ),
    layer: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--layer",
        "-c",
        help="Restrict to one replication layer (default: use all recorded).",
    ),
    emit_hpa_patch: bool = typer.Option(
        False,
        "--emit-hpa-patch",
        help="Emit an apply-able HPA manifest to stdout instead of the summary. "
        "Requires --target.",
    ),
    target: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--target",
        help="Deployment name for the HPA manifest (RFC 1123). Use with "
        "--emit-hpa-patch.",
    ),
    namespace: Optional[str] = typer.Option(  # noqa: UP045
        None, "--namespace", help="Namespace for the emitted HPA manifest."
    ),
    db: Optional[Path] = typer.Option(  # noqa: UP045, B008
        None, "--db", help="Override the store path (default: ~/.pat/observations.db)."
    ),
) -> None:
    """
    Proactive scaling recommendation from observed history (SMA or ARIMA).

    Reads the rolling observation store, projects demand a few minutes ahead, and
    recommends the replica count to serve it. `--model arima` fits a statsmodels
    ARIMA with a 95% confidence interval (and replica range), auto-falling back to
    SMA below 30 samples. Record samples first with `pat observe`.
    """
    from presidio_arch_translucency import observe as store  # noqa: PLC0415
    from presidio_arch_translucency.optimize import (  # noqa: PLC0415
        ARIMA_DEFAULT_HISTORY,
        OptimizeError,
        optimize_arima,
        optimize_sma,
    )

    model_name = model.lower()
    if model_name not in ("sma", "arima"):
        err_console.print(
            f"[bold red]Unsupported --model {model!r}.[/] Choose 'sma' or 'arima'."
        )
        raise typer.Exit(code=2)

    layer_filter: str | None = None
    if layer is not None:
        try:
            layer_filter = sanitize_layer(layer, VALID_LAYERS)
        except InputValidationError as exc:
            err_console.print(f"[bold red]Input validation error:[/] {exc}")
            raise typer.Exit(code=2) from exc

    _resolve_commitment_or_exit(layer_filter)

    # ARIMA wants as much history as it can get; SMA uses the smoothing window.
    fetch_n = max(window, ARIMA_DEFAULT_HISTORY) if model_name == "arima" else window
    rows = store.latest_observations(fetch_n, db_path=db, layer=layer_filter)
    if not rows:
        scope = f" for layer {layer_filter!r}" if layer_filter else ""
        console.print(
            f"\n[yellow]No observations{scope} yet.[/] "
            "Record some with [bold]pat observe[/] (or schedule collection via "
            "cron/launchd), then re-run.\n"
        )
        return

    # Use the calibrated per-replica capacity for the selected layer (if any),
    # falling back to the global fit then the model default.
    concurrency = resolve_concurrency(layer_filter)
    try:
        if model_name == "arima":
            result = optimize_arima(
                rows,
                horizon_minutes=horizon_minutes,
                concurrency=concurrency,
                max_p=max_p,
                max_d=max_d,
                max_q=max_q,
                auto_diff=auto_diff,
            )
        else:
            result = optimize_sma(
                rows, horizon_minutes=horizon_minutes, concurrency=concurrency
            )
    except OptimizeError as exc:  # pragma: no cover - guarded by the empty check
        err_console.print(f"[bold red]Cannot optimise:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if result.fallback_reason:
        warn_console.print(f"[yellow]⚠ {result.fallback_reason}[/]")

    log_security_event(
        "OPTIMIZE_INVOCATION", {"model": result.model, "samples": result.samples}
    )

    if emit_hpa_patch:
        _emit_hpa_patch(result, target, namespace)
        return

    _render_optimize(result)


def _emit_hpa_patch(result: object, target: str | None, namespace: str | None) -> None:
    """Emit a sanitised HPA manifest to stdout (for `kubectl apply`)."""
    from presidio_arch_translucency.hpa_patch import (  # noqa: PLC0415
        HpaPatchError,
        build_hpa_patch,
    )
    from presidio_arch_translucency.optimize import OptimizeResult  # noqa: PLC0415

    assert isinstance(result, OptimizeResult)  # noqa: S101

    if not target:
        err_console.print(
            "[bold red]--emit-hpa-patch requires --target[/] (the Deployment to scale)."
        )
        raise typer.Exit(code=2)

    # min = the point recommendation (pre-provision it); max = the ARIMA upper
    # CI bound when available, else the point estimate.
    min_replicas = result.recommended_replicas
    max_replicas = result.recommended_replicas_upper or result.recommended_replicas
    try:
        manifest = build_hpa_patch(
            target=target,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            namespace=namespace,
        )
    except HpaPatchError as exc:
        err_console.print(f"[bold red]Cannot emit HPA patch:[/] {exc}")
        raise typer.Exit(code=2) from exc

    # Plain stdout — clean YAML, no Rich styling/markup interference.
    typer.echo(manifest, nl=False)


# ── rendering helpers ─────────────────────────────────────────────────────────


def _render_optimize(result: object) -> None:
    """Render the proactive scaling recommendation (SMA or ARIMA)."""
    from presidio_arch_translucency.optimize import OptimizeResult  # local import

    assert isinstance(result, OptimizeResult)  # noqa: S101

    trend_color = "green" if result.trend_pct >= 0 else "red"
    action_label = {
        "scale-up": "[yellow]Scale up to[/]",
        "scale-down": "[cyan]Scale down to[/]",
        "hold": "[green]Hold at[/]",
    }[result.action]

    if result.model == "arima" and result.arima_order is not None:
        p, d, q = result.arima_order
        model_tag = f"ARIMA({p},{d},{q})"
    else:
        model_tag = "SMA"

    # Predicted line — add the 95% CI band when ARIMA provides one.
    predicted_line = (
        f"  Predicted   ~{result.predicted_rps:.0f} req/s "
        f"in ~{result.horizon_minutes:.0f} min"
    )
    if result.has_interval:
        predicted_line += (
            f"  [dim](95% CI {result.predicted_rps_lower:.0f}–"
            f"{result.predicted_rps_upper:.0f})[/]"
        )
    else:
        predicted_line += f"  [dim]({result.slope_rps_per_min:+.1f} req/s/min)[/]"

    # Recommend line — add the replica range under the CI when ARIMA provides one.
    recommend_line = (
        f"  Recommend   {action_label} [bold]{result.recommended_replicas}[/] "
        f"replicas ({result.layer})"
    )
    if (
        result.recommended_replicas_lower is not None
        and result.recommended_replicas_upper is not None
        and result.recommended_replicas_lower != result.recommended_replicas_upper
    ):
        recommend_line += (
            f"  [dim](range {result.recommended_replicas_lower}–"
            f"{result.recommended_replicas_upper})[/]"
        )

    span = result.window_minutes
    body = (
        f"[bold]Based on {result.samples} sample(s) ({model_tag}, "
        f"{result.layer}):[/]\n"
        f"  Window      {span:.0f} min  "
        f"({result.first_ts:%Y-%m-%d %H:%M} → {result.last_ts:%H:%M} UTC)\n"
        f"  Demand      {result.sma_rps:.0f} req/s smoothed  "
        f"([{trend_color}]{result.trend_pct:+.0f}%[/] over {span:.0f} min)\n"
        f"{predicted_line}\n"
        f"  Current     {result.current_replicas} replicas\n"
        f"{recommend_line}"
    )
    console.print()
    console.print(
        Panel(
            body,
            title=(
                "[bold blue]Presidio Architectural Translucency — "
                f"Optimize ({model_tag})[/]"
            ),
            border_style="blue",
        )
    )
    console.print()


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
    table.add_column("Source", style="magenta")

    for obs in rows:
        table.add_row(
            obs.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            obs.layer,
            f"{obs.rps:.0f}",
            f"{obs.avg_latency_ms:.0f}",
            f"{obs.p99_latency_ms:.0f}",
            f"{obs.throughput:.0f}",
            str(obs.replicas),
            obs.source,
        )

    console.print()
    console.print(table)
    console.print()


def _render_energy_observations(rows: list, total: int) -> None:
    """Render recent measured-energy observations from the chained store."""
    if not rows:
        console.print(
            "\n[dim]No measured energy observations yet. Record one with "
            "[bold]pat observe --prometheus URL --energy --energy-meter rapl "
            "--layer node[/].[/]\n"
        )
        return

    table = Table(
        title=f"Recent energy observations (showing {len(rows)} of {total})",
        box=box.ROUNDED,
        show_lines=False,
    )
    table.add_column("Timestamp (UTC)", style="dim", no_wrap=True)
    table.add_column("Layer", style="cyan")
    table.add_column("Meter", style="magenta")
    table.add_column("Watts", justify="right")
    table.add_column("Joules", justify="right")
    table.add_column("Window s", justify="right")
    table.add_column("req/s", justify="right")
    table.add_column("Replicas", justify="right")
    table.add_column("Source", style="dim")

    for eobs in rows:
        table.add_row(
            eobs.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            eobs.layer,
            eobs.meter,
            f"{eobs.watts:.1f}",
            f"{eobs.joules:.0f}",
            f"{eobs.window_s:.0f}",
            f"{eobs.rps:.0f}",
            str(eobs.replicas),
            eobs.source,
        )

    console.print()
    console.print(table)
    console.print()


def _render_benchmark_points(points: object) -> None:
    """Render the measured replica-sweep points before the fit."""
    from presidio_arch_translucency.benchmark import BenchmarkPoint  # noqa: PLC0415

    assert all(isinstance(p, BenchmarkPoint) for p in points)  # noqa: S101

    table = Table(
        title="Benchmark sweep — measured operating points",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Replicas", justify="right")
    table.add_column("Throughput\n(req/s)", justify="right")
    table.add_column("Avg latency\n(ms)", justify="right")
    table.add_column("p95 latency\n(ms)", justify="right")
    table.add_column("Errors", justify="right")
    for p in points:
        err_color = "green" if p.errors == 0 else "yellow"
        table.add_row(
            str(p.replicas),
            f"{p.throughput_rps:.1f}",
            f"{p.avg_latency_ms:.0f}",
            f"{p.p95_latency_ms:.0f}",
            f"[{err_color}]{p.errors}[/]",
        )
    console.print()
    console.print(table)


def _run_benchmark_calibration(
    sweep: list[int],
    *,
    requests: int,
    concurrency: int,
    iterations: int,
) -> object:
    """Run the Docker replica sweep, show the points, and fit the model."""
    from presidio_arch_translucency.benchmark import (  # noqa: PLC0415
        points_to_observations,
        run_benchmark_sweep,
    )
    from presidio_arch_translucency.calibrate import fit_calibration  # noqa: PLC0415

    points = run_benchmark_sweep(
        sweep,
        requests=requests,
        concurrency=concurrency,
        iterations=iterations,
        console=console,
    )
    _render_benchmark_points(points)
    return fit_calibration(points_to_observations(points))


def _render_calibration(
    result: object,
    path: Path,
    layer: Optional[str] = None,  # noqa: UP045
    global_record: Optional[dict] = None,  # noqa: UP045
    energy: object = None,
) -> None:
    """Render fitted parameters, per-point predictions, and fit quality."""
    from presidio_arch_translucency.calibrate import (  # local import
        CalibrationResult,
    )

    assert isinstance(result, CalibrationResult)  # noqa: S101

    table_title = "Calibration fit — observed vs predicted"
    if layer is not None:
        table_title = f"Calibration fit ({layer}) — observed vs predicted"

    table = Table(
        title=table_title,
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Observed rps", justify="right")
    table.add_column("Latency (ms)", justify="right")
    table.add_column("Replicas", justify="right")
    table.add_column("Predicted rps", justify="right")
    table.add_column("Residual", justify="right")

    for obs, pred, resid in zip(
        result.observations, result.predictions, result.residuals, strict=True
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
    if layer is not None:
        written_line = (
            f"Layer {layer!r} written to {path} (layers.{layer})\n"
            f"`pat analyze --layer {layer}` will now use these parameters."
        )
    else:
        written_line = (
            f"Written to {path}\n"
            "`pat analyze` will now use these calibrated parameters."
        )
    body = (
        f"[bold]Concurrency (κ):[/]   [cyan]{result.concurrency:.3f}[/] "
        "req/replica in-flight\n"
        f"[bold]Overhead β:[/]        [cyan]{result.overhead_beta:.4f}[/]\n"
        f"[bold]R²:[/]                [{r2_color}]{result.r_squared:.4f}[/]\n"
        f"[bold]RMSE:[/]              {result.rmse:.4f} req/s\n\n"
        f"[dim]{written_line}[/]"
    )

    panel_title = "[bold blue]Presidio Architectural Translucency — Calibration[/]"
    if layer is not None:
        panel_title = (
            "[bold blue]Presidio Architectural Translucency — "
            f"Calibration (layer: {layer})[/]"
        )

    console.print()
    console.print(
        Panel(
            body,
            title=panel_title,
            border_style="blue",
        )
    )
    console.print()
    console.print(table)

    if global_record is not None:
        g_conc = global_record.get("concurrency")
        g_beta = global_record.get("overhead_beta")
        if g_conc is not None:
            beta_str = f"{g_beta:.4f}" if isinstance(g_beta, (int, float)) else "n/a"
            console.print(
                f"\n[dim]Global (pooled) fit:[/] κ={float(g_conc):.3f}, β={beta_str}"
            )

    if energy is not None:
        _render_energy_calibration(energy)

    console.print()


def _render_energy_calibration(energy: object) -> None:
    """Render the fitted energy parameters, per-point predictions, and quality.

    Same table style as the throughput fit — the energy fit is written into the
    same model record, so it is shown right beneath it.
    """
    from presidio_arch_translucency.calibrate import (  # local import
        EnergyCalibrationResult,
    )

    assert isinstance(energy, EnergyCalibrationResult)  # noqa: S101

    e_r2_color = "green" if energy.r_squared >= 0.95 else "yellow"
    e_dyn = energy.energy_dyn_j_per_req
    body = (
        f"[bold]Idle power P_idle:[/]  [cyan]{energy.energy_idle_w:.2f}[/] W/replica\n"
        f"[bold]Dyn J/req e_dyn:[/]    [cyan]{e_dyn:.4f}[/] J/req\n"
        f"[bold]Energy β_E:[/]         [cyan]{energy.energy_beta:.4f}[/]\n"
        f"[bold]R²:[/]                 [{e_r2_color}]{energy.r_squared:.4f}[/]\n"
        f"[bold]RMSE:[/]               {energy.rmse:.4f} W\n\n"
        "[dim]Fitted from measured watt readings; `pat analyze` will use these "
        "for the energy columns instead of the MVP defaults.[/]"
    )
    console.print()
    console.print(
        Panel(
            body,
            title="[bold blue]Presidio Architectural Translucency — Energy fit[/]",
            border_style="blue",
        )
    )

    table = Table(
        title="Energy fit — observed vs predicted watts",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Observed W", justify="right")
    table.add_column("rps", justify="right")
    table.add_column("Replicas", justify="right")
    table.add_column("Predicted W", justify="right")
    table.add_column("Residual", justify="right")

    for obs, pred, resid in zip(
        energy.observations, energy.predictions, energy.residuals, strict=True
    ):
        resid_color = "green" if abs(resid) < 0.01 * max(obs.watts, 1.0) else "yellow"
        table.add_row(
            f"{obs.watts:.1f}",
            f"{obs.rps:.1f}",
            str(obs.replicas),
            f"{pred:.1f}",
            f"[{resid_color}]{resid:+.2f}[/]",
        )

    console.print()
    console.print(table)


def _render_tiered_cost(
    tiered: object,
    result: object,
    carbon: Optional[dict] = None,  # noqa: UP045
) -> None:
    """Render on-demand + optional reserved/spot pricing tiers."""
    from presidio_arch_translucency.cloud import TieredPricingResult  # local import

    assert isinstance(tiered, TieredPricingResult)  # noqa: S101

    od = tiered.on_demand
    cache_tag = " [dim](cached)[/dim]" if od.from_cache else ""
    _render_cost(
        build_cost_results(result.layers, od.params),  # type: ignore[union-attr]
        result,
        pricing_note=od.source_description + cache_tag,
        carbon=carbon,
    )

    if tiered.reserved_1yr is not None:
        r1 = tiered.reserved_1yr
        cache_tag = " [dim](cached)[/dim]" if r1.from_cache else ""
        console.print("\n[bold blue]1-Year Reserved Pricing[/]")
        _render_cost(
            build_cost_results(result.layers, r1.params),  # type: ignore[union-attr]
            result,
            pricing_note=r1.source_description + cache_tag,
            carbon=carbon,
        )

    if tiered.reserved_3yr is not None:
        r3 = tiered.reserved_3yr
        cache_tag = " [dim](cached)[/dim]" if r3.from_cache else ""
        console.print("\n[bold blue]3-Year Reserved Pricing[/]")
        _render_cost(
            build_cost_results(result.layers, r3.params),  # type: ignore[union-attr]
            result,
            pricing_note=r3.source_description + cache_tag,
            carbon=carbon,
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
            carbon=carbon,
        )


def _render_cost(
    cost_results: list,  # type: ignore[type-arg]
    result: object,
    pricing_note: Optional[str] = None,  # noqa: UP045
    carbon: Optional[dict] = None,  # noqa: UP045
) -> None:
    from presidio_arch_translucency.cost import build_carbon_ranking  # noqa: PLC0415
    from presidio_arch_translucency.model import AnalysisResult  # local import

    assert isinstance(result, AnalysisResult)  # noqa: S101

    carbon_by_layer: dict = {}
    if carbon is not None:
        for cres in build_carbon_ranking(
            cost_results, carbon["grams_req"], carbon["grams_hour"]
        ):
            carbon_by_layer[cres.layer] = cres

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
    if carbon is not None:
        table.add_column("gCO₂/req", justify="right")
        table.add_column("gCO₂/hr", justify="right")
        table.add_column("Greenest+Cheapest", justify="center")
    table.add_column("Best ROI", justify="center")

    for cr in cost_results:
        tp_color = "green" if cr.throughput_gain_pct >= 0 else "red"
        rt_color = "red" if cr.response_time_change_pct > 10 else "green"
        is_rec = cr.is_recommended
        is_cur = cr.layer == result.current_layer

        layer_label = cr.layer.value
        if is_cur:
            layer_label += " (current)"

        row: list[str] = [
            layer_label,
            str(cr.replicas),
            f"{cr.throughput_rps:.0f}",
            f"[{tp_color}]{cr.throughput_gain_pct:+.1f}%[/]",
            f"[{rt_color}]{cr.response_time_change_pct:+.1f}%[/]",
            f"${cr.hourly_cost_usd:.4f}",
            format_cost_per_request(cr.cost_per_request_usd),
            f"{cr.roi_score:.1f}",
        ]
        if carbon is not None:
            cres = carbon_by_layer.get(cr.layer)
            g_req = cres.grams_per_request if cres is not None else None
            g_hr = cres.grams_per_hour if cres is not None else None
            win = cres.is_greenest_cheapest if cres is not None else False
            row.append(_fmt_grams(g_req))
            row.append(_fmt_grams(g_hr))
            row.append("[bold green]✓[/]" if win else "")
        row.append("[bold green]✓[/]" if is_rec else "")
        table.add_row(*row)

    console.print()
    table_console = console
    if carbon is not None and not console.is_terminal:
        # The carbon columns widen the table past the 80-column default Rich
        # assumes for captured/non-TTY output; widen only that case.
        table_console = Console(width=200)
    table_console.print(table)
    pricing_line = f"\n[dim]Pricing source: {pricing_note}[/]" if pricing_note else ""
    carbon_line = ""
    if carbon is not None:
        from presidio_arch_translucency.carbon import static_annotation  # noqa: PLC0415

        ann = static_annotation(carbon["source"])
        carbon_line = (
            f"\n[dim]Grid intensity: {carbon['intensity']:.0f} gCO₂eq/kWh {ann}"
            "  ·  cheapest-greenest = lowest mean of min-max-normalised "
            "cost/req and gCO₂/req (modelled)[/]"
        )
    console.print(
        f"\n[dim]Baseline: {result.baseline_throughput_rps:.0f} req/s "
        f"@ {result.baseline_response_time_ms:.1f} ms  "
        f"(current layer: {result.current_layer.value})[/]"
        + pricing_line
        + carbon_line
        + "\n[dim]ROI score = throughput-gain-% / cost-per-request  "
        "(higher = better performance-per-dollar)[/]\n"
    )


def _build_energy_aware(
    result: ScaleEventResult,
    layer: ReplicationLayer,
    watts: float,
    base_cap: float,
    model_layer: Optional[str],  # noqa: UP045
    elec_cost: float,
    cost_per_req: Optional[float],  # noqa: UP045
    region: Optional[str],  # noqa: UP045
) -> dict:
    """Standing-energy-vs-trough figures for the what-if --energy-aware section.

    Standing energy = per-replica idle power × warm minReplicas (the pre-spike
    floor) over the simulated window; trough side reuses ``trough_cost_usd``.
    All modelled — never chained or signed (E1a).
    """
    idle_w, _dyn, _beta, source = resolve_energy_params(
        layer, watts, base_cap, model_layer
    )
    min_replicas = max(result.replicas_before, 0)
    window_s = result.timeline[-1].t_s if result.timeline else result.trough_duration_s
    window_h = window_s / 3600.0
    standing_watts = idle_w * min_replicas
    standing_wh = standing_watts * window_h
    standing_usd = standing_wh / 1000.0 * elec_cost
    trough_usd = (
        trough_cost_usd(result.missed_requests, cost_per_req)
        if cost_per_req is not None
        else None
    )

    intensity: float | None = None
    intensity_source: str | None = None
    standing_grams: float | None = None
    if region is not None:
        from presidio_arch_translucency.carbon import (  # noqa: PLC0415
            CarbonError,
            grams_per_hour,
            resolve_carbon_intensity,
        )

        try:
            intensity, intensity_source = resolve_carbon_intensity(region)
        except CarbonError as exc:
            # escape(): the message embeds the user-supplied --region, so
            # neutralise any Rich markup (e.g. "[blink]") in it — the region
            # renders as literal text, never as console styling.
            err_console.print(f"[bold red]Carbon error:[/] {escape(str(exc))}")
            raise typer.Exit(code=2) from exc
        standing_grams = grams_per_hour(standing_watts, intensity) * window_h

    return {
        "min_replicas": min_replicas,
        "idle_w_per_replica": idle_w,
        "standing_watts": standing_watts,
        "window_s": window_s,
        "standing_wh": standing_wh,
        "standing_usd": standing_usd,
        "trough_usd": trough_usd,
        "source": source,
        "intensity_g_per_kwh": intensity,
        "intensity_source": intensity_source,
        "standing_grams": standing_grams,
    }


def _render_energy_aware(data: dict) -> None:
    """Render the idle-energy-vs-trough section + verdict (the FLIP)."""
    from presidio_arch_translucency.carbon import static_annotation  # noqa: PLC0415

    standing_usd = data["standing_usd"]
    trough_usd = data["trough_usd"]

    lines = [
        "[bold]STANDING ENERGY[/]  (warm minReplicas over the simulated window)",
        f"  minReplicas   {data['min_replicas']}",
        f"  Idle power    {_fmt_watts(data['idle_w_per_replica'])} W/replica "
        f"→ {_fmt_watts(data['standing_watts'])} W",
        f"  Window        {data['window_s']:.0f} s",
        f"  Standing E    {_fmt_wh(data['standing_wh'])} Wh  (~${standing_usd:,.4f})",
    ]
    if data["intensity_g_per_kwh"] is not None:
        ann = static_annotation(data["intensity_source"] or "static")
        lines.append(
            f"  Standing CO₂  {_fmt_grams(data['standing_grams'])} gCO₂eq "
            f"[dim]@ {data['intensity_g_per_kwh']:.0f} gCO₂eq/kWh {ann}[/]"
        )

    lines.append("")
    lines.append("[bold]TROUGH[/]  (revenue lost while pods spin up)")
    if trough_usd is None:
        lines.append(
            "  [yellow]Add --cost-per-request to compare against standing energy.[/]"
        )
    else:
        lines.append(f"  Trough cost   ~${trough_usd:,.4f} revenue impact")

    lines.append("")
    if trough_usd is None:
        verdict = "[dim]Verdict needs --cost-per-request to weigh the two sides.[/]"
    elif standing_usd > trough_usd:
        verdict = (
            f"[bold yellow]Warm replicas cost more in standing energy "
            f"(${standing_usd:,.4f}) than the trough loses in revenue "
            f"(${trough_usd:,.4f}).[/]\n"
            "Scaling minReplicas down (or to zero) is the greener, cheaper choice."
        )
    else:
        verdict = (
            f"[bold green]The trough loses more in revenue (${trough_usd:,.4f}) "
            f"than warm replicas cost in standing energy "
            f"(${standing_usd:,.4f}).[/]\n"
            "Keeping minReplicas warm is justified."
        )
    lines.append(verdict)
    lines.append(f"\n{_energy_note(data['source'])}")

    console.print()
    console.print(
        Panel(
            "\n".join(lines),
            title="[bold blue]Idle energy vs trough[/]",
            border_style="blue",
        )
    )
    console.print()


def _render_what_if(
    r: ScaleEventResult,
    cost_per_req: Optional[float] = None,  # noqa: UP045
    energy: Optional[LayerEnergy] = None,  # noqa: UP045
    energy_aware: Optional[dict] = None,  # noqa: UP045
) -> None:
    spike_x = r.rps_spike / max(r.rps_baseline, 0.01)
    trough_color = "red" if r.trough_throughput_pct < 80 else "yellow"
    steady_ok = r.steady_throughput_rps >= r.rps_spike * 0.98

    trough_cost_line = ""
    if cost_per_req is not None:
        tc = trough_cost_usd(r.missed_requests, cost_per_req)
        trough_cost_line = f"\n  Trough cost   ~${tc:,.2f} revenue impact"

    energy_line = ""
    if energy is not None:
        energy_line = (
            f"\n  Est. power    {_fmt_watts(energy.watts)} W "
            f"(J/req {_fmt_jreq(energy.joules_per_request)})"
        )

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
        f"  p99 latency   {r.steady_p99_latency_ms:,.0f} ms" + energy_line
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

    if energy_aware is not None:
        _render_energy_aware(energy_aware)


def _render_slo(
    results: dict[ReplicationLayer, ScaleEventResult],
    p99_target: float,
    rps: float,
    spike_rps: float,
    params: ScaleEventParams,
    cost_params: Optional[CostParams] = None,  # noqa: UP045
    jreq_by_layer: Optional[dict] = None,  # noqa: UP045
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
    table.add_column("J/req", justify="right")
    table.add_column("SLO verdict", justify="left")

    best_layer = min(results.values(), key=lambda r: r.steady_p99_latency_ms)
    cp = cost_params if cost_params is not None else CostParams()
    jreq_map = jreq_by_layer or {}

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
            _fmt_jreq(jreq_map.get(r.layer)),
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


def _budget_carbon_caption(report: object) -> str:
    """Grid-intensity provenance caption for a budget render (or '')."""
    intensity = getattr(report, "intensity_g_per_kwh", None)
    if intensity is None:
        return ""
    from presidio_arch_translucency.carbon import static_annotation  # noqa: PLC0415

    ann = static_annotation(getattr(report, "intensity_source", None) or "static")
    return f"\n[dim]Grid intensity: {intensity:.0f} gCO₂eq/kWh {ann}[/]"


def _budget_json(report: object, commitment: Optional[dict]) -> dict:  # noqa: UP045
    """Additive JSON view of a :class:`budget.BudgetReport`."""
    layers = [
        {
            "layer": r.layer.value,
            "replicas": r.replicas,
            "throughput_rps": r.throughput_rps,
            "energy_wh": r.energy_wh,
            "joules_per_request": r.joules_per_request,
            "eei": r.eei,
            "headroom_wh": r.headroom_wh,
            "feasible": r.feasible,
            "energy_source": r.source,
            "grams_per_request": r.grams_per_request,
            "grams_per_window": r.grams_per_window,
        }
        for r in report.layers  # type: ignore[attr-defined]
    ]
    out: dict = {
        "direction": report.direction,  # type: ignore[attr-defined]
        "window_h": report.window_h,  # type: ignore[attr-defined]
        "budget_wh": report.budget_wh,  # type: ignore[attr-defined]
        "carbon_budget_g": report.carbon_budget_g,  # type: ignore[attr-defined]
        "recommended_layer": (
            report.recommended.value  # type: ignore[attr-defined]
            if report.recommended is not None  # type: ignore[attr-defined]
            else None
        ),
        "layers": layers,
        "commitment": commitment,
    }
    if report.intensity_g_per_kwh is not None:  # type: ignore[attr-defined]
        out["intensity_g_per_kwh"] = report.intensity_g_per_kwh  # type: ignore[attr-defined]
        out["intensity_source"] = report.intensity_source  # type: ignore[attr-defined]
    return out


def _render_budget(report: object, commitment: Optional[dict]) -> None:  # noqa: UP045
    """Render a :class:`budget.BudgetReport` (house table + recommendation panel)."""
    from presidio_arch_translucency.budget import BudgetReport  # noqa: PLC0415

    assert isinstance(report, BudgetReport)  # noqa: S101

    is_dir1 = report.direction == "max-output"
    has_carbon = report.intensity_g_per_kwh is not None
    src = report.layers[0].source if report.layers else "default"

    console.print()
    if is_dir1:
        header = (
            f"[bold]Energy budget:[/] {_fmt_wh(report.budget_wh or 0.0)} Wh "
            f"over {report.window_h:g} h"
        )
        if report.carbon_budget_g is not None:
            header += (
                f"  [dim](from {_fmt_grams(report.carbon_budget_g)} gCO₂eq @ "
                f"{report.intensity_g_per_kwh:.0f} gCO₂eq/kWh)[/]"
            )
        header += "\n[dim]Direction 1 — maximise throughput within the budget[/]"
    else:
        header = (
            "[bold]Minimum energy meeting demand[/]  "
            f"(window {report.window_h:g} h)\n"
            "[dim]Direction 2 — least watt-hours that saturate the workload[/]"
        )
    console.print(header)

    table = Table(box=box.ROUNDED, show_lines=True)
    table.add_column("Layer", style="cyan", no_wrap=True)
    table.add_column("Replicas", justify="right")
    table.add_column("Throughput (req/s)", justify="right")
    table.add_column("Energy (Wh)", justify="right")
    table.add_column("J/req", justify="right")
    if is_dir1:
        table.add_column("Headroom (Wh)", justify="right")
    else:
        table.add_column("EEI", justify="right")
    if has_carbon:
        table.add_column("gCO₂/req", justify="right")
        table.add_column("gCO₂/win", justify="right")
    table.add_column("Verdict", justify="left")

    for r in report.layers:
        is_rec = report.recommended is not None and r.layer == report.recommended
        row: list[str] = [
            r.layer.value,
            str(r.replicas),
            f"{r.throughput_rps:.0f}",
            _fmt_wh(r.energy_wh),
            _fmt_jreq(r.joules_per_request),
        ]
        if is_dir1:
            row.append(_fmt_wh(r.headroom_wh) if r.headroom_wh is not None else "—")
        else:
            row.append(_fmt_eei(r.eei))
        if has_carbon:
            row.append(_fmt_grams(r.grams_per_request))
            row.append(_fmt_grams(r.grams_per_window))
        if not r.feasible:
            verdict = "[red]infeasible ✗[/]"
        elif is_rec:
            verdict = "[bold green]✓ recommended[/]"
        else:
            verdict = "[green]feasible[/]" if is_dir1 else ""
        row.append(verdict)
        table.add_row(*row)

    console.print()
    table_console = console if console.is_terminal else Console(width=170)
    table_console.print(table)

    # --- Recommendation panel ---
    rec = None
    if report.recommended is not None:
        rec = next(r for r in report.layers if r.layer == report.recommended)

    if rec is None:
        if is_dir1:
            body = (
                "[bold red]No layer fits the budget.[/]\n"
                "Even a single replica exceeds the energy budget at every layer.\n"
                "Raise --energy-budget-wh / --carbon-budget-g, shorten "
                "--window-h, or reduce --replica-power-watts."
            )
        else:
            body = (
                "[bold red]No layer can meet the requested demand.[/]\n"
                "Every layer reaches its maximum replica count before saturating "
                "the workload. Reduce demand, increase per-replica capacity via "
                "calibration, or reconsider the architecture."
            )
    elif is_dir1:
        body = (
            f"[bold]Recommended layer:[/]   [cyan]{rec.layer.value}[/]\n"
            f"[bold]Feasible replicas:[/]   [cyan]{rec.replicas}[/]\n"
            f"[bold]Achieved throughput:[/] {rec.throughput_rps:.0f} req/s\n"
            f"[bold]Energy used:[/]         {_fmt_wh(rec.energy_wh)} Wh "
            f"(headroom {_fmt_wh(rec.headroom_wh or 0.0)} Wh)\n"
            f"[bold]J/req:[/]               {_fmt_jreq(rec.joules_per_request)}"
        )
        if has_carbon:
            body += (
                f"\n[bold]gCO₂/req:[/]            "
                f"{_fmt_grams(rec.grams_per_request)}  ·  "
                f"gCO₂/window {_fmt_grams(rec.grams_per_window)}"
            )
    else:
        body = (
            f"[bold]Recommended layer:[/]   [cyan]{rec.layer.value}[/] "
            "(least energy for the demand)\n"
            f"[bold]Replicas:[/]            [cyan]{rec.replicas}[/]\n"
            f"[bold]Throughput:[/]          {rec.throughput_rps:.0f} req/s\n"
            f"[bold]Energy:[/]              {_fmt_wh(rec.energy_wh)} Wh"
            f"  ·  J/req {_fmt_jreq(rec.joules_per_request)}"
            f"  ·  EEI {_fmt_eei(rec.eei)}"
        )
        if has_carbon:
            body += (
                f"\n[bold]gCO₂/req:[/]            "
                f"{_fmt_grams(rec.grams_per_request)}  ·  "
                f"gCO₂/window {_fmt_grams(rec.grams_per_window)}"
            )
    body += f"\n{_energy_note(src)}"

    console.print()
    console.print(
        Panel(
            body,
            title="[bold blue]Presidio Architectural Translucency — Energy Budget[/]",
            border_style="blue",
        )
    )
    caption = _budget_carbon_caption(report)
    if caption:
        console.print(caption)
    console.print(_commitment_line(commitment) + "\n")


@app.command("evidence-emit")
def evidence_emit_cmd(
    p99_target_ms: float = typer.Option(
        ...,
        "--p99-target-ms",
        help="SLO target: maximum acceptable p99 latency in milliseconds.",
        min=1.0,
    ),
    p99_latency_ms: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--p99-latency-ms",
        help="Observed p99 latency (ms). Omit to read the latest stored observation.",
        min=0.0,
    ),
    window: str = typer.Option("5m", "--window", help="Observation window label."),
    layer: Optional[str] = typer.Option(  # noqa: UP045
        None, "--layer", help="Filter the stored observation by replication layer."
    ),
    db: Optional[Path] = typer.Option(  # noqa: UP045, B008
        None, "--db", help="Observation store path (defaults to the standard location)."
    ),
    always: bool = typer.Option(
        False,
        "--always",
        help="Emit even when not degraded (default: only when p99 > target).",
    ),
) -> None:
    """Emit a key-less Layer-0 SLO degradation reading as JSON.

    arch-translucency holds **no signing key**: this prints an *unsigned* reading to
    stdout. Pipe it to the signing-bridge sidecar, which adds the Ed25519 signature
    that downstream family consumers verify fail-closed before acting. By
    default a reading is emitted only when the observed p99 breaches the target
    (cron/daemon-friendly); pass ``--always`` to emit regardless.
    """
    from presidio_arch_translucency.evidence_producer import (  # noqa: PLC0415
        build_layer0_reading,
        is_degraded,
    )

    try:
        target = sanitize_latency_ms(p99_target_ms)
        if p99_latency_ms is None:
            from presidio_arch_translucency.observe import (  # noqa: PLC0415
                latest_observations,
            )

            obs_layer = sanitize_layer(layer, VALID_LAYERS) if layer else None
            recent = latest_observations(1, db_path=db, layer=obs_layer)
            if not recent:
                err_console.print(
                    "[bold red]No observation found[/] — pass --p99-latency-ms or "
                    "record one with `pat observe`."
                )
                raise typer.Exit(code=1)
            observed = recent[-1].p99_latency_ms
        else:
            observed = sanitize_latency_ms(p99_latency_ms)
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=1) from exc

    reading = build_layer0_reading(
        slo="p99_latency_ms",
        value=round(observed),
        threshold=round(target),
        window=window,
    )
    degraded = is_degraded(reading)
    log_security_event("EVIDENCE_EMIT", {"slo": "p99_latency_ms", "degraded": degraded})

    if not degraded and not always:
        # Not degraded → nothing to authorize. Stay silent on stdout.
        raise typer.Exit(code=0)

    typer.echo(json.dumps(reading, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Training domain (MVP) — ML training parallelism analysis + evidence
# ---------------------------------------------------------------------------


def _samples_per_second_per_watt(r, watts_per_device: float) -> float | None:  # noqa: ANN001
    """samples/s/W = tp / (δ × watts_per_device); None when no feasible degree."""
    if not r.feasible or not r.optimal_degree or watts_per_device <= 0.0:
        return None
    return r.estimated_samples_per_second / (r.optimal_degree * watts_per_device)


def _render_training_results(
    result,  # noqa: ANN001
    show_all: bool,
    energy_info: Optional[dict] = None,  # noqa: UP045
) -> None:
    """Render a TrainingAnalysisResult as a Rich table + recommendation panel.

    When *energy_info* is supplied (fitted per-strategy power, or an explicit
    ``--device-power-watts`` placeholder) an additive ``Samples/s/W`` column is
    shown; the energy-best *feasible* strategy is marked only when every
    feasible strategy has comparable power data. Energy never changes the
    recommendation and never excludes a strategy (memory stays the only hard
    constraint). Without energy data the table is byte-identical to before.
    """
    by_strategy: dict = (energy_info or {}).get("by_strategy", {})
    energy_best: str | None = (energy_info or {}).get("best")

    table = Table(
        title="Training parallelism analysis (architectural translucency)",
        box=box.SIMPLE_HEAVY,
    )
    table.add_column("Strategy", style="bold")
    table.add_column("Degree δ", justify="right")
    table.add_column("Samples/s", justify="right")
    table.add_column("Scaling eff %", justify="right")
    table.add_column("Mem/device GB", justify="right")
    table.add_column("Feasible", justify="center")
    table.add_column("Gain %", justify="right")
    if energy_info is not None:
        table.add_column("Samples/s/W", justify="right")
        table.add_column("Energy", justify="center")

    for r in result.strategies:
        if not show_all and not r.feasible:
            continue
        marker = "✓" if r.feasible else "[red]✗[/]"
        highlight = (
            r.feasible
            and result.recommended_strategy is not None
            and r.strategy == result.recommended_strategy
        )
        style = "bold green" if highlight else None
        row = [
            r.strategy.value,
            str(r.optimal_degree) if r.optimal_degree else "—",
            f"{r.estimated_samples_per_second:,.2f}",
            f"{r.scaling_efficiency_pct:.1f}",
            f"{r.per_device_memory_gb:.2f}",
            marker,
            f"{r.throughput_gain_pct:+.1f}",
        ]
        if energy_info is not None:
            wpd = by_strategy.get(r.strategy.value)
            spw = (
                _samples_per_second_per_watt(r, wpd)
                if isinstance(wpd, (int, float))
                else None
            )
            row.append(f"{spw:,.4f}" if spw is not None else "—")
            row.append("[yellow]⚡ best[/]" if r.strategy.value == energy_best else "")
        table.add_row(*row, style=style)

    console.print(table)
    if energy_info is not None and not energy_info.get("complete", False):
        console.print(
            "[dim]Energy ranking incomplete: samples/s/W is shown only where "
            "power is calibrated; no cross-strategy energy-best claim is made.[/]"
        )

    if result.recommended_strategy is None:
        console.print(
            Panel(
                "[bold red]No feasible parallelism configuration[/] within the given "
                f"device count ({result.device_count}) and device memory. "
                "The model state does not fit even when sharded — add devices, "
                "use devices with more memory, or reduce model/optimizer state.",
                title="Recommendation",
            )
        )
        return

    best = next(
        r for r in result.strategies if r.strategy == result.recommended_strategy
    )
    console.print(
        Panel(
            f"Replicate at the [bold green]{best.strategy.value}[/] parallelism "
            f"layer with degree [bold]δ = {best.optimal_degree}[/] "
            f"→ ≈ [bold]{best.estimated_samples_per_second:,.2f}[/] samples/s "
            f"({best.throughput_gain_pct:+.1f}% vs single device, "
            f"{best.scaling_efficiency_pct:.1f}% scaling efficiency, "
            f"{best.per_device_memory_gb:.2f} GB model state per device).",
            title="Recommendation",
        )
    )


def _build_training_energy_info(
    result,  # noqa: ANN001
    device_power_watts: Optional[float],  # noqa: UP045
):
    """Assemble the ``samples/s/W`` energy overlay for a training analysis.

    Returns ``None`` when neither a fitted per-strategy power figure nor an
    explicit ``--device-power-watts`` placeholder is available (so the table
    stays byte-identical). Otherwise returns ``{"by_strategy": {strategy: wpd},
    "best": strategy|None}`` where ``wpd`` is the effective per-device watts (the
    explicit flag overrides the fitted figure). ``best`` is set only when every
    feasible strategy has comparable power data; otherwise the overlay is
    marked incomplete and no energy-best claim is made. Fails closed on a
    tampered training record via ``resolve_training_energy``.
    """
    from presidio_arch_translucency.training import (  # noqa: PLC0415
        ParallelismStrategy,
        resolve_training_energy,
    )

    by_strategy: dict = {}
    for r in result.strategies:
        wpd: float | None = None
        if device_power_watts is not None:
            wpd = device_power_watts
        else:
            fitted = resolve_training_energy(ParallelismStrategy(r.strategy.value))
            if fitted is not None:
                wpd = fitted["watts_per_device"]
        if wpd is not None:
            by_strategy[r.strategy.value] = wpd

    if not by_strategy:
        return None

    feasible = [r for r in result.strategies if r.feasible and r.optimal_degree]
    complete = all(r.strategy.value in by_strategy for r in feasible)
    best_strategy: str | None = None
    best_spw = -1.0
    for r in feasible if complete else []:
        wpd = by_strategy.get(r.strategy.value)
        if wpd is None:
            continue
        spw = _samples_per_second_per_watt(r, wpd)
        if spw is not None and spw > best_spw:
            best_spw = spw
            best_strategy = r.strategy.value
    return {"by_strategy": by_strategy, "best": best_strategy, "complete": complete}


def _training_commitment_line(statuses: list[tuple[str, dict]]) -> str:
    """One dim line summarising per-strategy training-calibration commitments.

    *statuses* is a list of ``(strategy, {"status", "digest"})`` for strategies
    that carry a training record (``uncalibrated`` ones are omitted). ``ok``
    shows the digest prefix; ``legacy`` flags an uncommitted hand-written fit.
    """
    if not statuses:
        return (
            "[dim]Training calibration: n/a (uncalibrated — default overhead "
            "parameters)[/]"
        )
    parts = []
    for strategy, status in statuses:
        kind = status.get("status")
        if kind == "ok":
            digest = (status.get("digest") or "")[:16]
            parts.append(f"{strategy} [green]{digest}…[/]")
        elif kind == "legacy":
            parts.append(f"{strategy} [yellow]legacy[/]")
    return "[dim]Training calibration commitments: " + ", ".join(parts) + "[/]"


def _resolve_training_commitments_or_exit(strategies) -> list[tuple[str, dict]]:  # noqa: ANN001
    """Resolve each strategy's training commitment, failing closed on tamper.

    A present-but-mismatched training commitment raises
    ``TrainingCalibrationTamperError`` → exit 2 (same pattern as the serving
    commitment gate). ``uncalibrated`` strategies are dropped from the summary.
    """
    from presidio_arch_translucency.training import (  # noqa: PLC0415
        ParallelismStrategy,
        TrainingCalibrationTamperError,
        resolve_training_commitment,
    )

    collected: list[tuple[str, dict]] = []
    for strategy in strategies:
        try:
            status = resolve_training_commitment(ParallelismStrategy(strategy))
        except TrainingCalibrationTamperError as exc:
            err_console.print(f"[bold red]Training calibration tamper:[/] {exc}")
            raise typer.Exit(code=2) from exc
        if status["status"] != "uncalibrated":
            collected.append((strategy, status))
    return collected


def _render_training_calibration(strategy: str, result, path: Path) -> None:  # noqa: ANN001
    """Render a fitted training calibration: params, fit quality, energy, per-run."""
    table = Table(
        title=f"Training calibration fit ({strategy}) — observed vs predicted",
        box=box.ROUNDED,
        show_lines=True,
    )
    table.add_column("Degree δ", justify="right")
    table.add_column("Observed samples/s", justify="right")
    table.add_column("Predicted samples/s", justify="right")
    table.add_column("Duration (s)", justify="right")
    table.add_column("Power (W)", justify="right")
    for run, pred, resid in zip(
        result.runs, result.predictions, result.residuals, strict=True
    ):
        resid_color = (
            "green"
            if abs(resid) < 0.01 * max(run.samples_per_second, 1.0)
            else "yellow"
        )
        power = f"{run.mean_power_w:,.1f}" if run.mean_power_w is not None else "—"
        table.add_row(
            str(run.degree),
            f"[{resid_color}]{run.samples_per_second:,.2f}[/]",
            f"{pred:,.2f}",
            f"{run.duration_s:,.1f}",
            power,
        )

    saturated = bool(getattr(result, "saturated", False))
    r2_color = "dim" if saturated else "green" if result.r_squared >= 0.95 else "yellow"
    r2_note = (
        "  [dim](exactly determined fit — R²/RMSE uninformative at this run "
        "count; add runs at more degrees to test the model)[/]"
        if saturated
        else ""
    )
    energy_lines = ""
    if result.mean_power_w is not None:
        energy_lines = (
            f"[bold]Mean power:[/]         [cyan]{result.mean_power_w:,.2f}[/] W\n"
            f"[bold]Watts/device:[/]       [cyan]{result.watts_per_device:,.2f}[/] "
            "W/device\n"
        )
    body = (
        f"[bold]Baseline:[/]           [cyan]{result.baseline_samples_per_second:,.3f}"
        "[/] samples/s (δ=1)\n"
        f"[bold]Overhead α:[/]         [cyan]{result.overhead_alpha:.4f}[/]\n"
        f"[bold]Overhead β:[/]         [cyan]{result.overhead_beta:.4f}[/]\n"
        f"{energy_lines}"
        f"[bold]R²:[/]                 [{r2_color}]{result.r_squared:.4f}[/]{r2_note}\n"
        f"[bold]RMSE:[/]               {result.rmse:,.4f} samples/s\n\n"
        f"[dim]Committed and written to {path} (training.{strategy}).\n"
        f"`pat train-analyze` / `train-what-if` now use these calibrated "
        "overhead parameters.[/]"
    )
    console.print()
    console.print(
        Panel(
            body,
            title=(
                "[bold blue]Presidio Architectural Translucency — "
                f"Training calibration ({strategy})[/]"
            ),
            border_style="blue",
        )
    )
    console.print()
    console.print(table)
    console.print()


def _parse_run_spec(spec: str, max_degree: int) -> tuple[int, str]:
    """Parse a ``--run degree:path`` spec into ``(degree, path)`` (fail-closed)."""
    if ":" not in spec:
        raise InputValidationError(
            f"--run {spec!r} must be 'degree:path' (e.g. 4:logs/ddp4.jsonl)"
        )
    degree_str, _, path = spec.partition(":")
    try:
        degree = int(degree_str)
    except ValueError as exc:
        raise InputValidationError(
            f"--run {spec!r}: degree must be an integer, got {degree_str!r}"
        ) from exc
    if not (1 <= degree <= max_degree):
        raise InputValidationError(
            f"--run {spec!r}: degree {degree} out of range (1..{max_degree})"
        )
    if not path.strip():
        raise InputValidationError(f"--run {spec!r}: empty log path")
    return degree, path


def _training_calibration_json(strategy: str, result, path: Path) -> dict:  # noqa: ANN001
    payload: dict = {
        "strategy": strategy,
        "baseline_samples_per_second": result.baseline_samples_per_second,
        "overhead_alpha": result.overhead_alpha,
        "overhead_beta": result.overhead_beta,
        "microbatches": result.microbatches,
        "r_squared": result.r_squared,
        "rmse": result.rmse,
        "runs": [
            {
                "degree": run.degree,
                "samples_per_second": run.samples_per_second,
                "duration_s": run.duration_s,
                "mean_power_w": run.mean_power_w,
            }
            for run in result.runs
        ],
        "model_path": str(path),
    }
    if result.mean_power_w is not None:
        payload["mean_power_w"] = result.mean_power_w
        payload["watts_per_device"] = result.watts_per_device
    return payload


@app.command("train-calibrate")
def train_calibrate_cmd(
    strategy: str = typer.Option(
        ...,
        "--strategy",
        help="Parallelism strategy to calibrate. One of: data, fsdp, tensor, pipeline.",
    ),
    runs: list[str] = typer.Option(  # noqa: B008
        ...,
        "--run",
        help=(
            "A run as 'degree:path' — the parallelism degree and the JSON-Lines "
            "step log recorded at that degree (repeat the flag). Every fit requires "
            "a degree-1 anchor. α/β strategies "
            "need >=3 distinct degrees for the full fit (exactly 2 hold β at the "
            "default); pipeline needs >=2."
        ),
    ),
    microbatches: Optional[int] = typer.Option(  # noqa: UP045
        None,
        "--microbatches",
        help="Pipeline microbatches m (bubble model). Default: 8.",
        min=1,
        max=4096,
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the fitted calibration as JSON instead of a table."
    ),
) -> None:
    """Fit training-overhead parameters from recorded step-time logs (L-TR-1).

    Parses one JSON-Lines step log per run (see the step-log contract in
    `train_calibrate.py`), fits ``tp(δ) = baseline · δ · eff(δ)`` for the
    strategy, and writes a committed record into `~/.pat/model.json` under
    `training.<strategy>`. Energy figures are aggregated when every run's log
    carries `power_w`. Fail-closed on malformed logs or too-few distinct degrees.

    \b
      pat train-calibrate --strategy data \\
          --run 1:logs/ddp1.jsonl --run 4:logs/ddp4.jsonl
    """
    from presidio_arch_translucency.train_calibrate import (  # noqa: PLC0415
        StepLogError,
        TrainingCalibrationError,
        fit_training_calibration,
        parse_step_log,
        write_training_fit,
    )
    from presidio_arch_translucency.training import (  # noqa: PLC0415
        DEFAULT_MICROBATCHES,
        STRATEGY_PARAMS,
        VALID_STRATEGIES,
        ParallelismStrategy,
    )

    try:
        strategy_str = sanitize_layer(strategy, VALID_STRATEGIES)
        strategy_enum = ParallelismStrategy(strategy_str)
        max_degree = STRATEGY_PARAMS[strategy_enum].max_degree
        parsed_runs = []
        for spec in runs:
            degree, path = _parse_run_spec(spec, max_degree)
            parsed_runs.append((degree, parse_step_log(path)))
        result = fit_training_calibration(
            strategy_enum,
            parsed_runs,
            microbatches=microbatches or DEFAULT_MICROBATCHES,
        )
        path = write_training_fit(strategy_enum, result)
    except (InputValidationError, StepLogError, TrainingCalibrationError) as exc:
        err_console.print(f"[bold red]Training calibration error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "TRAIN_CALIBRATE_INVOCATION",
        {
            "strategy": strategy_str,
            "runs": len(parsed_runs),
            "has_energy": result.mean_power_w is not None,
        },
    )
    if as_json:
        typer.echo(
            json.dumps(
                _training_calibration_json(strategy_str, result, path),
                separators=(",", ":"),
            )
        )
        return
    _render_training_calibration(strategy_str, result, path)


@app.command("train-analyze")
def train_analyze_cmd(
    samples_per_second: float = typer.Option(
        ...,
        "--samples-per-second",
        "-s",
        help="Measured single-device training throughput (samples/second).",
        min=0.000001,
    ),
    model_memory_gb: float = typer.Option(
        ...,
        "--model-memory-gb",
        "-m",
        help="Model state size (parameters + gradients + optimizer state) in GB.",
        min=0.001,
    ),
    device_memory_gb: float = typer.Option(
        ...,
        "--device-memory-gb",
        "-d",
        help="Memory per accelerator device in GB.",
        min=0.001,
    ),
    devices: int = typer.Option(
        ...,
        "--devices",
        "-n",
        help="Number of accelerator devices available.",
        min=1,
    ),
    microbatches: Optional[int] = typer.Option(  # noqa: UP045
        None,
        "--microbatches",
        help="Pipeline microbatches m (bubble = (δ-1)/(m+δ-1)). Default: 8.",
        min=1,
    ),
    show_all: bool = typer.Option(
        False,
        "--show-all",
        help="Also list strategies with no feasible degree.",
    ),
    device_power_watts: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--device-power-watts",
        help=(
            "Per-device board power in watts (MVP placeholder, 1–2000). When "
            "given — or when a committed training fit carries fitted power — a "
            "samples/s/W column is shown; an energy-best feasible strategy is "
            "marked only when all feasible strategies have comparable power. "
            "Energy never changes the recommendation or excludes a "
            "strategy."
        ),
        min=1.0,
        max=2000.0,
    ),
) -> None:
    """
    Analyze a training workload and recommend the parallelism strategy.

    Training-domain counterpart of `pat analyze`: applies the architectural
    translucency model to data / fsdp / tensor / pipeline parallelism, with
    per-device memory as a hard feasibility constraint (not a soft penalty).
    """
    from presidio_arch_translucency.training import (  # noqa: PLC0415
        DEFAULT_MICROBATCHES,
        ORDERED_STRATEGIES,
        TrainingCalibrationTamperError,
        TrainingDomainError,
        analyze_training,
    )

    try:
        sps = sanitize_bounded_number(
            samples_per_second, "samples_per_second", 1e-6, 1e9
        )
        model_mem = sanitize_bounded_number(
            model_memory_gb, "model_memory_gb", 1e-3, 1e6
        )
        device_mem = sanitize_bounded_number(
            device_memory_gb, "device_memory_gb", 1e-3, 1e6
        )
        power = (
            sanitize_bounded_number(
                device_power_watts, "device_power_watts", 1.0, 2000.0
            )
            if device_power_watts is not None
            else None
        )
        result = analyze_training(
            baseline_samples_per_second=sps,
            model_memory_gb=model_mem,
            device_memory_gb=device_mem,
            device_count=devices,
            microbatches=microbatches or DEFAULT_MICROBATCHES,
        )
    except (InputValidationError, TrainingDomainError) as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except TrainingCalibrationTamperError as exc:
        err_console.print(f"[bold red]Training calibration tamper:[/] {exc}")
        raise typer.Exit(code=2) from exc
    log_recommendation(
        layer=(
            result.recommended_strategy.value
            if result.recommended_strategy is not None
            else "none-feasible"
        ),
        replicas=result.recommended_degree,
        throughput_gain_pct=next(
            (
                r.throughput_gain_pct
                for r in result.strategies
                if r.strategy == result.recommended_strategy
            ),
            0.0,
        ),
    )
    try:
        energy_info = _build_training_energy_info(result, power)
    except TrainingCalibrationTamperError as exc:
        err_console.print(f"[bold red]Training calibration tamper:[/] {exc}")
        raise typer.Exit(code=2) from exc
    _render_training_results(result, show_all=show_all, energy_info=energy_info)

    statuses = _resolve_training_commitments_or_exit(
        [s.value for s in ORDERED_STRATEGIES]
    )
    if statuses:
        console.print(_training_commitment_line(statuses))


@app.command("train-what-if")
def train_what_if_cmd(
    strategy: str = typer.Option(
        ...,
        "--strategy",
        help="Parallelism strategy. One of: data, fsdp, tensor, pipeline.",
    ),
    degree: int = typer.Option(
        ...,
        "--degree",
        help="Parallelism degree δ (number of devices for the strategy).",
        min=1,
    ),
    samples_per_second: float = typer.Option(
        ...,
        "--samples-per-second",
        "-s",
        help="Measured single-device training throughput (samples/second).",
        min=0.000001,
    ),
    model_memory_gb: float = typer.Option(
        ...,
        "--model-memory-gb",
        "-m",
        help="Model state size in GB.",
        min=0.001,
    ),
    device_memory_gb: float = typer.Option(
        ...,
        "--device-memory-gb",
        "-d",
        help="Memory per accelerator device in GB.",
        min=0.001,
    ),
    microbatches: Optional[int] = typer.Option(  # noqa: UP045
        None,
        "--microbatches",
        help="Pipeline microbatches m. Default: 8.",
        min=1,
    ),
    device_power_watts: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--device-power-watts",
        help=(
            "Per-device board power in watts (MVP placeholder, 1–2000). When "
            "given — or when a committed training fit carries fitted power — a "
            "samples/s/W figure is shown. Energy is informational only."
        ),
        min=1.0,
        max=2000.0,
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Emit the result as JSON instead of a table."
    ),
) -> None:
    """Evaluate one specific (strategy, degree) training configuration.

    Fail-closed: a degree beyond the strategy's `max_degree` is out-of-domain
    and rejected (exit 2), never reported as feasible.
    """
    from presidio_arch_translucency.training import (  # noqa: PLC0415
        DEFAULT_MICROBATCHES,
        VALID_STRATEGIES,
        ParallelismStrategy,
        TrainingCalibrationTamperError,
        TrainingDomainError,
        evaluate_strategy,
        resolve_training_commitment,
        resolve_training_energy,
    )

    try:
        strategy_str = sanitize_layer(strategy, VALID_STRATEGIES)
        sps = sanitize_bounded_number(
            samples_per_second, "samples_per_second", 1e-6, 1e9
        )
        model_mem = sanitize_bounded_number(
            model_memory_gb, "model_memory_gb", 1e-3, 1e6
        )
        device_mem = sanitize_bounded_number(
            device_memory_gb, "device_memory_gb", 1e-3, 1e6
        )
        power = (
            sanitize_bounded_number(
                device_power_watts, "device_power_watts", 1.0, 2000.0
            )
            if device_power_watts is not None
            else None
        )
        strategy_enum = ParallelismStrategy(strategy_str)
        r = evaluate_strategy(
            strategy_enum,
            degree,
            baseline_samples_per_second=sps,
            model_memory_gb=model_mem,
            device_memory_gb=device_mem,
            microbatches=microbatches or DEFAULT_MICROBATCHES,
        )
    except (InputValidationError, TrainingDomainError) as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except TrainingCalibrationTamperError as exc:
        # evaluate_strategy resolves the (possibly committed) training params;
        # a tampered training record fails closed here too (exit 2).
        err_console.print(f"[bold red]Training calibration tamper:[/] {exc}")
        raise typer.Exit(code=2) from exc

    # Optional energy overlay: explicit --device-power-watts overrides a
    # committed fitted watts/device. Fails closed on a tampered training record.
    try:
        watts_per_device: float | None = power
        if watts_per_device is None:
            fitted = resolve_training_energy(strategy_enum)
            if fitted is not None:
                watts_per_device = fitted["watts_per_device"]
        commitment = resolve_training_commitment(strategy_enum)
    except TrainingCalibrationTamperError as exc:
        err_console.print(f"[bold red]Training calibration tamper:[/] {exc}")
        raise typer.Exit(code=2) from exc
    spw = (
        _samples_per_second_per_watt(r, watts_per_device)
        if watts_per_device is not None
        else None
    )

    if as_json:
        payload = {
            "strategy": r.strategy.value,
            "degree": r.optimal_degree,
            "estimated_samples_per_second": r.estimated_samples_per_second,
            "scaling_efficiency_pct": r.scaling_efficiency_pct,
            "per_device_memory_gb": r.per_device_memory_gb,
            "feasible": r.feasible,
            "throughput_gain_pct": r.throughput_gain_pct,
        }
        if spw is not None:
            payload["samples_per_second_per_watt"] = round(spw, 6)
        typer.echo(json.dumps(payload, separators=(",", ":")))
        return
    feasibility = "[green]feasible[/]" if r.feasible else "[bold red]INFEASIBLE[/]"
    energy_line = ""
    if spw is not None:
        energy_line = f"\n[dim]Energy: ≈ {spw:,.4f} samples/s/W (informational).[/]"
    console.print(
        Panel(
            f"[bold]{r.strategy.value}[/] at δ = {r.optimal_degree}: "
            f"≈ {r.estimated_samples_per_second:,.2f} samples/s "
            f"({r.throughput_gain_pct:+.1f}%, "
            f"{r.scaling_efficiency_pct:.1f}% scaling efficiency), "
            f"{r.per_device_memory_gb:.2f} GB/device — {feasibility}.{energy_line}",
            title="What-if (training)",
        )
    )
    if commitment["status"] != "uncalibrated":
        console.print(_training_commitment_line([(strategy_str, commitment)]))


@app.command("train-evidence-emit")
def train_evidence_emit_cmd(
    run_id: str = typer.Option(
        ..., "--run-id", help="Stable identifier of the training run."
    ),
    strategy: str = typer.Option(
        ...,
        "--strategy",
        help="Parallelism strategy used. One of: data, fsdp, tensor, pipeline.",
    ),
    degree: int = typer.Option(
        ..., "--degree", help="Parallelism degree δ used.", min=1
    ),
    samples_per_second: float = typer.Option(
        ...,
        "--samples-per-second",
        "-s",
        help="Achieved training throughput (samples/second; rounded to int).",
        min=0.0,
    ),
    duration_s: int = typer.Option(
        ..., "--duration-s", help="Wall-clock run duration in seconds.", min=0
    ),
    devices: int = typer.Option(
        ..., "--devices", "-n", help="Number of accelerator devices used.", min=1
    ),
    parent: list[str] = typer.Option(  # noqa: B008
        [],
        "--parent",
        help=(
            "Content hash of an upstream evidence payload (repeatable) — e.g. "
            "the eai-classification or gate-decision that authorized this run. "
            "Attested inside the signed content (provenance DAG convention)."
        ),
    ),
    model_hash: Optional[str] = typer.Option(  # noqa: UP045
        None, "--model-hash", help="Content hash of the trained model artifact."
    ),
    dataset_hash: Optional[str] = typer.Option(  # noqa: UP045
        None, "--dataset-hash", help="Content hash of the training dataset."
    ),
    energy_wh: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--energy-wh",
        help=(
            'Run energy in watt-hours as an int or decimal STRING (e.g. "12.5"). '
            "A producer's measured claim / modelled estimate (Energy Arc E1a), "
            "not an observation-chain reading. Floats rejected on the wire — pass "
            "a decimal string. Included in the attested content only when given."
        ),
    ),
    mean_power_w: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--mean-power-w",
        help=(
            'Mean total run power in watts as an int or decimal STRING (e.g. "420.0"). '
            "Same producer-claim discipline as --energy-wh; independently optional. "
            "When both are supplied they must agree with --duration-s."
        ),
    ),
) -> None:
    """Emit a key-less Layer-0 training-run record as JSON.

    arch-translucency holds **no signing key**: this prints an *unsigned*
    ``training-run@1`` record to stdout. Pipe it to the signing-bridge sidecar,
    which adds the Ed25519 signature. ``--parent`` hashes chain the run to the
    upstream evidence that authorized it (classification, gate decision),
    forming a verifiable provenance DAG across the suite. This can support
    broader operator documentation but is not standalone compliance evidence.

    Optional ``--energy-wh`` / ``--mean-power-w`` add producer-attributed energy
    figures to the attested content (additive; absent → byte-identical to a
    pre-v0.23 record). They are string-passed to the library, which rejects
    floats and non-round-trip decimals on the wire. When both are present, the
    library rejects a contradiction with the stated run duration.
    """
    from presidio_arch_translucency.evidence_producer import (  # noqa: PLC0415
        EvidenceProducerError,
        build_training_run_reading,
    )
    from presidio_arch_translucency.training import (  # noqa: PLC0415
        VALID_STRATEGIES,
    )

    try:
        strategy_str = sanitize_layer(strategy, VALID_STRATEGIES)
        sps = sanitize_bounded_number(
            samples_per_second, "samples_per_second", 0.0, 1e9
        )
        reading = build_training_run_reading(
            run_id=run_id,
            strategy=strategy_str,
            degree=degree,
            samples_per_second=round(sps),
            duration_s=duration_s,
            device_count=devices,
            parents=tuple(parent),
            model_hash=model_hash,
            dataset_hash=dataset_hash,
            energy_wh=energy_wh,
            mean_power_w=mean_power_w,
        )
    except (InputValidationError, EvidenceProducerError) as exc:
        err_console.print(f"[bold red]Evidence error:[/] {exc}")
        raise typer.Exit(code=1) from exc

    # Log a digest, not the raw run_id (audit finding: raw user-supplied
    # strings must not reach the security log).
    run_id_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]
    log_security_event(
        "TRAINING_EVIDENCE_EMIT",
        {
            "run_id_sha256_16": run_id_digest,
            "strategy": strategy_str,
            "parents": len(parent),
        },
    )
    typer.echo(json.dumps(reading, separators=(",", ":")))


if __name__ == "__main__":
    app()
