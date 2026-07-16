"""
Autoscaler emitter -- v0.15.0 ("Close the loop").

Sixth step of the monitoring-integration arc, and its conceptual payoff:
**translucency-aware autoscaling**. The exporter publishes
``pat_predicted_recommended_replicas`` (v0.10.0 ``--predict``); this command emits
the declarative glue so an autoscaler scales a Deployment to *track that
forecast* -- the model's prediction becomes the scaling signal.

Two emit formats:

* ``keda`` (default) -- a KEDA ``ScaledObject`` with a Prometheus trigger.
  ``threshold: "1"`` makes KEDA's ``desiredReplicas = ceil(query / 1)``, so the
  Deployment's replica count equals pat's predicted recommendation.
* ``prometheus-adapter`` -- a HorizontalPodAutoscaler (v2) on an **External**
  metric (``target.type: Value``, ``value: "1"`` → the same identity), plus a
  commented Prometheus-Adapter ``externalRules`` snippet to register the metric.

**Emit-only** (arc invariant A1): ``pat`` prints YAML to stdout and never
applies, installs, or scales anything -- apply via ``kubectl`` / GitOps.

Security: target / namespace / object names are RFC 1123-validated; the
Prometheus URL and PromQL query reject control characters and are double-quoted
in the YAML; no secrets or raw user input are echoed. Hand-rolled YAML, no
dependency (like ``hpa_patch`` / ``rules``).
"""

from __future__ import annotations

import re
import urllib.parse

VALID_LAYERS: tuple[str, ...] = ("container", "pod", "deployment", "node")
VALID_FORMATS: tuple[str, ...] = ("keda", "prometheus-adapter")

#: Scaling signals (v0.22.0). ``replicas`` (default) tracks pat's predicted
#: recommendation; ``energy`` scales on the modelled energy-intensity gauge.
VALID_SIGNALS: tuple[str, ...] = ("replicas", "energy")

DEFAULT_METRIC = "pat_predicted_recommended_replicas"
#: Exporter gauge for the energy signal — must match ``export.py`` exactly.
ENERGY_METRIC = "pat_energy_per_request_joules"
_RFC1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_MAX_NAME = 63

#: Comment block prepended to energy-signal YAML. Documents the emit-only
#: posture (A1/E1) and the caveat that scaling out on J/req only amortises
#: standing power when the layer's EEI > 1.
_ENERGY_NOTES: tuple[str, ...] = (
    "# Signal: energy (J/req). Scales OUT when modelled energy-per-request",
    "# exceeds the budget threshold. Caveat: adding replicas amortises standing",
    "# power only when the layer's EEI > 1 (throughput grows faster than energy",
    "# intensity); below that, more replicas raise total watts. Pick the layer",
    "# deliberately. Emit-only (A1/E1): pat models and emits, it never actuates",
    "# power — the operator applies this and owns the scaling decision.",
)


class ScalerError(ValueError):
    """Raised on invalid names, URL, query, or replica bounds."""


# -- validation ----------------------------------------------------------------


def _validate_name(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_NAME
        or not _RFC1123.match(value)
    ):
        raise ScalerError(
            f"{field} {value!r} is not a valid Kubernetes name "
            "(RFC 1123: lowercase alphanumeric and '-', max 63 chars)"
        )
    return value


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 or ord(ch) == 127 for ch in value)


def _validate_prometheus_url(url: str) -> str:
    if _has_control_chars(url):
        raise ScalerError("Prometheus URL must not contain control characters")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ScalerError(f"Prometheus URL must be an http(s) URL, got {url!r}")
    if parsed.username is not None or parsed.password is not None:
        raise ScalerError("Prometheus URL must not include embedded credentials")
    return url


def _validate_query(query: str) -> str:
    if _has_control_chars(query):
        raise ScalerError("query must not contain control characters")
    if not query.strip():
        raise ScalerError("query must be non-empty")
    return query


def _bounds(min_replicas: int, max_replicas: int) -> tuple[int, int]:
    mn = int(min_replicas)
    if mn < 1:
        raise ScalerError("min-replicas must be >= 1")
    mx = max(int(max_replicas), mn)
    return mn, mx


def default_query(metric: str = DEFAULT_METRIC, layer: str | None = None) -> str:
    """Default PromQL: ``max(<metric>{layer="<layer>"})`` (layer filter optional)."""
    if layer is not None:
        layer = layer if layer in VALID_LAYERS else _reject_bad_layer(layer)
        return f'max({metric}{{layer="{layer}"}})'
    return f"max({metric})"


def _reject_bad_layer(layer: str) -> str:
    raise ScalerError(f"layer {layer!r} is not one of: {', '.join(VALID_LAYERS)}")


def _fmt_threshold(value: float) -> str:
    """Render a positive finite threshold for a KEDA/HPA target (rejects junk)."""
    import math  # noqa: PLC0415

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ScalerError("energy budget must be a number")
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        raise ScalerError("energy budget (J/req) must be a positive finite number")
    return f"{v:g}"


# -- YAML helpers (hand-rolled, scalars double-quoted/escaped) ------------------


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# -- KEDA ScaledObject ---------------------------------------------------------


