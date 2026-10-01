"""Hosted mode §12.4: end-of-job retrieval, host inventory, retrieve jobs, decommission and
power-off guards (fake SSH runner producing the remote listing and the ``.pt`` files)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from helpers_server import (
    INVENTORY_KEYS,
    INVENTORY_RUN_KEYS,
    JOB_KEYS,
    RETRIEVAL_KEYS,
    SCW_HOST,
    FakeRunner,
    add_user_session,
    job_status_of,
    populate,
    wait_for,
)
from wmlab.server.app import create_app
from wmlab.server.context import ServerContext
from wmlab.server.executors import CommandResult, ExecContext, JobRecord, SshExecutor
from wmlab.server.inventory import (
    InventoryCache,
    build_inventory,
    listing_command,
    parse_listing,
    retrieval_result,
    verify_runs,
)
from wmlab.server.jobs import JobManager
from wmlab.spec import HostSpec
from wmlab.tracking import atomic_write_json, read_json

SPEC = "projects/demo/experiments/baseline.yaml"
TOKEN = "t"

LISTING = (
    "==RUNS==\n"
    'runs/r2/run.json\t{"run_id": "r2", "project": "demo", "status": "completed"}\n'
    'runs/r3/run.json\t{"run_id": "r3", "project": "demo", "status": "running"}\n'
    'runs/remote-only/run.json\t{"format_version": 1, "run_id": "remote-only", "project": "demo",'
    ' "experiment": "baseline", "modality": "image", "status": "completed",'
    ' "created_at": "2026-03-04T10:00:00Z", "started_at": "2026-03-04T10:00:00Z",'
    ' "ended_at": "2026-03-04T11:00:00Z", "host": "scw", "price_eur_per_hour": 1.5}\n'
    "runs/../etc/run.json\t{}\n"
    "==INDEX==\n"
    'runs/r2/checkpoints/index.json\t[{"name": "step_000010", "file": "step_000010.pt",'
    ' "step": 10, "size_bytes": 100}, {"name": "step_000030", "file": "step_000030.pt",'
    ' "step": 30, "size_bytes": 200}]\n'
    'runs/remote-only/checkpoints/index.json\t[{"name": "step_000005",'
    ' "file": "step_000005.pt", "step": 5, "size_bytes": 50}]\n'
    "runs/broken/checkpoints/index.json\tnot json\n"
    "==FILES==\n"
    "runs/r2/checkpoints/step_000010.pt\t100\n"
    "runs/r2/checkpoints/step_000030.pt\t200\n"
    "runs/remote-only/checkpoints/step_000005.pt\t50\n"
    "runs/r3/checkpoints/.step_000040.pt.tmp\t7\n"
    "==STUDIES==\n"
    "studies/s1/study.json\t5\n"
    "studies/s9/study.json\t2\n"
    "==END==\n"
)
REMOTE_SIZES: dict[str, dict[str, int]] = {
    "r2": {"step_000010.pt": 100, "step_000030.pt": 200},
    "remote-only": {"step_000005.pt": 50},
}


class RemoteRunner(FakeRunner):
    """FakeRunner answering the inventory command and materialising pulled folders."""

    def __init__(self) -> None:
        super().__init__()
        self.listing = LISTING
        self.sizes = {run: dict(files) for run, files in REMOTE_SIZES.items()}
        self.pull_sizes: dict[str, dict[str, int]] | None = None  # what rsync really writes
        self.pull_code = 0
        self.pulled: list[str] = []
        self.on_pull = self._materialise

    def __call__(self, argv: list[str], *, timeout: float | None = None) -> CommandResult:
        if argv[0] == "ssh" and not self.fail_ssh and "==RUNS==" in argv[-1]:
            self.calls.append(list(argv))
            return CommandResult(0, self.listing)
        if argv[0] == "rsync" and ":" in argv[-2] and self.pull_code:
            self.calls.append(list(argv))
            return CommandResult(self.pull_code, "", "rsync: connection unexpectedly closed")
        return super().__call__(argv, timeout=timeout)

    def _materialise(self, argv: list[str]) -> None:
        remote, local = argv[-2], Path(argv[-1])
        kind, item = remote.rstrip("/").split("/")[-2:]
        self.pulled.append(f"{kind}/{item}")
        full = "checkpoints/*.pt" not in argv
        local.mkdir(parents=True, exist_ok=True)
        if kind == "runs":
            sizes = (self.pull_sizes or self.sizes).get(item, {})
            index = [{"name": name[:-3], "file": name, "step": i, "phase": "p", "metrics": {},
                      "tags": [], "created_at": "2026-03-04T10:00:00Z",
                      "size_bytes": self.sizes.get(item, {}).get(name, size)}
                     for i, (name, size) in enumerate(sorted(sizes.items()))]
            if index:
                atomic_write_json(local / "checkpoints" / "index.json", index)
            if full:
                for name, size in sizes.items():
                    (local / "checkpoints").mkdir(parents=True, exist_ok=True)
                    (local / "checkpoints" / name).write_bytes(b"x" * size)
            if item == "remote-only" and not (local / "run.json").is_file():
                line = next(line for line in self.listing.splitlines()
                            if line.startswith("runs/remote-only/run.json"))
                atomic_write_json(local / "run.json", json.loads(line.split("\t", 1)[1]))
        elif kind == "studies" and item == "s9":
            atomic_write_json(local / "study.json", {"study_id": "s9", "status": "completed",
                                                     "project": "demo", "plan": "sweep"})
            (local / "trials.jsonl").write_text('{"number": 0}\n{"number": 1}\n', encoding="utf-8")


@pytest.fixture
def ws(workspace: Path) -> Path:
    populate(workspace)
    return workspace


@pytest.fixture
def runner() -> RemoteRunner:
    return RemoteRunner()


@pytest.fixture
def client(ws: Path, runner: RemoteRunner) -> TestClient:
    return TestClient(create_app(ws, TOKEN, background=False, runner=runner),
                      headers={"X-WMLab-Token": TOKEN})


def executor(ws: Path, runner: FakeRunner) -> SshExecutor:
    ctx = ExecContext(workspace=ws, data=ws / ".wmlab", runner=runner)
    return SshExecutor(HostSpec.model_validate(SCW_HOST), ctx)


# -- parsing & verification ---------------------------------------------------------------------
def test_listing_command_and_parsing():
    command = listing_command("'/root/wm data'")
    assert command.startswith("cd '/root/wm data' 2>/dev/null || { echo '==NODATA=='; exit 0; }; ")
    assert "find runs -path '*/checkpoints/*.pt' -type f -printf '%p\\t%s\\n'" in command
    assert "runs/*/checkpoints/index.json" in command and "studies/*/study.json" in command
    listing = parse_listing(LISTING)
    assert listing.complete and listing.data_present
    assert listing.run_ids == ["r2", "r3", "remote-only"]  # ``..`` and ``broken`` ignored
    assert listing.runs["r3"]["status"] == "running" and listing.runs["r2"]["project"] == "demo"
    assert [e["name"] for e in listing.indexes["r2"]] == ["step_000010", "step_000030"]
    assert "broken" not in listing.indexes
    assert listing.files == REMOTE_SIZES  # the .tmp file is not a checkpoint
    assert listing.studies == {"s1": 5, "s9": 2}
    empty = parse_listing("==NODATA==\n")
    assert empty.data_present is False and empty.run_ids == []
    truncated = parse_listing("==RUNS==\nruns/a/run.json\t{}\n")
    assert truncated.complete is False and truncated.run_ids == ["a"]


def test_build_inventory_and_verify(ws: Path):
    listing = parse_listing(LISTING)
    inventory = build_inventory("scw", listing, ws / ".wmlab", {"r1", "r2", "r3", "t1"},
                                active_jobs=1, remote_data_dir="/root/wmlab-data")
    assert set(inventory) == INVENTORY_KEYS and all(set(r) == INVENTORY_RUN_KEYS
                                                    for r in inventory["runs"])
    by_id = {r["run_id"]: r for r in inventory["runs"]}
    assert by_id["r2"] == {"run_id": "r2", "project": "demo", "status": "completed",
                           "checkpoints_total": 2, "checkpoints_local": 0, "bytes_remote": 300,
                           "bytes_missing": 300, "indexed": True}
    assert by_id["r3"]["checkpoints_total"] == 0 and by_id["r3"]["indexed"] is True
    assert by_id["remote-only"]["indexed"] is False and by_id["remote-only"]["bytes_missing"] == 50
    assert inventory["unretrieved_checkpoints"] == 3 and inventory["unretrieved_bytes"] == 350
    assert inventory["unindexed_runs"] == 1 and inventory["active_jobs"] == 1
    assert inventory["studies"] == [{"study_id": "s1", "trials": 5, "synced": True},
                                    {"study_id": "s9", "trials": 2, "synced": False}]
    # a local file with the right size counts as retrieved; a wrong size does not
    ckpt = ws / ".wmlab" / "runs" / "r2" / "checkpoints"
    (ckpt / "step_000010.pt").write_bytes(b"x" * 100)
    (ckpt / "step_000030.pt").write_bytes(b"x" * 199)
    again = build_inventory("scw", listing, ws / ".wmlab", set(), active_jobs=0,
                            remote_data_dir=None)
    r2 = next(r for r in again["runs"] if r["run_id"] == "r2")
    assert r2["checkpoints_local"] == 1 and r2["bytes_missing"] == 200
    atomic_write_json(ckpt / "index.json", [
        {"name": "step_000010", "file": "step_000010.pt", "size_bytes": 100},
        {"name": "step_000030", "file": "step_000030.pt", "size_bytes": 200}])
    check = verify_runs(ws / ".wmlab" / "runs", ["r2", "absent"])
    assert check["checkpoints"] == 1 and check["bytes"] == 100
    assert check["missing"] == ["r2/checkpoints/step_000030.pt"]
    assert retrieval_result(check) == {"status": "partial", "checkpoints": 1, "bytes": 100,
                                       "missing": ["r2/checkpoints/step_000030.pt"]}
    assert retrieval_result(check, failed=True)["status"] == "failed"
    (ckpt / "step_000030.pt").write_bytes(b"x" * 200)
    assert retrieval_result(verify_runs(ws / ".wmlab" / "runs", ["r2"]))["status"] == "complete"


def test_inventory_cache(tmp_path: Path):
    cache = InventoryCache(tmp_path / "hosts_inventory.json")
    assert cache.get("scw") is None and cache.all() == {}
    cache.set("scw", {"host": "scw", "reachable": True})
    cache.set("gpu2", {"host": "gpu2", "reachable": False})
    assert set(cache.all()) == {"scw", "gpu2"} and cache.get("scw")["reachable"] is True
    cache.remove("gpu2")
    assert cache.get("gpu2") is None


# -- end-of-job retrieval -----------------------------------------------------------------------
def train_job(run_id: str = "r2") -> JobRecord:
    return JobRecord(job_id="job-r", kind="train", host="scw", target=SPEC, run_id=run_id,
                     pid=4242, status="running")


def test_executor_retrieve_complete_partial_failed(ws: Path, runner: RemoteRunner):
    ex = executor(ws, runner)
    result = ex.retrieve(train_job())
    assert set(result) == RETRIEVAL_KEYS
    assert result == {"status": "complete", "checkpoints": 2, "bytes": 300, "missing": []}
    pull = runner.rsync_calls()[-1]
    assert "checkpoints/*.pt" not in pull and "checkpoints/.*.tmp" in pull
    assert (ws / ".wmlab" / "runs" / "r2" / "checkpoints" / "step_000030.pt").stat().st_size == 200
    # a transfer that leaves a wrong size is retried once, then reported partial
    runner.pull_sizes = {"r2": {"step_000010.pt": 100, "step_000030.pt": 150}}
    runner.pulled.clear()
    partial = ex.retrieve(train_job())
    assert partial["status"] == "partial" and partial["missing"] == ["r2/checkpoints/step_000030.pt"]
    assert partial["checkpoints"] == 1 and partial["bytes"] == 100
    assert runner.pulled == ["runs/r2", "runs/r2"]  # one retry
    runner.pull_sizes = None
    assert ex.retrieve(train_job())["status"] == "complete"
    runner.pull_code = 255
    failed = ex.retrieve(train_job("absent-run"))
    assert failed["status"] == "failed" and failed["checkpoints"] == 0
    assert ex.retrieve(JobRecord(job_id="s", kind="setup", host="scw", target="scw")) is None


def test_executor_retrieve_hpo_pulls_trials(ws: Path, runner: RemoteRunner):
    ex = executor(ws, runner)
    runner.sizes["t1"] = {"step_000010.pt": 10}
    study = ws / ".wmlab" / "studies" / "s1"
    rows = [json.loads(line) for line in (study / "trials.jsonl").read_text().splitlines()]
    for row in rows:
        row["run_id"] = "t1" if row["number"] == 0 else None
    (study / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = ex.retrieve(JobRecord(job_id="job-h", kind="hpo", host="scw", target="x",
                                   study_id="s1"))
    assert result["status"] == "complete" and result["checkpoints"] == 1 and result["bytes"] == 10
    assert runner.pulled[0] == "studies/s1" and "runs/t1" in runner.pulled


def test_job_retrieval_recorded_and_repaired(client: TestClient, ws: Path, runner: RemoteRunner):
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC}).json()
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "running")
    run_id = job["run_id"]
    runner.sizes[run_id] = {"step_000020.pt": 20}
    runner.pull_sizes = {run_id: {"step_000020.pt": 5}}  # the remote file keeps changing…

    def finish_manifest(argv: list[str]) -> None:
        runner._materialise(argv)
        local = Path(argv[-1])
        if local.name == run_id:
            atomic_write_json(local / "run.json", {"run_id": run_id, "status": "completed",
                                                   "project": "demo", "experiment": "baseline",
                                                   "created_at": "2026-03-01T00:00:00Z"})

    runner.on_pull = finish_manifest
    runner.alive = False
    done = client.post(f"/api/jobs/{job['job_id']}/sync").json()
    assert done["status"] == "completed"  # the job stays completed even if partial
    assert done["retrieval"] == {"status": "partial", "checkpoints": 0, "bytes": 0,
                                 "missing": [f"{run_id}/checkpoints/step_000020.pt"]}
    runner.pull_sizes = None  # …a manual sync of the finished job repairs it
    repaired = client.post(f"/api/jobs/{job['job_id']}/sync").json()
    assert repaired["status"] == "completed" and repaired["retrieval"]["status"] == "complete"
    assert repaired["retrieval"]["checkpoints"] == 1 and repaired["retrieval"]["bytes"] == 20
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200 and detail.json()["checkpoints"][0]["available_locally"]


# -- inventory route ------------------------------------------------------------------------------
def test_inventory_route_and_host_fields(client: TestClient, ws: Path, runner: RemoteRunner):
    hosts = {h["name"]: h for h in client.get("/api/hosts").json()}
    assert hosts["scw"]["unretrieved_checkpoints"] is None and hosts["scw"]["inventory_at"] is None
    assert hosts["scw"]["active_jobs"] == 0 and hosts["scw"]["scaleway"] is None
    inventory = client.get("/api/hosts/scw/inventory")
    assert inventory.status_code == 200, inventory.text
    inventory = inventory.json()
    assert set(inventory) == INVENTORY_KEYS
    assert inventory["host"] == "scw" and inventory["reachable"] and inventory["error"] is None
    assert inventory["remote_data_dir"] == "/root/wmlab-data"
    assert inventory["unretrieved_checkpoints"] == 3 and inventory["unretrieved_bytes"] == 350
    assert inventory["unindexed_runs"] == 1
    command = runner.calls[-1]
    assert command[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--"]
    assert command[-1] == listing_command("/root/wmlab-data")
    cached = read_json(ws / ".wmlab" / "hosts_inventory.json")
    assert cached["scw"]["checked_at"] == inventory["checked_at"]
    scw = next(h for h in client.get("/api/hosts").json() if h["name"] == "scw")
    assert scw["unretrieved_checkpoints"] == 3 and scw["unretrieved_bytes"] == 350
    assert scw["inventory_at"] == inventory["checked_at"]
    # unreachable host
    runner.fail_ssh = True
    down = client.get("/api/hosts/scw/inventory").json()
    assert down["reachable"] is False and "Connexion SSH impossible" in down["error"]
    assert down["runs"] == [] and down["unretrieved_checkpoints"] == 0
    scw = next(h for h in client.get("/api/hosts").json() if h["name"] == "scw")
    assert scw["unretrieved_checkpoints"] is None and scw["inventory_at"] == down["checked_at"]
    # local host: trivial inventory
    local = client.get("/api/hosts/local/inventory").json()
    assert local["reachable"] and local["unretrieved_checkpoints"] == 0
    assert {r["run_id"] for r in local["runs"]} == {"r1"}
    assert client.get("/api/hosts/nope/inventory").status_code == 404
    # the computed fields are accepted (and dropped) when the hosts list is saved back
    saved = client.put("/api/hosts", json=client.get("/api/hosts").json())
    assert saved.status_code == 200, saved.text
    raw = yaml.safe_load((ws / ".wmlab" / "hosts.yaml").read_text(encoding="utf-8"))
    assert "active_jobs" not in raw["hosts"][1] and "inventory_at" not in raw["hosts"][1]


# -- retrieve job ---------------------------------------------------------------------------------
def test_retrieve_job(client: TestClient, ws: Path, runner: RemoteRunner):
    job = client.post("/api/hosts/scw/retrieve")
    assert job.status_code == 200, job.text
    job = job.json()
    assert set(job) == JOB_KEYS and job["kind"] == "retrieve" and job["target"] == "scw"
    assert job["user"] == "admin"
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "completed")
    done = job_status_of(client, job["job_id"])
    r3_pt = sum(p.stat().st_size for p in (ws / ".wmlab" / "runs" / "r3" / "checkpoints").glob("*.pt"))
    assert done["result"] == {"runs": 3, "studies": 2, "checkpoints": 5, "bytes": 350 + r3_pt,
                              "missing": []}  # r3's checkpoints were already local
    assert done["retrieval"] is None and done["error"] is None
    assert sorted(runner.pulled) == ["runs/r2", "runs/r3", "runs/remote-only", "studies/s1",
                                     "studies/s9"]
    assert (ws / ".wmlab" / "runs" / "remote-only" / "checkpoints" / "step_000005.pt").is_file()
    # reindexed: the remote-only run is now known, the inventory is clean
    assert "remote-only" in {r["run_id"] for r in client.get("/api/runs").json()}
    scw = next(h for h in client.get("/api/hosts").json() if h["name"] == "scw")
    assert scw["unretrieved_checkpoints"] == 0
    inventory = client.get("/api/hosts/scw/inventory").json()
    assert inventory["unretrieved_checkpoints"] == 0 and inventory["unindexed_runs"] == 0
    assert all(s["synced"] for s in inventory["studies"])
    assert client.post("/api/hosts/local/retrieve").status_code == 400


def test_retrieve_job_does_not_count_and_reports_failure(client: TestClient, ws: Path,
                                                         runner: RemoteRunner):
    # two running jobs fill max_concurrent_jobs = 2 of scw: the retrieval still starts
    first = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC}).json()
    second = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC}).json()
    assert wait_for(lambda: job_status_of(client, second["job_id"])["status"] == "running")
    runner.pull_code = 255
    job = client.post("/api/hosts/scw/retrieve").json()
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "failed")
    failed = job_status_of(client, job["job_id"])
    assert "Rapatriement échoué" in failed["error"] and failed["result"]["runs"] == 0
    assert job_status_of(client, first["job_id"])["status"] == "running"
    runner.pull_code = 0
    again = client.post("/api/hosts/scw/retrieve").json()
    assert wait_for(lambda: job_status_of(client, again["job_id"])["status"] == "completed")
    duplicate = client.post("/api/hosts/scw/retrieve")  # nothing active: allowed again
    assert duplicate.status_code == 200


def test_retrieve_job_lost_on_restart_then_relaunched(ws: Path, runner: RemoteRunner):
    ctx = ServerContext.for_workspace(ws)
    atomic_write_json(ctx.jobs_dir / "job-20260101-000000-ret1.json", {
        "job_id": "job-20260101-000000-ret1", "kind": "retrieve", "host": "scw", "target": "scw",
        "status": "running", "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:00:01Z", "result": {"runs": 1, "studies": 0,
                                                          "checkpoints": 0, "bytes": 0,
                                                          "missing": []}})
    manager = JobManager(ctx, runner=runner)
    lost = manager.get("job-20260101-000000-ret1")
    assert lost.status == "lost" and lost.ended_at and "arrêté" in lost.error
    assert read_json(ctx.jobs_dir / "job-20260101-000000-ret1.json")["status"] == "lost"
    relaunched = manager.retrieve_host("scw", "admin")
    assert relaunched.job_id != lost.job_id
    assert wait_for(lambda: manager.get(relaunched.job_id).status == "completed")
    with pytest.raises(Exception, match="déjà en cours"):
        manager._jobs[relaunched.job_id].status = "running"
        manager.retrieve_host("scw")


# -- decommission & poweroff -------------------------------------------------------------------
def test_decommission_guards(client: TestClient, ws: Path, runner: RemoteRunner,
                             monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCW_SECRET_KEY", raising=False)
    assert client.post("/api/hosts/scw/decommission", json={}).status_code == 400
    assert client.post("/api/hosts/local/decommission", json={"confirm": True}).status_code == 400
    assert client.post("/api/hosts/nope/decommission", json={"confirm": True}).status_code == 404
    # active job
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC}).json()
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "running")
    busy = client.post("/api/hosts/scw/decommission", json={"confirm": True})
    assert busy.status_code == 409 and "actif" in busy.json()["detail"]
    client.post(f"/api/jobs/{job['job_id']}/cancel")
    runner.alive = False
    client.post(f"/api/jobs/{job['job_id']}/sync")
    # unretrieved checkpoints (inventory recomputed at the call)
    refused = client.post("/api/hosts/scw/decommission", json={"confirm": True})
    assert refused.status_code == 409
    assert refused.json()["detail"].startswith("3 checkpoints (0,0 Go) non rapatriés")
    assert "lancez le rapatriement" in refused.json()["detail"]
    assert "1 run(s) non indexé(s)" in refused.json()["detail"]
    # unreachable ⇒ 409 unless forced
    runner.fail_ssh = True
    down = client.post("/api/hosts/scw/decommission", json={"confirm": True})
    assert down.status_code == 409 and "injoignable" in down.json()["detail"]
    runner.fail_ssh = False
    # retrieval, then the decommission succeeds
    retrieve = client.post("/api/hosts/scw/retrieve").json()
    assert wait_for(lambda: job_status_of(client, retrieve["job_id"])["status"] == "completed")
    ok = client.post("/api/hosts/scw/decommission", json={"confirm": True})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert set(body) == {"ok", "deleted_instance", "instance_state", "message"}
    assert body["ok"] is True and body["deleted_instance"] is False
    assert body["instance_state"] is None and "retirée" in body["message"]
    scw = next(h for h in client.get("/api/hosts").json() if h["name"] == "scw")
    assert scw["decommissioned_at"]
    raw = yaml.safe_load((ws / ".wmlab" / "hosts.yaml").read_text(encoding="utf-8"))
    assert raw["hosts"][1]["decommissioned_at"] == scw["decommissioned_at"]
    # a decommissioned host refuses jobs, setup, retrieval is still possible, no double retirement
    launch = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC})
    assert launch.status_code == 400 and "retirée" in launch.json()["detail"]
    assert client.post("/api/hosts/scw/setup").status_code == 400
    assert client.post("/api/hosts/scw/retrieve").status_code == 200
    again = client.post("/api/hosts/scw/decommission", json={"confirm": True})
    assert again.status_code == 409 and "déjà retirée" in again.json()["detail"]


def test_decommission_forced_when_unreachable(client: TestClient, ws: Path, runner: RemoteRunner):
    runner.fail_ssh = True
    forced = client.post("/api/hosts/scw/decommission", json={"confirm": True, "force": True})
    assert forced.status_code == 200, forced.text
    assert forced.json()["ok"] is True
    log = (ws / ".wmlab" / "logs" / "server.log").read_text(encoding="utf-8")
    assert "retrait forcé de scw" in log
    # delete_instance without a Scaleway configuration: explained, not an error
    hosts = client.get("/api/hosts").json()
    hosts[1]["decommissioned_at"] = None
    client.put("/api/hosts", json=hosts)
    body = client.post("/api/hosts/scw/decommission",
                       json={"confirm": True, "force": True, "delete_instance": True}).json()
    assert body["deleted_instance"] is False and "Aucune instance Scaleway" in body["message"]


def test_decommission_requires_admin(ws: Path, runner: RemoteRunner):
    member = add_user_session(ws, "marie", "member")
    client = TestClient(create_app(ws, TOKEN, background=False, runner=runner),
                        headers={"X-WMLab-Token": member})
    assert client.post("/api/hosts/scw/decommission", json={"confirm": True}).status_code == 403
    assert client.post("/api/hosts/scw/poweroff").status_code == 403
    # a member is read-only: no inventory (SSH command), no retrieval job
    assert client.get("/api/hosts/scw/inventory").status_code == 403
    assert client.post("/api/hosts/scw/retrieve").status_code == 403
    hosts = client.get("/api/hosts")
    assert hosts.status_code == 200 and all(h["ssh_target"] is None for h in hosts.json())


def test_poweroff_guards(client: TestClient, ws: Path, runner: RemoteRunner,
                         monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCW_SECRET_KEY", raising=False)
    assert client.post("/api/hosts/local/poweroff").status_code == 400
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC}).json()
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "running")
    busy = client.post("/api/hosts/scw/poweroff")
    assert busy.status_code == 409 and "actif" in busy.json()["detail"]
    client.post(f"/api/jobs/{job['job_id']}/cancel")
    runner.alive = False
    client.post(f"/api/jobs/{job['job_id']}/sync")
    missing = client.post("/api/hosts/scw/poweroff")
    assert missing.status_code == 400 and "Scaleway" in missing.json()["detail"]
    hosts = client.get("/api/hosts").json()
    hosts[1]["scaleway"] = {"zone": "fr-par-2", "server_id": "11111111-2222-3333-4444-555555555555"}
    saved = client.put("/api/hosts", json=hosts)
    assert saved.status_code == 200 and saved.json()[1]["scaleway"]["zone"] == "fr-par-2"
    no_key = client.post("/api/hosts/scw/poweroff")
    assert no_key.status_code == 400 and "SCW_SECRET_KEY" in no_key.json()["detail"]
    instance = client.get("/api/hosts/scw/instance").json()
    assert instance["configured"] is True and instance["state"] is None
    assert "SCW_SECRET_KEY" in instance["error"] and instance["zone"] == "fr-par-2"
    bad = client.put("/api/hosts", json=[hosts[0], {**hosts[1], "scaleway": {"zone": "paris",
                                                                         "server_id": "x"}}])
    assert bad.status_code == 422
