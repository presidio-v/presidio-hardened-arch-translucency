"""Tests for the measured-energy store and its parallel hash chain (v0.21.0).

Covers EnergyObservation validation (each field, non-finite, the strict meter
enum incl. rejection of "manual"/"analytic"), record+load round-trip, the
private-file chmod discipline, and the energy chain: append/verify plus every
tamper class (edit / delete / insert / reorder). Also checks that energy writes
never touch the serving chain and that verify_all_chains reports both.

The chain proves the local energy history was not rewritten after the fact; it
does NOT assert the readings were honest at capture — out of scope by design.
"""

from __future__ import annotations

import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest

from presidio_arch_translucency.observe import (
    VALID_ENERGY_SOURCES,
    VALID_METERS,
    EnergyObservation,
    EnergyObservationError,
    Observation,
    _record_energy_observation_unchecked,
    count_energy_observations,
    energy_record_hash,
    load_energy_observations,
    load_verified_energy_observations,
    record_energy_observation,
    record_observation,
    verify_all_chains,
    verify_chain,
    verify_energy_chain,
)

_T0 = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    return tmp_path / "observations.db"


def _eobs(
    ts: datetime,
    watts: float = 120.0,
    layer: str = "node",
    replicas: int = 4,
    meter: str = "rapl",
    window_s: float = 60.0,
    rps: float = 500.0,
) -> EnergyObservation:
    return EnergyObservation(
        timestamp=ts,
        watts=watts,
        joules=watts * window_s,
        window_s=window_s,
        rps=rps,
        layer=layer,
        replicas=replicas,
        meter=meter,
        source="prometheus",
    )


def _seed(db, n: int = 4) -> None:
    for i in range(n):
        _record_energy_observation_unchecked(
            _eobs(_T0 + timedelta(minutes=i), watts=100.0 + i, replicas=1 + i),
            db_path=db,
        )


# ── validation ────────────────────────────────────────────────────────────────


def test_valid_observation_validates(db):
    assert _eobs(_T0).validate() is not None


@pytest.mark.parametrize("meter", VALID_METERS)
def test_all_measured_meters_accepted(meter):
    _eobs(_T0, meter=meter).validate()


@pytest.mark.parametrize("bad_meter", ["manual", "analytic", "", "MODEL", "estimator"])
def test_bad_meter_rejected(bad_meter):
    with pytest.raises(EnergyObservationError, match="meter"):
        _eobs(_T0, meter=bad_meter).validate()


def test_manual_and_analytic_explicitly_rejected():
    # E1a: an unmeasured / modelled watt cannot enter the store.
    for meter in ("manual", "analytic"):
        with pytest.raises(EnergyObservationError):
            _eobs(_T0, meter=meter).validate()


@pytest.mark.parametrize("field", ["watts", "joules", "rps"])
def test_negative_numeric_rejected(field):
    kwargs = {field: -1.0}
    with pytest.raises(EnergyObservationError, match=field):
        _replace(_eobs(_T0), **kwargs).validate()


@pytest.mark.parametrize("field", ["watts", "joules", "window_s", "rps"])
def test_non_finite_rejected(field):
    with pytest.raises(EnergyObservationError):
        _replace(_eobs(_T0), **{field: float("nan")}).validate()
    with pytest.raises(EnergyObservationError):
        _replace(_eobs(_T0), **{field: float("inf")}).validate()


def test_zero_window_rejected():
    with pytest.raises(EnergyObservationError, match="window_s"):
        _replace(_eobs(_T0), window_s=0.0).validate()


def test_negative_window_rejected():
    with pytest.raises(EnergyObservationError, match="window_s"):
        _replace(_eobs(_T0), window_s=-5.0).validate()


def test_zero_watts_allowed():
    # A genuine 0 W measurement is valid (0 is not "missing").
    _replace(_eobs(_T0), watts=0.0, joules=0.0).validate()


def test_zero_rps_allowed():
    _replace(_eobs(_T0), rps=0.0).validate()


