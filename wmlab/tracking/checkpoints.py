"""Checkpoint index, tags, retention and selectors (§6.2)."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import yaml

from wmlab.tracking.io import read_json

CHECKPOINT_FORMAT = "wmlab-ckpt-v1"


class CheckpointNotFoundError(KeyError):
    """No checkpoint matches a selector (readable French message)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def checkpoint_name(step: int) -> str:
    """Return the canonical checkpoint name, e.g. ``step_000123``."""
    return f"step_{int(step):06d}"


def index_path(run_dir: Path) -> Path:
    return Path(run_dir) / "checkpoints" / "index.json"


def load_index(run_dir: Path) -> list[dict[str, Any]]:
    """Read ``checkpoints/index.json`` (empty list if missing)."""
    data = read_json(index_path(run_dir), default=[])
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def _metric(entry: dict[str, Any], key: str) -> float | None:
    value = (entry.get("metrics") or {}).get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def best_entry(index: list[dict[str, Any]], metric: str, direction: str = "max"
               ) -> dict[str, Any] | None:
    """Return the entry with the best value of ``metric`` (earliest on ties)."""
    candidates = [(e, _metric(e, metric)) for e in index]
    candidates = [(e, v) for e, v in candidates if v is not None]
    if not candidates:
        return None
    sign = -1.0 if direction in ("min", "minimize") else 1.0
    best = candidates[0]
    for entry, value in candidates[1:]:
        if sign * value > sign * best[1]:
            best = (entry, value)
    return best[0]


def retag(index: list[dict[str, Any]], keep_best: dict[str, str]) -> None:
    """Recompute the ``last`` and ``best:<metric>`` tags in place."""
    managed = {"last"} | {f"best:{m}" for m in keep_best}
    for entry in index:
        entry["tags"] = [t for t in entry.get("tags") or [] if t not in managed]
    if not index:
        return
    last = max(index, key=lambda e: (int(e.get("step", 0)), str(e.get("created_at", ""))))
    last["tags"].append("last")
    for metric, direction in keep_best.items():
        best = best_entry(index, metric, direction)
        if best is not None:
            best["tags"].append(f"best:{metric}")


def select_retained(index: list[dict[str, Any]], keep_last: int, keep_phase_end: bool = True
                    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the index into ``(kept, deleted)`` according to the retention policy.

    Kept: the ``keep_last`` most recent (the ``last`` one is always kept), every
    ``best:*`` tagged entry, and every ``phase_end:*`` entry if ``keep_phase_end``.
    """
    by_step = sorted(index, key=lambda e: int(e.get("step", 0)), reverse=True)
    keep_ids = {id(e) for e in by_step[:max(int(keep_last), 1)]}
    for entry in index:
        tags = entry.get("tags") or []
        if "last" in tags or any(t.startswith("best:") for t in tags):
            keep_ids.add(id(entry))
        if keep_phase_end and any(t.startswith("phase_end:") for t in tags):
            keep_ids.add(id(entry))
    kept = [e for e in index if id(e) in keep_ids]
    deleted = [e for e in index if id(e) not in keep_ids]
    return kept, deleted


def _keep_best_from_spec(run_dir: Path) -> dict[str, str]:
    try:
        spec = yaml.safe_load((Path(run_dir) / "spec.yaml").read_text(encoding="utf-8")) or {}
        keep_best = (spec.get("checkpoint") or {}).get("keep_best") or {}
        return {str(k): str(v) for k, v in keep_best.items()}
    except (OSError, yaml.YAMLError, AttributeError):
        return {}


def resolve_checkpoint(run_dir: Path, selector: str) -> dict[str, Any]:
    """Resolve ``last`` / ``best:<metric>`` / ``phase_end:<phase>`` / name into an index entry.

    The returned dict is the index entry plus ``path`` (absolute) and ``available_locally``.
    """
    run_dir = Path(run_dir)
    index = load_index(run_dir)
    selector = str(selector).strip()
    names = [e.get("name") for e in index]
    if not index:
        raise CheckpointNotFoundError(f"aucun checkpoint dans le run {run_dir.name}")
    entry: dict[str, Any] | None = None
    if selector == "last":
        tagged = [e for e in index if "last" in (e.get("tags") or [])]
        entry = tagged[-1] if tagged else max(index, key=lambda e: int(e.get("step", 0)))
    elif selector.startswith("best:"):
        metric = selector[5:]
        tagged = [e for e in index if selector in (e.get("tags") or [])]
        if tagged:
            entry = tagged[-1]
        else:
            direction = _keep_best_from_spec(run_dir).get(metric, "max")
            entry = best_entry(index, metric, direction)
        if entry is None:
            raise CheckpointNotFoundError(
                f"aucun checkpoint ne porte la métrique '{metric}' dans le run {run_dir.name}")
    elif selector.startswith("phase_end:"):
        tagged = [e for e in index if selector in (e.get("tags") or [])]
        if not tagged:
            phases = sorted({t for e in index for t in e.get("tags") or []
                             if t.startswith("phase_end:")})
            raise CheckpointNotFoundError(
                f"aucun checkpoint '{selector}' dans le run {run_dir.name} — disponibles : "
                f"{', '.join(phases) if phases else '(aucun)'}")
        entry = tagged[-1]
    else:
        name = selector[:-3] if selector.endswith(".pt") else selector
        if re.fullmatch(r"\d+", name):
            name = checkpoint_name(int(name))
        matches = [e for e in index if e.get("name") == name]
        if not matches:
            raise CheckpointNotFoundError(
                f"checkpoint '{selector}' introuvable dans le run {run_dir.name} — "
                f"sélecteurs : last, best:<métrique>, phase_end:<phase> ou un nom parmi "
                f"{', '.join(str(n) for n in names)}")
        entry = matches[-1]
    result = dict(entry)
    path = (run_dir / "checkpoints" / str(entry.get("file") or f"{entry.get('name')}.pt")).resolve()
    result["path"] = str(path)
    result["available_locally"] = path.is_file()
    return result
