"""
Dated GPU price catalogs for inference layouts (Inference Arc Phase 1f).

A catalog is an operator-supplied JSON file of GPU instance prices, one
retrieval campaign, committed by content digest so an audit can cite exactly
which prices it used. pat ships the schema, the loader and the pricing rule —
**no price data**: list prices move monthly, and a stale price in a published
audit is a liability (keeping catalogs fresh is outside this package).

Schema ``presidio-hardened/gpu-price-catalog@1`` (strict; unknown keys are
refused)::

  {"schema": …, "as_of": "2026-10-01", "currency": "EUR", "vat": "excluded",
   "entries": [{
     "provider": "scaleway", "region": {"country": "FR", "datacenter": "fr-par-2"},
     "gpu": {"model": "L40S", "memory_gb": 48, "bandwidth_gbs": 864},
     "sku": "L40S-2-48G", "gpus_per_instance": 2,
     "billing": {"basis": "instance", "unit": "hour", "price": "2.80"},
     "tier": "on-demand",
     "source_url": "https://…", "retrieved_at": "2026-10-01T09:12:00Z",
     "valid_from": "2026-09-01"}],
   "catalog_commitment": {"schema": …, "digest": "<sha256>"}}

Rules:

* **Money is decimal.** Prices are strings parsed with :class:`~decimal.Decimal`
  and bounded to 0.05–500 EUR per GPU-hour; floats never enter the digest.
  Currency is EUR and VAT excluded, fixed by the schema.
* **Committed.** ``pat infer-prices commit`` writes the SHA-256 of the
  canonical content; loading recomputes it and fails closed on any change.
  The digest proves "unchanged since committed", not "true at source".
* **Dated.** At use, a catalog older than :data:`FRESH_DAYS` warns and is marked
  stale; older than :data:`REFUSE_DAYS` is refused, with no override. An
  ``as_of`` in the future, or retrievals after ``as_of`` or spread over more
  than :data:`MAX_RETRIEVAL_SPREAD_DAYS` before it, are refused: a catalog is
  one retrieval campaign, not a mix.
* **One family.** A selection (provider, region, GPU, tier) must resolve to
  exactly one family; ambiguity is refused with the candidates, never
  resolved by picking the cheapest.
* **Instance packing.** A layout ``(tp, n)`` is priced on the family's
  cheapest SKU that holds a TP group (``gpus_per_instance ≥ tp``; a group
  never spans instances): ``⌊g/tp⌋`` engines per instance, ``⌈n/⌊g/tp⌋⌉``
  instances, at least the committed tier's minimum. Replicas may share an
  instance; SKUs are not mixed. A layout no SKU holds has no price. This is
  what makes tp2×1 and tp1×2 cost different amounts on the same two GPUs.

Bounded claims: hourly prices only (egress, storage and network are out of
scope); committed and spot tiers carry term and interruption semantics the
hourly model ignores, so the tier is always printed; packing assumes a
provider lets replicas share an instance, true for VMs but not for every
bare-metal offer.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from presidio_arch_translucency.calibrate import _canonical_bytes
from presidio_arch_translucency.infer_calibrate import (
    HARDWARE_MATCH_TOLERANCE,
    InferenceCalibrationError,
    _canonicalize,
    _label,
    read_regular_file,
)

CATALOG_SCHEMA: Final[str] = "presidio-hardened/gpu-price-catalog@1"
COMMITMENT_SCHEMA: Final[str] = "presidio-hardened/gpu-price-catalog-commitment@1"
COMMITMENT_KEY: Final[str] = "catalog_commitment"
MAX_CATALOG_BYTES: Final[int] = 1_048_576
MAX_ENTRIES: Final[int] = 500
FRESH_DAYS: Final[int] = 30
REFUSE_DAYS: Final[int] = 90
MAX_RETRIEVAL_SPREAD_DAYS: Final[int] = 30
MIN_EUR_PER_GPU_HOUR: Final[Decimal] = Decimal("0.05")
MAX_EUR_PER_GPU_HOUR: Final[Decimal] = Decimal("500")
MAX_GPUS_PER_INSTANCE: Final[int] = 16
MAX_URL_LEN: Final[int] = 512
TIERS: Final[tuple[str, ...]] = ("on-demand", "committed", "spot")

_SLUG: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_COUNTRY: Final[re.Pattern[str]] = re.compile(r"[A-Z]{2}")
_PRICE: Final[re.Pattern[str]] = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6})?")
_DIGEST: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")

_CATALOG_KEYS: Final[frozenset[str]] = frozenset(
    {"schema", "as_of", "currency", "vat", "entries", COMMITMENT_KEY}
)
_ENTRY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "provider",
        "region",
        "gpu",
        "sku",
        "gpus_per_instance",
        "billing",
        "tier",
        "commitment",
        "source_url",
        "retrieved_at",
        "valid_from",
        "notes",
    }
)
_REQUIRED_ENTRY_KEYS: Final[frozenset[str]] = _ENTRY_KEYS - {"commitment", "notes"}


def money(value: Decimal) -> str:
    """Plain decimal text for a cited amount: ``10``, never ``1E+1``."""
    return format(value.normalize(), "f")


class PriceCatalogError(ValueError):
    """Raised on a malformed, stale or ambiguous price catalog."""


class PriceCatalogTamperError(PriceCatalogError):
    """Raised when a catalog does not match its commitment."""


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceEntry:
    provider: str
    country: str
    datacenter: str | None
    gpu_model: str
    memory_gb: float
    bandwidth_gbs: float | None
    sku: str
    gpus_per_instance: int
    basis: str  # "instance" or "gpu"
    unit: str  # "hour" or "minute"
    price: Decimal
    tier: str
    term_months: int | None
    min_instances: int
    source_url: str
    retrieved_at: datetime
    valid_from: date

    @property
    def eur_per_instance_hour(self) -> Decimal:
        per_unit = self.price * (60 if self.unit == "minute" else 1)
        return per_unit * (self.gpus_per_instance if self.basis == "gpu" else 1)

    @property
    def eur_per_gpu_hour(self) -> Decimal:
        return self.eur_per_instance_hour / self.gpus_per_instance

    @property
    def family(self) -> tuple:
        """What one selection must pin down: who sells which GPU where, how."""
        return (
            self.provider,
            self.country,
            self.datacenter,
            self.gpu_model,
            self.memory_gb,
            self.bandwidth_gbs,
            self.tier,
            self.term_months,
        )

    def cited(self) -> dict:
        return {
            "sku": self.sku,
            "gpus_per_instance": self.gpus_per_instance,
            "eur_per_gpu_hour": money(self.eur_per_gpu_hour),
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


@dataclass(frozen=True)
class PriceCatalog:
    name: str
    as_of: date
    entries: tuple[PriceEntry, ...]
    digest: str


def _fail(message: str, index: int | None = None) -> PriceCatalogError:
    where = f"entry {index}: " if index is not None else ""
    return PriceCatalogError(f"price catalog {where}{message}")


def _keys(data: object, allowed: frozenset[str], what: str, index: int) -> dict:
    if not isinstance(data, dict):
        raise _fail(f"{what} must be an object", index)
    unknown = set(data) - allowed
    if unknown:
        raise _fail(f"unknown {what} keys {sorted(unknown)}", index)
    return data


def _slug(value: object, name: str, index: int) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise _fail(f"{name} must be a lower-case slug", index)
    return value


def _text(value: object, name: str, index: int) -> str:
    try:
        label = _label(value, name)
    except InferenceCalibrationError as exc:
        raise _fail(str(exc), index) from exc
    if label is None:
        raise _fail(f"{name} is required", index)
    return label


def _number(value: object, name: str, low: float, high: float, index: int) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not (math.isfinite(value) and low <= value <= high)
    ):
        raise _fail(f"{name} must be a number in [{low:g}, {high:g}]", index)
    return float(value)


def _integer(value: object, name: str, low: int, high: int, index: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise _fail(f"{name} must be an integer in [{low}, {high}]", index)
    return value


def _date(value: object, name: str, index: int | None) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise _fail(f"{name} must be a YYYY-MM-DD date", index)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _fail(f"{name} is not a valid date", index) from exc


def _timestamp(value: object, name: str, index: int) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ):
        raise _fail(f"{name} must be a UTC timestamp YYYY-MM-DDTHH:MM:SSZ", index)
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise _fail(f"{name} is not a valid timestamp", index) from exc


def _url(value: object, index: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_URL_LEN
        or not value.isascii()
        or not value.isprintable()
        or any(ch.isspace() for ch in value)
        or not value.startswith("https://")
    ):
        raise _fail(
            f"source_url must be an https URL of at most {MAX_URL_LEN} printable "
            "ASCII characters",
            index,
        )
    host = value[len("https://") :].split("/", 1)[0]
    if not host or "@" in host:
        raise _fail("source_url must have a host and no credentials", index)
    return value


def _entry(data: object, index: int) -> PriceEntry:
    data = _keys(data, _ENTRY_KEYS, "entry", index)
    missing = _REQUIRED_ENTRY_KEYS - set(data)
    if missing:
        raise _fail(f"missing keys {sorted(missing)}", index)
    region = _keys(
        data["region"], frozenset({"country", "datacenter"}), "region", index
    )
    country = region.get("country")
    if not isinstance(country, str) or not _COUNTRY.fullmatch(country):
        raise _fail("region.country must be an ISO 3166-1 alpha-2 code", index)
    datacenter = region.get("datacenter")
    if datacenter is not None:
        datacenter = _text(datacenter, "region.datacenter", index)
    gpu = _keys(
        data["gpu"], frozenset({"model", "memory_gb", "bandwidth_gbs"}), "gpu", index
    )
    if "model" not in gpu or "memory_gb" not in gpu:
        raise _fail("gpu needs model and memory_gb", index)
    bandwidth = gpu.get("bandwidth_gbs")
    billing = _keys(
        data["billing"], frozenset({"basis", "unit", "price"}), "billing", index
    )
    if billing.get("basis") not in ("instance", "gpu"):
        raise _fail("billing.basis must be 'instance' or 'gpu'", index)
    if billing.get("unit") not in ("hour", "minute"):
        raise _fail("billing.unit must be 'hour' or 'minute'", index)
    raw_price = billing.get("price")
    if not isinstance(raw_price, str) or not _PRICE.fullmatch(raw_price):
        raise _fail('billing.price must be a decimal string such as "2.80"', index)
    try:
        price = Decimal(raw_price)
    except InvalidOperation as exc:  # pragma: no cover - the pattern prevents it
        raise _fail("billing.price is not a decimal", index) from exc
    tier = data["tier"]
    if tier not in TIERS:
        raise _fail(f"tier must be one of {TIERS}", index)
    commitment = data.get("commitment")
    if (commitment is not None) != (tier == "committed"):
        raise _fail(
            "commitment is required for, and only for, the committed tier", index
        )
    term_months, min_instances = None, 1
    if commitment is not None:
        commitment = _keys(
            commitment, frozenset({"term_months", "min_instances"}), "commitment", index
        )
        term_months = _integer(
            commitment.get("term_months"), "term_months", 1, 60, index
        )
        min_instances = _integer(
            commitment.get("min_instances", 1), "min_instances", 1, 1024, index
        )
    if "notes" in data:
        _text(data["notes"], "notes", index)
    entry = PriceEntry(
        provider=_slug(data["provider"], "provider", index),
        country=country,
        datacenter=datacenter,
        gpu_model=_text(gpu["model"], "gpu.model", index),
        memory_gb=_number(gpu["memory_gb"], "gpu.memory_gb", 1.0, 1e4, index),
        bandwidth_gbs=(
            None
            if bandwidth is None
            else _number(bandwidth, "gpu.bandwidth_gbs", 1.0, 1e6, index)
        ),
        sku=_text(data["sku"], "sku", index),
        gpus_per_instance=_integer(
            data["gpus_per_instance"],
            "gpus_per_instance",
            1,
            MAX_GPUS_PER_INSTANCE,
            index,
        ),
        basis=billing["basis"],
        unit=billing["unit"],
        price=price,
        tier=tier,
        term_months=term_months,
        min_instances=min_instances,
        source_url=_url(data["source_url"], index),
        retrieved_at=_timestamp(data["retrieved_at"], "retrieved_at", index),
        valid_from=_date(data["valid_from"], "valid_from", index),
    )
    if not MIN_EUR_PER_GPU_HOUR <= entry.eur_per_gpu_hour <= MAX_EUR_PER_GPU_HOUR:
        raise _fail(
            f"{money(entry.eur_per_gpu_hour)} EUR per GPU-hour is outside "
            f"[{MIN_EUR_PER_GPU_HOUR}, {MAX_EUR_PER_GPU_HOUR}]; check basis and unit",
            index,
        )
    return entry


# ---------------------------------------------------------------------------
# Loading and committing
# ---------------------------------------------------------------------------


def _reject_duplicate_keys(pairs: list) -> dict:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise PriceCatalogError("price catalog has a duplicate key")
    return dict(pairs)


def _reject_constant(name: str) -> float:
    raise PriceCatalogError(f"price catalog has a non-finite constant {name!r}")


def _read(path: str | Path) -> dict:
    try:
        text = read_regular_file(path, MAX_CATALOG_BYTES, "price catalog")
    except InferenceCalibrationError as exc:
        raise PriceCatalogError(str(exc)) from exc
    except OSError as exc:
        raise PriceCatalogError(f"cannot read price catalog: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise PriceCatalogError("price catalog is not UTF-8") from exc
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ValueError as exc:
        if isinstance(exc, PriceCatalogError):
            raise
        raise PriceCatalogError(f"price catalog is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PriceCatalogError("price catalog must be a JSON object")
    return data


def catalog_digest(record: dict) -> str:
    """SHA-256 over the canonical catalog content, excluding the commitment."""
    content = {key: value for key, value in record.items() if key != COMMITMENT_KEY}
    envelope = {"schema": COMMITMENT_SCHEMA, "record": content}
    try:
        canonical = _canonicalize(envelope)
    except InferenceCalibrationError as exc:
        raise PriceCatalogError(f"price catalog cannot be hashed: {exc}") from exc
    return hashlib.sha256(_canonical_bytes(canonical)).hexdigest()


def _validate(
    data: dict, today: date, now: datetime | None = None
) -> tuple[date, tuple[PriceEntry, ...]]:
    data = _keys(data, _CATALOG_KEYS, "catalog", None)  # type: ignore[arg-type]
    if data.get("schema") != CATALOG_SCHEMA:
        raise PriceCatalogError(f"price catalog schema is not {CATALOG_SCHEMA}")
    if data.get("currency") != "EUR":
        raise PriceCatalogError("price catalog currency must be EUR")
    if data.get("vat") != "excluded":
        raise PriceCatalogError("price catalog prices must exclude VAT (vat: excluded)")
    as_of = _date(data.get("as_of"), "as_of", None)
    if as_of > today + timedelta(days=1):
        raise PriceCatalogError(f"price catalog as_of {as_of} is in the future")
    raw = data.get("entries")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ENTRIES:
        raise PriceCatalogError(f"price catalog needs 1–{MAX_ENTRIES} entries")
    entries = tuple(_entry(item, i) for i, item in enumerate(raw))
    # One campaign: retrieved on as_of or up to MAX_RETRIEVAL_SPREAD_DAYS
    # calendar days before it, and never after now.
    now = now or datetime.now(timezone.utc)
    start = datetime.combine(
        as_of - timedelta(days=MAX_RETRIEVAL_SPREAD_DAYS),
        datetime.min.time(),
        timezone.utc,
    )
    end = datetime.combine(as_of + timedelta(days=1), datetime.min.time(), timezone.utc)
    for i, entry in enumerate(entries):
        if not start <= entry.retrieved_at < end:
            raise _fail(
                f"retrieved_at {entry.retrieved_at:%Y-%m-%d} must fall between "
                f"{start:%Y-%m-%d} and as_of {as_of} ({MAX_RETRIEVAL_SPREAD_DAYS} "
                "days, inclusive)",
                i,
            )
        if entry.retrieved_at > now:
            raise _fail(
                f"retrieved_at {entry.retrieved_at:%Y-%m-%dT%H:%M:%SZ} is in the "
                "future",
                i,
            )
        if entry.valid_from > as_of:
            raise _fail(f"valid_from {entry.valid_from} is after as_of {as_of}", i)
    seen: set[tuple] = set()
    for i, entry in enumerate(entries):
        key = (*entry.family, entry.sku)
        if key in seen:
            raise _fail(f"duplicate SKU {entry.sku!r} in one family", i)
        seen.add(key)
    return as_of, entries


def load_price_catalog(
    path: str | Path, today: date | None = None, now: datetime | None = None
) -> PriceCatalog:
    """Load a committed catalog; fail closed on tamper or malformed content.

    Freshness is checked separately (:func:`catalog_age`), at use.
    """
    today = today or datetime.now(timezone.utc).date()
    data = _read(path)
    commitment = data.get(COMMITMENT_KEY)
    digest = commitment.get("digest") if isinstance(commitment, dict) else None
    if (
        not isinstance(commitment, dict)
        or set(commitment) != {"schema", "digest"}
        or commitment.get("schema") != COMMITMENT_SCHEMA
        or not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
    ):
        raise PriceCatalogTamperError(
            "price catalog carries no valid commitment; run `pat infer-prices "
            "commit` on the reviewed file"
        )
    if catalog_digest(data) != digest:
        raise PriceCatalogTamperError(
            "price catalog does not match its commitment; it was modified after "
            "it was committed"
        )
    as_of, entries = _validate(data, today, now)
    return PriceCatalog(
        name=os.path.basename(str(path)), as_of=as_of, entries=entries, digest=digest
    )


def commit_price_catalog(
    source: str | Path,
    target: str | Path,
    today: date | None = None,
    now: datetime | None = None,
) -> str:
    """Validate an uncommitted catalog and write it with its commitment.

    The target must not exist; a file that already carries a commitment is
    refused, so an edit is never silently re-blessed.
    """
    today = today or datetime.now(timezone.utc).date()
    data = _read(source)
    if COMMITMENT_KEY in data:
        raise PriceCatalogError(
            "price catalog is already committed; edit a copy without "
            f"{COMMITMENT_KEY!r} and commit that"
        )
    _validate(data, today, now)
    digest = catalog_digest(data)
    record = {**data, COMMITMENT_KEY: {"schema": COMMITMENT_SCHEMA, "digest": digest}}
    payload = (json.dumps(record, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,  # owner-only, like every file pat writes; share it deliberately
    )
    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    return digest


def catalog_age(catalog: PriceCatalog, today: date | None = None) -> tuple[int, bool]:
    """``(age in days, stale)``; refuses a catalog past :data:`REFUSE_DAYS`."""
    today = today or datetime.now(timezone.utc).date()
    age = (today - catalog.as_of).days
    if age > REFUSE_DAYS:
        raise PriceCatalogError(
            f"price catalog {catalog.name} is {age} days old (as_of "
            f"{catalog.as_of}); prices older than {REFUSE_DAYS} days are refused. "
            "Use a fresh catalog."
        )
    return age, age > FRESH_DAYS


def catalog_template() -> dict:
    """A skeleton to fill in: it has no prices, so it can never load."""
    return {
        "schema": CATALOG_SCHEMA,
        "as_of": "YYYY-MM-DD",
        "currency": "EUR",
        "vat": "excluded",
        "entries": [
            {
                "provider": "provider-slug",
                "region": {"country": "FR", "datacenter": "datacenter-label"},
                "gpu": {"model": "GPU model", "memory_gb": 0, "bandwidth_gbs": 0},
                "sku": "instance type as sold",
                "gpus_per_instance": 1,
                "billing": {"basis": "instance", "unit": "hour"},
                "tier": "on-demand",
                "source_url": "https://provider.example/pricing",
                "retrieved_at": "YYYY-MM-DDTHH:MM:SSZ",
                "valid_from": "YYYY-MM-DD",
            }
        ],
    }


# ---------------------------------------------------------------------------
# Selection and pricing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PriceFamily:
    catalog: PriceCatalog
    entries: tuple[PriceEntry, ...]  # one provider, region, GPU and tier

    @property
    def head(self) -> PriceEntry:
        return self.entries[0]

    def describe(self) -> str:
        e = self.head
        where = e.country + (f"/{e.datacenter}" if e.datacenter else "")
        term = f" {e.term_months}-month" if e.term_months else ""
        return f"{e.provider} {where} {e.gpu_model} {e.tier}{term}"

    def selection(self) -> dict:
        e = self.head
        return {
            "provider": e.provider,
            "country": e.country,
            "datacenter": e.datacenter,
            "gpu": e.gpu_model,
            "memory_gb": e.memory_gb,
            "tier": e.tier,
            "term_months": e.term_months,
        }


def select_family(
    catalog: PriceCatalog,
    provider: str,
    region: str | None = None,
    gpu: str | None = None,
    tier: str = "on-demand",
) -> PriceFamily:
    """Exactly one (provider, region, GPU, tier) family, or a refusal."""
    if tier not in TIERS:
        raise PriceCatalogError(f"tier must be one of {TIERS}")

    def wanted(e: PriceEntry) -> bool:
        return (
            e.provider == provider
            and e.tier == tier
            and (region is None or region in (e.country, e.datacenter))
            and (gpu is None or gpu_model_key(e.gpu_model) == gpu_model_key(gpu))
        )

    families: dict[tuple, list[PriceEntry]] = {}
    for entry in catalog.entries:
        if wanted(entry):
            families.setdefault(entry.family, []).append(entry)
    if not families:
        raise PriceCatalogError(
            f"no {tier} prices for provider {provider!r}"
            + (f" in {region!r}" if region else "")
            + (f" for GPU {gpu!r}" if gpu else "")
            + f" in {catalog.name}"
        )
    if len(families) > 1:
        options = sorted(
            PriceFamily(catalog, tuple(entries)).describe()
            for entries in families.values()
        )
        raise PriceCatalogError(
            "the selection matches several price families; narrow it with "
            "--price-region / --price-gpu / --price-tier: " + "; ".join(options)
        )
    (entries,) = families.values()
    return PriceFamily(
        catalog, tuple(sorted(entries, key=lambda e: e.gpus_per_instance))
    )


_VENDORS: Final[frozenset[str]] = frozenset({"nvidia", "amd", "intel"})


def gpu_model_key(name: str) -> tuple[str, ...]:
    """One rule for GPU names from profiles, catalogs and DCGM labels.

    Casefolded whitespace tokens without a leading vendor: "NVIDIA L40S" and
    "l40s" agree, "L40" and "L40S" do not (a substring test would match).
    """
    tokens = name.casefold().split()
    while tokens and tokens[0] in _VENDORS:
        tokens = tokens[1:]
    return tuple(tokens)


def gpu_family_key(name: str) -> str:
    """The GPU family alone: "NVIDIA H100 80GB HBM3" and "H100" → "h100".

    For DCGM's ``modelName``, which spells out variants ("NVIDIA
    A100-SXM4-80GB") that profiles and catalogs do not; the variant is pinned
    by the memory and bandwidth checks instead. "L40" and "L40S" still differ.
    """
    key = gpu_model_key(name)
    return key[0].split("-")[0] if key else ""


def require_gpu_match(
    family: PriceFamily,
    gpu_memory_gb: float,
    gpu_bandwidth_gbs: float,
    gpu_type: str | None = None,
    gpus_per_node: int | None = None,
) -> None:
    """Fail closed when the priced GPU is not the analysed one (>1 %)."""
    e = family.head
    for entry in family.entries:
        checks = [("GPU memory (GB)", gpu_memory_gb, entry.memory_gb)]
        if entry.bandwidth_gbs is not None:
            checks.append(
                ("GPU bandwidth (GB/s)", gpu_bandwidth_gbs, entry.bandwidth_gbs)
            )
        for name, given, priced in checks:
            if abs(given - priced) > HARDWARE_MATCH_TOLERANCE * priced:
                raise PriceCatalogError(
                    f"the selected prices ({entry.sku}) are for {entry.gpu_model} "
                    f"with {name} = {priced:g}, not the analysed {given:g}"
                )
    if gpu_type is not None and gpu_model_key(gpu_type) != gpu_model_key(e.gpu_model):
        raise PriceCatalogError(
            f"the calibration is for GPU {gpu_type!r}; the selected prices are for "
            f"{e.gpu_model!r}"
        )
    largest = max(entry.gpus_per_instance for entry in family.entries)
    if gpus_per_node is not None and gpus_per_node < largest:
        raise PriceCatalogError(
            f"the selected family sells {largest}-GPU instances but "
            f"--gpus-per-node is {gpus_per_node}; pass --gpus-per-node {largest}"
        )


@dataclass(frozen=True)
class LayoutPrice:
    eur_per_hour: Decimal | None
    entry: PriceEntry | None
    instances_billed: int
    gpus_billed: int
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "eur_per_hour": (
                None if self.eur_per_hour is None else money(self.eur_per_hour)
            ),
            "sku": None if self.entry is None else self.entry.sku,
            "instances_billed": self.instances_billed,
            "gpus_billed": self.gpus_billed,
            "reason": self.reason,
        }


def layout_price(family: PriceFamily, tp: int, instances: int) -> LayoutPrice:
    """Cheapest single-SKU packing of ``instances`` TP groups of ``tp`` GPUs."""
    best: LayoutPrice | None = None
    for entry in family.entries:
        per_instance = entry.gpus_per_instance // tp
        if per_instance < 1:
            continue
        billed = max(math.ceil(instances / per_instance), entry.min_instances)
        cost = billed * entry.eur_per_instance_hour
        if best is None or cost < best.eur_per_hour:  # type: ignore[operator]
            best = LayoutPrice(cost, entry, billed, billed * entry.gpus_per_instance)
    if best is None:
        return LayoutPrice(None, None, 0, 0, reason=f"no SKU holds a tp={tp} group")
    return best


def price_catalog_json(
    family: PriceFamily, age_days: int, stale: bool, used: list[PriceEntry]
) -> dict:
    """What a report cites: which catalog, which selection, which prices."""
    unique = {entry.sku: entry for entry in used}
    return {
        "name": family.catalog.name,
        "as_of": family.catalog.as_of.isoformat(),
        "digest": family.catalog.digest,
        "age_days": age_days,
        "stale": stale,
        "currency": "EUR",
        "vat": "excluded",
        "selection": family.selection(),
        "entries_used": [unique[sku].cited() for sku in sorted(unique)],
    }
