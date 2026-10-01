"""SQLite index of run and study folders (stdlib ``sqlite3``, WAL).

The index is a cache: it can always be rebuilt from the files (``wmlab index``). Tables
``runs``, ``studies``, ``evals`` and ``checkpoints`` keep the raw JSON documents plus a
few columns used for filtering.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    sig         TEXT NOT NULL,
    project     TEXT,
    experiment  TEXT,
    modality    TEXT,
    status      TEXT,
    host        TEXT,
    study       TEXT,
    trial       INTEGER,
    created_at  TEXT,
    heartbeat   REAL,
    manifest    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_project ON runs(project);
CREATE INDEX IF NOT EXISTS runs_study ON runs(study);
CREATE TABLE IF NOT EXISTS studies (
    study_id    TEXT PRIMARY KEY,
    sig         TEXT NOT NULL,
    project     TEXT,
    plan        TEXT,
    status      TEXT,
    host        TEXT,
    created_at  TEXT,
    heartbeat   REAL,
    manifest    TEXT NOT NULL,
    stats       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evals (
    run_id      TEXT NOT NULL,
    file        TEXT NOT NULL,
    project     TEXT,
    experiment  TEXT,
    benchmark   TEXT,
    checkpoint  TEXT,
    created_at  TEXT,
    data        TEXT NOT NULL,
    PRIMARY KEY (run_id, file)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    run_id      TEXT NOT NULL,
    name        TEXT NOT NULL,
    step        INTEGER,
    data        TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
);
"""

TABLES = ("runs", "studies", "evals", "checkpoints")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


CORRUPTION_MARKERS = ("malformed", "not a database", "file is encrypted", "corrupt")


def is_corruption(exc: sqlite3.DatabaseError) -> bool:
    """True when ``exc`` means the file itself is unusable (as opposed to locked/busy)."""
    if isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return any(marker in message for marker in CORRUPTION_MARKERS)


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class IndexDB:
    """Thread-safe access to the index (one connection guarded by a lock)."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.path = Path(path)
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._generation = 0
        self._conn = self._open()

    # -- lifecycle -------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=self.timeout)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            for table in TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    def _open(self) -> sqlite3.Connection:
        try:
            return self._connect()
        except sqlite3.DatabaseError as exc:
            if not is_corruption(exc):
                raise  # e.g. "database is locked" by another live server: never delete it
            # Corrupted cache: delete it, it is rebuilt from the run folders.
            for suffix in ("", "-wal", "-shm"):
                Path(f"{self.path}{suffix}").unlink(missing_ok=True)
            return self._connect()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    @property
    def generation(self) -> int:
        """Counter incremented on every write (cache key for readers)."""
        return self._generation

    def _touch(self) -> None:
        self._generation += 1

    def clear(self) -> None:
        """Delete every row (full rebuild)."""
        with self._lock, self._conn:
            for table in TABLES:
                self._conn.execute(f"DELETE FROM {table}")
            self._touch()

    # -- signatures ------------------------------------------------------------------------
    def run_signatures(self) -> dict[str, str]:
        with self._lock:
            return dict(self._conn.execute("SELECT run_id, sig FROM runs").fetchall())

    def study_signatures(self) -> dict[str, str]:
        with self._lock:
            return dict(self._conn.execute("SELECT study_id, sig FROM studies").fetchall())

    # -- writes ----------------------------------------------------------------------------
    def upsert_run(self, run_id: str, sig: str, manifest: dict[str, Any], heartbeat: float | None,
                   evals: Iterable[tuple[str, dict[str, Any]]],
                   checkpoints: Iterable[dict[str, Any]]) -> None:
        """Replace the rows of one run (manifest, evals, checkpoints)."""
        project = manifest.get("project")
        experiment = manifest.get("experiment")
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, sig, project, experiment, modality, status,"
                " host, study, trial, created_at, heartbeat, manifest)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, sig, project, experiment, manifest.get("modality"),
                 manifest.get("status"), manifest.get("host"), manifest.get("study"),
                 _int_or_none(manifest.get("trial")), manifest.get("created_at"), heartbeat,
                 _dumps(manifest)))
            self._conn.execute("DELETE FROM evals WHERE run_id = ?", (run_id,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO evals (run_id, file, project, experiment, benchmark,"
                " checkpoint, created_at, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, file, project, experiment, result.get("benchmark"),
                  result.get("checkpoint"), result.get("created_at"), _dumps(result))
                 for file, result in evals])
            self._conn.execute("DELETE FROM checkpoints WHERE run_id = ?", (run_id,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO checkpoints (run_id, name, step, data) VALUES (?, ?, ?, ?)",
                [(run_id, str(c.get("name")), _int_or_none(c.get("step")), _dumps(c))
                 for c in checkpoints if c.get("name")])
            self._touch()

    def delete_runs(self, run_ids: Iterable[str]) -> None:
        ids = [(r,) for r in run_ids]
        if not ids:
            return
        with self._lock, self._conn:
            for table in ("runs", "evals", "checkpoints"):
                self._conn.executemany(f"DELETE FROM {table} WHERE run_id = ?", ids)
            self._touch()

    def upsert_study(self, study_id: str, sig: str, manifest: dict[str, Any],
                     heartbeat: float | None, stats: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO studies (study_id, sig, project, plan, status, host,"
                " created_at, heartbeat, manifest, stats) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (study_id, sig, manifest.get("project"), manifest.get("plan"),
                 manifest.get("status"), manifest.get("host"), manifest.get("created_at"),
                 heartbeat, _dumps(manifest), _dumps(stats)))
            self._touch()

    def delete_studies(self, study_ids: Iterable[str]) -> None:
        ids = [(s,) for s in study_ids]
        if not ids:
            return
        with self._lock, self._conn:
            self._conn.executemany("DELETE FROM studies WHERE study_id = ?", ids)
            self._touch()

    # -- reads -----------------------------------------------------------------------------
    def runs(self) -> list[dict[str, Any]]:
        """Return ``[{"manifest", "heartbeat"}]`` for every indexed run."""
        with self._lock:
            rows = self._conn.execute("SELECT manifest, heartbeat FROM runs").fetchall()
        out = []
        for manifest, heartbeat in rows:
            data = _loads(manifest, {})
            if isinstance(data, dict):
                out.append({"manifest": data, "heartbeat": heartbeat})
        return out

    def studies(self) -> list[dict[str, Any]]:
        """Return ``[{"manifest", "heartbeat", "stats"}]`` for every indexed study."""
        with self._lock:
            rows = self._conn.execute("SELECT manifest, heartbeat, stats FROM studies").fetchall()
        out = []
        for manifest, heartbeat, stats in rows:
            data = _loads(manifest, {})
            if isinstance(data, dict):
                out.append({"manifest": data, "heartbeat": heartbeat,
                            "stats": _loads(stats, {}) or {}})
        return out

    def evals(self) -> list[dict[str, Any]]:
        """Return every EvalResult with the ``project``/``experiment`` of its run."""
        with self._lock:
            rows = self._conn.execute("SELECT project, experiment, data FROM evals").fetchall()
        out = []
        for project, experiment, data in rows:
            result = _loads(data, {})
            if isinstance(result, dict):
                out.append({**result, "project": project, "experiment": experiment})
        return out

    def checkpoints(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT data FROM checkpoints WHERE run_id = ? ORDER BY step", (run_id,)).fetchall()
        return [d for d in (_loads(r[0], {}) for r in rows) if isinstance(d, dict)]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {table: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in TABLES}
