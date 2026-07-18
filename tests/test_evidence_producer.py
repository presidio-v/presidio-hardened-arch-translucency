"""Conformance + behaviour tests for the SLO evidence producer.

The golden vectors below are the family ``evidence-ref@1`` wire-format pins —
byte-identical to the vectors in presidio-hardened-x402 (mica.py) and
presidio-evidence. If these drift, downstream family consumers can no longer
verify our degradation readings.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from presidio_arch_translucency.evidence_producer import (
    _EMIT_METERS,
    _SERVING_LAYERS,
    _TRAINING_STRATEGIES,
    DEFAULT_SIGNER,
    ENERGY_READING_SCHEMA_ID,
    LAYER0_SCHEMA_ID,
    EvidenceProducerError,
    build_energy_reading,
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
# Ed25519 signature of the envelope downstream family consumers verify.
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


# ---------------------------------------------------------------------------
# energy-reading@1 (v0.24.0) — measured-energy anchoring reading, Layer 0,
# key-less. Carries the energy-chain head hash (ADR-0010 anchoring discharged).
# ---------------------------------------------------------------------------

# Deterministic anchoring head for the self-pins (sha256 of a fixed byte string).
_SELFPIN_HEAD = hashlib.sha256(b"pat-energy-arc-selfpin").hexdigest()
# The training-run family vector hash (pinned in test_training.py) doubles as a
# valid upstream parent content hash for the parents-bearing self-pin.
_TRAINING_RUN_VECTOR_HASH = (
    "91733915b4797d71bfc42422dcfff105b512f613c4d6ad3f1013463d1853b378"
)


def _energy_kwargs(**extra):
    base = dict(
        window_start="2026-07-10T00:00:00+00:00",
        window_end="2026-07-10T00:05:00+00:00",
        energy_wh="12.5",
        mean_power_w="150.0",
        meter="rapl",
        layer="container",
        energy_chain_head=_SELFPIN_HEAD,
        source_version="test",
        observed_at="2026-07-10T00:05:00+00:00",
    )
    base.update(extra)
    return base


def test_energy_reading_shape_and_hash():
    reading = build_energy_reading(**_energy_kwargs())
    assert reading["schema"] == ENERGY_READING_SCHEMA_ID
    assert set(reading) == {
        "schema",
        "attested_content",
        "content_hash",
        "source",
        "source_version",
        "generated_at",
    }
    content = reading["attested_content"]
    assert content["window_start"] == "2026-07-10T00:00:00+00:00"
    assert content["window_end"] == "2026-07-10T00:05:00+00:00"
    assert content["meter"] == "rapl"
    assert content["layer"] == "container"
    assert content["energy_chain_head"] == _SELFPIN_HEAD
    assert "strategy" not in content
    assert "parents" not in content  # omitted when empty
    assert reading["content_hash"] == sha256_hex(content)


# Self-pin (D): pat's own rapl-emitting canonical form. The merged family
# vectors (presidio-evidence PR #14) are pinned SEPARATELY, not by editing these:
# the emittable DCGM/parents vector in
# test_energy_reading_matches_merged_family_parents_vector, and the nominal
# KEPLER vector — which pat can no longer emit — byte-for-byte in
# test_energy_reading_matches_merged_family_nominal_vector. These stay meter="rapl",
# the emittable analogue, and guard the string-decimal / field-order profile.
_ENERGY_READING_SELF_PIN_HASH = (
    "8409b62d8511a38c4d9295c290a7fc302517cfc4c041aa36eaa3d5856d2acd40"
)
_ENERGY_READING_SELF_PIN_WITH_PARENT_HASH = (
    "3eff5cb511929e054d52dec07d45d14367fd6b0fac4b3e354462e7e6a3a76494"
)


def test_energy_reading_self_pin_no_parents():
    reading = build_energy_reading(**_energy_kwargs())
    assert reading["content_hash"] == _ENERGY_READING_SELF_PIN_HASH
    assert reading["content_hash"] == sha256_hex(reading["attested_content"])


def test_energy_reading_self_pin_with_parent():
    reading = build_energy_reading(
        **_energy_kwargs(parents=[_TRAINING_RUN_VECTOR_HASH])
    )
    content = reading["attested_content"]
    assert content["parents"] == [_TRAINING_RUN_VECTOR_HASH]
    assert reading["content_hash"] == _ENERGY_READING_SELF_PIN_WITH_PARENT_HASH
    # A non-empty parents list must change the hash vs the no-parents self-pin.
    assert reading["content_hash"] != _ENERGY_READING_SELF_PIN_HASH


def test_energy_reading_matches_merged_family_parents_vector():
    """Pin the emittable PR #14 DCGM/training vector byte-for-byte."""
    reading = build_energy_reading(
        window_start="2026-07-02T00:00:00+00:00",
        window_end="2026-07-02T02:00:00+00:00",
        energy_wh="4800.0",
        mean_power_w="2400.0",
        meter="dcgm",
        strategy="pipeline",
        energy_chain_head=(
            "6cf76f69d0d27b89d1207a1ee4c20c254a37640886c21835762b045693c48e44"
        ),
        parents=[_TRAINING_RUN_VECTOR_HASH],
        source_version="test",
        observed_at="2026-07-02T00:00:00+00:00",
    )
    assert reading["content_hash"] == (
        "391083ae2426dc1dc0616a77fb9760d3ddca2c976ee16dd600ade78c9246a3d1"
    )


