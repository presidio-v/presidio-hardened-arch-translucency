"""Signed SLO-degradation evidence producer (``evidence-ref@1``).

Turns an arch-translucency observation into a ``presidio-hardened/evidence-ref@1``
envelope that downstream family consumers verify fail-closed before acting on
it: a spoofed or misconfigured reading carries no valid signature and is
rejected.

Vendored contract: this implements the family canonical-JSON + detached-signature
wire format directly. ``presidio-evidence`` is a private repo, so this public
repo vendors the schema/vectors and signs from its own copy — exactly as
``presidio-hardened-x402``'s ``mica.py`` does. Conformance is pinned to the family
golden vector in the tests. Realizes evidence backlog L-EV-3 (arch-translucency as
the runtime-posture evidence producer).

Ed25519 needs the optional ``[evidence]`` extra (``cryptography``). The Ed25519
key format is a raw 32-byte private seed encoded as exactly 64 lowercase hex
characters; HMAC-SHA256 and the canonical/hash layer are pure stdlib.
"""

from __future__ import annotations

import hashlib
import hmac as hmaclib
import json
import math
import re
import string
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .observe import Observation

EVIDENCE_SCHEMA_ID = "presidio-hardened/evidence-ref@1"
#: Wire id for the **unsigned** Layer-0 reading arch-translucency emits (key-less);
#: a signing-bridge sidecar turns it into a signed ``evidence-ref@1`` envelope.
LAYER0_SCHEMA_ID = "presidio-hardened/slo-reading@1"
#: Wire id for the **unsigned** Layer-0 training-run record (key-less; same
#: signing-bridge pattern as ``slo-reading@1``). Carries the payload-level
#: provenance convention: ``parents`` — content hashes of upstream evidence
#: (e.g. the eai-classification and gate-decision envelopes that authorized
#: the run) — turning isolated envelopes into a verifiable provenance DAG.
TRAINING_SCHEMA_ID = "presidio-hardened/training-run@1"
#: Wire id for the **unsigned** Layer-0 measured-energy reading (key-less; same
#: signing-bridge pattern as ``slo-reading@1`` / ``training-run@1``). Emits a
#: window of chained energy_observations plus the energy-chain head hash it
#: anchors on, discharging ADR-0010's anchoring deferral (v0.24.0, Energy Arc
#: finale). Corollary E1a: *pat never signs a watt it did not measure* — the
#: figures come exclusively from the preset-attested measured-energy store.
ENERGY_READING_SCHEMA_ID = "presidio-hardened/energy-reading@1"
DEFAULT_SIGNER = "presidio-hardened-arch-translucency"
SIGNING_ALGORITHMS = ("ed25519", "hmac-sha256")
_LOWER_HEX = frozenset(string.hexdigits.lower()[:16])
_ENERGY_DECIMAL_MAX_CHARS = 64


class EvidenceProducerError(ValueError):
    """Raised on invalid evidence configuration or signing failure (fail-closed)."""


