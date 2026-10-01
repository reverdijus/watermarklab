"""Jobs: real local subprocesses (fake CLI), SSH jobs through an injected runner, estimates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from helpers_server import (
    JOB_KEYS,
    FakeRunner,
    make_fake_run,
    populate,
    wait_for,
    write_fake_cli,
)
from wmlab.server.app import create_app
from wmlab.server.jobs import JobManager
from wmlab.server.context import ServerContext
from wmlab.tracking import atomic_write_json, read_json

SPEC = "projects/demo/experiments/baseline.yaml"


@pytest.fixture
def ws(workspace: Path) -> Path:
    populate(workspace)
    return workspace


@pytest.fixture
def cli(tmp_path: Path) -> list[str]:
    return write_fake_cli(tmp_path)


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def live(ws: Path, cli: list[str], runner: FakeRunner) -> Iterator[TestClient]:
    """App with its background threads (jobs polled every 0.05 s)."""
    app = create_app(ws, None, background=True, runner=runner, cli_prefix=cli,
                     poll_interval=0.05, index_interval=0.2)
    with TestClient(app) as client:
        yield client


def job_status(client: TestClient, job_id: str) -> dict:
    return next(j for j in client.get("/api/jobs").json() if j["job_id"] == job_id)


def test_local_job_cycle(live: TestClient, ws: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WMLAB_FAKE_SLEEP", "0.6")
    first = live.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC,
                                         "overrides": {"optimizer.lr": "3e-4"}, "smoke": True,
                                         "max_hours": 1.5})
    assert first.status_code == 200, first.text
    first = first.json()
    assert set(first) == JOB_KEYS
    assert first["kind"] == "train" and first["target"] == SPEC and first["run_id"]
    assert first["run_id"].split("-")[2] == "baseline"
    second = live.post("/api/jobs", json={"kind": "train", "host": "local",
                                          "spec_path": SPEC}).json()
    # max_concurrent_jobs = 1 on "local": the second job waits
    assert job_status(live, second["job_id"])["status"] == "queued"
    assert wait_for(lambda: job_status(live, first["job_id"])["status"] == "running")
    running = job_status(live, first["job_id"])
    assert running["pid"] and running["started_at"]
    assert job_status(live, second["job_id"])["status"] == "queued"
    assert wait_for(lambda: job_status(live, first["job_id"])["status"] == "completed")
    assert wait_for(lambda: job_status(live, second["job_id"])["status"] == "running")
    assert wait_for(lambda: job_status(live, second["job_id"])["status"] == "completed")
    done = job_status(live, first["job_id"])
    assert done["ended_at"] and done["error"] is None

    data = ws / ".wmlab"
    manifest = read_json(data / "runs" / first["run_id"] / "run.json")
    argv = manifest["argv"]
    assert argv[:4] == ["train", SPEC, "--run-id", first["run_id"]]
    assert argv[4:6] == ["--set", "optimizer.lr=3e-4"]
    assert "--smoke" in argv and argv[argv.index("--max-hours") + 1] == "1.5"
    assert argv[-2] == "--launch-file"
    assert manifest["cwd"] == str(ws) and manifest["workspace"] == str(ws)
    assert manifest["host"] == "local"
    launch = read_json(Path(argv[-1]))
    assert launch["job_id"] == first["job_id"] and launch["host"] == "local"
    assert launch["overrides"] == {"optimizer.lr": "3e-4"} and "launched_at" in launch
    assert set(launch) >= {"git", "git_diff"}
    log = (data / "runs" / first["run_id"] / "stdout.log").read_text(encoding="utf-8")
    assert "fake cli train" in log
    assert (data / "jobs" / f"{first['job_id']}.json").is_file()
    # the index picks the new runs up
    assert wait_for(lambda: first["run_id"] in {r["run_id"] for r in
                                                live.get("/api/runs").json()}, timeout=5)
    completed = live.get("/api/jobs?status=completed").json()
    assert {j["job_id"] for j in completed} == {first["job_id"], second["job_id"]}


def test_local_job_cancel(live: TestClient, ws: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WMLAB_FAKE_MODE", "sleep")
    job = live.post("/api/jobs", json={"kind": "train", "host": "local",
                                       "spec_path": SPEC}).json()
    run_json = ws / ".wmlab" / "runs" / job["run_id"] / "run.json"
    assert wait_for(lambda: job_status(live, job["job_id"])["status"] == "running")
    assert wait_for(lambda: (read_json(run_json) or {}).get("status") == "running")
    cancelled = live.post(f"/api/jobs/{job['job_id']}/cancel")
    assert cancelled.status_code == 200 and set(cancelled.json()) == JOB_KEYS
    assert wait_for(lambda: job_status(live, job["job_id"])["status"] == "killed")
    assert read_json(run_json)["status"] == "killed"


def test_run_stop_cancels_its_job(live: TestClient, ws: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WMLAB_FAKE_MODE", "sleep")
    job = live.post("/api/jobs", json={"kind": "train", "host": "local",
                                       "spec_path": SPEC}).json()
    run_json = ws / ".wmlab" / "runs" / job["run_id"] / "run.json"
    assert wait_for(lambda: (read_json(run_json) or {}).get("status") == "running")
    assert wait_for(lambda: live.get(f"/api/runs/{job['run_id']}").status_code == 200)
    stopped = live.post(f"/api/runs/{job['run_id']}/stop").json()
    assert stopped["job_id"] == job["job_id"]
    assert wait_for(lambda: job_status(live, job["job_id"])["status"] == "killed")


def test_lost_job_closes_run_manifest(live: TestClient, ws: Path, monkeypatch: pytest.MonkeyPatch):
    """A process that vanishes (SIGKILL) leaves run.json ``running``: the job manager closes it."""
    import os
    import signal

    monkeypatch.setenv("WMLAB_FAKE_MODE", "sleep")
    job = live.post("/api/jobs", json={"kind": "train", "host": "local",
                                       "spec_path": SPEC}).json()
    run_json = ws / ".wmlab" / "runs" / job["run_id"] / "run.json"
    assert wait_for(lambda: (read_json(run_json) or {}).get("status") == "running")
    pid = job_status(live, job["job_id"])["pid"]
    os.killpg(os.getpgid(pid), signal.SIGKILL)
    assert wait_for(lambda: job_status(live, job["job_id"])["status"] == "lost")
    manifest = read_json(run_json)
    assert manifest["status"] == "lost" and manifest["ended_at"] and manifest["error"]
    assert wait_for(lambda: live.get(f"/api/runs/{job['run_id']}").json()["status"] == "lost",
                    timeout=5)
    assert job["run_id"] not in {r["run_id"] for r in live.get("/api/runs?status=running").json()}
    assert live.post(f"/api/runs/{job['run_id']}/stop").status_code == 409
    assert live.put(f"/api/runs/{job['run_id']}/tags", json={"tags": ["x"]}).status_code == 200


def test_cancel_queued_job(live: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WMLAB_FAKE_MODE", "sleep")
    first = live.post("/api/jobs", json={"kind": "train", "host": "local",
                                         "spec_path": SPEC}).json()
    second = live.post("/api/jobs", json={"kind": "train", "host": "local",
                                          "spec_path": SPEC}).json()
    killed = live.post(f"/api/jobs/{second['job_id']}/cancel").json()
    assert killed["status"] == "killed" and killed["started_at"] is None
    live.post(f"/api/jobs/{first['job_id']}/cancel")
    assert wait_for(lambda: job_status(live, first["job_id"])["status"] == "killed")


def test_failed_job_reports_log(live: TestClient, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WMLAB_FAKE_MODE", "fail")
    job = live.post("/api/jobs", json={"kind": "train", "host": "local",
                                       "spec_path": SPEC}).json()
    assert wait_for(lambda: job_status(live, job["job_id"])["status"] == "failed")
    assert "échec simulé" in job_status(live, job["job_id"])["error"]


def test_hpo_and_eval_jobs(live: TestClient, ws: Path):
    hpo = live.post("/api/jobs", json={"kind": "hpo", "host": "local",
                                       "spec_path": "projects/demo/hpo/sweep.yaml",
                                       "smoke": True}).json()
    assert hpo["study_id"] and hpo["run_id"] is None and hpo["max_hours"] is None
    assert wait_for(lambda: job_status(live, hpo["job_id"])["status"] == "completed")
    study = read_json(ws / ".wmlab" / "studies" / hpo["study_id"] / "study.json")
    assert study["argv"][:4] == ["hpo", "projects/demo/hpo/sweep.yaml", "--study-id",
                                 hpo["study_id"]]
    assert "--smoke" in study["argv"] and "--launch-file" in study["argv"]
    # overrides and max_hours are forwarded to `wmlab hpo --set … --max-hours …`
    hpo2 = live.post("/api/jobs", json={"kind": "hpo", "host": "local",
                                        "spec_path": "projects/demo/hpo/sweep.yaml",
                                        "overrides": {"seed": "1"}, "max_hours": 0.5,
                                        "smoke": True})
    assert hpo2.status_code == 200, hpo2.text
    hpo2 = hpo2.json()
    assert hpo2["overrides"] == {"seed": "1"} and hpo2["max_hours"] == 0.5
    assert wait_for(lambda: job_status(live, hpo2["job_id"])["status"] == "completed")
    study2 = read_json(ws / ".wmlab" / "studies" / hpo2["study_id"] / "study.json")
    argv = study2["argv"]
    assert argv[argv.index("--set") + 1] == "seed=1"
    assert argv[argv.index("--max-hours") + 1] == "0.5"
    assert live.post("/api/jobs", json={"kind": "hpo", "host": "local",
                                        "spec_path": "projects/demo/hpo/sweep.yaml",
                                        "overrides": {"nope.key": "1"}}).status_code == 400

    ev = live.post("/api/jobs", json={"kind": "eval", "host": "local", "run_id": "r1",
                                      "checkpoint": "last", "benchmark": "image_v1"})
    assert ev.status_code == 200, ev.text
    ev = ev.json()
    assert ev["target"] == "r1/last@image_v1" and ev["run_id"] == "r1"
    assert wait_for(lambda: job_status(live, ev["job_id"])["status"] == "completed")
    log = ws / ".wmlab" / "jobs" / "logs" / f"{ev['job_id']}.log"
    assert "fake cli eval --run r1 --checkpoint last --benchmark image_v1" in log.read_text(
        encoding="utf-8")


def test_job_validation_errors(ws: Path, cli: list[str]):
    client = TestClient(create_app(ws, None, background=False, cli_prefix=cli))
    cases = [
        ({"kind": "train", "host": "local"}, 400, "spec_path"),
        ({"kind": "train", "host": "local", "spec_path": "../x.yaml"}, 403, "hors du workspace"),
        ({"kind": "train", "host": "local", "spec_path": "projects/demo/hpo/sweep.yaml"}, 400,
         "n'est pas une expérience"),
        ({"kind": "train", "host": "local", "spec_path": "projects/demo/experiments/no.yaml"},
         404, "introuvable"),
        ({"kind": "train", "host": "local", "spec_path": SPEC,
          "overrides": {"optimizer.nope.x": "1"}}, 400, "Expérience invalide"),
        ({"kind": "train", "host": "local", "spec_path": SPEC, "max_hours": 0}, 400, "durée"),
        ({"kind": "eval", "host": "local", "run_id": "r1"}, 400, "requis"),
        ({"kind": "eval", "host": "local", "run_id": "r1", "checkpoint": "last",
          "benchmark": "absent"}, 400, "Benchmark inconnu"),
        ({"kind": "eval", "host": "local", "run_id": "r2", "checkpoint": "last",
          "benchmark": "image_v1"}, 400, "rapatriez"),
        ({"kind": "eval", "host": "scw", "run_id": "r1", "checkpoint": "last",
          "benchmark": "image_v1"}, 400, "entraîné sur « local »"),
        ({"kind": "eval", "host": "local", "run_id": "r1", "checkpoint": "best:val/absent",
          "benchmark": "image_v1"}, 400, "métrique"),
        ({"kind": "eval", "host": "local", "run_id": "r1", "checkpoint": "last",
          "benchmark": "../workspace"}, 400, "nom de benchmark invalide"),
        ({"kind": "eval", "host": "local", "run_id": "r1", "checkpoint": "last",
          "benchmark": "../projects/demo/experiments/baseline"}, 400, "nom de benchmark"),
    ]
    for body, status, fragment in cases:
        response = client.post("/api/jobs", json=body)
        assert response.status_code == status, (body, response.text)
        assert fragment in response.json()["detail"], (body, response.json())
    # NaN / Infinity would silently disable the duration cap
    for literal in ("NaN", "Infinity", "-Infinity"):
        response = client.post("/api/jobs", headers={"content-type": "application/json"},
                               content='{"kind": "train", "host": "local", "spec_path": "%s", '
                                       '"max_hours": %s}' % (SPEC, literal))
        assert response.status_code == 400 and "durée" in response.json()["detail"], (
            literal, response.text)
    assert client.get("/api/jobs").json() == []


def test_init_from_checks(ws: Path, cli: list[str]):
    client = TestClient(create_app(ws, None, background=False, cli_prefix=cli))
    remote_parent = client.post("/api/jobs", json={
        "kind": "train", "host": "local", "spec_path": SPEC,
        "init_from": {"run": "r2", "checkpoint": "last", "start_phase": "robust"}})
    assert remote_parent.status_code == 400
    assert "n'est pas présent sur « local »" in remote_parent.json()["detail"]
    wrong_host = client.post("/api/jobs", json={
        "kind": "train", "host": "scw", "spec_path": SPEC,
        "init_from": {"run": "r1", "checkpoint": "last"}})
    assert wrong_host.status_code == 400 and "entraîné sur « local »" in wrong_host.json()["detail"]
    missing = client.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC,
                                             "init_from": {"run": "absent"}})
    assert missing.status_code == 400 and "introuvable" in missing.json()["detail"]
    ok = client.post("/api/jobs", json={
        "kind": "train", "host": "local", "spec_path": SPEC, "smoke": True,
        "init_from": {"run": "r1", "checkpoint": "phase_end:warmup", "start_phase": "robust"}})
    assert ok.status_code == 200, ok.text
    job = ok.json()
    init = json.loads(job["overrides"]["init_from"])
    assert init == {"run": "r1", "checkpoint": "phase_end:warmup", "start_phase": "robust"}
    assert wait_for(lambda: job_status(client, job["job_id"])["status"] == "completed")
    resumed = client.post("/api/jobs", json={
        "kind": "train", "host": "local", "spec_path": SPEC, "smoke": True,
        "init_from": {"run": "r1", "checkpoint": "last", "resume": True, "start_phase": "robust"}})
    assert resumed.status_code == 200, resumed.text
    init = json.loads(resumed.json()["overrides"]["init_from"])
    assert init == {"run": "r1", "checkpoint": "last", "start_phase": None, "resume": True}
    argv = read_json(ws / ".wmlab" / "runs" / job["run_id"] / "run.json")["argv"]
    assert f"init_from={job['overrides']['init_from']}" in argv


def test_ssh_job_lifecycle(ws: Path, runner: FakeRunner):
    client = TestClient(create_app(ws, None, background=False, runner=runner))
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC,
                                         "overrides": {"optimizer.lr": "3e-4"}}).json()
    assert wait_for(lambda: job_status(client, job["job_id"])["status"] == "running")
    running = job_status(client, job["job_id"])
    assert running["pid"] == 4242
    commands = runner.ssh_commands()
    assert commands[0].startswith("mkdir -p /root/wmlab /root/wmlab-data/runs")
    launch = next(c for c in commands if "nohup setsid" in c)
    run_id = job["run_id"]
    assert launch.startswith("cd /root/wmlab && { WMLAB_DATA=/root/wmlab-data WMLAB_HOST=scw "
                             "WMLAB_PRICE_EUR_PER_HOUR=1.5 nohup setsid .venv/bin/python -m "
                             f"wmlab.cli train {SPEC} --run-id {run_id} "
                             "--set optimizer.lr=3e-4 --launch-file "
                             f"/root/wmlab-data/launch/{job['job_id']}.json")
    assert launch.endswith(f"> /root/wmlab-data/runs/{run_id}/stdout.log 2>&1 < /dev/null "
                           "& echo $!; ps -o lstart= -p $! 2>/dev/null || true; }")
    push, launch_copy = runner.rsync_calls()[:2]
    assert push[:3] == ["rsync", "-az", "--delete"]
    for pattern in (".git", ".venv", ".wmlab", "desktop", "node_modules", "__pycache__"):
        assert pattern in push
    assert push[-2] == f"{ws}/" and push[-1] == "root@51.15.0.1:/root/wmlab/"
    assert launch_copy[-1] == f"root@51.15.0.1:/root/wmlab-data/launch/{job['job_id']}.json"

    # the remote process ends: the final rsync brings back a completed run.json
    def on_pull(argv: list[str]) -> None:
        local = Path(argv[-1])
        atomic_write_json(local / "run.json", {"run_id": run_id, "status": "completed",
                                               "project": "demo", "experiment": "baseline",
                                               "created_at": "2026-03-01T00:00:00Z"})

    runner.on_pull = on_pull
    runner.alive = True
    client.post(f"/api/jobs/{job['job_id']}/sync")
    pull = runner.rsync_calls()[-1]  # while running: light sync (no .pt)
    assert pull[:7] == ["rsync", "-az", "--safe-links", "--exclude", "checkpoints/*.pt",
                        "--exclude", "checkpoints/.*.tmp"]
    runner.alive = False
    synced = client.post(f"/api/jobs/{job['job_id']}/sync").json()
    assert synced["status"] == "completed" and synced["last_sync_at"]
    pull = runner.rsync_calls()[-1]  # end of job: full sync (*.pt included, §12.4)
    assert pull[:5] == ["rsync", "-az", "--safe-links", "--exclude", "checkpoints/.*.tmp"]
    assert "checkpoints/*.pt" not in pull
    assert pull[-2] == f"root@51.15.0.1:/root/wmlab-data/runs/{run_id}/"
    assert pull[-1] == f"{ws / '.wmlab' / 'runs' / run_id}/"
    assert synced["retrieval"] == {"status": "complete", "checkpoints": 0, "bytes": 0,
                                   "missing": []}
    assert synced["user"] == "admin" and synced["git_ref"] is None and synced["git_sha"] is None


def test_ssh_job_cancel_and_lost(ws: Path, runner: FakeRunner):
    client = TestClient(create_app(ws, None, background=False, runner=runner))
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw",
                                         "spec_path": SPEC}).json()
    assert wait_for(lambda: job_status(client, job["job_id"])["status"] == "running")
    client.post(f"/api/jobs/{job['job_id']}/cancel")
    assert runner.ssh_commands()[-1] == "kill -TERM -4242 2>/dev/null || kill -TERM 4242"
    runner.alive = False
    assert client.post(f"/api/jobs/{job['job_id']}/sync").json()["status"] == "killed"

    other = client.post("/api/jobs", json={"kind": "train", "host": "scw",
                                           "spec_path": SPEC}).json()
    runner.alive = True
    assert wait_for(lambda: job_status(client, other["job_id"])["status"] == "running")
    runner.fail_ssh = True  # unreachable host: the job stays running
    assert client.post(f"/api/jobs/{other['job_id']}/sync").json()["status"] == "running"
    runner.fail_ssh, runner.alive = False, False
    lost = client.post(f"/api/jobs/{other['job_id']}/sync").json()
    assert lost["status"] == "lost" and "statut final" in lost["error"]


def test_jobs_reattached_after_restart(ws: Path, cli: list[str],
                                       monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WMLAB_FAKE_MODE", "sleep")
    ctx = ServerContext.for_workspace(ws)
    first = JobManager(ctx, cli_prefix=cli)
    job = first.submit(kind="train", host="local", spec_path=SPEC)
    assert wait_for(lambda: first.get(job.job_id).status == "running")
    run_json = ws / ".wmlab" / "runs" / str(job.run_id) / "run.json"
    assert wait_for(lambda: (read_json(run_json) or {}).get("status") == "running")
    # a new manager (server restart) re-attaches the running job from its file
    second = JobManager(ctx, cli_prefix=cli)
    again = second.get(job.job_id)
    assert again.status == "running" and again.pid == job.pid
    second.tick()
    assert second.get(job.job_id).status == "running"
    second.cancel(job.job_id)
    assert wait_for(lambda: (read_json(run_json) or {}).get("status") == "killed")
    first.tick()  # reaps the child process
    assert wait_for(lambda: (second.tick(), second.get(job.job_id).status)[1] == "killed")


def test_launching_job_without_pid_is_lost_on_restart(ws: Path):
    ctx = ServerContext.for_workspace(ws)
    atomic_write_json(ctx.jobs_dir / "job-20260101-000000-abcd.json", {
        "job_id": "job-20260101-000000-abcd", "kind": "train", "host": "local",
        "target": SPEC, "status": "launching", "created_at": "2026-01-01T00:00:00Z"})
    job = JobManager(ctx).get("job-20260101-000000-abcd")
    assert job.status == "lost" and "lancement" in job.error


def test_estimate(ws: Path):
    client = TestClient(create_app(ws, None, background=False))
    local = client.post("/api/jobs/estimate", json={"spec_path": SPEC, "host": "local"}).json()
    assert set(local) == {"total_steps", "est_hours", "est_eur", "basis"}
    assert local["total_steps"] == 30
    assert local["est_hours"] == pytest.approx(30 / (3000 / 7200) / 3600, abs=1e-3)
    assert local["est_eur"] == 0.0 and "« baseline »" in local["basis"]
    scw = client.post("/api/jobs/estimate", json={
        "spec_path": SPEC, "host": "scw", "overrides": {"phases.0.steps": "3590"}}).json()
    assert scw["total_steps"] == 3610
    assert scw["est_hours"] == pytest.approx(3610 / 1.0 / 3600, abs=1e-3)
    assert scw["est_eur"] == pytest.approx(scw["est_hours"] * 1.5, abs=0.01)
    hpo = client.post("/api/jobs/estimate", json={"spec_path": "projects/demo/hpo/sweep.yaml",
                                                  "host": "scw"}).json()
    assert hpo["total_steps"] == 15 * 4  # fidelity 0.5 × 4 trials
    hosts = client.get("/api/hosts").json() + [{"name": "gpu2", "kind": "ssh",
                                                "ssh_target": "gpu2", "price_eur_per_hour": 3}]
    client.put("/api/hosts", json=hosts)
    unknown = client.post("/api/jobs/estimate", json={"spec_path": SPEC, "host": "gpu2"}).json()
    assert unknown["est_hours"] is None and unknown["est_eur"] is None
    assert "estimation impossible" in unknown["basis"]
    assert client.post("/api/jobs/estimate", json={"spec_path": SPEC, "host": "nope"}
                       ).status_code == 400


def test_estimate_same_modality_fallback(ws: Path):
    make_fake_run(ws / ".wmlab" / "runs", "r9", host="local", steps=600, hours=0.5,
                  with_eval=False)
    manifest = read_json(ws / ".wmlab" / "runs" / "r9" / "run.json")
    manifest["experiment"] = "other"
    atomic_write_json(ws / ".wmlab" / "runs" / "r9" / "run.json", manifest)
    for run_id in ("r1",):
        path = ws / ".wmlab" / "runs" / run_id / "run.json"
        data = read_json(path)
        data["experiment"] = "legacy"
        atomic_write_json(path, data)
    client = TestClient(create_app(ws, None, background=False))
    body = client.post("/api/jobs/estimate", json={"spec_path": SPEC, "host": "local"}).json()
    assert "Aucun run de « baseline »" in body["basis"]
    assert body["est_hours"] is not None
