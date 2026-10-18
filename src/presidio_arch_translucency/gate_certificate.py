"""Gate-certificate parents: verify an ikigov-assess certificate, then name it.

``pat train-evidence-emit`` and ``pat energy-evidence-emit`` accept
``--gate-certificate PATH`` (repeatable) with ``--gate-trust PATH``. Each
certificate is verified with ikigov-assess's own verifier before its identity
enters ``parents``, so a run can only name a gate decision that verifies today:

* ``verify_certificate`` checks the schema, the issuer signature against the
  trust store, the predicate identity, the validity bound (``expired``), every
  embedded evidence-ref and the recomputed decision. Any failure refuses.
* the verified decision must be ``OPEN``. A PARTIAL or BLOCKED gate did not
  admit the run, however well it verifies.

The identity is the family evidence-contract identity of a
``gate-certificate@1`` (ADR-0006): SHA-256 over the canonical JSON of the
document minus its top-level ``signature``, i.e. exactly the bytes the issuer
signs. ``framework_content_hash`` names the gate rule, shared by every
certificate under it, and is never used as a parent.

Verifying is this producer's policy (ADR-0006 G4); resolving parents later is
still the consumer's walk (ADR-0002 P4). The verifier comes from the optional
``[gate]`` extra; nothing here is imported unless the option is used.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from presidio_arch_translucency.evidence_producer import (
    EvidenceProducerError,
    canonical_bytes,
)

#: Schema id of an ikigov-assess gate certificate.
GATE_CERTIFICATE_SCHEMA = "presidio-hardened/gate-certificate@1"

#: Upper bound on a certificate or trust-store file. A certificate embeds one
#: evidence-ref per gate item; 1 MiB is far above any real one.
MAX_FILE_BYTES = 1024 * 1024

_REQUIRED_DECISION = "OPEN"
_EXTRA_HINT = (
    "--gate-certificate needs the [gate] extra: "
    "pip install 'presidio-hardened-arch-translucency[gate]'"
)


class GateCertificateError(ValueError):
    """A gate certificate was refused (fail-closed: nothing is emitted)."""


def certificate_identity(document: object) -> str:
    """ADR-0006 identity of a ``gate-certificate@1`` (lowercase hex SHA-256).

    SHA-256 over the canonical JSON of *document* with its top-level
    ``signature`` removed. Fails closed on a non-object, a foreign schema or
    content the canonical profile rejects. Says nothing about validity.
    """
    if not isinstance(document, Mapping):
        raise GateCertificateError("gate certificate must be a JSON object")
    if document.get("schema") != GATE_CERTIFICATE_SCHEMA:
        raise GateCertificateError(f"not a {GATE_CERTIFICATE_SCHEMA} document")
    stripped = {k: v for k, v in document.items() if k != "signature"}
    try:
        message = canonical_bytes(stripped)
    except (EvidenceProducerError, TypeError, ValueError) as exc:
        raise GateCertificateError(f"gate certificate is not canonical: {exc}") from exc
    return hashlib.sha256(message).hexdigest()


def _read_bounded(path: Path, what: str) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise GateCertificateError(
            f"cannot read {what} {path}: {exc.strerror}"
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise GateCertificateError(f"{what} {path} is not a regular file")
    try:
        with path.open("rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise GateCertificateError(
            f"cannot read {what} {path}: {exc.strerror}"
        ) from exc
    if len(raw) > MAX_FILE_BYTES:
        raise GateCertificateError(f"{what} {path} exceeds {MAX_FILE_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateCertificateError(f"{what} {path} is not UTF-8") from exc


def _load_json(path: Path, what: str) -> object:
    text = _read_bounded(path, what)
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise GateCertificateError(f"{what} {path} is not valid JSON") from exc


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-finite number {name}")


def _ikigov():  # noqa: ANN202
    try:
        from presidio_ikigov_assess import (  # noqa: PLC0415
            certificate,
            evidence,
        )
    except ImportError as exc:
        raise GateCertificateError(_EXTRA_HINT) from exc
    return certificate, evidence


def verify_gate_certificates(
    certificate_paths: Sequence[Path],
    trust_path: Path,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Verify each certificate against the trust store; return their identities.

    Order follows *certificate_paths*; a certificate given twice yields its
    identity twice (callers dedupe with :func:`merge_parents`). The first
    refusal raises :class:`GateCertificateError` and nothing is returned.
    ``now`` (timezone-aware) overrides the wall clock for the expiry check.
    """
    if not certificate_paths:
        return []
    cert_mod, evidence_mod = _ikigov()
    trust_text = _read_bounded(trust_path, "gate trust store")
    try:
        trust = evidence_mod.load_trust_store(trust_text)
    except (evidence_mod.EvidenceError, ImportError) as exc:
        raise GateCertificateError(f"gate trust store {trust_path}: {exc}") from exc

    identities: list[str] = []
    for path in certificate_paths:
        document = _load_json(path, "gate certificate")
        identity = certificate_identity(document)
        try:
            result = cert_mod.verify_certificate(document, trust, now=now)
        except (cert_mod.CertificateError, evidence_mod.EvidenceError) as exc:
            raise GateCertificateError(f"gate certificate {path}: {exc}") from exc
        if not result.ok:
            raise GateCertificateError(
                f"gate certificate {path} does not verify: {result.reason}"
            )
        if result.decision_recomputed != _REQUIRED_DECISION:
            raise GateCertificateError(
                f"gate certificate {path} records decision "
                f"{result.decision_recomputed}, not {_REQUIRED_DECISION}; "
                "only an open gate admits a run"
            )
        identities.append(identity)
    return identities


def merge_parents(explicit: Iterable[str], gate_identities: Iterable[str]) -> list[str]:
    """Bare ``--parent`` values unchanged, then each new gate identity once.

    The explicit list is kept exactly as given (so a run without
    ``--gate-certificate`` hashes as before); a gate identity already present,
    explicitly or from an earlier certificate, is not repeated.
    """
    merged = list(explicit)
    seen = set(merged)
    for identity in gate_identities:
        if identity not in seen:
            merged.append(identity)
            seen.add(identity)
    return merged


__all__ = [
    "GATE_CERTIFICATE_SCHEMA",
    "MAX_FILE_BYTES",
    "GateCertificateError",
    "certificate_identity",
    "merge_parents",
    "verify_gate_certificates",
]
