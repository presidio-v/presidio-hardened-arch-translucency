"""Atheris property fuzzer for the calibration untrusted-input boundary.

``calibrate.py`` sits on two untrusted-input surfaces and this harness drives
both:

* **CLI text** — :func:`parse_observation` (``rps:latency_ms:replicas``) and
  :func:`parse_energy_observation` (``rps:latency_ms:replicas:watts``) turn
  arbitrary operator-supplied strings into bounded, finite operating points.
  Only :class:`CalibrationError` is a documented failure; anything else escaping
  is a real finding, so exactly that is caught and everything else propagates.
* **On-disk fit record** — the global model file ``~/.pat/model.json`` is
  untrusted JSON. :func:`commitment_of`, :func:`verify_commitment`,
  :func:`training_commitment_of`, :func:`verify_training_commitment` and
  :func:`training_commitment_status` are the fail-closed readers a consumer
  runs over that record; their contract is that *no* record shape makes them
  raise.

Properties asserted:

* parse totality  — for arbitrary unicode only ``CalibrationError`` escapes; on
                    success every field is finite and within the documented
                    bounds, and re-parsing the same string is deterministic;
* record totality — for arbitrary JSON-safe record trees (dict / list / str /
                    int / float / bool / None, str keys, bounded depth, floats
                    included since on-disk records carry them) ``commitment_of``
                    returns ``None`` or 64 lowercase hex, ``verify_commitment``
                    returns a ``bool``, ``training_commitment_status`` returns
                    one of ``{"ok", "tampered", "legacy"}``, and none of the
                    readers raise;
* tamper detection — a genuinely committed record built through the public API
                     verifies ``True``; a single mutation to a committed numeric
                     field or observation row flips ``verify_commitment`` to
                     ``False`` exactly when the re-hashed committed content
                     actually changed (an identity mutation must keep it
                     ``True``), which also pins the result-based and
                     record-based digests to agree.
"""

from __future__ import annotations

import copy
import math
import sys
from dataclasses import replace

import atheris

from presidio_arch_translucency.calibrate import (
    CALIBRATION_COMMITMENT_SCHEMA,
    CALIBRATION_LATENCY_MS_MAX,
    CALIBRATION_REPLICAS_MAX,
    CALIBRATION_RPS_MAX,
    CALIBRATION_WATTS_MAX,
    COMMITMENT_KEY,
    CalibrationError,
    CalibrationResult,
    Observation,
    commitment_digest,
    commitment_of,
    parse_energy_observation,
    parse_observation,
    training_commitment_of,
    training_commitment_status,
    verify_commitment,
    verify_training_commitment,
)

#: Bounded recursion depth for generated record trees.
_MAX_DEPTH = 4

#: The statuses ``training_commitment_status`` is contractually allowed to return.
_STATUSES = frozenset({"ok", "tampered", "legacy"})


def _hex64(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _txt(fdp: atheris.FuzzedDataProvider) -> str:
    return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 24))


def _build_record(fdp: atheris.FuzzedDataProvider, depth: int) -> object:
    """Build one arbitrary JSON-safe record node (floats included, str keys)."""
    # Only scalars are permitted once the depth budget is exhausted.
    max_choice = 6 if depth > 0 else 4
    choice = fdp.ConsumeIntInRange(0, max_choice)
    if choice == 0:
        return _txt(fdp)
    if choice == 1:
        return fdp.ConsumeInt(8)
    if choice == 2:
        # Floats (including inf/nan) belong in the domain: persisted records
        # store bare floats for concurrency/beta/rmse and observation rows.
        return fdp.ConsumeFloat()
    if choice == 3:
        return fdp.ConsumeBool()
    if choice == 4:
        return None
    if choice == 5:
        count = fdp.ConsumeIntInRange(0, 4)
        return [_build_record(fdp, depth - 1) for _ in range(count)]
    return {
        _txt(fdp): _build_record(fdp, depth - 1)
        for _ in range(fdp.ConsumeIntInRange(0, 4))
    }


def _assert_parse(point: object, watts: bool) -> None:
    """A parsed point must be finite and within the documented bounds."""
    if not (0 < point.rps <= CALIBRATION_RPS_MAX and math.isfinite(point.rps)):
        raise AssertionError(f"parsed rps out of bounds: {point.rps!r}")
    if not (
        0 < point.latency_ms <= CALIBRATION_LATENCY_MS_MAX
        and math.isfinite(point.latency_ms)
    ):
        raise AssertionError(f"parsed latency_ms out of bounds: {point.latency_ms!r}")
    if not 0 < point.replicas <= CALIBRATION_REPLICAS_MAX:
        raise AssertionError(f"parsed replicas out of bounds: {point.replicas!r}")
    if watts and not (
        0 < point.watts <= CALIBRATION_WATTS_MAX and math.isfinite(point.watts)
    ):
        raise AssertionError(f"parsed watts out of bounds: {point.watts!r}")


