"""Tests for the Prometheus rules emitter (`pat rules`, v0.11.0)."""

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.rules import (
    Rule,
    RuleError,
    RuleGroup,
    build_alert_group,
    build_recording_group,
    build_rule_groups,
    render_rules_yaml,
)

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def _alert_names(group: RuleGroup) -> set[str]:
    return {r.alert for r in group.rules if r.alert is not None}


# ── recording group ───────────────────────────────────────────────────────────


def test_recording_group_records() -> None:
    group = build_recording_group()
    records = {r.record for r in group.rules}
    assert {
        "pat:predicted_rps",
        "pat:observed_rps",
        "pat:demand_growth_ratio",
        "pat:trend_ratio",
    } == records


# ── alert group ───────────────────────────────────────────────────────────────


def test_alert_group_defaults_only() -> None:
    group = build_alert_group()
    assert _alert_names(group) == {
        "PatDemandSurgeForecast",
        "PatDemandTrendRising",
        "PatExporterAbsent",
    }


def test_alert_group_with_layer_adds_mismatch() -> None:
    group = build_alert_group(current_layer="container")
    assert "PatLayerTranslucencyMismatch" in _alert_names(group)
    mismatch = next(r for r in group.rules if r.alert == "PatLayerTranslucencyMismatch")
    assert 'pat_layer_recommended{layer="container"} == 0' == mismatch.expr


def test_alert_group_with_cost_adds_cost_alert() -> None:
    group = build_alert_group(cost_budget=1e-06)
    cost = next(r for r in group.rules if r.alert == "PatCostPerRequestOverBudget")
    assert "pat_cost_per_request > 1e-06" == cost.expr


def test_alert_group_surge_and_trend_thresholds_in_expr() -> None:
    group = build_alert_group(surge_ratio=1.5, trend_threshold=0.3)
    surge = next(r for r in group.rules if r.alert == "PatDemandSurgeForecast")
    trend = next(r for r in group.rules if r.alert == "PatDemandTrendRising")
    assert surge.expr == "pat:demand_growth_ratio > 1.5"
    assert trend.expr == "pat:trend_ratio > 0.3"


def test_alert_group_invalid_layer_raises() -> None:
    with pytest.raises(RuleError):
        build_alert_group(current_layer="bogus")


def test_alert_group_invalid_duration_raises() -> None:
    with pytest.raises(RuleError):
        build_alert_group(for_duration="soon")


def test_alert_group_negative_cost_budget_raises() -> None:
    with pytest.raises(RuleError):
        build_alert_group(cost_budget=-1.0)


# ── energy alert group (v0.21.0) ──────────────────────────────────────────────


def test_default_alert_group_has_no_energy_rules() -> None:
    names = _alert_names(build_alert_group())
    assert "PatIdleEnergyWaste" not in names
    assert "PatEnergyPerRequestOverBudget" not in names


def test_energy_budget_adds_both_energy_alerts() -> None:
    names = _alert_names(build_alert_group(energy_budget=0.5))
    assert "PatEnergyPerRequestOverBudget" in names
    assert "PatIdleEnergyWaste" in names


def test_energy_flag_adds_only_idle_alert() -> None:
    names = _alert_names(build_alert_group(energy=True))
    assert "PatIdleEnergyWaste" in names
    assert "PatEnergyPerRequestOverBudget" not in names


def test_energy_budget_expr_and_modelled_annotation() -> None:
    group = build_alert_group(energy_budget=0.25)
    over = next(r for r in group.rules if r.alert == "PatEnergyPerRequestOverBudget")
    assert over.expr == "pat_energy_per_request_joules > 0.25"
    assert "modelled" in over.annotations["description"]
    assert "0.25" in over.annotations["description"]


def test_idle_energy_waste_uses_real_metrics() -> None:
    group = build_alert_group(energy=True)
    idle = next(r for r in group.rules if r.alert == "PatIdleEnergyWaste")
    assert "pat_power_watts > 0" in idle.expr
    assert "pat_predicted_recommended_replicas < pat_recommended_replicas" in idle.expr


def test_zero_energy_budget_raises() -> None:
    with pytest.raises(RuleError, match="must be > 0"):
        build_alert_group(energy_budget=0.0)


def test_negative_energy_budget_raises() -> None:
    with pytest.raises(RuleError):
        build_alert_group(energy_budget=-1.0)


