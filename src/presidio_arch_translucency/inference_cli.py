"""
CLI surface for the LLM inference serving domain (`pat infer-analyze`,
`pat infer-what-if`). Kept out of ``cli.py`` (registered there, like
``demo``) so the inference profile stays one self-contained module pair.

All output is modelled (ADR-0012). Without ``--calibration`` every table
carries an "uncalibrated" notice; with it, the committed profile's name and
digest are shown, and the profile must match the hardware being analysed.
`pat infer-calibrate` and `pat infer-validate` live here too.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from presidio_arch_translucency.infer_calibrate import (
    HardwareSpec,
    InferenceCalibrationError,
    InferenceCalibrationResult,
    InferenceCalibrationTamperError,
    InferencePoint,
    InferenceProfile,
    ValidationReport,
    fit_inference_calibration,
    load_inference_profile,
    load_points_file,
    parse_point,
    require_hardware_match,
    validate_holdout,
    validate_leave_one_out,
    validate_profile_name,
    write_inference_profile,
)
from presidio_arch_translucency.inference import (
    DEFAULT_ENGINE_PARAMS,
    DEFAULT_GPU_MEMORY_UTILIZATION,
    DEFAULT_GPUS_PER_NODE,
    DEFAULT_MAX_NUM_SEQS,
    DEFAULT_TP_DEGREES,
    ConfigResult,
    EngineParams,
    EngineSpec,
    GpuSpec,
    InferenceAnalysis,
    InferenceDomainError,
    InferenceWorkload,
    ModelSpec,
    analyze_inference,
    evaluate_config,
    representative_configs,
)
from presidio_arch_translucency.security import (
    log_recommendation,
    log_security_event,
)

console = Console()
err_console = Console(stderr=True, style="bold red")
info_console = Console(stderr=True)

_UNCALIBRATED_NOTE = (
    "[yellow]⚠ Modelled, uncalibrated: overhead α/β and engine efficiencies are "
    "MVP placeholders (ADR-0012). TTFT p99 is M/M/c-derived, not observed; "
    "no figure here is a measurement.[/]"
)


# -- shared options --------------------------------------------------------------


def _rps_option() -> float:
    return typer.Option(
        ...,
        "--requests-per-second",
        "-r",
        help="Mean request arrival rate λ (req/s).",
    )


def _prompt_option() -> float:
    return typer.Option(..., "--prompt-tokens", "-p", help="Mean prompt length P.")


def _output_option() -> float:
    return typer.Option(..., "--output-tokens", "-o", help="Mean output length O.")


def _weights_option() -> float:
    return typer.Option(
        ...,
        "--model-weights-gb",
        "-w",
        help="Model weights W in GB at the deployed dtype.",
    )


def _kv_option() -> float:
    return typer.Option(
        ...,
        "--kv-bytes-per-token",
        "-k",
        help="KV bytes per token: 2 · layers · kv_heads · head_dim · dtype_bytes.",
    )


def _gpus_option() -> int:
    return typer.Option(..., "--gpus", "-n", help="GPU budget N.")


def _gpu_mem_option() -> float:
    return typer.Option(
        ...,
        "--gpu-memory-gb",
        "-m",
        help=(
            "Memory per GPU M in GB (10^9 bytes); entering the GiB figure as-is "
            "gives a conservative estimate."
        ),
    )


def _gpu_bw_option() -> float:
    return typer.Option(
        ...,
        "--gpu-bandwidth-gbs",
        "-b",
        help="Spec HBM bandwidth per GPU (GB/s), e.g. 864 for L40S, 3350 for H100 SXM.",
    )


def _gpus_per_node_option() -> int:
    return typer.Option(
        DEFAULT_GPUS_PER_NODE,
        "--gpus-per-node",
        help="GPUs per node; caps tensor parallelism (no cross-node TP).",
    )


def _mem_util_option() -> float:
    return typer.Option(
        DEFAULT_GPU_MEMORY_UTILIZATION,
        "--gpu-memory-utilization",
        help="vLLM gpu_memory_utilization h (fraction of M usable).",
    )


def _max_seqs_option() -> int:
    return typer.Option(
        DEFAULT_MAX_NUM_SEQS, "--max-num-seqs", help="vLLM max_num_seqs B_max."
    )


def _ttft_slo_option() -> Optional[float]:  # noqa: UP045
    return typer.Option(None, "--ttft-slo-ms", help="TTFT p99 target (ms).")


def _tpot_slo_option() -> Optional[float]:  # noqa: UP045
    return typer.Option(None, "--tpot-slo-ms", help="Mean TPOT target (ms).")


def _cost_option() -> Optional[float]:  # noqa: UP045
    return typer.Option(
        None, "--cost-per-gpu-hour", help="Uniform GPU price for $/h and $/Mtok."
    )


def _bw_eff_option() -> Optional[float]:  # noqa: UP045
    return typer.Option(
        None,
        "--bandwidth-efficiency",
        help=(
            "Achieved fraction of spec bandwidth η (placeholder "
            f"{DEFAULT_ENGINE_PARAMS.bandwidth_efficiency:g}). "
            "Not allowed with --calibration."
        ),
    )


def _step_overhead_option() -> Optional[float]:  # noqa: UP045
    return typer.Option(
        None,
        "--step-overhead-ms",
        help=(
            "Fixed per-decode-step cost t₀ in ms (placeholder "
            f"{DEFAULT_ENGINE_PARAMS.step_overhead_ms:g}). "
            "Not allowed with --calibration."
        ),
    )


def _prefill_rate_option() -> Optional[float]:  # noqa: UP045
    return typer.Option(
        None,
        "--prefill-tokens-per-s",
        help=(
            "Prefill throughput per GPU R_pre (placeholder "
            f"{DEFAULT_ENGINE_PARAMS.prefill_tokens_per_s_per_gpu:g}). "
            "Not allowed with --calibration."
        ),
    )


def _calibration_option() -> Optional[str]:  # noqa: UP045
    return typer.Option(
        None,
        "--calibration",
        help=(
            "Use a committed `pat infer-calibrate` profile. Fails closed if it "
            "was tampered with or fitted for different weights/KV/GPU."
        ),
    )


def _resolve_params(
    calibration: str | None,
    overrides: dict[str, float | None],
    weights_gb: float,
    kv_bytes: float,
    gpu_bandwidth_gbs: float,
    gpu_memory_gb: float,
) -> tuple[EngineParams, InferenceProfile | None]:
    """Engine parameters from a verified profile, or from placeholders/overrides."""
    given = {name: value for name, value in overrides.items() if value is not None}
    if calibration is None:
        return EngineParams(**given), None
    if given:
        raise InferenceCalibrationError(
            "--calibration cannot be combined with engine overrides "
            f"({', '.join(sorted(given))}); the profile supplies them"
        )
    profile = load_inference_profile(calibration)
    require_hardware_match(
        profile, weights_gb, kv_bytes, gpu_bandwidth_gbs, gpu_memory_gb
    )
    return profile.params, profile


def _engine_overrides(
    bandwidth_efficiency: float | None,
    step_overhead_ms: float | None,
    prefill_tokens_per_s: float | None,
) -> dict[str, float | None]:
    return {
        "bandwidth_efficiency": bandwidth_efficiency,
        "step_overhead_ms": step_overhead_ms,
        "prefill_tokens_per_s_per_gpu": prefill_tokens_per_s,
    }


def _calibrated_tp_max(profile: InferenceProfile) -> int:
    return max(p.tp for p in profile.points)


def _extrapolated(c: ConfigResult, profile: InferenceProfile | None) -> bool | None:
    """True when a calibrated row lies outside what the profile observed.

    Beyond the largest calibrated tp, or any n ≥ 2 (router cost is never
    observed by single-instance points). ``None`` when uncalibrated.
    """
    if profile is None:
        return None
    return c.tp > _calibrated_tp_max(profile) or c.instances >= 2


def _note(
    profile: InferenceProfile | None,
    gpu: GpuSpec | None = None,
    engine: EngineSpec | None = None,
) -> str:
    if profile is None:
        return _UNCALIBRATED_NOTE
    note = (
        f"[green]Calibrated: profile {profile.name!r} "
        f"(commitment {profile.digest[:16]}…).[/] [yellow]Figures are still "
        "modelled: TTFT p99 is M/M/c-derived. † = extrapolated beyond the "
        f"profile (tp > {_calibrated_tp_max(profile)}, or n ≥ 2 where router "
        "cost is unobserved).[/]"
    )
    hw = profile.hardware
    if gpu is not None and gpu.memory_utilization != hw.gpu_memory_utilization:
        note += (
            f"\n[dim]gpu_memory_utilization: profile {hw.gpu_memory_utilization:g},"
            f" analysed {gpu.memory_utilization:g}.[/]"
        )
    if engine is not None and engine.max_num_seqs != hw.max_num_seqs:
        note += (
            f"\n[dim]max_num_seqs: profile {hw.max_num_seqs}, analysed "
            f"{engine.max_num_seqs}.[/]"
        )
    return note


def _calibration_json(
    profile: InferenceProfile | None,
    gpu: GpuSpec | None = None,
    engine: EngineSpec | None = None,
) -> dict:
    if profile is None:
        return {"calibrated": False, "calibration": None}
    hw = profile.hardware
    return {
        "calibrated": True,
        "calibration": {
            "profile": profile.name,
            "digest": profile.digest,
            "calibrated_tp_max": _calibrated_tp_max(profile),
            "gpu_memory_utilization": {
                "profile": hw.gpu_memory_utilization,
                "analysed": gpu.memory_utilization if gpu else None,
            },
            "max_num_seqs": {
                "profile": hw.max_num_seqs,
                "analysed": engine.max_num_seqs if engine else None,
            },
        },
    }


def _build_inputs(
    rps: float,
    prompt_tokens: float,
    output_tokens: float,
    weights_gb: float,
    kv_bytes: float,
    gpus: int,
    gpu_memory_gb: float,
    gpu_bandwidth_gbs: float,
    gpus_per_node: int,
    memory_utilization: float,
    max_num_seqs: int,
    tp_degrees: tuple[int, ...],
) -> tuple[InferenceWorkload, ModelSpec, GpuSpec, EngineSpec]:
    return (
        InferenceWorkload(rps, prompt_tokens, output_tokens),
        ModelSpec(weights_gb, kv_bytes),
        GpuSpec(
            gpus, gpu_memory_gb, gpu_bandwidth_gbs, gpus_per_node, memory_utilization
        ),
        EngineSpec(max_num_seqs, tp_degrees),
    )


def _fmt(value: float | None, spec: str) -> str:
    return "—" if value is None else format(value, spec)


def _config_json(c: ConfigResult, profile: InferenceProfile | None = None) -> dict:
    data = asdict(c)
    data["strategy"] = c.strategy.value
    data["extrapolated"] = _extrapolated(c, profile)
    return data


# -- rendering -------------------------------------------------------------------


def _render_analysis(
    analysis: InferenceAnalysis,
    show_all: bool,
    note: str = _UNCALIBRATED_NOTE,
    profile: InferenceProfile | None = None,
) -> None:
    rows = analysis.configs if show_all else representative_configs(analysis)
    has_cost = any(c.cost_per_hour is not None for c in rows)
    table = Table(
        title="Inference serving analysis (architectural translucency)",
        box=box.SIMPLE_HEAVY,
        collapse_padding=True,
        pad_edge=False,
    )
    for name, justify in (
        ("Strategy", "left"),
        ("tp×n", "right"),
        ("GPUs", "right"),
        ("Batch", "right"),
        ("ρ", "right"),
        ("Cap r/s", "right"),
        ("TTFT99", "right"),
        ("TPOT", "right"),
        ("SLO", "center"),
    ):
        table.add_column(name, justify=justify, no_wrap=True)
    if has_cost:
        table.add_column("$/M out", justify="right", no_wrap=True)

    rec = analysis.recommended
    for c in rows:
        if not c.feasible:
            status = "[red]no fit[/]"
        elif c.saturated:
            status = "[red]sat.[/]"
        elif c.near_saturation:
            status = "[yellow]ρ>.95[/]"
        else:
            status = "✓" if c.slo_ok else "[yellow]✗[/]"
        cells = [
            c.strategy.value,
            f"{c.tp}×{c.instances}{'†' if _extrapolated(c, profile) else ''}",
            str(c.gpus),
            str(c.max_batch),
            _fmt(c.utilization if c.feasible else None, ".2f"),
            _fmt(c.capacity_rps if c.feasible else None, ".2f"),
            _fmt(c.ttft_p99_ms, ".1f"),
            _fmt(c.tpot_ms, ".1f"),
            status,
        ]
        if has_cost:
            cells.append(_fmt(c.cost_per_million_output_tokens, ".4f"))
        is_rec = rec is not None and (c.tp, c.instances) == (rec.tp, rec.instances)
        table.add_row(*cells, style="bold green" if is_rec else None)
    console.print(table)
    console.print(
        "[dim]TTFT99 = prefill + M/M/c queueing wait, p99 ms (prefill–decode "
        "interference not modelled) · TPOT = mean ms · $/M out = per million "
        "output tokens · no fit = weights/KV do not fit · sat. = saturated "
        "(ρ ≥ 1) · ρ>.95 = too close to capacity to recommend[/]"
    )

    if rec is not None:
        body = (
            f"Recommended:  {rec.strategy.value}  (tp={rec.tp} × {rec.instances} "
            f"instance{'s' if rec.instances != 1 else ''} = {rec.gpus} GPU"
            f"{'s' if rec.gpus != 1 else ''} of {analysis.gpu_budget})\n"
            f"TTFT p99 {rec.ttft_p99_ms:.1f} ms · TPOT {rec.tpot_ms:.1f} ms · "
            f"ρ {rec.utilization:.2f} · capacity {rec.capacity_rps:.2f} req/s"
        )
        console.print(Panel(body, title="Recommendation", border_style="green"))
    else:
        be = analysis.best_effort
        body = (
            "No configuration meets the SLO at this demand within "
            f"{analysis.gpu_budget} GPUs."
        )
        if be is not None:
            body += (
                f"\nHighest capacity: {be.strategy.value} tp={be.tp} × "
                f"{be.instances} → {be.capacity_rps:.2f} req/s "
                f"(demand {analysis.workload.requests_per_second:g} req/s)"
            )
        console.print(Panel(body, title="Recommendation", border_style="red"))
    console.print(note)


# -- commands --------------------------------------------------------------------


def infer_analyze_command(
    requests_per_second: float = _rps_option(),
    prompt_tokens: float = _prompt_option(),
    output_tokens: float = _output_option(),
    model_weights_gb: float = _weights_option(),
    kv_bytes_per_token: float = _kv_option(),
    gpus: int = _gpus_option(),
    gpu_memory_gb: float = _gpu_mem_option(),
    gpu_bandwidth_gbs: float = _gpu_bw_option(),
    gpus_per_node: int = _gpus_per_node_option(),
    gpu_memory_utilization: float = _mem_util_option(),
    max_num_seqs: int = _max_seqs_option(),
    tp: Optional[list[int]] = typer.Option(  # noqa: UP045, B008
        None,
        "--tp",
        help="Tensor-parallel degree to consider (repeatable). Default: 1 2 4 8.",
    ),
    ttft_slo_ms: Optional[float] = _ttft_slo_option(),  # noqa: UP045
    tpot_slo_ms: Optional[float] = _tpot_slo_option(),  # noqa: UP045
    cost_per_gpu_hour: Optional[float] = _cost_option(),  # noqa: UP045
    bandwidth_efficiency: Optional[float] = _bw_eff_option(),  # noqa: UP045
    step_overhead_ms: Optional[float] = _step_overhead_option(),  # noqa: UP045
    prefill_tokens_per_s: Optional[float] = _prefill_rate_option(),  # noqa: UP045
    calibration: Optional[str] = _calibration_option(),  # noqa: UP045
    show_all: bool = typer.Option(
        False, "--show-all", help="List every (tp, n) configuration, not one per tp."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of tables."),
) -> None:
    """
    Recommend how to spend a GPU budget serving an LLM: replicas, tensor
    parallelism, or replicas of TP groups.

    Inference-domain counterpart of `pat analyze` (ADR-0012). Sweeps every
    (tp, n) with tp·n ≤ N; weights or KV cache that do not fit are infeasible,
    configurations at capacity are saturated, and the recommendation is the
    fewest GPUs meeting the TTFT/TPOT SLO at the given demand.
    """
    try:
        workload, model, gpu, engine = _build_inputs(
            requests_per_second,
            prompt_tokens,
            output_tokens,
            model_weights_gb,
            kv_bytes_per_token,
            gpus,
            gpu_memory_gb,
            gpu_bandwidth_gbs,
            gpus_per_node,
            gpu_memory_utilization,
            max_num_seqs,
            tuple(tp) if tp else DEFAULT_TP_DEGREES,
        )
        params, profile = _resolve_params(
            calibration,
            _engine_overrides(
                bandwidth_efficiency, step_overhead_ms, prefill_tokens_per_s
            ),
            model_weights_gb,
            kv_bytes_per_token,
            gpu_bandwidth_gbs,
            gpu_memory_gb,
        )
        analysis = analyze_inference(
            workload,
            model,
            gpu,
            engine,
            params,
            ttft_slo_ms=ttft_slo_ms,
            tpot_slo_ms=tpot_slo_ms,
            cost_per_gpu_hour=cost_per_gpu_hour,
        )
    except (InferenceDomainError, InferenceCalibrationError) as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except InferenceCalibrationTamperError as exc:
        err_console.print(f"[bold red]Calibration refused:[/] {exc}")
        raise typer.Exit(code=2) from exc

    rec = analysis.recommended
    log_recommendation(
        layer=rec.strategy.value if rec is not None else "none-qualifying",
        replicas=rec.gpus if rec is not None else 0,
        throughput_gain_pct=0.0,
    )
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "modelled": True,
                    **_calibration_json(profile, gpu, engine),
                    "ttft_p99_basis": "prefill + M/M/c wait; no interference",
                    "recommended": _config_json(rec, profile) if rec else None,
                    "best_effort": (
                        _config_json(analysis.best_effort, profile)
                        if analysis.best_effort
                        else None
                    ),
                    "baseline": (
                        _config_json(analysis.baseline, profile)
                        if analysis.baseline
                        else None
                    ),
                    "configs": [_config_json(c, profile) for c in analysis.configs],
                },
                separators=(",", ":"),
            )
        )
        return
    _render_analysis(
        analysis,
        show_all=show_all,
        note=_note(profile, gpu, engine),
        profile=profile,
    )


def infer_what_if_command(
    tp: int = typer.Option(..., "--tp", help="Tensor-parallel degree per instance."),
    instances: int = typer.Option(
        1, "--instances", "-i", help="Number of engine instances n."
    ),
    requests_per_second: float = _rps_option(),
    prompt_tokens: float = _prompt_option(),
    output_tokens: float = _output_option(),
    model_weights_gb: float = _weights_option(),
    kv_bytes_per_token: float = _kv_option(),
    gpu_memory_gb: float = _gpu_mem_option(),
    gpu_bandwidth_gbs: float = _gpu_bw_option(),
    gpus_per_node: int = _gpus_per_node_option(),
    gpu_memory_utilization: float = _mem_util_option(),
    max_num_seqs: int = _max_seqs_option(),
    ttft_slo_ms: Optional[float] = _ttft_slo_option(),  # noqa: UP045
    tpot_slo_ms: Optional[float] = _tpot_slo_option(),  # noqa: UP045
    cost_per_gpu_hour: Optional[float] = _cost_option(),  # noqa: UP045
    bandwidth_efficiency: Optional[float] = _bw_eff_option(),  # noqa: UP045
    step_overhead_ms: Optional[float] = _step_overhead_option(),  # noqa: UP045
    prefill_tokens_per_s: Optional[float] = _prefill_rate_option(),  # noqa: UP045
    calibration: Optional[str] = _calibration_option(),  # noqa: UP045
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Evaluate one (tp, instances) inference configuration.

    Fail-closed: an unsupported tp, tp beyond --gpus-per-node, or any
    out-of-domain number is rejected (exit 2), never reported as feasible.
    """
    try:
        workload, model, gpu, engine = _build_inputs(
            requests_per_second,
            prompt_tokens,
            output_tokens,
            model_weights_gb,
            kv_bytes_per_token,
            max(1, tp) * max(1, instances),
            gpu_memory_gb,
            gpu_bandwidth_gbs,
            gpus_per_node,
            gpu_memory_utilization,
            max_num_seqs,
            DEFAULT_TP_DEGREES,
        )
        params, profile = _resolve_params(
            calibration,
            _engine_overrides(
                bandwidth_efficiency, step_overhead_ms, prefill_tokens_per_s
            ),
            model_weights_gb,
            kv_bytes_per_token,
            gpu_bandwidth_gbs,
            gpu_memory_gb,
        )
        c = evaluate_config(
            tp,
            instances,
            workload,
            model,
            gpu,
            engine,
            params,
            ttft_slo_ms=ttft_slo_ms,
            tpot_slo_ms=tpot_slo_ms,
            cost_per_gpu_hour=cost_per_gpu_hour,
        )
    except (InferenceDomainError, InferenceCalibrationError) as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except InferenceCalibrationTamperError as exc:
        err_console.print(f"[bold red]Calibration refused:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "INFER_WHAT_IF_INVOCATION",
        {"strategy": c.strategy.value, "tp": c.tp, "instances": c.instances},
    )
    if as_json:
        payload = {
            "modelled": True,
            **_calibration_json(profile, gpu, engine),
            **_config_json(c, profile),
        }
        typer.echo(json.dumps(payload, separators=(",", ":")))
        return

    table = Table(
        title=f"Inference what-if: {c.strategy.value} tp={c.tp} × {c.instances}",
        box=box.SIMPLE_HEAVY,
        show_header=False,
    )
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    status = (
        "infeasible (weights or KV cache do not fit)"
        if not c.feasible
        else "saturated (ρ ≥ 1)"
        if c.saturated
        else "near saturation (ρ > 0.95)"
        if c.near_saturation
        else "ok"
    )
    for label, value in (
        ("Status", status),
        (
            "Extrapolated",
            "—" if profile is None else ("yes" if _extrapolated(c, profile) else "no"),
        ),
        ("GPUs", str(c.gpus)),
        ("Max batch / instance", str(c.max_batch)),
        ("Effective batch", _fmt(c.effective_batch if c.feasible else None, ".1f")),
        ("Utilization ρ", _fmt(c.utilization if c.feasible else None, ".2f")),
        ("Capacity req/s", _fmt(c.capacity_rps if c.feasible else None, ".2f")),
        ("Output tokens/s", _fmt(c.output_tokens_per_s if c.feasible else None, ".1f")),
        ("TTFT mean ms", _fmt(c.ttft_mean_ms, ".1f")),
        ("TTFT p99 ms", _fmt(c.ttft_p99_ms, ".1f")),
        ("TPOT ms", _fmt(c.tpot_ms, ".1f")),
        ("SLO met", "yes" if c.slo_ok else "no"),
        ("$/h", _fmt(c.cost_per_hour, ".2f")),
        ("$/M output tokens", _fmt(c.cost_per_million_output_tokens, ".4f")),
    ):
        table.add_row(label, value)
    console.print(table)
    console.print(_note(profile, gpu, engine))


