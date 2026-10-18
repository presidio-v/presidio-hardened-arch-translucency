"""
Measured GPU energy per benchmark level (ADR-0012, Inference Arc Phase 1g).

`pat infer-benchmark --energy-gpu UUID[,UUID…]` reads the DCGM exporter's
firmware energy counter for exactly the GPUs of the layout, over the same
window and evaluation instant as the vLLM readings, so H4 can compare joules
per output token between layouts. Metric names are pinned to dcgm-exporter's
default counters (``default-counters.csv``):

  DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION  counter  mJ since driver load (primary)
  DCGM_FI_DEV_POWER_USAGE               gauge    W, cross-check of the counter
  DCGM_FI_DEV_GPU_UTIL                  gauge    %, idle and landing checks
  DCGM_FI_DEV_GPU_TEMP                  gauge    °C, context
  DCGM_FI_DEV_SM_CLOCK                  gauge    MHz, context
  DCGM_FI_DEV_POWER_MGMT_LIMIT          gauge    W, optional (not a default)

Corollary E1a holds: these are direct hardware counters behind the same gate
as `pat observe --energy-meter dcgm` (a ``gpu`` label, no estimator tell), and
they go into the unsigned benchmark report only, never into the chained
``energy_observations`` store or an ``energy-reading@1``. No query overrides.

An energy window is refused, never recorded, unless:

* every selected GPU has exactly one series over the window and no counter
  reset (driver reload);
* the counter changed at least once per :data:`MAX_CADENCE_S` seconds:
  dcgm-exporter collects every 30 s by default, a staircase on which
  ``increase()`` extrapolates up to a whole cadence at each window edge
  (run the exporter with ``--collect-interval 1000``);
* the counter agrees with the sampled power integral within
  :data:`POWER_CHECK_TOLERANCE` (a stuck or wrapped counter);
* the power limit is the same on every GPU, constant over the window, and
  the one recorded at idle (capping changes the energy per token);
* the load landed on the named GPUs: utilisation of at least
  :data:`LANDING_MIN_UTIL` % and power above the idle baseline by
  :data:`LANDING_MIN_RISE_W` W or :data:`LANDING_MIN_RISE`, whichever is
  larger. The idle baseline, which is mandatory, requires utilisation below
  :data:`IDLE_MAX_UTIL` %, so nothing else ran on them.

Energy refusals never touch the capacity verdict: H3 does not lose a level to
an exporter hiccup, and the level records why its energy is missing.

Bounded claims: GPU board energy only, no host CPU, memory, NIC or cooling,
while a price covers whole instances; hosts per layout are recorded. Which
GPUs serve the model is the operator's statement, falsified (not proven) by
the landing check. ``j_per_output_token`` divides by vLLM's in-window token
counters, which cover the same work as the joules, including requests in
flight at the window edges.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from presidio_arch_translucency.prometheus import (
    PrometheusError,
    _estimator_tell,
    _resolve_token,
    instant_query,
    instant_query_vector,
)

ENERGY_METRIC: Final[str] = "DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION"
POWER_METRIC: Final[str] = "DCGM_FI_DEV_POWER_USAGE"
UTIL_METRIC: Final[str] = "DCGM_FI_DEV_GPU_UTIL"
TEMP_METRIC: Final[str] = "DCGM_FI_DEV_GPU_TEMP"
SM_CLOCK_METRIC: Final[str] = "DCGM_FI_DEV_SM_CLOCK"
POWER_LIMIT_METRIC: Final[str] = "DCGM_FI_DEV_POWER_MGMT_LIMIT"
#: The counter must change at least this often (s): a staircase lets
#: ``increase()`` extrapolate a whole cadence at each window edge.
MAX_CADENCE_S: Final[float] = 10.0
#: Allowed disagreement between the counter and avg power × window.
POWER_CHECK_TOLERANCE: Final[float] = 0.15
#: Idle baseline: mean utilisation (%) above which the GPU was not idle.
IDLE_MAX_UTIL: Final[float] = 2.0
#: Landing: mean utilisation (%) and power rise over idle under load.
LANDING_MIN_UTIL: Final[float] = 5.0
LANDING_MIN_RISE_W: Final[float] = 20.0
LANDING_MIN_RISE: Final[float] = 0.10
#: Drift control: the repeated first level may differ in watts by this much.
DRIFT_TOLERANCE: Final[float] = 0.05
#: Power limits closer than this (W) count as equal.
POWER_LIMIT_EPSILON_W: Final[float] = 0.5

_UUID: Final[re.Pattern[str]] = re.compile(
    r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

ScalarQuery = Callable[..., "float | None"]
VectorQuery = Callable[..., "list[tuple[dict[str, str], float]]"]


class InferenceEnergyError(ValueError):
    """Raised when the GPUs cannot be measured (before load or at idle)."""


def parse_energy_gpus(
    groups: Sequence[str], tp: int, instances: int
) -> tuple[tuple[str, ...], ...]:
    """One comma-separated group of tp GPU UUIDs per engine, all distinct."""
    if len(groups) != instances:
        raise InferenceEnergyError(
            f"{len(groups)} --energy-gpu group(s) for {instances} engine(s); give "
            "one group of tp UUIDs per engine"
        )
    parsed: list[tuple[str, ...]] = []
    seen: set[str] = set()
    for group in groups:
        uuids = tuple(part.strip() for part in group.split(","))
        for uuid in uuids:
            if uuid.upper().startswith("MIG-"):
                raise InferenceEnergyError(
                    f"{uuid!r} is a MIG instance; DCGM energy is per physical GPU"
                )
            if not _UUID.fullmatch(uuid):
                raise InferenceEnergyError(
                    f"{uuid!r} is not a GPU UUID (GPU-xxxxxxxx-xxxx-xxxx-xxxx-"
                    "xxxxxxxxxxxx, as nvidia-smi -L prints it)"
                )
            if uuid.lower() in seen:
                raise InferenceEnergyError(f"GPU {uuid} is named twice")
            seen.add(uuid.lower())
        if len(uuids) != tp:
            raise InferenceEnergyError(
                f"an engine of tp={tp} has {tp} GPU(s), got {len(uuids)} in {group!r}"
            )
        parsed.append(uuids)
    return tuple(parsed)


@dataclass
class EnergyMeter:
    """The gated GPUs of one benchmark run and their idle baseline."""

    prometheus: str
    groups: tuple[tuple[str, ...], ...]
    # Resolved at call time so tests can substitute module attributes.
    scalar_query: ScalarQuery | None = field(default=None, repr=False)
    vector_query: VectorQuery | None = field(default=None, repr=False)
    labels: dict[str, dict[str, str | None]] = field(default_factory=dict)
    idle: dict[str, dict] | None = None

    @property
    def uuids(self) -> tuple[str, ...]:
        return tuple(uuid for group in self.groups for uuid in group)

    def _selector(self) -> str:
        # UUIDs are validated hex and hyphens: nothing to escape.
        return '{UUID=~"' + "|".join(self.uuids) + '"}'

    def _token(self) -> str | None:
        return _resolve_token(self.prometheus)

    def _vector(self) -> VectorQuery:
        return self.vector_query or instant_query_vector

    def _scalar(self) -> ScalarQuery:
        return self.scalar_query or instant_query

    # -- gate --------------------------------------------------------------

    def gate(self, eval_time: float | None = None) -> None:
        """E1a before any load: one real, labelled series per named GPU."""
        try:
            series = self._vector()(
                self.prometheus,
                f"{ENERGY_METRIC}{self._selector()}",
                token=self._token(),
                eval_time=eval_time,
            )
        except PrometheusError as exc:
            raise InferenceEnergyError(str(exc)) from exc
        by_uuid: dict[str, list[dict[str, str]]] = {}
        for labels, _value in series:
            tell = _estimator_tell(labels)
            if tell is not None:
                raise InferenceEnergyError(
                    f"no real power source detected: a DCGM series carries the "
                    f"estimator tell {tell}; pat never records a modelled watt"
                )
            by_uuid.setdefault(labels.get("UUID", ""), []).append(labels)
        labels_by_uuid: dict[str, dict[str, str | None]] = {}
        for engine, group in enumerate(self.groups):
            hosts: set[str] = set()
            for uuid in group:
                found = by_uuid.get(uuid, [])
                if len(found) != 1:
                    raise InferenceEnergyError(
                        f"{len(found)} {ENERGY_METRIC} series for GPU {uuid}; "
                        "exactly one is needed (is dcgm-exporter scraped once, by "
                        "this Prometheus, and MIG off on that GPU?)"
                    )
                labels = found[0]
                if not labels.get("gpu", "").strip():
                    raise InferenceEnergyError(
                        f"no real power source detected: the series for {uuid} "
                        "has no gpu label"
                    )
                host = (labels.get("Hostname") or "").strip()
                if not host:
                    raise InferenceEnergyError(
                        f"the series for {uuid} has no Hostname label; pat records "
                        "hosts per layout"
                    )
                hosts.add(host)
                labels_by_uuid[uuid] = {
                    "engine": str(engine),
                    "gpu": labels["gpu"],
                    "host": host,
                    "model_name": labels.get("modelName"),
                    "driver": labels.get("DCGM_FI_DRIVER_VERSION"),
                }
            if len(hosts) != 1:
                raise InferenceEnergyError(
                    f"engine {engine}'s GPUs sit on {len(hosts)} hosts; a tensor-"
                    "parallel group never spans hosts"
                )
        models = {entry["model_name"] for entry in labels_by_uuid.values()}
        if len(models) != 1:
            raise InferenceEnergyError(
                f"the named GPUs are different models ({sorted(map(str, models))})"
            )
        self.labels = labels_by_uuid

    # -- windows -----------------------------------------------------------

    def _queries(self, window_s: int) -> dict[str, str]:
        sel = self._selector()
        w = f"[{window_s}s]"

        def by(agg: str, fn: str, metric: str) -> str:
            return f"{agg} by (UUID) ({fn}({metric}{sel}{w}))"

        return {
            "energy_mj": by("sum", "increase", ENERGY_METRIC),
            "changes": by("sum", "changes", ENERGY_METRIC),
            "resets": by("sum", "resets", ENERGY_METRIC),
            "series": f"count by (UUID) (count_over_time({ENERGY_METRIC}{sel}{w}))",
            "samples": by("sum", "count_over_time", ENERGY_METRIC),
            "power_mean_w": by("avg", "avg_over_time", POWER_METRIC),
            "power_max_w": by("max", "max_over_time", POWER_METRIC),
            "util_mean": by("avg", "avg_over_time", UTIL_METRIC),
            "temp_mean_c": by("avg", "avg_over_time", TEMP_METRIC),
            "sm_clock_mean_mhz": by("avg", "avg_over_time", SM_CLOCK_METRIC),
            "sm_clock_min_mhz": by("min", "min_over_time", SM_CLOCK_METRIC),
            "power_limit_min_w": by("min", "min_over_time", POWER_LIMIT_METRIC),
            "power_limit_max_w": by("max", "max_over_time", POWER_LIMIT_METRIC),
        }

    def read(self, window_s: int, eval_time: float) -> list[dict]:
        """Per-GPU readings of one window, in engine order. Raises on no data."""
        token = self._token()
        values: dict[str, dict[str, float]] = {}
        try:
            for name, query in self._queries(window_s).items():
                for labels, value in self._vector()(
                    self.prometheus, query, token=token, eval_time=eval_time
                ):
                    values.setdefault(labels.get("UUID", ""), {})[name] = value
        except PrometheusError as exc:
            raise InferenceEnergyError(str(exc)) from exc
        rows = []
        for uuid in self.uuids:
            got = values.get(uuid, {})
            missing = [
                name
                for name in ("energy_mj", "changes", "resets", "series")
                + ("power_mean_w", "util_mean")
                if name not in got
            ]
            if missing:
                raise InferenceEnergyError(
                    f"GPU {uuid} has no {', '.join(missing)} over the window"
                )
            limit_lo = got.get("power_limit_min_w")
            limit_hi = got.get("power_limit_max_w")
            joules = got["energy_mj"] / 1e3
            rows.append(
                {
                    "uuid": uuid,
                    **{
                        key: self.labels.get(uuid, {}).get(key)
                        for key in ("engine", "gpu", "host")
                    },
                    "joules": joules,
                    "watts": joules / window_s,
                    "changes": got["changes"],
                    "resets": got["resets"],
                    "series": got["series"],
                    "samples": got.get("samples"),
                    "power_mean_w": got["power_mean_w"],
                    "power_max_w": got.get("power_max_w"),
                    "power_check": (
                        got["power_mean_w"] * window_s / joules - 1.0
                        if joules > 0
                        else None
                    ),
                    "util_mean": got["util_mean"],
                    "temp_mean_c": got.get("temp_mean_c"),
                    "sm_clock_mean_mhz": got.get("sm_clock_mean_mhz"),
                    "sm_clock_min_mhz": got.get("sm_clock_min_mhz"),
                    "power_limit_w": limit_hi,
                    "power_limit_varied": (
                        limit_lo is not None
                        and limit_hi is not None
                        and limit_hi - limit_lo > POWER_LIMIT_EPSILON_W
                    ),
                }
            )
        return rows

    def problems(self, rows: list[dict], window_s: int) -> list[str]:
        """Why a window's counters cannot be trusted (empty when they can)."""
        found: list[str] = []
        min_changes = window_s / MAX_CADENCE_S
        for row in rows:
            uuid = row["uuid"]
            if row["series"] != 1:
                found.append(f"{uuid}: {row['series']:g} series over the window")
            if row["resets"] > 0:
                found.append(f"{uuid}: energy counter reset (driver reload)")
            if row["changes"] < min_changes:
                # changes() is at most samples − 1, so a slow scrape caps it.
                samples = row.get("samples")
                knob = (
                    "Prometheus scrape interval too long"
                    if samples is not None and samples - 1 < min_changes
                    else "dcgm-exporter --collect-interval too long"
                )
                found.append(
                    f"{uuid}: counter changed {row['changes']:g} times in "
                    f"{window_s}s"
                    + ("" if samples is None else f" ({samples:g} samples)")
                    + f", under one per {MAX_CADENCE_S:g}s ({knob})"
                )
            if row["joules"] <= 0:
                found.append(f"{uuid}: no energy recorded")
            elif abs(row["power_check"]) > POWER_CHECK_TOLERANCE:
                found.append(
                    f"{uuid}: counter and sampled power disagree by "
                    f"{row['power_check']:+.0%}"
                )
            if row["power_limit_varied"]:
                found.append(f"{uuid}: power limit changed during the window")
        limits = [row["power_limit_w"] for row in rows]
        known = [limit for limit in limits if limit is not None]
        if known and (
            len(known) != len(limits) or max(known) - min(known) > POWER_LIMIT_EPSILON_W
        ):
            found.append("the GPUs do not share one power limit")
        if self.idle is not None:
            for row in rows:
                idle = self.idle.get(row["uuid"], {}).get("power_limit_w")
                limit = row["power_limit_w"]
                if (idle is None) != (limit is None) or (
                    idle is not None
                    and limit is not None
                    and abs(limit - idle) > POWER_LIMIT_EPSILON_W
                ):
                    found.append(f"{row['uuid']}: power limit differs from idle")
        return found

    # -- idle baseline -----------------------------------------------------

    def measure_idle(self, window_s: int, eval_time: float) -> dict[str, dict]:
        """Per-GPU idle reading; refuses GPUs that were busy or unmeasurable."""
        rows = self.read(window_s, eval_time)
        found = self.problems(rows, window_s)
        for row in rows:
            if row["util_mean"] > IDLE_MAX_UTIL:
                found.append(
                    f"{row['uuid']}: {row['util_mean']:.1f}% utilisation with no "
                    "load from pat (another workload shares the GPU)"
                )
        if found:
            raise InferenceEnergyError("idle baseline refused: " + "; ".join(found))
        return {
            row["uuid"]: {
                key: row[key]
                for key in (
                    "watts",
                    "util_mean",
                    "temp_mean_c",
                    "sm_clock_mean_mhz",
                    "power_limit_w",
                )
            }
            for row in rows
        }

    # -- levels ------------------------------------------------------------

    def level(
        self,
        window_s: int,
        eval_time: float,
        vllm_selector: str,
        steady: bool,
        engines: int = 1,
    ) -> dict:
        """One level's energy record. Never raises: a refusal is recorded.

        ``engines`` is how many vLLM engines serve the layout: the token
        counters must come from exactly that many series with no reset, or
        another server of the same model (or a restart) would skew J/token
        even when the capacity window was refused for another reason.
        """
        record: dict = {
            "verdict": "refused",
            "reason": None,
            "steady": steady,
            "window_s": window_s,
            "eval_time": eval_time,
            "gpus": None,
            "joules": None,
            "watts": None,
            "output_tokens": None,
            "prompt_tokens": None,
            "completions": None,
            "j_per_output_token": None,
            "j_per_request": None,
        }
        try:
            rows = self.read(window_s, eval_time)
        except InferenceEnergyError as exc:
            record["reason"] = str(exc)
            return record
        record["gpus"] = rows
        found = self.problems(rows, window_s)
        for row in rows:
            idle = (self.idle or {}).get(row["uuid"], {}).get("watts")
            if row["util_mean"] < LANDING_MIN_UTIL or (
                idle is not None
                and row["watts"]
                < idle + max(LANDING_MIN_RISE_W, LANDING_MIN_RISE * idle)
            ):
                found.append(f"{row['uuid']}: the load did not land on this GPU")
        joules = sum(row["joules"] for row in rows)
        record["joules"] = joules
        record["watts"] = joules / window_s
        w = f"[{window_s}s]"
        token = self._token()
        try:
            counts = {
                name: self._scalar()(
                    self.prometheus,
                    f"sum(increase({metric}{vllm_selector}{w}))",
                    token=token,
                    eval_time=eval_time,
                )
                for name, metric in (
                    ("output_tokens", "vllm:generation_tokens_total"),
                    ("prompt_tokens", "vllm:prompt_tokens_total"),
                    ("completions", "vllm:request_success_total"),
                )
            }
            token_series = self._scalar()(
                self.prometheus,
                "count(count_over_time("
                f"vllm:generation_tokens_total{vllm_selector}{w}))",
                token=token,
                eval_time=eval_time,
            )
            token_resets = self._scalar()(
                self.prometheus,
                f"sum(resets(vllm:generation_tokens_total{vllm_selector}{w}))",
                token=token,
                eval_time=eval_time,
            )
        except PrometheusError as exc:
            counts, token_series, token_resets = {}, None, None
            found.append(f"vLLM token counters: {exc}")
        if counts and token_series != engines:
            found.append(
                f"{0 if token_series is None else token_series:g} vLLM token "
                f"counter series for {engines} engine(s): another server of this "
                "model shares the Prometheus, or an engine restarted"
            )
        if token_resets:
            found.append("vLLM token counters reset during the window")
        record.update(counts)
        output = counts.get("output_tokens")
        completions = counts.get("completions")
        if output is None or not math.isfinite(output) or output <= 0:
            found.append("no output tokens counted by vLLM over the window")
        else:
            record["j_per_output_token"] = joules / output
        if completions is not None and completions > 0:
            record["j_per_request"] = joules / completions
        if found:
            record["reason"] = "; ".join(found)
        else:
            record["verdict"] = "recorded"
        return record

    def describe(self) -> dict:
        """The run-level energy section of the report (config, idle, drift)."""
        return {
            "meter": "dcgm",
            "metric": ENERGY_METRIC,
            "gpus": [list(group) for group in self.groups],
            "labels": self.labels,
            "hosts": len({entry["host"] for entry in self.labels.values()}),
            "max_cadence_s": MAX_CADENCE_S,
            "scope": "GPU board energy only; no host CPU, memory, NIC or cooling",
            "idle_before": self.idle,
            "idle_after": None,
            "drift_control": None,
        }


def drift_control(first: dict | None, repeat: dict | None) -> dict:
    """Compare the first level's watts with its repeat at the end of the run."""
    first_w = (
        (first or {}).get("watts")
        if (first or {}).get("verdict") == ("recorded")
        else None
    )
    repeat_w = (
        (repeat or {}).get("watts")
        if (repeat or {}).get("verdict") == ("recorded")
        else None
    )
    drift = (
        repeat_w / first_w - 1.0
        if first_w is not None and repeat_w is not None and first_w > 0
        else None
    )
    return {
        "watts_first": first_w,
        "watts_repeat": repeat_w,
        "drift": drift,
        "drifted": None if drift is None else abs(drift) > DRIFT_TOLERANCE,
        "tolerance": DRIFT_TOLERANCE,
    }
