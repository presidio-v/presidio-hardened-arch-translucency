"""
Grid carbon intensity for cloud regions (v0.22.0) — "Budget the watt".

Maps the cloud regions the pricing modules already understand
(``cloud.py`` / ``cloud_gcp.py`` / ``cloud_azure.py``) to a **grid carbon
intensity** in gCO₂eq/kWh, so ``pat budget`` / ``pat cost --carbon`` can turn
the modelled watts of the energy model into modelled grams of CO₂.

Invariants (PRESIDIO-REQ Energy Arc, A1 / E1 / E1a):
    Everything here is **emit-only** and every figure is a *modelled estimate*.
    A carbon number produced by this module is the product of a modelled watt
    (``energy.py``) and a documented/cited grid-intensity figure. It never
    enters the hash-chained ``energy_observations`` store, never becomes an
    ``energy-reading@1``, and is never signed — E1a: *pat never signs a watt
    (or a gram) it did not measure*.

Data sources (cited, MVP-placeholder snapshot — the v0.18/v0.20 defaults
precedent):
    - Google Cloud's Apache-2.0 ``region-carbon-info`` 2023 CSV supplies exact
      grid intensities for GCP regions and same-grid locality proxies.
    - Ember's CC-BY-4.0 yearly electricity data supplies country-average
      context where no same-grid Google locality exists.
    The embedded table :data:`CARBON_INTENSITY_DEFAULTS` is a *documented
    placeholder snapshot* for the :data:`SNAPSHOT_YEAR` annual average, not a
    live or real-time reading. Calibrate against a live source (below) or treat
    the figures as order-of-magnitude, coherent with the cited datasets.

Methodology — **location-based annual average** (not market-based, not
marginal):
    Location-based average is the correct basis for *cross-region placement
    ranking*: it reflects the physical grid a workload would actually draw on.
    Market-based accounting (RECs / PPAs) obscures grid physics behind
    contractual attribution, and marginal intensity answers a different
    question (intra-day load shifting on one grid), not "which region is
    cleaner to run in".

Live resolution (optional, opt-in via an env token):
    ``resolve_carbon_intensity`` prefers a live Electricity Maps reading when
    ``PAT_CARBON_TOKEN`` is set, cached owner-only in ``~/.pat/carbon-cache.json``
    (TTL 3600 s), and falls back — never failing the command — to the static
    table with a documented annotation. The token is env-only, never a CLI
    argument, never logged.
"""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Final, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Static table — location-based annual-average grid intensity (gCO₂eq/kWh)
# ---------------------------------------------------------------------------

#: Snapshot year for the embedded static intensities (annual average).
SNAPSHOT_YEAR: Final[int] = 2023

#: MVP-placeholder per-region **location-based annual-average** grid carbon
#: intensity in gCO₂eq/kWh. GCP and same-grid locality values are exact entries
#: from Google's 2023 ``region-carbon-info`` CSV; Ireland uses rounded Ember
#: country context because that Google dataset has no Irish region. These are
#: placement proxies, not provider-specific accounting factors or live readings.
#: Source: https://github.com/GoogleCloudPlatform/region-carbon-info/blob/main/data/yearly/2023.csv
#: Context: https://ember-energy.org/data/yearly-electricity-data/
CARBON_INTENSITY_DEFAULTS: Final[dict[str, float]] = {
    # --- AWS ---
    "us-east-1": 322.0,  # N. Virginia — Google us-east4 same-grid proxy
    "us-west-2": 94.0,  # Oregon — Google us-west1 same-grid proxy
    "eu-west-1": 300.0,  # Ireland
    "eu-central-1": 345.0,  # Frankfurt — Google europe-west3 proxy
    "ap-southeast-1": 369.0,  # Singapore — Google asia-southeast1 proxy
    "ap-northeast-1": 459.0,  # Tokyo — Google asia-northeast1 proxy
    # --- GCP ---
    "us-central1": 430.0,  # Iowa — MISO
    "europe-west1": 122.0,  # Belgium
    "europe-west3": 345.0,  # Frankfurt, Germany
    "europe-north1": 46.0,  # Finland
    # --- Azure ---
    "eastus": 322.0,  # Virginia — Google us-east4 same-grid proxy
    "westeurope": 236.0,  # Netherlands — Google europe-west4 proxy
    "northeurope": 300.0,  # Ireland
    "germanywestcentral": 345.0,  # Frankfurt — Google europe-west3 proxy
}