# -- calibration and validation ------------------------------------------------------


def _collect_points(
    point: Optional[list[str]],  # noqa: UP045
    points_file: Optional[str],  # noqa: UP045
) -> list[InferencePoint]:
    points = [parse_point(raw) for raw in point or []]
    if points_file is not None:
        points.extend(load_points_file(points_file))
    if not points:
        raise InferenceCalibrationError("give points with --point or --points-file")
    return points


def _point_label(p: InferencePoint) -> str:
    return f"tp={p.tp} b={p.batch:g}"


def _calibration_result_json(
    result: InferenceCalibrationResult, path: str | None, digest: str | None
) -> dict:
    return {
        "params": {
            "bandwidth_efficiency": result.params.bandwidth_efficiency,
            "step_overhead_ms": result.params.step_overhead_ms,
            "prefill_tokens_per_s_per_gpu": result.params.prefill_tokens_per_s_per_gpu,
            "replica_alpha": result.params.replica_alpha,
            "tensor_alpha": result.params.tensor_alpha,
            "tensor_beta": result.params.tensor_beta,
        },
        "fitted": list(result.fitted),
        "held": list(result.held),
        "at_bound": list(result.at_bound),
        "degrees_of_freedom": result.degrees_of_freedom,
        "r_squared": result.r_squared,
        "rmse_ms": result.rmse_ms,
        "points": [
            {
                **pr.point.as_row(),
                "predicted_tpot_ms": pr.tpot_ms,
                "tpot_error_pct": pr.tpot_error_pct,
                "predicted_prefill_ms": pr.prefill_ms,
                "prefill_error_pct": pr.prefill_error_pct,
            }
            for pr in result.predictions
        ],
        "path": path,
        "digest": digest,
    }


