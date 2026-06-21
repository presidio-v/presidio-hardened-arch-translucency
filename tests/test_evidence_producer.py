"""Conformance + behaviour tests for the SLO evidence producer.

The golden vectors below are the family ``evidence-ref@1`` wire-format pins —
byte-identical to the vectors in presidio-hardened-x402 (mica.py) and
presidio-evidence. If these drift, the x402 SLO broker can no longer verify our
degradation triggers.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from presidio_arch_translucency.evidence_producer import (
    DEFAULT_SIGNER,
    EvidenceProducerError,
    build_slo_evidence,
    canonical_bytes,
    observation_to_evidence,
    sha256_hex,
    sign_evidence,
)
from presidio_arch_translucency.observe import Observation

# Family golden vectors (do not change within evidence-ref@1).
GOLD_PRIV = "01" * 32
GOLD_PUB = "8a88e3dd7409f195fd52db2d3cba5d72ca6709bf1d94121bf3748801b40f6f5c"
GOLD_CH = "abc123def456"
GOLD_SIGNER = "presidio-hardened-ai"
GOLD_SIG = (
    "a0dc8599958734457f194ebce15c60ec097754b59897ab5dc758f73abadafe36"
    "97874049d9f7736de4e3a9cc28b2fb4d76b15d8bce7fa0b26c8434bebbba590a"
)
GOLD_HMAC_KEY = "shared-key"  # noqa: S105 - public golden-vector key, not a secret
GOLD_HMAC_SIG = "2e7af6d2882dd53847dcf3032e1fe36e58c5a879c224ea97b505b3e3b626b87a"


def test_canonical_matches_family_and_rejects_floats():
    assert canonical_bytes({"b": "2", "a": "1"}) == b'{"a":"1","b":"2"}'
    with pytest.raises(EvidenceProducerError):
        canonical_bytes({"latency": 1.5})


def test_golden_vector_ed25519():
    pytest.importorskip("cryptography")
    assert (
        sign_evidence(GOLD_CH, GOLD_SIGNER, algorithm="ed25519", key=GOLD_PRIV)
        == GOLD_SIG
    )


def test_golden_vector_hmac():
    sig = sign_evidence(
        GOLD_CH, GOLD_SIGNER, algorithm="hmac-sha256", key=GOLD_HMAC_KEY
    )
    assert sig == GOLD_HMAC_SIG


def test_signing_fails_closed():
    with pytest.raises(EvidenceProducerError):
        sign_evidence(GOLD_CH, GOLD_SIGNER, key="")
    with pytest.raises(EvidenceProducerError):
        sign_evidence(GOLD_CH, GOLD_SIGNER, algorithm="rsa", key=GOLD_PRIV)


def test_build_slo_evidence_structure_and_self_verifies():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import ed25519

    env = build_slo_evidence(
        slo="p99_latency_ms",
        value=420,
        threshold=200,
        window="5m",
        private_key_hex=GOLD_PRIV,
        source_version="0.16.0",
    )
    assert env["schema"] == "presidio-hardened/evidence-ref@1"
    ref = env["evidence"][0]
    assert set(ref) == {
        "item_id",
        "source",
        "source_version",
        "ledger_ref",
        "content_hash",
        "signer",
        "signature",
        "claimed_at",
    }
    # content <-> hash binding
    assert ref["content_hash"] == sha256_hex(env["attested_content"])
    # signature verifies under the family verifier (Ed25519 over {content_hash, signer})
    sk = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(GOLD_PRIV))
    message = canonical_bytes(
        {"content_hash": ref["content_hash"], "signer": ref["signer"]}
    )
    sk.public_key().verify(
        bytes.fromhex(ref["signature"]), message
    )  # raises on failure
    assert ref["signer"] == DEFAULT_SIGNER


def test_observation_to_evidence_rounds_p99_to_int():
    env = observation_to_evidence(
        Observation(
            timestamp=datetime.now(timezone.utc),
            rps=480.0,
            avg_latency_ms=80.0,
            p99_latency_ms=420.6,
            throughput=480.0,
            layer="container",
            replicas=4,
        ).validate(),
        slo_target_ms=200,
        private_key_hex=GOLD_HMAC_KEY,
        algorithm="hmac-sha256",
    )
    content = env["attested_content"]
    assert content["value"] == 421 and isinstance(content["value"], int)
    assert content["threshold"] == 200