#: Country-level fallback intensities (gCO₂eq/kWh, Ember 2023 country averages).
#: Used only by :func:`country_fallback_intensity`; the primary path is the
#: per-region table above.
COUNTRY_INTENSITY_FALLBACK: Final[dict[str, float]] = {
    "US": 370.0,
    "IE": 300.0,
    "DE": 330.0,
    "SG": 470.0,
    "JP": 470.0,
    "FI": 45.0,
    "BE": 140.0,
    "NL": 330.0,
    "FR": 55.0,
    "GB": 240.0,
    "NO": 30.0,
    "SE": 40.0,
}

#: Best-effort region → Electricity Maps zone key, for the live path. Zones are
#: the coarse grid zones Electricity Maps publishes; a cloud region maps to the
#: zone whose grid it draws on.
REGION_TO_ZONE: Final[dict[str, str]] = {
    "us-east-1": "US-MIDA-PJM",
    "us-west-2": "US-NW-PACW",
    "eu-west-1": "IE",
    "eu-central-1": "DE",
    "ap-southeast-1": "SG",
    "ap-northeast-1": "JP-TK",
    "us-central1": "US-MIDW-MISO",
    "europe-west1": "BE",
    "europe-west3": "DE",
    "europe-north1": "FI",
    "eastus": "US-MIDA-PJM",
    "westeurope": "NL",
    "northeurope": "IE",
    "germanywestcentral": "DE",
}

# ---------------------------------------------------------------------------
# Cache (mirrors cloud.py discipline: owner-only, TTL, offline-tolerant)
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".pat"
CACHE_FILE = CACHE_DIR / "carbon-cache.json"
CACHE_TTL_SECONDS: Final[int] = 3600  # 1 hour — live intensity is time-varying
MAX_CACHE_BYTES: Final[int] = 1_048_576
MAX_RESPONSE_BYTES: Final[int] = 65_536

TOKEN_ENV: Final[str] = "PAT_CARBON_TOKEN"  # noqa: S105 — env var name, not a secret
_LIVE_URL: Final[str] = "https://api.electricitymaps.com/v3/carbon-intensity/latest"


class CarbonError(ValueError):
    """Raised when a region is unknown (fail-closed, no silent global average)."""


# ---------------------------------------------------------------------------
# Untrusted-intensity validation (single choke point)
# ---------------------------------------------------------------------------

#: Upper sanity ceiling for a grid-intensity reading (gCO₂eq/kWh). No real-world
#: electricity grid exceeds ~1600 gCO₂eq/kWh — a pure-coal grid sits near
#: ~1000, and the dirtiest oil-peaker island grids top out around ~1300–1600 —
#: so 2000 leaves generous headroom while still refusing absurd or poisoned
#: values (negatives, zero, NaN, ±∞, or e.g. 1e12).
_SANE_MAX_INTENSITY: Final[float] = 2000.0


def _validate_intensity(value: object) -> float:
    """Return *value* as a float iff it is a sane grid intensity, else raise.

    A trustworthy reading is finite and ``0 < v <= _SANE_MAX_INTENSITY``. This
    is the single choke point for every *untrusted* intensity — a live API
    response and a cache entry both flow through here — so a malformed,
    out-of-range, or poisoned value (negative, zero, NaN, ±∞, absurdly large)
    is refused before it can become a carbon figure or a division denominator.

    Raises :class:`CarbonError` for an out-of-range/non-finite number, and
    lets ``TypeError`` (e.g. ``None`` / list-shaped input) propagate so a
    wrong-typed live response is treated as a fetch failure by the caller.
    """
    v = float(value)  # TypeError for None/list; ValueError for a non-numeric str
    if not (math.isfinite(v) and 0 < v <= _SANE_MAX_INTENSITY):
        raise CarbonError(
            f"grid intensity {v!r} out of sane range "
            f"(0, {_SANE_MAX_INTENSITY:g}] gCO₂eq/kWh"
        )
    return v


