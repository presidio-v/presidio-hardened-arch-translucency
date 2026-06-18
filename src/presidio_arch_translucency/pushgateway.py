"""
Prometheus Pushgateway target -- v0.14.0 ("Reach ephemeral contexts").

Single-shot / batch jobs (cron, CI, Kubernetes ``Job``/``CronJob``) have no
scrape endpoint for Prometheus to poll. The **Pushgateway** is Prometheus's
native answer: a job PUSHes its metrics to the gateway, and Prometheus scrapes
the gateway. ``pat export --pushgateway <url> --job <job>`` pushes the exporter's
metric set there once and exits.

This reuses the exporter's existing Prometheus **text exposition** output
verbatim -- the gateway speaks the same 0.0.4 format -- so it is a thin,
zero-dependency push (``urllib`` only), consistent with ADR-0006's posture.

Security mirrors ``otlp.py`` / ``annotate.py``: an optional bearer token is read
from ``PAT_PUSHGATEWAY_TOKEN`` only (gateways are often unauthenticated
in-cluster); HTTPS is required when a token is sent (unless ``--insecure-http``);
the URL, job, and grouping labels reject control characters and ``/`` (they are
URL path segments) and are percent-encoded.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.parse
import urllib.request

from presidio_arch_translucency import __version__

TOKEN_ENV = "PAT_PUSHGATEWAY_TOKEN"  # noqa: S105 -- env var name, not a secret
_USER_AGENT = f"pat-cli/{__version__}"
# The gateway accepts the standard Prometheus text exposition format.
_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class PushgatewayError(RuntimeError):
    """Raised when metrics cannot be pushed to the Pushgateway."""


# -- validation helpers (shared shape with otlp.py / annotate.py) --------------


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _reject_control_chars(value: str, field_name: str) -> None:
    if _has_control_chars(value):
        raise PushgatewayError(
            f"Pushgateway {field_name} must not contain control characters"
        )


def _parsed_url(base_url: str) -> urllib.parse.ParseResult:
    _reject_control_chars(base_url, "URL")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PushgatewayError(
            f"Pushgateway URL must be an http(s) URL with a host, got {base_url!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise PushgatewayError("Pushgateway URL must not include embedded credentials")
    return parsed


def _segment(value: str, field_name: str) -> str:
    """Validate and percent-encode one URL path segment (job / label / value)."""
    _reject_control_chars(value, field_name)
    if not value:
        raise PushgatewayError(f"Pushgateway {field_name} must be non-empty")
    if "/" in value:
        raise PushgatewayError(f"Pushgateway {field_name} must not contain '/'")
    return urllib.parse.quote(value, safe="")


def parse_grouping(values: list[str]) -> dict[str, str]:
    """Parse ``key=value`` grouping labels into a dict (validated)."""
    grouping: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise PushgatewayError(
                f"grouping label {item!r} must be key=value (e.g. instance=ci-7)"
            )
        key, value = item.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            raise PushgatewayError(f"grouping label {item!r} has an empty key")
        grouping[key] = value
    return grouping


def pushgateway_url(
    base_url: str, job: str, grouping: dict[str, str] | None = None
) -> str:
    """
    Build the gateway URL ``<base>/metrics/job/<job>{/<label>/<value>}``.

    Job and grouping labels/values are validated and percent-encoded.
    """
    _parsed_url(base_url)
    parts = [base_url.rstrip("/"), "metrics", "job", _segment(job, "job")]
    for key, value in (grouping or {}).items():
        parts.append(_segment(key, "grouping label"))
        parts.append(_segment(value, "grouping value"))
    return "/".join(parts)


# -- auth + outbound push ------------------------------------------------------


def _token_from_env() -> str | None:
    token = os.environ.get(TOKEN_ENV)
    if not token or not token.strip():
        return None
    if _has_control_chars(token):
        raise PushgatewayError(f"{TOKEN_ENV} must not contain control characters")
    return token.strip()


def resolve_token(base_url: str, insecure_http: bool = False) -> str | None:
    """Bearer token from ``PAT_PUSHGATEWAY_TOKEN``; HTTPS required to send it."""
    token = _token_from_env()
    if not token:
        return None
    if _has_control_chars(token):
        raise PushgatewayError(f"{TOKEN_ENV} must not contain control characters")
    parsed = _parsed_url(base_url)
    if parsed.scheme != "https" and not insecure_http:
        raise PushgatewayError(
            f"{TOKEN_ENV} requires an https Pushgateway URL; refusing to send a "
            "bearer token over cleartext HTTP (use --insecure-http for localhost)."
        )
    return token


def push(
    base_url: str,
    job: str,
    exposition: str,
    *,
    grouping: dict[str, str] | None = None,
    token: str | None = None,
    timeout: float = 10.0,
    method: str = "PUT",
) -> int:
    """
    PUT (default) the *exposition* text to the gateway and return the HTTP status.

    ``PUT`` replaces all metrics in the job/grouping; ``POST`` replaces only
    same-named metrics. Raises :class:`PushgatewayError` on transport errors or
    non-2xx responses.
    """
    url = pushgateway_url(base_url, job, grouping)
    data = exposition.encode("utf-8")
    headers = {"User-Agent": _USER_AGENT, "Content-Type": _CONTENT_TYPE}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(  # noqa: S310 -- scheme checked in pushgateway_url
        url, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        raise PushgatewayError(
            f"Pushgateway rejected the push: HTTP {exc.code}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise PushgatewayError(f"failed to push to {url!r}: {exc}") from exc
