"""Tests for the H4 energy-vs-cost comparison (infer_h4.py) and its CLI."""

import json

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.infer_calibrate import InferenceCalibrationError
from presidio_arch_translucency.infer_h4 import analyze_h4, common_levels
from presidio_arch_translucency.infer_prices import gpu_model_key
from presidio_arch_translucency.infer_sweep import Slo, Sweep, load_sweep
from tests.test_infer_prices import _catalog, _committed
from tests.test_infer_sweep import _profile, _report, _write

PROMPT, OUTPUT, WINDOW = 900, 300, 120
SLO = Slo(tpot_ms=45.0)
runner = CliRunner()


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)


def _uuid(tag, i):
    return f"GPU-{tag:0>8}-0000-0000-0000-{i:012d}"


def _level(
    lam,
    j,
    *,
    tpot=30.0,
    steady=True,
    energy_verdict="recorded",
    rate=1.0,
    temp=60.0,
    saturated=False,
    refine=False,
    hosts=("node-a",),
):
    tokens = lam * OUTPUT * WINDOW * rate
    joules = j * tokens
    return {
        "level": lam,
        "verdict": "recorded" if steady else "refused",
        "saturated": saturated,
        "refine": refine,
        "point": {"tpot_ms": tpot} if steady else None,
        "energy": {
            "verdict": energy_verdict,
            "reason": None if energy_verdict == "recorded" else "counter reset",
            "steady": steady,
            "window_s": WINDOW,
            "joules": joules,
            "watts": joules / WINDOW,
            "output_tokens": tokens,
            "j_per_output_token": j,
            "gpus": [
                {"uuid": _uuid("a", i), "host": host, "temp_mean_c": temp}
                for i, host in enumerate(hosts)
            ],
        },
    }


def _energy(
    tp,
    n,
    tag,
    hosts=1,
    idle_w=30.0,
    limit=350.0,
    drift=0.0,
    drifted=False,
    model="NVIDIA L40S",
    driver="580.65.06",
    with_drift=True,
):
    groups = [[_uuid(tag, e * tp + g) for g in range(tp)] for e in range(n)]
    labels = {
        uuid: {
            "engine": str(e),
            "gpu": str(g),
            "host": f"node-{e % hosts}",
            "model_name": model,
            "driver": driver,
        }
        for e, group in enumerate(groups)
        for g, uuid in enumerate(group)
    }
    return {
        "meter": "dcgm",
        "gpus": groups,
        "labels": labels,
        "hosts": hosts,
        "idle_before": {
            uuid: {"watts": idle_w, "power_limit_w": limit} for uuid in labels
        },
        "idle_after": None,
        "drift_control": (
            {"drift": drift, "drifted": drifted, "level": 1.0} if with_drift else None
        ),
    }


def _sweep(tp, n, levels, tag=None, **energy_kw):
    return Sweep(
        path=f"sweep-tp{tp}x{n}.json",
        sha256="0" * 64,
        model="m",
        tp=tp,
        instances=n,
        prompt_tokens=PROMPT,
        output_tokens=OUTPUT,
        levels=levels,
        routing="single" if n == 1 else "client-random",
        window_s=WINDOW,
        energy=_energy(tp, n, tag or f"{tp}{n}", **energy_kw),
    )


PRICES = {(1, 1): 1.40, (2, 1): 2.60, (1, 2): 2.60, (4, 1): 5.00}


def _price(tp, n):
    value = PRICES.get((tp, n))
    return value, {"eur_per_hour": value}


def _ladder(j_low, j_high, **kw):
    return [_level(1.0, j_low, **kw), _level(2.0, j_high, **kw)]


def _h4(sweeps, lam=2.0, price=_price, **kw):
    return analyze_h4(sweeps, SLO, lam, price=price, **kw)


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------


def test_diverge_when_the_least_energy_layout_costs_more():
    result = _h4([_sweep(1, 1, _ladder(0.8, 0.50)), _sweep(2, 1, _ladder(0.9, 0.40))])
    assert result["verdict"] == "diverge"
    assert result["h4"] == "pass"
    assert result["energy_optimal"] == "tp2×1"
    assert result["cost_optimal"] == ["tp1×1"]
    assert result["energy_saving"] == pytest.approx(0.2)
    assert result["cost_premium"] == pytest.approx(2.60 / 1.40 - 1.0)
    assert result["tie_margin_pct"] == 5.0
    row = result["layouts"][0]
    assert row["eur_per_mtok"] == pytest.approx(1.40 / (2.0 * OUTPUT * 3600) * 1e6)
    assert row["watts_over_idle"] == pytest.approx(row["watts"] - 30.0)
    assert result["warnings"] == []
    json.dumps(result)


