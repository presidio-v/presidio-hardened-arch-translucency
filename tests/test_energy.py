"""Tests for the energy model (`pat` v0.20.0 — "Model the watt").

Covers the analytic equations (``energy.py``), the energy calibration fit and
its commitment extension (``calibrate.py``), and the CLI surface (analyze
energy columns, what-if energy line, slo J/req column, ``pat calibrate
--energy-observation`` round-trip). The critical backward-compat guarantee —
a v0.19-shape fit record still verifies ``ok`` under v0.20 code — is pinned
here alongside the new tamper-detection cases.
"""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.calibrate import (
    CALIBRATION_COMMITMENT_SCHEMA,
    CalibrationError,
    EnergyObservation,
    Observation,
    commitment_digest,
    fit_calibration,
    fit_energy_calibration,
    global_model_path,
    parse_energy_observation,
    predict_watts,
    verify_commitment,
    write_model_file,
)
from presidio_arch_translucency.cli import app
from presidio_arch_translucency.energy import (
    DEFAULT_REPLICA_POWER_WATTS,
    ENERGY_PARAMS,
    EnergyParams,
    dyn_joules_per_request,
    eei,
    idle_watts_per_replica,
    joules_per_request,
    layer_energy,
    power_watts,
    resolve_energy_fit_scope,
    resolve_energy_params,
)
from presidio_arch_translucency.model import (
    ALL_REPLICATION_LAYERS,
    ReplicationLayer,
    load_calibrated_model,
)

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """Isolated HOME + cwd so model-file reads/writes never touch the real store."""
    home = tmp_path / "home"
    (home / ".pat").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    return home


# ── helpers ───────────────────────────────────────────────────────────────────


def _synthetic_energy(
    p_idle: float, e_dyn: float, beta: float
) -> list[EnergyObservation]:
    """Energy observations that exactly satisfy W = P_idle·δ + e_dyn·rps·(1+β lnδ)."""
    points = [(300.0, 2), (500.0, 5), (700.0, 8), (400.0, 3), (600.0, 6)]
    return [
        EnergyObservation(
            rps=rps,
            latency_ms=80.0,
            replicas=rep,
            watts=float(predict_watts(rep, rps, p_idle, e_dyn, beta)),
        )
        for rps, rep in points
    ]


def _throughput_fit():
    return fit_calibration(
        [Observation(rps=100, latency_ms=50, replicas=2), Observation(300, 80, 5)]
    )


def _energy_fit(p_idle: float = 5.0, e_dyn: float = 0.5, beta: float = 0.01):
    return fit_energy_calibration(_synthetic_energy(p_idle, e_dyn, beta))


def _invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


# ── equations ─────────────────────────────────────────────────────────────────


class TestEquations:
    def test_idle_watts_is_alpha_times_peak(self):
        assert idle_watts_per_replica(0.36, 15.0) == pytest.approx(5.4)

    def test_dyn_joules_per_request(self):
        # (1 - α)·P_peak / capacity
        assert dyn_joules_per_request(0.36, 15.0, 100.0) == pytest.approx(
            (1 - 0.36) * 15.0 / 100.0
        )

    def test_power_monotonic_in_delta_at_fixed_omega(self):
        w2 = power_watts(2, 100.0, 5.0, 0.5, 0.01)
        w4 = power_watts(4, 100.0, 5.0, 0.5, 0.01)
        assert w4 > w2  # every extra replica adds its idle floor

    def test_no_coordination_penalty_at_delta_one(self):
        # ln(1) == 0 → the β_E coordination term vanishes at a single replica.
        w = power_watts(1, 100.0, 5.0, 0.5, 0.03)
        assert w == pytest.approx(5.0 * 1 + 0.5 * 100.0 * 1.0)

    def test_joules_per_request_at_saturation_positive(self):
        watts = power_watts(4, 400.0, 5.0, 0.5, 0.01)
        assert joules_per_request(watts, 400.0) > 0

    def test_joules_per_request_zero_throughput_guard(self):
        assert joules_per_request(100.0, 0.0) is None

    def test_eei_known_values(self):
        assert eei(2.0, 1.0) == pytest.approx(2.0)
        assert eei(1.0, 2.0) == pytest.approx(0.5)

    def test_eei_classification_above_and_below_one(self):
        assert eei(2.0, 1.0) > 1.0  # replication buys more throughput than energy
        assert eei(1.0, 2.0) < 1.0  # energy intensity worsened faster than gain

    def test_eei_division_guard(self):
        assert eei(1.0, 0.0) is None

    def test_node_jreq_worse_than_container_at_low_utilisation(self):
        # The translucency insight for energy: a node's idle floor dominates
        # when the fleet is underutilised, so J/req is worse than a container's.
        base_capacity = 1000.0
        low_rps = 50.0
        node = layer_energy(ReplicationLayer.NODE, 1, low_rps, base_capacity, 15.0)
        container = layer_energy(
            ReplicationLayer.CONTAINER, 1, low_rps, base_capacity, 15.0
        )
        assert node.joules_per_request > container.joules_per_request


