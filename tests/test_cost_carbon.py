"""Tests for `pat cost --carbon` and the cheapest-greenest ranking (v0.22.0)."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.cost import CostResult, build_carbon_ranking
from presidio_arch_translucency.model import ReplicationLayer

runner = CliRunner()

C = ReplicationLayer.CONTAINER
P = ReplicationLayer.POD


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def _cost(layer, cpr: float) -> CostResult:
    return CostResult(
        layer=layer,
        replicas=4,
        throughput_rps=500.0,
        throughput_gain_pct=400.0,
        response_time_change_pct=10.0,
        hourly_cost_usd=0.1,
        cost_per_request_usd=cpr,
        roi_score=1.0,
        description="x",
    )


# ── ranking logic ─────────────────────────────────────────────────────────────


def test_ranking_picks_cheapest_and_greenest() -> None:
    cost_results = [_cost(C, 1.0), _cost(P, 2.0)]
    grams_req = {C: 100.0, P: 200.0}
    grams_hr = {C: 10.0, P: 20.0}
    ranked = build_carbon_ranking(cost_results, grams_req, grams_hr)
    winner = next(r for r in ranked if r.is_greenest_cheapest)
    assert winner.layer == C  # cheapest AND greenest
    assert sum(r.is_greenest_cheapest for r in ranked) == 1


def test_ranking_tradeoff_mean() -> None:
    # container cheaper but dirtier; pod pricier but cleaner → tie on mean.
    cost_results = [_cost(C, 1.0), _cost(P, 2.0)]
    grams_req = {C: 200.0, P: 100.0}
    grams_hr = {C: 20.0, P: 10.0}
    ranked = build_carbon_ranking(cost_results, grams_req, grams_hr)
    scores = {r.layer: r.combined_score for r in ranked}
    assert scores[C] == pytest.approx(0.5)
    assert scores[P] == pytest.approx(0.5)


def test_ranking_single_layer_scores_zero() -> None:
    ranked = build_carbon_ranking([_cost(C, 1.0)], {C: 100.0}, {C: 10.0})
    assert ranked[0].combined_score == pytest.approx(0.0)
    assert ranked[0].is_greenest_cheapest


def test_ranking_excludes_undefined_jreq() -> None:
    cost_results = [_cost(C, 1.0), _cost(P, 2.0)]
    grams_req = {C: 100.0, P: None}  # pod J/req undefined
    grams_hr = {C: 10.0, P: 20.0}
    ranked = build_carbon_ranking(cost_results, grams_req, grams_hr)
    pod = next(r for r in ranked if r.layer == P)
    assert pod.combined_score is None
    assert not pod.is_greenest_cheapest


def test_ranking_excludes_infinite_cost() -> None:
    cost_results = [_cost(C, 1.0), _cost(P, float("inf"))]
    grams_req = {C: 100.0, P: 50.0}
    grams_hr = {C: 10.0, P: 5.0}
    ranked = build_carbon_ranking(cost_results, grams_req, grams_hr)
    pod = next(r for r in ranked if r.layer == P)
    assert pod.combined_score is None


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_ranking_excludes_invalid_carbon(bad: float) -> None:
    ranked = build_carbon_ranking(
        [_cost(C, 1.0), _cost(P, 2.0)],
        {C: 100.0, P: bad},
        {C: 10.0, P: bad},
    )
    pod = next(r for r in ranked if r.layer == P)
    assert pod.grams_per_request is None
    assert pod.grams_per_hour is None
    assert pod.combined_score is None
    assert not pod.is_greenest_cheapest


def test_ranking_does_not_invent_missing_hourly_carbon() -> None:
    ranked = build_carbon_ranking([_cost(C, 1.0)], {C: 100.0}, {})
    assert ranked[0].grams_per_hour is None
    assert ranked[0].combined_score is None
    assert not ranked[0].is_greenest_cheapest


# ── CLI ───────────────────────────────────────────────────────────────────────


def test_cli_carbon_columns_and_marker() -> None:
    result = _invoke(
        "cost",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
        "--carbon",
        "--region",
        "eu-central-1",
    )
    assert result.exit_code == 0
    assert "gCO₂" in result.output
    assert "Grid intensity" in result.output
    assert "cheapest-greenest" in result.output
    assert "static 2023 average" in result.output


def test_cli_carbon_requires_region() -> None:
    result = _invoke("cost", "-r", "500", "-l", "80", "-c", "container", "--carbon")
    assert result.exit_code == 2
    assert "region" in result.output.lower()


def test_cli_carbon_unknown_region_fails_closed() -> None:
    result = _invoke(
        "cost",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
        "--carbon",
        "--region",
        "atlantis-1",
    )
    assert result.exit_code == 2
    assert "unknown region" in result.output.lower()


def test_cli_carbon_region_markup_is_escaped() -> None:
    # FIX 2: the CarbonError message embeds --region; Rich markup in it must
    # render as literal text (exit 2), never be interpreted as console styling.
    result = _invoke(
        "cost",
        "-r",
        "500",
        "-l",
        "80",
        "-c",
        "container",
        "--carbon",
        "--region",
        "[blink]x[/]",
    )
    assert result.exit_code == 2
    assert "[blink]x[/]" in result.output


def test_cli_default_cost_has_no_carbon() -> None:
    result = _invoke("cost", "-r", "500", "-l", "80", "-c", "container")
    assert result.exit_code == 0
    assert "gCO₂" not in result.output
    assert "Grid intensity" not in result.output
