"""Tests for the CLI entry-point."""

import re

from typer.testing import CliRunner

from presidio_arch_translucency.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def strip_ansi(text: str) -> str:
    """Remove ANSI escape codes so assertions are not tripped by Rich styling."""
    return _ANSI_RE.sub("", text)


def combined_output(result) -> str:
    """Runner stdout+stderr, robust across click versions.

    Newer click (>=8.2) captures stderr separately; the click resolved on
    Python 3.9 mixes stderr into stdout and leaves ``stderr_bytes`` as None, so
    accessing ``result.stderr`` raises. Append stderr only when captured apart.
    """
    text = result.output or ""
    if getattr(result, "stderr_bytes", None) is not None:
        text += result.stderr
    return text


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
        assert "0.6.0" in result.output

    def test_help_flag(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "analyze" in result.output.lower()

    def test_analyze_help(self):
        result = invoke("analyze", "--help")
        assert result.exit_code == 0
        clean = strip_ansi(result.output)
        # The full option name may be truncated by Typer when the terminal
        # column is narrow (e.g. default 80-char width shows
        # "--requests-per-seco…").  Check the visible prefix + short form.
        assert "requests-per-sec" in clean  # always fits before truncation
        assert "-r" in clean  # short alias for --requests-per-second


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
        assert "Recommendation" in strip_ansi(result.output)

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
        clean = strip_ansi(result.output)
        for layer in ("container", "pod", "deployment", "node"):
            assert layer in clean

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
        output_lower = strip_ansi(result.output).lower()
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
        assert "ms" in strip_ansi(result.output)


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


# ---------------------------------------------------------------------------
# Envelope warning (v0.7.0) — fires unless a calibrated model exists
# ---------------------------------------------------------------------------


class TestEnvelopeWarning:
    def test_warning_shown_when_uncalibrated(self, tmp_path, monkeypatch):
        # Empty HOME + cwd ⇒ no calibrated model ⇒ warning must appear.
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.chdir(tmp_path)
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
        combined = strip_ansi(combined_output(result))
        assert "pat calibrate" in combined
        assert "default parameters" in combined.lower()

    def test_warning_suppressed_when_calibrated(self, tmp_path, monkeypatch):
        # A global ~/.pat/model.json suppresses the warning.
        home = tmp_path / "home"
        pat_dir = home / ".pat"
        pat_dir.mkdir(parents=True)
        (pat_dir / "model.json").write_text('{"concurrency": 8.0}', encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.chdir(tmp_path)
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
        combined = strip_ansi(combined_output(result))
        assert "pat calibrate" not in combined