def test_replicas_below_one_rejected():
    with pytest.raises(EnergyObservationError, match="replicas"):
        _replace(_eobs(_T0), replicas=0).validate()


def test_empty_layer_rejected():
    with pytest.raises(EnergyObservationError, match="layer"):
        _replace(_eobs(_T0), layer="  ").validate()


def test_empty_source_rejected():
    with pytest.raises(EnergyObservationError, match="source"):
        _replace(_eobs(_T0), source="").validate()


@pytest.mark.parametrize("source", VALID_ENERGY_SOURCES)
def test_valid_energy_sources_accepted(source):
    # Both 'prometheus' and 'prometheus-override' validate (P1-1).
    _replace(_eobs(_T0), source=source).validate()


@pytest.mark.parametrize("bad_source", ["manual", "analytic", "prom", "PROMETHEUS"])
def test_source_not_in_allowlist_rejected(bad_source):
    with pytest.raises(EnergyObservationError, match="source"):
        _replace(_eobs(_T0), source=bad_source).validate()


# ── joules ≈ watts × window_s consistency (P3) ────────────────────────────────


def test_joules_inconsistent_with_watts_window_rejected():
    # watts=120, window=60 -> joules must be ~7200; 9999 is inconsistent.
    with pytest.raises(EnergyObservationError, match="joules"):
        _replace(_eobs(_T0), joules=9999.0).validate()


def test_joules_consistent_within_tolerance_accepted():
    # A float rounding wobble within the relative tolerance still validates.
    obs = _eobs(_T0, watts=120.0)  # joules seeded as 120*60 = 7200
    _replace(obs, joules=7200.0 + 1e-6).validate()


def test_source_override_distinguishes_chain_hash():
    # source is hash-chained, so an overridden reading hashes differently from an
    # otherwise-identical preset-attested one (P1-1).
    base = _eobs(_T0)
    preset = _replace(base, source="prometheus")
    override = _replace(base, source="prometheus-override")
    assert energy_record_hash(preset, "0" * 64, 0) != energy_record_hash(
        override, "0" * 64, 0
    )


def test_bad_timestamp_rejected():
    with pytest.raises(EnergyObservationError, match="timestamp"):
        _replace(_eobs(_T0), timestamp="2026-07-15").validate()


def _replace(obs: EnergyObservation, **kwargs) -> EnergyObservation:
    data = {
        "timestamp": obs.timestamp,
        "watts": obs.watts,
        "joules": obs.joules,
        "window_s": obs.window_s,
        "rps": obs.rps,
        "layer": obs.layer,
        "replicas": obs.replicas,
        "meter": obs.meter,
        "source": obs.source,
    }
    data.update(kwargs)
    return EnergyObservation(**data)


# ── record + load round-trip ──────────────────────────────────────────────────


def test_record_and_load_round_trip(db):
    eid = _record_energy_observation_unchecked(_eobs(_T0, watts=123.5), db_path=db)
    assert isinstance(eid, int) and eid >= 1
    (loaded,) = load_energy_observations(db_path=db)
    assert loaded.watts == pytest.approx(123.5)
    assert loaded.joules == pytest.approx(123.5 * 60.0)
    assert loaded.window_s == pytest.approx(60.0)
    assert loaded.layer == "node"
    assert loaded.meter == "rapl"
    assert loaded.source == "prometheus"
    assert loaded.timestamp.tzinfo is not None


def test_record_rejects_invalid_before_write(db):
    with pytest.raises(EnergyObservationError):
        record_energy_observation(_eobs(_T0, meter="manual"), db_path=db)
    assert count_energy_observations(db_path=db) == 0


def test_public_record_rejects_unsealed_caller_observation(db):
    with pytest.raises(EnergyObservationError, match="collector-sealed"):
        record_energy_observation(_eobs(_T0), db_path=db)
    assert not db.exists()