# The nominal PR #14 family vector uses meter="kepler", which pat deliberately
# cannot emit (v0.21 audit — refused at BOTH build_energy_reading._EMIT_METERS
# and observe.VALID_METERS). Its cross-repo conformance is therefore pinned
# byte-for-byte against the exact family signed_content, NOT routed through
# build_energy_reading (which correctly rejects it). Loosening the builder to
# accept kepler would reverse an audited invariant; pinning the canonical bytes
# here gives the family-vector guarantee (L-EV-7) without that cost.
# Source: presidio-evidence vectors/energy-reading/valid-envelope.json.
_FAMILY_ENERGY_READING_NOMINAL_CONTENT = {
    "window_start": "2026-07-02T00:00:00+00:00",
    "window_end": "2026-07-02T00:05:00+00:00",
    "energy_wh": "12.5",
    "mean_power_w": "150.0",
    "meter": "kepler",
    "layer": "pod",
    "energy_chain_head": (
        "6cf76f69d0d27b89d1207a1ee4c20c254a37640886c21835762b045693c48e44"
    ),
}
_FAMILY_ENERGY_READING_NOMINAL_HASH = (
    "3950f28a608aea47e356e8096099042d8e3e2de73afaedf61ec547e167af0252"
)


def test_energy_reading_matches_merged_family_nominal_vector():
    """Pin the nominal PR #14 KEPLER family vector byte-for-byte, without the builder.

    pat cannot construct this reading via :func:`build_energy_reading` — ``meter``
    ``"kepler"`` is refused (v0.21 audit, enforced at the builder and
    ``observe.VALID_METERS``) — so conformance to the family bytes is asserted
    directly on the canonical hash of the exact ``signed_content`` rather than by
    re-emitting it. Cross-repo source: presidio-evidence
    ``vectors/energy-reading/valid-envelope.json``.
    """
    assert (
        sha256_hex(_FAMILY_ENERGY_READING_NOMINAL_CONTENT)
        == _FAMILY_ENERGY_READING_NOMINAL_HASH
    )
    # The builder genuinely refuses this shape — the reason the pin bypasses it.
    # If a future change makes build_energy_reading accept kepler, this guard
    # fails and forces a deliberate revisit of the audited emit restriction.
    with pytest.raises(EvidenceProducerError, match="kepler"):
        build_energy_reading(**_FAMILY_ENERGY_READING_NOMINAL_CONTENT)


def test_energy_reading_content_hash_stable_across_calls():
    a = build_energy_reading(**_energy_kwargs())
    b = build_energy_reading(**_energy_kwargs())
    assert a["content_hash"] == b["content_hash"]


