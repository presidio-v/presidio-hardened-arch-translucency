"""Tests for Docker benchmark calibration (`pat calibrate --benchmark`, v0.9.0 D4).

The Docker layer is always mocked -- no daemon is required to run these tests.
"""

import json

import pytest
from rich.console import Console
from typer.testing import CliRunner

import presidio_arch_translucency.benchmark as bench
import presidio_arch_translucency.demo as demo
from presidio_arch_translucency.cli import app

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, ["--skip-audit", *args])


def _point(
    replicas: int, rps: float, lat: float, errors: int = 0
) -> bench.BenchmarkPoint:
    return bench.BenchmarkPoint(
        replicas=replicas,
        throughput_rps=rps,
        avg_latency_ms=lat,
        p95_latency_ms=lat * 1.4,
        errors=errors,
    )


# ── parse_replica_sweep ───────────────────────────────────────────────────────


def test_parse_replica_sweep_sorts_and_dedupes() -> None:
    assert bench.parse_replica_sweep([4, 1, 2, 2, 4]) == [1, 2, 4]


def test_parse_replica_sweep_empty_raises() -> None:
    with pytest.raises(bench.BenchmarkError):
        bench.parse_replica_sweep([])


def test_parse_replica_sweep_single_count_raises() -> None:
    # One distinct count cannot constrain two parameters.
    with pytest.raises(bench.BenchmarkError):
        bench.parse_replica_sweep([4, 4, 4])


@pytest.mark.parametrize("bad", [[0, 2], [-1, 3], [1, -2, 4]])
def test_parse_replica_sweep_non_positive_raises(bad: list[int]) -> None:
    with pytest.raises(bench.BenchmarkError):
        bench.parse_replica_sweep(bad)


# ── points_to_observations ────────────────────────────────────────────────────


def test_points_to_observations_maps_fields() -> None:
    obs = bench.points_to_observations([_point(1, 95.0, 80.0), _point(2, 180.0, 78.0)])
    assert [(o.rps, o.latency_ms, o.replicas) for o in obs] == [
        (95.0, 80.0, 1),
        (180.0, 78.0, 2),
    ]


def test_points_to_observations_drops_zero_signal_points() -> None:
    pts = [_point(1, 95.0, 80.0), _point(2, 0.0, 78.0), _point(4, 340.0, 0.0)]
    obs = bench.points_to_observations(pts + [_point(8, 600.0, 70.0)])
    # The two zero-signal points are dropped; two usable points remain.
    assert {o.replicas for o in obs} == {1, 8}


def test_points_to_observations_insufficient_raises() -> None:
    with pytest.raises(bench.BenchmarkError):
        bench.points_to_observations([_point(1, 95.0, 80.0), _point(2, 0.0, 0.0)])


# ── measure_replica_point (Docker mocked) ─────────────────────────────────────


class _FakeContainer:
    def __init__(self, idx: int) -> None:
        self.id = f"cid-{idx}"
        self.stopped = False
        self.removed = False

    def stop(self, timeout: int = 5) -> None:
        self.stopped = True

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainers:
    def __init__(self) -> None:
        self.created: list[_FakeContainer] = []

    def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
        c = _FakeContainer(len(self.created))
        self.created.append(c)
        return c


class _FakeClient:
    def __init__(self) -> None:
        self.containers = _FakeContainers()


def test_measure_replica_point_returns_measured_point(monkeypatch) -> None:
    monkeypatch.setattr(demo, "_wait_url", lambda *a, **k: True)
    monkeypatch.setattr(
        demo,
        "_run_variant",
        lambda *a, **k: demo.VariantResult(
            name="2 replica(s)",
            description="",
            n_workers=2,
            n_lb=0,
            throughput_rps=180.0,
            avg_latency_ms=78.0,
            p95_latency_ms=110.0,
            cpu_pct=50.0,
            errors=0,
        ),
    )
    client = _FakeClient()
    point = bench.measure_replica_point(
        client,
        Console(),
        progress=None,
        replicas=2,
        requests=20,
        concurrency=4,
        iterations=1000,
    )
    assert point == bench.BenchmarkPoint(
        replicas=2,
        throughput_rps=180.0,
        avg_latency_ms=78.0,
        p95_latency_ms=110.0,
        errors=0,
    )
    # Two containers were started, then all stopped and removed.
    assert len(client.containers.created) == 2
    assert all(c.stopped and c.removed for c in client.containers.created)