def test_coincide_when_the_cheapest_also_uses_least_energy():
    result = _h4([_sweep(1, 1, _ladder(0.5, 0.40)), _sweep(2, 1, _ladder(0.6, 0.50))])
    assert (result["verdict"], result["h4"]) == ("coincide", "fail")
    assert result["energy_saving"] == pytest.approx(0.0)


def test_cost_tie_broken_by_energy_is_an_h4_fail():
    result = _h4(
        [
            _sweep(1, 1, _ladder(0.5, 0.5, tpot=50.0)),  # misses the SLO
            _sweep(2, 1, _ladder(0.5, 0.40)),
            _sweep(1, 2, _ladder(0.5, 0.48, hosts=("node-0", "node-0"))),
        ]
    )
    assert (result["verdict"], result["h4"]) == ("cost_tied", "fail")
    assert result["cost_optimal"] == ["tp2×1", "tp1×2"]
    assert result["tie_energy_spread"] == pytest.approx(0.2)
    assert result["layouts"][0]["reasons"] == ["does not meet the SLO at λ*"]


def test_energy_gap_inside_the_margin_is_inconclusive():
    result = _h4([_sweep(1, 1, _ladder(0.5, 0.50)), _sweep(2, 1, _ladder(0.5, 0.485))])
    assert result["verdict"] == "inconclusive"
    assert result["reason"] == "energy_unresolved"
    assert result["h4"] is None


def test_margin_widens_with_measured_drift():
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.50), drift=0.04),
        _sweep(2, 1, _ladder(0.5, 0.47), drift=-0.045),
    ]
    result = _h4(sweeps)
    assert result["tie_margin_pct"] == pytest.approx(8.5)
    assert result["reason"] == "energy_unresolved"


def test_different_host_counts_never_diverge():
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.50)),
        _sweep(1, 2, _ladder(0.5, 0.40), hosts=2),
    ]
    result = _h4(sweeps)
    assert result["verdict"] == "inconclusive"
    assert result["reason"] == "host_scope"
    # J_e + 2·P·w/T_e = J_c + 1·P·w/T_c with T = 2·300·120 for both layouts.
    tokens = 2.0 * OUTPUT * WINDOW
    assert result["host_watts_to_flip"] == pytest.approx(0.1 / (WINDOW / tokens))


def test_without_prices_energy_is_still_ranked():
    result = _h4(
        [_sweep(1, 1, _ladder(0.5, 0.5)), _sweep(2, 1, _ladder(0.5, 0.4))],
        price=None,
    )
    assert result["reason"] == "no_prices"
    assert result["energy_optimal"] == "tp2×1"
    assert result["layouts"][0]["eur_per_hour"] is None


def test_fewer_than_two_qualifying_layouts():
    result = _h4(
        [
            _sweep(1, 1, _ladder(0.5, 0.5)),
            _sweep(2, 1, _ladder(0.5, 0.4, energy_verdict="refused")),
        ]
    )
    assert result["reason"] == "fewer than two qualifying layouts"
    assert "energy refused: counter reset" in result["layouts"][1]["reasons"]


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level_kw", "sweep_kw", "price", "reason"),
    [
        ({"steady": False}, {}, _price, "no SLO verdict at λ*"),
        ({"rate": 0.9}, {}, _price, "throughput_miss"),
        ({}, {"drift": 0.08, "drifted": True}, _price, "drift control flagged +8.0%"),
        ({}, {}, lambda tp, n: (None, {"reason": "no SKU"}), "no price"),
        ({"saturated": True}, {}, _price, "does not meet the SLO"),
    ],
)
def test_qualification_reasons(level_kw, sweep_kw, price, reason):
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.5)),
        _sweep(2, 1, _ladder(0.5, 0.4, **level_kw), **sweep_kw),
    ]
    row = _h4(sweeps, price=price)["layouts"][1]
    assert not row["qualifies"]
    assert any(reason in text for text in row["reasons"]), row["reasons"]


