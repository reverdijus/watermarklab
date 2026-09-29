"""Readers of run and study folders (tolerant to missing files and partial lines)."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import yaml

from wmlab.ids import parse_iso
from wmlab.tracking.checkpoints import load_index
from wmlab.tracking.env import read_env
from wmlab.tracking.io import atomic_write_text, read_json, read_jsonl, tail_lines

LOST_AFTER_S = 600.0


# -- helpers --------------------------------------------------------------------------------
def read_heartbeat(folder: Path, fallback_file: str | None = None) -> float | None:
    """Return the heartbeat epoch of a run/study folder (or the fallback file mtime)."""
    try:
        return float((Path(folder) / "heartbeat").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    if fallback_file:
        try:
            return (Path(folder) / fallback_file).stat().st_mtime
        except OSError:
            return None
    return None


def derive_status(status: str | None, heartbeat: float | None, now: float | None = None) -> str:
    """Return ``lost`` for a ``running`` item whose heartbeat is older than 600 s."""
    status = status or "queued"
    if status == "running" and heartbeat is not None:
        if (time.time() if now is None else now) - heartbeat > LOST_AFTER_S:
            return "lost"
    return status


def _end_epoch(manifest: dict[str, Any], now: float | None) -> float:
    end = parse_iso(manifest.get("ended_at"))
    if end is None:
        heartbeat = manifest.get("heartbeat")
        if isinstance(heartbeat, (int, float)) and not isinstance(heartbeat, bool):
            end = float(heartbeat)
    if end is None:
        end = time.time() if now is None else now
    return end


def duration_s(manifest: dict[str, Any], now: float | None = None) -> float | None:
    """Duration started→ended|heartbeat|now in seconds (None if not started)."""
    start = parse_iso(manifest.get("started_at"))
    if start is None:
        return None
    return max(_end_epoch(manifest, now) - start, 0.0)


def cost_eur(manifest: dict[str, Any], now: float | None = None) -> float:
    """Cost = hours(started→ended|heartbeat|now) × price_eur_per_hour × gpu_share."""
    seconds = duration_s(manifest, now)
    if not seconds:
        return 0.0
    try:
        price = float(manifest.get("price_eur_per_hour") or 0.0)
        share = manifest.get("gpu_share")
        share = 1.0 if share is None else float(share)
    except (TypeError, ValueError):
        return 0.0
    return round(seconds / 3600.0 * price * share, 6)


def uniform_indices(n: int, max_points: int) -> list[int]:
    """Evenly spaced indices keeping the first and last element."""
    if max_points <= 0 or n <= max_points:
        return list(range(n))
    if max_points == 1:
        return [n - 1]
    return sorted({round(i * (n - 1) / (max_points - 1)) for i in range(max_points)})


def lttb_indices(xs: list[float], ys: list[float], threshold: int) -> list[int]:
    """Largest-Triangle-Three-Buckets downsampling (keeps first and last point)."""
    n = len(xs)
    if threshold <= 0 or n <= threshold:
        return list(range(n))
    if threshold < 3:
        return uniform_indices(n, threshold)
    selected = [0]
    bucket = (n - 2) / (threshold - 2)
    a = 0
    for i in range(threshold - 2):
        next_start = int(math.floor((i + 1) * bucket)) + 1
        next_end = min(int(math.floor((i + 2) * bucket)) + 1, n)
        if next_start >= next_end:
            avg_x, avg_y = xs[-1], ys[-1]
        else:
            count = next_end - next_start
            avg_x = sum(xs[next_start:next_end]) / count
            avg_y = sum(ys[next_start:next_end]) / count
        start = int(math.floor(i * bucket)) + 1
        end = min(int(math.floor((i + 1) * bucket)) + 1, n - 1)
        ax, ay = xs[a], ys[a]
        best, best_area = start, -1.0
        for j in range(start, max(end, start + 1)):
            area = abs((ax - avg_x) * (ys[j] - ay) - (ax - xs[j]) * (avg_y - ay))
            if area > best_area:
                best, best_area = j, area
        selected.append(best)
        a = best
    selected.append(n - 1)
    return sorted(set(selected))


def downsample_series(steps: list[float], values: list[float], max_points: int | None
                      ) -> tuple[list[float], list[float]]:
    """Downsample a series with LTTB (first/last points kept)."""
    if not max_points or len(steps) <= max_points:
        return steps, values
    idx = lttb_indices(steps, values, max_points)
    return [steps[i] for i in idx], [values[i] for i in idx]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# -- runs -----------------------------------------------------------------------------------
class RunReader:
    """Read-only access to a run folder (§6)."""

    def __init__(self, run_dir: Path) -> None:
        self.dir = Path(run_dir)

    @property
    def run_id(self) -> str:
        return self.dir.name

    def exists(self) -> bool:
        return (self.dir / "run.json").is_file()

    def heartbeat(self) -> float | None:
        return read_heartbeat(self.dir, "run.json")

    def raw_manifest(self) -> dict[str, Any]:
        """Return ``run.json`` as written (``FileNotFoundError`` if missing)."""
        data = read_json(self.dir / "run.json")
        if not isinstance(data, dict):
            raise FileNotFoundError(f"run.json introuvable ou illisible dans {self.dir}")
        return data

    def manifest(self, now: float | None = None) -> dict[str, Any]:
        """Manifest with derived ``status`` (lost), ``heartbeat``, ``duration_s``, ``cost_eur``."""
        manifest = dict(self.raw_manifest())
        now = time.time() if now is None else now
        heartbeat = self.heartbeat()
        manifest["heartbeat"] = heartbeat
        manifest["status"] = derive_status(manifest.get("status"), heartbeat, now)
        manifest["duration_s"] = duration_s(manifest, now)
        manifest["cost_eur"] = cost_eur(manifest, now)
        return manifest

    def metrics(self, keys: list[str] | None = None, max_points: int | None = None
                ) -> dict[str, dict[str, list[float]]]:
        """Return ``{key: {"steps": [...], "values": [...]}}`` (LTTB-downsampled)."""
        wanted = set(keys) if keys else None
        series: dict[str, dict[str, list[float]]] = {}
        for row in read_jsonl(self.dir / "metrics.jsonl"):
            step = row.get("step")
            values = row.get("values")
            if not _is_number(step) or not isinstance(values, dict):
                continue
            for key, value in values.items():
                if (wanted is not None and key not in wanted) or not _is_number(value):
                    continue
                target = series.setdefault(key, {"steps": [], "values": []})
                target["steps"].append(step)
                target["values"].append(float(value))
        if max_points:
            for key, data in series.items():
                data["steps"], data["values"] = downsample_series(data["steps"], data["values"],
                                                                  max_points)
        return series

    def metric_keys(self) -> list[str]:
        keys: set[str] = set()
        for row in read_jsonl(self.dir / "metrics.jsonl"):
            values = row.get("values")
            if isinstance(values, dict):
                keys.update(values)
        return sorted(keys)

    def last_values(self) -> dict[str, dict[str, float]]:
        """Latest ``{"step", "value"}`` of every metric key."""
        last: dict[str, dict[str, float]] = {}
        for row in read_jsonl(self.dir / "metrics.jsonl"):
            values = row.get("values")
            if not isinstance(values, dict):
                continue
            for key, value in values.items():
                if _is_number(value):
                    last[key] = {"step": row.get("step"), "value": float(value)}
        return last

    def system(self, max_points: int | None = None) -> dict[str, Any]:
        """Return ``{"t": [...], "series": {key: [...]}}`` (uniform downsampling)."""
        rows = [r for r in read_jsonl(self.dir / "system.jsonl") if _is_number(r.get("t"))]
        if max_points:
            rows = [rows[i] for i in uniform_indices(len(rows), max_points)]
        keys = sorted({k for r in rows for k in r if k != "t"})
        return {
            "t": [r["t"] for r in rows],
            "series": {k: [r.get(k) if _is_number(r.get(k)) else None for r in rows] for k in keys},
        }

    def events(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = read_jsonl(self.dir / "events.jsonl")
        return rows[-limit:] if limit else rows

    def checkpoints(self) -> list[dict[str, Any]]:
        out = []
        for entry in load_index(self.dir):
            item = dict(entry)
            file = self.dir / "checkpoints" / str(entry.get("file") or f"{entry.get('name')}.pt")
            item["available_locally"] = file.is_file()
            out.append(item)
        return out

    def samples(self) -> list[dict[str, Any]]:
        return read_jsonl(self.dir / "samples" / "index.jsonl")

    def evals(self) -> list[dict[str, Any]]:
        folder = self.dir / "eval"
        if not folder.is_dir():
            return []
        results = [read_json(p) for p in sorted(folder.glob("*.json"))]
        results = [r for r in results if isinstance(r, dict)]
        return sorted(results, key=lambda r: str(r.get("created_at") or ""))

    def notes(self) -> str:
        try:
            return (self.dir / "notes.md").read_text(encoding="utf-8")
        except OSError:
            return ""

    def set_notes(self, text: str) -> None:
        atomic_write_text(self.dir / "notes.md", text)

    def logs(self, tail: int = 500) -> list[str]:
        return tail_lines(self.dir / "stdout.log", tail)

    def spec_yaml(self) -> str:
        try:
            return (self.dir / "spec.yaml").read_text(encoding="utf-8")
        except OSError:
            return ""

    def spec(self) -> dict[str, Any]:
        """Return the resolved spec as a dict (empty if missing)."""
        try:
            data = yaml.safe_load(self.spec_yaml())
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}

    def env(self) -> dict[str, Any]:
        return read_env(self.dir / "env")


def list_runs(runs_dir: Path) -> list[Path]:
    """Return run folders (containing ``run.json``), most recent id first."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    return sorted((p for p in runs_dir.iterdir() if (p / "run.json").is_file()),
                  key=lambda p: p.name, reverse=True)


