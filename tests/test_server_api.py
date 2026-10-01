"""HTTP API (§9): every route, response shapes, auth, file access, costs and settings."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from helpers_server import (
    BENCHMARK_KEYS,
    CHECKPOINT_KEYS,
    COST_ITEM_KEYS,
    COSTS_KEYS,
    DATASET_KEYS,
    ENV_KEYS,
    EVAL_KEYS,
    EXPERIMENT_INFO_KEYS,
    HOST_KEYS,
    HPO_INFO_KEYS,
    JOB_KEYS,
    LEADER_ROW_KEYS,
    LEADERBOARD_KEYS,
    OBJECTIVE_KEYS,
    OVERVIEW_KEYS,
    PHASE_PREVIEW_KEYS,
    PROJECT_SUMMARY_KEYS,
    REGISTRY_ENTRY_KEYS,
    RUN_DETAIL_KEYS,
    RUN_SUMMARY_KEYS,
    SAMPLE_KEYS,
    SCW_HOST,
    SETTINGS_KEYS,
    STUDY_DETAIL_KEYS,
    STUDY_SUMMARY_KEYS,
    TRIAL_KEYS,
    FakeRunner,
    populate,
)
from wmlab.server.app import create_app
from wmlab.tracking import read_json

TOKEN = "s3cret-token"


@pytest.fixture
def ws(workspace: Path) -> Path:
    populate(workspace)
    return workspace


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def client(ws: Path, runner: FakeRunner) -> TestClient:
    return TestClient(create_app(ws, None, background=False, runner=runner))


# -- auth -------------------------------------------------------------------------------------
def test_health_and_no_token(client: TestClient, ws: Path):
    body = client.get("/api/health").json()
    assert body["ok"] is True and body["workspace"] == str(ws) and body["version"]
    assert client.get("/api/projects").status_code == 200
    # an app built without token (tests, embedding) is not exposed cross-origin
    cors = client.get("/api/health", headers={"Origin": "http://example.org"})
    assert "access-control-allow-origin" not in cors.headers


def test_token_required(ws: Path):
    client = TestClient(create_app(ws, TOKEN, background=False))
    public = client.get("/api/health")
    assert public.status_code == 200 and public.json() == {"ok": True, "version": public.json()["version"]}
    assert "workspace" not in public.json()  # the workspace path is only shown to the app
    assert client.get("/api/health", headers={"X-WMLab-Token": TOKEN}).json()["workspace"] == str(ws)
    denied = client.get("/api/projects")
    assert denied.status_code == 401 and "jeton" in denied.json()["detail"]
    assert client.get("/api/projects", headers={"X-WMLab-Token": "faux"}).status_code == 401
    # a non-ASCII token is refused (401), not a 500 from ``compare_digest``
    assert client.get("/api/projects",
                      headers={"X-WMLab-Token": "tést".encode("utf-8")}).status_code == 401
    assert client.get("/api/projects?token=%C3%A9").status_code == 401
    assert client.get("/api/projects", headers={"X-WMLab-Token": TOKEN}).status_code == 200
    assert client.get(f"/api/runs/r1/logs?token={TOKEN}").status_code == 200
    preflight = client.options("/api/projects", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "x-wmlab-token"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] in ("*", "http://localhost:5173")
    cors = client.get("/api/health", headers={"Origin": "http://example.org"})
    assert cors.headers.get("access-control-allow-origin") in ("*", "http://example.org")


def test_non_ascii_token_is_compared_as_bytes(ws: Path):
    client = TestClient(create_app(ws, "jeton-é", background=False))
    assert client.get("/api/projects",
                      headers={"X-WMLab-Token": "jeton-é".encode("utf-8")}).status_code == 200
    assert client.get("/api/projects?token=jeton-%C3%A9").status_code == 200
    assert client.get("/api/projects", headers={"X-WMLab-Token": "jeton-e"}).status_code == 401


def test_resolve_token(monkeypatch: pytest.MonkeyPatch):
    from wmlab.server.app import resolve_token

    monkeypatch.delenv("WMLAB_TOKEN", raising=False)
    assert resolve_token("abc") == ("abc", False)
    generated, flag = resolve_token(None)
    assert flag is True and len(generated) >= 32 and generated != resolve_token(None)[0]
    monkeypatch.setenv("WMLAB_TOKEN", "from-env")
    assert resolve_token(None) == ("from-env", False)
    assert resolve_token("cli-wins", "0.0.0.0") == ("cli-wins", False)
    monkeypatch.delenv("WMLAB_TOKEN")
    with pytest.raises(RuntimeError, match="jeton explicite"):
        resolve_token(None, "0.0.0.0")


def test_static_frontend(ws: Path):
    dist = ws / "desktop" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>Watermark Lab</html>", encoding="utf-8")
    (dist / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    client = TestClient(create_app(ws, TOKEN, background=False))
    page = client.get(f"/?token={TOKEN}")
    assert page.status_code == 200 and "Watermark Lab" in page.text
    # A rebuilt index.html must never be masked by the browser cache; hashed assets are immutable.
    assert page.headers["cache-control"] == "no-cache"
    assert client.get("/index.html").headers["cache-control"] == "no-cache"
    assert "immutable" in client.get("/assets/index-abc123.js").headers["cache-control"]
    assert client.get("/api/overview").status_code == 401


def test_gzip_responses(client: TestClient):
    """Large JSON bodies (run details with git_diff) are compressed for clients accepting gzip."""
    plain = client.get("/api/runs", headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200 and "content-encoding" not in plain.headers
    zipped = client.get("/api/runs", headers={"Accept-Encoding": "gzip"})
    assert zipped.status_code == 200 and zipped.headers.get("content-encoding") == "gzip"
    assert zipped.json() == plain.json()
    small = client.get("/api/health", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers  # below the 1 kB threshold


def test_server_log_written(client: TestClient, ws: Path):
    assert (ws / ".wmlab" / "logs" / "server.log").exists()


# -- overview & projects ----------------------------------------------------------------------
def test_overview(client: TestClient):
    body = client.get("/api/overview").json()
    assert set(body) == OVERVIEW_KEYS
    assert body["workspace"]["name"] == "Workspace de test"
    assert set(body["budget"]) == {"total_eur", "spent_eur", "remaining_eur",
                                   "running_eur_per_hour"}
    assert body["budget"]["running_eur_per_hour"] == pytest.approx(2.0)
    assert body["counts"]["projects"] == 1 and body["counts"]["runs"] == 3
    assert body["counts"]["running"] == 1 and body["counts"]["studies"] == 1
    assert [r["run_id"] for r in body["running"]] == ["r3"]
    assert [r["run_id"] for r in body["recent"]] == ["r3", "r2", "r1"]
    assert all(set(r) == RUN_SUMMARY_KEYS for r in body["recent"])
    assert set(body["projects"][0]) == PROJECT_SUMMARY_KEYS
    assert body["jobs"] == []


def test_projects_list_and_best(client: TestClient):
    projects = client.get("/api/projects").json()
    assert len(projects) == 1
    demo = projects[0]
    assert set(demo) == PROJECT_SUMMARY_KEYS
    assert set(demo["objectives"]) == {"quality", "robustness"}
    assert set(demo["objectives"]["quality"]) == OBJECTIVE_KEYS
    assert demo["n_experiments"] == 2 and demo["n_hpo_plans"] == 1
    assert demo["n_runs"] == 3 and demo["n_running"] == 1
    assert demo["default_benchmark"] == "image_v1"
    assert demo["cost_eur"] == pytest.approx(16.5, abs=0.02)
    best = demo["best"]
    assert best == {"run_id": "r2", "checkpoint": "step_000030", "quality": 39.0,
                    "robustness": 0.95, "source": "benchmark"}
    assert demo["last_activity"]


def test_project_best_constraint_and_val_fallback(client: TestClient, ws: Path):
    project = ws / "projects" / "demo" / "project.yaml"
    data = yaml.safe_load(project.read_text(encoding="utf-8"))
    data["objectives"]["quality"]["constraint"] = ">= 40"
    project.write_text(yaml.safe_dump(data), encoding="utf-8")
    best = client.get("/api/projects").json()[0]["best"]
    assert best["run_id"] == "r1"  # r2 violates PSNR >= 40
    data["default_benchmark"] = None
    project.write_text(yaml.safe_dump(data), encoding="utf-8")
    best = client.get("/api/projects").json()[0]["best"]
    assert best["source"] == "val" and best["checkpoint"] is None


def test_project_detail(client: TestClient):
    body = client.get("/api/projects/demo").json()
    assert set(body) == {"summary", "spec", "experiments", "hpo_plans", "notes"}
    assert body["notes"].startswith("# Journal")
    assert body["spec"]["name"] == "demo"
    experiments = {e["name"]: e for e in body["experiments"]}
    assert set(experiments) == {"baseline", "folder_exp"}
    baseline = experiments["baseline"]
    assert set(baseline) == EXPERIMENT_INFO_KEYS
    assert baseline["valid"] is True, baseline["errors"]
    assert baseline["total_steps"] == 30 and baseline["n_phases"] == 2
    assert baseline["model"] == "_test_model" and baseline["n_runs"] == 3
    assert baseline["path"] == "projects/demo/experiments/baseline.yaml"
    plan = body["hpo_plans"][0]
    assert set(plan) == HPO_INFO_KEYS
    assert plan["n_params"] == 4 and plan["n_studies"] == 1 and plan["experiment"] == "baseline"
    assert set(plan["objectives"][0]) == OBJECTIVE_KEYS
    assert client.get("/api/projects/nope").status_code == 404


def test_invalid_experiment_reported(client: TestClient, ws: Path):
    path = ws / "projects" / "demo" / "experiments" / "broken.yaml"
    path.write_text("name: broken\nmodel: {name: inconnu}\nphases: []\n", encoding="utf-8")
    body = client.get("/api/projects/demo").json()
    broken = next(e for e in body["experiments"] if e["name"] == "broken")
    assert broken["valid"] is False and broken["errors"]
    assert set(broken["errors"][0]) == {"loc", "msg"}
    assert broken["model"] == "inconnu" and broken["total_steps"] == 0


def test_create_project_experiment_hpo(client: TestClient, ws: Path):
    created = client.post("/api/projects", json={
        "name": "audio-x", "title": "Audio X", "modality": "audio", "message_bits": 32,
        "description": "Projet audio"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert set(body) == PROJECT_SUMMARY_KEYS
    assert body["modality"] == "audio" and body["message_bits"] == 32
    assert (ws / "projects" / "audio-x" / "project.yaml").is_file()
    assert client.post("/api/projects", json={"name": "audio-x", "modality": "audio"}
                       ).status_code == 409
    assert client.post("/api/projects", json={"name": "x", "modality": "radio"}
                       ).status_code == 400

    exp = client.post("/api/projects/demo/experiments", json={"name": "exp2", "from": "baseline"})
    assert exp.json() == {"path": "projects/demo/experiments/exp2.yaml"}
    assert "name: exp2" in (ws / exp.json()["path"]).read_text(encoding="utf-8")
    assert client.post("/api/projects/demo/experiments", json={"name": "exp2"}).status_code == 409
    hpo = client.post("/api/projects/demo/hpo", json={"name": "lr", "experiment": "baseline"})
    assert hpo.json() == {"path": "projects/demo/hpo/lr.yaml"}
    assert client.post("/api/projects/demo/hpo", json={"name": "z", "experiment": "nope"}
                       ).status_code == 400
    missing = client.post("/api/projects/demo/hpo", json={"name": "z"})
    assert missing.status_code == 422 and isinstance(missing.json()["detail"], str)


# -- files & validation -----------------------------------------------------------------------
def test_files_read_write(client: TestClient, ws: Path):
    got = client.get("/api/files", params={"path": "projects/demo/experiments/baseline.yaml"})
    body = got.json()
    assert set(body) == {"path", "content", "kind", "validation"}
    assert body["kind"] == "experiment" and body["validation"] == {"valid": True, "errors": []}
    assert body["path"] == "projects/demo/experiments/baseline.yaml"

    saved = client.put("/api/files", json={"path": "projects/demo/experiments/baseline.yaml",
                                           "content": body["content"] + "\nbogus: 1\n"})
    assert saved.status_code == 200
    assert saved.json()["saved"] is True and saved.json()["validation"]["valid"] is False

    plugin = client.put("/api/files", json={"path": "lab/models/new_model.py",
                                            "content": "def f(:\n"})
    assert plugin.status_code == 200
    assert plugin.json()["validation"]["valid"] is False
    assert "syntaxe" in plugin.json()["validation"]["errors"][0]["msg"]
    assert (ws / "lab" / "models" / "new_model.py").is_file()
    readme = client.get("/api/files", params={"path": "workspace.yaml"}).json()
    assert readme["kind"] == "workspace"


def test_published_benchmark_is_frozen(client: TestClient, ws: Path):
    """A benchmark used by recorded evaluations cannot be overwritten (409); new files can."""
    content = client.get("/api/files", params={"path": "benchmarks/image_v1.yaml"}).json()["content"]
    refused = client.put("/api/files", json={"path": "benchmarks/image_v1.yaml",
                                             "content": content + "\n# note\n"})
    assert refused.status_code == 409 and "nouvelle version" in refused.json()["detail"]
    assert client.get("/api/files", params={"path": "benchmarks/image_v1.yaml"}).json()[
        "content"] == content
    new = content.replace("name: image_v1", "name: image_v2").replace("version: 1", "version: 2")
    created = client.put("/api/files", json={"path": "benchmarks/image_v2.yaml", "content": new})
    assert created.status_code == 200 and created.json()["saved"]
    # not used by any evaluation yet: still editable
    edited = client.put("/api/files", json={"path": "benchmarks/image_v2.yaml",
                                            "content": new + "\n# note\n"})
    assert edited.status_code == 200


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", ".wmlab/hosts.yaml",
                                  ".git/config", "projects/../../outside.txt",
                                  ".venv/pyvenv.cfg", "desktop/node_modules/x.js",
                                  ".WMLAB/hosts.yaml", ".Wmlab/runs/r1/run.json",
                                  ".GIT/config", ".VENV/pyvenv.cfg"])
def test_files_path_traversal_read(client: TestClient, ws: Path, path: str):
    (ws.parent / "outside.txt").write_text("secret", encoding="utf-8")
    for folder, name in ((".git", "config"), (".venv", "pyvenv.cfg")):
        (ws / folder).mkdir(exist_ok=True)
        (ws / folder / name).write_text("secret", encoding="utf-8")
    response = client.get("/api/files", params={"path": path})
    assert response.status_code == 403, response.text


def test_files_case_insensitive_paths_are_canonical(client: TestClient, ws: Path):
    """On APFS ``PROJECTS/x`` opens ``projects/x``: the returned path is the on-disk spelling."""
    if not (ws / "PROJECTS").exists():
        pytest.skip("système de fichiers sensible à la casse")
    body = client.get("/api/files", params={"path": "PROJECTS/demo/PROJECT.yaml"}).json()
    assert body["path"] == "projects/demo/project.yaml" and body["kind"] == "project"


def test_files_symlink_escape(client: TestClient, ws: Path):
    (ws.parent / "outside.txt").write_text("secret", encoding="utf-8")
    os.symlink(ws.parent / "outside.txt", ws / "projects" / "link.yaml")
    assert client.get("/api/files", params={"path": "projects/link.yaml"}).status_code == 403
    assert client.put("/api/files", json={"path": "projects/link.yaml", "content": "x"}
                      ).status_code == 403
    assert (ws.parent / "outside.txt").read_text(encoding="utf-8") == "secret"


@pytest.mark.parametrize("path", ["workspace.yaml", "wmlab/evil.py", "../evil.yaml",
                                  "projects/../README.md", ".wmlab/runs/r1/run.json"])
def test_files_write_refused(client: TestClient, path: str):
    response = client.put("/api/files", json={"path": path, "content": "x: 1\n"})
    assert response.status_code == 403, response.text


def test_files_limits(client: TestClient, ws: Path):
    (ws / "datasets" / "big.yaml").write_text("x" * (1024 * 1024 + 10), encoding="utf-8")
    assert client.get("/api/files", params={"path": "datasets/big.yaml"}).status_code == 413
    assert client.put("/api/files", json={"path": "datasets/big2.yaml",
                                          "content": "x" * (1024 * 1024 + 10)}).status_code == 413
    (ws / "datasets" / "bin.yaml").write_bytes(b"\xff\xfe\x00\x81")
    assert client.get("/api/files", params={"path": "datasets/bin.yaml"}).status_code == 415
    assert client.get("/api/files", params={"path": "datasets/none.yaml"}).status_code == 404


def test_validate_experiment_and_hpo(client: TestClient, ws: Path):
    content = (ws / "projects/demo/experiments/baseline.yaml").read_text(encoding="utf-8")
    body = client.post("/api/validate", json={
        "content": content, "path": "projects/demo/experiments/baseline.yaml"}).json()
    assert body["valid"] is True, body["errors"]
    assert body["plan"]["total_steps"] == 30
    assert set(body["plan"]["phases"][0]) == PHASE_PREVIEW_KEYS
    assert body["plan"]["phases"][1]["attacks"] == ["_test_noise", "quant"]

    hpo = (ws / "projects/demo/hpo/sweep.yaml").read_text(encoding="utf-8")
    body = client.post("/api/validate", json={"content": hpo, "kind": "hpo",
                                              "path": "projects/demo/hpo/sweep.yaml"}).json()
    assert body["valid"] is True, body["errors"]
    assert body["plan"]["total_steps"] == 15  # steps_scale 0.5 applied to 10 + 20

    bad = client.post("/api/validate", json={"content": "name: [", "kind": "experiment"}).json()
    assert bad["valid"] is False and bad["plan"] is None and "YAML" in bad["errors"][0]["msg"]
    unknown = client.post("/api/validate", json={"content": "a: 1", "kind": "nope"}).json()
    assert unknown["valid"] is False


def test_registry(client: TestClient):
    body = client.get("/api/registry").json()
    assert set(body) == {"entries", "errors"}
    assert set(body["entries"]) == {"model", "attack", "dataset", "preprocess", "loss", "metric"}
    models = {e["name"]: e for e in body["entries"]["model"]}
    assert "_test_model" in models
    assert set(models["_test_model"]) == REGISTRY_ENTRY_KEYS
    assert models["_test_model"]["builtin"] is False
    reloaded = client.get("/api/registry?reload=1").json()
    assert "_test_model" in {e["name"] for e in reloaded["entries"]["model"]}
    assert all(set(e) == {"module", "error"} for e in reloaded["errors"])


def test_registry_reports_plugin_errors(client: TestClient, ws: Path):
    (ws / "lab" / "broken_plugin.py").write_text("import does_not_exist\n", encoding="utf-8")
    body = client.get("/api/registry?reload=1").json()
    assert any(e["module"] == "lab.broken_plugin" for e in body["errors"])


def test_saving_lab_plugin_reloads_registry(client: TestClient):
    source = (
        "from wmlab.registry import ATTACKS\n\n\n"
        "@ATTACKS.register(\"_test_saved_attack\", modality=\"image\")\n"
        "def saved(x, strength: float = 1.0):\n"
        "    \"\"\"Attack saved from the app.\"\"\"\n"
        "    return x\n")
    client.get("/api/registry")
    saved = client.put("/api/files", json={"path": "lab/saved_attack.py", "content": source})
    assert saved.json()["validation"]["valid"] is True
    attacks = {e["name"]: e for e in client.get("/api/registry").json()["entries"]["attack"]}
    assert attacks["_test_saved_attack"]["description"] == "Attack saved from the app."


def test_validate_path_outside_workspace_is_ignored(client: TestClient, ws: Path):
    content = (ws / "projects/demo/experiments/baseline.yaml").read_text(encoding="utf-8")
    body = client.post("/api/validate", json={"content": content, "kind": "experiment",
                                              "path": "../../etc/passwd"}).json()
    assert body["plan"]["total_steps"] == 30


# -- runs -------------------------------------------------------------------------------------
def test_runs_list_filters(client: TestClient):
    runs = client.get("/api/runs").json()
    assert [r["run_id"] for r in runs] == ["r3", "r2", "r1"]
    assert all(set(r) == RUN_SUMMARY_KEYS for r in runs)
    r2 = runs[1]
    assert r2["cost_eur"] == pytest.approx(3.0) and r2["duration_s"] == pytest.approx(7200)
    assert r2["progress"] == {"step": 7200, "total_steps": 7200, "phase": "robust",
                              "phase_index": 1, "n_phases": 2}
    assert r2["summary"]["val/bit_acc"] == pytest.approx(0.95)
    assert r2["params"]["optimizer.lr"] == "1e-3" or r2["params"]["optimizer.lr"] == 0.001
    assert len(client.get("/api/runs?include_trials=true").json()) == 4
    assert [r["run_id"] for r in client.get("/api/runs?study=s1").json()] == ["t1"]
    assert {r["run_id"] for r in client.get("/api/runs?status=completed").json()} == {"r1", "r2"}
    assert [r["run_id"] for r in client.get("/api/runs?host=scw").json()] == ["r3", "r2"]
    assert client.get("/api/runs?project=other").json() == []
    assert len(client.get("/api/runs?limit=1").json()) == 1


def test_lost_status(client: TestClient, ws: Path):
    (ws / ".wmlab" / "runs" / "r3" / "heartbeat").write_text("1000.0", encoding="utf-8")
    runs = {r["run_id"]: r for r in client.get("/api/runs").json()}
    assert runs["r3"]["status"] == "lost"


def test_run_detail(client: TestClient):
    body = client.get("/api/runs/r1").json()
    assert set(body) == RUN_DETAIL_KEYS
    assert set(body["env"]) == ENV_KEYS
    assert body["plan"]["total_steps"] == 30
    assert body["spec"]["name"] == "baseline" and "phases" in body["spec_yaml"]
    assert body["spec_path"] == "projects/demo/experiments/baseline.yaml"
    assert body["overrides"] == {"optimizer.lr": "1e-3"}
    assert body["price_eur_per_hour"] == 0.0
    assert [c["name"] for c in body["checkpoints"]] == ["step_000010", "step_000030"]
    assert all(set(c) == CHECKPOINT_KEYS and c["available_locally"] for c in body["checkpoints"])
    assert set(body["evals"][0]) == EVAL_KEYS
    assert set(body["samples"][0]) == SAMPLE_KEYS
    assert "val/bit_acc_mean" in body["metric_keys"]
    assert {e["type"] for e in body["events"]} >= {"status", "checkpoint", "eval", "note"}
    assert body["notes"].startswith("# baseline")
    r2 = client.get("/api/runs/r2").json()
    assert not any(c["available_locally"] for c in r2["checkpoints"])
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/..%2F..").status_code in (400, 404)


def test_run_metrics_system_logs(client: TestClient):
    metrics = client.get("/api/runs/r1/metrics?keys=val/bit_acc,train/loss&max_points=2").json()
    assert set(metrics) == {"series", "phases"}
    assert set(metrics["series"]) == {"val/bit_acc", "train/loss"}
    assert set(metrics["series"]["train/loss"]) == {"steps", "values"}
    assert len(metrics["series"]["train/loss"]["steps"]) == 2
    assert metrics["phases"] == [{"name": "warmup", "start": 0, "end": 10},
                                 {"name": "robust", "start": 10, "end": 30}]
    all_keys = client.get("/api/runs/r1/metrics").json()["series"]
    assert "val/_test_psnr" in all_keys
    system = client.get("/api/runs/r1/system").json()
    assert set(system) == {"t", "series"} and system["series"]["cpu_percent"] == [12.0]
    assert client.get("/api/runs/r1/logs?tail=1").json() == {"lines": ["ligne 2"]}


def test_run_file(client: TestClient):
    sample = client.get("/api/runs/r1").json()["samples"][0]["path"]
    response = client.get("/api/runs/r1/file", params={"path": sample})
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert client.get("/api/runs/r1/file", params={"path": "../r2/run.json"}).status_code == 403
    assert client.get("/api/runs/r1/file", params={"path": "/etc/passwd"}).status_code == 403
    assert client.get("/api/runs/r1/file", params={"path": "absent.png"}).status_code == 404


def test_run_notes_and_tags(client: TestClient, ws: Path):
    runs = ws / ".wmlab" / "runs"
    assert client.put("/api/runs/r1/notes", json={"notes": "# Conclusion\nOK"}).json() == {
        "ok": True}
    assert (runs / "r1" / "notes.md").read_text(encoding="utf-8") == "# Conclusion\nOK"
    assert client.put("/api/runs/r1/tags", json={"tags": ["best", " v1 ", "best"]}).json() == {
        "ok": True}
    assert read_json(runs / "r1" / "run.json")["tags"] == ["best", "v1"]
    assert client.get("/api/runs/r1").json()["tags"] == ["best", "v1"]
    assert client.put("/api/runs/r3/tags", json={"tags": ["x"]}).status_code == 409
    # a truncated run.json (write in progress / corrupted) is a 409, not a 500
    (runs / "r1" / "run.json").write_text('{"run_id": "r1", "project": "de', encoding="utf-8")
    broken = client.put("/api/runs/r1/tags", json={"tags": ["x"]})
    assert broken.status_code == 409 and "illisible" in broken.json()["detail"]
    assert client.get("/api/runs/r1").status_code == 404


def test_run_stop_without_process(client: TestClient):
    response = client.post("/api/runs/r3/stop")
    assert response.status_code == 409 and "process" in response.json()["detail"]
    assert client.post("/api/runs/r1/stop").status_code == 409
    assert client.post("/api/runs/t1/stop").status_code == 409


def _sleeping(argv_tail: list[str]) -> "subprocess.Popen[bytes]":
    import subprocess
    import sys

    code = "import signal, sys, time\nsignal.signal(signal.SIGTERM, lambda *_: sys.exit(15))\n" \
           "time.sleep(60)"
    return subprocess.Popen([sys.executable, "-c", code, *argv_tail],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def test_run_stop_targets_only_the_run_process(client: TestClient, ws: Path):
    """A running run is stopped through the pid it wrote in run.json (never a look-alike)."""
    import time

    from helpers_server import patch_manifest
    from wmlab.server.jobs import find_run_process, run_process_pid

    pytest.importorskip("psutil")
    run_dir = ws / ".wmlab" / "runs" / "r3"
    patch_manifest(run_dir, host="local")
    # a foreign process whose command line mentions the run id and ".wmlab" (tail, editor…)
    foreign = _sleeping(["tail", "-f", ".wmlab/runs/r3/stdout.log", "r3"])
    trainer = _sleeping(["-m", "wmlab.cli", "train", "spec.yaml", "--run-id", "r3"])
    try:
        time.sleep(0.3)
        assert find_run_process("r3") == trainer.pid
        # substring matches are not enough: the foreign process is never selected
        trainer.terminate()
        trainer.wait(timeout=5)
        assert find_run_process("r3") is None
        # a recorded pid that now belongs to another process is ignored
        patch_manifest(run_dir, pid=foreign.pid, proc_start=1.0)
        assert run_process_pid(read_json(run_dir / "run.json")) is None
        patch_manifest(run_dir, pid=foreign.pid, proc_start=None)
        assert run_process_pid(read_json(run_dir / "run.json")) is None  # not a wmlab process
        response = client.post("/api/runs/r3/stop")
        assert response.status_code == 409
        assert foreign.poll() is None  # untouched
        # the trainer's own pid (with its creation time) is the only target
        trainer = _sleeping(["-m", "wmlab.cli", "train", "spec.yaml", "--run-id", "r3"])
        time.sleep(0.3)
        import psutil

        patch_manifest(run_dir, pid=trainer.pid,
                       proc_start=psutil.Process(trainer.pid).create_time())
        assert client.post("/api/runs/r3/stop").json() == {"ok": True}
        assert trainer.wait(timeout=5) == 15
        assert foreign.poll() is None
    finally:
        for proc in (foreign, trainer):
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_run_stop_refuses_remote_run(client: TestClient, ws: Path):
    from helpers_server import patch_manifest

    patch_manifest(ws / ".wmlab" / "runs" / "r3", pid=os.getpid(), host="scw")
    response = client.post("/api/runs/r3/stop")
    assert response.status_code == 409 and "scw" in response.json()["detail"]


def test_checkpoint_fetch(client: TestClient, runner: FakeRunner, ws: Path):
    assert client.post("/api/runs/r1/checkpoints/last/fetch").json() == {"ok": True}
    assert runner.calls == []
    assert client.post("/api/runs/r2/checkpoints/step_000030/fetch").json() == {"ok": True}
    rsync = runner.rsync_calls()[-1]
    assert rsync[-2] == "root@51.15.0.1:/root/wmlab-data/runs/r2/checkpoints/step_000030.pt"
    assert rsync[-1] == str(ws / ".wmlab" / "runs" / "r2" / "checkpoints" / "step_000030.pt")
    assert client.post("/api/runs/r2/checkpoints/nope/fetch").status_code == 404


def test_compare(client: TestClient):
    body = client.get("/api/compare?runs=r1,r2,absent&keys=val/bit_acc").json()
    assert [r["run_id"] for r in body["runs"]] == ["r1", "r2"]
    assert set(body["series"]) == {"r1", "r2"}
    assert body["series"]["r2"]["val/bit_acc"]["values"] == [0.95, 0.95, 0.95]


# -- studies, leaderboard, benchmarks, datasets -----------------------------------------------
def test_studies(client: TestClient, ws: Path):
    studies = client.get("/api/studies").json()
    assert len(studies) == 1 and set(studies[0]) == STUDY_SUMMARY_KEYS
    s1 = studies[0]
    assert (s1["n_trials"], s1["n_complete"], s1["n_pruned"], s1["n_failed"],
            s1["n_running"]) == (5, 4, 1, 0, 0)
    assert s1["cost_eur"] == pytest.approx(1.0)
    assert s1["budget"] == {"max_trials": 5, "max_hours": None, "max_eur": 20, "parallel": 1}
    assert set(s1["objectives"][0]) == OBJECTIVE_KEYS
    assert client.get("/api/studies?project=other").json() == []

    detail = client.get("/api/studies/s1").json()
    assert set(detail) == STUDY_DETAIL_KEYS
    assert all(set(t) == TRIAL_KEYS for t in detail["trials"])
    assert detail["pareto"] == [3]
    assert isinstance(detail["importances"], dict)
    assert set(detail["importances"]) <= {"optimizer.lr", "model.params.channels"}
    assert detail["spec"]["name"] == "sweep"
    assert detail["notes"] == ""  # no notes.md in this fake study yet
    assert client.get("/api/studies/nope").status_code == 404
    # conclusions of a study live in <study>/notes.md, like a run's
    assert client.put("/api/studies/s1/notes", json={"notes": "# Conclusion\nLR 1e-3"}).json() == {
        "ok": True}
    assert (ws / ".wmlab" / "studies" / "s1" / "notes.md").read_text(encoding="utf-8") == \
        "# Conclusion\nLR 1e-3"
    assert client.get("/api/studies/s1").json()["notes"] == "# Conclusion\nLR 1e-3"
    assert client.put("/api/studies/nope/notes", json={"notes": "x"}).status_code == 404


def test_study_importances_need_three_complete(client: TestClient, ws: Path):
    from helpers_server import make_study

    make_study(ws / ".wmlab" / "studies", "s2", n_complete=2)
    detail = client.get("/api/studies/s2").json()
    assert detail["importances"] is None and detail["pareto"] == [1]


def test_leaderboard(client: TestClient):
    board = client.get("/api/leaderboard").json()
    assert set(board) == LEADERBOARD_KEYS
    assert all(set(r) == LEADER_ROW_KEYS for r in board["rows"])
    sources = [r["source"] for r in board["rows"]]
    assert sources.count("benchmark") == 2 and sources.count("val") == 3
    assert board["benchmarks"] == ["image_v1"]
    assert "_test_psnr" in board["quality_metrics"]
    assert {"bit_acc", "tpr@0.01"} <= set(board["robustness_metrics"])
    assert {"none", "quant", "noise"} <= set(board["attacks"])
    val = next(r for r in board["rows"] if r["source"] == "val" and r["run_id"] == "r1")
    assert val["checkpoint"] is None and val["benchmark"] is None
    assert val["summary"]["bit_acc"] == pytest.approx(0.85)
    assert val["robustness"]["noise"]["bit_acc"] == pytest.approx(0.8)
    only_bench = client.get("/api/leaderboard?benchmark=image_v1").json()
    assert {r["source"] for r in only_bench["rows"]} == {"benchmark"}
    only_val = client.get("/api/leaderboard?source=val").json()
    assert {r["source"] for r in only_val["rows"]} == {"val"}
    assert only_val["benchmarks"] == ["image_v1"]
    assert client.get("/api/leaderboard?project=other").json()["rows"] == []
    assert client.get("/api/leaderboard?source=x").status_code == 400


def test_benchmarks_and_datasets(client: TestClient):
    benchmarks = client.get("/api/benchmarks").json()
    assert len(benchmarks) == 1 and set(benchmarks[0]) == BENCHMARK_KEYS
    bench = benchmarks[0]
    assert bench["valid"] is True, bench["errors"]
    assert bench["n_items"] == 4 and bench["attacks"][1] == {"label": "quant",
                                                             "name": "_test_quant", "params": {}}
    datasets = client.get("/api/datasets").json()
    assert len(datasets) == 1 and set(datasets[0]) == DATASET_KEYS
    assert datasets[0]["used_by"] == ["projects/demo/experiments/folder_exp.yaml"]
    assert datasets[0]["paths"] == {"default": "~/datasets/coco"}


# -- hosts ------------------------------------------------------------------------------------
def test_hosts_crud_and_test(client: TestClient, runner: FakeRunner, ws: Path):
    hosts = client.get("/api/hosts").json()
    assert [h["name"] for h in hosts] == ["local", "scw"]
    assert all(set(h) == HOST_KEYS for h in hosts)
    new = [hosts[0], {**SCW_HOST, "price_eur_per_hour": 2.5}, {"name": "gpu2", "kind": "ssh",
                                                               "ssh_target": "gpu2",
                                                               "price_eur_per_hour": 1.0,
                                                               "max_concurrent_jobs": 1}]
    saved = client.put("/api/hosts", json=new)
    assert saved.status_code == 200
    assert [h["price_eur_per_hour"] for h in saved.json()] == [0.0, 2.5, 1.0]
    assert saved.json()[2]["workdir"] is None
    bad = client.put("/api/hosts", json=[{"name": "x", "kind": "ssh"}])
    assert bad.status_code == 422 and "ssh_target" in bad.json()["detail"]
    # option injection through the target or the remote paths is refused
    for field, value in (("ssh_target", "-oProxyCommand=touch /tmp/pwned"),
                         ("ssh_target", "root@1.2.3.4 evil"), ("workdir", "-rf"),
                         ("data_dir", "/data\nrm -rf /"), ("python", "--version")):
        bad = client.put("/api/hosts", json=[{**SCW_HOST, field: value}])
        assert bad.status_code == 422, (field, value, bad.text)
    assert client.put("/api/hosts", json=[{**SCW_HOST, "ssh_target": "scw-gpu"}]).status_code == 200
    assert client.put("/api/hosts", json=hosts).status_code == 200

    local = client.post("/api/hosts/local/test").json()
    assert set(local) == {"ok", "latency_ms", "output", "error"}
    assert local["ok"] is True and "Python" in local["output"]
    remote = client.post("/api/hosts/scw/test").json()
    assert remote["ok"] is True and "L40S" in remote["output"] and remote["latency_ms"] >= 0
    command = runner.calls[-1]
    assert command[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--",
                           "root@51.15.0.1"]
    assert command[-1] == ("echo ok; nvidia-smi --query-gpu=name,memory.total,utilization.gpu "
                           "--format=csv,noheader || true; df -h /root/wmlab | tail -1")
    runner.fail_ssh = True
    failed = client.post("/api/hosts/scw/test").json()
    assert failed["ok"] is False and "Connexion SSH impossible" in failed["error"]
    assert client.post("/api/hosts/nope/test").status_code == 404


def test_host_setup(client: TestClient, runner: FakeRunner):
    assert client.post("/api/hosts/local/setup").status_code == 400
    job = client.post("/api/hosts/scw/setup").json()
    assert set(job) == JOB_KEYS and job["kind"] == "setup" and job["target"] == "scw"
    from helpers_server import wait_for

    assert wait_for(lambda: client.get("/api/jobs").json()[0]["status"] == "completed")
    commands = runner.ssh_commands()
    assert commands[0] == "mkdir -p /root/wmlab"
    assert commands[-1] == "cd /root/wmlab && bash scripts/remote_bootstrap.sh /root/wmlab-data"
    push = runner.rsync_calls()[0]
    assert push[:3] == ["rsync", "-az", "--delete"]
    assert push[-1] == "root@51.15.0.1:/root/wmlab/"


# -- costs & settings -------------------------------------------------------------------------
def test_costs(client: TestClient):
    body = client.get("/api/costs").json()
    assert set(body) == COSTS_KEYS
    assert body["total_eur"] == 1000.0
    assert body["spent_eur"] == pytest.approx(16.5, abs=0.02)
    assert body["remaining_eur"] == pytest.approx(1000 - body["spent_eur"], abs=0.011)
    assert body["running_eur_per_hour"] == pytest.approx(2.0)
    assert body["budget"] == {"total_eur": 1000.0, "start": "2026-10-01", "end": "2027-09-30"}
    kinds = sorted(i["kind"] for i in body["items"])
    assert kinds == ["eval", "eval", "manual", "run", "run", "run", "study"]
    assert all(COST_ITEM_KEYS <= set(i) for i in body["items"])
    manual = next(i for i in body["items"] if i["kind"] == "manual")
    assert manual["label"] == "Stockage" and manual["eur"] == 10.0
    assert "t1" not in {i["id"] for i in body["items"]}  # trials are paid by the study
    by_host = {h["host"]: h for h in body["by_host"]}
    assert by_host["scw"]["eur"] == pytest.approx(3.0 + 1.0 + 1.5 + 1.0, abs=0.02)
    assert by_host["(manuel)"]["eur"] == 10.0
    assert by_host["local"]["gpu_hours"] == pytest.approx(2.0 + 60 / 3600, abs=1e-3)
    assert [p["project"] for p in body["by_project"]] == ["demo"]
    months = {m["month"]: m for m in body["by_month"]}
    assert months["2026-03"]["eur"] == pytest.approx(4.5)  # r1 + r2 + eval of r2
    assert months["2026-04"]["eur"] == pytest.approx(1.0)  # study
    # r3 is running "now" (started 30 min ago at 2 €/h = 1 €): its month depends on the day
    # the suite runs and may be the month of the manual entry (2026-10)
    r3_month = next(i for i in body["items"] if i["id"] == "r3")["started_at"][:7]
    assert months["2026-10"]["eur"] == pytest.approx(10.0 + (1.0 if r3_month == "2026-10"
                                                             else 0.0))  # manual entry (+ r3)
    assert months[r3_month]["eur"] >= 1.0 - 1e-6
    assert list(months) == sorted(months)


def test_costs_skip_in_run_and_trial_evaluations(ws: Path, runner: FakeRunner):
    """The trainer's final benchmark (written before ended_at) and evaluations of trial runs
    are already paid by the run / the study: only separate evaluations are billed."""
    from helpers_server import HOUR, T0, eval_result
    from wmlab.ids import utc_iso
    from wmlab.tracking import Run

    runs = ws / ".wmlab" / "runs"
    # r2: 2 h at 1.5 €/h on scw + a separate eval of 1 h (populate) = 3 + 1.5 €
    run = Run(runs / "r2", read_json(runs / "r2" / "run.json"))
    run.log_eval(eval_result("r2", "step_000010", psnr=39.0, bit_acc=0.9, host="scw", price=1.5,
                             duration_s=600.0, created_at=utc_iso(T0 + 86400 + 2 * HOUR)))
    run = Run(runs / "t1", read_json(runs / "t1" / "run.json"))
    run.log_eval(eval_result("t1", "step_000030", psnr=39.0, bit_acc=0.9, host="scw", price=1.0,
                             duration_s=900.0, created_at=utc_iso(T0 + 10 * HOUR)))
    client = TestClient(create_app(ws, None, background=False, runner=runner))
    body = client.get("/api/costs").json()
    evals = [i for i in body["items"] if i["kind"] == "eval"]
    assert {i["id"].split("/")[0] for i in evals} == {"r1", "r2"}
    assert len(evals) == 2  # the in-run eval of r2 and the eval of trial t1 are not billed
    assert body["spent_eur"] == pytest.approx(16.5, abs=0.02)
    by_host = {h["host"]: h for h in body["by_host"]}
    # r2 (2 h) + its separate eval (1 h) + r3 running (0.5 h) + study s1 (1 h)
    assert by_host["scw"]["gpu_hours"] == pytest.approx(2.0 + 1.0 + 0.5 + 1.0, abs=0.01)


def test_billed_separately():
    from wmlab.server.costs import billed_separately

    run = {"run_id": "r", "started_at": "2026-03-02T10:00:00Z",
           "ended_at": "2026-03-02T12:00:00Z", "status": "completed"}
    assert billed_separately({"created_at": "2026-03-02T12:00:00Z"}, run) is False
    assert billed_separately({"created_at": "2026-03-02T11:00:00Z"}, run) is False
    assert billed_separately({"created_at": "2026-03-02T12:00:01Z"}, run) is True
    assert billed_separately({"created_at": "2026-03-02T09:00:00Z"}, run) is True
    assert billed_separately({"created_at": "2026-03-02T11:00:00Z"}, None) is True
    assert billed_separately({"created_at": "2026-03-02T11:00:00Z"}, {**run, "study": "s"}) is False
    live = {**run, "ended_at": None, "status": "running"}
    assert billed_separately({"created_at": "2026-03-02T11:00:00Z"}, live) is False
    # results written by the current engine say it explicitly (same-second cases included)
    assert billed_separately({"created_at": "2026-03-02T12:00:00Z", "in_run": False}, run) is True
    assert billed_separately({"created_at": "2026-03-03T12:00:00Z", "in_run": True}, run) is False
    assert billed_separately({"created_at": "2026-03-03T12:00:00Z", "in_run": True}, None) is False


def test_costs_csv(client: TestClient):
    response = client.get("/api/costs.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    text = response.content.decode("utf-8")
    assert text.startswith("﻿")
    lines = text.lstrip("﻿").strip().splitlines()
    assert lines[0] == "Type;Identifiant;Projet;Machine;Début;Heures;Montant (€)"
    assert len(lines) == 8
    manual = next(line for line in lines if line.startswith("Saisie manuelle"))
    assert manual == "Saisie manuelle;Stockage;demo;;2026-10-05;0,000;10,00"
    r2 = next(line for line in lines if line.startswith("Run;r2;"))
    assert r2.endswith(";2,000;3,00")
    assert any(line.startswith("Étude HPO;s1;demo;scw;") for line in lines)


def test_settings_roundtrip(client: TestClient, ws: Path):
    settings = client.get("/api/settings").json()
    assert set(settings) == SETTINGS_KEYS
    assert set(settings["budget"]) == {"total_eur", "start", "end", "manual_entries"}
    assert settings["budget"]["manual_entries"][0]["label"] == "Stockage"
    settings["budget"]["total_eur"] = 42000
    settings["sync_interval_s"] = 60
    saved = client.put("/api/settings", json=settings)
    assert saved.status_code == 200 and saved.json()["budget"]["total_eur"] == 42000
    raw = yaml.safe_load((ws / "workspace.yaml").read_text(encoding="utf-8"))
    assert list(raw) == ["name", "budget", "data_dir", "sync_interval_s", "default_host",
                         "hosted"]
    assert list(raw["budget"]) == ["total_eur", "start", "end", "manual_entries"]
    assert raw["sync_interval_s"] == 60
    assert client.get("/api/costs").json()["total_eur"] == 42000
    bad = client.put("/api/settings", json={**settings, "sync_interval_s": 0})
    assert bad.status_code == 422
    unknown = client.put("/api/settings", json={**settings, "foo": 1})
    assert unknown.status_code == 422


def test_jobs_empty_and_errors(client: TestClient):
    assert client.get("/api/jobs").json() == []
    assert client.post("/api/jobs/nope/cancel").status_code == 404
    assert client.post("/api/jobs", json={"kind": "train", "host": "nope",
                                          "spec_path": "x.yaml"}).status_code == 400
    assert client.post("/api/jobs", json={"kind": "dance"}).status_code == 400
