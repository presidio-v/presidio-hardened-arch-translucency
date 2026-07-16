"""
Prometheus rules emitter -- v0.11.0 ("Alert").

Second step of the monitoring-integration arc. ``pat rules`` emits a Prometheus
rule file (recording + alerting rules) derived from the metrics the v0.10.0
exporter publishes, so the model's signals become actionable inside the existing
Prometheus/Alertmanager pipeline:

    pat rules --current-layer container --cost-budget 0.000001 > pat-rules.yml
    # then reference pat-rules.yml from prometheus.yml `rule_files:`

Emit-only (arc invariant A1): ``pat`` produces declarative YAML and never loads,
applies, or reloads anything itself.

Security: the only user-supplied values that reach the YAML are a layer name
(validated against the four known layers), numeric thresholds (rendered as
numbers), and a Prometheus duration (validated against ``\\d+[smhdw]``). Every
string scalar is double-quoted with ``\\`` and ``"`` escaped, so the emitted
rule file is always valid and cannot smuggle arbitrary content. Built by hand --
no PyYAML dependency (matching ``hpa_patch``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

VALID_LAYERS: tuple[str, ...] = ("container", "pod", "deployment", "node")

DEFAULT_SURGE_RATIO = 1.2
DEFAULT_TREND_THRESHOLD = 0.2
DEFAULT_FOR = "10m"
_ABSENT_FOR = "5m"

_DURATION_RE = re.compile(r"^\d+[smhdw]$")


class RuleError(ValueError):
    """Raised on an invalid layer, threshold, or duration."""


@dataclass(frozen=True)
class Rule:
    """A single recording or alerting rule."""

    record: str | None = None
    alert: str | None = None
    expr: str = ""
    for_: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleGroup:
    """A named group of rules."""

    name: str
    rules: list[Rule]


# -- validation ----------------------------------------------------------------


def _validate_layer(layer: str) -> str:
    if layer not in VALID_LAYERS:
        raise RuleError(f"layer {layer!r} is not one of: {', '.join(VALID_LAYERS)}")
    return layer


def _validate_duration(value: str, field_name: str) -> str:
    if not _DURATION_RE.match(value):
        raise RuleError(
            f"{field_name} {value!r} is not a Prometheus duration (e.g. 30s, 10m, 1h)"
        )
    return value


def _num(value: float) -> str:
    """Render a threshold as a compact, PromQL-valid number."""
    return f"{value:g}"


# -- rule construction ---------------------------------------------------------


def build_recording_group() -> RuleGroup:
    """Recording rules that normalise the exporter's metrics for alerting."""
    return RuleGroup(
        name="pat.recording",
        rules=[
            Rule(record="pat:predicted_rps", expr="max(pat_predicted_rps)"),
            Rule(record="pat:observed_rps", expr="max(pat_observed_rps)"),
            Rule(
                record="pat:demand_growth_ratio",
                expr="max(pat_predicted_rps) / clamp_min(max(pat_observed_rps), 1)",
            ),
            Rule(record="pat:trend_ratio", expr="max(pat_optimize_trend_ratio)"),
        ],
    )


def build_alert_group(
    current_layer: str | None = None,
    cost_budget: float | None = None,
    surge_ratio: float = DEFAULT_SURGE_RATIO,
    trend_threshold: float = DEFAULT_TREND_THRESHOLD,
    for_duration: str = DEFAULT_FOR,
    energy_budget: float | None = None,
    energy: bool = False,
) -> RuleGroup:
    """
    Alerting rules over the recording rules and raw exporter metrics.

    Always emits: a demand-surge forecast, a demand-trend warning, and an
    exporter-absent (scrape health) alert. Emits a layer-translucency-mismatch
    alert only when *current_layer* is given, and a cost-budget alert only when
    *cost_budget* is given (the cost metric exists only under
    ``--cost-per-replica-hour``).

    Energy alerts (v0.21.0) form an optional group, gated exactly like the cost
    alert: when *energy_budget* is given OR *energy* is set,
    ``PatIdleEnergyWaste`` is always included, and ``PatEnergyPerRequestOverBudget``
    additionally when a budget is supplied. Both fire on the exporter's MODELLED
    energy gauges (the annotations say so). Without either flag the emitted rule
    file is byte-identical to the pre-v0.21 output.
    """
    _validate_duration(for_duration, "--for")
    rules: list[Rule] = [
        Rule(
            alert="PatDemandSurgeForecast",
            expr=f"pat:demand_growth_ratio > {_num(surge_ratio)}",
            for_=for_duration,
            labels={"severity": "warning"},
            annotations={
                "summary": "Forecast demand exceeds current observed demand",
                "description": (
                    "pat forecasts demand at {{ $value | humanize }}x the current "
                    f"observed level (> {_num(surge_ratio)}x) for {for_duration} -- "
                    "scale-up likely needed soon."
                ),
            },
        ),
        Rule(
            alert="PatDemandTrendRising",
            expr=f"pat:trend_ratio > {_num(trend_threshold)}",
            for_=for_duration,
            labels={"severity": "info"},
            annotations={
                "summary": "Demand trending up across the observation window",
                "description": (
                    "pat demand trend is {{ $value | humanizePercentage }} "
                    f"(> {_num(trend_threshold)}) for {for_duration}."
                ),
            },
        ),
        Rule(
            alert="PatExporterAbsent",
            expr="absent(pat_build_info)",
            for_=_ABSENT_FOR,
            labels={"severity": "critical"},
            annotations={
                "summary": "pat exporter is not being scraped",
                "description": (
                    "No pat_build_info series for "
                    f"{_ABSENT_FOR} -- the pat exporter target is down or "
                    "misconfigured."
                ),
            },
        ),
    ]

    if current_layer is not None:
        layer = _validate_layer(current_layer)
        rules.append(
            Rule(
                alert="PatLayerTranslucencyMismatch",
                expr=f'pat_layer_recommended{{layer="{layer}"}} == 0',
                for_=for_duration,
                labels={"severity": "warning"},
                annotations={
                    "summary": "Running on a layer pat no longer recommends",
                    "description": (
                        f"pat no longer recommends the {layer!r} layer for "
                        f"{for_duration}; another layer scores higher. Review "
                        "`pat analyze`."
                    ),
                },
            )
        )

    if cost_budget is not None:
        if cost_budget < 0:
            raise RuleError("--cost-budget must be >= 0")
        rules.append(
            Rule(
                alert="PatCostPerRequestOverBudget",
                expr=f"pat_cost_per_request > {_num(cost_budget)}",
                for_=for_duration,
                labels={"severity": "warning"},
                annotations={
                    "summary": "Cost per request over budget",
                    "description": (
                        "pat_cost_per_request for {{ $labels.layer }} is "
                        "{{ $value }} USD/request (budget "
                        f"{_num(cost_budget)}) for {for_duration}."
                    ),
                },
            )
        )

    if energy_budget is not None or energy:
        rules.extend(
            _energy_alert_rules(energy_budget=energy_budget, for_duration=for_duration)
        )

    return RuleGroup(name="pat.alerts", rules=rules)