# ── defaults ──────────────────────────────────────────────────────────────────


class TestDefaults:
    def test_energy_params_complete_for_all_layers(self):
        for layer in ALL_REPLICATION_LAYERS:
            assert isinstance(ENERGY_PARAMS[layer], EnergyParams)

    def test_alpha_ordering_node_gt_deployment_gt_pod_gt_container(self):
        a = {
            layer: ENERGY_PARAMS[layer].energy_alpha for layer in ALL_REPLICATION_LAYERS
        }
        assert (
            a[ReplicationLayer.NODE]
            > a[ReplicationLayer.DEPLOYMENT]
            > a[ReplicationLayer.POD]
            > a[ReplicationLayer.CONTAINER]
        )

    def test_default_replica_power_watts(self):
        assert DEFAULT_REPLICA_POWER_WATTS == pytest.approx(15.0)


# ── parse_energy_observation ──────────────────────────────────────────────────


class TestParseEnergyObservation:
    def test_valid(self):
        obs = parse_energy_observation("300:80:5:420")
        assert obs == EnergyObservation(
            rps=300.0, latency_ms=80.0, replicas=5, watts=420.0
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "300:80:5",  # wrong arity (throughput triple)
            "300:80:5:420:1",  # too many fields
            "abc:80:5:420",  # non-numeric rps
            "300:80:5:watts",  # non-numeric watts
            "0:80:5:420",  # zero rps
            "300:0:5:420",  # zero latency
            "300:80:0:420",  # zero replicas
            "300:80:5:0",  # zero watts
            "300:80:5:-1",  # negative watts
        ],
    )
    def test_invalid(self, raw):
        with pytest.raises(CalibrationError):
            parse_energy_observation(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "nan:80:5:420",  # NaN rps
            "inf:80:5:420",  # +inf rps
            "-inf:80:5:420",  # -inf rps
            "300:nan:5:420",  # NaN latency
            "300:inf:5:420",  # +inf latency
            "300:80:nan:420",  # NaN replicas (int parse also rejects)
            "300:80:5:nan",  # NaN watts
            "300:80:5:inf",  # +inf watts
        ],
    )
    def test_non_finite_rejected(self, raw):
        # nan/inf pass the positivity checks (nan <= 0 is False); math.isfinite
        # must reject them before they reach the energy fit.
        with pytest.raises(CalibrationError):
            parse_energy_observation(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            "1000001:80:5:420",
            "300:300001:5:420",
            "300:80:10001:420",
            "300:80:5:1000001",
        ],
    )
    def test_out_of_bounds_rejected(self, raw):
        with pytest.raises(CalibrationError):
            parse_energy_observation(raw)


# ── fit_energy_calibration ────────────────────────────────────────────────────