def test_steady_flag_disqualifies_a_refused_capacity_window():
    level = _level(2.0, 0.4)
    level["energy"]["steady"] = False  # capacity verdict recorded, energy says no
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.5)),
        _sweep(2, 1, [_level(1.0, 0.5), level]),
    ]
    assert "capacity window refused" in _h4(sweeps)["layouts"][1]["reasons"]


def test_missing_drift_control_is_flagged_not_refused():
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.5), with_drift=False),
        _sweep(2, 1, _ladder(0.5, 0.4)),
    ]
    result = _h4(sweeps)
    assert result["layouts"][0]["qualifies"]
    assert result["layouts"][0]["drift_checked"] is False
    assert "a qualifying layout has no drift control" in result["warnings"]


def test_refine_levels_are_not_the_registered_level():
    sweeps = [
        _sweep(1, 1, [_level(1.0, 0.5), _level(2.0, 0.5, refine=True)]),
        _sweep(2, 1, _ladder(0.5, 0.4)),
    ]
    with pytest.raises(InferenceCalibrationError, match="not a level of every"):
        _h4(sweeps)
    assert common_levels(sweeps) == [1.0]


def test_context_flags_and_warnings():
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.5, temp=45.0), idle_w=50.0),
        _sweep(2, 1, _ladder(0.5, 0.4, temp=70.0), driver="575.57.08"),
    ]
    result = _h4(sweeps)
    a = result["layouts"][0]
    # 0.5 J × 600 tok/s = 300 W against 50 W idle: well above 2×max(20, 5) W.
    assert a["watts_over_idle"] == pytest.approx(250.0)
    assert a["idle_dominated"] is False
    assert a["temp_mean_c"] == 45.0
    assert any("different drivers" in w for w in result["warnings"])
    assert any("differ by 25 °C" in w for w in result["warnings"])


def test_idle_dominated_layouts_are_flagged():
    sweeps = [
        _sweep(1, 1, _ladder(0.05, 0.05), idle_w=25.0),  # 30 W total
        _sweep(2, 1, _ladder(0.5, 0.4)),
    ]
    result = _h4(sweeps)
    assert result["layouts"][0]["idle_dominated"] is True
    assert any("idle-dominated" in w for w in result["warnings"])


def test_lambda_over_capacity_from_the_measured_slo_bracket():
    levels = [_level(1.0, 0.5), _level(2.0, 0.5), _level(4.0, 0.5, tpot=60.0)]
    result = _h4([_sweep(1, 1, levels), _sweep(2, 1, _ladder(0.5, 0.4))])
    a, b = result["layouts"]
    assert a["lambda_over_capacity"] == pytest.approx(2.0 / 8**0.5)
    assert a["lambda_over_capacity_bound"] == "estimate"
    # Never failed: λ* over the highest passing level bounds the ratio above.
    assert b["lambda_over_capacity"] == pytest.approx(1.0)
    assert b["lambda_over_capacity_bound"] == "at_most"


def test_verdict_at_every_other_common_level():
    sweeps = [
        _sweep(1, 1, _ladder(0.40, 0.50)),
        _sweep(2, 1, _ladder(0.60, 0.40)),
    ]
    result = _h4(sweeps)
    assert result["verdict"] == "diverge"
    assert result["ladder"] == [
        pytest.approx({**result["ladder"][0], "lambda": 1.0, "verdict": "coincide"})
    ]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_refusals():
    pair = [_sweep(1, 1, _ladder(0.5, 0.5)), _sweep(2, 1, _ladder(0.5, 0.4))]
    with pytest.raises(InferenceCalibrationError, match="meet an SLO"):
        analyze_h4(pair, Slo(), 2.0)
    with pytest.raises(InferenceCalibrationError, match="shared levels: 1, 2"):
        _h4(pair, lam=3.0)
    with pytest.raises(InferenceCalibrationError, match="positive rate"):
        _h4(pair, lam=float("nan"))


