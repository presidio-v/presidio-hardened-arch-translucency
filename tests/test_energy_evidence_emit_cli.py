"""CLI tests for the Energy Arc finale (v0.24.0).

Covers `pat energy-evidence-emit` (store-derived measured-energy reading) and
`pat observe verify --emit-head` (the external-anchoring path). The governing
constraints under test: E1a (store-only figures; a prometheus-override row in
the window is refused), emission gated on a CLEAN energy-chain walk (a broken
chain is never anchored), one reading = one meter = one layer, and the
security-event log discipline (counts/digests, never raw energy figures).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.observe import (
    EnergyObservation,
    Observation,
    _record_energy_observation_unchecked,
    energy_chain_head_hash,
    record_observation,
)

runner = CliRunner()
_PARENT = "a" * 64


def _emit(*args):
    return runner.invoke(app, ["--skip-audit", "energy-evidence-emit", *args])


def _verify(db, *extra):
    return runner.invoke(
        app, ["--skip-audit", "observe", "verify", "--db", str(db), *extra]
    )


def _recent(seconds_ago: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def _eobs(
    ts: datetime,
    *,
    watts: float = 120.0,
    layer: str = "node",
    meter: str = "rapl",
    source: str = "prometheus",
    window_s: float = 30.0,
) -> EnergyObservation:
    return EnergyObservation(
        timestamp=ts,
        watts=watts,
        joules=watts * window_s,
        window_s=window_s,
        rps=500.0,
        layer=layer,
        replicas=4,
        meter=meter,
        source=source,
    )


def _seed_recent(db, n: int = 3, **kw) -> None:
    kw.setdefault("window_s", 10.0)
    first = _recent(120)
    for i in range(n):
        _record_energy_observation_unchecked(
            _eobs(first + timedelta(seconds=i * 10), **kw), db_path=db
        )


# ── pat energy-evidence-emit ─────────────────────────────────────────────────


class TestEnergyEvidenceEmit:
    def test_store_round_trip(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_recent(db, 3)
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 0, res.output
        reading = json.loads(res.output.strip())
        content = reading["attested_content"]
        assert reading["schema"] == "presidio-hardened/energy-reading@1"
        assert content["meter"] == "rapl"
        assert content["layer"] == "node"
        # The reading anchors on the current energy-chain head.
        assert content["energy_chain_head"] == energy_chain_head_hash(db_path=db)
        # Figures are string-decimals derived from the measured joules.
        assert isinstance(content["energy_wh"], str)
        assert isinstance(content["mean_power_w"], str)
        assert "parents" not in content

    def test_window_filters_out_old_rows(self, tmp_path):
        db = tmp_path / "obs.db"
        # An OLD row on a different layer/meter would trigger a mixed refusal if
        # it were included; the window must exclude it.
        _record_energy_observation_unchecked(
            _eobs(_recent(7200), layer="pod", meter="dcgm"), db_path=db
        )
        _seed_recent(db, 2, layer="node", meter="rapl")
        res = _emit("--window-minutes", "5", "--db", str(db))
        assert res.exit_code == 0, res.output
        content = json.loads(res.output.strip())["attested_content"]
        assert content["layer"] == "node"
        assert content["meter"] == "rapl"

    def test_empty_window_exits_two(self, tmp_path):
        db = tmp_path / "obs.db"
        _record_energy_observation_unchecked(_eobs(_recent(7200)), db_path=db)
        res = _emit("--window-minutes", "5", "--db", str(db))
        assert res.exit_code == 2
        assert not res.output.strip().startswith("{")

    def test_empty_store_exits_two(self, tmp_path):
        db = tmp_path / "obs.db"
        res = _emit("--window-minutes", "5", "--db", str(db))
        assert res.exit_code == 2
        assert "no measured-energy observations" in res.output.lower()

    def test_mixed_meter_refused(self, tmp_path):
        db = tmp_path / "obs.db"
        _record_energy_observation_unchecked(
            _eobs(_recent(100), meter="rapl"), db_path=db
        )
        _record_energy_observation_unchecked(
            _eobs(_recent(90), meter="dcgm"), db_path=db
        )
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 1
        assert "mixed meters" in res.output.lower()

    def test_mixed_layer_refused(self, tmp_path):
        db = tmp_path / "obs.db"
        _record_energy_observation_unchecked(
            _eobs(_recent(100), layer="node"), db_path=db
        )
        _record_energy_observation_unchecked(
            _eobs(_recent(90), layer="pod"), db_path=db
        )
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 1
        assert "mixed layers" in res.output.lower()

    def test_override_row_refused_with_count(self, tmp_path):
        db = tmp_path / "obs.db"
        _record_energy_observation_unchecked(_eobs(_recent(100)), db_path=db)
        _record_energy_observation_unchecked(
            _eobs(_recent(90), source="prometheus-override"), db_path=db
        )
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 1
        out = res.output.lower()
        assert "e1a" in out
        assert "1 of 2" in res.output  # names the row count
        assert "override" in out
        assert not res.output.strip().startswith("{")

    def test_broken_chain_refused_exit_one(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_recent(db, 3)
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE energy_observations SET watts = 1 WHERE id = 2")
        conn.commit()
        conn.close()
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 1
        assert "broken" in res.output.lower()
        assert not res.output.strip().startswith("{")

    def test_parent_passthrough(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_recent(db, 2)
        res = _emit("--window-minutes", "60", "--db", str(db), "--parent", _PARENT)
        assert res.exit_code == 0, res.output
        content = json.loads(res.output.strip())["attested_content"]
        assert content["parents"] == [_PARENT]

    def test_security_log_discipline(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_recent(db, 2)
        with patch("presidio_arch_translucency.cli.log_security_event") as log:
            res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 0, res.output
        assert log.called
        _event, payload = log.call_args[0]
        # Never log the raw energy figures; do log counts + the head digest.
        assert "energy_wh" not in payload
        assert "mean_power_w" not in payload
        assert payload["rows"] == 2
        assert len(payload["energy_chain_head_16"]) == 16

    def test_internal_head_mismatch_fails_closed(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_recent(db, 1)
        forged = {"attested_content": {"energy_chain_head": "0" * 64}}
        with patch(
            "presidio_arch_translucency.cli._derive_energy_reading",
            return_value=forged,
        ):
            res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 1
        assert "internal integrity error" in res.output.lower()
        assert not res.stdout.strip().startswith("{")


# ── pat observe verify --emit-head ───────────────────────────────────────────


def _seed_energy(db, n=3, **kw):
    kw.setdefault("window_s", 10.0)
    first = _recent(300)
    for i in range(n):
        _record_energy_observation_unchecked(
            _eobs(first + timedelta(seconds=i * 10), **kw), db_path=db
        )


class TestVerifyEmitHead:
    def test_clean_report_then_record(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_energy(db, 3)
        res = _verify(db, "--emit-head")
        assert res.exit_code == 0, res.output
        # The two-section report renders AND the record is emitted.
        assert "Measured energy chain" in res.output
        assert '"schema":"presidio-hardened/energy-reading@1"' in res.output
        # The JSON line is parseable.
        json_line = next(ln for ln in res.output.splitlines() if ln.startswith("{"))
        content = json.loads(json_line)["attested_content"]
        assert content["energy_chain_head"] == energy_chain_head_hash(db_path=db)
        # Machine stdout is one JSON record; reports are diagnostics on stderr.
        assert json.loads(res.stdout.strip())["schema"].endswith("energy-reading@1")
        assert "Measured energy chain" not in res.stdout
        assert "Measured energy chain" in res.stderr

    def test_broken_chain_exit_one_no_record(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_energy(db, 3)
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE energy_observations SET watts = 1 WHERE id = 2")
        conn.commit()
        conn.close()
        res = _verify(db, "--emit-head")
        assert res.exit_code == 1
        assert '"schema":"presidio-hardened/energy-reading@1"' not in res.output

    def test_empty_energy_chain_exit_two_no_record(self, tmp_path):
        db = tmp_path / "obs.db"
        res = _verify(db, "--emit-head")
        assert res.exit_code == 2
        assert "no energy chain to anchor" in res.output.lower()
        assert "family-reserved reading type" in res.output.lower()

    def test_serving_only_store_defers(self, tmp_path):
        db = tmp_path / "obs.db"
        record_observation(
            Observation(
                _recent(60), 500.0, 80.0, 140.0, 480.0, "container", 6, "manual"
            ),
            db_path=db,
        )
        res = _verify(db, "--emit-head")
        assert res.exit_code == 2
        assert "no energy chain to anchor" in res.output.lower()

    def test_serving_legacy_does_not_block_energy_emit_head(self, tmp_path):
        db = tmp_path / "obs.db"
        # A legacy (unchained) serving row: recorded then its chain link removed.
        record_observation(
            Observation(
                _recent(120), 500.0, 80.0, 140.0, 480.0, "container", 6, "manual"
            ),
            db_path=db,
        )
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM observation_chain")
        conn.commit()
        conn.close()
        _seed_energy(db, 2)
        res = _verify(db, "--emit-head")
        # The energy record IS emitted even though the serving chain is legacy.
        assert '"schema":"presidio-hardened/energy-reading@1"' in res.output

    def test_default_verify_output_byte_identical_without_flag(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_energy(db, 3)
        baseline = _verify(db)
        # Re-run identical store contents is deterministic; the --emit-head path
        # must not alter the report section preceding the emitted JSON.
        with_flag = _verify(db, "--emit-head")
        assert with_flag.stderr == baseline.stdout
        assert with_flag.stdout.startswith('{"schema"')

    def test_emit_head_internal_mismatch_fails_closed(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_energy(db, 1)
        forged = {"attested_content": {"energy_chain_head": "0" * 64}}
        with patch(
            "presidio_arch_translucency.cli._derive_energy_reading",
            return_value=forged,
        ):
            res = _verify(db, "--emit-head")
        assert res.exit_code == 1
        assert "internal integrity error" in res.output.lower()
        assert not res.stdout.strip().startswith("{")


# ── FIX 1: span-overlap closure (signed window == scanned coverage) ───────────


class TestSpanOverlapClosure:
    def test_override_outside_window_pulled_into_closure_and_refuses(self, tmp_path):
        # The review's exact scenario: a preset row 240s ago with a 120s window
        # pulls the emitted window-start back to 360s ago; an override row 320s
        # ago sits OUTSIDE the naive [now-300, now] selection but INSIDE the
        # measured span the signature would claim. The closure must pull it in and
        # refuse — no sliver of unattested coverage under the signature.
        db = tmp_path / "obs.db"
        _record_energy_observation_unchecked(
            _eobs(_recent(320), source="prometheus-override"), db_path=db
        )
        _record_energy_observation_unchecked(
            _eobs(_recent(240), window_s=120.0), db_path=db
        )
        res = _emit("--window-minutes", "5", "--db", str(db))
        assert res.exit_code == 1
        out = res.output.lower()
        assert "e1a" in out
        assert "override" in out
        assert "1 of 2" in res.output
        assert not res.output.strip().startswith("{")

    def test_closure_growth_pulls_transitive_chain_then_refuses_overlap(self, tmp_path):
        # A (100s ago, 120s window) pulls B (200s ago, 120s window) which pulls
        # C (300s ago). Only A is in the naive 2-minute window; the fixed-point
        # closure must include all three in the figures and the window.
        db = tmp_path / "obs.db"
        _record_energy_observation_unchecked(
            _eobs(_recent(300), window_s=30.0), db_path=db
        )
        _record_energy_observation_unchecked(
            _eobs(_recent(200), window_s=120.0), db_path=db
        )
        _record_energy_observation_unchecked(
            _eobs(_recent(100), window_s=120.0), db_path=db
        )
        res = _emit("--window-minutes", "2", "--db", str(db))
        assert res.exit_code == 1
        assert "overlap" in res.output.lower()

    def test_simple_all_in_window_matches_direct_sum(self, tmp_path):
        # Clamped/simple case: every row is inside the interval, so the closure is
        # exactly the selection — figures identical to a straight sum.
        db = tmp_path / "obs.db"
        first = _recent(120)
        for offset in (0, 30, 60):
            _record_energy_observation_unchecked(
                _eobs(
                    first + timedelta(seconds=offset),
                    watts=100.0,
                    window_s=30.0,
                ),
                db_path=db,
            )
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 0, res.output
        content = json.loads(res.output.strip())["attested_content"]
        # 3 rows × 100 W × 30 s = 9000 J = 2.5 Wh; mean power = 100 W.
        assert float(content["energy_wh"]) == 9000.0 / 3600.0
        assert float(content["mean_power_w"]) == 100.0

    def test_unmeasured_gap_is_refused(self, tmp_path):
        db = tmp_path / "obs.db"
        first = _recent(120)
        _record_energy_observation_unchecked(_eobs(first, window_s=10.0), db_path=db)
        _record_energy_observation_unchecked(
            _eobs(first + timedelta(seconds=20), window_s=10.0), db_path=db
        )
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 1
        assert "unmeasured gap" in res.output.lower()

    def test_fixed_point_terminates_and_refuses_long_overlapping_chain(self, tmp_path):
        # 50 rows, each span overlapping the next — the fixed point must close
        # over all 50 and terminate quickly (no runaway iteration).
        db = tmp_path / "obs.db"
        for i in range(50):
            _record_energy_observation_unchecked(
                _eobs(_recent(i * 10), window_s=20.0), db_path=db
            )
        res = _emit("--window-minutes", "1", "--db", str(db))
        assert res.exit_code == 1
        assert "overlap" in res.output.lower()


# ── FIX 2: single-snapshot chain gate (anchored head == last verified link) ──


class TestSnapshotChainGate:
    def test_emitted_head_equals_snapshot_last_verified_link(self, tmp_path):
        from presidio_arch_translucency.observe import verified_energy_snapshot

        db = tmp_path / "obs.db"
        _seed_recent(db, 3)
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 0, res.output
        content = json.loads(res.output.strip())["attested_content"]
        _report, _rows, head = verified_energy_snapshot(db_path=db)
        assert content["energy_chain_head"] == head

    def test_head_bounded_to_snapshot_not_later_append(self, tmp_path):
        # Emit once, append a row, emit again: the second head advances, proving
        # each emission anchors on its own snapshot's tip (never a fresher row
        # bleeding into an already-derived reading).
        db = tmp_path / "obs.db"
        first_ts = _recent(20)
        _record_energy_observation_unchecked(_eobs(first_ts, window_s=10.0), db_path=db)
        first = json.loads(
            _emit("--window-minutes", "60", "--db", str(db)).output.strip()
        )["attested_content"]["energy_chain_head"]
        _record_energy_observation_unchecked(
            _eobs(first_ts + timedelta(seconds=10), window_s=10.0), db_path=db
        )
        second = json.loads(
            _emit("--window-minutes", "60", "--db", str(db)).output.strip()
        )["attested_content"]["energy_chain_head"]
        assert first != second

    def test_broken_chain_snapshot_refuses_exit_one(self, tmp_path):
        db = tmp_path / "obs.db"
        _seed_recent(db, 3)
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE energy_observations SET watts = 1 WHERE id = 2")
        conn.commit()
        conn.close()
        res = _emit("--window-minutes", "60", "--db", str(db))
        assert res.exit_code == 1
        assert "broken" in res.output.lower()
        assert not res.output.strip().startswith("{")
