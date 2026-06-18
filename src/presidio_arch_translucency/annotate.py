"""
Grafana annotation writer -- v0.12.0 ("Visualize & Annotate").

The first **outbound write** of the monitoring-integration arc: ``pat annotate``
posts an annotation to Grafana's HTTP API marking the current recommendation, so
the model's events show up on dashboards alongside the metrics. This is the one
place ``pat`` writes outward -- and it writes an *informational annotation*,
never infrastructure, so arc invariant A1 still holds (``pat`` informs/emits; it
does not mutate infra).

Security (mirrors the Prometheus source, decision D3):

* **Token from ``PAT_GRAFANA_TOKEN`` only** -- never a CLI arg, never logged. The
  token is *required* (Grafana's annotations API needs an editor token).
* **HTTPS required** when sending the token; ``--insecure-http`` is an explicit,
  warned opt-out for localhost development.
* URL and tags reject control characters; the annotation text is pat-generated.
* ``urllib`` only -- no client library, no new dependency.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from presidio_arch_translucency import __version__

TOKEN_ENV = "PAT_GRAFANA_TOKEN"  # noqa: S105 -- env var name, not a secret
_USER_AGENT = f"pat-cli/{__version__}"
_MAX_TAG_LEN = 100


class AnnotateError(RuntimeError):
    """Raised when an annotation cannot be built or posted."""


# -- validation helpers (shared shape with prometheus.py) ----------------------


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _reject_control_chars(value: str, field_name: str) -> None:
    if _has_control_chars(value):
        raise AnnotateError(f"Grafana {field_name} must not contain control characters")


def _parsed_url(base_url: str) -> urllib.parse.ParseResult:
    _reject_control_chars(base_url, "URL")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AnnotateError(
            f"Grafana URL must be an http(s) URL with a host, got {base_url!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise AnnotateError("Grafana URL must not include embedded credentials")
    return parsed


def _sanitize_tag(tag: str) -> str:
    tag = tag.strip()
    if not tag:
        raise AnnotateError("annotation tags must be non-empty")
    _reject_control_chars(tag, "tag")
    if len(tag) > _MAX_TAG_LEN:
        raise AnnotateError(f"annotation tag {tag!r} exceeds {_MAX_TAG_LEN} chars")
    return tag


def token_from_env() -> str | None:
    """Bearer token from ``PAT_GRAFANA_TOKEN`` (never a CLI arg)."""
    token = os.environ.get(TOKEN_ENV)
    if not token or not token.strip():
        return None
    _reject_control_chars(token, "token")
    return token.strip()


def resolve_token() -> str:
    """Return the env token, or raise -- annotations require auth."""
    token = token_from_env()
    if not token:
        raise AnnotateError(
            f"{TOKEN_ENV} is required to post Grafana annotations "
            "(token is read from the environment only, never a flag)."
        )
    return token


# -- annotation model ----------------------------------------------------------


@dataclass(frozen=True)
class Annotation:
    """A Grafana annotation payload."""

    text: str
    tags: list[str] = field(default_factory=list)
    dashboard_uid: str | None = None
    time_ms: int | None = None  # epoch ms; None -> Grafana stamps "now"

    def payload(self) -> dict:
        body: dict[str, object] = {"text": self.text, "tags": list(self.tags)}
        if self.dashboard_uid:
            body["dashboardUID"] = self.dashboard_uid
        if self.time_ms is not None:
            body["time"] = self.time_ms
        return body


def build_annotation(
    result: object,
    extra_tags: tuple[str, ...] = (),
    dashboard_uid: str | None = None,
) -> Annotation:
    """
    Build an annotation describing an ``AnalysisResult`` recommendation.

    The text and the base tags are pat-generated; only *extra_tags* and
    *dashboard_uid* come from the user and are sanitised.
    """
    best = next(
        r
        for r in result.layers  # type: ignore[attr-defined]
        if r.layer == result.recommended_layer  # type: ignore[attr-defined]
    )
    layer = result.recommended_layer.value  # type: ignore[attr-defined]
    text = (
        f"pat recommends {layer} × {result.recommended_replicas} "  # type: ignore[attr-defined]
        f"(+{best.throughput_gain_pct:.0f}% throughput) for "
        f"{result.baseline_throughput_rps:.0f} req/s baseline"  # type: ignore[attr-defined]
    )
    tags = ["pat", "pat-recommendation", f"layer:{layer}"]
    tags.extend(_sanitize_tag(t) for t in extra_tags)
    uid = _sanitize_tag(dashboard_uid) if dashboard_uid else None
    return Annotation(text=text, tags=tags, dashboard_uid=uid)


# -- outbound write ------------------------------------------------------------


def post_annotation(
    base_url: str,
    annotation: Annotation,
    *,
    token: str,
    timeout: float = 10.0,
    insecure_http: bool = False,
) -> dict:
    """
    POST *annotation* to ``<base_url>/api/annotations`` and return the response.

    Refuses to send the bearer token over cleartext HTTP unless *insecure_http*
    is set. Raises :class:`AnnotateError` on transport or non-success responses.
    """
    parsed = _parsed_url(base_url)
    if parsed.scheme != "https" and not insecure_http:
        raise AnnotateError(
            "refusing to send a bearer token over cleartext HTTP; use an https "
            "Grafana URL (or --insecure-http for localhost development)."
        )

    url = base_url.rstrip("/") + "/api/annotations"
    data = json.dumps(annotation.payload()).encode("utf-8")
    headers = {
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    req = urllib.request.Request(  # noqa: S310 -- scheme checked above
        url, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise AnnotateError(
            f"Grafana rejected the annotation: HTTP {exc.code}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise AnnotateError(f"failed to post annotation to Grafana: {exc}") from exc

    try:
        return json.loads(raw or b"{}")
    except ValueError:
        return {}
