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
    DEFAULT_LAYER_NAME,
    REFERENCE_LATENCY_RANGE_MS,
    REFERENCE_RPS_RANGE,
    VALID_LAYERS,
    CalibrationTamperError,
    ReplicationLayer,
    analyze,
    model_is_calibrated,
    resolve_calibration_commitment,
    resolve_concurrency,
)
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

    # --- Calibration-commitment gate (v0.19.0): fail closed if the model
    #     file's stored parameters no longer match their commitment. ---
    commitment = _resolve_commitment_or_exit(model_layer)

    # --- Run analysis ---
    result = analyze(
        requests_per_second=rps,
        avg_latency_ms=lat,
        current_layer=current,
        layer=model_layer,
    )

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
    except InputValidationError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    current = ReplicationLayer(layer_str)

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
        metrics = build_metrics(
            rps,
            lat,
            current,
            layer=model_layer,
            cost_per_replica_hour=cost_per_replica_hour,
        )
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

    try:
        groups = build_rule_groups(
            current_layer=layer_str,
            cost_budget=cost_budget,
            surge_ratio=surge_ratio,
            trend_threshold=trend_threshold,
            for_duration=for_duration,
        )
    except RuleError as exc:
        err_console.print(f"[bold red]Rules error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "RULES_EMIT",
        {
            "layer": layer_str or "none",
            "cost_alert": cost_budget is not None,
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
) -> None:
    """
    Emit autoscaler config that scales a Deployment to track pat's forecast.

    Closes the loop: the exporter publishes pat_predicted_recommended_replicas
    (run `pat export --predict`, scraped into Prometheus); this emits a KEDA
    ScaledObject (default) or a Prometheus-Adapter HPA that scales --target to
    match it. Emit-only — prints YAML to stdout; `pat` never applies or scales
    anything.

    \b
      pat scaler -t web --prometheus-url http://prom:9090 -c container
      pat scaler -t web --prometheus-url http://prom:9090 --format prometheus-adapter
    """
    from presidio_arch_translucency.scaler import (  # noqa: PLC0415
        DEFAULT_METRIC,
        VALID_FORMATS,
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

    try:
        effective_query = query if query else default_query(DEFAULT_METRIC, layer)
        yaml = build_scaler(
            fmt,
            target,
            prometheus_url,
            effective_query,
            metric=DEFAULT_METRIC,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            namespace=namespace,
            name=name,
        )
    except ScalerError as exc:
        err_console.print(f"[bold red]Scaler error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "SCALER_EMIT", {"format": fmt, "target": target, "layer": layer or "all"}
    )
    typer.echo(yaml, nl=False)


def _render_results(
    result: AnalysisResult,  # type: ignore[name-defined]  # noqa: F821
    show_all: bool,
    uniform_cost: Optional[float] = None,  # noqa: UP045
    commitment: Optional[dict] = None,  # noqa: UP045
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
        concurrency=resolve_concurrency(model_layer),
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
        fit_calibration,
        parse_observation,
        write_model_file,
    )

    layer_name = layer.strip() or DEFAULT_LAYER_NAME
    is_named_layer = layer_name != DEFAULT_LAYER_NAME
    observations = observations or []

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

    path = write_model_file(result, layer=layer_name if is_named_layer else None)
    log_security_event(
        "CALIBRATE_INVOCATION",
        {
            "observations": n_points,
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
    """Verify the observation hash chain and report the first break, if any.

    Walks the per-observation hash chain (v0.19.0) and detects any post-hoc
    edit, insertion, deletion, or reorder of chained history relative to the
    chain head. Rows recorded before chaining existed carry no chain link and
    are reported as an UNVERIFIABLE legacy prefix — never counted as verified.

    Honest scope: a clean chain proves the local history was not rewritten after
    the fact; it does NOT prove the readings were honest when captured. Exits 0
    when the chain is intact and fully covered, 1 when a break is found, and 2
    when the chain suffix is intact but coverage is incomplete. ``--allow-legacy``
    downgrades only the incomplete-coverage exit to 0; a broken chain still exits 1.
    """
    from presidio_arch_translucency import observe as store  # noqa: PLC0415

    report = store.verify_chain(db_path=db)
    log_security_event(
        "OBSERVE_VERIFY",
        {
            "total": report.total,
            "verified": report.verified,
            "legacy": report.legacy_count,
            "ok": report.ok,
            "allow_legacy": allow_legacy,
        },
    )
    _render_chain_report(report)
    if report.broken_obs_id is not None:
        raise typer.Exit(code=1)
    if not report.ok and not (allow_legacy and report.legacy_count > 0):
        raise typer.Exit(code=2)


def _render_chain_report(report: object) -> None:
    """Render a ChainReport: coverage, legacy prefix, and the first break."""
    from presidio_arch_translucency.observe import ChainReport  # noqa: PLC0415

    assert isinstance(report, ChainReport)  # noqa: S101

    if report.total == 0:
        console.print("\n[dim]No observations recorded yet — nothing to verify.[/]\n")
        return

    lines = [
        f"[bold]Observations:[/]   {report.total}",
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
            f"\n[bold red]✗ Chain broken[/] at observation id "
            f"{report.broken_obs_id} (seq {report.broken_seq}):\n"
            f"  {report.break_reason}"
        )
        border = "red"
        title = "[bold red]Observation chain — BROKEN[/]"
    elif report.legacy_count:
        lines.append(
            "\n[yellow]⚠ Chain intact over the chained suffix, but a legacy "
            "prefix predates chaining and cannot be verified.[/]\n"
            "[dim]The chain proves the chained history was not rewritten after "
            "the fact; it does not attest the readings were honest at "
            "capture.[/]"
        )
        border = "yellow"
        title = "[bold yellow]Observation chain — PARTIAL (legacy prefix)[/]"
    else:
        lines.append(
            "\n[green]✓ Chain intact[/] — no post-hoc edit, insertion, "
            "deletion, or reorder detected.\n"
            "[dim]Proves the local history was not rewritten after the fact "
            "relative to the chain head; not that the readings were honest at "
            "capture.[/]"
        )
        border = "green"
        title = "[bold blue]Observation chain — verified[/]"

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


def _render_training_results(result, show_all: bool) -> None:  # noqa: ANN001
    """Render a TrainingAnalysisResult as a Rich table + recommendation panel."""
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
        table.add_row(
            r.strategy.value,
            str(r.optimal_degree) if r.optimal_degree else "—",
            f"{r.estimated_samples_per_second:,.2f}",
            f"{r.scaling_efficiency_pct:.1f}",
            f"{r.per_device_memory_gb:.2f}",
            marker,
            f"{r.throughput_gain_pct:+.1f}",
            style=style,
        )

    console.print(table)

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
) -> None:
    """
    Analyze a training workload and recommend the parallelism strategy.

    Training-domain counterpart of `pat analyze`: applies the architectural
    translucency model to data / fsdp / tensor / pipeline parallelism, with
    per-device memory as a hard feasibility constraint (not a soft penalty).
    """
    from presidio_arch_translucency.training import (  # noqa: PLC0415
        DEFAULT_MICROBATCHES,
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
    _render_training_results(result, show_all=show_all)


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
        TrainingDomainError,
        evaluate_strategy,
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
        r = evaluate_strategy(
            ParallelismStrategy(strategy_str),
            degree,
            baseline_samples_per_second=sps,
            model_memory_gb=model_mem,
            device_memory_gb=device_mem,
            microbatches=microbatches or DEFAULT_MICROBATCHES,
        )
    except (InputValidationError, TrainingDomainError) as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "strategy": r.strategy.value,
                    "degree": r.optimal_degree,
                    "estimated_samples_per_second": r.estimated_samples_per_second,
                    "scaling_efficiency_pct": r.scaling_efficiency_pct,
                    "per_device_memory_gb": r.per_device_memory_gb,
                    "feasible": r.feasible,
                    "throughput_gain_pct": r.throughput_gain_pct,
                },
                separators=(",", ":"),
            )
        )
        return
    feasibility = "[green]feasible[/]" if r.feasible else "[bold red]INFEASIBLE[/]"
    console.print(
        Panel(
            f"[bold]{r.strategy.value}[/] at δ = {r.optimal_degree}: "
            f"≈ {r.estimated_samples_per_second:,.2f} samples/s "
            f"({r.throughput_gain_pct:+.1f}%, "
            f"{r.scaling_efficiency_pct:.1f}% scaling efficiency), "
            f"{r.per_device_memory_gb:.2f} GB/device — {feasibility}.",
            title="What-if (training)",
        )
    )


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
) -> None:
    """Emit a key-less Layer-0 training-run record as JSON.

    arch-translucency holds **no signing key**: this prints an *unsigned*
    ``training-run@1`` record to stdout. Pipe it to the signing-bridge sidecar,
    which adds the Ed25519 signature. ``--parent`` hashes chain the run to the
    upstream evidence that authorized it (classification, gate decision),
    forming a verifiable provenance DAG across the suite — traceability
    (EU AI Act Art. 12 record-keeping) as a data structure.
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