class TestFitEnergyCalibration:
    def test_recovers_known_parameters(self):
        result = fit_energy_calibration(_synthetic_energy(5.0, 0.5, 0.03))
        assert result.energy_idle_w == pytest.approx(5.0, abs=1e-2)
        assert result.energy_dyn_j_per_req == pytest.approx(0.5, abs=1e-3)
        assert result.energy_beta == pytest.approx(0.03, abs=1e-2)
        assert result.r_squared == pytest.approx(1.0, abs=1e-6)
        assert result.rmse < 1e-3

    def test_two_point_exact_solve(self):
        points = _synthetic_energy(5.0, 0.5, 0.02)[:2]  # δ = 2 and 5, distinct
        result = fit_energy_calibration(points)
        assert result.energy_idle_w == pytest.approx(5.0, abs=1e-6)
        assert result.energy_dyn_j_per_req == pytest.approx(0.5, abs=1e-6)
        assert result.energy_beta == pytest.approx(0.02)  # held at default

    def test_three_points_hold_beta_at_default(self):
        points = _synthetic_energy(5.0, 0.5, 0.02)[:3]
        result = fit_energy_calibration(points)
        assert result.energy_idle_w == pytest.approx(5.0, abs=1e-6)
        assert result.energy_dyn_j_per_req == pytest.approx(0.5, abs=1e-6)
        assert result.energy_beta == pytest.approx(0.02)

    def test_two_point_same_delta_rejected(self):
        points = [
            EnergyObservation(rps=300, latency_ms=80, replicas=4, watts=200),
            EnergyObservation(rps=500, latency_ms=80, replicas=4, watts=300),
        ]
        with pytest.raises(CalibrationError):
            fit_energy_calibration(points)

    def test_single_point_rejected(self):
        with pytest.raises(CalibrationError):
            fit_energy_calibration(
                [EnergyObservation(rps=300, latency_ms=80, replicas=5, watts=400)]
            )

    def test_empty_rejected(self):
        with pytest.raises(CalibrationError):
            fit_energy_calibration([])

    def test_negative_solution_rejected(self):
        # More replicas at the same load should draw MORE watts; a large drop
        # forces a negative standing power → inconsistent with the model.
        points = [
            EnergyObservation(rps=100, latency_ms=80, replicas=2, watts=1000),
            EnergyObservation(rps=100, latency_ms=80, replicas=10, watts=100),
        ]
        with pytest.raises(CalibrationError):
            fit_energy_calibration(points)

    def test_duplicate_operating_point_rejected(self):
        points = [
            EnergyObservation(rps=100, latency_ms=80, replicas=1, watts=100),
            EnergyObservation(rps=100, latency_ms=80, replicas=1, watts=100),
            EnergyObservation(rps=200, latency_ms=80, replicas=2, watts=200),
        ]
        with pytest.raises(CalibrationError, match="unique"):
            fit_energy_calibration(points)

    def test_rank_deficient_four_point_fit_rejected(self):
        points = [
            EnergyObservation(
                rps=float(100 * replicas),
                latency_ms=80,
                replicas=replicas,
                watts=float(60 * replicas),
            )
            for replicas in (1, 2, 3, 4)
        ]
        with pytest.raises(CalibrationError, match="rank-deficient"):
            fit_energy_calibration(points)

    def test_programmatic_out_of_bounds_point_rejected(self):
        points = _synthetic_energy(5.0, 0.5, 0.02)
        points[0] = EnergyObservation(
            rps=points[0].rps,
            latency_ms=points[0].latency_ms,
            replicas=points[0].replicas,
            watts=1_000_001,
        )
        with pytest.raises(CalibrationError, match="watts"):
            fit_energy_calibration(points)

    def test_predictions_and_residuals_aligned(self):
        obs = _synthetic_energy(6.0, 0.4, 0.02)
        result = fit_energy_calibration(obs)
        assert len(result.predictions) == len(obs)
        assert len(result.residuals) == len(obs)
        for o, pred, resid in zip(
            obs, result.predictions, result.residuals, strict=True
        ):
            assert resid == pytest.approx(o.watts - pred, abs=1e-6)


# ── resolve_energy_params ─────────────────────────────────────────────────────