def _record_from_result(result: CalibrationResult, digest: str) -> dict:
    """Mirror ``_fit_record``'s persisted throughput-only shape (v0.19 record)."""
    return {
        "concurrency": result.concurrency,
        "overhead_beta": result.overhead_beta,
        "r_squared": result.r_squared,
        "rmse": result.rmse,
        "observations": [
            [o.rps, o.latency_ms, o.replicas] for o in result.observations
        ],
        COMMITMENT_KEY: {
            "schema": CALIBRATION_COMMITMENT_SCHEMA,
            "digest": digest,
        },
    }


def _mutate(
    fdp: atheris.FuzzedDataProvider,
    result: CalibrationResult,
    record: dict,
) -> CalibrationResult:
    """Apply one identical mutation to *record* and a copy of *result*.

    The committed content is a function of the numeric fields alone, so mutating
    both in lockstep lets the harness compute the expected ``verify_commitment``
    outcome from the public :func:`commitment_digest` without touching any
    private helper.
    """
    kind = fdp.ConsumeIntInRange(0, 4)
    if kind < 4:
        name = ("concurrency", "overhead_beta", "r_squared", "rmse")[kind]
        value = fdp.ConsumeFloat()
        record[name] = value
        return replace(result, **{name: value})

    observations = list(result.observations)
    index = fdp.ConsumeIntInRange(0, len(observations) - 1)
    column = fdp.ConsumeIntInRange(0, 2)
    point = observations[index]
    if column == 2:
        value = fdp.ConsumeIntInRange(0, CALIBRATION_REPLICAS_MAX)
        record["observations"][index][2] = value
        observations[index] = replace(point, replicas=value)
    else:
        value = fdp.ConsumeFloat()
        record["observations"][index][column] = value
        field = "rps" if column == 0 else "latency_ms"
        observations[index] = replace(point, **{field: value})
    return replace(result, observations=observations)


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)

    # --- parse totality: only CalibrationError escapes; bounds + determinism ---
    throughput_text = _txt(fdp)
    try:
        observation = parse_observation(throughput_text)
    except CalibrationError:
        observation = None
    if observation is not None:
        _assert_parse(observation, watts=False)
        if parse_observation(throughput_text) != observation:
            raise AssertionError(
                f"parse_observation not deterministic: {throughput_text!r}"
            )

    energy_text = _txt(fdp)
    try:
        energy = parse_energy_observation(energy_text)
    except CalibrationError:
        energy = None
    if energy is not None:
        _assert_parse(energy, watts=True)
        if parse_energy_observation(energy_text) != energy:
            raise AssertionError(
                f"parse_energy_observation not deterministic: {energy_text!r}"
            )

    # --- record totality: the fail-closed readers never raise, whatever tree ---
    tree = _build_record(fdp, _MAX_DEPTH)
    digest = commitment_of(tree)
    if digest is not None and not _hex64(digest):
        raise AssertionError(f"commitment_of returned non-hex digest: {digest!r}")
    if not isinstance(verify_commitment(tree), bool):
        raise AssertionError("verify_commitment did not return a bool")
    training_digest = training_commitment_of(tree)
    if training_digest is not None and not _hex64(training_digest):
        raise AssertionError(
            f"training_commitment_of returned non-hex digest: {training_digest!r}"
        )
    if not isinstance(verify_training_commitment(tree), bool):
        raise AssertionError("verify_training_commitment did not return a bool")
    status = training_commitment_status(tree)
    if status not in _STATUSES:
        raise AssertionError(f"training_commitment_status out of contract: {status!r}")

    # --- tamper detection: a genuine committed record, then one mutation ---
    observations = [
        Observation(
            rps=fdp.ConsumeFloatInRange(1e-6, CALIBRATION_RPS_MAX),
            latency_ms=fdp.ConsumeFloatInRange(1e-6, CALIBRATION_LATENCY_MS_MAX),
            replicas=fdp.ConsumeIntInRange(1, CALIBRATION_REPLICAS_MAX),
        )
        for _ in range(fdp.ConsumeIntInRange(1, 4))
    ]
    result = CalibrationResult(
        concurrency=fdp.ConsumeFloatInRange(0.1, 1000.0),
        overhead_beta=fdp.ConsumeFloatInRange(0.0, 0.45),
        r_squared=fdp.ConsumeFloatInRange(-1.0, 1.0),
        rmse=fdp.ConsumeFloatInRange(0.0, 1e6),
        observations=observations,
        predictions=[],
        residuals=[],
    )
    stored = commitment_digest(result)
    record = _record_from_result(result, stored)
    if not verify_commitment(record):
        raise AssertionError(f"genuine committed record failed to verify: {record!r}")

    mutated_record = copy.deepcopy(record)
    mutated_result = _mutate(fdp, result, mutated_record)
    # The record re-hash and the result-based digest build identical canonical
    # content, so verify_commitment must be True iff the content is unchanged.
    expected = commitment_digest(mutated_result) == stored
    if verify_commitment(mutated_record) != expected:
        raise AssertionError(
            f"verify_commitment disagreed with committed-content change: "
            f"expected {expected!r} for {mutated_record!r}"
        )


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