# ---------------------------------------------------------------------------
# Pure conversions (unit-tested)
# ---------------------------------------------------------------------------


def grams_per_request(j_per_req: float, intensity_g_per_kwh: float) -> float:
    """gCO₂eq emitted serving one request at *j_per_req* joules on this grid.

    ``grams = joules / 3.6e6 (J per kWh) × intensity``. A pure conversion of a
    modelled joules-per-request figure — never a measurement.
    """
    return j_per_req / 3.6e6 * intensity_g_per_kwh


def grams_per_hour(watts: float, intensity_g_per_kwh: float) -> float:
    """gCO₂eq per hour for a *watts* draw on this grid.

    ``grams/h = watts / 1000 (W per kW) × intensity``.
    """
    return watts / 1000.0 * intensity_g_per_kwh


def country_fallback_intensity(country_code: str) -> Optional[float]:  # noqa: UP045
    """Country-average intensity for *country_code* (ISO-2), or ``None``."""
    return COUNTRY_INTENSITY_FALLBACK.get(country_code.upper())


def known_regions() -> tuple[str, ...]:
    """Regions with a static intensity, sorted for stable error messages."""
    return tuple(sorted(CARBON_INTENSITY_DEFAULTS))


def static_annotation(source: str) -> str:
    """Rendered provenance suffix for a resolved intensity + *source*."""
    if source == "live":
        return "(live · Electricity Maps)"
    if source == "cache":
        return "(cached live · Electricity Maps)"
    return f"(static {SNAPSHOT_YEAR} average)"


# ---------------------------------------------------------------------------
# Token / control-char handling (env-only, never a CLI arg, never logged)
# ---------------------------------------------------------------------------


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _reject_control_chars(value: str, field: str) -> None:
    if _has_control_chars(value):
        raise CarbonError(f"carbon {field} must not contain control characters")


def _token_from_env() -> Optional[str]:  # noqa: UP045
    """Bearer token from ``PAT_CARBON_TOKEN`` only (never a CLI arg, never logged)."""
    token = os.environ.get(TOKEN_ENV)
    if not token or not token.strip():
        return None
    _reject_control_chars(token, "token")
    return token.strip()


# ---------------------------------------------------------------------------
# Cache helpers (copy of cloud.py's owner-only, TTL, best-effort chmod)
# ---------------------------------------------------------------------------


def _load_cache() -> dict:
    try:
        info = CACHE_FILE.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
            return {}
        if info.st_mode & 0o077:
            return {}
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return {}
        value = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeError):
        return {}


def _save_cache(cache: dict) -> None:
    # Owner-only: the cache drives carbon output, so another local user must not
    # be able to read or poison it (cloud.py precedent).
    if CACHE_DIR.is_symlink():
        raise OSError("refusing to write carbon cache through a symlinked directory")
    CACHE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    CACHE_DIR.chmod(0o700)
    payload = json.dumps(cache, indent=2)
    if len(payload.encode("utf-8")) > MAX_CACHE_BYTES:
        raise OSError("carbon cache exceeds the size limit")

    fd, tmp_name = tempfile.mkstemp(prefix=".carbon-cache-", dir=CACHE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, CACHE_FILE)
    finally:
        try:
            Path(tmp_name).unlink()
        except FileNotFoundError:
            pass