def test_energy_rules_yaml_round_trips() -> None:
    text = render_rules_yaml(build_rule_groups(energy_budget=0.5))
    assert 'alert: "PatEnergyPerRequestOverBudget"' in text
    assert 'alert: "PatIdleEnergyWaste"' in text
    assert "pat_energy_per_request_joules > 0.5" in text


def test_default_rules_yaml_unchanged_without_energy() -> None:
    # The pre-v0.21 default output must be byte-stable (chart bundle sync).
    text = render_rules_yaml(build_rule_groups())
    assert "PatIdleEnergyWaste" not in text
    assert "PatEnergyPerRequestOverBudget" not in text


def test_cli_energy_budget_renders_yaml() -> None:
    result = invoke("rules", "--energy-budget", "0.5")
    assert result.exit_code == 0
    assert "PatEnergyPerRequestOverBudget" in result.output
    assert "PatIdleEnergyWaste" in result.output


def test_cli_energy_flag_only_idle() -> None:
    result = invoke("rules", "--energy")
    assert result.exit_code == 0
    assert "PatIdleEnergyWaste" in result.output
    assert "PatEnergyPerRequestOverBudget" not in result.output


def test_cli_zero_energy_budget_errors() -> None:
    result = invoke("rules", "--energy-budget", "0")
    assert result.exit_code == 2


def test_cli_nan_energy_budget_rejected() -> None:
    result = invoke("rules", "--energy-budget", "nan")
    assert result.exit_code == 2


def test_render_minimal_alert_omits_optional_blocks() -> None:
    text = render_rules_yaml(
        [RuleGroup(name="g", rules=[Rule(alert="A", expr="up == 0")])]
    )
    assert '      - alert: "A"' in text
    assert "        for:" not in text
    assert "        labels:" not in text
    assert "        annotations:" not in text


def test_exporter_absent_uses_fixed_short_for() -> None:
    absent = next(
        r for r in build_alert_group().rules if r.alert == "PatExporterAbsent"
    )
    assert absent.for_ == "5m"
    assert absent.labels["severity"] == "critical"


# ── render_rules_yaml ─────────────────────────────────────────────────────────


def test_render_quotes_and_escapes() -> None:
    group = RuleGroup(
        name="g",
        rules=[Rule(alert="A", expr='x{a="b\\c"} == 0', for_="10m")],
    )
    text = render_rules_yaml([group])
    # Inner quotes/backslashes escaped inside a double-quoted YAML scalar.
    assert r'expr: "x{a=\"b\\c\"} == 0"' in text


def test_render_full_document_shape() -> None:
    text = render_rules_yaml(build_rule_groups())
    assert text.startswith("# Generated by `pat rules`")
    assert "groups:" in text
    assert '  - name: "pat.recording"' in text
    assert '  - name: "pat.alerts"' in text
    assert '      - record: "pat:predicted_rps"' in text
    assert '      - alert: "PatExporterAbsent"' in text
    # Every expr scalar is quoted.
    for line in text.splitlines():
        if line.strip().startswith("expr:"):
            assert line.strip().removeprefix("expr:").strip().startswith('"')


# ── pat rules CLI ─────────────────────────────────────────────────────────────


def test_rules_cmd_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke("rules")
    assert result.exit_code == 0, result.output
    assert "groups:" in result.output
    assert "PatDemandSurgeForecast" in result.output
    assert "PatExporterAbsent" in result.output
    assert "PatLayerTranslucencyMismatch" not in result.output
    assert "PatCostPerRequestOverBudget" not in result.output


def test_rules_cmd_with_layer(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke("rules", "-c", "container")
    assert result.exit_code == 0, result.output
    assert "PatLayerTranslucencyMismatch" in result.output
    assert r"layer=\"container\"" in result.output


def test_rules_cmd_with_cost_budget(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke("rules", "--cost-budget", "0.000001")
    assert result.exit_code == 0, result.output
    assert "PatCostPerRequestOverBudget" in result.output
    assert "pat_cost_per_request > 1e-06" in result.output


def test_rules_cmd_bad_duration_exits_2(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke("rules", "--for", "soon")
    assert result.exit_code == 2
    assert "duration" in result.output.lower()


def test_rules_cmd_bad_layer_exits_2(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = invoke("rules", "-c", "bogus")
    assert result.exit_code == 2
