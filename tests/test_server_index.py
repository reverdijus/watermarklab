"""SQLite index: incremental scan, removal, corrupted file, ``reindex_workspace``, views."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from helpers_server import make_fake_run, make_study, patch_manifest, populate
from wmlab.server.costs import costs_csv
from wmlab.server.db import IndexDB
from wmlab.server.indexer import Indexer, reindex_workspace
from wmlab.server.views import pareto_front, trial_view
from wmlab.tracking import atomic_write_json


def test_incremental_scan(workspace: Path):
    paths = populate(workspace)
    db = IndexDB(workspace / ".wmlab" / "index.sqlite")
    indexer = Indexer(db, paths["runs"], paths["studies"])
    first = indexer.scan()
    assert first == {"runs": 4, "studies": 1, "updated": 5, "removed": 0}
    assert indexer.scan()["updated"] == 0  # nothing changed
    counts = db.counts()
    assert counts["evals"] == 2 and counts["checkpoints"] == 8
    assert [c["name"] for c in db.checkpoints("r1")] == ["step_000010", "step_000030"]

    time.sleep(0.01)
    patch_manifest(paths["runs"] / "r1", tags=["nouveau"])
    stats = indexer.scan()
    assert stats["updated"] == 1
    snap = indexer.snapshot()
    r1 = snap.run_map()["r1"]
    assert r1["tags"] == ["nouveau"] and r1["status"] == "completed"
    assert snap.run_map()["r3"]["status"] == "running"
    assert {e["run_id"] for e in snap.evals} == {"r1", "r2"}
    assert snap.evals[0]["project"] == "demo"
    assert snap.studies[0]["_stats"]["n_complete"] == 4

    atomic_write_json(paths["runs"] / "r2" / "eval" / "extra__image_v1.json",
                      {"run_id": "r2", "checkpoint": "extra", "benchmark": "image_v1"})
    assert indexer.scan()["updated"] == 1
    assert db.counts()["evals"] == 3

    shutil.rmtree(paths["runs"] / "r1")
    shutil.rmtree(paths["studies"] / "s1")
    stats = indexer.scan()
    assert stats["removed"] == 2 and stats["runs"] == 3 and stats["studies"] == 0
    assert db.counts()["evals"] == 2
    db.close()


def test_partial_run_json_is_retried(workspace: Path):
    runs = workspace / ".wmlab" / "runs"
    (runs / "half").mkdir(parents=True)
    (runs / "half" / "run.json").write_text('{"run_id": "half", "sta', encoding="utf-8")
    indexer = Indexer(IndexDB(workspace / ".wmlab" / "index.sqlite"), runs,
                      workspace / ".wmlab" / "studies")
    assert indexer.scan()["runs"] == 0
    time.sleep(0.01)
    atomic_write_json(runs / "half" / "run.json", {"run_id": "half", "status": "running"})
    assert indexer.scan()["runs"] == 1


def test_periodic_thread(workspace: Path):
    runs = workspace / ".wmlab" / "runs"
    indexer = Indexer(IndexDB(workspace / ".wmlab" / "index.sqlite"), runs,
                      workspace / ".wmlab" / "studies")
    indexer.start(interval=0.05)
    try:
        assert indexer.running
        make_fake_run(runs, "late", with_eval=False)
        deadline = time.time() + 5
        while time.time() < deadline and "late" not in indexer.snapshot().run_map():
            time.sleep(0.05)
        assert "late" in indexer.snapshot().run_map()
    finally:
        indexer.stop()
    assert not indexer.running


def test_corrupted_index_is_rebuilt(workspace: Path):
    populate(workspace)
    index = workspace / ".wmlab" / "index.sqlite"
    index.write_bytes(b"this is not a sqlite database" * 100)
    for suffix in ("-wal", "-shm"):
        Path(f"{index}{suffix}").unlink(missing_ok=True)
    result = reindex_workspace(workspace)
    assert result["runs"] == 4 and result["études"] == 1 and result["évaluations"] == 2
    assert result["index"] == str(index.resolve())


def test_locked_index_is_not_deleted(workspace: Path):
    """"database is locked" (another live server) must not destroy the index file."""
    import sqlite3

    import pytest

    index = workspace / ".wmlab" / "index.sqlite"
    index.parent.mkdir(parents=True, exist_ok=True)
    other = sqlite3.connect(str(index), isolation_level=None)
    other.execute("CREATE TABLE marker (x INTEGER)")
    other.execute("BEGIN EXCLUSIVE")
    other.execute("INSERT INTO marker VALUES (1)")
    try:
        with pytest.raises(sqlite3.OperationalError):
            IndexDB(index, timeout=0.2)
    finally:
        other.execute("COMMIT")
        other.close()
    assert index.is_file()
    db = IndexDB(index, timeout=0.2)  # usable again once the lock is released
    assert db._conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0] == 1
    db.close()


def test_reindex_uses_workspace_data_dir(workspace: Path, monkeypatch):
    monkeypatch.delenv("WMLAB_DATA", raising=False)
    text = (workspace / "workspace.yaml").read_text(encoding="utf-8")
    (workspace / "workspace.yaml").write_text(text.replace("data_dir: .wmlab",
                                                           "data_dir: donnees"), encoding="utf-8")
    make_fake_run(workspace / "donnees" / "runs", "x1", with_eval=False)
    make_study(workspace / "donnees" / "studies", "s9")
    result = reindex_workspace(workspace)
    assert result["runs"] == 1 and result["études"] == 1
    assert (workspace / "donnees" / "index.sqlite").is_file()


def test_schema_version_upgrade(workspace: Path):
    path = workspace / "idx.sqlite"
    db = IndexDB(path)
    db._conn.execute("PRAGMA user_version=99")
    db._conn.commit()
    db.close()
    db = IndexDB(path)
    assert db._conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.counts() == {"runs": 0, "studies": 0, "evals": 0, "checkpoints": 0}
    db.close()


def test_views_helpers():
    trials = [trial_view(t) for t in [
        {"number": 0, "state": "complete", "values": [1.0, 5.0]},
        {"number": 1, "state": "COMPLETE", "values": [2.0, 4.0]},
        {"number": 2, "state": "COMPLETE", "values": [1.5, 6.0]},
        {"number": 3, "state": "COMPLETE", "values": [0.5, 3.0]},
        {"number": 4, "state": "PRUNED", "values": None},
        {"number": 5, "state": "weird", "values": 0.3},
    ]]
    assert trials[0]["state"] == "COMPLETE" and trials[5]["state"] == "RUNNING"
    assert trials[5]["values"] == [0.3]
    assert pareto_front(trials, ["maximize", "maximize"]) == [1, 2]
    assert pareto_front(trials, ["minimize", "minimize"]) == [3]
    assert pareto_front(trials, ["maximize", "minimize"]) == [1, 3]


def test_costs_csv_format():
    items = [{"kind": "run", "id": "r1", "project": "p", "host": "scw",
              "started_at": "2026-10-01T10:00:00Z", "hours": 1.23456, "eur": 1.7284},
             {"kind": "manual", "id": "manual-1", "project": None, "host": None,
              "started_at": "2026-09-01", "hours": 0.0, "eur": 5.0, "label": "Données; achat"}]
    lines = costs_csv(items).lstrip("﻿").splitlines()
    assert lines[1] == 'Saisie manuelle;"Données; achat";;;2026-09-01;0,000;5,00'
    assert lines[2] == "Run;r1;p;scw;2026-10-01T10:00:00Z;1,235;1,73"
    # spreadsheet formula injection through a manual label is neutralised
    hostile = [{"kind": "manual", "id": "manual-2", "project": "+p", "host": None,
                "started_at": "2026-09-02", "hours": 0.0, "eur": 1.0,
                "label": "=HYPERLINK(\"http://evil\",\"x\")"}]
    line = costs_csv(hostile).lstrip("\ufeff").splitlines()[1]
    assert line.startswith("Saisie manuelle;\"'=HYPERLINK(") and ";'+p;" in line


def test_server_package_does_not_need_fastapi_for_index():
    import subprocess
    import sys

    code = ("import sys; sys.modules['fastapi'] = None; "
            "import wmlab.server.indexer, wmlab.server.jobs; print('ok')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env={**os.environ}, timeout=60)
    assert out.stdout.strip() == "ok", out.stderr
