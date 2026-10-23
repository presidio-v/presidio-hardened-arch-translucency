"""Gate-certificate parents on train-/energy-evidence-emit (issue #120).

Certificates are issued here by ikigov-assess itself (``build_certificate`` +
``sign``, the ``[gate]`` extra) with the family Ed25519 test key, so the
verifier under test is the real one. Coverage:

- identity: the family evidence-contract vector hashes to its pinned value
  (ADR-0006) and equals sha256 over ikigov's own ``signing_bytes``;
- accept: a valid OPEN certificate's identity is appended to ``parents`` after
  the bare ``--parent`` values, once (dedupe, order-preserving);
- refuse, nothing emitted: tampered, expired, untrusted issuer, wrong key,
  a decision other than OPEN, missing ``--gate-trust``, ``--gate-trust``
  alone, unreadable / oversized / non-JSON files, the extra not installed;
- unchanged: without the option, output hashes are pinned to the pre-0.25
  values (bare duplicates are still kept as given).
"""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from presidio_arch_translucency.cli import app
from presidio_arch_translucency.evidence_producer import sha256_hex
from presidio_arch_translucency.gate_certificate import (
    MAX_FILE_BYTES,
    GateCertificateError,
    certificate_identity,
    merge_parents,
    verify_gate_certificates,
)

pytest.importorskip("cryptography")
ikigov_cert = pytest.importorskip("presidio_ikigov_assess.certificate")

runner = CliRunner()

VECTOR = json.loads(
    (
        Path(__file__).parent / "fixtures" / "gate_certificate_identity_vector.json"
    ).read_text(encoding="utf-8")
)
VECTOR_IDENTITY = "dbb7c6a36f0aea55bbe5dead3daf8f4f3d59116966e79bd4b78ecfe2a83ebd4a"

ISSUER = "presidio-hardened-ikigov-assess"
SEED = "01" * 32  # family test key (private seed)
PUBLIC_KEY = "8a88e3dd7409f195fd52db2d3cba5d72ca6709bf1d94121bf3748801b40f6f5c"
OTHER_SEED = "02" * 32
G0_ITEMS = frozenset({"S1", "S2", "S3"})

TRAIN_BASE = [
    "--skip-audit",
    "train-evidence-emit",
    "--run-id",
    "run-pin-1",
    "--strategy",
    "fsdp",
    "--degree",
    "4",
    "-s",
    "120",
    "--duration-s",
    "3600",
    "-n",
    "4",
]


def _utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _issue(
    *,
    affirmed: frozenset[str] = G0_ITEMS,
    assessed_at: datetime | None = None,
    valid_days: int = 30,
    seed: str = SEED,
    use_case: str = "training-gate",
) -> dict:
    issued = assessed_at or datetime.now(UTC) - timedelta(days=1)
    doc = ikigov_cert.build_certificate(
        use_case=use_case,
        gate="G0",
        risk_class="medium",
        affirmed=affirmed,
        skipped=G0_ITEMS - affirmed,
        issuer=ISSUER,
        assessed_at=_utc(issued),
        not_after=_utc(issued + timedelta(days=valid_days)),
    )
    return ikigov_cert.sign(doc, alg="ed25519", key_hex_or_secret=seed, signer=ISSUER)