class TestResolveEnergyParams:
    def test_default_fallback_when_uncalibrated(self):
        idle, dyn, beta, source = resolve_energy_params(
            ReplicationLayer.NODE, 15.0, 100.0
        )
        assert source == "default"
        assert idle == pytest.approx(0.36 * 15.0)
        assert beta == pytest.approx(0.03)
        assert dyn == pytest.approx((1 - 0.36) * 15.0 / 100.0)

    def test_dyn_none_when_capacity_nonpositive(self):
        _, dyn, _, source = resolve_energy_params(ReplicationLayer.NODE, 15.0, 0.0)
        assert source == "default"
        assert dyn is None

    def test_global_fit_used_when_present(self):
        write_model_file(_throughput_fit(), energy=_energy_fit(5.0, 0.5, 0.01))
        idle, dyn, _, source = resolve_energy_params(
            ReplicationLayer.CONTAINER, 15.0, 100.0
        )
        assert source == "calibrated"
        assert dyn == pytest.approx(0.5, abs=1e-3)
        assert idle == pytest.approx(5.0, abs=1e-2)

    def test_per_layer_fit_used(self):
        write_model_file(
            _throughput_fit(), layer="api", energy=_energy_fit(7.0, 0.9, 0.01)
        )
        _, dyn, _, source = resolve_energy_params(
            ReplicationLayer.CONTAINER, 15.0, 100.0, model_layer="api"
        )
        assert source == "calibrated"
        assert dyn == pytest.approx(0.9, abs=1e-3)

    def test_global_fallback_when_layer_lacks_energy(self):
        write_model_file(_throughput_fit(), energy=_energy_fit(5.0, 0.5, 0.01))
        _, dyn, _, source = resolve_energy_params(
            ReplicationLayer.CONTAINER, 15.0, 100.0, model_layer="missing"
        )
        assert source == "calibrated"
        assert dyn == pytest.approx(0.5, abs=1e-3)

    def test_per_layer_precedence_over_global(self):
        write_model_file(_throughput_fit(), energy=_energy_fit(5.0, 0.5, 0.01))
        write_model_file(
            _throughput_fit(), layer="api", energy=_energy_fit(7.0, 0.9, 0.01)
        )
        _, dyn_layer, _, _ = resolve_energy_params(
            ReplicationLayer.CONTAINER, 15.0, 100.0, model_layer="api"
        )
        _, dyn_global, _, _ = resolve_energy_params(
            ReplicationLayer.CONTAINER, 15.0, 100.0, model_layer=None
        )
        assert dyn_layer == pytest.approx(0.9, abs=1e-3)
        assert dyn_global == pytest.approx(0.5, abs=1e-3)

    def test_uncommitted_legacy_energy_fields_are_ignored(self):
        global_model_path().write_text(
            json.dumps(
                {
                    "concurrency": 8.0,
                    "overhead_beta": 0.02,
                    "energy_idle_w": 999.0,
                    "energy_dyn_j_per_req": 999.0,
                    "energy_beta": 0.4,
                }
            )
        )
        idle, dyn, beta, source = resolve_energy_params(
            ReplicationLayer.CONTAINER, 15.0, 100.0
        )
        assert source == "default"
        assert idle == pytest.approx(0.3)
        assert dyn == pytest.approx(0.147)
        assert beta == pytest.approx(0.005)


# ── layer_energy ──────────────────────────────────────────────────────────────


class TestLayerEnergy:
    def test_default_source_and_positive_watts(self):
        le = layer_energy(ReplicationLayer.CONTAINER, 4, 500.0, 100.0, 15.0)
        assert le.source == "default"
        assert le.watts > 0
        assert le.joules_per_request is not None

    def test_calibrated_source(self):
        write_model_file(_throughput_fit(), energy=_energy_fit(5.0, 0.5, 0.01))
        le = layer_energy(ReplicationLayer.NODE, 4, 500.0, 100.0, 15.0)
        assert le.source == "calibrated"

    def test_nonpositive_capacity_yields_none_ratios(self):
        le = layer_energy(ReplicationLayer.NODE, 3, 500.0, 0.0, 15.0)
        assert le.joules_per_request is None
        assert le.eei is None


