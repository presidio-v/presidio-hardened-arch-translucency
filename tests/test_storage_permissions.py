"""Permission regression tests for local PAT stores."""

from __future__ import annotations

import stat

from presidio_arch_translucency.calibrate import (
    CalibrationResult,
    Observation as CalibrationObservation,
    global_model_path,
    write_model_file,
)
from presidio_arch_translucency.observe import default_db_path, init_store


def _mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_default_observation_store_is_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    path = init_store()

    assert path == default_db_path()
    assert path.is_file()
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600


def test_default_model_store_is_private(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = CalibrationResult(
        concurrency=8.0,
        overhead_beta=0.02,
        r_squared=1.0,
        rmse=0.0,
        observations=[CalibrationObservation(rps=100.0, latency_ms=50.0, replicas=2)],
        predictions=[100.0],
        residuals=[0.0],
    )

    path = write_model_file(result)

    assert path == global_model_path()
    assert path.is_file()
    assert _mode(path.parent) == 0o700
    assert _mode(path) == 0o600
