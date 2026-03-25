"""Tests for the CLI entry-point."""

from typer.testing import CliRunner

from presidio_arch_translucency.cli import app

runner = CliRunner()


def invoke(*args: str, skip_audit: bool = True):
    """Helper: invoke pat with --skip-audit by default to avoid network calls."""
    base = ["--skip-audit"] if skip_audit else []
    return runner.invoke(app, base + list(args))


# ---------------------------------------------------------------------------
# Version / help
# ---------------------------------------------------------------------------


class TestVersionAndHelp:
    def test_version_flag(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_help_flag(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "analyze" in result.output.lower()

    def test_analyze_help(self):
        result = invoke("analyze", "--help")
        assert result.exit_code == 0
        assert "requests-per-sec" in result.output


# ---------------------------------------------------------------------------
# Successful analyze invocations
# ---------------------------------------------------------------------------


class TestAnalyzeSuccess:
    def test_basic_invocation(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "500",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "container",
        )
        assert result.exit_code == 0
        assert "Recommendation" in result.output

    def test_pod_layer(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "200",
            "--avg-latency-ms",
            "40",
            "--current-layer",
            "pod",
        )
        assert result.exit_code == 0

    def test_deployment_layer(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "1000",
            "--avg-latency-ms",
            "100",
            "--current-layer",
            "deployment",
        )
        assert result.exit_code == 0

    def test_node_layer(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "50",
            "--avg-latency-ms",
            "200",
            "--current-layer",
            "node",
        )
        assert result.exit_code == 0

    def test_show_all_flag(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "500",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "container",
            "--show-all",
        )
        assert result.exit_code == 0
        # All four layers should appear in the table
        for layer in ("container", "pod", "deployment", "node"):
            assert layer in result.output

    def test_output_contains_throughput_info(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "500",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "container",
        )
        assert result.exit_code == 0
        output_lower = result.output.lower()
        assert "throughput" in output_lower or "req/s" in output_lower

    def test_output_contains_response_time_info(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "500",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "container",
        )
        assert "ms" in result.output


# ---------------------------------------------------------------------------
# Input validation errors (exit code 2)
# ---------------------------------------------------------------------------


class TestAnalyzeValidationErrors:
    def test_negative_rps(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "-1",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "container",
        )
        assert result.exit_code != 0

    def test_zero_latency(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "500",
            "--avg-latency-ms",
            "0",
            "--current-layer",
            "container",
        )
        assert result.exit_code != 0

    def test_invalid_layer(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "500",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "invalid_layer",
        )
        assert result.exit_code != 0

    def test_missing_required_args(self):
        result = invoke("analyze")
        assert result.exit_code != 0

    def test_layer_case_insensitive_uppercase(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "500",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "CONTAINER",
        )
        assert result.exit_code == 0

    def test_layer_case_insensitive_mixed(self):
        result = invoke(
            "analyze",
            "--requests-per-second",
            "500",
            "--avg-latency-ms",
            "80",
            "--current-layer",
            "Pod",
        )
        assert result.exit_code == 0