# ── commitment: backward-compat + tamper ──────────────────────────────────────


class TestEnergyCommitment:
    def test_v019_shape_record_still_verifies_ok(self):
        # A record written in the v0.19 scheme (no energy keys) must re-hash to
        # its stored digest under v0.20 code — the byte-identical guarantee.
        result = _throughput_fit()
        digest = commitment_digest(result)  # energy=None → v0.19 content
        record = {
            "concurrency": result.concurrency,
            "overhead_beta": result.overhead_beta,
            "r_squared": result.r_squared,
            "rmse": result.rmse,
            "observations": [
                [o.rps, o.latency_ms, o.replicas] for o in result.observations
            ],
            "calibration_commitment": {
                "schema": CALIBRATION_COMMITMENT_SCHEMA,
                "digest": digest,
            },
        }
        assert verify_commitment(record) is True

    def test_energy_bearing_record_roundtrips(self):
        write_model_file(_throughput_fit(), energy=_energy_fit())
        model = load_calibrated_model()
        assert "energy_idle_w" in model
        assert verify_commitment(model) is True

    @pytest.mark.parametrize(
        "field,new_value",
        [
            ("energy_idle_w", 999.0),
            ("energy_dyn_j_per_req", 999.0),
            ("energy_beta", 0.4),
            ("energy_r_squared", 0.1),
            ("energy_rmse", 999.0),
        ],
    )
    def test_tamper_energy_scalar_field(self, field, new_value):
        write_model_file(_throughput_fit(), energy=_energy_fit())
        path = global_model_path()
        data = json.loads(path.read_text())
        data[field] = new_value
        path.write_text(json.dumps(data))
        assert verify_commitment(load_calibrated_model()) is False

    def test_tamper_energy_observations(self):
        write_model_file(_throughput_fit(), energy=_energy_fit())
        path = global_model_path()
        data = json.loads(path.read_text())
        data["energy_observations"] = [[1, 1, 1, 1]]
        path.write_text(json.dumps(data))
        assert verify_commitment(load_calibrated_model()) is False

    def test_analyze_fails_closed_on_tampered_energy(self):
        write_model_file(_throughput_fit(), energy=_energy_fit())
        path = global_model_path()
        data = json.loads(path.read_text())
        data["energy_idle_w"] = 999.0
        path.write_text(json.dumps(data))
        result = _invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
        assert result.exit_code == 2
        assert "tamper" in result.output.lower()


# ── CLI surface ───────────────────────────────────────────────────────────────


