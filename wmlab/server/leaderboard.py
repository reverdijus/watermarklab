"""Leaderboard rows (benchmark evaluations and validation summaries) and project "best".

Smoke evaluations (``eval/*__smoke.json``, ``"smoke": true``) and the validation
summaries of smoke runs (tag ``smoke``) are left out of both: a few synthetic steps say
nothing about a scheme's quality × robustness trade-off.
"""

from __future__ import annotations

from typing import Any

from wmlab.spec import parse_constraint
from wmlab.server.views import epoch_of, is_number, nested_numeric, numeric_dict

VAL_NON_QUALITY = {"loss", "bit_acc", "bit_acc_mean"}


def is_smoke_run(manifest: dict[str, Any]) -> bool:
    """True for a ``wmlab train --smoke`` run (tagged ``smoke`` by the trainer)."""
    tags = manifest.get("tags")
    return isinstance(tags, list) and "smoke" in tags


def benchmark_row(result: dict[str, Any]) -> dict[str, Any]:
    """``LeaderRow`` of an EvalResult (``project``/``experiment`` added by the index)."""
    return {
        "run_id": str(result.get("run_id") or ""),
        "project": str(result.get("project") or ""),
        "experiment": str(result.get("experiment") or ""),
        "checkpoint": str(result.get("checkpoint")) if result.get("checkpoint") else None,
        "benchmark": str(result.get("benchmark")) if result.get("benchmark") else None,
        "source": "benchmark",
        "created_at": str(result.get("created_at") or ""),
        "quality": numeric_dict(result.get("quality")),
        "summary": numeric_dict(result.get("summary")),
        "robustness": nested_numeric(result.get("robustness")),
    }


def val_row(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """``LeaderRow`` built from the ``val/*`` summary of a run (``None`` if empty)."""
    summary: dict[str, float] = {}
    quality: dict[str, float] = {}
    robustness: dict[str, dict[str, float]] = {}
    for key, value in numeric_dict(manifest.get("summary")).items():
        if not key.startswith("val/"):
            continue
        name = key[4:]
        if name.startswith("bit_acc/"):
            robustness.setdefault(name[len("bit_acc/"):], {})["bit_acc"] = value
            continue
        if "/" in name:
            continue
        summary[name] = value
        if name not in VAL_NON_QUALITY and not name.startswith("tpr"):
            quality[name] = value
    if not summary and not robustness:
        return None
    return {
        "run_id": str(manifest.get("run_id") or ""),
        "project": str(manifest.get("project") or ""),
        "experiment": str(manifest.get("experiment") or ""),
        "checkpoint": None,
        "benchmark": None,
        "source": "val",
        "created_at": str(manifest.get("created_at") or ""),
        "quality": quality,
        "summary": summary,
        "robustness": robustness,
    }


def leaderboard(runs: list[dict[str, Any]], evals: list[dict[str, Any]], *,
                project: str | None = None, benchmark: str | None = None,
                source: str | None = None) -> dict[str, Any]:
    """``Leaderboard``; ``benchmarks`` lists every benchmark of the (project-filtered) rows."""
    bench_rows = [benchmark_row(e) for e in evals if not e.get("smoke")]
    val_rows = [row for m in runs if not m.get("study") and not is_smoke_run(m)
                for row in [val_row(m)] if row]
    if project:
        bench_rows = [r for r in bench_rows if r["project"] == project]
        val_rows = [r for r in val_rows if r["project"] == project]
    benchmarks = sorted({r["benchmark"] for r in bench_rows if r["benchmark"]})
    rows: list[dict[str, Any]] = []
    if source in (None, "", "benchmark"):
        rows += [r for r in bench_rows if not benchmark or r["benchmark"] == benchmark]
    if source in (None, "", "val") and not benchmark:
        rows += val_rows
    rows.sort(key=lambda r: (epoch_of(r["created_at"]) or 0.0, r["run_id"]), reverse=True)
    return {
        "benchmarks": benchmarks,
        "quality_metrics": sorted({k for r in rows for k in r["quality"]}),
        "robustness_metrics": sorted({k for r in rows for att in r["robustness"].values()
                                      for k in att}),
        "attacks": sorted({a for r in rows for a in r["robustness"]}),
        "rows": rows,
    }


# -- project best -----------------------------------------------------------------------------
def _metric_value(values: dict[str, Any], metric: str, prefix: str = "") -> float | None:
    """Look up ``metric`` (with or without the ``val/`` prefix) in a summary dict."""
    bare = metric[4:] if metric.startswith("val/") else metric
    for key in (f"{prefix}{bare}", metric, bare):
        value = values.get(key)
        if is_number(value):
            return float(value)
    return None


def _satisfies(value: float | None, constraint: str | None) -> bool:
    if not constraint:
        return True
    if value is None:
        return False
    try:
        op, bound = parse_constraint(constraint)
    except ValueError:
        return True
    return {">=": value >= bound, "<=": value <= bound, ">": value > bound,
            "<": value < bound, "==": value == bound}[op]


def _score(value: float | None, direction: str) -> float:
    if value is None:
        return float("-inf")
    return value if direction != "minimize" else -value


def pick_best(candidates: list[dict[str, Any]], objectives: dict[str, dict[str, Any]]
              ) -> dict[str, Any] | None:
    """Best candidate: constraints satisfied first, then robustness, then quality."""
    if not candidates:
        return None
    quality, robustness = objectives["quality"], objectives["robustness"]

    def key(c: dict[str, Any]) -> tuple:
        ok = _satisfies(c["quality"], quality.get("constraint")) and \
            _satisfies(c["robustness"], robustness.get("constraint"))
        return (ok, _score(c["robustness"], robustness.get("direction", "maximize")),
                _score(c["quality"], quality.get("direction", "maximize")))

    return max(candidates, key=key)


def project_best(objectives: dict[str, dict[str, Any]], default_benchmark: str | None,
                 runs: list[dict[str, Any]], evals: list[dict[str, Any]]
                 ) -> dict[str, Any] | None:
    """``ProjectSummary.best`` from the default benchmark evals, else from val summaries."""
    q_metric = objectives["quality"]["metric"]
    r_metric = objectives["robustness"]["metric"]
    candidates = []
    if default_benchmark:
        for result in evals:
            if result.get("benchmark") != default_benchmark or result.get("smoke"):
                continue
            summary = {**numeric_dict(result.get("quality")), **numeric_dict(result.get("summary"))}
            q, r = _metric_value(summary, q_metric), _metric_value(summary, r_metric)
            if q is None and r is None:
                continue
            candidates.append({"run_id": str(result.get("run_id") or ""),
                               "checkpoint": str(result.get("checkpoint") or "") or None,
                               "quality": q, "robustness": r, "source": "benchmark"})
    if not candidates:
        for m in runs:
            if m.get("study") or is_smoke_run(m):
                continue
            summary = numeric_dict(m.get("summary"))
            q = _metric_value(summary, q_metric, "val/")
            r = _metric_value(summary, r_metric, "val/")
            if q is None and r is None:
                continue
            candidates.append({"run_id": str(m.get("run_id") or ""), "checkpoint": None,
                               "quality": q, "robustness": r, "source": "val"})
    return pick_best(candidates, objectives)
