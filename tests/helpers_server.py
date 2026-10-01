"""Helpers for the server tests: a populated workspace, a fake CLI and a fake SSH runner."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np
import yaml

from helpers_core import (
    benchmark_dict,
    experiment_dict,
    hpo_dict,
    install_fake_plugins,
    make_project,
    write_yaml,
)
from wmlab.ids import parse_iso, utc_iso
from wmlab.server.executors import CommandResult
from wmlab.tracking import Run, atomic_write_json, read_json

T0 = parse_iso("2026-03-02T10:00:00Z")  # always in the past
assert T0 is not None
HOUR = 3600.0
SCW_HOST = {"name": "scw", "kind": "ssh", "ssh_target": "root@51.15.0.1",
            "workdir": "/root/wmlab", "data_dir": "/root/wmlab-data",
            "python": ".venv/bin/python", "price_eur_per_hour": 1.5, "gpu": "L40S 48GB",
            "max_concurrent_jobs": 2, "notes": ""}

# -- §9 shapes (keys) ---------------------------------------------------------------------------
RUN_SUMMARY_KEYS = {"run_id", "project", "experiment", "modality", "status", "host", "created_at",
                    "started_at", "ended_at", "duration_s", "cost_eur", "study", "trial", "parent",
                    "tags", "progress", "summary", "params", "error", "user"}
RUN_DETAIL_KEYS = RUN_SUMMARY_KEYS | {"spec", "spec_yaml", "spec_path", "env", "plan", "events",
                                      "checkpoints", "evals", "notes", "samples", "metric_keys",
                                      "price_eur_per_hour", "datasets", "overrides"}
PROJECT_SUMMARY_KEYS = {"name", "title", "modality", "description", "message_bits",
                        "default_benchmark", "tags", "objectives", "n_experiments",
                        "n_hpo_plans", "n_runs", "n_running", "last_activity", "cost_eur", "best"}
OBJECTIVE_KEYS = {"metric", "direction", "label", "constraint"}
EXPERIMENT_INFO_KEYS = {"name", "path", "description", "hypothesis", "model", "n_phases",
                        "total_steps", "valid", "errors", "n_runs"}
HPO_INFO_KEYS = {"name", "path", "experiment", "description", "n_params", "objectives", "valid",
                 "errors", "n_studies"}
CHECKPOINT_KEYS = {"name", "step", "phase", "metrics", "tags", "created_at", "size_bytes",
                   "available_locally"}
EVAL_KEYS = {"run_id", "checkpoint", "benchmark", "benchmark_version", "created_at",
             "duration_s", "nbits", "n_items", "quality", "robustness", "summary"}
SAMPLE_KEYS = {"step", "kind", "name", "path", "group"}
ENV_KEYS = {"git", "git_diff", "hardware", "pip_freeze_lines", "command", "launch"}
PHASE_PREVIEW_KEYS = {"name", "start", "end", "steps", "lr", "attack_strength", "loss_weights",
                      "attacks"}
STUDY_SUMMARY_KEYS = {"study_id", "project", "plan", "experiment", "status", "host", "created_at",
                      "ended_at", "n_trials", "n_complete", "n_pruned", "n_failed", "n_running",
                      "objectives", "best", "cost_eur", "budget", "user"}
STUDY_DETAIL_KEYS = STUDY_SUMMARY_KEYS | {"spec", "trials", "importances", "pareto", "notes"}
TRIAL_KEYS = {"number", "state", "params", "values", "intermediate", "run_id", "duration_s"}
LEADER_ROW_KEYS = {"run_id", "project", "experiment", "checkpoint", "benchmark", "source",
                   "created_at", "quality", "summary", "robustness"}
LEADERBOARD_KEYS = {"benchmarks", "quality_metrics", "robustness_metrics", "attacks", "rows"}
REGISTRY_ENTRY_KEYS = {"name", "kind", "modality", "description", "params_schema", "source",
                       "builtin"}
BENCHMARK_KEYS = {"name", "path", "modality", "version", "description", "attacks",
                  "quality_metrics", "fpr", "n_items", "valid", "errors"}
DATASET_KEYS = {"name", "path", "modality", "description", "license", "source", "paths",
                "used_by", "valid", "errors"}
HOST_KEYS = {"name", "kind", "ssh_target", "workdir", "data_dir", "python", "price_eur_per_hour",
             "gpu", "max_concurrent_jobs", "notes", "scaleway", "decommissioned_at",
             "unretrieved_checkpoints", "unretrieved_bytes", "inventory_at", "active_jobs"}
JOB_KEYS = {"job_id", "kind", "host", "target", "overrides", "status", "run_id", "study_id",
            "created_at", "started_at", "ended_at", "max_hours", "smoke", "error", "pid",
            "last_sync_at", "user", "git_ref", "git_sha", "retrieval", "result"}
COST_ITEM_KEYS = {"kind", "id", "project", "host", "started_at", "hours", "eur"}
COSTS_KEYS = {"total_eur", "spent_eur", "remaining_eur", "running_eur_per_hour", "by_project",
              "by_host", "by_month", "items", "budget"}
SETTINGS_KEYS = {"name", "budget", "data_dir", "sync_interval_s", "default_host", "hosted"}
HOSTED_KEYS = {"enabled", "git_branch", "auto_pull_s", "auto_commit", "public_url",
               "allow_registration"}
GIT_STATE_KEYS = {"available", "hosted", "branch", "sha", "short_sha", "dirty", "remote", "ahead",
                  "behind", "last_pull_at", "last_pull_error"}
INVENTORY_KEYS = {"host", "reachable", "error", "checked_at", "active_jobs", "remote_data_dir",
                  "runs", "studies", "unretrieved_checkpoints", "unretrieved_bytes",
                  "unindexed_runs"}
INVENTORY_RUN_KEYS = {"run_id", "project", "status", "checkpoints_total", "checkpoints_local",
                      "bytes_remote", "bytes_missing", "indexed"}
RETRIEVAL_KEYS = {"status", "checkpoints", "bytes", "missing"}
OVERVIEW_KEYS = {"workspace", "budget", "counts", "running", "recent", "projects", "jobs"}


# -- workspace ----------------------------------------------------------------------------------
def patch_manifest(run_dir: Path, **changes: Any) -> dict[str, Any]:
    manifest = read_json(run_dir / "run.json")
    manifest.update(changes)
    atomic_write_json(run_dir / "run.json", manifest)
    return manifest


def eval_result(run_id: str, checkpoint: str, *, psnr: float, bit_acc: float, host: str,
                price: float, duration_s: float, created_at: str) -> dict[str, Any]:
    return {
        "run_id": run_id, "checkpoint": checkpoint, "benchmark": "image_v1",
        "benchmark_version": 1, "created_at": created_at, "duration_s": duration_s,
        "host": host, "price_eur_per_hour": price, "nbits": 16, "n_items": 4,
        "quality": {"_test_psnr": psnr},
        "robustness": {"none": {"bit_acc": 1.0, "tpr@0.01": 1.0},
                       "quant": {"bit_acc": bit_acc, "tpr@0.01": bit_acc}},
        "summary": {"_test_psnr": psnr, "bit_acc_mean": bit_acc, "tpr@0.01_mean": bit_acc},
    }


def make_fake_run(runs: Path, run_id: str, *, host: str = "local", price: float = 0.0,
                  start: float = T0, hours: float = 2.0, status: str = "completed",
                  steps: int = 30, bit_acc: float = 0.9, psnr: float = 40.0,
                  study: str | None = None, trial: int | None = None,
                  eval_price: float | None = None, eval_duration: float = 60.0,
                  with_eval: bool = True) -> Path:
    """Write a complete run folder through ``wmlab.tracking.Run`` then set times/price."""
    run = Run.create(project="demo", experiment="baseline", modality="image",
                     spec=experiment_dict(), spec_path="projects/demo/experiments/baseline.yaml",
                     run_id=run_id, runs_dir=runs, study=study, trial=trial,
                     overrides={"optimizer.lr": "1e-3"}, capture_env=False,
                     monitor_system=False)
    try:
        run.set_checkpoint_policy(keep_last=2, keep_best={"val/bit_acc_mean": "max"})
        for step in (10, 20, 30):
            phase = "warmup" if step <= 10 else "robust"
            run.log_metrics(step, {"train/loss": 1.0 / step, "train/bit_acc": 0.5 + step / 100},
                            phase=phase)
            run.log_metrics(step, {"val/_test_psnr": psnr - 30 + step,
                                   "val/bit_acc": bit_acc, "val/bit_acc/noise": bit_acc - 0.05,
                                   "val/bit_acc_mean": bit_acc - 0.05 + step / 1000},
                            phase=phase)
        run.log_system({"cpu_percent": 12.0, "ram_gb": 3.5})
        run.save_checkpoint({"model": {}}, step=10, phase="warmup",
                            metrics={"val/bit_acc_mean": bit_acc - 0.04},
                            tags=["phase_end:warmup"])
        run.save_checkpoint({"model": {}}, step=30, phase="robust",
                            metrics={"val/bit_acc_mean": bit_acc - 0.02},
                            tags=["phase_end:robust"])
        run.log_sample(30, "image", "0", np.zeros((3, 8, 8), dtype=np.float32),
                       group="watermarked")
        run.set_progress(step=steps, total_steps=steps, phase="robust", phase_index=1,
                         n_phases=2)
        if with_eval:
            run.log_eval(eval_result(run_id, "step_000030", psnr=psnr, bit_acc=bit_acc,
                                     host=host,
                                     price=price if eval_price is None else eval_price,
                                     duration_s=eval_duration,
                                     created_at=utc_iso(start + hours * HOUR + 600)))
        run.event("note", text="bonjour")
    finally:
        run.finish("completed")
    run_dir = runs / run_id
    changes: dict[str, Any] = {"host": host, "price_eur_per_hour": price,
                               "started_at": utc_iso(start), "created_at": utc_iso(start),
                               "ended_at": utc_iso(start + hours * HOUR), "status": status}
    if status == "running":
        changes["ended_at"] = None
        (run_dir / "heartbeat").write_text(f"{start + hours * HOUR:.3f}", encoding="utf-8")
    patch_manifest(run_dir, **changes)
    (run_dir / "stdout.log").write_text("ligne 1\nligne 2\n", encoding="utf-8")
    return run_dir


def make_study(studies: Path, study_id: str = "s1", *, start: float = T0 + 30 * 86400,
               hours: float = 1.0, price: float = 1.0, n_complete: int = 4) -> Path:
    folder = studies / study_id
    folder.mkdir(parents=True, exist_ok=True)
    hpo = hpo_dict()
    atomic_write_json(folder / "study.json", {
        "format_version": 1, "kind": "hpo", "study_id": study_id, "project": "demo",
        "plan": "sweep", "experiment": "baseline",
        "spec_path": "projects/demo/hpo/sweep.yaml", "status": "completed",
        "created_at": utc_iso(start), "started_at": utc_iso(start),
        "ended_at": utc_iso(start + hours * HOUR), "host": "scw", "price_eur_per_hour": price,
        "objectives": [{"metric": "val/bit_acc_mean", "direction": "maximize"}],
        "budget": {"max_trials": 5, "max_hours": None, "max_eur": 20, "parallel": 1},
        "n_trials": n_complete + 1,
        "best": [{"trial": n_complete - 1, "values": [0.9], "params": {"optimizer.lr": 0.001},
                  "run_id": "t1"}],
        "error": None,
    })
    (folder / "spec.yaml").write_text(yaml.safe_dump(hpo, sort_keys=False), encoding="utf-8")
    lines = []
    for i in range(n_complete):
        lines.append({"number": i, "state": "COMPLETE",
                      "params": {"optimizer.lr": 10 ** (-4 + 0.5 * i),
                                 "model.params.channels": [4, 8][i % 2]},
                      "values": [0.6 + 0.1 * i], "intermediate": [[10, 0.5], [20, 0.6 + 0.1 * i]],
                      "run_id": f"t{i}", "duration_s": 600.0, "t": start + i})
    lines.append({"number": n_complete, "state": "PRUNED", "params": {"optimizer.lr": 0.005},
                  "values": None, "intermediate": [[10, 0.2]], "run_id": None,
                  "duration_s": 100.0, "t": start + 10})
    (folder / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines),
                                         encoding="utf-8")
    return folder


def populate(root: Path) -> dict[str, Any]:
    """Project ``demo`` + benchmark, dataset, HPO plan, 3 runs, 1 trial run and 1 study."""
    install_fake_plugins(root)
    folder_exp = experiment_dict(name="folder_exp")
    folder_exp["data"]["train"]["params"]["root"] = "@coco"
    make_project(root, "demo", default_benchmark="image_v1")
    write_yaml(root / "projects" / "demo" / "experiments" / "folder_exp.yaml", folder_exp)
    write_yaml(root / "projects" / "demo" / "hpo" / "sweep.yaml",
               hpo_dict(fidelity={"steps_scale": 0.5, "overrides": {}}))
    (root / "projects" / "demo" / "notes.md").write_text("# Journal\n", encoding="utf-8")
    write_yaml(root / "benchmarks" / "image_v1.yaml", benchmark_dict())
    write_yaml(root / "datasets" / "coco.yaml",
               {"name": "coco", "modality": "image", "description": "COCO", "license": "CC",
                "source": "https://cocodataset.org", "paths": {"default": "~/datasets/coco"}})
    settings = yaml.safe_load((root / "workspace.yaml").read_text(encoding="utf-8"))
    settings["budget"]["manual_entries"] = [{"label": "Stockage", "eur": 10.0,
                                             "date": "2026-10-05", "project": "demo"}]
    (root / "workspace.yaml").write_text(yaml.safe_dump(settings, sort_keys=False,
                                                        allow_unicode=True), encoding="utf-8")
    data = root / ".wmlab"
    runs, studies = data / "runs", data / "studies"
    now = time.time()
    make_fake_run(runs, "r1", steps=3000, bit_acc=0.85, psnr=41.0)
    make_fake_run(runs, "r2", host="scw", price=1.5, start=T0 + 86400, steps=7200,
                  bit_acc=0.95, psnr=39.0, eval_duration=HOUR)
    make_fake_run(runs, "r3", host="scw", price=2.0, start=now - 1800, hours=0.5,
                  status="running", with_eval=False)
    make_fake_run(runs, "t1", host="scw", price=1.0, study="s1", trial=0, with_eval=False)
    # r2 was trained remotely: its checkpoint files are not on the Mac
    for pt in (runs / "r2" / "checkpoints").glob("*.pt"):
        pt.unlink()
    make_study(studies)
    (data / "hosts.yaml").write_text(yaml.safe_dump({"hosts": [
        {"name": "local", "kind": "local", "price_eur_per_hour": 0.0, "max_concurrent_jobs": 1},
        SCW_HOST]}, sort_keys=False), encoding="utf-8")
    return {"runs": runs, "studies": studies, "data": data}


# -- fake CLI & SSH runner ----------------------------------------------------------------------
FAKE_CLI = r'''
"""Fake ``wmlab.cli`` used by the job tests (behaviour chosen by WMLAB_FAKE_MODE)."""
import json, os, signal, sys, time
from pathlib import Path

args = sys.argv[1:]
data = Path(os.environ["WMLAB_DATA"])
mode = os.environ.get("WMLAB_FAKE_MODE", "complete")


def arg(name):
    return args[args.index(name) + 1]


if args[0] == "train":
    item_id, folder, name = arg("--run-id"), "runs", "run.json"
elif args[0] == "hpo":
    item_id, folder, name = arg("--study-id"), "studies", "study.json"
else:
    item_id, folder, name = arg("--run"), "runs", None
target = data / folder / item_id
target.mkdir(parents=True, exist_ok=True)
started = time.time()


def stamp(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def write(status):
    if name is None:
        return
    manifest = {"format_version": 1, "kind": "train", "run_id": item_id, "study_id": item_id,
                "project": "demo", "experiment": "baseline", "plan": "sweep", "modality": "image",
                "status": status, "created_at": stamp(started), "started_at": stamp(started),
                "ended_at": None if status == "running" else stamp(time.time()),
                "host": os.environ.get("WMLAB_HOST"),
                "price_eur_per_hour": float(os.environ.get("WMLAB_PRICE_EUR_PER_HOUR") or 0),
                "gpu_share": 1.0, "tags": [], "params": {}, "overrides": {}, "datasets": {},
                "progress": None, "summary": {}, "error": None, "argv": args,
                "cwd": os.getcwd(), "workspace": os.environ.get("WMLAB_WORKSPACE")}
    tmp = target / (name + ".tmp")
    tmp.write_text(json.dumps(manifest))
    os.replace(tmp, target / name)
    (target / "heartbeat").write_text(str(time.time()))


print("fake cli", " ".join(args), flush=True)
if mode == "fail":
    print("boom: échec simulé", flush=True)
    sys.exit(3)
if mode == "sleep":
    write("running")

    def on_term(*_):
        write("killed")
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    for _ in range(600):
        time.sleep(0.05)
    write("completed")
    sys.exit(0)
write("running")
time.sleep(float(os.environ.get("WMLAB_FAKE_SLEEP", "0.3")))
write("completed")
'''


def write_fake_cli(folder: Path) -> list[str]:
    """Write the fake CLI script; return the ``cli_prefix`` to inject."""
    path = folder / "fake_cli.py"
    path.write_text(FAKE_CLI, encoding="utf-8")
    return [sys.executable, str(path)]


class FakeRunner:
    """Records commands and answers like ssh/rsync would (no network)."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.alive = True
        self.pid = 4242
        self.on_pull: Callable[[list[str]], None] | None = None
        self.fail_ssh = False

    def __call__(self, argv: list[str], *, timeout: float | None = None) -> CommandResult:
        self.calls.append(list(argv))
        if argv[0] == "ssh":
            if self.fail_ssh:
                return CommandResult(255, "", "ssh: connect to host 51.15.0.1: Connection refused")
            command = argv[-1]
            if "nohup setsid" in command:
                return CommandResult(0, f"{self.pid}\n")
            if command.startswith("kill -0"):
                return CommandResult(0, "alive\n" if self.alive else "dead\n")
            if command.startswith("echo ok"):
                return CommandResult(0, "ok\nNVIDIA L40S, 46068 MiB, 3 %\n"
                                        "/dev/vda1  100G  20G  80G  20% /root\n")
            return CommandResult(0, "")
        if argv[0] == "rsync" and self.on_pull is not None and ":" in argv[-2]:
            self.on_pull(argv)
        return CommandResult(0, "")

    def ssh_commands(self) -> list[str]:
        return [c[-1] for c in self.calls if c[0] == "ssh"]

    def rsync_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "rsync"]


