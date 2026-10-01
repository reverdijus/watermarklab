"""Incremental indexing of run and study folders into the SQLite index.

A folder is re-read only when its signature changes (mtime/size of ``run.json``,
``heartbeat``, ``checkpoints/index.json``, ``eval/*.json`` — or ``study.json``,
``heartbeat``, ``trials.jsonl`` for studies). A daemon thread rescans every 5 s.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wmlab.server.context import ServerContext
from wmlab.server.db import IndexDB
from wmlab.server.views import derive_manifest, trial_stats, trial_view
from wmlab.tracking import RunReader, StudyReader, read_heartbeat, read_json

log = logging.getLogger("wmlab.server")

DEFAULT_INTERVAL_S = 5.0


def _stat_sig(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return "-"
    return f"{st.st_mtime_ns}:{st.st_size}"


def run_signature(run_dir: Path) -> str:
    """Signature of the files of a run folder that feed the index."""
    parts = [_stat_sig(run_dir / "run.json"), _stat_sig(run_dir / "heartbeat"),
             _stat_sig(run_dir / "checkpoints"), _stat_sig(run_dir / "checkpoints" / "index.json")]
    eval_dir = run_dir / "eval"
    try:
        entries = sorted(os.scandir(eval_dir), key=lambda e: e.name)
    except OSError:
        entries = []
    for entry in entries:
        if entry.name.endswith(".json"):
            parts.append(f"{entry.name}={_stat_sig(Path(entry.path))}")
    return "|".join(parts)


def study_signature(study_dir: Path) -> str:
    return "|".join(_stat_sig(study_dir / name)
                    for name in ("study.json", "heartbeat", "trials.jsonl"))


def _folders(parent: Path, marker: str) -> list[Path]:
    try:
        return [Path(e.path) for e in os.scandir(parent)
                if e.is_dir() and (Path(e.path) / marker).is_file()]
    except OSError:
        return []


@dataclass
class Snapshot:
    """Derived view of the index at one instant (manifests with status/cost computed)."""

    runs: list[dict[str, Any]] = field(default_factory=list)
    studies: list[dict[str, Any]] = field(default_factory=list)
    evals: list[dict[str, Any]] = field(default_factory=list)
    now: float = 0.0

    def run_map(self) -> dict[str, dict[str, Any]]:
        return {str(r.get("run_id")): r for r in self.runs}


class Indexer:
    """Keeps the SQLite index in sync with ``<data>/runs`` and ``<data>/studies``."""

    def __init__(self, db: IndexDB, runs_dir: Path, studies_dir: Path) -> None:
        self.db = db
        self.runs_dir = Path(runs_dir)
        self.studies_dir = Path(studies_dir)
        self.last_scan_at: float | None = None
        self._scan_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache_key: int | None = None
        self._cache_rows: tuple[list, list, list] = ([], [], [])

    @classmethod
    def for_context(cls, ctx: ServerContext, db: IndexDB | None = None) -> "Indexer":
        return cls(db or IndexDB(ctx.index_path), ctx.runs_dir, ctx.studies_dir)

    # -- scanning --------------------------------------------------------------------------
    def scan(self) -> dict[str, int]:
        """Incremental scan; return ``{"runs", "studies", "updated", "removed"}``."""
        with self._scan_lock:
            updated = removed = 0
            known = self.db.run_signatures()
            seen: set[str] = set()
            for run_dir in _folders(self.runs_dir, "run.json"):
                run_id = run_dir.name
                seen.add(run_id)
                sig = run_signature(run_dir)
                if known.get(run_id) != sig and self._index_run(run_dir, sig):
                    updated += 1
            gone = set(known) - seen
            self.db.delete_runs(gone)
            removed += len(gone)

            known = self.db.study_signatures()
            seen = set()
            for study_dir in _folders(self.studies_dir, "study.json"):
                study_id = study_dir.name
                seen.add(study_id)
                sig = study_signature(study_dir)
                if known.get(study_id) != sig and self._index_study(study_dir, sig):
                    updated += 1
            gone = set(known) - seen
            self.db.delete_studies(gone)
            removed += len(gone)
            self.last_scan_at = time.time()
            counts = self.db.counts()
            return {"runs": counts["runs"], "studies": counts["studies"], "updated": updated,
                    "removed": removed}

    def _index_run(self, run_dir: Path, sig: str) -> bool:
        manifest = read_json(run_dir / "run.json")
        if not isinstance(manifest, dict):
            return False  # partially written: retried at the next scan
        manifest.setdefault("run_id", run_dir.name)
        manifest["run_id"] = run_dir.name
        reader = RunReader(run_dir)
        evals = []
        eval_dir = run_dir / "eval"
        if eval_dir.is_dir():
            for path in sorted(eval_dir.glob("*.json")):
                result = read_json(path)
                if isinstance(result, dict):
                    result.setdefault("run_id", run_dir.name)
                    evals.append((path.name, result))
        try:
            checkpoints = reader.checkpoints()
        except Exception:
            checkpoints = []
        self.db.upsert_run(run_dir.name, sig, manifest, reader.heartbeat(), evals, checkpoints)
        return True

    def _index_study(self, study_dir: Path, sig: str) -> bool:
        manifest = read_json(study_dir / "study.json")
        if not isinstance(manifest, dict):
            return False
        manifest["study_id"] = study_dir.name
        reader = StudyReader(study_dir)
        stats = trial_stats([trial_view(t) for t in reader.trials()])
        self.db.upsert_study(study_dir.name, sig, manifest,
                             read_heartbeat(study_dir, "study.json"), stats)
        return True

    # -- background thread -----------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, interval: float = DEFAULT_INTERVAL_S) -> None:
        """Start the periodic scan thread (idempotent)."""
        if self.running:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.scan()
                except Exception:
                    log.exception("échec du scan de l'index")
                self._stop.wait(interval)

        self._thread = threading.Thread(target=loop, name="wmlab-indexer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None

    def ensure_fresh(self, max_age_s: float = 0.5) -> None:
        """Scan synchronously when no thread keeps the index fresh (or before the 1st scan)."""
        if self.last_scan_at is None or (
                not self.running and time.time() - self.last_scan_at > max_age_s):
            self.scan()

    # -- reading ---------------------------------------------------------------------------
    def snapshot(self, now: float | None = None) -> Snapshot:
        """Derived runs/studies/evals (parsed rows cached until the next index write)."""
        self.ensure_fresh()
        generation = self.db.generation
        if self._cache_key != generation:
            self._cache_rows = (self.db.runs(), self.db.studies(), self.db.evals())
            self._cache_key = generation
        now = time.time() if now is None else now
        runs_rows, study_rows, evals = self._cache_rows
        runs = [derive_manifest(r["manifest"], r["heartbeat"], now) for r in runs_rows]
        studies = []
        for row in study_rows:
            manifest = derive_manifest(row["manifest"], row["heartbeat"], now)
            manifest["_stats"] = row["stats"]
            studies.append(manifest)
        return Snapshot(runs=runs, studies=studies, evals=list(evals), now=now)


def reindex_workspace(workspace: Path | None = None) -> dict[str, Any]:
    """Rebuild the SQLite index of a workspace from its run/study folders (``wmlab index``)."""
    if workspace is None:
        from wmlab.paths import workspace_root

        workspace = workspace_root()
    ctx = ServerContext.for_workspace(Path(workspace))
    db = IndexDB(ctx.index_path)
    try:
        db.clear()
        Indexer.for_context(ctx, db).scan()
        counts = db.counts()
    finally:
        db.close()
    return {"runs": counts["runs"], "études": counts["studies"], "évaluations": counts["evals"],
            "checkpoints": counts["checkpoints"], "index": str(ctx.index_path)}
