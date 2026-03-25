"""
Unit tests for demo.py pure functions.
These tests do not require a running Docker daemon.
"""

from pathlib import Path

from rich.console import Console

from presidio_arch_translucency.demo import (
    CONTAINER_PREFIX,
    VariantResult,
    _render_cost_section,
    _render_hpa_section,
    nginx_conf,
    save_plot,
    translucency_insight,
)

# ── VariantResult ──────────────────────────────────────────────────────────────


def _vr(name: str, tp: float, lat: float, cpu: float = 50.0) -> VariantResult:
    return VariantResult(
        name=name,
        description="",
        n_workers=1,
        n_lb=0,
        throughput_rps=tp,
        avg_latency_ms=lat,
        p95_latency_ms=lat * 1.5,
        cpu_pct=cpu,
        errors=0,
    )


def test_variant_result_fields() -> None:
    r = _vr("1 — Single container", 10.0, 500.0)
    assert r.name == "1 — Single container"
    assert r.throughput_rps == 10.0
    assert r.avg_latency_ms == 500.0
    assert r.errors == 0


# ── nginx_conf ─────────────────────────────────────────────────────────────────


def test_nginx_conf_contains_all_servers() -> None:
    conf = nginx_conf(4)
    for i in range(4):
        assert f"{CONTAINER_PREFIX}-v3-{i}:8080" in conf


def test_nginx_conf_structure() -> None:
    conf = nginx_conf(2)
    assert "upstream workload" in conf
    assert "proxy_pass http://workload" in conf
    assert "listen 80" in conf
    assert "/health" in conf


def test_nginx_conf_single_worker() -> None:
    conf = nginx_conf(1)
    assert f"{CONTAINER_PREFIX}-v3-0:8080" in conf
    assert f"{CONTAINER_PREFIX}-v3-1:8080" not in conf


def test_nginx_conf_returns_string() -> None:
    assert isinstance(nginx_conf(3), str)


# ── translucency_insight ───────────────────────────────────────────────────────


def test_translucency_insight_empty() -> None:
    assert translucency_insight([]) == "No results to analyse."


def test_translucency_insight_single() -> None:
    insight = translucency_insight([_vr("1 — Single container", 10.0, 500.0)])
    assert "Best layer" in insight
    assert "1 — Single container" in insight


def test_translucency_insight_best_is_variant2() -> None:
    results = [
        _vr("1 — Single container", 10.0, 500.0),
        _vr("2 — 4 containers (round-robin)", 35.0, 120.0),
        _vr("3 — nginx LB (4 workers)", 28.0, 160.0),
    ]
    insight = translucency_insight(results)
    assert "2" in insight
    assert "Speedup" in insight
    assert "Architectural translucency insight" in insight
    assert "Manual container replication" in insight


def test_translucency_insight_best_is_variant3() -> None:
    results = [
        _vr("1 — Single container", 10.0, 500.0),
        _vr("2 — 4 containers (round-robin)", 28.0, 160.0),
        _vr("3 — nginx LB (4 workers)", 35.0, 120.0),
    ]
    insight = translucency_insight(results)
    assert "nginx" in insight or "3" in insight


def test_translucency_insight_baseline_wins() -> None:
    results = [
        _vr("1 — Single container", 35.0, 50.0),
        _vr("2 — 4 containers (round-robin)", 10.0, 500.0),
    ]
    insight = translucency_insight(results)
    assert "single container" in insight.lower() or "Adding" in insight


def test_translucency_insight_speedup_ratio() -> None:
    results = [
        _vr("1 — Single container", 10.0, 400.0),
        _vr("2 — 4 containers (round-robin)", 40.0, 100.0),
    ]
    insight = translucency_insight(results)
    assert "4.00×" in insight


# ── save_plot ──────────────────────────────────────────────────────────────────


def test_save_plot_creates_file(tmp_path: Path) -> None:
    results = [
        _vr("1 — Single container", 10.0, 500.0, 30.0),
        _vr("2 — 4 containers (round-robin)", 35.0, 120.0, 80.0),
        _vr("3 — nginx LB (4 workers)", 28.0, 160.0, 70.0),
    ]
    out = tmp_path / "results.png"
    save_plot(results, out)
    assert out.exists()
    assert out.stat().st_size > 0


def test_save_plot_two_variants(tmp_path: Path) -> None:
    results = [
        _vr("1 — Single container", 10.0, 500.0),
        _vr("2 — 4 containers", 30.0, 150.0),
    ]
    out = tmp_path / "two.png"
    save_plot(results, out)
    assert out.exists()


# ── _render_hpa_section ────────────────────────────────────────────────────────


def _demo_results() -> list[VariantResult]:
    return [
        _vr("1 — Single container", 10.0, 500.0, 30.0),
        VariantResult(
            "2 — 4 containers (round-robin)", "", 4, 0, 35.0, 120.0, 180.0, 80.0, 0
        ),
        _vr("3 — nginx LB (4 workers)", 28.0, 160.0, 70.0),
    ]


def test_render_hpa_section_creates_plot(tmp_path: Path) -> None:
    out = tmp_path / "demo-results.png"
    console = Console(file=open(tmp_path / "out.txt", "w"))  # noqa: SIM115
    _render_hpa_section(_demo_results(), out, 3.0, console)
    hpa_out = tmp_path / "demo-results-hpa.png"
    assert hpa_out.exists()
    assert hpa_out.stat().st_size > 0


def test_render_hpa_section_skips_zero_throughput(tmp_path: Path) -> None:
    out = tmp_path / "demo-results.png"
    console = Console(file=open(tmp_path / "out.txt", "w"))  # noqa: SIM115
    # All zero throughput — should not crash
    bad = [_vr("1 — Single container", 0.0, 0.0)]
    _render_hpa_section(bad, out, 3.0, console)
    assert not (tmp_path / "demo-results-hpa.png").exists()


# ── _render_cost_section ───────────────────────────────────────────────────────


def test_render_cost_section_basic(tmp_path: Path) -> None:
    console = Console(file=open(tmp_path / "out.txt", "w"))  # noqa: SIM115
    _render_cost_section(_demo_results(), 0.02, console)
    out = (tmp_path / "out.txt").read_text()
    assert "Cost" in out or "cost" in out.lower() or "$" in out


def test_render_cost_section_skips_zero_throughput(tmp_path: Path) -> None:
    console = Console(file=open(tmp_path / "out.txt", "w"))  # noqa: SIM115
    bad = [_vr("1 — Single container", 0.0, 0.0)]
    # Should not crash
    _render_cost_section(bad, 0.02, console)


def test_render_cost_section_custom_cost(tmp_path: Path) -> None:
    console = Console(file=open(tmp_path / "out.txt", "w"))  # noqa: SIM115
    _render_cost_section(_demo_results(), 0.10, console)
    out = (tmp_path / "out.txt").read_text()
    assert "0.10" in out or "0.1000" in out


def test_render_hpa_section_custom_multiplier(tmp_path: Path) -> None:
    out = tmp_path / "demo-results.png"
    console = Console(file=open(tmp_path / "out.txt", "w"))  # noqa: SIM115
    _render_hpa_section(_demo_results(), out, 5.0, console)
    assert (tmp_path / "demo-results-hpa.png").exists()