def wait_for(predicate: Callable[[], bool], timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -- git (hosted mode §12.3) ---------------------------------------------------------------------
GIT_ENV = {"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.org",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.org",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "LC_ALL": "C",
           "HOME": ""}


def git(cwd: Path, *args: str) -> str:
    """Run ``git`` in ``cwd`` (deterministic identity, no user config); return stdout."""
    import os
    import subprocess

    env = {**os.environ, **GIT_ENV, "HOME": str(cwd)}
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                          env=env, check=False)
    assert done.returncode == 0, f"git {' '.join(args)} : {done.stderr}"
    return done.stdout.strip()


def git_workspace(ws: Path, branch: str = "main") -> Path:
    """Turn ``ws`` into a git checkout tracking a bare remote ``origin``; return the bare
    repository path (``.wmlab`` is ignored)."""
    bare = ws.parent / "origin.git"
    git(ws.parent, "init", "--bare", "--initial-branch", branch, str(bare))
    (ws / ".gitignore").write_text(".wmlab/\n__pycache__/\n*.pyc\n", encoding="utf-8")
    git(ws, "init", "--initial-branch", branch)
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "initial")
    git(ws, "remote", "add", "origin", str(bare))
    git(ws, "push", "-q", "-u", "origin", branch)
    return bare


def git_clone(bare: Path, where: Path, branch: str = "main") -> Path:
    """Second clone of the bare remote (another team member's checkout)."""
    git(where.parent, "clone", "-q", "--branch", branch, str(bare), str(where))
    return where


def job_status_of(client: Any, job_id: str) -> dict[str, Any]:
    """The ``Job`` of ``job_id`` as returned by ``GET /api/jobs``."""
    return next(j for j in client.get("/api/jobs").json() if j["job_id"] == job_id)


PASSWORD = "mot-de-passe-de-test-123"


def add_user_session(ws: Path, name: str, role: str = "member", *, email: str | None = None,
                     password: str = PASSWORD) -> str:
    """Create an active account in ``<ws>/.wmlab/users.yaml`` and open a session; return
    the session token (``wmls_…``) to send in ``X-WMLab-Token``."""
    from wmlab.users import UserStore

    store = UserStore(ws / ".wmlab")
    user, _ = store.add(email or f"{name}@example.com", name, role, password=password, by="test")
    return store.open_session(user, "127.0.0.1")[1]
