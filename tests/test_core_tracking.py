"""Run folder writer/reader: manifest, JSONL, checkpoints, samples, costs, signals."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
import wave
from pathlib import Path

import numpy as np
import pytest
import torch
from PIL import Image

from helpers_core import experiment_dict
from wmlab.ids import utc_iso
from wmlab.tracking import (
    CheckpointNotFoundError,
    Run,
    RunReader,
    StudyReader,
    atomic_write_json,
    cost_eur,
    flatten_params,
    list_runs,
    list_studies,
    read_jsonl,
    resolve_checkpoint,
)
from wmlab.tracking.reader import lttb_indices, uniform_indices

REPO = Path(__file__).resolve().parents[1]


def make_run(runs: Path, run_id: str = "r1", **kwargs) -> Run:
    spec = experiment_dict()
    defaults = dict(project="demo", experiment="baseline", modality="image", spec=spec,
                    spec_path="projects/demo/experiments/baseline.yaml", run_id=run_id,
                    runs_dir=runs, capture_env=False, monitor_system=False)
    defaults.update(kwargs)
    return Run.create(**defaults)


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def test_create_writes_run_folder(workspace: Path):
    run = Run.create(project="demo", experiment="baseline", modality="image",
                     spec=experiment_dict(), overrides={"optimizer.lr": "1e-3"}, tags=["t1"],
                     launch_info={"job_id": "job-1", "host": "local",
                                  "git": {"sha": "abc", "branch": "main", "dirty": False,
                                          "remote": None}},
                     monitor_system=False)
    try:
        assert run.dir.parent == workspace / ".wmlab" / "runs"
        manifest = json.loads((run.dir / "run.json").read_text())
        assert manifest["format_version"] == 1 and manifest["kind"] == "train"
        assert manifest["status"] == "running" and manifest["ended_at"] is None
        assert manifest["created_at"].endswith("Z") and manifest["started_at"].endswith("Z")
        assert manifest["host"] == "local" and manifest["price_eur_per_hour"] == 0.0
        assert manifest["tags"] == ["t1"] and manifest["overrides"] == {"optimizer.lr": "1e-3"}
        assert manifest["params"]["optimizer.lr"] == 1e-3
        assert manifest["params"]["phases.1.steps"] == 20
        for key in ("study", "trial", "parent", "progress", "error"):
            assert manifest[key] is None
        assert manifest["summary"] == {} and manifest["datasets"] == {}
        spec_text = (run.dir / "spec.yaml").read_text()
        assert spec_text.startswith("name: baseline")  # key order preserved
        notes = (run.dir / "notes.md").read_text()
        assert "Le warmup sans attaque aide le décodeur" in notes
        assert "## Observations" in notes and "## Conclusion" in notes
        env = run.dir / "env"
        assert {p.name for p in env.iterdir()} >= {"pip_freeze.txt", "hardware.json",
                                                   "command.txt", "launch.json", "git.json"}
        hardware = json.loads((env / "hardware.json").read_text())
        assert hardware["torch"] == torch.__version__ and hardware["cpu_count"]
        assert json.loads((env / "git.json").read_text())["sha"] == "abc"
        assert float((run.dir / "heartbeat").read_text()) == pytest.approx(time.time(), abs=60)
        events = read_jsonl(run.dir / "events.jsonl")
        assert events[0]["type"] == "status" and events[0]["status"] == "running"
    finally:
        run.finish()
    manifest = RunReader(run.dir).manifest()
    assert manifest["status"] == "completed" and manifest["ended_at"].endswith("Z")
    assert RunReader(run.dir).env()["launch"]["job_id"] == "job-1"


def test_existing_run_is_protected(runs: Path):
    make_run(runs).finish()
    with pytest.raises(FileExistsError, match="existe déjà"):
        make_run(runs)
    # a run pre-created by the job manager with status "queued" can be started
    queued = runs / "q1"
    queued.mkdir(parents=True)
    atomic_write_json(queued / "run.json", {"run_id": "q1", "status": "queued",
                                            "created_at": "2026-10-01T00:00:00Z"})
    run = make_run(runs, run_id="q1")
    run.finish()
    assert RunReader(queued).manifest()["created_at"] == "2026-10-01T00:00:00Z"


def test_metrics_summary_and_progress(runs: Path):
    run = make_run(runs)
    run.set_checkpoint_policy(2, {"val/bit_acc_mean": "max", "val/loss": "min"})
    run.log_metrics(20, {"train/loss": torch.tensor(0.5), "train/nan": float("nan")}, phase="warmup")
    run.log_metrics(40, {"val/bit_acc_mean": 0.8, "val/loss": 0.3}, phase="warmup")
    run.log_metrics(60, {"val/bit_acc_mean": 0.7, "val/loss": 0.2}, phase="robust")
    run.set_progress(step=60, total_steps=30, phase="robust", phase_index=1, n_phases=2)
    run.set_datasets({"train": "a1b2c3d4e5f6"})
    run.update_summary({"custom": 1.5})
    run.finish()
    reader = RunReader(run.dir)
    manifest = reader.manifest()
    assert manifest["summary"] == {"val/bit_acc_mean": 0.7, "val/loss": 0.2, "step": 60,
                                   "best/val/bit_acc_mean": 0.8, "best/val/loss": 0.2,
                                   "custom": 1.5}
    assert manifest["progress"]["phase"] == "robust" and manifest["datasets"]["train"]
    rows = read_jsonl(run.dir / "metrics.jsonl")
    assert rows[0]["values"] == {"train/loss": 0.5, "train/nan": None}
    assert rows[0]["phase"] == "warmup" and isinstance(rows[0]["t"], float)
    assert reader.metric_keys() == ["train/loss", "train/nan", "val/bit_acc_mean", "val/loss"]
    assert "train/nan" not in reader.metrics()
    assert reader.last_values()["val/loss"] == {"step": 60, "value": 0.2}


def test_manifest_write_is_throttled(runs: Path):
    run = make_run(runs)
    run.set_progress(step=1, total_steps=10, phase="warmup")  # phase change: written
    run.set_progress(step=2)  # throttled
    on_disk = json.loads((run.dir / "run.json").read_text())
    assert on_disk["progress"]["step"] == 1
    run.finish()
    assert json.loads((run.dir / "run.json").read_text())["progress"]["step"] == 2


def test_context_manager_statuses(runs: Path):
    with pytest.raises(RuntimeError):
        with make_run(runs, run_id="boom"):
            raise RuntimeError("explosion")
    failed = RunReader(runs / "boom").manifest()
    assert failed["status"] == "failed" and "explosion" in failed["error"]
    with pytest.raises(KeyboardInterrupt):
        with make_run(runs, run_id="ctrlc"):
            raise KeyboardInterrupt
    assert RunReader(runs / "ctrlc").manifest()["status"] == "killed"
    with make_run(runs, run_id="ok"):
        pass
    assert RunReader(runs / "ok").manifest()["status"] == "completed"
    with make_run(runs, run_id="early") as run:
        run.finish("killed", "max_hours reached")
    assert RunReader(runs / "early").manifest()["error"] == "max_hours reached"
    with pytest.raises(ValueError):
        make_run(runs, run_id="bad").finish("done")


def test_checkpoint_retention_and_selectors(runs: Path):
    run = make_run(runs)
    run.set_checkpoint_policy(keep_last=2, keep_best={"val/acc": "max"})
    accs = [0.5, 0.9, 0.6, 0.7, 0.8, 0.85]
    for i, acc in enumerate(accs, start=1):
        tags = ["phase_end:warmup"] if i == 3 else None
        name = run.save_checkpoint({"model": {"w": torch.zeros(2)}}, step=i * 10,
                                   phase="warmup" if i <= 3 else "robust",
                                   metrics={"val/acc": acc}, tags=tags)
        assert name == f"step_{i * 10:06d}"
    run.finish()
    reader = RunReader(run.dir)
    index = {c["name"]: c for c in reader.checkpoints()}
    assert list(index) == ["step_000020", "step_000030", "step_000050", "step_000060"]
    assert index["step_000020"]["tags"] == ["best:val/acc"]
    assert index["step_000030"]["tags"] == ["phase_end:warmup"]
    assert index["step_000060"]["tags"] == ["last"]
    assert all(c["available_locally"] for c in index.values())
    assert set(index["step_000060"]) >= {"name", "file", "step", "phase", "metrics", "tags",
                                         "created_at", "size_bytes"}
    files = sorted(p.name for p in (run.dir / "checkpoints").glob("*.pt"))
    assert files == ["step_000020.pt", "step_000030.pt", "step_000050.pt", "step_000060.pt"]
    deleted = [e["name"] for e in reader.events() if e["type"] == "checkpoint_deleted"]
    assert deleted == ["step_000010", "step_000040"]
    payload = torch.load(run.dir / "checkpoints" / "step_000060.pt", weights_only=False)
    assert payload["format"] == "wmlab-ckpt-v1" and payload["step"] == 60

    assert resolve_checkpoint(run.dir, "last")["name"] == "step_000060"
    assert resolve_checkpoint(run.dir, "best:val/acc")["name"] == "step_000020"
    assert resolve_checkpoint(run.dir, "phase_end:warmup")["name"] == "step_000030"
    assert resolve_checkpoint(run.dir, "step_000050")["name"] == "step_000050"
    assert resolve_checkpoint(run.dir, "50")["name"] == "step_000050"
    resolved = resolve_checkpoint(run.dir, "last")
    assert Path(resolved["path"]).is_file() and resolved["available_locally"]
    with pytest.raises(CheckpointNotFoundError, match="phase_end:robust"):
        resolve_checkpoint(run.dir, "phase_end:robust")
    with pytest.raises(KeyError):
        resolve_checkpoint(run.dir, "step_000010")
    with pytest.raises(KeyError):
        resolve_checkpoint(run.dir, "best:val/other")


def test_checkpoint_same_step_merges_tags(runs: Path):
    run = make_run(runs)
    run.set_checkpoint_policy(keep_last=1, keep_best={})
    run.save_checkpoint({"model": {}}, step=10, phase="warmup")
    run.save_checkpoint({"model": {}}, step=10, phase="warmup", tags=["phase_end:warmup"])
    run.save_checkpoint({"model": {}}, step=20, phase="robust")
    run.finish()
    index = {c["name"]: c["tags"] for c in RunReader(run.dir).checkpoints()}
    assert index == {"step_000010": ["phase_end:warmup"], "step_000020": ["last"]}


def test_samples(runs: Path):
    run = make_run(runs)
    png = run.log_sample(10, "image", "0", torch.rand(3, 12, 20), group="watermarked")
    png2 = run.log_sample(10, "image", "1", np.zeros((12, 20, 3), dtype=np.uint8), group="orig")
    wav = run.log_sample(10, "audio", "0", torch.rand(1, 1600) * 2 - 1, group="watermarked",
                         sample_rate=8000)
    vid = run.log_sample(10, "video", "0", torch.rand(4, 3, 17, 19), group="attacked", fps=12)
    raw = run.log_sample(20, "image", "raw", b"\x89PNG fake", group="x")
    run.finish()
    assert png == "samples/10/0_watermarked.png"
    with Image.open(run.dir / png) as img:
        assert img.size == (20, 12) and img.mode == "RGB"
    with Image.open(run.dir / png2) as img:
        assert img.size == (20, 12)
    with wave.open(str(run.dir / wav)) as handle:
        assert (handle.getnchannels(), handle.getframerate(), handle.getnframes()) == (1, 8000, 1600)
        assert handle.getsampwidth() == 2
    assert vid.startswith("samples/10/0_attacked.") and vid.endswith((".mp4", ".gif"))
    assert (run.dir / vid).stat().st_size > 0
    assert (run.dir / raw).read_bytes() == b"\x89PNG fake"
    samples = RunReader(run.dir).samples()
    assert [s["kind"] for s in samples] == ["image", "image", "audio", "video", "image"]
    assert samples[0] == {"step": 10, "kind": "image", "name": "0", "path": png,
                          "group": "watermarked"}


def test_log_eval(runs: Path):
    run = make_run(runs)
    path = run.log_eval({"checkpoint": "step_000060", "benchmark": "image_v1",
                         "benchmark_version": 1, "summary": {"psnr": 38.0}})
    run.finish()
    assert path == run.dir / "eval" / "step_000060__image_v1.json"
    evals = RunReader(run.dir).evals()
    assert evals[0]["run_id"] == "r1" and evals[0]["summary"]["psnr"] == 38.0
    assert any(e["type"] == "eval" for e in RunReader(run.dir).events())


def test_lost_status_and_cost(runs: Path, monkeypatch):
    monkeypatch.setenv("WMLAB_PRICE_EUR_PER_HOUR", "2.0")
    run = make_run(runs, gpu_share=0.5)
    run.finish()
    # simulate a crashed process: running, heartbeat 20 minutes old, started 2 h ago
    now = time.time()
    manifest = json.loads((run.dir / "run.json").read_text())
    manifest.update(status="running", ended_at=None, started_at=utc_iso(now - 7200))
    atomic_write_json(run.dir / "run.json", manifest)
    (run.dir / "heartbeat").write_text(str(now - 1200))
    reader = RunReader(run.dir)
    derived = reader.manifest(now=now)
    assert derived["status"] == "lost"
    assert derived["cost_eur"] == pytest.approx((6000 / 3600) * 2.0 * 0.5, rel=1e-3)
    (run.dir / "heartbeat").write_text(str(now - 30))
    assert reader.manifest(now=now)["status"] == "running"
    assert cost_eur({"started_at": utc_iso(now - 3600), "ended_at": utc_iso(now),
                     "price_eur_per_hour": 1.4}) == pytest.approx(1.4, rel=1e-3)
    assert cost_eur({"started_at": utc_iso(now - 3600), "price_eur_per_hour": 1.0},
                    now=now) == pytest.approx(1.0, rel=1e-3)
    assert cost_eur({"started_at": None, "price_eur_per_hour": 3.0}) == 0.0


def test_reader_tolerates_partial_and_missing_files(runs: Path):
    run = make_run(runs)
    for step in range(0, 1000, 10):
        run.log_metrics(step, {"train/loss": 1.0 / (1 + step), "val/x": float(step % 7)})
    run.finish()
    with open(run.dir / "metrics.jsonl", "a") as handle:
        handle.write('{"t": 1, "step": 5000, "values": {"train/lo')  # partial last line
    reader = RunReader(run.dir)
    full = reader.metrics()
    assert len(full["train/loss"]["steps"]) == 100
    sampled = reader.metrics(keys=["train/loss"], max_points=20)
    assert list(sampled) == ["train/loss"]
    steps = sampled["train/loss"]["steps"]
    assert len(steps) <= 20 and steps[0] == 0 and steps[-1] == 990
    empty = RunReader(runs / "missing")
    assert empty.metrics() == {} and empty.events() == [] and empty.checkpoints() == []
    assert empty.samples() == [] and empty.evals() == [] and empty.logs() == []
    assert empty.notes() == "" and empty.spec_yaml() == "" and empty.spec() == {}
    with pytest.raises(FileNotFoundError):
        empty.manifest()


def test_reader_system_logs_notes_env(runs: Path, capsys):
    run = make_run(runs)
    run.log_system({"cpu_percent": 10.0, "ram_gb": 3.0})
    run.log_system({"cpu_percent": 20.0, "gpu_util": 50.0})
    print("ligne 1")
    print("progress 10%\rprogress 100%")
    run.finish()
    reader = RunReader(run.dir)
    system = reader.system()
    assert len(system["t"]) == 2
    assert system["series"]["cpu_percent"] == [10.0, 20.0]
    assert system["series"]["gpu_util"] == [None, 50.0]
    assert len(reader.system(max_points=1)["t"]) == 1
    assert reader.logs(tail=2) == ["ligne 1", "progress 100%"]
    assert "ligne 1" in capsys.readouterr().out  # tee keeps the original output
    reader.set_notes("# Conclusion\nça marche")
    assert reader.notes() == "# Conclusion\nça marche"
    assert reader.spec()["name"] == "baseline"
    env = reader.env()
    assert env == {"git": None, "git_diff": None, "hardware": None, "pip_freeze_lines": 0,
                   "command": None, "launch": None}
    assert list_runs(runs) == [run.dir]
    assert list_runs(runs / "nope") == []


def test_background_threads(runs: Path, monkeypatch):
    monkeypatch.setattr(Run, "HEARTBEAT_INTERVAL_S", 0.05)
    monkeypatch.setattr(Run, "SYSTEM_INTERVAL_S", 0.05)
    run = make_run(runs, monitor_system=True)
    first = float((run.dir / "heartbeat").read_text())
    run.set_progress(step=1, phase="warmup")
    run.set_progress(step=5)  # throttled, flushed by the heartbeat thread
    deadline = time.time() + 5
    while time.time() < deadline:
        progress = json.loads((run.dir / "run.json").read_text())["progress"]
        if progress["step"] == 5 and read_jsonl(run.dir / "system.jsonl"):
            break
        time.sleep(0.05)
    assert float((run.dir / "heartbeat").read_text()) > first
    assert progress["step"] == 5
    rows = read_jsonl(run.dir / "system.jsonl")
    assert rows and "cpu_percent" in rows[0] and "ram_gb" in rows[0]
    run.finish()
    assert not any(t.is_alive() for t in run._threads)


@pytest.mark.parametrize("with_block", [True, False])
def test_sigterm_marks_run_killed(tmp_path: Path, with_block: bool):
    runs = tmp_path / "runs"
    body = "with run:\n    os.kill(os.getpid(), signal.SIGTERM)\n    time.sleep(10)\n" \
        if with_block else "os.kill(os.getpid(), signal.SIGTERM)\ntime.sleep(10)\n"
    script = textwrap.dedent(f"""
        import os, signal, time
        from pathlib import Path
        from wmlab.tracking import Run
        run = Run.create(project="p", experiment="e", modality="image", spec={{"name": "e"}},
                         runs_dir=Path({str(runs)!r}), run_id="sig", capture_env=False,
                         monitor_system=False)
    """) + body
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          timeout=60, env=env)
    assert proc.returncode != 0
    manifest = RunReader(runs / "sig").manifest()
    assert manifest["status"] == "killed", proc.stderr
    assert "SIGTERM" in (manifest["error"] or "")


def test_flatten_params():
    spec = experiment_dict()
    spec["optimizer"]["betas"] = [0.9, 0.99]
    params = flatten_params(spec, {"phases.robust.attack_strength": "0.5", "custom.key": "3e-4"})
    assert params == {
        "model.name": "_test_model", "model.params.channels": 8,
        "optimizer.name": "adamw", "optimizer.lr": 1e-3,
        "data.batch_size": 4,
        "loss.terms.msg.weight": 1.0, "loss.terms.img.weight": 0.5,
        "phases.0.steps": 10, "phases.1.steps": 20,
        "seed": 0,
        "phases.robust.attack_strength": "0.5",
        "custom.key": 3e-4,
    }


def test_study_reader(tmp_path: Path):
    studies = tmp_path / "studies"
    study_dir = studies / "20261001-120000-sweep-abcd"
    study_dir.mkdir(parents=True)
    now = time.time()
    atomic_write_json(study_dir / "study.json", {
        "format_version": 1, "kind": "hpo", "study_id": study_dir.name, "status": "running",
        "started_at": utc_iso(now - 3600), "ended_at": None, "price_eur_per_hour": 1.0})
    (study_dir / "heartbeat").write_text(str(now - 900))
    with open(study_dir / "trials.jsonl", "w") as handle:
        for row in ({"number": 1, "state": "RUNNING"}, {"number": 0, "state": "COMPLETE"},
                    {"number": 1, "state": "PRUNED"}):
            handle.write(json.dumps(row) + "\n")
    reader = StudyReader(study_dir)
    manifest = reader.manifest(now=now)
    assert manifest["status"] == "lost"
    assert manifest["cost_eur"] == pytest.approx(2700 / 3600, rel=1e-3)
    assert [(t["number"], t["state"]) for t in reader.trials()] == [(0, "COMPLETE"), (1, "PRUNED")]
    assert reader.events() == [] and reader.logs() == []
    assert list_studies(studies) == [study_dir]


def test_downsampling_helpers():
    assert uniform_indices(10, 3) == [0, 4, 9] or uniform_indices(10, 3) == [0, 5, 9]
    assert uniform_indices(3, 10) == [0, 1, 2]
    xs = list(range(100))
    ys = [0.0] * 100
    ys[37] = 10.0  # a spike must survive LTTB
    idx = lttb_indices(xs, ys, 10)
    assert idx[0] == 0 and idx[-1] == 99 and 37 in idx and len(idx) <= 10


def test_log_eval_to_existing_run(runs: Path):
    from wmlab.tracking import append_event, log_eval_to

    run = make_run(runs)
    run.finish()
    path = log_eval_to(run.dir, {"checkpoint": "last", "benchmark": "image_v1",
                                 "summary": {"bit_acc_mean": 0.9}})
    append_event(run.dir, "note", text="évaluation manuelle")
    reader = RunReader(run.dir)
    assert path.name == "last__image_v1.json"
    assert reader.evals()[0]["run_id"] == "r1" and reader.evals()[0]["created_at"].endswith("Z")
    assert [e["type"] for e in reader.events()[-2:]] == ["eval", "note"]


def test_run_id_is_validated(runs: Path):
    with pytest.raises(ValueError, match="run_id invalide"):
        make_run(runs, run_id="../escape")
