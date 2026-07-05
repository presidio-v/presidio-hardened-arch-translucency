"""Shared model/calibration constants kept out of the model<->calibrate import path."""

from __future__ import annotations

from typing import Final

DEFAULT_CONCURRENCY: Final[float] = 8.0
GLOBAL_MODEL_RELPATH: Final[tuple[str, str]] = (".pat", "model.json")
DEFAULT_LAYER_NAME: Final[str] = "default"
