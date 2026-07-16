"""Tests for `pat what-if --energy-aware` (v0.22.0)."""

from __future__ import annotations

from typer.testing import CliRunner

from presidio_arch_translucency.cli import app

runner = CliRunner()


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


_BASE = ("what-if", "-r", "300", "-s", "900", "-l", "80", "-c", "node")


# ── without the flag: output unchanged (strongest available pin) ──────────────
#
# `--energy-aware` is a single-name bool flag (`typer.Option(False,
# "--energy-aware")`), so Typer generates no `--no-energy-aware` spelling. The
# scaler pin's explicit==legacy equality (an explicit "off" spelling proven byte-
# equal to the default) is therefore not expressible for the gate itself, and two
# default runs being byte-equal only proves determinism — not that the v0.22
# energy/carbon code paths stayed un-entered. So we pin the strongest available
# combination: (1) NONE of the energy/carbon output markers appear, (2) the run
# is deterministic, and (3) spelling every value-flag default explicitly — the
# closest analogue to explicit==legacy here — leaves the output byte-identical
# (the flag is inert while the gate is off).

# Every string the v0.22 energy-aware / carbon path can emit. None may leak into
# the default output.
# NB: bare "TROUGH" is excluded — it also labels a base time-series segment;
# "Trough cost" below is the energy-aware-only variant.
_ENERGY_MARKERS = (
    "Idle energy vs trough",
    "STANDING ENERGY",
    "Standing E",
    "Standing CO₂",
    "Trough cost",
    "gCO₂",
    "gCO₂eq/kWh",
    "greener, cheaper",
    "warm replicas cost more",
    "trough loses more in revenue",
    "Add --cost-per-request",
    "static 2023 average",
)


def test_without_flag_no_energy_aware_markers() -> None:
    result = _invoke(*_BASE)
    assert result.exit_code == 0
    for marker in _ENERGY_MARKERS:
        assert marker not in result.output, marker


def test_without_flag_output_deterministic() -> None:
    # The default what-if output must not shift when the new flags default off.
    a = _invoke(*_BASE).output
    b = _invoke(*_BASE).output
    assert a == b
    assert "HPA Scale Event" in a


def test_value_flag_default_is_inert_while_gate_off() -> None:
    # Explicit==legacy analogue: spelling --electricity-cost-per-kwh's default
    # while --energy-aware is absent must not perturb a single byte of output
    # (the flag is read only inside the energy-aware branch). --cost-per-request
    # is deliberately not spelled here: it affects the base render independently
    # of the gate, so it has no inert default.
    base = _invoke(*_BASE).output
    spelled = _invoke(*_BASE, "--electricity-cost-per-kwh", "0.12").output
    assert spelled == base


# ── with the flag: both sides + verdict ───────────────────────────────────────


def test_energy_aware_renders_both_sides() -> None:
    result = _invoke(*_BASE, "--energy-aware", "--cost-per-request", "0.001")
    assert result.exit_code == 0
    assert "Idle energy vs trough" in result.output
    assert "STANDING ENERGY" in result.output
    assert "TROUGH" in result.output


def test_missing_cost_renders_energy_side_and_notice() -> None:
    result = _invoke(*_BASE, "--energy-aware")
    assert result.exit_code == 0
    assert "STANDING ENERGY" in result.output
    assert "Add --cost-per-request" in result.output


# ── the FLIP: verdict swings across the two sides ─────────────────────────────


def test_verdict_trough_wins_when_requests_valuable() -> None:
    result = _invoke(
        *_BASE,
        "--energy-aware",
        "--cost-per-request",
        "1.0",
        "--electricity-cost-per-kwh",
        "0.1",
    )
    assert result.exit_code == 0
    assert "trough loses more in revenue" in result.output.lower()
    assert "justified" in result.output.lower()


def test_verdict_standing_wins_when_energy_dear() -> None:
    result = _invoke(
        *_BASE,
        "--energy-aware",
        "--cost-per-request",
        "0.000000000001",
        "--electricity-cost-per-kwh",
        "10",
    )
    assert result.exit_code == 0
    assert "warm replicas cost more" in result.output.lower()
    assert "greener, cheaper" in result.output.lower()


# ── region carbon line ────────────────────────────────────────────────────────


def test_energy_aware_region_adds_co2_line() -> None:
    result = _invoke(
        *_BASE,
        "--energy-aware",
        "--cost-per-request",
        "0.001",
        "--region",
        "eu-central-1",
    )
    assert result.exit_code == 0
    assert "Standing CO₂" in result.output
    assert "gCO₂eq/kWh" in result.output


def test_energy_aware_unknown_region_fails_closed() -> None:
    result = _invoke(*_BASE, "--energy-aware", "--region", "atlantis-1")
    assert result.exit_code == 2
    assert "unknown region" in result.output.lower()


# ── bounds on the new flags ───────────────────────────────────────────────────


def test_electricity_cost_out_of_bounds_rejected() -> None:
    result = _invoke(*_BASE, "--energy-aware", "--electricity-cost-per-kwh", "999")
    assert result.exit_code == 2
