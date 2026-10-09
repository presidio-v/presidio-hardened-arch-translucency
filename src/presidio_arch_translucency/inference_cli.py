"""
CLI surface for the LLM inference serving domain (`pat infer-analyze`,
`pat infer-what-if`). Kept out of ``cli.py`` (registered there, like
``demo``) so the inference profile stays one self-contained module pair.

All output is modelled (ADR-0012 Phase 0): every table carries an
"uncalibrated" notice until `pat infer-calibrate` lands.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

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


def _bw_eff_option() -> float:
    return typer.Option(
        DEFAULT_ENGINE_PARAMS.bandwidth_efficiency,
        "--bandwidth-efficiency",
        help="Achieved fraction of spec bandwidth (placeholder 0.6).",
    )


def _step_overhead_option() -> float:
    return typer.Option(
        DEFAULT_ENGINE_PARAMS.step_overhead_ms,
        "--step-overhead-ms",
        help="Fixed per-decode-step cost t₀ in ms (placeholder 2.0).",
    )


def _prefill_rate_option() -> float:
    return typer.Option(
        DEFAULT_ENGINE_PARAMS.prefill_tokens_per_s_per_gpu,
        "--prefill-tokens-per-s",
        help="Prefill throughput per GPU R_pre (placeholder 20000).",
    )


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
    bandwidth_efficiency: float,
    step_overhead_ms: float,
    prefill_rate: float,
) -> tuple[InferenceWorkload, ModelSpec, GpuSpec, EngineSpec, EngineParams]:
    return (
        InferenceWorkload(rps, prompt_tokens, output_tokens),
        ModelSpec(weights_gb, kv_bytes),
        GpuSpec(
            gpus, gpu_memory_gb, gpu_bandwidth_gbs, gpus_per_node, memory_utilization
        ),
        EngineSpec(max_num_seqs, tp_degrees),
        EngineParams(bandwidth_efficiency, step_overhead_ms, prefill_rate),
    )


def _fmt(value: float | None, spec: str) -> str:
    return "—" if value is None else format(value, spec)


def _config_json(c: ConfigResult) -> dict:
    data = asdict(c)
    data["strategy"] = c.strategy.value
    return data


# -- rendering -------------------------------------------------------------------


def _render_analysis(analysis: InferenceAnalysis, show_all: bool) -> None:
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
            f"{c.tp}×{c.instances}",
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
    console.print(_UNCALIBRATED_NOTE)


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
    bandwidth_efficiency: float = _bw_eff_option(),
    step_overhead_ms: float = _step_overhead_option(),
    prefill_tokens_per_s: float = _prefill_rate_option(),
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
        workload, model, gpu, engine, params = _build_inputs(
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
            bandwidth_efficiency,
            step_overhead_ms,
            prefill_tokens_per_s,
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
    except InferenceDomainError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
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
                    "calibrated": False,
                    "ttft_p99_basis": "prefill + M/M/c wait; no interference",
                    "recommended": _config_json(rec) if rec else None,
                    "best_effort": (
                        _config_json(analysis.best_effort)
                        if analysis.best_effort
                        else None
                    ),
                    "baseline": (
                        _config_json(analysis.baseline) if analysis.baseline else None
                    ),
                    "configs": [_config_json(c) for c in analysis.configs],
                },
                separators=(",", ":"),
            )
        )
        return
    _render_analysis(analysis, show_all=show_all)


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
    bandwidth_efficiency: float = _bw_eff_option(),
    step_overhead_ms: float = _step_overhead_option(),
    prefill_tokens_per_s: float = _prefill_rate_option(),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
) -> None:
    """Evaluate one (tp, instances) inference configuration.

    Fail-closed: an unsupported tp, tp beyond --gpus-per-node, or any
    out-of-domain number is rejected (exit 2), never reported as feasible.
    """
    try:
        workload, model, gpu, engine, params = _build_inputs(
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
            bandwidth_efficiency,
            step_overhead_ms,
            prefill_tokens_per_s,
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
    except InferenceDomainError as exc:
        err_console.print(f"[bold red]Input validation error:[/] {exc}")
        raise typer.Exit(code=2) from exc

    log_security_event(
        "INFER_WHAT_IF_INVOCATION",
        {"strategy": c.strategy.value, "tp": c.tp, "instances": c.instances},
    )
    if as_json:
        payload = {"modelled": True, "calibrated": False, **_config_json(c)}
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
    console.print(_UNCALIBRATED_NOTE)