def _cache_get(region: str, ttl: int = CACHE_TTL_SECONDS) -> Optional[float]:  # noqa: UP045
    """Return a cached intensity for *region* if present and within *ttl*."""
    entry = _load_cache().get(region)
    if not isinstance(entry, dict):
        return None
    try:
        timestamp = float(entry.get("ts", 0))
        age = time.time() - timestamp
        if math.isfinite(timestamp) and 0.0 <= age < ttl:
            # Bounds-validate before trusting a cache entry: a poisoned or
            # malformed value (negative/zero/NaN/∞/absurd) is dropped and
            # treated as a miss, never returned. CarbonError is a ValueError.
            return _validate_intensity(entry["intensity"])
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _cache_set(region: str, intensity: float) -> None:
    cache = _load_cache()
    cache[region] = {"intensity": _validate_intensity(intensity), "ts": time.time()}
    _save_cache(cache)


# ---------------------------------------------------------------------------
# Live fetch (Electricity Maps latest-intensity, HTTPS + token header)
# ---------------------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the non-standard auth header cannot leave its host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _open_live(req: urllib.request.Request, timeout: float):
    opener = urllib.request.build_opener(_NoRedirectHandler())
    return opener.open(req, timeout=timeout)


def _fetch_live_intensity(region: str, token: str) -> float:
    """Fetch the latest grid intensity for *region*'s zone from Electricity Maps.

    HTTPS only; the token travels in the ``auth-token`` header, never in the URL
    and never logged. Raises on any transport / parse error so the caller can
    fall back to the static table.
    """
    zone = REGION_TO_ZONE.get(region)
    if zone is None:
        raise CarbonError(f"no Electricity Maps zone mapping for region {region!r}")
    url = f"{_LIVE_URL}?{urllib.parse.urlencode({'zone': zone})}"
    req = urllib.request.Request(  # noqa: S310 — literal https scheme
        url,
        headers={"auth-token": token, "User-Agent": "pat-cli/0.22.0"},
    )
    with _open_live(req, timeout=30) as resp:
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CarbonError("carbon-intensity response exceeds the size limit")
    data = json.loads(raw)
    # Bounds-validate BEFORE the caller caches it: an out-of-range/non-finite
    # reading raises CarbonError, and a wrong-typed one (``null`` / list) raises
    # TypeError — either way the caller treats it as a fetch failure, warns, and
    # falls back to the static snapshot; the bad value is never cached.
    return _validate_intensity(data["carbonIntensity"])


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_carbon_intensity(region: str) -> tuple[float, str]:
    """Resolve ``(gCO₂eq/kWh, source)`` for *region*.

    ``source`` is one of ``"live"`` (fresh Electricity Maps reading),
    ``"cache"`` (a still-valid cached live reading), or ``"static"`` (the
    embedded :data:`CARBON_INTENSITY_DEFAULTS` snapshot). Resolution:

      1. When ``PAT_CARBON_TOKEN`` is set: a still-valid cache entry wins; else a
         live fetch is attempted (cached owner-only on success); a live failure
         **never fails the command** — it logs a warning and falls back to the
         static table.
      2. With no token: the static table.

    An unknown region fails closed with :class:`CarbonError` listing the known
    regions — never a silent global average.
    """
    if region not in CARBON_INTENSITY_DEFAULTS:
        raise CarbonError(
            f"unknown region {region!r} for carbon intensity. Known regions: "
            + ", ".join(known_regions())
        )

    token = _token_from_env()
    if token is not None:
        cached = _cache_get(region)
        if cached is not None:
            return cached, "cache"
        try:
            value = _fetch_live_intensity(region, token)
            _cache_set(region, value)
            return value, "live"
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            # Offline-tolerant: warn (no token in the message) and fall back to
            # the documented static snapshot rather than failing the command.
            # TypeError covers a wrong-typed response (``carbonIntensity: null``
            # or list-shaped), honouring the "never fails the command" invariant.
            log.warning(
                "Live carbon-intensity fetch failed for region %s; "
                "falling back to the static %d average (%s).",
                region,
                SNAPSHOT_YEAR,
                type(exc).__name__,
            )

    return CARBON_INTENSITY_DEFAULTS[region], "static"