def infer_calibrate_command(
    profile: str = typer.Option(
        ..., "--profile", help="Profile name (one per model × GPU pair)."
    ),
    point: Optional[list[str]] = typer.Option(  # noqa: UP045, B008
        None,
        "--point",
        help=(
            "Measured point, repeatable: tp=2,batch=24.3,prompt=1000,output=256,"
            "tpot_ms=35.1[,prefill_ms=60][,kv_usage=0.41]"
        ),
    ),
    points_file: Optional[str] = typer.Option(  # noqa: UP045
        None, "--points-file", help="JSON-Lines file of points (same keys)."
    ),
    model_weights_gb: float = _weights_option(),
    kv_bytes_per_token: float = _kv_option(),
    gpu_bandwidth_gbs: float = _gpu_bw_option(),
    gpu_memory_gb: float = _gpu_mem_option(),
    gpu_memory_utilization: float = _mem_util_option(),
    max_num_seqs: int = _max_seqs_option(),
    model_name: Optional[str] = typer.Option(  # noqa: UP045
        None, "--model-name", help="Label bound into the profile, e.g. the HF id."
    ),
    gpu_type: Optional[str] = typer.Option(  # noqa: UP045
        None, "--gpu-type", help="Label bound into the profile, e.g. L40S."
    ),
    engine_version: Optional[str] = typer.Option(  # noqa: UP045
        None, "--engine-version", help="Label bound into the profile, e.g. vllm 0.31.0."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Fit and report without writing the profile."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of tables."),
) -> None:
    """Fit inference engine parameters from measured vLLM points.

    Each point is one steady-state window on a single instance at a fixed tp:
    mean running batch, mean inter-token latency (TPOT) and, optionally, mean
    prefill time (measure it at low load) and mean KV-cache usage. Needs ≥2
    tp=1 points with distinct batch and ≥1 tp>1 point; two distinct tp>1
    degrees also fit β. The fit is written as a committed profile under
    `inference.<profile>` in ~/.pat/model.json.
    """
    try:
        name = validate_profile_name(profile)
        points = _collect_points(point, points_file)
        hardware = HardwareSpec(
            weights_gb=model_weights_gb,
            kv_bytes_per_token=kv_bytes_per_token,
            gpu_bandwidth_gbs=gpu_bandwidth_gbs,
            gpu_memory_gb=gpu_memory_gb,
            gpu_memory_utilization=gpu_memory_utilization,
            max_num_seqs=max_num_seqs,
            model_name=model_name,
            gpu_type=gpu_type,
            engine_version=engine_version,
        )
        result = fit_inference_calibration(points, hardware)
        path = None if dry_run else write_inference_profile(name, result)
        digest = None if dry_run else load_inference_profile(name).digest
    except (InferenceCalibrationError, InferenceCalibrationTamperError) as exc:
        err_console.print(f"[bold red]Inference calibration error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "INFER_CALIBRATE_INVOCATION",
        {"points": len(points), "dry_run": dry_run, "fitted": len(result.fitted)},
    )
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "profile": name,
                    **_calibration_result_json(
                        result, str(path) if path else None, digest
                    ),
                },
                separators=(",", ":"),
            )
        )
        return

    params = Table(title=f"Inference calibration: {name}", box=box.SIMPLE_HEAVY)
    params.add_column("Parameter", style="bold")
    params.add_column("Value", justify="right")
    params.add_column("Source")
    for attr, label, spec in (
        ("bandwidth_efficiency", "Bandwidth efficiency η", ".3f"),
        ("step_overhead_ms", "Step overhead t₀ ms", ".3f"),
        ("tensor_alpha", "α tensor", ".4f"),
        ("tensor_beta", "β tensor", ".4f"),
        ("replica_alpha", "α replica", ".4f"),
        ("prefill_tokens_per_s_per_gpu", "Prefill tok/s/GPU", ",.0f"),
    ):
        if attr in result.at_bound:
            source = "[yellow]fitted, at bound[/]"
        elif attr in result.fitted:
            source = "fitted"
        else:
            source = "[dim]held at default[/]"
        params.add_row(label, format(getattr(result.params, attr), spec), source)
    console.print(params)

    residuals = Table(box=box.SIMPLE, title="Measured vs fitted")
    for col in ("Point", "TPOT ms", "Fitted", "Err %", "Prefill ms", "Fitted", "Err %"):
        residuals.add_column(col, justify="right")
    for pr in result.predictions:
        residuals.add_row(
            _point_label(pr.point),
            f"{pr.point.tpot_ms:.2f}",
            f"{pr.tpot_ms:.2f}",
            f"{pr.tpot_error_pct:+.1f}",
            _fmt(pr.point.prefill_ms, ".1f"),
            _fmt(pr.prefill_ms, ".1f"),
            _fmt(pr.prefill_error_pct, "+.1f"),
        )
    console.print(residuals)
    quality = (
        f"R² {result.r_squared:.4f}"
        if result.r_squared is not None
        else "R² not reported (fewer than 2 spare points)"
    )
    console.print(
        f"Degrees of freedom {result.degrees_of_freedom} · {quality} · "
        f"TPOT RMSE {result.rmse_ms:.3f} ms"
    )
    if result.at_bound:
        console.print(
            "[yellow]Parameters at a bound are poorly identified by these points; "
            "add points at more tp degrees.[/]"
        )
    if path is None:
        console.print("[dim]Dry run: profile not written.[/]")
    else:
        console.print(f"[green]Profile written →[/] {path} (commitment {digest})")
    console.print(
        "[dim]η is an effective bandwidth: measured inter-token latency includes "
        "chunked-prefill interference. Validate on held-out configurations with "
        "`pat infer-validate`.[/]"
    )


