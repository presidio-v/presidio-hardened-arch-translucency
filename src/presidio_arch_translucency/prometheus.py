"""
Prometheus observation source (v0.8.0, Phase 3).

Scrapes a single workload sample from a Prometheus server's instant-query API
and turns it into an :class:`~presidio_arch_translucency.observe.Observation`
(``source="prometheus"``) that plugs straight into ``record_observation``.

Design (decisions D2 / D3):

* **Single-shot.** :func:`fetch_observation` collects *one* sample and returns;
  recurring collection is scheduled externally (cron / launchd / a Kubernetes
  CronJob), exactly like the rest of ``pat observe``.
* **Auth: env token only.** A bearer token is read from ``PAT_PROMETHEUS_TOKEN``
  and sent as ``Authorization: Bearer …``.  It is never accepted as a CLI arg
  and never logged.  With no token the request is unauthenticated (in-cluster
  use).  kubeconfig auth is deferred beyond v0.8.0.
* **urllib only.** No client library and no new dependency — same pattern as
  ``cloud.py``.

The four PromQL queries default to the spec's metrics but are overridable so the
module works against differently-named application metrics:

* rps        rate of ``http_requests_total`` (1m), summed
* p99 (s)    ``histogram_quantile(0.99, …)`` over ``http_request_duration_seconds``
* avg (s)    request-duration ``_sum`` / ``_count`` rates
* replicas   ``count(up == 1)``  (healthy scrape targets ≈ pod count)

See the ``DEFAULT_*_QUERY`` constants for the exact PromQL.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from presidio_arch_translucency import __version__
from presidio_arch_translucency.observe import Observation, utcnow

TOKEN_ENV = "PAT_PROMETHEUS_TOKEN"  # noqa: S105 — env var name, not a secret
_USER_AGENT = f"pat-cli/{__version__}"

DEFAULT_RPS_QUERY = "sum(rate(http_requests_total[1m]))"
DEFAULT_P99_QUERY = (
    "histogram_quantile(0.99, "
    "sum(rate(http_request_duration_seconds_bucket[1m])) by (le))"
)
DEFAULT_AVG_QUERY = (
    "sum(rate(http_request_duration_seconds_sum[1m])) "
    "/ sum(rate(http_request_duration_seconds_count[1m]))"
)
DEFAULT_REPLICAS_QUERY = "count(up == 1)"


class PrometheusError(RuntimeError):
    """Raised when a Prometheus query fails or returns malformed data."""


# ---------------------------------------------------------------------------
# HTTP / query building
# ---------------------------------------------------------------------------


def _build_query_url(base_url: str, query: str) -> str:
    """Build the instant-query URL, rejecting non-HTTP(S) schemes."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PrometheusError(
            f"Prometheus URL must be an http(s) URL with a host, got {base_url!r}"
        )
    root = base_url.rstrip("/")
    return f"{root}/api/v1/query?{urllib.parse.urlencode({'query': query})}"


def _token_from_env() -> str | None:
    """Bearer token from ``PAT_PROMETHEUS_TOKEN`` (never a CLI arg)."""
    token = os.environ.get(TOKEN_ENV)
    return token.strip() if token and token.strip() else None


def instant_query(
    base_url: str,
    query: str,
    token: str | None = None,
    timeout: float = 30.0,
) -> float | None:
    """
    Run a single instant PromQL query and return its scalar value.

    Returns ``None`` when the query matches no series (empty vector) or yields a
    non-finite value (``NaN`` / ``±Inf``).  Raises :class:`PrometheusError` on
    transport errors, non-success responses, or malformed payloads.
    """
    url = _build_query_url(base_url, query)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 — scheme checked above
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read())
    except (OSError, ValueError) as exc:
        raise PrometheusError(
            f"failed to query Prometheus at {base_url!r}: {exc}"
        ) from exc
    return _scalar_from_response(payload, query)


def _scalar_from_response(payload: object, query: str) -> float | None:
    """Extract the single numeric value from a Prometheus query response."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        msg = (
            payload.get("error", "unknown error")
            if isinstance(payload, dict)
            else "non-JSON response"
        )
        raise PrometheusError(f"Prometheus query {query!r} failed: {msg}")

    data = payload.get("data") or {}
    result_type = data.get("resultType")
    result = data.get("result")

    if result_type == "scalar":
        return _parse_value(result)
    if result_type == "vector":
        if not result:
            return None  # no matching series
        return _parse_value(result[0].get("value") if result[0] else None)
    raise PrometheusError(
        f"Prometheus query {query!r} returned unsupported resultType "
        f"{result_type!r} (expected scalar or vector)"
    )


def _parse_value(value_pair: object) -> float | None:
    """A Prometheus value is ``[timestamp, "stringified-number"]``."""
    try:
        raw = value_pair[1]  # type: ignore[index]
    except (TypeError, IndexError, KeyError) as exc:
        raise PrometheusError(f"malformed Prometheus value: {value_pair!r}") from exc
    if raw in ("NaN", "+Inf", "-Inf"):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise PrometheusError(f"non-numeric Prometheus value: {raw!r}") from exc


# ---------------------------------------------------------------------------
# Observation source
# ---------------------------------------------------------------------------


def fetch_observation(
    base_url: str,
    layer: str,
    rps_query: str = DEFAULT_RPS_QUERY,
    p99_query: str = DEFAULT_P99_QUERY,
    avg_query: str = DEFAULT_AVG_QUERY,
    replicas_query: str = DEFAULT_REPLICAS_QUERY,
    timeout: float = 30.0,
) -> Observation:
    """
    Scrape one sample from Prometheus and build an Observation.

    ``layer`` is the replication layer to tag the sample with (Prometheus does
    not know whether the workload is a container/pod/deployment/node).  Latency
    seconds are converted to milliseconds; ``replicas`` comes from the
    pod/target-count query.  Raises :class:`PrometheusError` when the replica
    count cannot be determined (an observation needs a replica count).
    """
    token = _token_from_env()

    def q(query: str) -> float | None:
        return instant_query(base_url, query, token=token, timeout=timeout)

    rps = q(rps_query)
    p99_seconds = q(p99_query)
    avg_seconds = q(avg_query)
    replicas_value = q(replicas_query)

    if replicas_value is None or replicas_value < 1:
        raise PrometheusError(
            f"replica/pod-count query {replicas_query!r} returned no usable value "
            f"({replicas_value!r}); cannot record without a replica count. "
            "Override it for your metrics if needed."
        )

    rps = max(0.0, rps or 0.0)  # no traffic ⇒ 0 rps is valid
    return Observation(
        timestamp=utcnow(),
        rps=rps,
        avg_latency_ms=max(0.0, (avg_seconds or 0.0) * 1000.0),
        p99_latency_ms=max(0.0, (p99_seconds or 0.0) * 1000.0),
        throughput=rps,  # rate(http_requests_total) counts served requests
        layer=layer,
        replicas=int(round(replicas_value)),
        source="prometheus",
    ).validate()
