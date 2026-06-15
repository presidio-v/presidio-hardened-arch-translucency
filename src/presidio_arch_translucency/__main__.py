"""Module entry-point so ``python -m presidio_arch_translucency`` runs the CLI.

Provides a stable, path-independent way to invoke ``pat`` (used as the launchd /
systemd fallback command when the ``pat`` console script is not on ``PATH``).
"""

from __future__ import annotations

from presidio_arch_translucency.cli import app

if __name__ == "__main__":
    app()