def test_load_filters_by_layer_and_meter(db):
    _record_energy_observation_unchecked(
        _eobs(_T0, layer="node", meter="rapl"), db_path=db
    )
    _record_energy_observation_unchecked(
        _eobs(_T0 + timedelta(minutes=1), layer="node", meter="dcgm"), db_path=db
    )
    _record_energy_observation_unchecked(
        _eobs(_T0 + timedelta(minutes=2), layer="container", meter="rapl"),
        db_path=db,
    )
    assert count_energy_observations(db_path=db) == 3
    assert count_energy_observations(db_path=db, layer="node") == 2
    assert count_energy_observations(db_path=db, meter="rapl") == 2
    assert count_energy_observations(db_path=db, layer="node", meter="dcgm") == 1
    rows = load_energy_observations(db_path=db, layer="node")
    assert {r.meter for r in rows} == {"rapl", "dcgm"}


def test_load_limit_returns_most_recent_oldest_first(db):
    _seed(db, 5)
    rows = load_energy_observations(db_path=db, limit=2)
    assert len(rows) == 2
    assert rows[0].timestamp < rows[1].timestamp  # oldest-first within the window


def test_chmod_private_file_discipline(db):
    _record_energy_observation_unchecked(_eobs(_T0), db_path=db)
    mode = stat.S_IMODE(db.stat().st_mode)
    # Same private-file discipline as the serving store (0o600), best-effort.
    assert mode & 0o077 == 0


# ── chain: build + verify ─────────────────────────────────────────────────────


def test_empty_energy_chain_verifies(db):
    report = verify_energy_chain(db_path=db)
    assert report.ok is True
    assert report.total == 0


def test_clean_energy_chain_verifies(db):
    _seed(db, 5)
    report = verify_energy_chain(db_path=db)
    assert report.ok is True
    assert report.total == 5
    assert report.chained == 5
    assert report.verified == 5
    assert report.legacy_count == 0
    assert report.fully_covered is True


# ── tamper classes ────────────────────────────────────────────────────────────


def test_energy_row_edit_detected(db):
    _seed(db, 4)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE energy_observations SET watts = 9999 WHERE id = 2")
    conn.commit()
    conn.close()
    report = verify_energy_chain(db_path=db)
    assert report.ok is False
    assert report.broken_obs_id == 2
    assert "hash mismatch" in report.break_reason


def test_verified_loader_refuses_tampered_chain(db):
    _seed(db, 2)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE energy_observations SET watts = 9999 WHERE id = 1")
    conn.commit()
    conn.close()
    with pytest.raises(EnergyObservationError, match="not fully verified"):
        load_verified_energy_observations(db_path=db)


def test_verified_loader_missing_store_is_empty_without_creation(db):
    assert load_verified_energy_observations(db_path=db) == []
    assert not db.exists()


def test_malformed_timestamp_reports_broken_chain_without_traceback(db):
    _seed(db, 1)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE energy_observations SET timestamp = 'not-a-time' WHERE id = 1")
    conn.commit()
    conn.close()
    report = verify_energy_chain(db_path=db)
    assert report.ok is False
    assert "malformed" in report.break_reason


def test_energy_chain_link_delete_detected(db):
    _seed(db, 4)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM energy_observation_chain WHERE seq = 1")
    conn.commit()
    conn.close()
    report = verify_energy_chain(db_path=db)
    assert report.ok is False
    assert "sequence gap" in report.break_reason


def test_energy_row_delete_detected(db):
    _seed(db, 4)
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM energy_observations WHERE id = 2")
    conn.commit()
    conn.close()
    assert verify_energy_chain(db_path=db).ok is False


def test_energy_unchained_insert_reported_legacy(db):
    _seed(db, 3)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO energy_observations "
        "(timestamp, watts, joules, window_s, rps, layer, replicas, meter, source) "
        "VALUES ('2026-05-01T00:00:00+00:00', 50, 3000, 60, 100, 'node', 2, "
        "'rapl', 'prometheus')"
    )
    conn.commit()
    conn.close()
    report = verify_energy_chain(db_path=db)
    assert report.ok is False
    assert report.legacy_count == 1
    assert report.verified == 3


