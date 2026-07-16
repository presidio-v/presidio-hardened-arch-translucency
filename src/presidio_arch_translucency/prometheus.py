"""
Prometheus observation source (v0.8.0, Phase 3).

Scrapes a single workload sample from a Prometheus server's instant-query API
and turns it into an :class:`~presidio_arch_translucency.observe.Observation`
(``source="prometheus"``) that plugs straight into ``record_observation``.

Design (decisions D2 / D3):

* **Single-shot.** :func:`fetch_observation` collects *one* sample and returns;
  recurring collection is scheduled externally (cron / launchd / a Kubernetes
  CronJob), exactly like the rest of ``pat observe``.
* **Auth: env token -> unauthenticated.** :func:`_resolve_token` reads only
  ``PAT_PROMETHEUS_TOKEN`` and otherwise falls back to an unauthenticated
  request (in-cluster use). Tokens are never accepted as CLI args, never logged,
  and are sent only as ``Authorization: Bearer ...``. When a token is present,
  the Prometheus URL must be HTTPS so credentials are not exposed on cleartext
  transport or accidentally reused against an arbitrary HTTP endpoint.
* **urllib only.** No client library and no new dependency -- same pattern as
  ``cloud.py``.

The four PromQL queries default to the spec's metrics but are overridable so the
module works against differently-named application metrics:

* rps        rate of ``http_requests_total`` (1m), summed
* p99 (s)    ``histogram_quantile(0.99, ...)`` over ``http_request_duration_seconds``
* avg (s)    request-duration ``_sum`` / ``_count`` rates
* replicas   ``count(up == 1)``  (healthy scrape targets ~= pod count)

See the ``DEFAULT_*_QUERY`` constants for the exact PromQL.
"""

from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request

from presidio_arch_translucency import __version__
from presidio_arch_translucency.observe import (
    VALID_METERS,
    EnergyObservation,
    Observation,
    _seal_collected_energy_observation,
    utcnow,
)

TOKEN_ENV = "PAT_PROMETHEUS_TOKEN"  # noqa: S105 -- env var name, not a secret
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

# ---------------------------------------------------------------------------
# Measured-energy meter presets (v0.21.0, ADR-0011 §2 amendment)
# ---------------------------------------------------------------------------
#
# Metric names are PINNED here against current upstream docs; a comment states
# each source and the supported floor. Bumping a metric name is a deliberate
# edit, not a silent scrape of a renamed series.
#
#   * node-exporter: ``node_rapl_package_joules_total`` is the RAPL package-domain
#     joules counter, labelled by ``path`` (one series per powercap zone file).
#   * DCGM exporter: ``DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION`` is total energy in
#     **millijoules** per ``gpu``; the watts query divides the mJ/s rate by 1000.
RAPL_WATTS_QUERY = "sum(increase(node_rapl_package_joules_total[60s])) / 60"
RAPL_GATE_QUERY = "sum by (path) (increase(node_rapl_package_joules_total[60s]))"
DCGM_WATTS_QUERY = (
    "sum(increase(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION[60s])) / 1000 / 60"
)
DCGM_GATE_QUERY = "sum by (gpu) (increase(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION[60s]))"

#: Per-meter default queries. ``gate_label`` is the hardware-identity label the
#: gate query groups by (powercap path / gpu). Overridable per invocation.
ENERGY_METER_PRESETS: dict[str, dict[str, str]] = {
    "rapl": {
        "watts": RAPL_WATTS_QUERY,
        "gate": RAPL_GATE_QUERY,
        "gate_label": "path",
    },
    "dcgm": {
        "watts": DCGM_WATTS_QUERY,
        "gate": DCGM_GATE_QUERY,
        "gate_label": "gpu",
    },
}


def _meter_preset(meter: str, window_s: float) -> dict[str, str]:
    """Return pinned queries whose PromQL range matches *window_s* exactly."""
    seconds = int(window_s)
    duration = f"{seconds}s"
    if meter == "rapl":
        return {
            "watts": (
                f"sum(increase(node_rapl_package_joules_total[{duration}])) / {seconds}"
            ),
            "gate": (
                f"sum by (path) (increase(node_rapl_package_joules_total[{duration}]))"
            ),
            "gate_label": "path",
        }
    if meter == "dcgm":
        return {
            "watts": (
                "sum(increase(DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"
                f"[{duration}])) / 1000 / {seconds}"
            ),
            "gate": (
                "sum by (gpu) (increase("
                f"DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION[{duration}]))"
            ),
            "gate_label": "gpu",
        }
    raise PrometheusError(f"unknown direct hardware meter {meter!r}")


