"""Tests for grid carbon intensity (`carbon.py`, v0.22.0)."""

from __future__ import annotations

import json
import time

import pytest

from presidio_arch_translucency import carbon
from presidio_arch_translucency.carbon import (
    _SANE_MAX_INTENSITY,
    CARBON_INTENSITY_DEFAULTS,
    REGION_TO_ZONE,
    SNAPSHOT_YEAR,
    CarbonError,
    _cache_get,
    _cache_set,
    _load_cache,
    _save_cache,
    _validate_intensity,
    country_fallback_intensity,
    grams_per_hour,
    grams_per_request,
    known_regions,
    resolve_carbon_intensity,
    static_annotation,
)

_SECRET = "super-secret-carbon-token-abc123"  # noqa: S105 — test fixture, not a real secret


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Point the carbon cache at a temp dir and clear the token by default."""
    monkeypatch.setattr(carbon, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(carbon, "CACHE_FILE", tmp_path / "carbon-cache.json")
    monkeypatch.delenv("PAT_CARBON_TOKEN", raising=False)


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode()

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False


def _fake_urlopen(payload: dict, captured: dict):
    def _open(req, timeout=None):  # noqa: ANN001, ARG001
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        return _FakeResp(payload)

    return _open


# ── pure conversions ──────────────────────────────────────────────────────────


def test_grams_per_request_exact() -> None:
    # 3.6e6 J = 1 kWh; at 500 gCO₂/kWh, 3.6e6 J emits exactly 500 g.
    assert grams_per_request(3.6e6, 500.0) == pytest.approx(500.0)
    assert grams_per_request(0.18, 300.0) == pytest.approx(0.18 / 3.6e6 * 300.0)


def test_grams_per_hour_exact() -> None:
    # 1000 W for 1 h = 1 kWh → equals the intensity in grams.
    assert grams_per_hour(1000.0, 420.0) == pytest.approx(420.0)
    assert grams_per_hour(76.0, 330.0) == pytest.approx(76.0 / 1000.0 * 330.0)


def test_country_fallback() -> None:
    assert country_fallback_intensity("de") == pytest.approx(330.0)
    assert country_fallback_intensity("FI") == pytest.approx(45.0)
    assert country_fallback_intensity("ZZ") is None


# ── static resolution + annotation ────────────────────────────────────────────


def test_static_resolution_no_token() -> None:
    value, source = resolve_carbon_intensity("eu-central-1")
    assert source == "static"
    assert value == CARBON_INTENSITY_DEFAULTS["eu-central-1"]


def test_static_annotation_variants() -> None:
    assert static_annotation("static") == f"(static {SNAPSHOT_YEAR} average)"
    assert "live" in static_annotation("live").lower()
    assert "cache" in static_annotation("cache").lower()


def test_known_regions_sorted_and_complete() -> None:
    regions = known_regions()
    assert regions == tuple(sorted(CARBON_INTENSITY_DEFAULTS))
    # Every AWS/GCP/Azure region the briefing names is covered.
    for r in ("us-east-1", "europe-north1", "eastus", "germanywestcentral"):
        assert r in regions


def test_every_static_region_has_a_zone() -> None:
    for region in CARBON_INTENSITY_DEFAULTS:
        assert region in REGION_TO_ZONE


def test_intensity_magnitudes_are_coherent() -> None:
    d = CARBON_INTENSITY_DEFAULTS
    assert d["europe-north1"] < 60  # Nordic, very clean
    assert 300 <= d["eu-central-1"] <= 350  # Germany
    assert 350 <= d["ap-southeast-1"] <= 400  # Singapore
    assert 300 <= d["us-east-1"] <= 350  # Virginia


# ── unknown region fails closed ───────────────────────────────────────────────


def test_unknown_region_raises_listing_regions() -> None:
    with pytest.raises(CarbonError) as exc:
        resolve_carbon_intensity("moon-base-1")
    msg = str(exc.value)
    assert "us-east-1" in msg and "europe-north1" in msg


# ── live path + cache ─────────────────────────────────────────────────────────


def test_live_fetch_writes_cache_and_is_owner_only(monkeypatch) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)
    captured: dict = {}
    monkeypatch.setattr(
        carbon,
        "_open_live",
        _fake_urlopen({"carbonIntensity": 123.0}, captured),
    )
    value, source = resolve_carbon_intensity("eu-central-1")
    assert source == "live"
    assert value == pytest.approx(123.0)
    # Zone resolved and token sent in the auth-token header (not the URL).
    assert REGION_TO_ZONE["eu-central-1"] in captured["url"]
    assert captured["headers"].get("Auth-token") == _SECRET
    assert _SECRET not in captured["url"]
    # Cache written owner-only.
    assert carbon.CACHE_FILE.exists()
    mode = carbon.CACHE_FILE.stat().st_mode & 0o777
    assert mode == 0o600


def test_cache_hit_within_ttl_skips_network(monkeypatch) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)
    _cache_set("eu-central-1", 99.0)

    def _boom(*_a, **_k):
        raise AssertionError("network must not be hit on a fresh cache")

    monkeypatch.setattr(carbon, "_open_live", _boom)
    value, source = resolve_carbon_intensity("eu-central-1")
    assert source == "cache"
    assert value == pytest.approx(99.0)


def test_expired_cache_and_live_failure_falls_back_to_static(
    monkeypatch, caplog
) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)
    # Seed an expired entry.
    _save_cache({"eu-central-1": {"intensity": 99.0, "ts": time.time() - 10_000}})

    def _fail(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(carbon, "_open_live", _fail)
    with caplog.at_level("WARNING"):
        value, source = resolve_carbon_intensity("eu-central-1")
    assert source == "static"
    assert value == CARBON_INTENSITY_DEFAULTS["eu-central-1"]
    assert any("carbon" in r.message.lower() for r in caplog.records)


def test_live_failure_no_cache_falls_back_to_static(monkeypatch, caplog) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)

    def _fail(*_a, **_k):
        raise ValueError("bad json")

    monkeypatch.setattr(carbon, "_open_live", _fail)
    with caplog.at_level("WARNING"):
        value, source = resolve_carbon_intensity("us-west-2")
    assert source == "static"
    assert value == CARBON_INTENSITY_DEFAULTS["us-west-2"]


def test_negative_live_intensity_falls_back(monkeypatch) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)
    monkeypatch.setattr(
        carbon,
        "_open_live",
        _fake_urlopen({"carbonIntensity": -5.0}, {}),
    )
    value, source = resolve_carbon_intensity("eu-central-1")
    assert source == "static"  # negative reading rejected → static


# ── intensity bounds validation (the single choke point) ──────────────────────


@pytest.mark.parametrize("bad", [-5.0, 0.0, 1e12, float("inf"), float("nan")])
def test_validate_intensity_rejects_out_of_range(bad: float) -> None:
    with pytest.raises(CarbonError):
        _validate_intensity(bad)


@pytest.mark.parametrize("bad", [None, [1, 2, 3], {"a": 1}])
def test_validate_intensity_typeerror_on_wrong_type(bad: object) -> None:
    # Wrong-typed input (null / list / dict) raises TypeError so the live path
    # treats it as a fetch failure and falls back — not a CarbonError.
    with pytest.raises(TypeError):
        _validate_intensity(bad)


def test_validate_intensity_accepts_sane_values() -> None:
    assert _validate_intensity(330.0) == pytest.approx(330.0)
    assert _validate_intensity(1) == pytest.approx(1.0)
    assert _validate_intensity(_SANE_MAX_INTENSITY) == pytest.approx(
        _SANE_MAX_INTENSITY
    )
    # The documented ceiling comfortably clears any real grid (~1600 max).
    assert _SANE_MAX_INTENSITY == pytest.approx(2000.0)


@pytest.mark.parametrize("poison", [-5.0, 0.0, 1e12, float("inf"), float("nan")])
def test_poisoned_cache_entry_is_dropped(poison: float) -> None:
    # A fresh-but-poisoned cache entry is refused (treated as a miss).
    _save_cache({"eu-central-1": {"intensity": poison, "ts": time.time()}})
    assert _cache_get("eu-central-1") is None


@pytest.mark.parametrize("poison", [-5.0, 0.0, 1e12, float("inf"), float("nan")])
def test_poisoned_cache_falls_through_to_static(monkeypatch, poison: float) -> None:
    # Token set (so the cache is consulted); poisoned entry → miss → live fetch
    # (forced offline) → documented static snapshot. The bad value is never used.
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)
    _save_cache({"eu-central-1": {"intensity": poison, "ts": time.time()}})

    def _fail(*_a, **_k):
        raise OSError("network down")

    monkeypatch.setattr(carbon, "_open_live", _fail)
    value, source = resolve_carbon_intensity("eu-central-1")
    assert source == "static"
    assert value == CARBON_INTENSITY_DEFAULTS["eu-central-1"]


@pytest.mark.parametrize(
    "payload",
    [
        {"carbonIntensity": 0.0},
        {"carbonIntensity": float("nan")},
        {"carbonIntensity": float("inf")},
        {"carbonIntensity": 1e12},
        {"carbonIntensity": None},
        {"carbonIntensity": [1, 2, 3]},
    ],
)
def test_invalid_live_response_not_cached_and_falls_back(
    monkeypatch, caplog, payload: dict
) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)
    monkeypatch.setattr(carbon, "_open_live", _fake_urlopen(payload, {}))
    with caplog.at_level("WARNING"):
        value, source = resolve_carbon_intensity("eu-central-1")
    # Out-of-range/non-finite (CarbonError→ValueError) and wrong-typed
    # (null/list → TypeError) responses alike fall back, never fail the command.
    assert source == "static"
    assert value == CARBON_INTENSITY_DEFAULTS["eu-central-1"]
    assert any("carbon" in r.message.lower() for r in caplog.records)
    # Nothing was written to the cache.
    assert not carbon.CACHE_FILE.exists()


def test_valid_live_value_still_cached_and_returned(monkeypatch) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)
    monkeypatch.setattr(
        carbon,
        "_open_live",
        _fake_urlopen({"carbonIntensity": 412.0}, {}),
    )
    value, source = resolve_carbon_intensity("us-east-1")
    assert source == "live"
    assert value == pytest.approx(412.0)
    # A valid reading is cached and served on the next call without network.
    assert _cache_get("us-east-1") == pytest.approx(412.0)


# ── token hygiene ─────────────────────────────────────────────────────────────


def test_token_with_control_chars_rejected(monkeypatch) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", "bad\ntoken")
    with pytest.raises(CarbonError):
        resolve_carbon_intensity("eu-central-1")


def test_empty_token_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", "   ")
    value, source = resolve_carbon_intensity("eu-central-1")
    assert source == "static"


def test_token_never_appears_in_logs(monkeypatch, caplog) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)

    def _fail(*_a, **_k):
        raise OSError("down")

    monkeypatch.setattr(carbon, "_open_live", _fail)
    with caplog.at_level("DEBUG"):
        resolve_carbon_intensity("eu-central-1")
    assert _SECRET not in caplog.text


# ── cache helpers ─────────────────────────────────────────────────────────────


def test_cache_roundtrip_and_ttl() -> None:
    _cache_set("us-east-1", 288.0)
    assert _cache_get("us-east-1") == pytest.approx(288.0)
    # Expired outside TTL.
    _save_cache({"us-east-1": {"intensity": 288.0, "ts": time.time() - 99_999}})
    assert _cache_get("us-east-1") is None


def test_load_cache_handles_corruption() -> None:
    carbon.CACHE_FILE.write_text("{ not json")
    assert _load_cache() == {}


def test_load_cache_rejects_non_mapping_root() -> None:
    carbon.CACHE_FILE.write_text("[]")
    carbon.CACHE_FILE.chmod(0o600)
    assert _load_cache() == {}


def test_cache_rejects_future_timestamp() -> None:
    _save_cache({"us-east-1": {"intensity": 288.0, "ts": time.time() + 3600}})
    assert _cache_get("us-east-1") is None


def test_load_cache_rejects_group_readable_file() -> None:
    carbon.CACHE_FILE.write_text("{}")
    carbon.CACHE_FILE.chmod(0o640)
    assert _load_cache() == {}


def test_load_cache_rejects_symlink(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}")
    target.chmod(0o600)
    carbon.CACHE_FILE.symlink_to(target)
    assert _load_cache() == {}


def test_live_response_size_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("PAT_CARBON_TOKEN", _SECRET)

    class _Oversized(_FakeResp):
        def __init__(self) -> None:
            self._payload = b"x" * (carbon.MAX_RESPONSE_BYTES + 1)

    monkeypatch.setattr(carbon, "_open_live", lambda *_a, **_k: _Oversized())
    value, source = resolve_carbon_intensity("eu-central-1")
    assert source == "static"
    assert value == CARBON_INTENSITY_DEFAULTS["eu-central-1"]


def test_redirect_handler_refuses_redirects() -> None:
    handler = carbon._NoRedirectHandler()
    assert (
        handler.redirect_request(None, None, 302, "Found", {}, "https://evil") is None
    )
