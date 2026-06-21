"""Signed SLO-degradation evidence producer (``evidence-ref@1``).

Turns an arch-translucency observation into a ``presidio-hardened/evidence-ref@1``
envelope that the ``presidio-hardened-x402`` SLO payment broker verifies
fail-closed *before* paying for a capacity upgrade. This makes a degradation
signal an **authorization, not a metric**: a spoofed or misconfigured reading
cannot trigger a payment because it carries no valid signature.

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
