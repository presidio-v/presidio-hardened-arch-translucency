"""
Docker benchmark calibration -- v0.9.0 (design decision D4).

The Docker-backed counterpart to the analytical ``calibrate`` mode. Instead of
the user supplying measured ``rps:latency_ms:replicas`` points, this module runs
a *controlled replica sweep* on the local Docker daemon: for each replica count
it starts that many workload containers, load-tests them, and records the
measured throughput and latency. The resulting operating points are turned into
``calibrate.Observation`` triples that ``calibrate.fit_calibration`` fits.

The sweep reuses the ``demo`` module's workload image and load-test harness, so a
benchmark exercises exactly the same Monte Carlo pi workload as ``pat demo`` --
each replica count is measured the way ``pat demo`` measures its "N independent
containers" variant (round-robin across loopback-published ports).

``calibrate`` stays intentionally Docker-free; all Docker orchestration lives
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from presidio_arch_translucency.calibrate import Observation

if TYPE_CHECKING:  # pragma: no cover - typing only
    from rich.console import Console
    from rich.progress import Progress


class BenchmarkError(RuntimeError):
    """Raised when the Docker benchmark cannot run or yields no usable points."""


@dataclass(frozen=True)
class BenchmarkPoint:
    """One measured operating point from a replica sweep."""

    replicas: int
    throughput_rps: float
    avg_latency_ms: float
    p95_latency_ms: float
    errors: int


def parse_replica_sweep(values: list[int]) -> list[int]:
    """
    Validate and normalise a replica sweep into sorted, de-duplicated counts.

    A fit needs at least two distinct replica counts to constrain both
    parameters, so fewer than two (or any non-positive count) is rejected.
    """
    if not values:
        raise BenchmarkError(
            "Benchmark mode needs at least two replica counts to sweep "
            "(e.g. --replicas 1 --replicas 2 --replicas 4)."
        )
    if any(int(v) < 1 for v in values):
        raise BenchmarkError("Replica counts must be positive integers (>= 1).")
    cleaned = sorted({int(v) for v in values})
    if len(cleaned) < 2:
        raise BenchmarkError(
            "Benchmark mode needs at least two distinct replica counts to fit "
            "the model (e.g. --replicas 1 --replicas 2 --replicas 4)."
        )
    return cleaned


def points_to_observations(points: list[BenchmarkPoint]) -> list[Observation]:
    """
    Convert measured benchmark points to calibration observations.

    Points with zero throughput or zero latency (a replica count that never
    served a request) carry no signal and are dropped. At least two usable
    points must remain to fit the model.
    """
    observations = [
        Observation(
            rps=p.throughput_rps,
            latency_ms=p.avg_latency_ms,
            replicas=p.replicas,
        )
        for p in points
        if p.throughput_rps > 0 and p.avg_latency_ms > 0
    ]
    if len(observations) < 2:
        raise BenchmarkError(
            "Benchmark produced fewer than two usable measurements "
            "(non-zero throughput and latency); cannot fit the model. "
            "Try fewer --iterations or more --requests."
        )
    return observations


def measure_replica_point(
    client: object,
    console: Console,
    progress: Progress,
    *,
    replicas: int,
    requests: int,
    concurrency: int,
    iterations: int,
) -> BenchmarkPoint:
    """
    Start *replicas* workload containers, load-test them, and measure one point.

    Containers are published to loopback only (via the ``demo`` helpers) and are
    always stopped and removed, even if the load test raises.
    """
    from presidio_arch_translucency import demo  # noqa: PLC0415

    containers: list[object] = []
    urls: list[str] = []
    try:
        for i in range(replicas):
            port = demo.V2_BASE_PORT + i
            container = client.containers.run(  # type: ignore[attr-defined]
                demo.FULL_IMAGE,
                name=f"{demo.CONTAINER_PREFIX}-bench-{i}",
                ports={"8080/tcp": demo._localhost_port(port)},
                detach=True,
                network=demo.NETWORK_NAME,
            )
            containers.append(container)
            urls.append(f"http://{demo.LOCALHOST}:{port}")

        for url in urls:
            if not demo._wait_url(f"{url}/health"):
                raise BenchmarkError(
                    f"Benchmark worker at {url} failed its health check."
                )

        result = demo._run_variant(
            f"{replicas} replica(s)",
            f"{replicas} independent containers, round-robin",
            urls,
            [c.id for c in containers],  # type: ignore[attr-defined]
            n_workers=replicas,
            n_lb=0,
            n_requests=requests,
            concurrency=concurrency,
            iterations=iterations,
            client=client,
            console=console,
            progress=progress,
        )
    finally:
        for container in containers:
            try:
                container.stop(timeout=5)  # type: ignore[attr-defined]
                container.remove(force=True)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001, S110
                pass

    return BenchmarkPoint(
        replicas=replicas,
        throughput_rps=result.throughput_rps,
        avg_latency_ms=result.avg_latency_ms,
        p95_latency_ms=result.p95_latency_ms,
        errors=result.errors,
    )


def run_benchmark_sweep(
    sweep: list[int],
    *,
    requests: int,
    concurrency: int,
    iterations: int,
    client: object | None = None,
    console: Console | None = None,
    force_rebuild: bool = False,
) -> list[BenchmarkPoint]:
    """
    Run the full replica sweep and return one measured point per replica count.

    Builds the workload image once, then measures each replica count in
    *sweep*. Always cleans up the demo containers and network afterwards. Raises
    ``BenchmarkError`` if the Docker daemon is unavailable.
    """
    from rich.console import Console  # noqa: PLC0415
    from rich.progress import (  # noqa: PLC0415
        BarColumn,
        Progress,
        TextColumn,
        TimeElapsedColumn,
    )

    from presidio_arch_translucency import demo  # noqa: PLC0415

    console = console or Console()

    if client is None:
        import docker  # noqa: PLC0415
        import docker.errors  # noqa: PLC0415

        try:
            client = docker.from_env()
            client.ping()
        except docker.errors.DockerException as exc:
            raise BenchmarkError(f"Docker daemon not available: {exc}") from exc

    demo._cleanup(client)
    demo._build_image(client, console, force=force_rebuild)
    demo._ensure_network(client)

    points: list[BenchmarkPoint] = []
    try:
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            for count in sweep:
                console.print(f"\n[bold]Benchmarking {count} replica(s)[/]")
                points.append(
                    measure_replica_point(
                        client,
                        console,
                        progress,
                        replicas=count,
                        requests=requests,
                        concurrency=concurrency,
                        iterations=iterations,
                    )
                )
    finally:
        demo._cleanup(client)

    return points