#: Measured-energy collection window bounds (seconds). The preset's increase()
#: range uses this exact window before watts are converted to recorded joules.
ENERGY_WINDOW_MIN_S = 1.0
ENERGY_WINDOW_MAX_S = 3600.0


class PrometheusError(RuntimeError):
    """Raised when a Prometheus query fails or returns malformed data."""


# ---------------------------------------------------------------------------
# HTTP / query building
# ---------------------------------------------------------------------------


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _reject_control_chars(value: str, field: str) -> None:
    if _has_control_chars(value):
        raise PrometheusError(f"Prometheus {field} must not contain control characters")


def _parsed_prometheus_url(base_url: str) -> urllib.parse.ParseResult:
    """Parse and validate a Prometheus base URL."""
    _reject_control_chars(base_url, "URL")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise PrometheusError(
            f"Prometheus URL must be an http(s) URL with a host, got {base_url!r}"
        )
    if parsed.username is not None or parsed.password is not None:
        raise PrometheusError("Prometheus URL must not include embedded credentials")
    return parsed


def _build_query_url(base_url: str, query: str) -> str:
    """Build the instant-query URL, rejecting non-HTTP(S) schemes."""
    _parsed_prometheus_url(base_url)
    _reject_control_chars(query, "query")
    root = base_url.rstrip("/")
    return f"{root}/api/v1/query?{urllib.parse.urlencode({'query': query})}"


def _token_from_env() -> str | None:
    """Bearer token from ``PAT_PROMETHEUS_TOKEN`` (never a CLI arg)."""
    token = os.environ.get(TOKEN_ENV)
    if not token or not token.strip():
        return None
    _reject_control_chars(token, "token")
    return token.strip()


def _resolve_token(url: str) -> str | None:
    """Resolve the bearer token for ``url`` from ``PAT_PROMETHEUS_TOKEN`` only."""
    env_token = _token_from_env()
    if not env_token:
        return None
    parsed = _parsed_prometheus_url(url)
    if parsed.scheme != "https":
        raise PrometheusError(
            f"{TOKEN_ENV} requires an https Prometheus URL; refusing to send a "
            "bearer token over cleartext HTTP."
        )
    return env_token


def instant_query(
    base_url: str,
    query: str,
    token: str | None = None,
    timeout: float = 30.0,
) -> float | None:
    """
    Run a single instant PromQL query and return its scalar value.

    Returns ``None`` when the query matches no series (empty vector) or yields a
    non-finite value (``NaN`` / ``+Inf`` / ``-Inf``).  Raises
    :class:`PrometheusError` on transport errors, non-success responses, or
    malformed payloads.
    """
    url = _build_query_url(base_url, query)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 -- scheme checked
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
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise PrometheusError(f"non-numeric Prometheus value: {raw!r}") from exc
    return value if math.isfinite(value) else None


def instant_query_vector(
    base_url: str,
    query: str,
    token: str | None = None,
    timeout: float = 30.0,
) -> list[tuple[dict[str, str], float]]:
    """
    Run an instant PromQL query and return **every** series with its label set.

    Like :func:`instant_query`, but returns ``[(labels, value), ...]`` — one
    tuple per matching series, its ``metric`` label dict paired with its scalar
    value. Series whose value is non-finite (``NaN`` / ``±Inf``) are skipped.
    An empty vector returns ``[]``. Raises :class:`PrometheusError` on transport
    errors, non-success responses, or a non-vector ``resultType``. Used by the
    E1a platform gate, which inspects the label sets (not just a scalar) to
    prove a real power interface exists.
    """
    url = _build_query_url(base_url, query)
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)  # noqa: S310 -- scheme checked
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read())
    except (OSError, ValueError) as exc:
        raise PrometheusError(
            f"failed to query Prometheus at {base_url!r}: {exc}"
        ) from exc
    return _vector_from_response(payload, query)


def _vector_from_response(
    payload: object, query: str
) -> list[tuple[dict[str, str], float]]:
    """Extract ``[(labels, value), ...]`` from a Prometheus vector response."""
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
    if result_type != "vector":
        raise PrometheusError(
            f"Prometheus query {query!r} returned unsupported resultType "
            f"{result_type!r} (expected vector)"
        )

    series: list[tuple[dict[str, str], float]] = []
    for item in result or []:
        if not isinstance(item, dict):
            raise PrometheusError(f"malformed Prometheus series: {item!r}")
        metric = item.get("metric")
        if metric is None:
            metric = {}
        if not isinstance(metric, dict):
            raise PrometheusError(f"malformed Prometheus metric labels: {metric!r}")
        value = _parse_value(item.get("value"))
        if value is None:  # non-finite sample -> skip
            continue
        labels = {str(key): str(val) for key, val in metric.items()}
        series.append((labels, value))
    return series


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

    rps = max(0.0, rps or 0.0)  # no traffic -> 0 rps is valid
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