class TestEnergyCLI:
    def test_analyze_show_all_energy_columns(self):
        result = _invoke(
            "analyze", "-r", "500", "-l", "80", "-c", "container", "--show-all"
        )
        assert result.exit_code == 0
        out = strip_ansi(result.output)
        assert "EEI" in out
        assert "J/req" in out
        assert "energy model" in out

    def test_analyze_replica_power_watts_note_default(self):
        result = _invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
        assert result.exit_code == 0
        assert "MVP defaults" in strip_ansi(result.output)

    def test_analyze_replica_power_watts_too_low_rejected(self):
        result = _invoke(
            "analyze",
            "-r",
            "500",
            "-l",
            "80",
            "-c",
            "container",
            "--replica-power-watts",
            "0",
        )
        assert result.exit_code == 2

    def test_analyze_replica_power_watts_nan_rejected(self):
        result = _invoke(
            "analyze",
            "-r",
            "500",
            "-l",
            "80",
            "-c",
            "container",
            "--replica-power-watts",
            "nan",
        )
        assert result.exit_code == 2

    def test_what_if_energy_output(self):
        result = _invoke("what-if", "-r", "200", "-s", "600", "-l", "80", "-c", "pod")
        assert result.exit_code == 0
        assert "Est. power" in strip_ansi(result.output)

    def test_slo_jreq_column_present(self):
        result = _invoke("slo", "-r", "200", "-l", "80", "--p99-target-ms", "500")
        assert result.exit_code == 0
        assert "J/req" in strip_ansi(result.output)

    def test_calibrate_energy_observation_end_to_end(self):
        result = _invoke(
            "calibrate",
            "--observation",
            "100:50:2",
            "--observation",
            "300:80:5",
            "--observation",
            "500:80:6",
            "--energy-observation",
            "300:80:2:162.07944154167984",
            "--energy-observation",
            "500:80:5:283.0471895621705",
            "--energy-observation",
            "700:80:8:404.55609079175885",
            "--energy-observation",
            "400:80:3:219.39444915467245",
        )
        assert result.exit_code == 0
        assert "Energy fit" in strip_ansi(result.output)
        model = load_calibrated_model()
        assert "energy_idle_w" in model
        assert verify_commitment(model) is True

    def test_calibrate_energy_observation_uses_calibrated_source(self):
        _invoke(
            "calibrate",
            "--observation",
            "100:50:2",
            "--observation",
            "300:80:5",
            "--energy-observation",
            "300:80:2:162.07944154167984",
            "--energy-observation",
            "500:80:5:283.0471895621705",
            "--energy-observation",
            "700:80:8:404.55609079175885",
            "--energy-observation",
            "400:80:3:219.39444915467245",
        )
        result = _invoke(
            "analyze", "-r", "500", "-l", "80", "-c", "container", "--show-all"
        )
        assert result.exit_code == 0
        assert "calibrated" in strip_ansi(result.output)

    def test_calibrate_bad_energy_observation_exits_2(self):
        result = _invoke(
            "calibrate",
            "--observation",
            "100:50:2",
            "--observation",
            "300:80:5",
            "--energy-observation",
            "not-a-quad",
        )
        assert result.exit_code == 2

    def test_calibrate_large_energy_observation_is_clean_error(self):
        result = _invoke(
            "calibrate",
            "--observation",
            "100:50:1",
            "--observation",
            "200:50:2",
            "--energy-observation",
            "100:50:1:30000",
            "--energy-observation",
            "200:50:2:40000",
            "--energy-observation",
            "300:50:3:50000",
        )
        assert result.exit_code == 2
        assert "traceback" not in result.output.lower()
        assert "energy calibration error" in strip_ansi(result.output).lower()


# ── P2-1: cross-scope energy tamper gate ──────────────────────────────────────


def _tamper_global(**changes) -> None:
    """Mutate top-level (global) fields of the model file in place."""
    path = global_model_path()
    data = json.loads(path.read_text())
    data.update(changes)
    path.write_text(json.dumps(data))


class TestCrossScopeEnergyGate:
    def test_scope_reports_default_global_and_layer(self):
        assert resolve_energy_fit_scope(None) == "default"
        write_model_file(_throughput_fit(), energy=_energy_fit())
        assert resolve_energy_fit_scope(None) == "global"
        # A named layer without its own energy fit falls back to global.
        write_model_file(_throughput_fit(), layer="api")
        assert resolve_energy_fit_scope("api") == "global"
        # A named layer with its own energy fit is read at layer scope.
        write_model_file(_throughput_fit(), layer="api", energy=_energy_fit(7.0, 0.9))
        assert resolve_energy_fit_scope("api") == "layer"

    def test_named_layer_analyze_tampered_global_energy_fails_closed(self):
        # Global record carries the energy fit; the named layer's own record has
        # an intact throughput commitment but NO energy fit → energy falls back
        # to the tampered global record, which must be gated and fail closed.
        write_model_file(_throughput_fit(), energy=_energy_fit())
        write_model_file(_throughput_fit(), layer="api")  # no energy on api
        _tamper_global(energy_idle_w=999.0)
        result = _invoke(
            "analyze", "-r", "500", "-l", "80", "-c", "container", "-L", "api"
        )
        assert result.exit_code == 2
        assert "tamper" in result.output.lower()

    def test_named_layer_analyze_intact_global_energy_calibrated(self):
        write_model_file(_throughput_fit(), energy=_energy_fit())
        write_model_file(_throughput_fit(), layer="api")  # no energy on api
        result = _invoke(
            "analyze",
            "-r",
            "500",
            "-l",
            "80",
            "-c",
            "container",
            "-L",
            "api",
            "--show-all",
        )
        assert result.exit_code == 0
        assert "calibrated" in strip_ansi(result.output)

    def test_named_layer_own_energy_not_cross_gated(self):
        # api reads its OWN energy fit → the tampered global energy is never
        # consumed, so analyze must NOT be gated on it (no over-gating).
        write_model_file(_throughput_fit(), energy=_energy_fit(5.0, 0.5))
        write_model_file(_throughput_fit(), layer="api", energy=_energy_fit(7.0, 0.9))
        _tamper_global(energy_idle_w=999.0)
        result = _invoke(
            "analyze", "-r", "500", "-l", "80", "-c", "container", "-L", "api"
        )
        assert result.exit_code == 0

    def test_global_analyze_intact_energy_unchanged(self):
        # Plain global analyze: energy scope == gated scope, behaviour unchanged.
        write_model_file(_throughput_fit(), energy=_energy_fit())
        result = _invoke("analyze", "-r", "500", "-l", "80", "-c", "container")
        assert result.exit_code == 0
        assert "calibrated" in strip_ansi(result.output)