def build_keda_scaledobject(
    target: str,
    prometheus_url: str,
    query: str,
    min_replicas: int = 1,
    max_replicas: int = 10,
    namespace: str | None = None,
    name: str | None = None,
    trigger_name: str = "pat",
    threshold: str = "1",
    notes: tuple[str, ...] = (),
) -> str:
    """Build a KEDA ``ScaledObject`` (YAML) tracking *query* against *threshold*.

    The default ``threshold="1"`` makes ``desiredReplicas = ceil(query / 1)``
    (the replicas signal). The energy signal passes the J/req budget as the
    threshold and *notes* documenting the semantics/caveats.
    """
    target = _validate_name(target, "target")
    obj = _validate_name(name, "name") if name else f"{target}-pat"
    ns = _validate_name(namespace, "namespace") if namespace else None
    _validate_name(trigger_name, "trigger-name")
    url = _validate_prometheus_url(prometheus_url)
    query = _validate_query(query)
    mn, mx = _bounds(min_replicas, max_replicas)

    lines = [
        "# Generated by `pat scaler`. Review before applying. Requires KEDA.",
        *notes,
        "apiVersion: keda.sh/v1alpha1",
        "kind: ScaledObject",
        "metadata:",
        f"  name: {_q(obj)}",
    ]
    if ns is not None:
        lines.append(f"  namespace: {_q(ns)}")
    lines += [
        "spec:",
        "  scaleTargetRef:",
        f"    name: {_q(target)}",
        f"  minReplicaCount: {mn}",
        f"  maxReplicaCount: {mx}",
        "  triggers:",
        "    - type: prometheus",
        f"      name: {_q(trigger_name)}",
        "      metadata:",
        f"        serverAddress: {_q(url)}",
        f"        query: {_q(query)}",
        # threshold -> desiredReplicas = ceil(query / threshold).
        f"        threshold: {_q(threshold)}",
    ]
    return "\n".join(lines) + "\n"


# -- Prometheus-Adapter (HPA v2 External + adapter rule snippet) ---------------


def build_prometheus_adapter(
    target: str,
    query: str,
    metric: str = DEFAULT_METRIC,
    min_replicas: int = 1,
    max_replicas: int = 10,
    namespace: str | None = None,
    name: str | None = None,
    value: str = "1",
    notes: tuple[str, ...] = (),
) -> str:
    """
    Build a HorizontalPodAutoscaler (v2) on an External metric, plus a commented
    Prometheus-Adapter ``externalRules`` snippet that registers *metric*.

    The External target ``value`` defaults to ``"1"`` (replicas signal); the
    energy signal passes the J/req budget and *notes* documenting the caveats.
    """
    target = _validate_name(target, "target")
    obj = _validate_name(name, "name") if name else f"{target}-pat"
    ns = _validate_name(namespace, "namespace") if namespace else None
    metric = _validate_query(metric)  # metric name: reject control chars
    query = _validate_query(query)
    mn, mx = _bounds(min_replicas, max_replicas)

    rule = [
        "# --- Prometheus Adapter rule (example) -------------------------------",
        "# Add to your prometheus-adapter ConfigMap under `externalRules:` so the",
        f"# {metric} series is exposed as a Kubernetes external metric:",
        "#",
        f"#   - seriesQuery: '{metric}'",
        '#     name: {matches: "", as: "' + metric + '"}',
        f"#     metricsQuery: '{query}'",
        "# ---------------------------------------------------------------------",
    ]
    hpa = [
        "apiVersion: autoscaling/v2",
        "kind: HorizontalPodAutoscaler",
        "metadata:",
        f"  name: {_q(obj)}",
    ]
    if ns is not None:
        hpa.append(f"  namespace: {_q(ns)}")
    hpa += [
        "spec:",
        "  scaleTargetRef:",
        "    apiVersion: apps/v1",
        "    kind: Deployment",
        f"    name: {_q(target)}",
        f"  minReplicas: {mn}",
        f"  maxReplicas: {mx}",
        "  metrics:",
        "    - type: External",
        "      external:",
        "        metric:",
        f"          name: {_q(metric)}",
        "        target:",
        "          type: Value",
        # value -> desiredReplicas = ceil(metricValue / value).
        f"          value: {_q(value)}",
    ]
    header = "# Generated by `pat scaler --format prometheus-adapter`. Review before applying.\n"  # noqa: E501
    notes_block = ("\n".join(notes) + "\n") if notes else ""
    return header + notes_block + "\n".join(rule) + "\n" + "\n".join(hpa) + "\n"


def build_scaler(
    fmt: str,
    target: str,
    prometheus_url: str,
    query: str,
    metric: str = DEFAULT_METRIC,
    min_replicas: int = 1,
    max_replicas: int = 10,
    namespace: str | None = None,
    name: str | None = None,
    signal: str = "replicas",
    energy_budget_j_per_req: float | None = None,
) -> str:
    """Dispatch to the requested emit format and *signal*.

    ``signal="replicas"`` (default) scales the target to track pat's predicted
    recommendation (threshold/value ``1``). ``signal="energy"`` scales out when
    the modelled ``pat_energy_per_request_joules`` gauge exceeds
    *energy_budget_j_per_req* — the threshold/value is the budget, and the YAML
    carries the :data:`_ENERGY_NOTES` caveat block.
    """
    if signal not in VALID_SIGNALS:
        raise ScalerError(
            f"signal {signal!r} is not one of: {', '.join(VALID_SIGNALS)}"
        )

    threshold = "1"
    value = "1"
    notes: tuple[str, ...] = ()
    if signal == "energy":
        if energy_budget_j_per_req is None:
            raise ScalerError(
                "--energy-budget-j-per-req is required when --signal energy"
            )
        threshold = _fmt_threshold(energy_budget_j_per_req)
        value = threshold
        notes = _ENERGY_NOTES

    if fmt == "keda":
        return build_keda_scaledobject(
            target,
            prometheus_url,
            query,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            namespace=namespace,
            name=name,
            threshold=threshold,
            notes=notes,
        )
    if fmt == "prometheus-adapter":
        return build_prometheus_adapter(
            target,
            query,
            metric=metric,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            namespace=namespace,
            name=name,
            value=value,
            notes=notes,
        )
    raise ScalerError(f"format {fmt!r} is not one of: {', '.join(VALID_FORMATS)}")