# ---------------------------------------------------------------------------
# Measured-energy source (v0.21.0) — the E1a platform gate
# ---------------------------------------------------------------------------
#
# D2 interpretation recorded here: for the remote-Prometheus source, ADR-0011's
# "probe for a real power source" IS the gate query. The cluster proves it has a
# power interface by exposing real path/gpu-labelled counters; if it does
# not, we refuse and write nothing. There is no estimator fallback — a signed
# estimate would weaponise the very capture-time honesty gap ADR-0010 concedes,
# so estimator tells are rejected at the door, not detected after the fact.


def _estimator_tell(labels: dict[str, str]) -> str | None:
    """Return the estimator tell present in *labels*, or ``None``.

    Defense in depth for direct hardware presets: check the two value tells,
    normalized (strip + casefold), so casing/whitespace variants cannot slip
    through:

    * ``components_power_source == "estimator"``
    * ``cpu_architecture == "unknown"``

    Empty labels are not treated as tells because grouped PromQL cannot
    distinguish an absent label from a present-but-empty label. Kepler is
    rejected before this gate regardless of labels because its synthetic and
    attributed modes cannot be proven direct by series shape.
    """

    def _norm(value: str) -> str:
        return value.strip().casefold()

    cps = labels.get("components_power_source")
    if cps is not None and _norm(cps) == "estimator":
        return 'components_power_source="estimator"'
    arch = labels.get("cpu_architecture")
    if arch is not None and _norm(arch) == "unknown":
        return 'cpu_architecture="unknown"'
    return None


def _run_platform_gate(
    base_url: str,
    meter: str,
    gate_query: str,
    gate_label: str,
    token: str | None,
    timeout: float,
) -> None:
    """Fail-closed E1a gate: prove a real power source before recording a watt.

    Refuses (raising :class:`PrometheusError` with a distinct "no real power
    source detected" message) when the gate vector is empty, when any series
    carries an estimator tell, or — for EVERY meter — when every series has an
    empty/missing power-interface label (rapl→``path``, dcgm→``gpu``). Returns
    ``None`` (proceed) only when the cluster demonstrably
    exposes real, power-interface-labelled counters.
    """
    series = instant_query_vector(base_url, gate_query, token=token, timeout=timeout)
    if not series:
        raise PrometheusError(
            f"no real power source detected: {meter} gate query {gate_query!r} "
            "returned no series. This platform exposes no real power interface "
            "(readable RAPL zone / DCGM device); pat refuses to sign an "
            "unmeasured watt (ADR-0011 E1a). Nothing was recorded."
        )
    for labels, _value in series:
        tell = _estimator_tell(labels)
        if tell is not None:
            raise PrometheusError(
                f"no real power source detected: {meter} gate series carries the "
                f"estimator tell {tell} — a modelled reading, not a measured one. "
                "Refused at the door (ADR-0011 E1a). Nothing was recorded."
            )
    # Label proof for ALL meters (FIX P1-2): the gate proves a real power
    # interface only if at least one series carries a non-empty identity label
    # for that interface (a powercap path / gpu). If every series lacks
    # it, nothing on the cluster identifies a real power source — refuse.
    if all(not labels.get(gate_label, "").strip() for labels, _ in series):
        raise PrometheusError(
            f"no real power source detected: every {meter} gate series has an "
            f"empty/missing {gate_label!r} label — no series identifies a real "
            "power interface (readable powercap path / GPU) on this "
            "platform (ADR-0011 E1a). Nothing was recorded."
        )