def test_measure_replica_point_cleans_up_on_health_failure(monkeypatch) -> None:
    monkeypatch.setattr(demo, "_wait_url", lambda *a, **k: False)
    client = _FakeClient()
    with pytest.raises(bench.BenchmarkError, match="health check"):
        bench.measure_replica_point(
            client,
            Console(),
            progress=None,
            replicas=3,
            requests=20,
            concurrency=4,
            iterations=1000,
        )
    # Even on failure, every started container is cleaned up.
    assert len(client.containers.created) == 3
    assert all(c.stopped and c.removed for c in client.containers.created)


# ── run_benchmark_sweep (Docker mocked) ───────────────────────────────────────


def test_run_benchmark_sweep_measures_each_count(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(demo, "_cleanup", lambda c: calls.append("cleanup"))
    monkeypatch.setattr(demo, "_build_image", lambda *a, **k: calls.append("build"))
    monkeypatch.setattr(demo, "_ensure_network", lambda c: calls.append("network"))

    measured: list[int] = []

    def _fake_measure(client, console, progress, *, replicas, **kw):  # noqa: ANN001
        measured.append(replicas)
        return _point(replicas, 90.0 * replicas, 80.0)

    monkeypatch.setattr(bench, "measure_replica_point", _fake_measure)

    points = bench.run_benchmark_sweep(
        [1, 2, 4],
        requests=20,
        concurrency=4,
        iterations=1000,
        client=object(),
    )
    assert measured == [1, 2, 4]
    assert [p.replicas for p in points] == [1, 2, 4]
    # Image built once; network ensured; cleanup runs before and after.
    assert calls.count("build") == 1
    assert calls.count("cleanup") == 2


def test_measure_replica_point_swallows_cleanup_errors(monkeypatch) -> None:
    """A container that refuses to stop must not break the measurement."""

    class _StubbornContainer(_FakeContainer):
        def stop(self, timeout: int = 5) -> None:
            raise RuntimeError("will not stop")

    class _StubbornContainers(_FakeContainers):
        def run(self, *args, **kwargs):  # noqa: ANN002, ANN003
            c = _StubbornContainer(len(self.created))
            self.created.append(c)
            return c

    client = _FakeClient()
    client.containers = _StubbornContainers()
    monkeypatch.setattr(demo, "_wait_url", lambda *a, **k: True)
    monkeypatch.setattr(
        demo,
        "_run_variant",
        lambda *a, **k: demo.VariantResult(
            name="1 replica(s)",
            description="",
            n_workers=1,
            n_lb=0,
            throughput_rps=95.0,
            avg_latency_ms=80.0,
            p95_latency_ms=112.0,
            cpu_pct=50.0,
            errors=0,
        ),
    )
    point = bench.measure_replica_point(
        client,
        Console(),
        progress=None,
        replicas=1,
        requests=20,
        concurrency=4,
        iterations=1000,
    )
    assert point.throughput_rps == 95.0


def test_run_benchmark_sweep_creates_client_from_env(monkeypatch) -> None:
    """When no client is passed, one is created via docker.from_env()."""
    import docker

    class _PingClient:
        def __init__(self) -> None:
            self.pinged = False

        def ping(self) -> None:
            self.pinged = True

    created = _PingClient()
    monkeypatch.setattr(docker, "from_env", lambda: created)
    monkeypatch.setattr(demo, "_cleanup", lambda c: None)
    monkeypatch.setattr(demo, "_build_image", lambda *a, **k: None)
    monkeypatch.setattr(demo, "_ensure_network", lambda c: None)
    monkeypatch.setattr(
        bench,
        "measure_replica_point",
        lambda *a, **k: _point(k["replicas"], 90.0, 80.0),
    )
    points = bench.run_benchmark_sweep(
        [1, 2], requests=20, concurrency=4, iterations=1000, client=None
    )
    assert created.pinged is True
    assert [p.replicas for p in points] == [1, 2]


def test_run_benchmark_sweep_docker_unavailable_raises(monkeypatch) -> None:
    import docker

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise docker.errors.DockerException("no daemon")

    monkeypatch.setattr(docker, "from_env", _boom)
    with pytest.raises(bench.BenchmarkError, match="Docker daemon not available"):
        bench.run_benchmark_sweep(
            [1, 2], requests=20, concurrency=4, iterations=1000, client=None
        )


# ── pat calibrate --benchmark CLI (Docker mocked) ─────────────────────────────


def _patch_sweep(monkeypatch, points: list[bench.BenchmarkPoint]) -> None:
    """Replace the Docker sweep with a synthetic one."""
    monkeypatch.setattr(bench, "run_benchmark_sweep", lambda *a, **k: list(points))


def test_calibrate_benchmark_writes_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _patch_sweep(
        monkeypatch,
        [_point(1, 95.0, 80.0), _point(2, 180.0, 78.0), _point(4, 340.0, 75.0)],
    )
    result = invoke(
        "calibrate",
        "--benchmark",
        "--replicas",
        "1",
        "--replicas",
        "2",
        "--replicas",
        "4",
    )
    assert result.exit_code == 0, result.output
    assert "Benchmark sweep" in result.output
    assert "Calibration" in result.output
    assert (tmp_path / ".pat" / "model.json").is_file()


def test_calibrate_benchmark_layer_writes_layers_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _patch_sweep(
        monkeypatch,
        [_point(1, 95.0, 80.0), _point(2, 180.0, 78.0), _point(4, 340.0, 75.0)],
    )
    result = invoke("calibrate", "--benchmark", "--layer", "container")
    assert result.exit_code == 0, result.output
    payload = json.loads((tmp_path / ".pat" / "model.json").read_text(encoding="utf-8"))
    assert "container" in payload["layers"]
    # Security log records benchmark mode.
    assert "layers.container" in result.output


def test_calibrate_benchmark_default_sweep(tmp_path, monkeypatch) -> None:
    """No --replicas given uses the default 1/2/4 sweep."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    seen: dict[str, object] = {}

    def _capture(sweep, **kw):  # noqa: ANN001, ANN003
        seen["sweep"] = sweep
        return [_point(1, 95.0, 80.0), _point(2, 180.0, 78.0), _point(4, 340.0, 75.0)]

    monkeypatch.setattr(bench, "run_benchmark_sweep", _capture)
    result = invoke("calibrate", "--benchmark")
    assert result.exit_code == 0, result.output
    assert seen["sweep"] == [1, 2, 4]


def test_calibrate_benchmark_rejects_observation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = invoke("calibrate", "--benchmark", "--observation", "300:80:5")
    assert result.exit_code == 2
    assert "drop --observation" in result.output


def test_calibrate_benchmark_single_replica_count_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = invoke("calibrate", "--benchmark", "--replicas", "4")
    assert result.exit_code == 2
    assert "two distinct replica counts" in result.output


def test_calibrate_benchmark_docker_error_exits_1(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise bench.BenchmarkError("Docker daemon not available: nope")

    monkeypatch.setattr(bench, "run_benchmark_sweep", _boom)
    result = invoke("calibrate", "--benchmark")
    assert result.exit_code == 1
    assert "Benchmark error" in result.output


def test_calibrate_no_observation_no_benchmark_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    result = invoke("calibrate")
    assert result.exit_code == 2
    assert "--observation" in result.output
