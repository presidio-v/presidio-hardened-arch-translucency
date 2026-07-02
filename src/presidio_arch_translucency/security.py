"""
Presidio security extensions for presidio-hardened-arch-translucency.

Provides:
  - Input sanitization / validation for all workload parameters
  - Secure structured logging (no sensitive data leakage)
  - On-run CVE/dependency check via pip-audit
  - Security event logging ("Presidio architectural-translucency recommendation
    applied")
"""

from __future__ import annotations

import logging
import math
import subprocess
import sys
from typing import Any

# ---------------------------------------------------------------------------
# Secure logger -- never logs raw user-supplied strings at WARNING+ level
# ---------------------------------------------------------------------------

_SECURITY_LOGGER = logging.getLogger("presidio.arch_translucency.security")
_AUDIT_LOGGER = logging.getLogger("presidio.arch_translucency.audit")
_SENSITIVE_CONTEXT_KEY_PARTS = (
    "auth",
    "credential",
    "key",
    "password",
    "secret",
    "token",
)
_REDACTED = "[REDACTED]"


def configure_logging(verbose: bool = False) -> None:
    """Configure secure structured logging for the tool."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s -- %(message)s"
    logging.basicConfig(format=fmt, level=level, stream=sys.stderr)
    # Ensure audit logger always emits at INFO+
    _AUDIT_LOGGER.setLevel(logging.INFO)


def log_security_event(event: str, context: dict[str, Any] | None = None) -> None:
    """
    Emit a Presidio security event.  context values are sanitized before logging.
    """
    safe_ctx = _sanitize_log_context(context or {})
    _AUDIT_LOGGER.info("SECURITY_EVENT event=%r context=%r", event, safe_ctx)


def log_recommendation(
    layer: str,
    replicas: int,
    throughput_gain_pct: float,
) -> None:
    """
    Log a replication recommendation.  Only non-sensitive numeric data is logged.
    No user-supplied free-form strings are included.
    """
    _AUDIT_LOGGER.info(
        "Presidio architectural-translucency recommendation applied: "
        "layer=%r replicas=%d throughput_gain_pct=%.1f",
        layer,
        replicas,
        throughput_gain_pct,
    )
    log_security_event(
        "Presidio architectural-translucency recommendation applied",
        {"layer": layer, "replicas": replicas},
    )


def _is_sensitive_context_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_CONTEXT_KEY_PARTS)


def _sanitize_log_context(ctx: dict[str, Any]) -> dict[str, Any]:
    """Strip any context values that look like secrets or are non-scalar."""
    safe: dict[str, Any] = {}
    for k, v in ctx.items():
        key = str(k)[:64]
        if _is_sensitive_context_key(key):
            safe[key] = _REDACTED
        elif isinstance(v, (str, int, float, bool)):
            safe[key] = v
    return safe


# ---------------------------------------------------------------------------
# Input sanitization
# ---------------------------------------------------------------------------

_RPS_MIN: float = 0.01
_RPS_MAX: float = 1_000_000.0
_LATENCY_MIN_MS: float = 0.1
_LATENCY_MAX_MS: float = 300_000.0  # 5 minutes


class InputValidationError(ValueError):
    """Raised when a workload parameter fails sanitization."""


def sanitize_requests_per_second(value: float) -> float:
    """Validate and return a safe requests-per-second value."""
    if not isinstance(value, (int, float)):
        raise InputValidationError(
            f"requests_per_second must be a number, got {type(value).__name__!r}"
        )
    v = float(value)
    if not (_RPS_MIN <= v <= _RPS_MAX):
        raise InputValidationError(
            f"requests_per_second must be between {_RPS_MIN} and {_RPS_MAX}, got {v}"
        )
    return v


def sanitize_latency_ms(value: float) -> float:
    """Validate and return a safe average latency in milliseconds."""
    if not isinstance(value, (int, float)):
        raise InputValidationError(
            f"avg_latency_ms must be a number, got {type(value).__name__!r}"
        )
    v = float(value)
    if not (_LATENCY_MIN_MS <= v <= _LATENCY_MAX_MS):
        raise InputValidationError(
            f"avg_latency_ms must be between {_LATENCY_MIN_MS} and "
            f"{_LATENCY_MAX_MS}, got {v}"
        )
    return v


def sanitize_bounded_number(
    value: float,
    name: str,
    minimum: float,
    maximum: float,
) -> float:
    """Validate a finite number within [minimum, maximum] (rejects nan/inf).

    Added for the v0.18.0 training-domain inputs (third-party audit finding:
    Typer's ``min=`` bound does not reject ``nan``/``inf``). ``bool`` is
    rejected explicitly — it is an ``int`` subclass but never a valid workload
    number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputValidationError(
            f"{name} must be a number, got {type(value).__name__!r}"
        )
    v = float(value)
    if not math.isfinite(v):
        raise InputValidationError(f"{name} must be finite, got {v!r}")
    if not (minimum <= v <= maximum):
        raise InputValidationError(
            f"{name} must be between {minimum} and {maximum}, got {v}"
        )
    return v


def sanitize_layer(value: str, valid_layers: tuple[str, ...]) -> str:
    """Validate that the supplied layer name is one of the allowed values."""
    if not isinstance(value, str):
        raise InputValidationError(
            f"layer must be a string, got {type(value).__name__!r}"
        )
    cleaned = value.strip().lower()
    if cleaned not in valid_layers:
        raise InputValidationError(
            f"layer must be one of {valid_layers!r}, got {cleaned!r}"
        )
    return cleaned


# ---------------------------------------------------------------------------
# CVE / dependency audit
# ---------------------------------------------------------------------------


def run_dependency_audit(skip_on_error: bool = True) -> bool:
    """
    Run pip-audit to check for known CVEs in installed packages.

    Returns True if audit passes (no vulnerabilities found).
    Returns False if vulnerabilities found.
    Skips gracefully if pip-audit is not installed.
    """
    try:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "pip_audit", "--progress-spinner=off"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            _SECURITY_LOGGER.info(
                "Dependency CVE audit: PASSED (no known vulnerabilities)"
            )
            return True
        else:
            _SECURITY_LOGGER.warning(
                "Dependency CVE audit: VULNERABILITIES FOUND\n%s",
                result.stdout[:2000],  # cap output length for log safety
            )
            log_security_event("CVE_VULNERABILITIES_DETECTED")
            return False
    except FileNotFoundError:
        _SECURITY_LOGGER.debug(
            "pip-audit not installed; skipping CVE check. "
            "Install with: pip install pip-audit"
        )
        return True  # non-blocking when audit tool absent
    except subprocess.TimeoutExpired:
        if not skip_on_error:
            raise
        _SECURITY_LOGGER.warning("Dependency CVE audit timed out; continuing.")
        return True
    except Exception as exc:  # noqa: BLE001
        if not skip_on_error:
            raise
        _SECURITY_LOGGER.warning(
            "Dependency CVE audit failed unexpectedly (skipped): %s", exc
        )
        return True