@pytest.mark.parametrize(
    ("kw", "gpu_type", "match"),
    [
        ({"model": "NVIDIA H100 80GB HBM3"}, None, "different GPU models"),
        ({}, "L40", "the calibration is for 'L40'"),
        ({"limit": 300.0}, None, "different GPU power limits"),
        ({"limit": None}, None, "different GPU power limits"),
    ],
)
def test_incomparable_sweeps_are_refused(kw, gpu_type, match):
    sweeps = [_sweep(1, 1, _ladder(0.5, 0.5)), _sweep(2, 1, _ladder(0.5, 0.4), **kw)]
    with pytest.raises(InferenceCalibrationError, match=match):
        _h4(sweeps, gpu_type=gpu_type)


def test_gpu_model_key():
    assert gpu_model_key("NVIDIA L40S") == gpu_model_key(" l40s ") == ("l40s",)
    assert gpu_model_key("L40") != gpu_model_key("NVIDIA L40S")
    assert gpu_model_key("NVIDIA H100 80GB HBM3") == ("h100", "80gb", "hbm3")
    _h4(
        [_sweep(1, 1, _ladder(0.5, 0.5)), _sweep(2, 1, _ladder(0.5, 0.4))],
        gpu_type="l40s",
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _file_report(sweep: Sweep) -> dict:
    data = _report(sweep.levels, tp=sweep.tp, instances=sweep.instances)
    data["config"]["window_s"] = WINDOW
    data["config"]["routing"] = sweep.routing
    data["energy"] = sweep.energy
    return data


def test_loader_keeps_energy_and_window(tmp_path):
    sweep = _sweep(2, 1, _ladder(0.5, 0.4))
    loaded = load_sweep(_write(tmp_path, "s.json", _file_report(sweep)))
    assert loaded.window_s == WINDOW
    assert loaded.energy["hosts"] == 1
    assert loaded.levels[1]["energy"]["j_per_output_token"] == 0.4


def _broken(change):
    data = _file_report(_sweep(2, 1, _ladder(0.5, 0.4)))
    change(data)
    return data


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (
            lambda d: d["levels"][0]["energy"].update(verdict="maybe"),
            "malformed energy record",
        ),
        (
            lambda d: d["levels"][0]["energy"].update(steady="yes"),
            "malformed energy record",
        ),
        (
            lambda d: d["levels"][0]["energy"].update(j_per_output_token=0.1),
            "malformed energy record",
        ),
        (
            lambda d: d["levels"][0]["energy"].update(watts=1.0),
            "malformed energy record",
        ),
        (
            lambda d: d["levels"][0]["energy"].update(window_s=60),
            "malformed energy record",
        ),
        (
            lambda d: d["levels"][0]["energy"].update(gpus=[{"uuid": 1}]),
            "malformed energy record",
        ),
        (
            lambda d: d["levels"][0]["energy"].update(joules=float("inf")),
            "not JSON",
        ),
        (lambda d: d["energy"].update(gpus=[["GPU-x"]]), "engine\\(s\\) of tp=2"),
        (lambda d: d["energy"].update(hosts=0), "malformed energy section"),
        (
            lambda d: d["energy"].update(idle_before={"x": {"watts": -1}}),
            "malformed energy section",
        ),
        (
            lambda d: d["energy"].update(drift_control={"drifted": "no"}),
            "malformed energy section",
        ),
        (lambda d: d.update(energy=[]), "malformed energy section"),
    ],
)
def test_loader_refuses_malformed_energy(tmp_path, change, match):
    path = tmp_path / "s.json"
    data = _broken(change)
    path.write_text(json.dumps(data, allow_nan=True))
    with pytest.raises(InferenceCalibrationError, match=match):
        load_sweep(path)


def test_refused_energy_needs_no_numbers(tmp_path):
    data = _file_report(_sweep(2, 1, _ladder(0.5, 0.4)))
    data["levels"][0]["energy"] = {"verdict": "refused", "steady": True}
    load_sweep(_write(tmp_path, "s.json", data))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_files(tmp_path):
    _profile("h4")
    a = _write(tmp_path, "a.json", _file_report(_sweep(1, 1, _ladder(0.8, 0.5))))
    b = _write(tmp_path, "b.json", _file_report(_sweep(2, 1, _ladder(0.9, 0.4))))
    catalog = _committed(tmp_path, _catalog())
    return a, b, str(catalog)


def _validate(*args):
    return runner.invoke(
        app,
        ["--skip-audit", "infer-validate", "--calibration", "h4", *args],
    )