def test_energy_reorder_detected(db):
    _seed(db, 4)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE energy_observation_chain SET seq = 99 WHERE seq = 1")
    conn.execute("UPDATE energy_observation_chain SET seq = 1 WHERE seq = 2")
    conn.execute("UPDATE energy_observation_chain SET seq = 2 WHERE seq = 99")
    conn.commit()
    conn.close()
    report = verify_energy_chain(db_path=db)
    assert report.ok is False
    assert "prev_hash mismatch" in report.break_reason


# ── cross-chain independence + verify_all_chains ──────────────────────────────


def test_energy_writes_do_not_touch_serving_chain(db):
    record_observation(
        Observation(_T0, 500.0, 80.0, 140.0, 480.0, "container", 6, "manual"),
        db_path=db,
    )
    _seed(db, 3)
    assert verify_chain(db_path=db).ok is True  # serving still intact
    assert verify_energy_chain(db_path=db).ok is True


def test_verify_all_chains_both_ok(db):
    record_observation(
        Observation(_T0, 500.0, 80.0, 140.0, 480.0, "container", 6, "manual"),
        db_path=db,
    )
    _seed(db, 2)
    serving, energy = verify_all_chains(db_path=db)
    assert serving.ok is True
    assert energy.ok is True


def test_verify_all_chains_energy_broken_serving_ok(db):
    record_observation(
        Observation(_T0, 500.0, 80.0, 140.0, 480.0, "container", 6, "manual"),
        db_path=db,
    )
    _seed(db, 3)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE energy_observations SET watts = 1 WHERE id = 2")
    conn.commit()
    conn.close()
    serving, energy = verify_all_chains(db_path=db)
    assert serving.ok is True
    assert energy.ok is False
    assert energy.broken_obs_id == 2


def test_verify_all_chains_serving_broken_energy_ok(db):
    record_observation(
        Observation(_T0, 500.0, 80.0, 140.0, 480.0, "container", 6, "manual"),
        db_path=db,
    )
    record_observation(
        Observation(
            _T0 + timedelta(minutes=1),
            510.0,
            80.0,
            140.0,
            480.0,
            "container",
            6,
            "manual",
        ),
        db_path=db,
    )
    _seed(db, 2)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE observations SET rps = 9999 WHERE id = 1")
    conn.commit()
    conn.close()
    serving, energy = verify_all_chains(db_path=db)
    assert serving.ok is False
    assert energy.ok is True


# ---------------------------------------------------------------------------
# Public chain-head accessors (v0.24.0) — the anchoring surface. A head is only
# meaningful next to a clean verify; these tests exercise emptiness, mutation on
# append, and agreement with a raw chain-table query.
# ---------------------------------------------------------------------------

from presidio_arch_translucency.observe import (  # noqa: E402
    chain_head_hash,
    energy_chain_head_hash,
)


def test_energy_chain_head_none_when_empty(db):
    assert energy_chain_head_hash(db_path=db) is None
    assert not db.exists()


def test_serving_chain_head_none_when_empty(db):
    assert chain_head_hash(db_path=db) is None
    assert not db.exists()


def test_energy_chain_head_changes_after_append(db):
    _record_energy_observation_unchecked(_eobs(_T0), db_path=db)
    first = energy_chain_head_hash(db_path=db)
    assert first is not None
    _record_energy_observation_unchecked(_eobs(_T0 + timedelta(minutes=1)), db_path=db)
    second = energy_chain_head_hash(db_path=db)
    assert second is not None and second != first


