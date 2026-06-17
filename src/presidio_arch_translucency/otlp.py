"""
OTLP/HTTP+JSON metrics export -- v0.13.0 ("Speak OTLP"), per ADR-0006.

Encodes the exporter's metrics as an OTLP ``ExportMetricsServiceRequest`` (JSON)
and POSTs them to an OpenTelemetry Collector (or any OTLP/HTTP endpoint that
accepts ``application/json``), so Datadog / New Relic / Honeycomb / Grafana Cloud
can ingest pat data **without Prometheus** -- vendor-neutral, via the Collector.

Per **ADR-0006** this is hand-rolled: no ``opentelemetry`` SDK, no ``protobuf``,
no ``grpcio``. JSON over HTTP only, targeting a Collector. Vendor-direct
protobuf/gRPC is an explicit non-goal (add the SDK as an opt-in ``[otlp]`` extra
if ever needed). This keeps the zero-client-dependency hardened posture that the
Prometheus-text, rules-YAML, HPA-YAML, and Grafana-JSON emitters already follow.

Security mirrors ``prometheus.py`` / ``annotate.py``: a bearer token is read from
``PAT_OTLP_TOKEN`` only (optional -- collectors are usually unauthenticated
in-cluster); HTTPS is required when a token is sent (unless ``--insecure-http``);
the URL and service name reject control characters; ``urllib`` only.
"""

from __future__ import annotations

import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from presidio_arch_translucency import __version__

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

    from presidio_arch_translucency.export import Metric

TOKEN_ENV = "PAT_OTLP_TOKEN"  # noqa: S105 -- env var name, not a secret
_USER_AGENT = f"pat-cli/{__version__}"
_SCOPE_NAME = "presidio_arch_translucency"
DEFAULT_SERVICE_NAME = "pat"
_METRICS_PATH = "/v1/metrics"


class OtlpError(RuntimeError):
    """Raised when OTLP metrics cannot be built or pushed."""


# -- validation helpers (shared shape with prometheus.py / annotate.py) --------


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _reject_control_chars(value: str, field_name: str) -> None:
    if _has_control_chars(value):
        raise OtlpError(f"OTLP {field_name} must not contain control characters")


def _parsed_url(base_url: str) -> urllib.parse.ParseResult:
    _reject_control_chars(base_url, "endpoint")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise OtlpError(
            f"OTLP endpoint must be an http(s) URL with a host, got {base_url!r}"
        )
    return parsed


def metrics_url(endpoint: str) -> str:
    """Resolve the OTLP metrics URL, appending ``/v1/metrics`` if absent."""
    parsed = _parsed_url(endpoint)
    root = endpoint.rstrip("/")
    if parsed.path.rstrip("/").endswith(_METRICS_PATH):
        return root
    return root + _METRICS_PATH


# -- OTLP/JSON encoding --------------------------------------------------------


def _attributes(labels: dict[str, str]) -> list[dict]:
    return [{"key": k, "value": {"stringValue": v}} for k, v in labels.items()]


def build_otlp_payload(
    metrics: Sequence[Metric],
    *,
    service_name: str = DEFAULT_SERVICE_NAME,
    timestamp_ns: int | None = None,
) -> dict:
    """
    Encode *metrics* as an OTLP ``ExportMetricsServiceRequest`` (JSON shape).

    Every pat metric is a gauge. Non-finite samples (``NaN`` / ``±Inf``) are
    dropped -- OTLP/JSON has no representation for them -- and a metric whose
    samples all drop is omitted.
    """
    _reject_control_chars(service_name, "service name")
    ts = str(int(timestamp_ns if timestamp_ns is not None else time.time_ns()))

    otlp_metrics: list[dict] = []
    for metric in metrics:
        points: list[dict] = []
        for sample in metric.samples:
            if not math.isfinite(sample.value):
                continue
            points.append(
                {
                    "attributes": _attributes(sample.labels),
                    "timeUnixNano": ts,
                    "asDouble": sample.value,
                }
            )
        if not points:
            continue
        otlp_metrics.append(
            {
                "name": metric.name,
                "description": metric.help,
                "unit": "",
                "gauge": {"dataPoints": points},
            }
        )

    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": service_name},
                        },
                        {
                            "key": "service.version",
                            "value": {"stringValue": __version__},
                        },
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": _SCOPE_NAME, "version": __version__},
                        "metrics": otlp_metrics,
                    }
                ],
            }
        ]
    }


# -- auth + outbound push ------------------------------------------------------


def _token_from_env() -> str | None:
    token = os.environ.get(TOKEN_ENV)
    return token.strip() if token and token.strip() else None


def resolve_token(endpoint: str, insecure_http: bool = False) -> str | None:
    """
    Resolve the bearer token from ``PAT_OTLP_TOKEN`` (or ``None``).

    A token requires an HTTPS endpoint unless *insecure_http* is set, so
    credentials are never sent over cleartext by default.
    """
    token = _token_from_env()
    if not token:
        return None
    parsed = _parsed_url(endpoint)
    if parsed.scheme != "https" and not insecure_http:
        raise OtlpError(
            f"{TOKEN_ENV} requires an https OTLP endpoint; refusing to send a "
            "bearer token over cleartext HTTP (use --insecure-http for localhost)."
        )
    return token


def post_otlp(
    endpoint: str,
    payload: dict,
    *,
    token: str | None = None,
    timeout: float = 10.0,
    insecure_http: bool = False,  # noqa: ARG001 -- symmetry with resolve_token
) -> int:
    """
    POST *payload* to ``<endpoint>/v1/metrics`` and return the HTTP status.

    Raises :class:`OtlpError` on transport errors or non-2xx responses.
    """
    url = metrics_url(endpoint)
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(  # noqa: S310 -- scheme checked in metrics_url
        url, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        raise OtlpError(f"OTLP collector rejected the export: HTTP {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise OtlpError(f"failed to push OTLP metrics to {url!r}: {exc}") from exc
