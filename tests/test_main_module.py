"""
Tests for the ``python -m presidio_arch_translucency`` module entry-point.

The entry-point (``__main__.py``) is the path-independent fallback the daemon
units use when the ``pat`` console script is not on ``PATH``. We execute the
module under ``run_name="__main__"`` so the ``if __name__ == "__main__"`` guard
runs and delegates to the Typer ``app``.
"""

from __future__ import annotations

import runpy
import sys
from unittest.mock import patch


def test_module_imports_without_running_app() -> None:
    """Importing the module normally must not invoke the CLI (guard is false)."""
    with patch("presidio_arch_translucency.cli.app") as mock_app:
        import presidio_arch_translucency.__main__  # noqa: F401

    mock_app.assert_not_called()


def test_module_entrypoint_invokes_cli_app() -> None:
    """``python -m presidio_arch_translucency`` should call the CLI ``app``."""
    # Drop any cached import so runpy executes the module body cleanly
    # (avoids the "found in sys.modules after import" RuntimeWarning).
    sys.modules.pop("presidio_arch_translucency.__main__", None)
    with patch("presidio_arch_translucency.cli.app") as mock_app:
        runpy.run_module(
            "presidio_arch_translucency",
            run_name="__main__",
        )
    mock_app.assert_called_once_with()