# ── window validation classes ────────────────────────────────────────────────


def test_energy_reading_rejects_naive_window():
    with pytest.raises(EvidenceProducerError, match="strict RFC3339 UTC"):
        build_energy_reading(**_energy_kwargs(window_start="2026-07-10T00:00:00"))


def test_energy_reading_rejects_non_utc_offset():
    with pytest.raises(EvidenceProducerError, match="strict RFC3339 UTC"):
        build_energy_reading(**_energy_kwargs(window_start="2026-07-10T02:00:00+02:00"))


def test_energy_reading_accepts_z_suffix():
    reading = build_energy_reading(
        **_energy_kwargs(
            window_start="2026-07-10T00:00:00Z",
            window_end="2026-07-10T00:05:00Z",
        )
    )
    # The Z spelling is preserved verbatim on the wire (caller's exact string).
    assert reading["attested_content"]["window_start"] == "2026-07-10T00:00:00Z"


def test_energy_reading_rejects_start_equal_end():
    with pytest.raises(EvidenceProducerError, match="strictly before"):
        build_energy_reading(**_energy_kwargs(window_end="2026-07-10T00:00:00+00:00"))


def test_energy_reading_rejects_start_after_end():
    with pytest.raises(EvidenceProducerError, match="strictly before"):
        build_energy_reading(
            **_energy_kwargs(
                window_start="2026-07-10T00:05:00+00:00",
                window_end="2026-07-10T00:00:00+00:00",
            )
        )


def test_energy_reading_rejects_garbage_window():
    with pytest.raises(EvidenceProducerError, match="strict RFC3339 UTC"):
        build_energy_reading(**_energy_kwargs(window_start="not-a-timestamp"))


def test_energy_reading_rejects_control_char_window():
    with pytest.raises(EvidenceProducerError, match="control characters"):
        build_energy_reading(
            **_energy_kwargs(window_start="2026-07-10T00:00:00+00:00\n")
        )


def test_energy_reading_rejects_overlong_window():
    with pytest.raises(EvidenceProducerError, match="no longer than"):
        build_energy_reading(**_energy_kwargs(window_start="9" * 65))


def test_energy_reading_rejects_non_string_window():
    with pytest.raises(EvidenceProducerError, match="RFC3339 UTC instant"):
        build_energy_reading(**_energy_kwargs(window_start=20260710))


# ── energy-value coercion reuse (spot-check the shared coercer) ───────────────


def test_energy_reading_rejects_float_energy():
    with pytest.raises(EvidenceProducerError, match="not a float"):
        build_energy_reading(**_energy_kwargs(energy_wh=12.5))


def test_energy_reading_rejects_negative_power():
    with pytest.raises(EvidenceProducerError, match=">= 0"):
        build_energy_reading(**_energy_kwargs(mean_power_w="-5"))


def test_energy_reading_int_energy_canonicalized_to_string():
    reading = build_energy_reading(**_energy_kwargs(energy_wh=13, mean_power_w=156))
    content = reading["attested_content"]
    assert content["energy_wh"] == "13.0"
    assert content["mean_power_w"] == "156.0"


def test_energy_reading_rejects_cross_field_contradiction():
    with pytest.raises(EvidenceProducerError, match="contradict the signed window"):
        build_energy_reading(**_energy_kwargs(energy_wh="13.0", mean_power_w="150.0"))


@pytest.mark.parametrize(
    "instant",
    [
        "2026-07-10 00:00:00+00:00",  # space instead of RFC3339 T
        "20260710T000000+00:00",  # ISO basic form is not RFC3339
        "2026-07-10T00:00:00z",  # lowercase z is not RFC3339 UTC syntax
        "2026-07-10T00:00:00+00:00:00",  # offset seconds are not RFC3339
    ],
)
def test_energy_reading_rejects_non_rfc3339_iso_variants(instant):
    with pytest.raises(EvidenceProducerError, match="strict RFC3339 UTC"):
        build_energy_reading(**_energy_kwargs(window_start=instant))


# ── meter enum (pat emits only its own measured meters) ──────────────────────


