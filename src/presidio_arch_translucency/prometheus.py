"""
Prometheus observation source (v0.8.0, Phase 3).

Scrapes a single workload sample from a Prometheus server's instant-query API
and turns it into an :class:`~presidio_arch_translucency.observe.Observation`
(``source="prometheus"``) that plugs straight into ``record_observation``.

Design (decisions D2 / D3):

* **Single-shot.** :func:`fetch_observation` collects *one* sample and returns;
  recurring collection is scheduled externally (cron / launchd / a Kubernetes
  CronJob), exactly like the rest of ``pat observe``.
* **Auth: env token → kubeconfig → unauthenticated.**
  :func:`_resolve_token` tries ``PAT_PROMETHEUS_TOKEN`` first, then the bearer
  token of the active kubeconfig context (``KUBECONFIG`` / ``~/.kube/config``;
  override the context with ``PAT_KUBECONFIG_CONTEXT``), then falls back to an
  unauthenticated request (in-cluster use).  The token is never accepted as a
  CLI arg and never logged; it is sent only as ``Authorization: Bearer …``.
  kubeconfig is read with a minimal, dependency-free YAML subset reader — no
  ``yaml``/``pyyaml`` import.
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
from pathlib import Path

from presidio_arch_translucency import __version__
from presidio_arch_translucency.observe import Observation, utcnow

TOKEN_ENV = "PAT_PROMETHEUS_TOKEN"  # noqa: S105 — env var name, not a secret
KUBECONFIG_ENV = "KUBECONFIG"
KUBECONFIG_CONTEXT_ENV = "PAT_KUBECONFIG_CONTEXT"
_DEFAULT_KUBECONFIG = Path("~/.kube/config")
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


# ---------------------------------------------------------------------------
# kubeconfig auth (D3 follow-on)
#
# Some clusters front Prometheus with the kube-apiserver proxy, where a bearer
# token already lives in the user's kubeconfig.  Rather than make the operator
# copy that token into ``PAT_PROMETHEUS_TOKEN`` by hand, we read it directly
# from the active context.  We deliberately avoid a YAML dependency: a kubeconfig
# uses a small, regular subset of YAML (block mappings + block sequences of
# mappings + scalars) that the reader below handles without any third-party
# parser.  Anything it can't parse degrades to "no token" — never an error.
# ---------------------------------------------------------------------------


def _kubeconfig_path() -> Path | None:
    """Locate the kubeconfig to read, or ``None`` if there is nothing to read.

    ``KUBECONFIG`` (a ``os.pathsep``-separated list) wins, first entry only;
    otherwise ``~/.kube/config``.  A missing/empty path yields ``None`` so the
    caller silently falls back to an unauthenticated request.
    """
    raw = os.environ.get(KUBECONFIG_ENV)
    if raw and raw.strip():
        first = raw.split(os.pathsep)[0].strip()
        if not first:
            return None
        path = Path(first).expanduser()
        return path if path.is_file() else None
    default = _DEFAULT_KUBECONFIG.expanduser()
    return default if default.is_file() else None


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(raw: str) -> str:
    """Unquote a scalar value (kubeconfig uses plain or quoted strings)."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return raw[1:-1]
    return raw


def _parse_yaml_subset(text: str) -> object:
    """Parse the kubeconfig YAML subset into nested ``dict``/``list``.

    Handles block mappings, block sequences of mappings, and scalar values
    (plain or quoted).  Blank lines and ``#`` comment lines are ignored.  This
    is **not** a general YAML parser — only what kubeconfig emits.
    """
    lines = [
        ln.rstrip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0)
    return value


def _parse_block(lines: list[str], i: int) -> tuple[object, int]:
    if lines[i].lstrip().startswith("- "):
        return _parse_sequence(lines, i)
    return _parse_mapping(lines, i)


def _parse_mapping(lines: list[str], i: int) -> tuple[dict, int]:
    base = _indent(lines[i])
    result: dict = {}
    while i < len(lines):
        indent = _indent(lines[i])
        if indent != base or lines[i].lstrip().startswith("- "):
            break
        key, _, rest = lines[i].strip().partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:
            result[key] = _scalar(rest)
            i += 1
            continue
        # Value is a nested block (deeper mapping) or a sibling-indent sequence.
        if i + 1 < len(lines) and (
            _indent(lines[i + 1]) > base
            or (
                _indent(lines[i + 1]) == base and lines[i + 1].lstrip().startswith("- ")
            )
        ):
            result[key], i = _parse_block(lines, i + 1)
        else:
            result[key] = None
            i += 1
    return result, i


def _parse_sequence(lines: list[str], i: int) -> tuple[list, int]:
    base = _indent(lines[i])
    items: list = []
    while i < len(lines):
        if _indent(lines[i]) != base or not lines[i].lstrip().startswith("- "):
            break
        # Re-align the "- " item so its first key sits two spaces in, letting the
        # mapping parser treat the whole item uniformly.
        item_lines = [" " * (base + 2) + lines[i].lstrip()[2:]]
        i += 1
        while i < len(lines) and _indent(lines[i]) > base:
            item_lines.append(lines[i])
            i += 1
        value, _ = _parse_block(item_lines, 0)
        items.append(value)
    return items, i


def _token_from_kubeconfig(context_override: str | None = None) -> str | None:
    """Bearer token of the active kubeconfig context, or ``None``.

    Resolves ``current-context`` (or ``context_override``) → its user → that
    user's ``token``.  Any missing piece, unreadable file, or parse failure
    degrades silently to ``None`` (client-cert-only users have no token).
    """
    path = _kubeconfig_path()
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        config = _parse_yaml_subset(text)
    except Exception:  # noqa: BLE001 — a malformed kubeconfig must not crash observe
        return None
    if not isinstance(config, dict):
        return None

    context_name = context_override or config.get("current-context")
    if not context_name or not isinstance(context_name, str):
        return None

    user_name = None
    for ctx in config.get("contexts") or []:
        if isinstance(ctx, dict) and ctx.get("name") == context_name:
            inner = ctx.get("context")
            if isinstance(inner, dict):
                user_name = inner.get("user")
            break
    if not user_name or not isinstance(user_name, str):
        return None

    for usr in config.get("users") or []:
        if isinstance(usr, dict) and usr.get("name") == user_name:
            inner = usr.get("user")
            token = inner.get("token") if isinstance(inner, dict) else None
            if token and str(token).strip():
                return str(token).strip()
            return None
    return None


def _resolve_token(url: str) -> str | None:  # noqa: ARG001 — url reserved for symmetry/mocking
    """Resolve the bearer token for ``url``: env var → kubeconfig → ``None``.

    ``PAT_PROMETHEUS_TOKEN`` keeps highest priority; otherwise the active
    kubeconfig context's token is used (override the context with
    ``PAT_KUBECONFIG_CONTEXT``); otherwise the request is unauthenticated.
    The resolved token is never logged.
    """
    env_token = _token_from_env()
    if env_token:
        return env_token
    context_override = (os.environ.get(KUBECONFIG_CONTEXT_ENV) or "").strip() or None
    return _token_from_kubeconfig(context_override)


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
    token = _resolve_token(base_url)

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