def _reject_floats(payload: object) -> None:
    """Strict profile (ADR-0001 D1): floats are non-deterministic across encoders,
    so a hash over them is not portable. Reject any float; ``bool`` is allowed."""
    if isinstance(payload, float):
        raise EvidenceProducerError(
            "canonical encoding rejects floats; use integers (round ms to int)"
        )
    if isinstance(payload, Mapping):
        for value in payload.values():
            _reject_floats(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            _reject_floats(value)


def canonical_bytes(payload: object) -> bytes:
    """Deterministic canonical JSON — must byte-match every family producer."""
    _reject_floats(payload)
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(payload: object) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _require_crypto():  # noqa: ANN202
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise EvidenceProducerError(
            "Ed25519 signing needs the optional extra: "
            "pip install 'presidio-hardened-arch-translucency[evidence]'"
        ) from exc
    return ed25519


def _ed25519_seed_from_hex(key: str) -> bytes:
    if len(key) != 64 or any(ch not in _LOWER_HEX for ch in key):
        raise EvidenceProducerError(
            "Ed25519 private key must be a raw 32-byte seed encoded as "
            "exactly 64 lowercase hex characters"
        )
    return bytes.fromhex(key)


def sign_evidence(
    content_hash: str, signer: str, *, algorithm: str = "ed25519", key: str = ""
) -> str:
    """Detached signature over ``canonical({content_hash, signer})`` (lowercase hex).

    Fail-closed: unknown algorithm or missing/malformed key raises.
    """
    if not key:
        raise EvidenceProducerError("signing requires a key (no unsigned output)")
    message = canonical_bytes({"content_hash": content_hash, "signer": signer})
    if algorithm == "ed25519":
        ed25519 = _require_crypto()
        try:
            sk = ed25519.Ed25519PrivateKey.from_private_bytes(
                _ed25519_seed_from_hex(key)
            )
        except ValueError as exc:
            raise EvidenceProducerError(
                "Ed25519 private key must be a raw 32-byte seed encoded as "
                "exactly 64 lowercase hex characters"
            ) from exc
        return sk.sign(message).hex()
    if algorithm == "hmac-sha256":
        return hmaclib.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()
    raise EvidenceProducerError(
        f"unknown signing algorithm {algorithm!r} (use ed25519 | hmac-sha256)"
    )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _package_version() -> str:
    try:
        from importlib.metadata import version

        return version("presidio-hardened-arch-translucency")
    except Exception:  # pragma: no cover - metadata absent in some dev trees
        return "0+unknown"


def build_slo_evidence(
    *,
    slo: str,
    value: int,
    threshold: int,
    window: str,
    private_key_hex: str,
    algorithm: str = "ed25519",
    signer: str = DEFAULT_SIGNER,
    source_version: str | None = None,
    ledger_ref: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Build a signed ``evidence-ref@1`` degradation envelope.

    The attested content is the SLO reading (``{slo, value, threshold, window}``,
    integers only). The consumer recomputes its hash and verifies the signature
    before acting — see ``presidio-hardened-x402`` ``ArchTranslucencyAdapter``.
    """
    content = {
        "slo": slo,
        "value": int(value),
        "threshold": int(threshold),
        "window": window,
    }
    content_hash = sha256_hex(content)
    signature = sign_evidence(
        content_hash, signer, algorithm=algorithm, key=private_key_hex
    )
    claimed_at = observed_at or _utcnow_iso()
    version = source_version or _package_version()
    ref = {
        "item_id": "SLO-DEGRADED",
        "source": signer,
        "source_version": version,
        "ledger_ref": ledger_ref or "arch-translucency:obs",
        "content_hash": content_hash,
        "signer": signer,
        "signature": signature,
        "claimed_at": claimed_at,
    }
    return {
        "schema": EVIDENCE_SCHEMA_ID,
        "attested_content": content,
        "evidence": [ref],
        "generated_at": claimed_at,
    }


def observation_to_evidence(
    observation: Observation,
    *,
    slo_target_ms: int,
    private_key_hex: str,
    window: str = "5m",
    algorithm: str = "ed25519",
    signer: str = DEFAULT_SIGNER,
    ledger_ref: str | None = None,
) -> dict:
    """Convenience: a p99-latency degradation envelope from an ``Observation``.

    ``p99_latency_ms`` is rounded to an integer (the canonical profile rejects
    floats); ``degraded`` on the consumer side is ``value > threshold``.
    """
    return build_slo_evidence(
        slo="p99_latency_ms",
        value=round(observation.p99_latency_ms),
        threshold=int(slo_target_ms),
        window=window,
        private_key_hex=private_key_hex,
        algorithm=algorithm,
        signer=signer,
        ledger_ref=ledger_ref,
    )


# ---------------------------------------------------------------------------
# Layer 0 — unsigned reading (key-less). arch-translucency holds NO signing key;
# it emits this, and a signing-bridge sidecar turns it into a signed
# ``evidence-ref@1`` envelope. The ``content_hash`` binds the reading; the sidecar
# recomputes it before signing so transport corruption is caught.
# ---------------------------------------------------------------------------


def build_layer0_reading(
    *,
    slo: str,
    value: int,
    threshold: int,
    window: str,
    source_version: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Build an **unsigned** Layer-0 SLO reading (no key held).

    Integers only (the canonical profile rejects floats). The result carries the
    attested content and its content hash but **no signature** — signing is the
    sidecar's job.
    """
    content = {
        "slo": slo,
        "value": int(value),
        "threshold": int(threshold),
        "window": window,
    }
    return {
        "schema": LAYER0_SCHEMA_ID,
        "attested_content": content,
        "content_hash": sha256_hex(content),
        "source": DEFAULT_SIGNER,
        "source_version": source_version or _package_version(),
        "generated_at": observed_at or _utcnow_iso(),
    }


def observation_to_layer0(
    observation: Observation,
    *,
    slo_target_ms: int,
    window: str = "5m",
    source_version: str | None = None,
) -> dict:
    """Convenience: an unsigned Layer-0 p99-latency reading from an ``Observation``."""
    return build_layer0_reading(
        slo="p99_latency_ms",
        value=round(observation.p99_latency_ms),
        threshold=int(slo_target_ms),
        window=window,
        source_version=source_version,
    )


def is_degraded(reading: Mapping[str, object]) -> bool:
    """True when a Layer-0 reading's observed value breaches its threshold."""
    content = reading.get("attested_content")
    if not isinstance(content, Mapping):
        raise EvidenceProducerError("reading has no attested_content mapping")
    return int(content["value"]) > int(content["threshold"])


# ---------------------------------------------------------------------------
# Training-run evidence (``training-run@1``) — Layer 0, key-less.
#
# A training run becomes a content-addressed, signable record: parallelism
# configuration, throughput, duration and device count. It can support broader
# operator technical documentation but is not standalone compliance evidence.
# ``parents`` implements the family payload-level
# provenance convention: hashes of upstream evidence payloads (classification,
# gate decision, dataset attestation) are attested *inside* the signed
# content, so the frozen ``evidence-ref@1`` envelope is untouched while
# envelopes become nodes of a provenance DAG.
# ---------------------------------------------------------------------------

#: Family hash discipline (mirrors ``evidence-ref@1``'s
#: ``^[0-9a-f]{8,128}$`` for ``content_hash``).
_PARENT_HASH_MIN, _PARENT_HASH_MAX = 8, 128

#: Valid training strategies. Kept local (not imported from ``training``) so
#: this vendored-contract module stays self-contained for signing-bridge
#: sidecars; a test pins it to ``training.VALID_STRATEGIES``.
_TRAINING_STRATEGIES = ("data", "fsdp", "tensor", "pipeline")

#: ``run_id`` bound matches the family ≤512-char field discipline.
_RUN_ID_MAX = 512


def _validate_run_id(value: object) -> str:
    """Non-empty printable string, ≤512 chars, no control characters.

    Audit finding (v0.18.0 third-party): a newline-bearing ``run_id`` produced
    a signable record; the library must enforce the contract independently of
    the CLI so a sidecar can never sign malformed content.
    """
    if not isinstance(value, str) or not value.strip():
        raise EvidenceProducerError("run_id must be a non-empty string")
    if len(value) > _RUN_ID_MAX:
        raise EvidenceProducerError(f"run_id must be <= {_RUN_ID_MAX} characters")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise EvidenceProducerError("run_id must not contain control characters")
    return value


def _coerce_int(value: object, name: str, *, minimum: int) -> int:
    """Coerce to int, fail-closed on nan/inf/non-numeric/below-minimum."""
    if isinstance(value, bool):
        raise EvidenceProducerError(f"{name} must be an integer, got bool")
    if isinstance(value, float) and not value.is_integer():
        # No silent truncation: the canonical profile rejects floats on the
        # wire, so rounding is the caller's explicit decision, not ours.
        raise EvidenceProducerError(f"{name} must be an integer (round upstream)")
    try:
        v = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceProducerError(f"{name} must be an integer") from exc
    if v < minimum:
        raise EvidenceProducerError(f"{name} must be >= {minimum}, got {v}")
    return v


def _coerce_energy_value(value: object, name: str) -> str:
    """Validate an optional energy field for the wire (fail-closed, float-rejecting).

    The canonical profile rejects bare floats (non-portable across encoders), so
    a ``float`` here **raises before any coercion** — a caller with a float must
    make the string/int decision explicitly (the wire discipline). Accepted:

    * ``int >= 0`` — normalized to the same string-decimal form as strings;
    * a decimal *string* that parses to a finite value ``>= 0`` **and** survives
      an IEEE-754 round-trip losslessly — normalized to ``repr(float(s))``.

    A decimal string that does not round-trip losslessly (``Decimal(s) !=
    Decimal(repr(float(s)))``) is rejected as "not representable as an IEEE-754
    round-trip decimal" rather than silently truncated.

    Accepted string grammar is deliberately Python's ``float()`` grammar (so
    scientific notation ``"1e3"``, a leading ``+``, and surrounding whitespace
    are accepted and normalized) — normalization to ``repr(float(s))`` makes
    every accepted spelling canonical on the wire. Negative zero (``"-0"`` /
    ``"-0.0"``) is normalized to ``"0.0"``: two spellings of a semantically
    identical value must not produce two different content hashes.
    """
    if isinstance(value, bool):
        raise EvidenceProducerError(
            f"{name} must be an int or decimal string, got bool"
        )
    if isinstance(value, float):
        raise EvidenceProducerError(
            f"{name} must be an int or decimal string, not a float "
            '(floats are non-portable on the wire; pass e.g. "12.5" or an int)'
        )
    if isinstance(value, int):
        if value < 0:
            raise EvidenceProducerError(f"{name} must be >= 0, got {value}")
        try:
            f = float(value)
        except OverflowError as exc:
            raise EvidenceProducerError(f"{name} is too large for the wire") from exc
        if not math.isfinite(f) or Decimal(value) != Decimal(repr(f)):
            raise EvidenceProducerError(
                f"{name}={value!r} is not representable as an IEEE-754 "
                "round-trip decimal"
            )
        return repr(f)
    if isinstance(value, str):
        s = value.strip()
        if not s or len(s) > _ENERGY_DECIMAL_MAX_CHARS:
            raise EvidenceProducerError(
                f"{name} must be a non-empty decimal string no longer than "
                f"{_ENERGY_DECIMAL_MAX_CHARS} characters"
            )
        try:
            dec = Decimal(s)
        except (InvalidOperation, ValueError) as exc:
            raise EvidenceProducerError(
                f"{name} must be a decimal string, got {value!r}"
            ) from exc
        if not dec.is_finite():
            raise EvidenceProducerError(f"{name} must be finite, got {value!r}")
        if dec < 0:
            raise EvidenceProducerError(f"{name} must be >= 0, got {value!r}")
        f = float(s)
        if f == 0.0:
            # Collapse IEEE-754 negative zero: "-0.0" and "0.0" are the same
            # energy; distinct wire strings would yield distinct content hashes.
            f = 0.0
        if Decimal(s) != Decimal(repr(f)):
            raise EvidenceProducerError(
                f"{name}={value!r} is not representable as an IEEE-754 round-trip "
                "decimal (would lose precision on the wire)"
            )
        return repr(f)
    raise EvidenceProducerError(
        f"{name} must be an int or decimal string, got {type(value).__name__!r}"
    )


def _validate_parent_hash(value: object) -> str:
    if (
        not isinstance(value, str)
        or not (_PARENT_HASH_MIN <= len(value) <= _PARENT_HASH_MAX)
        or any(ch not in _LOWER_HEX for ch in value)
    ):
        raise EvidenceProducerError(
            "parent reference must be a lowercase-hex content hash "
            f"({_PARENT_HASH_MIN}-{_PARENT_HASH_MAX} chars)"
        )
    return value


def build_training_run_reading(
    *,
    run_id: str,
    strategy: str,
    degree: int,
    samples_per_second: int,
    duration_s: int,
    device_count: int,
    parents: tuple[str, ...] | list[str] = (),
    model_hash: str | None = None,
    dataset_hash: str | None = None,
    energy_wh: str | int | None = None,
    mean_power_w: str | int | None = None,
    source_version: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Build an **unsigned** Layer-0 ``training-run@1`` record (no key held).

    Integers only for the core figures (the canonical profile rejects floats —
    round upstream). ``parents`` are content hashes of upstream evidence payloads
    and are attested inside the signed content; they are validated against the
    family lowercase-hex discipline and included only when non-empty (fail-closed
    on malformed hashes). ``model_hash`` / ``dataset_hash`` bind the run to its
    artifacts when available.

    Optional energy fields (v0.23.0, additive): ``energy_wh`` (run energy in
    watt-hours) and ``mean_power_w`` (mean total run power in watts) are **producer
    claims / modelled estimates** under the v0.18 trust boundary — attributed as
    such (PRESIDIO-REQ Energy Arc E1a), never observation-chain readings. Each is
    independently optional (both-or-neither is *not* required). When both are
    supplied, their relationship to ``duration_s`` is checked within 2% (or
    0.01 Wh) so contradictory signable evidence fails closed. A ``float`` is
    rejected on the wire — pass an int or a decimal string
    (e.g. ``"12.5"``). A field is included in the attested content **only when
    provided**, so a record with no energy hashes byte-identically to a pre-v0.23
    record for the same core inputs.

    The full contract is enforced here, independently of any CLI (fail-closed:
    a sidecar must never be handed malformed signable content): valid strategy,
    printable bounded ``run_id``, non-negative integers, degree/devices >= 1.
    """
    run_id = _validate_run_id(run_id)
    if strategy not in _TRAINING_STRATEGIES:
        raise EvidenceProducerError(
            f"strategy must be one of {_TRAINING_STRATEGIES!r}, got {strategy!r}"
        )
    content: dict[str, object] = {
        "run_id": run_id,
        "strategy": strategy,
        "degree": _coerce_int(degree, "degree", minimum=1),
        "samples_per_second": _coerce_int(
            samples_per_second, "samples_per_second", minimum=0
        ),
        "duration_s": _coerce_int(duration_s, "duration_s", minimum=0),
        "device_count": _coerce_int(device_count, "device_count", minimum=1),
    }
    validated_parents = [_validate_parent_hash(p) for p in parents]
    if validated_parents:
        content["parents"] = validated_parents
    if model_hash is not None:
        content["model_hash"] = _validate_parent_hash(model_hash)
    if dataset_hash is not None:
        content["dataset_hash"] = _validate_parent_hash(dataset_hash)
    canonical_energy = (
        _coerce_energy_value(energy_wh, "energy_wh") if energy_wh is not None else None
    )
    canonical_power = (
        _coerce_energy_value(mean_power_w, "mean_power_w")
        if mean_power_w is not None
        else None
    )
    if canonical_energy is not None and canonical_power is not None:
        expected_wh = Decimal(canonical_power) * Decimal(content["duration_s"]) / 3600
        difference = abs(Decimal(canonical_energy) - expected_wh)
        tolerance = max(abs(expected_wh) * Decimal("0.02"), Decimal("0.01"))
        if difference > tolerance:
            raise EvidenceProducerError(
                "energy_wh and mean_power_w contradict duration_s "
                f"(expected approximately {expected_wh} Wh)"
            )
    if canonical_energy is not None:
        content["energy_wh"] = canonical_energy
    if canonical_power is not None:
        content["mean_power_w"] = canonical_power
    return {
        "schema": TRAINING_SCHEMA_ID,
        "attested_content": content,
        "content_hash": sha256_hex(content),
        "source": DEFAULT_SIGNER,
        "source_version": source_version or _package_version(),
        "generated_at": observed_at or _utcnow_iso(),
    }


# ---------------------------------------------------------------------------
# Measured-energy reading (``energy-reading@1``) — Layer 0, key-less (v0.24.0).
#
# The Energy Arc finale: a window of the chained measured-energy store is emitted
# as an unsigned reading that carries the energy-chain HEAD hash it anchors on.
# Publishing that head externally (a sidecar signs the reading) makes any later
# local rewrite of the store detectable — an editor cannot reproduce the same
# head — discharging ADR-0010's anchoring deferral.
#
# Corollary E1a (*pat never signs a watt it did not measure*): every figure in
# this reading originates from the preset-attested measured-energy store; there
# is deliberately no override path for the energy numbers. The honest bound is
# unchanged: a clean chain + external anchor proves the recorded history was not
# rewritten after the fact, NOT that the meter was honest at capture time.
# ---------------------------------------------------------------------------

#: Max length of an RFC3339 UTC instant string on the wire (control-char free,
#: bounded so a malformed field can never grow unbounded signable content).
_INSTANT_MAX_CHARS = 64
_RFC3339_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)

#: The meters PAT is allowed to *emit* in a reading. Kept local so this
#: vendored-contract module stays self-contained for signing-bridge sidecars
#: (same discipline as ``_TRAINING_STRATEGIES``); a cross-check test pins it to
#: ``observe.VALID_METERS``. The family schema also allows ``"kepler"``, but the
#: v0.21 release audit refused Kepler attribution, so PAT never emits it.
_EMIT_METERS = ("rapl", "dcgm")

#: Serving replication layers PAT may name in a reading. Kept local for the same
#: self-containment reason; a cross-check test pins it to ``model.VALID_LAYERS``.
_SERVING_LAYERS = ("container", "pod", "deployment", "node")


def _validate_utc_instant(value: object, name: str) -> tuple[str, datetime]:
    """Validate an RFC3339 UTC *instant* string; return ``(wire_string, parsed)``.

    Fail-closed (a sidecar must never sign a malformed timestamp): the string
    must be non-empty, bounded, control-char free, parse as ISO-8601, and carry
    an **explicit UTC offset** (``Z`` or ``+00:00``). A naive (offset-less) or
    non-UTC instant is rejected — an anchored reading names a UTC window or none
    at all. The original wire string is returned unchanged so the caller's exact
    spelling is what gets hashed (the parsed value is for ordering only).
    """
    if not isinstance(value, str):
        raise EvidenceProducerError(
            f"{name} must be an RFC3339 UTC instant string, got "
            f"{type(value).__name__!r}"
        )
    if not value or len(value) > _INSTANT_MAX_CHARS:
        raise EvidenceProducerError(
            f"{name} must be a non-empty instant string no longer than "
            f"{_INSTANT_MAX_CHARS} characters"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise EvidenceProducerError(f"{name} must not contain control characters")
    if _RFC3339_UTC_RE.fullmatch(value) is None:
        raise EvidenceProducerError(
            f"{name} must be strict RFC3339 UTC using 'T' and either 'Z' or "
            f"'+00:00', got {value!r}"
        )
    # Python 3.10's fromisoformat does not accept a trailing 'Z'; normalise it.
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise EvidenceProducerError(
            f"{name} must be an ISO-8601 instant, got {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise EvidenceProducerError(
            f"{name} must carry an explicit UTC offset (naive instant rejected)"
        )
    if parsed.utcoffset() != timedelta(0):
        raise EvidenceProducerError(
            f"{name} must be UTC (offset 'Z' or '+00:00'), got {value!r}"
        )
    return value, parsed


def _validate_chain_head(value: object) -> str:
    """A chain head is a full SHA-256 hex digest: exactly 64 lowercase hex chars."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in _LOWER_HEX for ch in value)
    ):
        raise EvidenceProducerError(
            "energy_chain_head must be a lowercase-hex SHA-256 digest (64 chars)"
        )
    return value


def build_energy_reading(
    *,
    window_start: str,
    window_end: str,
    energy_wh: str | int,
    mean_power_w: str | int,
    meter: str,
    energy_chain_head: str,
    layer: str | None = None,
    strategy: str | None = None,
    parents: tuple[str, ...] | list[str] = (),
    source_version: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Build an **unsigned** Layer-0 ``energy-reading@1`` record (no key held).

    The measured-energy anchoring reading (v0.24.0, Energy Arc finale). The
    attested content is a UTC window of the chained measured-energy store plus
    the energy-chain **head hash** it anchors on; a signing-bridge sidecar turns
    it into a signed ``evidence-ref@1`` envelope. Publishing the head externally
    makes a later silent rewrite of the store detectable (ADR-0010 discharged).

    Trust boundary (mirrors :func:`build_training_run_reading`): this validates
    WIRE SHAPE only and trusts the caller for measured provenance; E1a
    store-provenance (*every figure came from the preset-attested measured-energy
    store*) is enforced by the CLI derivation (:func:`cli._derive_energy_reading`),
    not here. Sidecar authors must not treat shape validation as measured-ness.

    Fail-closed, independent of any CLI (a sidecar must never be handed malformed
    signable content):

    * ``window_start`` / ``window_end`` — RFC3339 UTC instant strings with an
      explicit offset; ``start < end`` (see :func:`_validate_utc_instant`).
    * ``energy_wh`` / ``mean_power_w`` — int or decimal *string* via
      :func:`_coerce_energy_value` (floats rejected on the wire; IEEE-754
      round-trip enforced; negative zero collapses).
    * ``meter`` — one of PAT's emitting meters (:data:`_EMIT_METERS`, pinned to
      ``observe.VALID_METERS``); ``"kepler"`` is refused (v0.21 audit).
    * exactly one of ``layer`` (serving) XOR ``strategy`` (training) — both or
      neither is an error; ``layer`` is validated against the serving layers,
      ``strategy`` against the training strategies.
    * ``energy_chain_head`` — a 64-char lowercase-hex SHA-256 digest.
    * ``parents`` — optional upstream content hashes (ADR-0002), attested inside
      the content and included **only when non-empty**.
    """
    ws, ws_dt = _validate_utc_instant(window_start, "window_start")
    we, we_dt = _validate_utc_instant(window_end, "window_end")
    if not ws_dt < we_dt:
        raise EvidenceProducerError("window_start must be strictly before window_end")
    if meter not in _EMIT_METERS:
        raise EvidenceProducerError(
            f"meter must be one of {_EMIT_METERS!r} (PAT emits only measured "
            "meters; 'kepler' was refused by the v0.21 release audit), "
            f"got {meter!r}"
        )
    if (layer is None) == (strategy is None):
        raise EvidenceProducerError(
            "exactly one of layer (serving) or strategy (training) is required; "
            "both given or neither given is rejected"
        )
    canonical_energy = _coerce_energy_value(energy_wh, "energy_wh")
    canonical_power = _coerce_energy_value(mean_power_w, "mean_power_w")
    duration_s = Decimal(str((we_dt - ws_dt).total_seconds()))
    expected_wh = Decimal(canonical_power) * duration_s / Decimal(3600)
    difference = abs(Decimal(canonical_energy) - expected_wh)
    # Permit only the tiny decimal wobble introduced by shortest IEEE-754
    # round-trip strings. Materially contradictory joules/watts must never
    # become signable content (the family contract re-derives this relation).
    tolerance = max(abs(expected_wh) * Decimal("1e-12"), Decimal("1e-12"))
    if difference > tolerance:
        raise EvidenceProducerError(
            "energy_wh and mean_power_w contradict the signed window "
            f"(expected approximately {expected_wh} Wh)"
        )
    content: dict[str, object] = {
        "window_start": ws,
        "window_end": we,
        "energy_wh": canonical_energy,
        "mean_power_w": canonical_power,
        "meter": meter,
        "energy_chain_head": _validate_chain_head(energy_chain_head),
    }
    if layer is not None:
        if layer not in _SERVING_LAYERS:
            raise EvidenceProducerError(
                f"layer must be one of {_SERVING_LAYERS!r}, got {layer!r}"
            )
        content["layer"] = layer
    else:
        if strategy not in _TRAINING_STRATEGIES:
            raise EvidenceProducerError(
                f"strategy must be one of {_TRAINING_STRATEGIES!r}, got {strategy!r}"
            )
        content["strategy"] = strategy
    validated_parents = [_validate_parent_hash(p) for p in parents]
    if validated_parents:
        content["parents"] = validated_parents
    return {
        "schema": ENERGY_READING_SCHEMA_ID,
        "attested_content": content,
        "content_hash": sha256_hex(content),
        "source": DEFAULT_SIGNER,
        "source_version": source_version or _package_version(),
        "generated_at": observed_at or _utcnow_iso(),
    }