def test_cli_h4_table_and_json(tmp_path):
    a, b, catalog = _cli_files(tmp_path)
    args = [
        "--sweep", a, "--sweep", b, "--tpot-slo-ms", "45", "--energy-at", "2",
        "--price-catalog", catalog, "--price-provider", "example",
    ]  # fmt: skip
    result = _validate(*args)
    assert result.exit_code == 0, result.output
    assert "H4 at λ* = 2 req/s" in result.output
    assert "H4 diverge (pass)" in result.output
    text = " ".join(result.output.split())  # Rich wraps at 80 columns
    assert "saving 20.0% J/token for +85.7% €/h" in text
    data = json.loads(_validate(*args, "--json").stdout)
    assert data["h4"]["verdict"] == "diverge"
    assert data["h4"]["layouts"][1]["price"]["sku"] == "L40S-2"
    assert {p["sku"] for p in data["prices"]["entries_used"]} == {"L40S-1", "L40S-2"}
    assert data["layouts"][0]["layout"] == "tp1×1"  # H3 rows unchanged


def test_cli_h4_without_prices(tmp_path):
    a, b, _catalog_path = _cli_files(tmp_path)
    result = _validate(
        "--sweep", a, "--sweep", b, "--tpot-slo-ms", "45", "--energy-at", "2"
    )
    assert result.exit_code == 0, result.output
    assert "H4 inconclusive: no_prices" in result.output
    assert "least energy: tp2×1" in result.output


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--energy-at", "2"], "meet an SLO"),
        (["--tpot-slo-ms", "45", "--energy-at", "3"], "not a level of every"),
        (["--tpot-slo-ms", "45", "--price-catalog", "x.json"], "give --energy-at"),
        (
            ["--tpot-slo-ms", "45", "--energy-at", "2", "--price-provider", "e"],
            "need --price-catalog",
        ),
    ],
)
def test_cli_h4_refusals(tmp_path, extra, message):
    a, b, _catalog_path = _cli_files(tmp_path)
    result = _validate("--sweep", a, "--sweep", b, *extra)
    assert result.exit_code == 2
    assert message in result.output


def test_cli_energy_at_needs_sweep_mode():
    result = _validate("--energy-at", "2", "--leave-one-out")
    assert result.exit_code == 2
    assert "apply to --sweep validation only" in result.output


# ---------------------------------------------------------------------------
# Implementation review fixes
# ---------------------------------------------------------------------------


def test_margin_holds_against_every_cheapest_layout():
    # e beats the runner-up (5 % margin) but not the cheapest (9.4 % margin).
    sweeps = [
        _sweep(2, 1, _ladder(0.5, 1.00), drift=0.045),
        _sweep(4, 1, _ladder(0.5, 1.06)),
        _sweep(1, 1, _ladder(0.5, 1.07), drift=0.049),
    ]
    result = _h4(sweeps)
    assert result["reason"] == "energy_unresolved"
    assert result["close"] == ["tp1×1"]
    assert result["tie_margin_pct"] == pytest.approx(9.4)


def test_cheapest_with_a_close_cheap_contender_fails():
    sweeps = [
        _sweep(2, 1, _ladder(0.5, 0.40)),
        _sweep(1, 2, _ladder(0.5, 0.41, hosts=("node-0", "node-0"))),
        _sweep(4, 1, _ladder(0.5, 0.60)),
    ]
    result = _h4(sweeps)
    assert (result["verdict"], result["h4"]) == ("cost_tied", "fail")
    assert result["close"] == ["tp1×2"]


def test_cheapest_with_a_close_expensive_contender_is_inconclusive():
    sweeps = [_sweep(1, 1, _ladder(0.5, 0.40)), _sweep(2, 1, _ladder(0.5, 0.41))]
    result = _h4(sweeps)
    assert result["reason"] == "energy_unresolved"


def test_more_hosts_elsewhere_cannot_block_diverge():
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.50)),
        _sweep(2, 1, _ladder(0.5, 0.40)),
        _sweep(1, 2, _ladder(0.5, 0.70), hosts=2),  # extra host only hurts it
    ]
    result = _h4(sweeps)
    assert result["verdict"] == "diverge"
    assert result["host_watts_to_flip"] is None


