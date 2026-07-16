"""Tests for the energy/carbon budgeting solver + `pat budget` (v0.22.0)."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.budget import (
    BudgetReport,
    solve_energy_budget,
    solve_min_energy,
)
from presidio_arch_translucency.calibrate import (
    Observation,
    fit_calibration,
    global_model_path,
    write_model_file,
)
from presidio_arch_translucency.carbon import (
    grams_per_hour,
    grams_per_request,
)
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.energy import power_watts, resolve_energy_params
from presidio_arch_translucency.model import (
    LAYER_PARAMS,
    base_capacity_rps,
    resolve_concurrency,
    throughput,
)

runner = CliRunner()
_WATTS = 15.0


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".pat").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def _json_out(output: str) -> dict:
    """Extract the JSON blob (the uncalibrated warning precedes it on stderr)."""
    return json.loads(output[output.index("{") :])


def _brute_max_delta(layer, rps, lat, budget_wh, window_h):
    """Independently compute the max-throughput feasible δ for one layer."""
    base_cap = base_capacity_rps(rps, lat, resolve_concurrency(None))
    idle_w, dyn, beta, _ = resolve_energy_params(layer, _WATTS, base_cap, None)
    dyn = dyn if dyn is not None else 0.0
    best = None
    for delta in range(1, LAYER_PARAMS[layer].max_replicas + 1):
        omega = throughput(rps, delta, layer, base_cap)
        watts = power_watts(delta, omega, idle_w, dyn, beta)
        if watts * window_h <= budget_wh and (best is None or omega > best[1] + 1e-9):
            best = (delta, omega)
    return best


# ── Direction 2: minimum energy meeting demand ────────────────────────────────


def test_min_energy_all_layers_feasible() -> None:
    report = solve_min_energy(500, 80, _WATTS)
    assert report.direction == "min-energy"
    assert len(report.layers) == 4
    assert all(r.feasible for r in report.layers)
    assert report.recommended is not None


def test_min_energy_recommends_lowest_wh() -> None:
    report = solve_min_energy(500, 80, _WATTS)
    rec = next(r for r in report.layers if r.layer == report.recommended)
    # No layer meets demand for fewer watt-hours than the recommended one.
    assert rec.energy_wh == pytest.approx(min(r.energy_wh for r in report.layers))


def test_min_energy_reports_eei_and_jreq() -> None:
    report = solve_min_energy(500, 80, _WATTS)
    for r in report.layers:
        assert r.joules_per_request is not None and r.joules_per_request > 0
        assert r.eei is not None
        assert r.headroom_wh is None  # Direction 2 has no budget headroom


def test_min_energy_window_scales_energy_linearly() -> None:
    one = solve_min_energy(500, 80, _WATTS, window_h=1.0)
    two = solve_min_energy(500, 80, _WATTS, window_h=2.0)
    r1 = next(r for r in one.layers if r.layer == one.layers[0].layer)
    r2 = next(r for r in two.layers if r.layer == one.layers[0].layer)
    assert r2.energy_wh == pytest.approx(2 * r1.energy_wh)


# ── Direction 1: max output within a budget ───────────────────────────────────


def test_energy_budget_matches_brute_force() -> None:
    budget, window = 40.0, 1.0
    report = solve_energy_budget(500, 80, _WATTS, budget_wh=budget, window_h=window)
    for r in report.layers:
        expected = _brute_max_delta(r.layer, 500, 80, budget, window)
        if expected is None:
            assert not r.feasible
        else:
            assert r.feasible
            assert r.replicas == expected[0]
            assert r.throughput_rps == pytest.approx(round(expected[1], 2))


def test_energy_budget_headroom_nonnegative_for_feasible() -> None:
    report = solve_energy_budget(500, 80, _WATTS, budget_wh=60.0, window_h=1.0)
    for r in report.layers:
        if r.feasible:
            assert r.headroom_wh is not None and r.headroom_wh >= -1e-9
            assert r.energy_wh <= 60.0 + 1e-9


def test_energy_budget_recommend_highest_throughput() -> None:
    report = solve_energy_budget(500, 80, _WATTS, budget_wh=100.0, window_h=1.0)
    rec = next(r for r in report.layers if r.layer == report.recommended)
    feasible = [r for r in report.layers if r.feasible]
    assert rec.throughput_rps == pytest.approx(max(r.throughput_rps for r in feasible))


def test_tiny_budget_marks_layers_infeasible() -> None:
    # A budget smaller than any single-replica node draw → node infeasible.
    report = solve_energy_budget(500, 80, _WATTS, budget_wh=0.5, window_h=1.0)
    node = next(r for r in report.layers if r.layer.value == "node")
    assert not node.feasible
    # Infeasible layers are excluded from the recommendation.
    if report.recommended is not None:
        assert report.recommended != node.layer


def test_all_infeasible_yields_no_recommendation() -> None:
    report = solve_energy_budget(500, 80, _WATTS, budget_wh=1e-6, window_h=1.0)
    assert all(not r.feasible for r in report.layers)
    assert report.recommended is None


# ── Carbon columns ────────────────────────────────────────────────────────────


def test_carbon_columns_match_conversions() -> None:
    report = solve_min_energy(
        500, 80, _WATTS, intensity_g_per_kwh=300.0, intensity_source="static"
    )
    for r in report.layers:
        watts = r.energy_wh / report.window_h
        assert r.grams_per_window == pytest.approx(grams_per_hour(watts, 300.0))
        if r.joules_per_request is not None:
            assert r.grams_per_request == pytest.approx(
                grams_per_request(r.joules_per_request, 300.0)
            )


def test_no_region_leaves_carbon_columns_none() -> None:
    report = solve_min_energy(500, 80, _WATTS)
    assert all(r.grams_per_request is None for r in report.layers)
    assert all(r.grams_per_window is None for r in report.layers)


# ── CLI: rendering, JSON, validation ──────────────────────────────────────────


def test_cli_direction2_default() -> None:
    result = _invoke("budget", "-r", "500", "-l", "80")
    assert result.exit_code == 0
    assert "Minimum energy meeting demand" in result.output
    assert "Recommended layer" in result.output


def test_cli_direction1_energy_budget() -> None:
    result = _invoke(
        "budget", "-r", "500", "-l", "80", "--energy-budget-wh", "40", "--window-h", "1"
    )
    assert result.exit_code == 0
    assert "Direction 1" in result.output


def test_cli_json_shape() -> None:
    result = _invoke("budget", "-r", "500", "-l", "80", "--json")
    assert result.exit_code == 0
    data = _json_out(result.output)
    assert data["direction"] == "min-energy"
    assert len(data["layers"]) == 4
    assert {"layer", "replicas", "energy_wh", "joules_per_request"} <= set(
        data["layers"][0]
    )
    assert "commitment" in data


def test_cli_json_includes_intensity_source_with_region() -> None:
    result = _invoke(
        "budget", "-r", "500", "-l", "80", "--region", "eu-central-1", "--json"
    )
    data = _json_out(result.output)
    assert data["intensity_g_per_kwh"] == pytest.approx(345.0)
    assert data["intensity_source"] == "static"
    assert data["layers"][0]["grams_per_request"] is not None


def test_cli_carbon_budget_conversion_roundtrip() -> None:
    # budget_wh = grams / intensity × 1000. eu-central-1 static = 345.
    result = _invoke(
        "budget",
        "-r",
        "500",
        "-l",
        "80",
        "--carbon-budget-g",
        "3300",
        "--region",
        "eu-central-1",
        "--json",
    )
    assert result.exit_code == 0
    data = _json_out(result.output)
    assert data["carbon_budget_g"] == pytest.approx(3300.0)
    assert data["budget_wh"] == pytest.approx(3300.0 / 345.0 * 1000.0)


def test_cli_mutual_exclusion() -> None:
    result = _invoke(
        "budget",
        "-r",
        "500",
        "-l",
        "80",
        "--energy-budget-wh",
        "40",
        "--carbon-budget-g",
        "100",
        "--region",
        "eu-central-1",
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_cli_carbon_budget_requires_region() -> None:
    result = _invoke("budget", "-r", "500", "-l", "80", "--carbon-budget-g", "100")
    assert result.exit_code == 2
    assert "region" in result.output.lower()


def test_cli_unknown_region_fails_closed() -> None:
    result = _invoke("budget", "-r", "500", "-l", "80", "--region", "atlantis-1")
    assert result.exit_code == 2
    assert "unknown region" in result.output.lower()


def test_cli_carbon_budget_zero_intensity_raises_carbon_error(monkeypatch) -> None:
    # Belt-and-suspenders (FIX 1c): a forced non-positive intensity must surface
    # as a clean CarbonError exit 2, never a ZeroDivisionError traceback. The
    # command's local `from carbon import resolve_carbon_intensity` reads the
    # module attribute at call time, so patching it on the module takes effect.
    from presidio_arch_translucency import carbon

    monkeypatch.setattr(
        carbon, "resolve_carbon_intensity", lambda region: (0.0, "static")
    )
    result = _invoke(
        "budget",
        "-r",
        "500",
        "-l",
        "80",
        "--carbon-budget-g",
        "100",
        "--region",
        "eu-central-1",
    )
    assert result.exit_code == 2
    assert "intensity is not positive" in result.output.lower()
    assert not isinstance(result.exception, ZeroDivisionError)


def test_cli_region_markup_is_escaped() -> None:
    # FIX 2: the CarbonError message embeds --region; Rich markup in it must
    # render as literal text, never as console styling.
    result = _invoke("budget", "-r", "500", "-l", "80", "--region", "[blink]x[/]")
    assert result.exit_code == 2
    assert "[blink]x[/]" in result.output


@pytest.mark.parametrize("window", ["0", "9000"])
def test_cli_window_h_bounds(window: str) -> None:
    result = _invoke("budget", "-r", "500", "-l", "80", "--window-h", window)
    assert result.exit_code == 2


def test_cli_energy_budget_must_be_positive() -> None:
    result = _invoke("budget", "-r", "500", "-l", "80", "--energy-budget-wh", "0")
    assert result.exit_code == 2


# ── commitment gate honoured (fail closed on tamper) ──────────────────────────


def _fit():
    return fit_calibration(
        [
            Observation(rps=100, latency_ms=50, replicas=2),
            Observation(rps=300, latency_ms=80, replicas=5),
        ]
    )


def test_cli_fails_closed_on_tampered_model() -> None:
    write_model_file(_fit())
    path = global_model_path()
    data = json.loads(path.read_text())
    data["concurrency"] = 999.0
    path.write_text(json.dumps(data))

    result = _invoke("budget", "-r", "500", "-l", "80")
    assert result.exit_code == 2
    assert "tamper" in result.output.lower()


def test_cli_reports_commitment_on_clean_model() -> None:
    write_model_file(_fit())
    result = _invoke("budget", "-r", "500", "-l", "80")
    assert result.exit_code == 0
    assert "commitment" in result.output.lower()


def test_report_is_budgetreport() -> None:
    assert isinstance(solve_min_energy(500, 80, _WATTS), BudgetReport)
