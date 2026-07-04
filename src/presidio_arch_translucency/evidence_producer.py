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
import string
from collections.abc import Mapping
from datetime import datetime, timezone
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
DEFAULT_SIGNER = "presidio-hardened-arch-translucency"
SIGNING_ALGORITHMS = ("ed25519", "hmac-sha256")
_LOWER_HEX = frozenset(string.hexdigits.lower()[:16])


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
# configuration, throughput, duration and device count (EU AI Act Art. 12
# record-keeping / GPAI compute documentation as a by-product of the
# optimization tool). ``parents`` implements the family payload-level
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
    source_version: str | None = None,
    observed_at: str | None = None,
) -> dict:
    """Build an **unsigned** Layer-0 ``training-run@1`` record (no key held).

    Integers only (the canonical profile rejects floats — round upstream).
    ``parents`` are content hashes of upstream evidence payloads and are
    attested inside the signed content; they are validated against the family
    lowercase-hex discipline and included only when non-empty (fail-closed on
    malformed hashes). ``model_hash`` / ``dataset_hash`` bind the run to its
    artifacts when available.

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
    return {
        "schema": TRAINING_SCHEMA_ID,
        "attested_content": content,
        "content_hash": sha256_hex(content),
        "source": DEFAULT_SIGNER,
        "source_version": source_version or _package_version(),
        "generated_at": observed_at or _utcnow_iso(),
    }