def test_a_fail_that_host_draw_could_overturn_is_host_scope():
    sweeps = [
        _sweep(1, 2, _ladder(0.5, 0.40), hosts=2),  # cheapest, least energy
        _sweep(4, 1, _ladder(0.5, 0.50)),  # dearer, one host
    ]
    PRICES[(1, 2)] = 2.0
    try:
        result = _h4(sweeps)
    finally:
        PRICES[(1, 2)] = 2.60
    assert result["reason"] == "host_scope"
    tokens = 2.0 * OUTPUT * WINDOW
    assert result["host_watts_to_flip"] == pytest.approx(0.1 / (WINDOW / tokens))


def test_no_flip_watts_at_equal_host_counts():
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.50, rate=1.01)),
        _sweep(2, 1, _ladder(0.5, 0.40, rate=0.99)),
    ]
    result = _h4(sweeps)
    assert result["verdict"] == "diverge"
    assert result["host_watts_to_flip"] is None


def test_dcgm_variant_names_match_the_profile_family():
    sweeps = [
        _sweep(1, 1, _ladder(0.5, 0.5), model="NVIDIA H100 80GB HBM3"),
        _sweep(2, 1, _ladder(0.5, 0.4), model="NVIDIA H100 80GB HBM3"),
    ]
    assert _h4(sweeps, gpu_type="H100")["verdict"] == "diverge"
    a100 = [
        _sweep(1, 1, _ladder(0.5, 0.5), model="NVIDIA A100-SXM4-80GB"),
        _sweep(2, 1, _ladder(0.5, 0.4), model="NVIDIA A100-SXM4-80GB"),
    ]
    assert _h4(a100, gpu_type="a100")["verdict"] == "diverge"
    with pytest.raises(InferenceCalibrationError, match="calibration is for 'L40'"):
        _h4(
            [_sweep(1, 1, _ladder(0.5, 0.5)), _sweep(2, 1, _ladder(0.5, 0.4))],
            gpu_type="L40",
        )


def test_price_gpu_selection_uses_the_same_name_rule(tmp_path):
    from presidio_arch_translucency.infer_prices import (  # noqa: PLC0415
        load_price_catalog,
        select_family,
    )

    loaded = load_price_catalog(_committed(tmp_path, _catalog()))
    family = select_family(loaded, "example", None, "nvidia  l40s", "on-demand")
    assert family.head.gpu_model == "L40S"


def test_real_1g_records_load(tmp_path):
    from tests.test_infer_energy import U1, FakeDcgm  # noqa: PLC0415

    fake = FakeDcgm([U1])
    fake.meter.gate()
    fake.loaded = False
    fake.meter.idle = fake.meter.measure_idle(WINDOW, 1.0)
    fake.loaded = True
    record = fake.meter.level(WINDOW, 1.0, "{}", steady=True)
    fake.override[U1] = {"temp_mean_c": None}
    untemped = fake.meter.level(WINDOW, 1.0, "{}", steady=True)
    fake.fail = True
    refused = fake.meter.level(WINDOW, 1.0, "{}", steady=False)
    levels = [
        {**_level(1.0, 0.5), "energy": record},
        {**_level(2.0, 0.5), "energy": untemped},
        {**_level(3.0, 0.5), "energy": refused},
    ]
    data = _report(levels, tp=1, instances=1)
    data["config"]["window_s"] = WINDOW
    data["energy"] = {
        **fake.meter.describe(),
        "drift_control": {"level": 1.0, "drift": None, "drifted": None},
        "idle_after": {"refused": "idle baseline refused: busy"},
    }
    loaded = load_sweep(_write(tmp_path, "real.json", json.loads(json.dumps(data))))
    assert loaded.levels[0]["energy"]["verdict"] == "recorded"
    assert loaded.levels[2]["energy"]["gpus"] is None


def test_cli_escapes_labels_from_the_unsigned_sweep(tmp_path):
    _profile("h4")
    a = _file_report(_sweep(1, 1, _ladder(0.5, 0.5), model="[bold red]x[/]"))
    b = _file_report(_sweep(2, 1, _ladder(0.5, 0.4)))
    result = _validate(
        "--sweep", _write(tmp_path, "a.json", a),
        "--sweep", _write(tmp_path, "b.json", b),
        "--tpot-slo-ms", "45", "--energy-at", "2",
    )  # fmt: skip
    assert result.exit_code == 2
    assert "[bold red]x[/]" in result.output