def _validation_json(report: ValidationReport, mode: str, profile: str) -> dict:
    return {
        "profile": profile,
        "mode": mode,
        "tpot_mape_pct": report.tpot_mape_pct,
        "tpot_max_error_pct": report.tpot_max_error_pct,
        "prefill_mape_pct": report.prefill_mape_pct,
        "prefill_max_error_pct": report.prefill_max_error_pct,
        "skipped_folds": report.skipped_folds,
        "degraded_folds": report.degraded_folds,
        "points": [
            {
                **pr.point.as_row(),
                "predicted_tpot_ms": pr.tpot_ms,
                "tpot_error_pct": pr.tpot_error_pct,
                "predicted_prefill_ms": pr.prefill_ms,
                "prefill_error_pct": pr.prefill_error_pct,
            }
            for pr in report.predictions
        ],
    }


def infer_validate_command(
    calibration: str = typer.Option(
        ..., "--calibration", help="Committed profile to validate."
    ),
    point: Optional[list[str]] = typer.Option(  # noqa: UP045, B008
        None, "--point", help="Held-out measured point (repeatable)."
    ),
    points_file: Optional[str] = typer.Option(  # noqa: UP045
        None, "--points-file", help="JSON-Lines file of held-out points."
    ),
    leave_one_out: bool = typer.Option(
        False,
        "--leave-one-out",
        help="Refit on the profile's own points minus one, predict that one.",
    ),
    sweep: Optional[list[str]] = typer.Option(  # noqa: UP045, B008
        None,
        "--sweep",
        help="Open-loop infer-benchmark report, one per layout (repeatable).",
    ),
    tpot_slo_ms: Optional[float] = typer.Option(  # noqa: UP045
        None, "--tpot-slo-ms", help="Sweep mode: mean TPOT SLO."
    ),
    ttft_slo_ms: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--ttft-slo-ms",
        help="Sweep mode: TTFT p99 SLO; must be a vLLM histogram bucket edge.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of tables."),
) -> None:
    """Report a profile's prediction error on held-out points or λ sweeps.

    Held-out mode predicts measured points the profile never saw (use other tp
    degrees to test transfer across layers). Leave-one-out mode refits on the
    profile's committed points; folds that leave the fit unidentifiable are
    skipped and counted. Sweep mode (--sweep) compares each layout's predicted
    λ_max with the bracket its open-loop sweep measured (H3): pass when the
    worst case over the bracket is within 20%, fail when even the nearer edge
    is more than 25% off, otherwise inconclusive.
    """
    if sweep:
        _validate_sweeps_command(
            calibration,
            sweep,
            tpot_slo_ms,
            ttft_slo_ms,
            as_json,
            other_mode=bool(point or points_file or leave_one_out),
        )
        return
    try:
        if tpot_slo_ms is not None or ttft_slo_ms is not None:
            raise InferenceCalibrationError("SLOs apply to --sweep validation only")
        profile = load_inference_profile(calibration)
        if leave_one_out:
            if point or points_file:
                raise InferenceCalibrationError(
                    "--leave-one-out uses the profile's own points; do not pass "
                    "--point/--points-file"
                )
            report = validate_leave_one_out(list(profile.points), profile.hardware)
            mode = "leave-one-out"
        else:
            report = validate_holdout(
                _collect_points(point, points_file), profile.hardware, profile.params
            )
            mode = "holdout"
    except InferenceCalibrationError as exc:
        err_console.print(f"[bold red]Validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except InferenceCalibrationTamperError as exc:
        err_console.print(f"[bold red]Calibration refused:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "INFER_VALIDATE_INVOCATION",
        {"mode": mode, "points": len(report.predictions)},
    )
    if as_json:
        typer.echo(
            json.dumps(
                _validation_json(report, mode, profile.name), separators=(",", ":")
            )
        )
        return

    table = Table(
        title=f"Inference validation ({mode}): {profile.name}", box=box.SIMPLE_HEAVY
    )
    for col in ("Point", "TPOT ms", "Predicted", "Err %", "Prefill ms", "Predicted"):
        table.add_column(col, justify="right")
    for pr in report.predictions:
        table.add_row(
            _point_label(pr.point),
            f"{pr.point.tpot_ms:.2f}",
            f"{pr.tpot_ms:.2f}",
            f"{pr.tpot_error_pct:+.1f}",
            _fmt(pr.point.prefill_ms, ".1f"),
            _fmt(pr.prefill_ms, ".1f"),
        )
    console.print(table)
    line = (
        f"TPOT MAPE {report.tpot_mape_pct:.2f}% · max |error| "
        f"{report.tpot_max_error_pct:.2f}%"
    )
    if report.prefill_mape_pct is not None:
        line += f" · prefill MAPE {report.prefill_mape_pct:.2f}%"
    if report.skipped_folds:
        line += f" · {report.skipped_folds} fold(s) skipped (unidentifiable)"
    if report.degraded_folds:
        line += (
            f" · {report.degraded_folds} fold(s) excluded (fitted fewer parameters "
            "than the full set)"
        )
    console.print(line)


def _validate_sweeps_command(
    calibration: str,
    paths: list[str],
    tpot_slo_ms: float | None,
    ttft_slo_ms: float | None,
    as_json: bool,
    other_mode: bool,
) -> None:
    from presidio_arch_translucency.infer_sweep import (  # noqa: PLC0415
        Slo,
        load_sweep,
        validate_sweeps,
    )

    try:
        if other_mode:
            raise InferenceCalibrationError(
                "--sweep cannot be combined with --point/--points-file/--leave-one-out"
            )
        slo = Slo(tpot_ms=tpot_slo_ms, ttft_ms=ttft_slo_ms)
        profile = load_inference_profile(calibration)
        result = validate_sweeps(profile, [load_sweep(path) for path in paths], slo)
    except (InferenceDomainError, InferenceCalibrationError) as exc:
        err_console.print(f"[bold red]Validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except InferenceCalibrationTamperError as exc:
        err_console.print(f"[bold red]Calibration refused:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "INFER_VALIDATE_INVOCATION",
        {"mode": "sweep", "layouts": len(result["layouts"])},
    )
    result = {
        "profile": profile.name,
        "commitment": profile.digest,
        "mode": "sweep",
        **result,
    }
    if as_json:
        typer.echo(json.dumps(result, separators=(",", ":")))
        return

    metrics = ["saturation"] + (["slo"] if slo.given else [])
    table = Table(
        title=f"Inference capacity validation: {profile.name}", box=box.SIMPLE_HEAVY
    )
    for col in (
        "Layout",
        "λ_max",
        "Measured",
        "Predicted",
        "Err % near/far",
        "Verdict",
    ):
        table.add_column(col, justify="right")
    for row in result["layouts"]:
        label = row["layout"] + (" †" if row["extrapolated"] else "")
        for metric in metrics:
            entry = row[metric]
            measured = entry["measured"]
            error = entry["error"]
            if metric == "saturation":
                kind = "capacity (check)"
            else:
                kind = "SLO" if entry["latency_tested"] else "SLO (ρ-bound)"
            table.add_row(
                label,
                kind,
                _bracket_label(measured),
                f"{entry['predicted']:.3g}",
                _error_label(error),
                error["verdict"],
            )
            label = ""
    console.print(table)
    for row in result["layouts"]:
        reason = (row["slo"] or {}).get("error", {}).get("reason")
        if reason:
            console.print(f"[yellow]{row['layout']}: {reason}[/]", markup=True)
    slo_text = ", ".join(
        f"{name} {value:g} ms"
        for name, value in (("TPOT", slo.tpot_ms), ("TTFT p99", slo.ttft_ms))
        if value is not None
    )
    console.print(
        f"SLO: {slo_text or 'none (capacity only)'} · pass ≤ "
        f"{result['thresholds']['pass_pct']:g}% worst case · fail > "
        f"{result['thresholds']['kill_pct']:g}% to the bracket · capacity (check) "
        "compares model ρ = 1 with client-side saturation · SLO (ρ-bound): the "
        "SLO never bound, so latency was not tested · † extrapolated"
    )
    for ranking in result["ranking"]:
        predicted = " > ".join(ranking["predicted"]) if ranking["predicted"] else "tie"
        agree = {True: "agree", False: "DISAGREE"}.get(
            ranking["agree"], f"no order ({ranking['reason']})"
        )
        console.print(
            f"{ranking['gpus']} GPU(s): predicted {predicted} · measured: {agree}"
        )


def _bracket_label(measured: dict) -> str:
    lo, hi = measured["lambda_lo"], measured["lambda_hi"]
    if lo is None and hi is None:
        return "no data"
    if hi is None:
        text = f"> {lo:.3g}"
    elif lo is None:
        text = f"< {hi:.3g}"
    else:
        text = f"{lo:.3g}–{hi:.3g}"
    if measured["non_monotone"]:
        text += " (non-monotone)"
    return text


def _error_label(error: dict) -> str:
    if error["to_bracket_pct"] is None:
        return "—"
    return f"{error['to_bracket_pct']:.1f} / {error['worst_pct']:.1f}"


# -- observation -----------------------------------------------------------------------


def infer_observe_command(
    prometheus: str = typer.Option(
        ..., "--prometheus", help="Prometheus base URL scraping the vLLM engine."
    ),
    tp: int = typer.Option(
        ..., "--tp", help="Tensor-parallel degree of the engine (not in metrics)."
    ),
    window_s: int = typer.Option(
        120, "--window-s", help="Steady-state window to average over (30–3600 s)."
    ),
    model_name: Optional[str] = typer.Option(  # noqa: UP045
        None, "--model-name", help="Match the vLLM model_name label exactly."
    ),
    engine: str = typer.Option(
        "0", "--engine", help="Match the vLLM engine label (data-parallel rank)."
    ),
    min_requests: int = typer.Option(
        20, "--min-requests", help="Refuse windows with fewer completed requests."
    ),
    prefill_max_batch: float = typer.Option(
        4.0,
        "--prefill-max-batch",
        help="Include prefill time only at or below this mean batch, with no queue.",
    ),
    no_kv_usage: bool = typer.Option(
        False, "--no-kv-usage", help="Omit KV-cache usage from the point."
    ),
    tpot_metric: str = typer.Option(
        "vllm:inter_token_latency_seconds",
        "--tpot-metric",
        help="TPOT histogram (older vLLM: vllm:time_per_output_token_seconds).",
    ),
    kv_usage_metric: str = typer.Option(
        "vllm:kv_cache_usage_perc",
        "--kv-usage-metric",
        help="KV usage gauge (older vLLM: vllm:gpu_cache_usage_perc).",
    ),
) -> None:
    """Read one steady-state vLLM window from Prometheus as a calibration point.

    Prints one JSON line for `pat infer-calibrate --points-file`; append it
    with `>> points.jsonl`. All queries share one evaluation instant. Refuses
    windows that are not steady: more than one engine series over the window,
    a counter reset, preemptions, a running batch that reached 0 or varied by
    CV ≥ 0.25, completions disagreeing with Little's law by more than 50%, a
    window shorter than 5 mean request latencies, or too few requests.
    Bearer token from PAT_PROMETHEUS_TOKEN only (https required).
    """
    from presidio_arch_translucency.infer_observe import (  # noqa: PLC0415
        InferenceObserveError,
        VllmSelector,
        observe_vllm_window,
    )

    try:
        observed = observe_vllm_window(
            prometheus,
            tp,
            VllmSelector(model_name=model_name, engine=engine),
            window_s=window_s,
            min_requests=min_requests,
            prefill_max_batch=prefill_max_batch,
            include_kv_usage=not no_kv_usage,
            tpot_metric=tpot_metric,
            kv_usage_metric=kv_usage_metric,
        )
    except (InferenceObserveError, InferenceCalibrationError) as exc:
        err_console.print(f"[bold red]Window refused:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "INFER_OBSERVE_INVOCATION",
        {
            "tp": tp,
            "window_s": observed.window_s,
            "prefill": observed.prefill_included,
        },
    )
    typer.echo(json.dumps(observed.point.as_row(), separators=(",", ":")))
    p = observed.point
    summary = (
        f"tp={p.tp} batch={p.batch:.2f} TPOT={p.tpot_ms:.2f} ms over "
        f"{observed.window_s}s, {observed.requests:.0f} requests, batch CV "
        f"{observed.batch_cv:.2f}, Little ratio {observed.little_ratio:.2f}"
    )
    summary += (
        f", prefill {p.prefill_ms:.1f} ms"
        if observed.prefill_included
        else ", prefill omitted (queue or batch too high)"
    )
    info_console.print(f"[green]{summary}[/]")


# -- benchmark harness -------------------------------------------------------------


def _parse_levels(raw: str, mode: str) -> tuple[float, ...]:
    try:
        levels = tuple(float(part) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise InferenceCalibrationError(f"levels must be numbers: {raw!r}") from exc
    if len(levels) > 64:
        raise InferenceCalibrationError("at most 64 levels")
    return levels


def infer_benchmark_command(
    endpoint: str = typer.Option(
        ..., "--endpoint", help="vLLM OpenAI-compatible base URL (pat sends load)."
    ),
    prometheus: str = typer.Option(
        ..., "--prometheus", help="Prometheus base URL scraping that engine."
    ),
    model: str = typer.Option(
        ..., "--model", help="Served model name (request field and model_name label)."
    ),
    tp: int = typer.Option(..., "--tp", help="Tensor-parallel degree of the engine."),
    prompt_tokens: int = typer.Option(
        ..., "--prompt-tokens", "-p", help="Target mean prompt length P."
    ),
    output_tokens: int = typer.Option(
        ..., "--output-tokens", "-o", help="Target mean output length O (±20%)."
    ),
    concurrency: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--concurrency",
        help="Closed-loop levels, e.g. 1,4,16,64: requests kept in flight.",
    ),
    rate: Optional[str] = typer.Option(  # noqa: UP045
        None,
        "--rate",
        help="Open-loop Poisson levels in req/s, e.g. 1,1.25,1.56,1.95.",
    ),
    window_s: int = typer.Option(120, "--window-s", help="Hold window per level."),
    warmup_s: int = typer.Option(60, "--warmup-s", help="Warm-up before each hold."),
    scrape_lag_s: int = typer.Option(
        15, "--scrape-lag-s", help="Keep load on this long after the hold."
    ),
    min_requests: int = typer.Option(
        20, "--min-requests", help="Refuse windows with fewer completions."
    ),
    engine: str = typer.Option("0", "--engine", help="vLLM engine label."),
    seed: int = typer.Option(0, "--seed", help="Seed for prompts and arrivals."),
    report: Optional[str] = typer.Option(  # noqa: UP045
        None, "--report", help="Write the per-level JSON report to this new file."
    ),
    keep_going: bool = typer.Option(
        False,
        "--keep-going",
        help="Open loop: continue past a saturated level instead of stopping.",
    ),
    refine: int = typer.Option(
        0,
        "--refine",
        help="Open loop: bisection levels per measured bracket after the sweep "
        "(2 narrows ×1.25 spacing to about ±3%).",
    ),
    tpot_slo_ms: Optional[float] = typer.Option(  # noqa: UP045
        None,
        "--tpot-slo-ms",
        help="Open loop: also refine the bracket where mean TPOT crosses this SLO.",
    ),
) -> None:
    """Drive load against a running vLLM engine and record each level.

    pat never launches the engine: start vLLM at the tp you are measuring,
    with prefix caching and speculative decoding off. Closed loop
    (--concurrency) produces calibration points; open loop (--rate) produces
    the per-layer SLO-capacity sweep. Recorded points go to stdout as JSON
    lines (`>> points.jsonl`); every level, refused or not, goes to --report.
    API key from PAT_VLLM_API_KEY only (https required).
    """
    from presidio_arch_translucency.infer_benchmark import (  # noqa: PLC0415
        BenchmarkConfig,
        BenchmarkReport,
        InferenceBenchmarkError,
        LevelReport,
        run_benchmark,
    )
    from presidio_arch_translucency.infer_observe import (  # noqa: PLC0415
        InferenceObserveError,
    )

    report_fd: int | None = None

    def write_report(current: BenchmarkReport) -> None:
        # Rewritten after every level through the held descriptor, so paid-for
        # levels survive a later failure or an interrupt.
        if report_fd is None:
            return
        data = (json.dumps(current.as_dict(), indent=2) + "\n").encode("utf-8")
        os.ftruncate(report_fd, 0)
        os.lseek(report_fd, 0, os.SEEK_SET)
        os.write(report_fd, data)
        os.fsync(report_fd)

    def on_level(result: LevelReport, current: BenchmarkReport) -> None:
        if result.point is not None:
            typer.echo(json.dumps(result.point, separators=(",", ":")))
        label = (
            f"{result.mode} {result.level:.4g}"
            + (" (refine)" if result.refine else "")
            + f": {result.completed} completed, "
            f"{result.achieved_rps:.2f} req/s"
        )
        if result.verdict == "recorded":
            info_console.print(f"[green]✓ {label}[/]")
        else:
            info_console.print(f"[yellow]✗ {label} — refused: {result.reason}[/]")
        write_report(current)

    try:
        if (concurrency is None) == (rate is None):
            raise InferenceCalibrationError(
                "give exactly one of --concurrency / --rate"
            )
        mode = "closed" if concurrency is not None else "open"
        levels = _parse_levels(concurrency or rate or "", mode)
        config = BenchmarkConfig(
            endpoint=endpoint,
            prometheus=prometheus,
            model=model,
            tp=tp,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            mode=mode,
            levels=levels,
            window_s=window_s,
            warmup_s=warmup_s,
            scrape_lag_s=scrape_lag_s,
            min_requests=min_requests,
            engine=engine,
            seed=seed,
            stop_on_saturation=not keep_going,
            refine=refine,
            tpot_slo_ms=tpot_slo_ms,
        )
        if report is not None:
            # Fail before spending GPU time if the report cannot be written.
            report_fd = os.open(
                report,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
    except (InferenceBenchmarkError, InferenceCalibrationError, OSError) as exc:
        err_console.print(f"[bold red]Benchmark error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    result = BenchmarkReport(config={})
    exit_code = 0
    try:
        run_benchmark(config, on_level=on_level, report=result)
    except (InferenceBenchmarkError, InferenceObserveError, OSError) as exc:
        result.error, result.stopped_early, exit_code = str(exc), True, 2
        err_console.print(f"[bold red]Benchmark error:[/] {exc}")
    except KeyboardInterrupt:
        result.error, result.stopped_early, exit_code = "interrupted", True, 130
        err_console.print("[bold red]Interrupted;[/] completed levels are kept.")
    finally:
        if report_fd is not None:
            try:
                write_report(result)
            finally:
                os.close(report_fd)

    recorded = sum(1 for level in result.levels if level.verdict == "recorded")
    log_security_event(
        "INFER_BENCHMARK_INVOCATION",
        {"mode": config.mode, "levels": len(result.levels), "recorded": recorded},
    )
    info_console.print(
        f"{recorded}/{len(result.levels)} level(s) recorded"
        + (" · stopped early" if result.stopped_early else "")
    )
    if exit_code:
        raise typer.Exit(code=exit_code)