def fetch_energy_observation(
    base_url: str,
    layer: str,
    meter: str,
    window_s: float = 60.0,
    watts_query: str | None = None,
    gate_query: str | None = None,
    replicas_query: str = DEFAULT_REPLICAS_QUERY,
    rps_query: str = DEFAULT_RPS_QUERY,
    timeout: float = 30.0,
) -> EnergyObservation:
    """
    Scrape one **measured** energy reading from Prometheus (E1a, fail-closed).

    1. Run the meter's GATE query and refuse unless the cluster proves a real
       power interface (see :func:`_run_platform_gate`) — nothing is recorded on
       refusal.
    2. Only then read watts from the meter's watts query: a usable ``>= 0``
       finite value is required. A *missing* metric (no series) is refused — pat
       never records a fabricated ``0`` for an absent power metric; ``0.0`` is
       accepted only when it is a genuine measured sample.
    3. ``rps`` comes from the serving ``rps`` query (``0`` allowed); ``replicas``
       from the replica-count query (required ``>= 1``, same as the serving path).
    4. ``joules = watts * window_s``. Returns a validated
       :class:`EnergyObservation`.

    **Override marking (P1-1).** The effective gate and watts queries are
    compared against the meter's pinned presets. If the caller overrode EITHER
    query away from its preset, the record's ``source`` is
    ``"prometheus-override"`` instead of ``"prometheus"``. Because ``source`` is
    hash-chained, an overridden reading is permanently distinguishable from a
    preset-attested one: the pinned-metric attestation only applies to a
    ``"prometheus"`` reading.

    **Bounded claim (P2-2, honesty caveat).** The gate value and the watts value
    come from *separate instant queries* — different scrape instants, and with
    overrides potentially different metrics entirely. The gate proves a real
    power interface exists on the cluster *at gate time*; it does NOT bind the
    watts sample to the gated series. This rides on the same capture-time honesty
    bound ADR-0010 concedes for the hash chain: the record attests what was
    recorded, not that the watt was honest (or drawn from the gated interface) at
    capture.

    ``window_s`` is bounded to ``[1, 3600]`` s. Token/HTTPS discipline is
    inherited from :func:`_resolve_token`. The analytic model never enters here.
    """
    if meter == "kepler":
        raise PrometheusError(
            "kepler workload energy is not accepted as direct measured power: "
            "current Kepler can use a synthetic CPU meter with the same metric/zone "
            "shape and proportionally attributes node energy to workloads. Use a "
            "direct node-exporter RAPL or DCGM hardware counter instead."
        )
    if meter not in VALID_METERS:
        raise PrometheusError(
            f"unknown meter {meter!r}; measured meters are {VALID_METERS!r} "
            "('manual'/'analytic' are not measured sources, ADR-0011 E1a)"
        )
    if (
        not isinstance(window_s, (int, float))
        or isinstance(window_s, bool)
        or not math.isfinite(window_s)
        or not (ENERGY_WINDOW_MIN_S <= window_s <= ENERGY_WINDOW_MAX_S)
        or not float(window_s).is_integer()
    ):
        raise PrometheusError(
            f"--energy-window-s must be a whole number of seconds between "
            f"{ENERGY_WINDOW_MIN_S:g} and {ENERGY_WINDOW_MAX_S:g}, got "
            f"{window_s!r}"
        )
    window_s = float(window_s)

    preset = _meter_preset(meter, window_s)
    effective_watts_query = watts_query or preset["watts"]
    effective_gate_query = gate_query or preset["gate"]
    gate_label = preset["gate_label"]

    # Override marking (P1-1): a query overridden away from the meter's pinned
    # preset (either the gate or the watts query) forfeits the pinned-metric
    # attestation, so the reading is recorded with a distinguishing source that
    # — being hash-chained — permanently marks it as non-preset.
    source = (
        "prometheus-override"
        if (
            effective_watts_query != preset["watts"]
            or effective_gate_query != preset["gate"]
        )
        else "prometheus"
    )

    token = _resolve_token(base_url)

    # Step 1: the fail-closed platform gate. Runs BEFORE any watt is read.
    _run_platform_gate(
        base_url, meter, effective_gate_query, gate_label, token, timeout
    )

    def q(query: str) -> float | None:
        return instant_query(base_url, query, token=token, timeout=timeout)

    # Step 2: watts. Distinguish "no series" (None -> refuse) from a real 0.0.
    watts = q(effective_watts_query)
    if watts is None:
        raise PrometheusError(
            f"watts query {effective_watts_query!r} returned no series after the "
            "power source gate passed; refusing to record a fabricated 0 for a "
            "missing power metric. Check the meter's metric name/labels."
        )
    if watts < 0:
        raise PrometheusError(
            f"watts query {effective_watts_query!r} returned a negative value "
            f"({watts!r}); a measured power draw cannot be negative."
        )

    # Step 3: workload context.
    rps_value = q(rps_query)
    rps = max(0.0, rps_value or 0.0)  # no traffic -> 0 rps is valid
    replicas_value = q(replicas_query)
    if replicas_value is None or replicas_value < 1:
        raise PrometheusError(
            f"replica/pod-count query {replicas_query!r} returned no usable value "
            f"({replicas_value!r}); cannot record without a replica count. "
            "Override it for your metrics if needed."
        )

    # Step 4: joules over the collection window; validate before returning.
    joules = watts * window_s
    return _seal_collected_energy_observation(
        EnergyObservation(
            timestamp=utcnow(),
            watts=watts,
            joules=joules,
            window_s=window_s,
            rps=rps,
            layer=layer,
            replicas=int(round(replicas_value)),
            meter=meter,
            source=source,
        )
    )
