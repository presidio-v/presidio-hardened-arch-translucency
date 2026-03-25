"""Tests for Presidio security extensions."""

import logging

import pytest

from presidio_arch_translucency.security import (
    InputValidationError,
    _sanitize_log_context,
    log_recommendation,
    log_security_event,
    run_dependency_audit,
    sanitize_latency_ms,
    sanitize_layer,
    sanitize_requests_per_second,
)

# ---------------------------------------------------------------------------
# sanitize_requests_per_second
# ---------------------------------------------------------------------------


class TestSanitizeRequestsPerSecond:
    def test_valid_value(self):
        assert sanitize_requests_per_second(500.0) == 500.0

    def test_valid_integer(self):
        assert sanitize_requests_per_second(100) == 100.0

    def test_minimum_boundary(self):
        assert sanitize_requests_per_second(0.01) == pytest.approx(0.01)

    def test_maximum_boundary(self):
        assert sanitize_requests_per_second(1_000_000.0) == 1_000_000.0

    def test_below_minimum_raises(self):
        with pytest.raises(InputValidationError, match="between"):
            sanitize_requests_per_second(0.0)

    def test_negative_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_requests_per_second(-1.0)

    def test_above_maximum_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_requests_per_second(1_000_001.0)

    def test_non_numeric_raises(self):
        with pytest.raises(InputValidationError, match="number"):
            sanitize_requests_per_second("500")  # type: ignore[arg-type]

    def test_none_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_requests_per_second(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# sanitize_latency_ms
# ---------------------------------------------------------------------------


class TestSanitizeLatencyMs:
    def test_valid_value(self):
        assert sanitize_latency_ms(80.0) == 80.0

    def test_valid_integer(self):
        assert sanitize_latency_ms(50) == 50.0

    def test_minimum_boundary(self):
        assert sanitize_latency_ms(0.1) == pytest.approx(0.1)

    def test_maximum_boundary(self):
        assert sanitize_latency_ms(300_000.0) == 300_000.0

    def test_zero_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_latency_ms(0.0)

    def test_negative_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_latency_ms(-10.0)

    def test_too_large_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_latency_ms(300_001.0)

    def test_string_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_latency_ms("fast")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# sanitize_layer
# ---------------------------------------------------------------------------

VALID = ("container", "pod", "deployment", "node")


class TestSanitizeLayer:
    def test_valid_container(self):
        assert sanitize_layer("container", VALID) == "container"

    def test_valid_pod(self):
        assert sanitize_layer("pod", VALID) == "pod"

    def test_valid_deployment(self):
        assert sanitize_layer("deployment", VALID) == "deployment"

    def test_valid_node(self):
        assert sanitize_layer("node", VALID) == "node"

    def test_uppercase_normalised(self):
        assert sanitize_layer("CONTAINER", VALID) == "container"

    def test_mixed_case_normalised(self):
        assert sanitize_layer("  Pod  ", VALID) == "pod"

    def test_invalid_raises(self):
        with pytest.raises(InputValidationError, match="must be one of"):
            sanitize_layer("kubernetes", VALID)

    def test_empty_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_layer("", VALID)

    def test_non_string_raises(self):
        with pytest.raises(InputValidationError):
            sanitize_layer(123, VALID)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _sanitize_log_context
# ---------------------------------------------------------------------------


class TestSanitizeLogContext:
    def test_allows_scalar_types(self):
        ctx = {"layer": "container", "replicas": 4, "gain": 12.5, "flag": True}
        result = _sanitize_log_context(ctx)
        assert result == ctx

    def test_strips_non_scalar(self):
        ctx = {"data": [1, 2, 3], "obj": object(), "layer": "container"}
        result = _sanitize_log_context(ctx)
        assert "data" not in result
        assert "obj" not in result
        assert result["layer"] == "container"

    def test_truncates_long_keys(self):
        long_key = "x" * 100
        ctx = {long_key: "value"}
        result = _sanitize_log_context(ctx)
        assert all(len(k) <= 64 for k in result)

    def test_empty_context(self):
        assert _sanitize_log_context({}) == {}


# ---------------------------------------------------------------------------
# log_security_event and log_recommendation
# ---------------------------------------------------------------------------


class TestLogging:
    def test_log_security_event_emits(self, caplog):
        with caplog.at_level(logging.INFO, logger="presidio.arch_translucency.audit"):
            log_security_event("TEST_EVENT", {"layer": "container"})
        assert "TEST_EVENT" in caplog.text

    def test_log_recommendation_emits(self, caplog):
        with caplog.at_level(logging.INFO, logger="presidio.arch_translucency.audit"):
            log_recommendation("container", 4, 35.5)
        assert "recommendation applied" in caplog.text.lower()

    def test_log_security_event_no_crash_with_none_context(self):
        # Should not raise even with None
        log_security_event("NO_CONTEXT", None)

    def test_log_recommendation_correct_layer(self, caplog):
        with caplog.at_level(logging.INFO, logger="presidio.arch_translucency.audit"):
            log_recommendation("pod", 8, 22.0)
        assert "pod" in caplog.text


# ---------------------------------------------------------------------------
# run_dependency_audit (smoke test — graceful skip when pip-audit absent)
# ---------------------------------------------------------------------------


class TestRunDependencyAudit:
    def test_returns_bool(self):
        result = run_dependency_audit(skip_on_error=True)
        assert isinstance(result, bool)

    def test_does_not_raise(self):
        # Must not raise regardless of pip-audit installation status
        run_dependency_audit(skip_on_error=True)