def test_energy_chain_head_matches_raw_table(db):
    _seed(db, 3)
    head = energy_chain_head_hash(db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT record_hash FROM energy_observation_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert head == row[0]


def test_serving_chain_head_matches_raw_table(db):
    record_observation(
        Observation(_T0, 500.0, 80.0, 140.0, 480.0, "container", 6, "manual"),
        db_path=db,
    )
    head = chain_head_hash(db_path=db)
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT record_hash FROM observation_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert head == row[0]


def test_energy_and_serving_heads_are_independent(db):
    # Recording an energy row must not create or change a serving head.
    _record_energy_observation_unchecked(_eobs(_T0), db_path=db)
    assert chain_head_hash(db_path=db) is None
    assert energy_chain_head_hash(db_path=db) is not None


# ---------------------------------------------------------------------------
# verified_energy_snapshot (v0.24.0, chain-gate TOCTOU fix) — one RO snapshot
# yields (report, rows, head). The head is the LAST VERIFIED link, bounded to
# the snapshot; a row appended afterward is in neither the rows nor the head.
# ---------------------------------------------------------------------------

from presidio_arch_translucency.observe import (  # noqa: E402
    verified_energy_snapshot,
)


def test_snapshot_clean_returns_report_rows_and_tip_head(db):
    _seed(db, 4)
    report, rows, head = verified_energy_snapshot(db_path=db)
    assert report.ok is True
    assert report.total == 4
    assert len(rows) == 4
    # On a clean walk the last verified link is the chain tip.
    assert head == energy_chain_head_hash(db_path=db)


def test_snapshot_missing_store_is_empty_without_creation(db):
    report, rows, head = verified_energy_snapshot(db_path=db)
    assert report.total == 0
    assert rows == []
    assert head is None
    assert not db.exists()


def test_snapshot_serving_only_store_has_no_energy_head(db):
    # A serving write creates the energy tables (empty); the snapshot must read
    # them as an empty energy store, not error.
    record_observation(
        Observation(_T0, 500.0, 80.0, 140.0, 480.0, "container", 6, "manual"),
        db_path=db,
    )
    report, rows, head = verified_energy_snapshot(db_path=db)
    assert report.total == 0
    assert rows == []
    assert head is None


def test_snapshot_head_is_last_verified_link_not_tip_on_break(db):
    _seed(db, 3)
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE energy_observations SET watts = 1 WHERE id = 2")
    conn.commit()
    conn.close()
    report, _rows, head = verified_energy_snapshot(db_path=db)
    assert report.ok is False
    assert report.broken_obs_id == 2
    # Only the first link verified; the head must be its record_hash, never the
    # tip past the break.
    assert report.verified == 1
    conn = sqlite3.connect(str(db))
    try:
        first = conn.execute(
            "SELECT record_hash FROM energy_observation_chain WHERE seq = 0"
        ).fetchone()[0]
        tip = conn.execute(
            "SELECT record_hash FROM energy_observation_chain ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert head == first
    assert head != tip


def test_snapshot_head_excludes_row_appended_after_snapshot(db):
    _seed(db, 2)
    _report1, rows1, head1 = verified_energy_snapshot(db_path=db)
    # A SECOND connection appends a row after the snapshot was taken.
    _record_energy_observation_unchecked(_eobs(_T0 + timedelta(minutes=99)), db_path=db)
    # The already-derived snapshot is unchanged — head/rows never see the append.
    assert len(rows1) == 2
    _report2, rows2, head2 = verified_energy_snapshot(db_path=db)
    assert len(rows2) == 3
    assert head1 != head2  # the fresh snapshot's head advances; the old one did not
    # head1 is the pre-append tip (seq 1), not the post-append tip (seq 2).
    conn = sqlite3.connect(str(db))
    try:
        pre_tip = conn.execute(
            "SELECT record_hash FROM energy_observation_chain WHERE seq = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert head1 == pre_tip


def test_snapshot_explicitly_begins_read_transaction(db, monkeypatch):
    """One connection is not enough: BEGIN must precede every snapshot SELECT."""
    _seed(db, 2)
    original_connect = sqlite3.connect
    statements = []

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            statements.append(str(sql))
            return super().execute(sql, parameters)

    def tracking_connect(*args, **kwargs):
        kwargs["factory"] = TrackingConnection
        return original_connect(*args, **kwargs)

    # ``sqlite3`` is the same module object used by ``observe``; patching its
    # connection factory exercises the production call without a mixed import.
    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    report, rows, head = verified_energy_snapshot(db_path=db)
    assert report.ok and len(rows) == 2 and head is not None
    assert statements[0] == "BEGIN"