# ── P2-2: what-if / slo honour the tamper signal ──────────────────────────────


class TestWhatIfSloGating:
    def test_what_if_fails_closed_on_tampered_concurrency(self):
        write_model_file(_throughput_fit())
        _tamper_global(concurrency=999.0)
        result = _invoke("what-if", "-r", "200", "-s", "600", "-l", "80", "-c", "pod")
        assert result.exit_code == 2
        assert "tamper" in result.output.lower()

    def test_what_if_fails_closed_on_tampered_global_energy_named_layer(self):
        write_model_file(_throughput_fit(), energy=_energy_fit())
        write_model_file(_throughput_fit(), layer="api")  # no energy on api
        _tamper_global(energy_idle_w=999.0)
        result = _invoke(
            "what-if", "-r", "200", "-s", "600", "-l", "80", "-c", "pod", "-L", "api"
        )
        assert result.exit_code == 2
        assert "tamper" in result.output.lower()

    def test_slo_fails_closed_on_tampered_concurrency(self):
        write_model_file(_throughput_fit())
        _tamper_global(concurrency=999.0)
        result = _invoke("slo", "-r", "200", "-l", "80", "--p99-target-ms", "500")
        assert result.exit_code == 2
        assert "tamper" in result.output.lower()

    def test_slo_fails_closed_on_tampered_global_energy_named_layer(self):
        write_model_file(_throughput_fit(), energy=_energy_fit())
        write_model_file(_throughput_fit(), layer="api")  # no energy on api
        _tamper_global(energy_idle_w=999.0)
        result = _invoke(
            "slo", "-r", "200", "-l", "80", "--p99-target-ms", "500", "-L", "api"
        )
        assert result.exit_code == 2
        assert "tamper" in result.output.lower()

    def test_what_if_runs_on_legacy_model(self):
        global_model_path().write_text('{"concurrency": 8.0, "overhead_beta": 0.02}')
        result = _invoke("what-if", "-r", "200", "-s", "600", "-l", "80", "-c", "pod")
        assert result.exit_code == 0

    def test_slo_runs_on_legacy_model(self):
        global_model_path().write_text('{"concurrency": 8.0, "overhead_beta": 0.02}')
        result = _invoke("slo", "-r", "200", "-l", "80", "--p99-target-ms", "500")
        assert result.exit_code == 0

    def test_what_if_runs_without_model(self):
        result = _invoke("what-if", "-r", "200", "-s", "600", "-l", "80", "-c", "pod")
        assert result.exit_code == 0

    def test_slo_runs_without_model(self):
        result = _invoke("slo", "-r", "200", "-l", "80", "--p99-target-ms", "500")
        assert result.exit_code == 0