def _write(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def trust(tmp_path: Path) -> Path:
    return _write(
        tmp_path, "trust.json", {ISSUER: {"alg": "ed25519", "public_key": PUBLIC_KEY}}
    )


def _json_out(result) -> dict:  # noqa: ANN001
    return json.loads(result.output.strip().splitlines()[-1])


def _train(*extra: str):
    return runner.invoke(app, [*TRAIN_BASE, *extra])


# ── identity (ADR-0006) ──────────────────────────────────────────────────────


def test_vector_identity() -> None:
    assert VECTOR["identity"] == VECTOR_IDENTITY
    assert certificate_identity(VECTOR["certificate"]) == VECTOR_IDENTITY


def test_identity_matches_ikigov_signing_bytes() -> None:
    cert = VECTOR["certificate"]
    expected = hashlib.sha256(ikigov_cert.signing_bytes(cert)).hexdigest()
    assert certificate_identity(cert) == expected
    fresh = _issue()
    assert (
        certificate_identity(fresh)
        == hashlib.sha256(ikigov_cert.signing_bytes(fresh)).hexdigest()
    )


def test_identity_ignores_signature() -> None:
    cert = copy.deepcopy(VECTOR["certificate"])
    cert["signature"] = {"alg": "hmac-sha256", "signer": "x", "sig": "00"}
    assert certificate_identity(cert) == VECTOR_IDENTITY
    del cert["signature"]
    assert certificate_identity(cert) == VECTOR_IDENTITY


@pytest.mark.parametrize(
    "bad",
    [None, [], "x", {"schema": "presidio-hardened/training-run@1"}, {"no": "schema"}],
)
def test_identity_fails_closed(bad: object) -> None:
    with pytest.raises(GateCertificateError):
        certificate_identity(bad)


def test_identity_rejects_floats() -> None:
    cert = dict(VECTOR["certificate"], extra=0.5)
    with pytest.raises(GateCertificateError):
        certificate_identity(cert)


def test_vector_verifies_at_its_instant(tmp_path: Path) -> None:
    cert = _write(tmp_path, "cert.json", VECTOR["certificate"])
    trust = _write(tmp_path, "trust.json", VECTOR["trust_store"])
    at = datetime(2026, 7, 3, tzinfo=UTC)
    assert verify_gate_certificates([cert], trust, now=at) == [VECTOR_IDENTITY]
    with pytest.raises(GateCertificateError, match="expired"):
        verify_gate_certificates([cert], trust, now=datetime(2028, 1, 1, tzinfo=UTC))


# ── merge ────────────────────────────────────────────────────────────────────


def test_merge_parents_order_and_dedupe() -> None:
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    assert merge_parents([a, b, a], [c, b, c]) == [a, b, a, c]
    assert merge_parents([], []) == []


# ── train-evidence-emit ──────────────────────────────────────────────────────


def test_train_without_option_unchanged() -> None:
    # Pinned on v0.24.2 before the option existed.
    assert (
        _json_out(_train())["content_hash"]
        == "6cb7f6646c251f9964fdf582db76a36227777c2f2193038eefa3dedeaa7b6b16"
    )
    one = _json_out(_train("--parent", "a" * 64))
    assert (
        one["content_hash"]
        == "3c3da525facdb68411ba9a578327fe43d48fc8e95cbfa581918dd3ec2b375e4d"
    )
    dup = _json_out(
        _train("--parent", "a" * 64, "--parent", "b" * 64, "--parent", "a" * 64)
    )
    assert dup["attested_content"]["parents"] == ["a" * 64, "b" * 64, "a" * 64]
    assert (
        dup["content_hash"]
        == "ae77eb3b14ac172b29c59a1af994cff9c0da4e281e014bad458c1973ad88cc8d"
    )


def test_train_appends_verified_identity(tmp_path: Path, trust: Path) -> None:
    doc = _issue()
    cert = _write(tmp_path, "cert.json", doc)
    identity = certificate_identity(doc)
    result = _train(
        "--parent",
        "a" * 64,
        "--gate-certificate",
        str(cert),
        "--gate-trust",
        str(trust),
    )
    assert result.exit_code == 0, result.output
    reading = _json_out(result)
    assert reading["attested_content"]["parents"] == ["a" * 64, identity]
    assert reading["content_hash"] == sha256_hex(reading["attested_content"])


def test_train_dedupes_identity(tmp_path: Path, trust: Path) -> None:
    doc = _issue()
    cert = _write(tmp_path, "cert.json", doc)
    other_doc = _issue(use_case="second-gate")
    other = _write(tmp_path, "other.json", other_doc)
    identity = certificate_identity(doc)
    result = _train(
        "--parent",
        identity,
        "--gate-certificate",
        str(cert),
        "--gate-certificate",
        str(other),
        "--gate-certificate",
        str(cert),
        "--gate-trust",
        str(trust),
    )
    assert result.exit_code == 0, result.output
    parents = _json_out(result)["attested_content"]["parents"]
    assert parents == [identity, certificate_identity(other_doc)]


def _assert_refused(result, needle: str) -> None:  # noqa: ANN001
    assert result.exit_code == 1, result.output
    assert '"schema"' not in result.output
    assert needle in " ".join(result.output.split())


def test_train_refuses_tampered(tmp_path: Path, trust: Path) -> None:
    doc = _issue()
    doc["use_case"] = "something-else"
    cert = _write(tmp_path, "cert.json", doc)
    _assert_refused(
        _train("--gate-certificate", str(cert), "--gate-trust", str(trust)),
        "bad-signature",
    )


def test_train_refuses_tampered_decision(tmp_path: Path, trust: Path) -> None:
    doc = _issue(affirmed=frozenset({"S1"}))
    doc["decision"] = "OPEN"
    resigned = ikigov_cert.sign(
        {k: v for k, v in doc.items() if k != "signature"},
        alg="ed25519",
        key_hex_or_secret=SEED,
        signer=ISSUER,
    )
    cert = _write(tmp_path, "cert.json", resigned)
    _assert_refused(
        _train("--gate-certificate", str(cert), "--gate-trust", str(trust)),
        "decision-mismatch",
    )


def test_train_refuses_expired(tmp_path: Path, trust: Path) -> None:
    doc = _issue(assessed_at=datetime.now(UTC) - timedelta(days=40), valid_days=30)
    cert = _write(tmp_path, "cert.json", doc)
    _assert_refused(
        _train("--gate-certificate", str(cert), "--gate-trust", str(trust)), "expired"
    )


def test_train_refuses_untrusted_issuer(tmp_path: Path) -> None:
    cert = _write(tmp_path, "cert.json", _issue())
    trust = _write(
        tmp_path,
        "trust.json",
        {"someone-else": {"alg": "ed25519", "public_key": PUBLIC_KEY}},
    )
    _assert_refused(
        _train("--gate-certificate", str(cert), "--gate-trust", str(trust)),
        "unknown-issuer",
    )


def test_train_refuses_wrong_key(tmp_path: Path, trust: Path) -> None:
    cert = _write(tmp_path, "cert.json", _issue(seed=OTHER_SEED))
    _assert_refused(
        _train("--gate-certificate", str(cert), "--gate-trust", str(trust)),
        "bad-signature",
    )


def test_train_refuses_gate_not_open(tmp_path: Path, trust: Path) -> None:
    doc = _issue(affirmed=frozenset({"S1"}))
    assert doc["decision"] != "OPEN"
    cert = _write(tmp_path, "cert.json", doc)
    _assert_refused(
        _train("--gate-certificate", str(cert), "--gate-trust", str(trust)), "not OPEN"
    )


def test_train_requires_trust(tmp_path: Path) -> None:
    cert = _write(tmp_path, "cert.json", _issue())
    _assert_refused(_train("--gate-certificate", str(cert)), "requires")


def test_train_trust_without_certificate(trust: Path) -> None:
    _assert_refused(_train("--gate-trust", str(trust)), "without")


def test_train_refuses_bad_files(tmp_path: Path, trust: Path) -> None:
    missing = tmp_path / "missing.json"
    _assert_refused(
        _train("--gate-certificate", str(missing), "--gate-trust", str(trust)),
        "cannot read",
    )
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{not json", encoding="utf-8")
    _assert_refused(
        _train("--gate-certificate", str(garbage), "--gate-trust", str(trust)),
        "not valid JSON",
    )
    _assert_refused(
        _train("--gate-certificate", str(tmp_path), "--gate-trust", str(trust)),
        "not a regular file",
    )
    big = tmp_path / "big.json"
    big.write_bytes(b" " * (MAX_FILE_BYTES + 1))
    _assert_refused(
        _train("--gate-certificate", str(big), "--gate-trust", str(trust)), "exceeds"
    )
    cert = _write(tmp_path, "cert.json", _issue())
    bad_trust = tmp_path / "bad-trust.json"
    bad_trust.write_text("[]", encoding="utf-8")
    _assert_refused(
        _train("--gate-certificate", str(cert), "--gate-trust", str(bad_trust)),
        "gate trust store",
    )


def test_train_refuses_without_extra(tmp_path: Path, trust: Path) -> None:
    cert = _write(tmp_path, "cert.json", _issue())
    with patch.dict(sys.modules, {"presidio_ikigov_assess": None}):
        result = _train("--gate-certificate", str(cert), "--gate-trust", str(trust))
    _assert_refused(result, "[gate] extra")


def test_train_logs_refusal_without_paths(tmp_path: Path, trust: Path) -> None:
    doc = _issue()
    doc["use_case"] = "tampered"
    cert = _write(tmp_path, "cert.json", doc)
    with patch("presidio_arch_translucency.cli.log_security_event") as log:
        result = _train("--gate-certificate", str(cert), "--gate-trust", str(trust))
    assert result.exit_code == 1
    event, payload = log.call_args[0]
    assert event == "GATE_CERTIFICATE_REFUSED"
    assert payload == {"certificates": 1}


# ── energy-evidence-emit ─────────────────────────────────────────────────────


def _seed_energy(db: Path) -> None:
    from presidio_arch_translucency.observe import (  # noqa: PLC0415
        EnergyObservation,
        _record_energy_observation_unchecked,
    )

    first = datetime.now(UTC) - timedelta(seconds=120)
    for i in range(2):
        _record_energy_observation_unchecked(
            EnergyObservation(
                timestamp=first + timedelta(seconds=i * 10),
                watts=120.0,
                joules=1200.0,
                window_s=10.0,
                rps=500.0,
                layer="node",
                replicas=4,
                meter="rapl",
                source="prometheus",
            ),
            db_path=db,
        )


def _energy(db: Path, *extra: str):
    return runner.invoke(
        app,
        [
            "--skip-audit",
            "energy-evidence-emit",
            "--window-minutes",
            "60",
            "--db",
            str(db),
            *extra,
        ],
    )


def test_energy_appends_verified_identity(tmp_path: Path, trust: Path) -> None:
    db = tmp_path / "obs.db"
    _seed_energy(db)
    doc = _issue()
    cert = _write(tmp_path, "cert.json", doc)
    result = _energy(db, "--gate-certificate", str(cert), "--gate-trust", str(trust))
    assert result.exit_code == 0, result.output
    content = _json_out(result)["attested_content"]
    assert content["parents"] == [certificate_identity(doc)]


def test_energy_without_option_has_no_parents(tmp_path: Path) -> None:
    db = tmp_path / "obs.db"
    _seed_energy(db)
    result = _energy(db)
    assert result.exit_code == 0, result.output
    assert "parents" not in _json_out(result)["attested_content"]


def test_energy_refuses_bad_certificate(tmp_path: Path, trust: Path) -> None:
    db = tmp_path / "obs.db"
    _seed_energy(db)
    doc = _issue(assessed_at=datetime.now(UTC) - timedelta(days=40), valid_days=30)
    cert = _write(tmp_path, "cert.json", doc)
    _assert_refused(
        _energy(db, "--gate-certificate", str(cert), "--gate-trust", str(trust)),
        "expired",
    )
