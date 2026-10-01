"""Hosted mode §12.2: users file, tokens, CLI, middleware identity, roles and attribution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from helpers_server import FakeRunner, JOB_KEYS, populate, wait_for, write_fake_cli
from wmlab.cli import app as cli_app
from wmlab.server.app import create_app
from wmlab.tracking import Run, read_json
from wmlab.users import (
    ADMIN_USER,
    UserError,
    UserStore,
    add_user,
    hash_token,
    load_users,
    resolve_token,
    revoke_user,
    users_path,
)

TOKEN = "server-token"
SPEC = "projects/demo/experiments/baseline.yaml"


@pytest.fixture
def ws(workspace: Path) -> Path:
    populate(workspace)
    return workspace


def users_file(ws: Path) -> Path:
    return users_path(ws / ".wmlab")


# -- module -----------------------------------------------------------------------------------
def test_add_list_revoke_and_resolve(ws: Path):
    path = users_file(ws)
    user, token = add_user("justin", "admin", path)
    assert token.startswith("wml_") and len(token) == 44
    assert user.name == "justin" and user.role == "admin" and user.revoked_at is None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["users"][0]["token_sha256"] == hash_token(token)
    assert token not in path.read_text(encoding="utf-8")  # only the digest is stored
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    member, member_token = add_user("marie", "member", path)
    assert resolve_token(token, path).name == "justin"
    assert resolve_token(member_token, path).role == "member"
    assert resolve_token("wml_" + "0" * 40, path) is None
    assert resolve_token(None, path) is None and resolve_token(b"\xff", path) is None
    with pytest.raises(UserError, match="existe déjà"):
        add_user("justin", "member", path)
    with pytest.raises(UserError, match="rôle invalide"):
        add_user("paul", "owner", path)
    with pytest.raises(UserError, match="invalide"):
        add_user("../x", "member", path)
    with pytest.raises(UserError, match="admin"):
        add_user("admin", "admin", path)
    revoked = revoke_user("marie", path)
    assert revoked.revoked_at and resolve_token(member_token, path) is None
    assert [u.to_api()["revoked_at"] is not None for u in load_users(path)] == [False, True]
    with pytest.raises(UserError, match="inconnu ou déjà révoqué"):
        revoke_user("marie", path)
    # a revoked name can be created again (new token), the old entry disappears
    again, new_token = add_user("marie", "member", path)
    assert resolve_token(new_token, path).name == "marie" and resolve_token(member_token, path) is None
    assert [u.name for u in load_users(path)] == ["justin", "marie"]
    assert load_users(ws / "absent.yaml") == []


def test_user_store_follows_the_file(ws: Path):
    path = users_file(ws)
    store = UserStore(path)
    assert store.resolve("wml_x") is None and store.users() == []
    _, token = add_user("justin", "member", path)
    assert store.resolve(token).identity() == {"name": "justin", "role": "member"}
    revoke_user("justin", path)
    assert store.resolve(token) is None


def test_cli_users(ws: Path, monkeypatch: pytest.MonkeyPatch):
    runner = CliRunner()
    added = runner.invoke(cli_app, ["users", "add", "justin", "--role", "admin"])
    assert added.exit_code == 0, added.output
    token = next(word for word in added.output.split() if word.startswith("wml_"))
    assert resolve_token(token, users_file(ws)).role == "admin"
    assert runner.invoke(cli_app, ["users", "add", "justin"]).exit_code == 1
    listed = runner.invoke(cli_app, ["users", "list", "--json"])
    assert listed.exit_code == 0
    rows = json.loads(listed.output)
    assert rows == [{"name": "justin", "role": "admin", "created_at": rows[0]["created_at"],
                     "revoked_at": None}]
    table = runner.invoke(cli_app, ["users", "list"])
    assert "justin" in table.output and "wml_" not in table.output
    revoked = runner.invoke(cli_app, ["users", "revoke", "justin"])
    assert revoked.exit_code == 0 and "révoqué" in revoked.output
    assert runner.invoke(cli_app, ["users", "revoke", "justin"]).exit_code == 1
    assert resolve_token(token, users_file(ws)) is None


# -- middleware, identity, roles ----------------------------------------------------------------
def make_client(ws: Path, token: str | None = TOKEN, **kwargs) -> TestClient:
    return TestClient(create_app(ws, token, background=False, **kwargs))


def test_me_and_token_kinds(ws: Path):
    _, admin_token = add_user("justin", "admin", users_file(ws))
    _, member_token = add_user("marie", "member", users_file(ws))
    client = make_client(ws)
    assert client.get("/api/me").status_code == 401
    server = client.get("/api/me", headers={"X-WMLab-Token": TOKEN}).json()
    assert server == {"name": "admin", "role": "admin", "hosted": False, "public_url": None}
    justin = client.get("/api/me", headers={"X-WMLab-Token": admin_token}).json()
    assert justin["name"] == "justin" and justin["role"] == "admin"
    marie = client.get("/api/me", params={"token": member_token}).json()
    assert marie["name"] == "marie" and marie["role"] == "member"
    assert client.get("/api/me", headers={"X-WMLab-Token": "wml_" + "f" * 40}).status_code == 401
    revoke_user("marie", users_file(ws))
    assert client.get("/api/me", headers={"X-WMLab-Token": member_token}).status_code == 401
    # health stays public and hides the workspace path without a token
    assert "workspace" not in client.get("/api/health").json()
    assert client.get("/api/health", headers={"X-WMLab-Token": admin_token}).json()["workspace"]
    # hosted settings are reflected by /api/me
    settings = client.get("/api/settings", headers={"X-WMLab-Token": TOKEN}).json()
    settings["hosted"].update(enabled=True, public_url="https://lab.example.com")
    assert client.put("/api/settings", json=settings, headers={"X-WMLab-Token": TOKEN}
                      ).status_code == 200
    me = client.get("/api/me", headers={"X-WMLab-Token": admin_token}).json()
    assert me["hosted"] is True and me["public_url"] == "https://lab.example.com"


def test_no_server_token_means_implicit_admin(ws: Path):
    client = make_client(ws, None)
    assert client.get("/api/me").json()["name"] == ADMIN_USER["name"]
    _, member_token = add_user("marie", "member", users_file(ws))
    assert client.get("/api/me", headers={"X-WMLab-Token": member_token}).json()["name"] == "marie"


def test_member_role_is_refused_on_admin_routes(ws: Path):
    _, member_token = add_user("marie", "member", users_file(ws))
    client = TestClient(create_app(ws, TOKEN, background=False),
                        headers={"X-WMLab-Token": member_token})
    admin = TestClient(create_app(ws, TOKEN, background=False), headers={"X-WMLab-Token": TOKEN})
    hosts = client.get("/api/hosts")
    assert hosts.status_code == 200  # reading is allowed
    for method, url, body in (
        ("put", "/api/hosts", hosts.json()),
        ("put", "/api/settings", client.get("/api/settings").json()),
        ("post", "/api/hosts/scw/decommission", {"confirm": True}),
        ("post", "/api/hosts/scw/poweroff", None),
        ("get", "/api/users", None),
    ):
        response = getattr(client, method)(url, json=body) if body is not None \
            else getattr(client, method)(url)
        assert response.status_code == 403, (url, response.text)
        assert "administrateur" in response.json()["detail"]
    users = admin.get("/api/users")
    assert users.status_code == 200
    assert users.json() == [{"name": "marie", "role": "member",
                             "created_at": users.json()[0]["created_at"], "revoked_at": None}]
    # members can still launch and follow jobs
    assert client.get("/api/jobs").status_code == 200


# -- attribution ---------------------------------------------------------------------------------
def test_job_and_run_attribution(ws: Path, tmp_path: Path):
    _, member_token = add_user("marie", "member", users_file(ws))
    cli = write_fake_cli(tmp_path)
    app = create_app(ws, TOKEN, background=True, cli_prefix=cli, poll_interval=0.05,
                     index_interval=0.2)
    with TestClient(app, headers={"X-WMLab-Token": member_token}) as client:
        job = client.post("/api/jobs", json={"kind": "train", "host": "local",
                                             "spec_path": SPEC, "smoke": True})
        assert job.status_code == 200, job.text
        job = job.json()
        assert set(job) == JOB_KEYS and job["user"] == "marie"
        assert job["git_ref"] is None and job["git_sha"] is None  # no repository here
        assert job["retrieval"] is None and job["result"] is None
        assert wait_for(lambda: next(j for j in client.get("/api/jobs").json()
                                     if j["job_id"] == job["job_id"])["status"] == "completed")
        launch = read_json(ws / ".wmlab" / "jobs" / "launch" / f"{job['job_id']}.json")
        assert launch["user"] == "marie" and launch["git_ref"] is None
        ev = client.post("/api/jobs", json={"kind": "eval", "host": "local", "run_id": "r1",
                                            "checkpoint": "last", "benchmark": "image_v1"}).json()
        assert ev["user"] == "marie"
        setup = client.post("/api/hosts/scw/setup").json()
        assert setup["user"] == "marie"


def test_run_and_study_manifests_carry_the_user(ws: Path, monkeypatch: pytest.MonkeyPatch):
    from wmlab.hpo.driver import prepare_study
    from wmlab.spec import load_experiment

    launch = {"job_id": "job-1", "user": "justin", "git": None}
    launch_file = ws / ".wmlab" / "launch.json"
    launch_file.write_text(json.dumps(launch), encoding="utf-8")
    spec, project, resolved = load_experiment(ws / SPEC, None)
    run = Run.create(project=project.name, experiment=spec.name, modality=project.modality,
                     spec=resolved, run_id="attributed", runs_dir=ws / ".wmlab" / "runs",
                     launch_info=launch, capture_env=False, monitor_system=False)
    run.finish("completed")
    manifest = read_json(ws / ".wmlab" / "runs" / "attributed" / "run.json")
    assert manifest["user"] == "justin"
    anonymous = Run.create(project=project.name, experiment=spec.name, modality=project.modality,
                           spec=resolved, run_id="anonymous", runs_dir=ws / ".wmlab" / "runs",
                           capture_env=False, monitor_system=False)
    anonymous.finish("completed")
    assert read_json(ws / ".wmlab" / "runs" / "anonymous" / "run.json")["user"] is None
    study_dir = prepare_study(ws / "projects" / "demo" / "hpo" / "sweep.yaml",
                              study_id="attributed-study", launch_file=str(launch_file))
    assert read_json(study_dir / "study.json")["user"] == "justin"
    client = make_client(ws, None)
    summary = next(r for r in client.get("/api/runs").json() if r["run_id"] == "attributed")
    assert summary["user"] == "justin"
    assert client.get("/api/runs/anonymous").json()["user"] is None
    study = next(s for s in client.get("/api/studies").json()
                 if s["study_id"] == "attributed-study")
    assert study["user"] == "justin"
    assert client.get("/api/studies/attributed-study").json()["user"] == "justin"