@pytest.mark.parametrize("meter", ["rapl", "dcgm"])
def test_energy_reading_accepts_emittable_meters(meter):
    reading = build_energy_reading(**_energy_kwargs(meter=meter))
    assert reading["attested_content"]["meter"] == meter


def test_energy_reading_rejects_kepler_meter():
    # The family schema allows "kepler"; pat refuses to EMIT it (v0.21 audit).
    with pytest.raises(EvidenceProducerError, match="kepler"):
        build_energy_reading(**_energy_kwargs(meter="kepler"))


def test_energy_reading_rejects_garbage_meter():
    with pytest.raises(EvidenceProducerError, match="meter must be one of"):
        build_energy_reading(**_energy_kwargs(meter="wattmeter"))


# ── layer XOR strategy (fail-closed both directions) ─────────────────────────


def test_energy_reading_rejects_both_layer_and_strategy():
    with pytest.raises(EvidenceProducerError, match="exactly one of"):
        build_energy_reading(**_energy_kwargs(strategy="data"))


def test_energy_reading_rejects_neither_layer_nor_strategy():
    kwargs = _energy_kwargs()
    kwargs.pop("layer")
    with pytest.raises(EvidenceProducerError, match="exactly one of"):
        build_energy_reading(**kwargs)


def test_energy_reading_strategy_mode_emits_strategy_key():
    kwargs = _energy_kwargs(strategy="fsdp")
    kwargs.pop("layer")
    reading = build_energy_reading(**kwargs)
    content = reading["attested_content"]
    assert content["strategy"] == "fsdp"
    assert "layer" not in content


def test_energy_reading_rejects_invalid_strategy():
    kwargs = _energy_kwargs(strategy="quantum")
    kwargs.pop("layer")
    with pytest.raises(EvidenceProducerError, match="strategy must be one of"):
        build_energy_reading(**kwargs)


def test_energy_reading_rejects_invalid_layer():
    with pytest.raises(EvidenceProducerError, match="layer must be one of"):
        build_energy_reading(**_energy_kwargs(layer="rack"))


# ── chain-head hex discipline ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_head",
    [
        "abc",  # too short
        "e" * 63,  # 63 chars
        "e" * 65,  # 65 chars
        "E" * 64,  # uppercase
        "g" * 64,  # non-hex
        _SELFPIN_HEAD + "0",  # 65
        1234,  # non-string
    ],
)
def test_energy_reading_rejects_bad_chain_head(bad_head):
    with pytest.raises(EvidenceProducerError, match="energy_chain_head"):
        build_energy_reading(**_energy_kwargs(energy_chain_head=bad_head))


# ── parents validation ───────────────────────────────────────────────────────


def test_energy_reading_rejects_malformed_parent():
    with pytest.raises(EvidenceProducerError, match="parent reference"):
        build_energy_reading(**_energy_kwargs(parents=["not-a-hash"]))


def test_energy_reading_empty_parents_omits_key():
    reading = build_energy_reading(**_energy_kwargs(parents=[]))
    assert "parents" not in reading["attested_content"]


# ── cross-check the self-contained allow-lists against their sources ─────────


def test_emit_meters_bound_to_observe_valid_meters():
    from presidio_arch_translucency.observe import VALID_METERS

    assert _EMIT_METERS == VALID_METERS


def test_serving_layers_bound_to_model_valid_layers():
    from presidio_arch_translucency.model import VALID_LAYERS

    assert _SERVING_LAYERS == VALID_LAYERS


def test_energy_strategies_bound_to_training_valid_strategies():
    from presidio_arch_translucency.training import VALID_STRATEGIES

    assert _TRAINING_STRATEGIES == VALID_STRATEGIES


# ── FIX 3: trust-boundary docstring (shape-only; provenance is the CLI's job) ─


def test_build_energy_reading_documents_shape_only_trust_boundary():
    doc = build_energy_reading.__doc__ or ""
    assert "WIRE SHAPE only" in doc
    assert "_derive_energy_reading" in doc
    assert "measured-ness" in doc