# -- studies --------------------------------------------------------------------------------
class StudyReader:
    """Read-only access to an HPO study folder (§6.4)."""

    def __init__(self, study_dir: Path) -> None:
        self.dir = Path(study_dir)

    @property
    def study_id(self) -> str:
        return self.dir.name

    def exists(self) -> bool:
        return (self.dir / "study.json").is_file()

    def heartbeat(self) -> float | None:
        return read_heartbeat(self.dir, "study.json")

    def raw_manifest(self) -> dict[str, Any]:
        data = read_json(self.dir / "study.json")
        if not isinstance(data, dict):
            raise FileNotFoundError(f"study.json introuvable ou illisible dans {self.dir}")
        return data

    def manifest(self, now: float | None = None) -> dict[str, Any]:
        """Manifest with derived ``status``, ``heartbeat``, ``duration_s`` and ``cost_eur``."""
        manifest = dict(self.raw_manifest())
        now = time.time() if now is None else now
        heartbeat = self.heartbeat()
        manifest["heartbeat"] = heartbeat
        manifest["status"] = derive_status(manifest.get("status"), heartbeat, now)
        manifest["duration_s"] = duration_s(manifest, now)
        manifest["cost_eur"] = cost_eur(manifest, now)
        return manifest

    def trials(self) -> list[dict[str, Any]]:
        """Latest record of every trial (``trials.jsonl``), sorted by number."""
        latest: dict[int, dict[str, Any]] = {}
        for row in read_jsonl(self.dir / "trials.jsonl"):
            number = row.get("number")
            if isinstance(number, int) and not isinstance(number, bool):
                latest[number] = row
        return [latest[n] for n in sorted(latest)]

    def events(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows = read_jsonl(self.dir / "events.jsonl")
        return rows[-limit:] if limit else rows

    def logs(self, tail: int = 500) -> list[str]:
        return tail_lines(self.dir / "stdout.log", tail)

    def spec_yaml(self) -> str:
        try:
            return (self.dir / "spec.yaml").read_text(encoding="utf-8")
        except OSError:
            return ""

    def spec(self) -> dict[str, Any]:
        try:
            data = yaml.safe_load(self.spec_yaml())
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}


def list_studies(studies_dir: Path) -> list[Path]:
    """Return study folders (containing ``study.json``), most recent id first."""
    studies_dir = Path(studies_dir)
    if not studies_dir.is_dir():
        return []
    return sorted((p for p in studies_dir.iterdir() if (p / "study.json").is_file()),
                  key=lambda p: p.name, reverse=True)
