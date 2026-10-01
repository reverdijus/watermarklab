"""JSON shapes of the HTTP API (ARCHITECTURE.md §9): every field present, snake_case."""

from __future__ import annotations

import logging
import math
from typing import Any

from wmlab.ids import parse_iso, utc_iso
from wmlab.tracking import cost_eur, derive_status, duration_s

log = logging.getLogger("wmlab.server")

TRIAL_STATES = ("COMPLETE", "PRUNED", "FAIL", "RUNNING", "WAITING")


# -- small coercions --------------------------------------------------------------------------
def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def as_float(value: Any) -> float | None:
    return float(value) if is_number(value) else None


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


def numeric_dict(value: Any) -> dict[str, float]:
    """Keep only finite numeric values."""
    if not isinstance(value, dict):
        return {}
    return {str(k): float(v) for k, v in value.items() if is_number(v)}


def nested_numeric(value: Any) -> dict[str, dict[str, float]]:
    if not isinstance(value, dict):
        return {}
    return {str(k): numeric_dict(v) for k, v in value.items() if isinstance(v, dict)}


def scalar_dict(value: Any) -> dict[str, str | int | float | bool | None]:
    """Keep scalars; stringify anything else (``RunSummary.params``)."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if item is None or isinstance(item, (str, bool, int)):
            out[str(key)] = item
        elif isinstance(item, float):
            out[str(key)] = item if math.isfinite(item) else None
        else:
            out[str(key)] = str(item)
    return out


def str_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(v) for v in value]


def str_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def epoch_of(value: Any) -> float | None:
    """Epoch of an ISO string or a number."""
    if is_number(value):
        return float(value)
    if isinstance(value, str):
        return parse_iso(value)
    return None


# -- runs -------------------------------------------------------------------------------------
def derive_manifest(raw: dict[str, Any], heartbeat: float | None, now: float) -> dict[str, Any]:
    """Add derived ``status`` (lost), ``heartbeat``, ``duration_s`` and ``cost_eur``."""
    manifest = dict(raw)
    manifest["heartbeat"] = heartbeat
    manifest["status"] = derive_status(manifest.get("status"), heartbeat, now)
    manifest["duration_s"] = duration_s(manifest, now)
    manifest["cost_eur"] = cost_eur(manifest, now)
    return manifest


def progress_view(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    return {
        "step": as_int(value.get("step")),
        "total_steps": as_int(value.get("total_steps")),
        "phase": str(value.get("phase") or ""),
        "phase_index": as_int(value.get("phase_index")),
        "n_phases": as_int(value.get("n_phases")),
    }


def parent_view(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value.get("run_id"):
        return None
    start = value.get("start_phase")
    view = {
        "run_id": str(value.get("run_id")),
        "checkpoint": str(value.get("checkpoint") or "last"),
        "start_phase": start if isinstance(start, (str, int)) and not isinstance(start, bool)
        else None,
    }
    if value.get("resume") is True:
        view["resume"] = True  # reprise au step du checkpoint (start_phase = phase localisée)
    return view


def run_summary(m: dict[str, Any]) -> dict[str, Any]:
    """``RunSummary`` from a derived manifest (see :func:`derive_manifest`)."""
    trial = m.get("trial")
    return {
        "run_id": str(m.get("run_id") or ""),
        "project": str(m.get("project") or ""),
        "experiment": str(m.get("experiment") or ""),
        "modality": str(m.get("modality") or "image"),
        "status": str(m.get("status") or "queued"),
        "host": str(m.get("host") or "local"),
        "created_at": str(m.get("created_at") or m.get("started_at") or ""),
        "started_at": opt_str(m.get("started_at")),
        "ended_at": opt_str(m.get("ended_at")),
        "duration_s": as_float(m.get("duration_s")),
        "cost_eur": round(float(m.get("cost_eur") or 0.0), 4),
        "study": opt_str(m.get("study")),
        "trial": trial if isinstance(trial, int) and not isinstance(trial, bool) else None,
        "parent": parent_view(m.get("parent")),
        "tags": str_list(m.get("tags")),
        "progress": progress_view(m.get("progress")),
        "summary": numeric_dict(m.get("summary")),
        "params": scalar_dict(m.get("params")),
        "error": opt_str(m.get("error")),
        "user": opt_str(m.get("user")),
    }


def checkpoint_view(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(entry.get("name") or ""),
        "step": as_int(entry.get("step")),
        "phase": str(entry.get("phase") or ""),
        "metrics": numeric_dict(entry.get("metrics")),
        "tags": str_list(entry.get("tags")),
        "created_at": str(entry.get("created_at") or ""),
        "size_bytes": as_int(entry.get("size_bytes")),
        "available_locally": bool(entry.get("available_locally")),
    }


def eval_view(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": str(result.get("run_id") or ""),
        "checkpoint": str(result.get("checkpoint") or ""),
        "benchmark": str(result.get("benchmark") or ""),
        "benchmark_version": as_int(result.get("benchmark_version"), 1),
        "created_at": str(result.get("created_at") or ""),
        "duration_s": as_float(result.get("duration_s")),
        "nbits": as_int(result.get("nbits")),
        "n_items": as_int(result.get("n_items")),
        "quality": numeric_dict(result.get("quality")),
        "robustness": nested_numeric(result.get("robustness")),
        "summary": numeric_dict(result.get("summary")),
    }


def sample_view(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": as_int(sample.get("step")),
        "kind": str(sample.get("kind") or "image"),
        "name": str(sample.get("name") or ""),
        "path": str(sample.get("path") or ""),
        "group": str(sample.get("group") or ""),
    }


def event_view(event: dict[str, Any]) -> dict[str, Any]:
    out = dict(event)
    out["t"] = as_float(event.get("t")) or 0.0
    out["type"] = str(event.get("type") or "")
    return out


def env_view(env: dict[str, Any]) -> dict[str, Any]:
    def obj(value: Any) -> dict[str, Any] | None:
        return value if isinstance(value, dict) else None

    launch = obj(env.get("launch"))
    if launch is not None and "git_diff" in launch:
        # The diff already travels in ``git_diff`` (it can weigh hundreds of kB): do not
        # send it twice on every poll of a running run.
        launch = {k: v for k, v in launch.items() if k != "git_diff"}
    return {
        "git": obj(env.get("git")),
        "git_diff": opt_str(env.get("git_diff")),
        "hardware": obj(env.get("hardware")),
        "pip_freeze_lines": as_int(env.get("pip_freeze_lines")),
        "command": opt_str(env.get("command")),
        "launch": launch,
    }


# -- studies ----------------------------------------------------------------------------------
def trial_view(row: dict[str, Any]) -> dict[str, Any]:
    state = str(row.get("state") or "RUNNING").upper()
    if state not in TRIAL_STATES:
        state = "FAIL" if state in ("FAILED", "ERROR") else "RUNNING"
    values = row.get("values")
    if is_number(values):
        values = [values]
    intermediate = []
    for point in row.get("intermediate") or []:
        if isinstance(point, (list, tuple)) and len(point) == 2 and all(is_number(p) for p in point):
            intermediate.append([point[0], float(point[1])])
    return {
        "number": as_int(row.get("number")),
        "state": state,
        "params": row.get("params") if isinstance(row.get("params"), dict) else {},
        "values": [float(v) if is_number(v) else None for v in values]
        if isinstance(values, list) else None,
        "intermediate": intermediate,
        "run_id": opt_str(row.get("run_id")),
        "duration_s": as_float(row.get("duration_s")),
    }


def trial_stats(trials: list[dict[str, Any]]) -> dict[str, int]:
    """Counts of trial states (trials already passed through :func:`trial_view`)."""
    states = [t["state"] for t in trials]
    return {
        "n_trials": len(trials),
        "n_complete": states.count("COMPLETE"),
        "n_pruned": states.count("PRUNED"),
        "n_failed": states.count("FAIL"),
        "n_running": states.count("RUNNING"),
    }


def objective_view(value: Any) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    direction = value.get("direction")
    return {
        "metric": str(value.get("metric") or ""),
        "direction": direction if direction in ("maximize", "minimize") else "maximize",
        "label": opt_str(value.get("label")),
        "constraint": opt_str(value.get("constraint")),
    }


def study_objectives(manifest: dict[str, Any], spec: dict[str, Any] | None = None
                     ) -> list[dict[str, Any]]:
    objectives = manifest.get("objectives")
    if not isinstance(objectives, list) or not objectives:
        objectives = (spec or {}).get("objectives") or []
    return [objective_view(o) for o in objectives if isinstance(o, dict)]


def _opt_number(value: Any) -> float | int | None:
    return value if is_number(value) else None


def study_summary(m: dict[str, Any], stats: dict[str, Any],
                  spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """``StudySummary`` from a derived study manifest and trial counts."""
    budget = m.get("budget") if isinstance(m.get("budget"), dict) else {}
    if not budget and isinstance((spec or {}).get("budget"), dict):
        budget = spec["budget"]  # type: ignore[index]
    best = []
    for item in m.get("best") or []:
        if not isinstance(item, dict):
            continue
        values = item.get("values")
        best.append({
            "trial": as_int(item.get("trial")),
            "values": [float(v) for v in values if is_number(v)] if isinstance(values, list)
            else ([float(values)] if is_number(values) else []),
            "params": item.get("params") if isinstance(item.get("params"), dict) else {},
            "run_id": opt_str(item.get("run_id")),
        })
    n_trials = as_int(stats.get("n_trials"), as_int(m.get("n_trials")))
    return {
        "study_id": str(m.get("study_id") or ""),
        "project": str(m.get("project") or ""),
        "plan": str(m.get("plan") or ""),
        "experiment": str(m.get("experiment") or ""),
        "status": str(m.get("status") or "queued"),
        "host": str(m.get("host") or "local"),
        "created_at": str(m.get("created_at") or m.get("started_at") or ""),
        "ended_at": opt_str(m.get("ended_at")),
        "n_trials": n_trials,
        "n_complete": as_int(stats.get("n_complete")),
        "n_pruned": as_int(stats.get("n_pruned")),
        "n_failed": as_int(stats.get("n_failed")),
        "n_running": as_int(stats.get("n_running")),
        "objectives": study_objectives(m, spec),
        "best": best,
        "cost_eur": round(float(m.get("cost_eur") or 0.0), 4),
        "budget": {
            "max_trials": _opt_number(budget.get("max_trials")),
            "max_hours": _opt_number(budget.get("max_hours")),
            "max_eur": _opt_number(budget.get("max_eur")),
            "parallel": max(as_int(budget.get("parallel"), 1), 1),
        },
        "user": opt_str(m.get("user")),
    }


def pareto_front(trials: list[dict[str, Any]], directions: list[str]) -> list[int]:
    """Numbers of the non-dominated COMPLETE trials (directions: maximize|minimize)."""
    n_obj = len(directions)
    points: list[tuple[int, list[float]]] = []
    for trial in trials:
        values = trial.get("values")
        if trial.get("state") != "COMPLETE" or not isinstance(values, list) or not values:
            continue
        if n_obj and len(values) != n_obj:
            continue
        if not all(is_number(v) for v in values):
            continue
        signs = [(-1.0 if (directions[i] if i < n_obj else "maximize") == "minimize" else 1.0)
                 for i in range(len(values))]
        points.append((int(trial["number"]), [s * float(v) for s, v in zip(signs, values)]))
    front = []
    for number, v in points:
        dominated = any(
            all(a >= b for a, b in zip(w, v)) and any(a > b for a, b in zip(w, v))
            for other, w in points if other != number
        )
        if not dominated:
            front.append(number)
    return sorted(front)


def _distribution(space: dict[str, Any]) -> Any:
    from optuna.distributions import (
        CategoricalDistribution,
        FloatDistribution,
        IntDistribution,
    )

    kind = space.get("type")
    if kind == "categorical":
        return CategoricalDistribution(list(space.get("choices") or []))
    if kind == "int":
        return IntDistribution(int(space["low"]), int(space["high"]), log=bool(space.get("log")),
                               step=int(space.get("step") or 1))
    step = space.get("step")
    return FloatDistribution(float(space["low"]), float(space["high"]),
                             log=bool(space.get("log")),
                             step=float(step) if step is not None else None)


def _infer_distribution(values: list[Any]) -> Any:
    from optuna.distributions import CategoricalDistribution, FloatDistribution

    if all(is_number(v) for v in values):
        low, high = min(values), max(values)
        if low < high:
            return FloatDistribution(float(low), float(high))
        return None
    try:
        choices = list(dict.fromkeys(values))
    except TypeError:
        return None
    return CategoricalDistribution(choices) if len(choices) > 1 else None


def param_importances(trials: list[dict[str, Any]], space: dict[str, Any] | None,
                      directions: list[str]) -> dict[str, float] | None:
    """fANOVA importances (optuna), only for ≥ 3 COMPLETE trials of a mono-objective study.

    Never raises: any problem (optuna missing, degenerate space…) returns ``None``.
    """
    if len(directions) != 1:
        return None
    complete = [t for t in trials if t.get("state") == "COMPLETE"
                and isinstance(t.get("values"), list) and t["values"]
                and is_number(t["values"][0]) and isinstance(t.get("params"), dict)]
    if len(complete) < 3:
        return None
    try:
        import optuna

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        keys = sorted({k for t in complete for k in t["params"]})
        dists: dict[str, Any] = {}
        for key in keys:
            try:
                spec = (space or {}).get(key)
                dist = _distribution(spec) if isinstance(spec, dict) else _infer_distribution(
                    [t["params"][key] for t in complete if key in t["params"]])
            except Exception:
                dist = None
            if dist is not None:
                dists[key] = dist
        if not dists:
            return None
        study = optuna.create_study(direction=directions[0])
        for trial in complete:
            params = {k: v for k, v in trial["params"].items() if k in dists}
            if not params:
                continue
            try:
                study.add_trial(optuna.trial.create_trial(
                    params=params, distributions={k: dists[k] for k in params},
                    value=float(trial["values"][0])))
            except Exception:
                continue
        if len(study.trials) < 3:
            return None
        importances = optuna.importance.get_param_importances(study)
        return {str(k): float(v) for k, v in importances.items()}
    except Exception as exc:
        log.debug("importances indisponibles : %s", exc)
        return None


# -- misc -------------------------------------------------------------------------------------
def last_activity(items: list[dict[str, Any]]) -> str | None:
    """Most recent of ended_at / heartbeat / started_at / created_at over manifests."""
    best: float | None = None
    for m in items:
        for key in ("ended_at", "heartbeat", "started_at", "created_at"):
            moment = epoch_of(m.get(key))
            if moment is not None:
                best = moment if best is None else max(best, moment)
                break
    return utc_iso(best) if best is not None else None
