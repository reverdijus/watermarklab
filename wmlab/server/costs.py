"""GPU budget accounting (ARCHITECTURE.md §6.1, §6.4, §9 ``Costs``).

Spent = runs outside HPO studies + studies (which carry the cost of their trials)
+ evaluations (``duration_s × price``) + manual entries of ``workspace.yaml``.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from typing import Any, Iterable

from wmlab.ids import utc_iso
from wmlab.spec import WorkspaceSettings
from wmlab.server.views import as_float, epoch_of

NO_PROJECT = "(hors projet)"
MANUAL_HOST = "(manuel)"
CSV_HEADER = ["Type", "Identifiant", "Projet", "Machine", "Début", "Heures", "Montant (€)"]
KIND_LABELS = {"run": "Run", "study": "Étude HPO", "eval": "Évaluation",
               "manual": "Saisie manuelle"}


def _hours(seconds: Any) -> float:
    value = as_float(seconds)
    return max(value, 0.0) / 3600.0 if value is not None else 0.0


def _price(value: Any) -> float:
    number = as_float(value)
    return max(number, 0.0) if number is not None else 0.0


def cost_items(runs: Iterable[dict[str, Any]], studies: Iterable[dict[str, Any]],
               evals: Iterable[dict[str, Any]], settings: WorkspaceSettings) -> list[dict[str, Any]]:
    """``CostItem[]`` from derived manifests (runs/studies), eval results and manual entries.

    Evaluations written while their run was running (the trainer's final benchmark) are
    already inside the run duration, and those of trial runs inside the study: only
    evaluations launched separately (``wmlab eval`` / eval jobs) are billed on their own.
    """
    items: list[dict[str, Any]] = []
    by_run: dict[str, dict[str, Any]] = {}
    for m in runs:
        by_run[str(m.get("run_id") or "")] = m
        if m.get("study"):
            continue  # trial runs are paid by their study
        share = as_float(m.get("gpu_share"))
        items.append({
            "kind": "run", "id": str(m.get("run_id") or ""),
            "project": m.get("project") or None, "host": m.get("host") or None,
            "started_at": m.get("started_at") or None,
            "hours": round(_hours(m.get("duration_s")) * (1.0 if share is None else share), 6),
            "eur": round(float(m.get("cost_eur") or 0.0), 6),
        })
    for m in studies:
        items.append({
            "kind": "study", "id": str(m.get("study_id") or ""),
            "project": m.get("project") or None, "host": m.get("host") or None,
            "started_at": m.get("started_at") or None,
            "hours": round(_hours(m.get("duration_s")), 6),
            "eur": round(float(m.get("cost_eur") or 0.0), 6),
        })
    for result in evals:
        run_id = str(result.get("run_id") or "")
        if not billed_separately(result, by_run.get(run_id)):
            continue
        hours = _hours(result.get("duration_s"))
        items.append({
            "kind": "eval",
            "id": f"{run_id}/{result.get('checkpoint') or '?'}__{result.get('benchmark') or '?'}",
            "project": result.get("project") or None, "host": result.get("host") or None,
            "started_at": result.get("created_at") or None,
            "hours": round(hours, 6),
            "eur": round(hours * _price(result.get("price_eur_per_hour")), 6),
        })
    for i, entry in enumerate(settings.budget.manual_entries):
        items.append({
            "kind": "manual", "id": f"manual-{i + 1}", "project": entry.project,
            "host": None, "started_at": entry.date, "hours": 0.0,
            "eur": round(float(entry.eur), 6), "label": entry.label,
        })
    return items


def billed_separately(result: dict[str, Any], run: dict[str, Any] | None) -> bool:
    """False for an evaluation whose GPU time is already counted elsewhere (see cost_items)."""
    if run is not None and run.get("study"):
        return False
    if "in_run" in result:  # written by the current engine: explicit
        return not result["in_run"]
    if run is None:
        return True
    created = epoch_of(result.get("created_at"))  # older results: time window heuristic
    started = epoch_of(run.get("started_at"))
    if created is None or started is None or created < started:
        return True
    ended = epoch_of(run.get("ended_at"))
    if ended is None:
        return run.get("status") != "running"
    return created > ended


def running_rate(runs: Iterable[dict[str, Any]], studies: Iterable[dict[str, Any]],
                 extra_eur_per_hour: float = 0.0) -> float:
    """Current spending rate in €/h (running runs outside studies + running studies)."""
    rate = extra_eur_per_hour
    for m in runs:
        if m.get("status") == "running" and not m.get("study"):
            share = as_float(m.get("gpu_share"))
            rate += _price(m.get("price_eur_per_hour")) * (1.0 if share is None else share)
    for m in studies:
        if m.get("status") == "running":
            rate += _price(m.get("price_eur_per_hour"))
    return round(rate, 6)


def _group(items: list[dict[str, Any]], key: str, label: str, fallback: str
           ) -> list[dict[str, Any]]:
    eur: dict[str, float] = defaultdict(float)
    hours: dict[str, float] = defaultdict(float)
    for item in items:
        name = item.get(key) or fallback
        eur[name] += item["eur"]
        hours[name] += item["hours"]
    rows = [{label: name, "eur": round(eur[name], 2), "gpu_hours": round(hours[name], 3)}
            for name in eur]
    return sorted(rows, key=lambda r: (-r["eur"], r[label]))


def _month(item: dict[str, Any]) -> str | None:
    text = item.get("started_at")
    if isinstance(text, str) and len(text) >= 7 and text[4] == "-":
        return text[:7]
    moment = epoch_of(text)
    return utc_iso(moment)[:7] if moment is not None else None


def costs_view(items: list[dict[str, Any]], settings: WorkspaceSettings,
               running_eur_per_hour: float) -> dict[str, Any]:
    """``Costs`` response."""
    spent = round(sum(i["eur"] for i in items), 2)
    total = float(settings.budget.total_eur)
    months: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for item in items:
        month = _month(item)
        if month is not None:
            months[month][0] += item["eur"]
            months[month][1] += item["hours"]
    return {
        "total_eur": total,
        "spent_eur": spent,
        "remaining_eur": round(total - spent, 2),
        "running_eur_per_hour": round(running_eur_per_hour, 4),
        "by_project": _group(items, "project", "project", NO_PROJECT),
        "by_host": _group(items, "host", "host", MANUAL_HOST),
        "by_month": [{"month": m, "eur": round(v[0], 2), "gpu_hours": round(v[1], 3)}
                     for m, v in sorted(months.items())],
        "items": sorted(items, key=lambda i: epoch_of(i.get("started_at")) or 0.0, reverse=True),
        "budget": {"total_eur": total, "start": settings.budget.start,
                   "end": settings.budget.end},
    }


def gpu_hours(items: Iterable[dict[str, Any]]) -> float:
    return round(sum(i["hours"] for i in items if i["kind"] != "manual"), 3)


def _fr_number(value: float, digits: int) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_text(value: Any) -> str:
    """Text cell safe for spreadsheets: a leading ``= + - @`` (formula trigger) is escaped."""
    text = str(value or "")
    return "'" + text if text.startswith(FORMULA_PREFIXES) else text


def costs_csv(items: list[dict[str, Any]]) -> str:
    """CSV export (``;`` separator, French headers and decimal commas, UTF-8 BOM for Excel)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for item in sorted(items, key=lambda i: epoch_of(i.get("started_at")) or 0.0):
        identifier = (item.get("label") or item["id"]) if item["kind"] == "manual" else item["id"]
        writer.writerow([
            KIND_LABELS.get(item["kind"], item["kind"]),
            csv_text(identifier),
            csv_text(item.get("project")),
            csv_text(item.get("host")),
            csv_text(item.get("started_at")),
            _fr_number(item["hours"], 3),
            _fr_number(item["eur"], 2),
        ])
    return "﻿" + buffer.getvalue()