def _energy_alert_rules(energy_budget: float | None, for_duration: str) -> list[Rule]:
    """The optional energy alert group (v0.21.0).

    ``PatIdleEnergyWaste`` is always present in the group; the over-budget alert
    joins it only when a budget is supplied. Both alert on MODELLED energy
    gauges — the annotations state this so an operator never mistakes a model
    output for a signed measurement (ADR-0011 E1a).

    ``PatIdleEnergyWaste`` expresses "standing energy with falling demand:
    replicas exceed what demand needs" using the metric pair the exporter
    actually exposes. There is no running-replica-count metric (the exporter is
    emit-only and models a supplied workload), so we compose the *prediction vs
    recommendation* pair: modelled power is being drawn while the demand forecast
    recommends fewer replicas than the current analytic recommendation. The
    forecast series exist only under ``pat export --predict``, so the alert is
    silent (no false positive) when forecasting is off.
    """
    rules: list[Rule] = []
    if energy_budget is not None:
        if energy_budget <= 0:
            raise RuleError("--energy-budget must be > 0")
        rules.append(
            Rule(
                alert="PatEnergyPerRequestOverBudget",
                expr=f"pat_energy_per_request_joules > {_num(energy_budget)}",
                for_=for_duration,
                labels={"severity": "warning"},
                annotations={
                    "summary": "Energy per request over budget (modelled)",
                    "description": (
                        "pat_energy_per_request_joules for {{ $labels.layer }} is "
                        "{{ $value }} J/request (budget "
                        f"{_num(energy_budget)} J/request, modelled by the analytic "
                        f"energy model) for {for_duration}."
                    ),
                },
            )
        )
    rules.append(
        Rule(
            alert="PatIdleEnergyWaste",
            expr=(
                "pat_power_watts > 0 and "
                "pat_predicted_recommended_replicas < pat_recommended_replicas"
            ),
            for_=for_duration,
            labels={"severity": "warning"},
            annotations={
                "summary": "Standing energy with falling demand",
                "description": (
                    "pat draws modelled power for {{ $labels.layer }} while the "
                    "demand forecast recommends fewer replicas than the current "
                    f"recommendation for {for_duration}: replicas exceed what "
                    "demand needs. Consider scaling in to reclaim idle energy."
                ),
            },
        )
    )
    return rules


def build_rule_groups(
    current_layer: str | None = None,
    cost_budget: float | None = None,
    surge_ratio: float = DEFAULT_SURGE_RATIO,
    trend_threshold: float = DEFAULT_TREND_THRESHOLD,
    for_duration: str = DEFAULT_FOR,
    energy_budget: float | None = None,
    energy: bool = False,
) -> list[RuleGroup]:
    """Build the full recording + alerting rule groups."""
    return [
        build_recording_group(),
        build_alert_group(
            current_layer=current_layer,
            cost_budget=cost_budget,
            surge_ratio=surge_ratio,
            trend_threshold=trend_threshold,
            for_duration=for_duration,
            energy_budget=energy_budget,
            energy=energy,
        ),
    ]


# -- YAML rendering (hand-rolled, every scalar double-quoted/escaped) -----------


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_rules_yaml(groups: list[RuleGroup]) -> str:
    """Render *groups* as a Prometheus rule file (YAML string)."""
    lines = [
        "# Generated by `pat rules`. Review before loading.",
        "# Reference from prometheus.yml `rule_files:`; alerts route via Alertmanager.",
        "groups:",
    ]
    for group in groups:
        lines.append(f"  - name: {_q(group.name)}")
        lines.append("    rules:")
        for rule in group.rules:
            if rule.record is not None:
                lines.append(f"      - record: {_q(rule.record)}")
                lines.append(f"        expr: {_q(rule.expr)}")
                continue
            lines.append(f"      - alert: {_q(rule.alert or '')}")
            lines.append(f"        expr: {_q(rule.expr)}")
            if rule.for_ is not None:
                lines.append(f"        for: {_q(rule.for_)}")
            if rule.labels:
                lines.append("        labels:")
                for key, val in rule.labels.items():
                    lines.append(f"          {key}: {_q(str(val))}")
            if rule.annotations:
                lines.append("        annotations:")
                for key, val in rule.annotations.items():
                    lines.append(f"          {key}: {_q(str(val))}")
    return "\n".join(lines) + "\n"
