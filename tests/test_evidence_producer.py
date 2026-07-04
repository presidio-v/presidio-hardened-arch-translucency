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
    LAYER0_SCHEMA_ID,
    EvidenceProducerError,
    build_layer0_reading,
    build_slo_evidence,
    canonical_bytes,
    is_degraded,
    observation_to_evidence,
    observation_to_layer0,
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

# Family slo-reading vector (presidio-evidence vectors/slo-reading/, appended
# 2026-07-03, v0.2.1 consumer-coverage arc; L-EV-7 re-pin). Pins the full
# degradation chain this producer feeds: content hash AND deterministic
# Ed25519 signature of the envelope the x402 SLO payment broker verifies.
SLO_VECTOR_CONTENT_HASH = (
    "cc0d5b97d442aa7afd1b9e33aabb952c5389381fea954256855fece2678580c0"
)
SLO_VECTOR_SIGNATURE = (
    "486280c962830a8291b72caa67e0c9409849dbc66fc4f3c10b60de3aea9a3ca4"
    "b347a234f6dc70dd230c6612fc437468c5cfed516f19fdd26a1c2472b308b709"
)


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


@pytest.mark.parametrize(
    "bad_key", ["AA" * 32, f"{GOLD_PRIV[:2]} {GOLD_PRIV[2:]}", "01"]
)
def test_ed25519_key_format_is_strict_lowercase_seed_hex(bad_key):
    pytest.importorskip("cryptography")
    with pytest.raises(EvidenceProducerError, match="64 lowercase hex"):
        sign_evidence(GOLD_CH, GOLD_SIGNER, algorithm="ed25519", key=bad_key)


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


def test_build_slo_evidence_matches_family_slo_reading_vector():
    """L-EV-7: byte-identity with presidio-evidence vectors/slo-reading/.

    Not merely self-consistency — the family vector's content hash and its
    deterministic Ed25519 signature must be reproduced exactly. If this
    producer ever drifts from the family canonical profile, this test breaks
    before the x402 consumer does.
    """
    pytest.importorskip("cryptography")
    env = build_slo_evidence(
        slo="p99_latency_ms",
        value=420,
        threshold=200,
        window="5m",
        private_key_hex=GOLD_PRIV,
        source_version="test",
        observed_at="2026-07-02T00:00:00+00:00",
    )
    ref = env["evidence"][0]
    assert ref["content_hash"] == SLO_VECTOR_CONTENT_HASH
    assert ref["signature"] == SLO_VECTOR_SIGNATURE
    assert ref["item_id"] == "SLO-DEGRADED"
    assert ref["ledger_ref"] == "arch-translucency:obs"
    assert ref["signer"] == DEFAULT_SIGNER
    # The Layer-0 reading binds the same content and hash (key-less path).
    reading = build_layer0_reading(
        slo="p99_latency_ms",
        value=420,
        threshold=200,
        window="5m",
        source_version="test",
        observed_at="2026-07-02T00:00:00+00:00",
    )
    assert reading["content_hash"] == SLO_VECTOR_CONTENT_HASH
    assert reading["schema"] == "presidio-hardened/slo-reading@1"


def test_layer0_reading_is_unsigned_and_hash_bound():
    reading = build_layer0_reading(
        slo="p99_latency_ms", value=420, threshold=200, window="5m"
    )
    assert reading["schema"] == LAYER0_SCHEMA_ID
    assert "evidence" not in reading  # key-less: no signature
    assert reading["content_hash"] == sha256_hex(reading["attested_content"])
    assert reading["source"] == DEFAULT_SIGNER


def test_is_degraded():
    assert is_degraded(
        build_layer0_reading(slo="x", value=420, threshold=200, window="5m")
    )
    assert not is_degraded(
        build_layer0_reading(slo="x", value=100, threshold=200, window="5m")
    )


def test_observation_to_layer0_rounds_and_is_unsigned():
    reading = observation_to_layer0(
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
    )
    assert reading["attested_content"]["value"] == 421
    assert "evidence" not in reading


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
