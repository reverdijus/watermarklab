"""Hosted mode §12.2: accounts, passwords, sessions, CLI, middleware identity, roles,
read-only member policy, redactions, rate limiting and attribution."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from helpers_server import (
    HOST_KEYS,
    JOB_KEYS,
    PASSWORD,
    FakeRunner,
    add_user_session,
    populate,
    wait_for,
    write_fake_cli,
)
from wmlab.cli import app as cli_app
from wmlab.server import auth as auth_module
from wmlab.server.app import create_app
from wmlab.tracking import Run, read_json
from wmlab.users import (
    ADMIN_USER,
    CLI_ACTOR,
    SESSION_TTL_S,
    DuplicateUserError,
    UserError,
    UserStore,
    check_password_policy,
    hash_password,
    load_users,
    users_path,
    verify_password,
)

TOKEN = "server-token"
SPEC = "projects/demo/experiments/baseline.yaml"
H = {"X-WMLab-Token": TOKEN}


@pytest.fixture
def ws(workspace: Path) -> Path:
    populate(workspace)
    return workspace


def store_of(ws: Path) -> UserStore:
    return UserStore(ws / ".wmlab")


def make_client(ws: Path, token: str | None = TOKEN, **kwargs) -> TestClient:
    return TestClient(create_app(ws, token, background=False, **kwargs))


def login(client: TestClient, email: str, password: str = PASSWORD) -> dict:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return res.json()


# -- passwords ----------------------------------------------------------------------------------
def test_password_hashing_and_policy():
    stored = hash_password("correct horse battery")
    assert stored.startswith("scrypt$15$8$1$") and len(stored.split("$")) == 6
    assert verify_password("correct horse battery", stored)
    assert not verify_password("Correct horse battery", stored)
    assert not verify_password("x", None) and not verify_password("x", "md5$abc")
    assert not verify_password("x", "scrypt$25$8$1$aaaa$bbbb")  # unreasonable cost refused
    assert hash_password("same") != hash_password("same")  # fresh salt
    with pytest.raises(UserError, match="trop court"):
        check_password_policy("courte")
    with pytest.raises(UserError, match="trop long"):
        check_password_policy("x" * 257)
    with pytest.raises(UserError, match="égal"):
        check_password_policy("Justin@Example.com", email="justin@example.com")
    check_password_policy("une phrase assez longue")


# -- store --------------------------------------------------------------------------------------
def test_store_lifecycle(ws: Path):
    store = store_of(ws)
    admin, temp = store.add("Justin@Example.com", "justin", "admin")
    assert admin.email == "justin@example.com" and admin.status == "active" and admin.role == "admin"
    assert temp and len(temp) == 16 and admin.must_change_password is True
    assert admin.approved_by == "cli" and admin.approved_at
    raw = yaml.safe_load(users_path(ws / ".wmlab").read_text(encoding="utf-8"))
    assert raw["users"][0]["password_hash"].startswith("scrypt$") and temp not in str(raw)
    assert oct(users_path(ws / ".wmlab").stat().st_mode & 0o777) == "0o600"
    # explicit password: policy applied, nothing to change at first login
    with pytest.raises(UserError, match="trop court"):
        store.add("p@example.com", "paul", "member", password="court")
    paul, none = store.add("p@example.com", "paul", "member", password=PASSWORD)
    assert none is None and paul.must_change_password is False
    # uniqueness (case-insensitive) and reserved / invalid identifiers
    with pytest.raises(DuplicateUserError):
        store.add("JUSTIN@example.com", "autre", "member")
    with pytest.raises(DuplicateUserError):
        store.add("autre@example.com", "Paul", "member")
    with pytest.raises(UserError, match="admin"):
        store.add("a@example.com", "admin", "admin")
    with pytest.raises(UserError, match="invalide"):
        store.add("a@example.com", "../x", "member")
    with pytest.raises(UserError, match="e-mail invalide"):
        store.add("pas-un-email", "x", "member")
    with pytest.raises(UserError, match="Rôle invalide"):
        store.add("a@example.com", "x", "owner")
    # self-registration: pending, member, no access
    marie = store.register("marie@example.com", "marie", PASSWORD)
    assert marie.status == "pending" and marie.role == "member" and marie.approved_at is None
    with pytest.raises(UserError, match="attente") as exc:
        store.authenticate("marie@example.com", PASSWORD)
    assert exc.value.status == 403
    approved = store.approve(marie.id, "member", by="justin")
    assert approved.status == "active" and approved.approved_by == "justin"
    with pytest.raises(UserError, match="pas en attente"):
        store.approve(marie.id, "member", by="justin")
    assert store.authenticate("MARIE@example.com", PASSWORD).id == marie.id
    # authentication failures are generic (unknown e-mail, wrong password, no password)
    for email, password in (("inconnu@example.com", PASSWORD), ("marie@example.com", "faux")):
        with pytest.raises(UserError, match="incorrect") as exc:
            store.authenticate(email, password)
        assert exc.value.status == 401
    # roles: no self-demotion, never the last active admin
    with pytest.raises(UserError, match="propre compte"):
        store.set_role(admin.id, "member", {"id": admin.id, "name": "justin"})
    with pytest.raises(UserError, match="dernier administrateur"):
        store.set_role(admin.id, "member", CLI_ACTOR)
    with pytest.raises(UserError, match="dernier administrateur"):
        store.disable(admin.id, CLI_ACTOR)
    assert store.set_role(marie.id, "admin", CLI_ACTOR).role == "admin"
    assert store.set_role(admin.id, "member", CLI_ACTOR).role == "member"
    # disable revokes sessions; enable restores; pending accounts go through approve
    _, token = store.open_session(paul, "10.0.0.1")
    assert store.resolve_session(token) is not None
    disabled = store.disable(paul.id, CLI_ACTOR)
    assert disabled.status == "disabled" and disabled.disabled_at
    assert store.resolve_session(token) is None
    with pytest.raises(UserError, match="désactivé"):
        store.authenticate("p@example.com", PASSWORD)
    assert store.enable(paul.id, CLI_ACTOR).status == "active"
    pending = store.register("z@example.com", "zoe", PASSWORD)
    with pytest.raises(UserError, match="approbation"):
        store.enable(pending.id, CLI_ACTOR)
    # reset password: temporary password, forced change, sessions gone
    _, token = store.open_session(paul, None)
    paul2, temporary = store.reset_password(paul.id, CLI_ACTOR)
    assert paul2.must_change_password and store.resolve_session(token) is None
    paul = store.authenticate("p@example.com", temporary)  # fresh object (new hash)
    # own password change: current checked, policy, other sessions revoked, kept one stays
    _, keep = store.open_session(paul, None)
    _, other = store.open_session(paul, None)
    keep_id = store.resolve_session(keep)[1].id
    with pytest.raises(UserError, match="actuel incorrect"):
        store.change_password(paul.id, "faux", PASSWORD + "x", keep_session=keep_id)
    with pytest.raises(UserError, match="différent"):
        store.change_password(paul.id, temporary, temporary, keep_session=keep_id)
    changed = store.change_password(paul.id, temporary, PASSWORD, keep_session=keep_id)
    assert changed.must_change_password is False
    assert store.resolve_session(keep) is not None and store.resolve_session(other) is None
    # unknown id
    with pytest.raises(UserError) as exc:
        store.disable("u_inconnu", CLI_ACTOR)
    assert exc.value.status == 404


def test_sessions_expire_and_follow_the_files(ws: Path, monkeypatch: pytest.MonkeyPatch):
    store = store_of(ws)
    user, _ = store.add("j@example.com", "justin", "admin", password=PASSWORD)
    session, token = store.open_session(user, "127.0.0.1")
    assert token.startswith("wmls_") and len(token) == 5 + 48
    raw = json.loads((ws / ".wmlab" / "sessions.json").read_text(encoding="utf-8"))
    assert raw["sessions"][0]["token_sha256"] != token and token not in str(raw)
    assert oct((ws / ".wmlab" / "sessions.json").stat().st_mode & 0o777) == "0o600"
    assert store.by_id(user.id).last_login_at == session.created_at
    # another store instance (another process) sees the session, and a revocation
    other = UserStore(ws / ".wmlab")
    assert other.resolve_session(token)[0].id == user.id
    assert store.resolve_session("wmls_" + "0" * 48) is None
    assert store.resolve_session(b"\xff") is None and store.resolve_session(None) is None
    assert store.resolve_session("wml_" + "0" * 40) is None  # old personal tokens
    assert other.revoke_session(session.id) and store.resolve_session(token) is None
    # expiry
    _, token = store.open_session(user, None)
    future = time.time() + SESSION_TTL_S + 1
    monkeypatch.setattr(time, "time", lambda: future)
    assert store.resolve_session(token) is None
    monkeypatch.undo()
    # legacy entries are ignored and reported
    path = users_path(ws / ".wmlab")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["users"].append({"name": "ancien", "role": "member", "token_sha256": "ab" * 32})
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    users, legacy = load_users(path)
    assert [u.name for u in users] == ["justin"] and legacy[0]["name"] == "ancien"
    assert store.legacy_entries() == [{"name": "ancien", "role": "member"}]


# -- CLI ----------------------------------------------------------------------------------------
def test_cli_users(ws: Path):
    runner = CliRunner()
    added = runner.invoke(cli_app, ["users", "add", "justin@example.com", "--name", "justin",
                                    "--role", "admin"])
    assert added.exit_code == 0, added.output
    assert "Mot de passe temporaire" in added.output
    temp = next(line for line in added.output.splitlines()
                if line.startswith("Mot de passe temporaire :")).split(":", 1)[1].strip()
    store = store_of(ws)
    assert store.authenticate("justin@example.com", temp).must_change_password is True
    assert runner.invoke(cli_app, ["users", "add", "justin@example.com", "--name", "j2"]).exit_code == 1
    with_pw = runner.invoke(cli_app, ["users", "add", "marie@example.com", "--name", "marie",
                                      "--password-stdin"], input=PASSWORD + "\n")
    assert with_pw.exit_code == 0, with_pw.output
    assert "Mot de passe temporaire" not in with_pw.output
    assert store.authenticate("marie@example.com", PASSWORD).must_change_password is False
    listed = runner.invoke(cli_app, ["users", "list", "--json"])
    rows = json.loads(listed.output)
    assert [r["email"] for r in rows] == ["justin@example.com", "marie@example.com"]
    assert set(rows[0]) == {"id", "name", "email", "role", "status", "must_change_password",
                            "created_at", "approved_at", "approved_by", "disabled_at",
                            "last_login_at"}
    table = runner.invoke(cli_app, ["users", "list"])
    assert "marie" in table.output and "scrypt" not in table.output
    # approve a pending registration, change the role, disable / enable, reset
    store.register("paul@example.com", "paul", PASSWORD)
    assert runner.invoke(cli_app, ["users", "approve", "paul@example.com"]).exit_code == 0
    assert store.by_email("paul@example.com").status == "active"
    assert runner.invoke(cli_app, ["users", "role", "paul@example.com", "admin"]).exit_code == 0
    assert store.by_email("paul@example.com").role == "admin"
    assert runner.invoke(cli_app, ["users", "role", "paul@example.com", "chef"]).exit_code == 1
    disabled = runner.invoke(cli_app, ["users", "disable", "paul@example.com"])
    assert disabled.exit_code == 0 and "désactivé" in disabled.output
    assert runner.invoke(cli_app, ["users", "enable", "paul@example.com"]).exit_code == 0
    reset = runner.invoke(cli_app, ["users", "reset-password", "marie@example.com"])
    assert reset.exit_code == 0 and "Mot de passe temporaire" in reset.output
    assert runner.invoke(cli_app, ["users", "disable", "inconnu@example.com"]).exit_code == 1


# -- middleware, identity, auth routes ----------------------------------------------------------
def test_login_logout_me_and_health(ws: Path):
    store = store_of(ws)
    store.add("justin@example.com", "justin", "admin", password=PASSWORD)
    client = make_client(ws)
    # public routes, nothing else
    health = client.get("/api/health").json()
    assert health == {"ok": True, "version": health["version"], "hosted": False,
                      "registration": True}
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/auth/logout").status_code == 401
    # server token = implicit admin
    me = client.get("/api/me", headers=H).json()
    assert me == {"id": "", "name": "admin", "email": "", "role": "admin", "hosted": False,
                  "public_url": None, "must_change_password": False, "auth": "server_token"}
    assert client.post("/api/auth/password", headers=H,
                       json={"current_password": "a", "new_password": PASSWORD}).status_code == 400
    assert client.post("/api/auth/logout", headers=H).json() == {"ok": True}
    # login: generic failure, success with a session token
    bad = client.post("/api/auth/login", json={"email": "justin@example.com", "password": "faux"})
    assert bad.status_code == 401 and bad.json()["detail"] == "E-mail ou mot de passe incorrect."
    unknown = client.post("/api/auth/login", json={"email": "x@example.com", "password": PASSWORD})
    assert unknown.status_code == 401 and unknown.json() == bad.json()
    assert client.post("/api/auth/login", json={"email": "x"}).status_code == 422
    body = login(client, "Justin@Example.com")
    token = body["token"]
    assert token.startswith("wmls_") and body["user"]["name"] == "justin"
    assert body["user"]["auth"] == "session" and body["user"]["id"]
    headers = {"X-WMLab-Token": token}
    assert client.get("/api/me", headers=headers).json()["email"] == "justin@example.com"
    assert client.get("/api/health", headers=headers).json()["workspace"] == str(ws)
    assert "workspace" not in client.get("/api/health").json()
    # ?token= works only on the download routes
    assert client.get("/api/runs/r1/file", params={"path": "run.json", "token": token}).status_code == 200
    assert client.get("/api/costs.csv", params={"token": token}).status_code == 200
    assert client.get("/api/me", params={"token": token}).status_code == 401
    # own password change keeps the current session and revokes the others
    other = login(client, "justin@example.com")["token"]
    changed = client.post("/api/auth/password", headers=headers,
                          json={"current_password": PASSWORD, "new_password": PASSWORD + "-2"})
    assert changed.status_code == 200, changed.text
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.get("/api/me", headers={"X-WMLab-Token": other}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "justin@example.com",
                                                "password": PASSWORD}).status_code == 401
    # logout revokes the session
    assert client.post("/api/auth/logout", headers=headers).json() == {"ok": True}
    assert client.get("/api/me", headers=headers).status_code == 401
    # hosted settings are reflected by /api/me and /api/health
    settings = client.get("/api/settings", headers=H).json()
    settings["hosted"].update(enabled=True, public_url="https://lab.example.com",
                              allow_registration=False)
    assert client.put("/api/settings", json=settings, headers=H).status_code == 200
    me = client.get("/api/me", headers=H).json()
    assert me["hosted"] is True and me["public_url"] == "https://lab.example.com"
    assert client.get("/api/health").json() == {"ok": True, "version": health["version"],
                                                "hosted": True, "registration": False}


def test_registration_and_approval(ws: Path):
    store = store_of(ws)
    store.add("justin@example.com", "justin", "admin", password=PASSWORD)
    client = make_client(ws)
    res = client.post("/api/auth/register", json={"email": "marie@example.com", "name": "marie",
                                                  "password": PASSWORD})
    assert res.status_code == 202 and res.json()["status"] == "pending"
    assert "approuver" in res.json()["message"]
    # same answer for a duplicate (no enumeration), validation errors are explicit
    dup = client.post("/api/auth/register", json={"email": "MARIE@example.com", "name": "autre",
                                                  "password": PASSWORD})
    assert dup.status_code == 202 and dup.json() == res.json()
    assert [u.name for u in store.users()] == ["justin", "marie"]
    short = client.post("/api/auth/register", json={"email": "p@example.com", "name": "paul",
                                                    "password": "court"})
    assert short.status_code == 400 and "12 caractères" in short.json()["detail"]
    assert client.post("/api/auth/register", json={"email": "p@example.com", "name": "admin",
                                                   "password": PASSWORD}).status_code == 400
    assert client.post("/api/auth/register", json={"email": "p@example.com", "name": "paul",
                                                   "password": PASSWORD, "role": "admin"}
                       ).status_code == 422  # extra fields are refused
    # a pending account cannot log in; an admin approves it from the API
    pending = client.post("/api/auth/login", json={"email": "marie@example.com",
                                                   "password": PASSWORD})
    assert pending.status_code == 403 and "attente" in pending.json()["detail"]
    admin_token = login(client, "justin@example.com")["token"]
    admin = {"X-WMLab-Token": admin_token}
    users = client.get("/api/users", headers=admin).json()
    assert [u["email"] for u in users] == ["marie@example.com", "justin@example.com"]  # pending first
    marie = next(u for u in users if u["name"] == "marie")
    assert marie["status"] == "pending" and marie["role"] == "member"
    approved = client.post(f"/api/users/{marie['id']}/approve", json={"role": "member"},
                           headers=admin)
    assert approved.status_code == 200 and approved.json()["status"] == "active"
    assert approved.json()["approved_by"] == "justin"
    assert client.post(f"/api/users/{marie['id']}/approve", json={"role": "member"},
                       headers=admin).status_code == 409
    member_token = login(client, "marie@example.com")["token"]
    assert client.get("/api/me", headers={"X-WMLab-Token": member_token}).json()["role"] == "member"
    # admin account management: create (temporary password), role, disable, enable, reset
    created = client.post("/api/users", json={"email": "paul@example.com", "name": "paul",
                                              "role": "member"}, headers=admin)
    assert created.status_code == 200, created.text
    temp = created.json()["temporary_password"]
    paul = created.json()["user"]
    assert paul["status"] == "active" and paul["must_change_password"] is True
    assert client.post("/api/users", json={"email": "paul@example.com", "name": "p2",
                                           "role": "member"}, headers=admin).status_code == 409
    first = login(client, "paul@example.com", temp)
    assert first["user"]["must_change_password"] is True
    paul_headers = {"X-WMLab-Token": first["token"]}
    assert client.get("/api/me", headers=paul_headers).json()["must_change_password"] is True
    assert client.post("/api/auth/password", headers=paul_headers,
                       json={"current_password": temp, "new_password": PASSWORD}).status_code == 200
    assert client.get("/api/me", headers=paul_headers).json()["must_change_password"] is False
    assert client.post(f"/api/users/{paul['id']}/role", json={"role": "admin"},
                       headers=admin).json()["role"] == "admin"
    me_id = client.get("/api/me", headers=admin).json()["id"]
    assert client.post(f"/api/users/{me_id}/role", json={"role": "member"},
                       headers=admin).status_code == 400
    assert client.post(f"/api/users/{me_id}/disable", headers=admin).status_code == 400
    disabled = client.post(f"/api/users/{paul['id']}/disable", headers=admin)
    assert disabled.status_code == 200 and disabled.json()["status"] == "disabled"
    assert client.get("/api/me", headers=paul_headers).status_code == 401  # sessions revoked
    refused = client.post("/api/auth/login", json={"email": "paul@example.com", "password": PASSWORD})
    assert refused.status_code == 403 and "désactivé" in refused.json()["detail"]
    assert client.post(f"/api/users/{paul['id']}/enable", headers=admin).json()["status"] == "active"
    reset = client.post(f"/api/users/{paul['id']}/reset-password", headers=admin).json()
    assert reset["user"]["must_change_password"] and login(client, "paul@example.com",
                                                             reset["temporary_password"])
    # the last active admin cannot be demoted or disabled (paul is an admin again: demote him)
    client.post(f"/api/users/{paul['id']}/role", json={"role": "member"}, headers=admin)
    assert client.post(f"/api/users/{me_id}/role", json={"role": "member"},
                       headers=H).status_code == 409
    assert client.post(f"/api/users/{me_id}/disable", headers=H).status_code == 409
    assert client.post("/api/users/u_inconnu/disable", headers=admin).status_code == 404
    # registration can be closed
    settings = client.get("/api/settings", headers=admin).json()
    settings["hosted"]["allow_registration"] = False
    assert client.put("/api/settings", json=settings, headers=admin).status_code == 200
    closed = client.post("/api/auth/register", json={"email": "z@example.com", "name": "zoe",
                                                     "password": PASSWORD})
    assert closed.status_code == 403 and "fermées" in closed.json()["detail"]


def test_rate_limiting(ws: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth_module, "LOGIN_FAILURES_PER_EMAIL", (3, 60))
    monkeypatch.setattr(auth_module, "LOGIN_FAILURES_PER_IP", (6, 60))
    monkeypatch.setattr(auth_module, "REGISTER_PER_IP", (2, 60))
    store = store_of(ws)
    store.add("justin@example.com", "justin", "admin", password=PASSWORD)
    client = make_client(ws)
    for _ in range(3):
        assert client.post("/api/auth/login", json={"email": "justin@example.com",
                                                    "password": "faux"}).status_code == 401
    throttled = client.post("/api/auth/login", json={"email": "justin@example.com",
                                                     "password": PASSWORD})
    assert throttled.status_code == 429 and throttled.headers["Retry-After"].isdigit()
    assert "réessayez" in throttled.json()["detail"]
    # per-IP window counts every attempt (the throttled one included): other e-mails too
    for i in range(2):
        assert client.post("/api/auth/login", json={"email": f"x{i}@example.com",
                                                    "password": "faux"}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "y@example.com",
                                                "password": "faux"}).status_code == 429
    # registration quota
    for i in range(2):
        assert client.post("/api/auth/register", json={"email": f"r{i}@example.com",
                                                       "name": f"r{i}", "password": PASSWORD}
                           ).status_code == 202
    assert client.post("/api/auth/register", json={"email": "r9@example.com", "name": "r9",
                                                   "password": PASSWORD}).status_code == 429
    limiter = auth_module.RateLimiter(2, 60)
    limiter.hit("k"), limiter.hit("k")
    assert limiter.retry_after("k") is not None and limiter.retry_after("other") is None
    limiter.reset("k")
    assert limiter.retry_after("k") is None


def test_no_server_token_means_implicit_admin(ws: Path):
    client = make_client(ws, None)
    assert client.get("/api/me").json()["name"] == ADMIN_USER["name"]
    member_token = add_user_session(ws, "marie", "member")
    assert client.get("/api/me", headers={"X-WMLab-Token": member_token}).json()["name"] == "marie"


# -- read-only member policy ------------------------------------------------------------------
def test_member_is_read_only_and_sees_no_code(ws: Path):
    member_token = add_user_session(ws, "marie", "member")
    client = TestClient(create_app(ws, TOKEN, background=False),
                        headers={"X-WMLab-Token": member_token})
    admin = TestClient(create_app(ws, TOKEN, background=False), headers=H)
    # every write is refused, whatever the route
    for method, url, body in (
        ("post", "/api/jobs", {"kind": "train", "host": "local", "spec_path": SPEC}),
        ("post", "/api/jobs/estimate", {"spec_path": SPEC}),
        ("post", "/api/projects", {"name": "x", "modality": "image"}),
        ("post", "/api/projects/demo/experiments", {"name": "x"}),
        ("put", "/api/files", {"path": SPEC, "content": "x"}),
        ("post", "/api/validate", {"content": "x"}),
        ("put", "/api/runs/r1/notes", {"notes": "x"}),
        ("put", "/api/runs/r1/tags", {"tags": ["x"]}),
        ("post", "/api/runs/r1/stop", {}),
        ("post", "/api/runs/r1/checkpoints/step_000030/fetch", {}),
        ("put", "/api/studies/s1/notes", {"notes": "x"}),
        ("put", "/api/hosts", []),
        ("post", "/api/hosts/scw/test", {}),
        ("post", "/api/hosts/scw/setup", {}),
        ("post", "/api/hosts/scw/retrieve", {}),
        ("post", "/api/hosts/scw/decommission", {"confirm": True}),
        ("post", "/api/hosts/scw/poweroff", {}),
        ("post", "/api/jobs/j1/cancel", {}),
        ("post", "/api/jobs/j1/sync", {}),
        ("put", "/api/settings", {}),
        ("post", "/api/git/pull", {}),
        ("post", "/api/users", {"email": "a@example.com", "name": "a", "role": "admin"}),
        ("post", "/api/users/u_x/approve", {"role": "admin"}),
        ("post", "/api/users/u_x/disable", {}),
    ):
        response = getattr(client, method)(url, json=body)
        assert response.status_code == 403, (url, response.text)
        assert "administrateur" in response.json()["detail"]
    # the GET routes describing the code or the infrastructure are refused too
    for url in ("/api/files?path=" + SPEC, "/api/registry", "/api/runs/r1/logs", "/api/users",
                "/api/git", "/api/settings", "/api/hosts/scw/inventory",
                "/api/hosts/scw/instance"):
        assert client.get(url).status_code == 403, url
    # what a member can do: follow the results (run files: logged samples only)
    sample = admin.get("/api/runs/r1").json()["samples"][0]["path"]
    for url in ("/api/overview", "/api/projects", "/api/projects/demo", "/api/runs",
                "/api/runs/r1", "/api/runs/r1/metrics", "/api/runs/r1/system",
                f"/api/runs/r1/file?path={sample}", "/api/compare?runs=r1,r2", "/api/studies",
                "/api/studies/s1", "/api/leaderboard", "/api/benchmarks", "/api/datasets",
                "/api/hosts", "/api/jobs", "/api/costs", "/api/costs.csv", "/api/me"):
        assert client.get(url).status_code == 200, url
    # redactions
    detail = client.get("/api/runs/r1").json()
    full = admin.get("/api/runs/r1").json()
    assert set(detail) == set(full)
    assert detail["spec"] == {} and detail["spec_yaml"] == "" and detail["spec_path"] is None
    assert detail["overrides"] == {} and full["overrides"] and full["spec"]
    assert detail["env"] == {"git": None, "git_diff": None, "hardware": full["env"]["hardware"],
                             "pip_freeze_lines": 0, "command": None, "launch": None}
    assert detail["params"] == {} and full["params"] and detail["plan"] is None and full["plan"]
    assert detail["checkpoints"] == full["checkpoints"] and detail["summary"] == full["summary"]
    listed = client.get("/api/runs").json()
    assert listed and all(r["params"] == {} for r in listed)
    assert any(r["params"] for r in admin.get("/api/runs").json())
    overview = client.get("/api/overview").json()
    assert all(r["params"] == {} for r in overview["recent"] + overview["running"])
    compared = client.get("/api/compare?runs=r1,r2").json()
    assert all(r["params"] == {} for r in compared["runs"]) and compared["series"]["r1"]
    study = client.get("/api/studies/s1").json()
    assert study["spec"] == {} and admin.get("/api/studies/s1").json()["spec"]
    assert study["trials"] == admin.get("/api/studies/s1").json()["trials"]
    hosts = client.get("/api/hosts").json()
    assert hosts and all(set(h) == HOST_KEYS for h in hosts)
    scw = next(h for h in hosts if h["name"] == "scw")
    assert scw["ssh_target"] is None and scw["workdir"] is None and scw["scaleway"] is None
    assert next(h for h in admin.get("/api/hosts").json() if h["name"] == "scw")["ssh_target"]
    datasets = client.get("/api/datasets").json()
    assert datasets and all(d["paths"] == {} for d in datasets)
    assert any(d["paths"] for d in admin.get("/api/datasets").json())
    # a failure traceback (code lines, server paths) is reduced to its last line
    from helpers_server import patch_manifest

    trace = "Traceback (most recent call last):\n  File \"/opt/wmlab/lab/models/x.py\", line 3\n" \
            "    secret = 1\nValueError: nbits invalide"
    patch_manifest(ws / ".wmlab" / "runs" / "r2", status="failed", error=trace)
    app_state = client.app.state.wmlab
    app_state.indexer.scan()
    r2 = client.get("/api/runs/r2").json()
    assert r2["error"] == "ValueError: nbits invalide"
    assert "/opt/wmlab" not in json.dumps(r2)
    listed = next(r for r in client.get("/api/runs").json() if r["run_id"] == "r2")
    assert listed["error"] == "ValueError: nbits invalide"
    assert "/opt/wmlab" in json.dumps(admin.get("/api/runs/r2").json())
    # the project detail keeps the experiment metadata (no YAML)
    project = client.get("/api/projects/demo").json()
    assert project["experiments"] and "path" in project["experiments"][0]
    # a member may end its own session
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_member_jobs_and_overview_are_redacted(ws: Path, tmp_path: Path):
    member_token = add_user_session(ws, "marie", "member")
    cli = write_fake_cli(tmp_path)
    app = create_app(ws, TOKEN, background=True, cli_prefix=cli, poll_interval=0.05,
                     index_interval=0.2)
    with TestClient(app) as client:
        job = client.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC,
                                             "smoke": True, "overrides": {"optimizer.lr": "0.1"}},
                          headers=H)
        assert job.status_code == 200, job.text
        assert job.json()["overrides"] == {"optimizer.lr": "0.1"} and job.json()["user"] == "admin"
        member = {"X-WMLab-Token": member_token}
        jobs = client.get("/api/jobs", headers=member).json()
        assert jobs and all(j["overrides"] == {} for j in jobs) and set(jobs[0]) == JOB_KEYS
        overview = client.get("/api/overview", headers=member).json()
        assert all(j["overrides"] == {} for j in overview["jobs"])
        assert client.get("/api/jobs", headers=H).json()[0]["overrides"]
        wait_for(lambda: all(j["status"] == "completed"
                             for j in client.get("/api/jobs", headers=H).json()))


# -- attribution ---------------------------------------------------------------------------------
def test_job_and_run_attribution(ws: Path, tmp_path: Path):
    marie_token = add_user_session(ws, "marie", "admin")
    cli = write_fake_cli(tmp_path)
    app = create_app(ws, TOKEN, background=True, cli_prefix=cli, poll_interval=0.05,
                     index_interval=0.2)
    with TestClient(app, headers={"X-WMLab-Token": marie_token}) as client:
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


def test_audit_log_lines(ws: Path):
    store = store_of(ws)
    store.add("justin@example.com", "justin", "admin", password=PASSWORD)
    client = make_client(ws)
    client.post("/api/auth/login", json={"email": "justin@example.com", "password": "faux"})
    token = login(client, "justin@example.com")["token"]
    member_token = add_user_session(ws, "marie", "member")
    client.get("/api/files", params={"path": SPEC}, headers={"X-WMLab-Token": member_token})
    client.post("/api/auth/logout", headers={"X-WMLab-Token": token})
    log_text = (ws / ".wmlab" / "logs" / "server.log").read_text(encoding="utf-8")
    assert "audit: login-failed email=justin@example.com" in log_text
    assert "audit: login user=justin" in log_text
    assert "audit: forbidden user=marie method=GET path=/api/files" in log_text
    assert "audit: logout user=justin" in log_text
    assert PASSWORD not in log_text and token not in log_text and "faux" not in log_text


# -- downloads, bodies and error messages ------------------------------------------------------
def test_run_files_are_never_pages_and_members_only_see_samples(ws: Path):
    member_token = add_user_session(ws, "marie", "member")
    admin = TestClient(create_app(ws, TOKEN, background=False), headers=H)
    member = TestClient(create_app(ws, TOKEN, background=False),
                        headers={"X-WMLab-Token": member_token})
    run_dir = ws / ".wmlab" / "runs" / "r1"
    (run_dir / "evil.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (run_dir / "samples" / "evil.svg").write_text("<svg onload='alert(1)'/>", encoding="utf-8")
    sample = admin.get("/api/runs/r1").json()["samples"][0]["path"]
    assert sample.startswith("samples/")
    # inline media keep their type; HTML / SVG / anything else become opaque downloads
    png = admin.get("/api/runs/r1/file", params={"path": sample})
    assert png.status_code == 200 and png.headers["content-type"].startswith("image/png")
    assert png.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in png.headers["content-security-policy"]
    for path in ("evil.html", "samples/evil.svg", "checkpoints/index.json"):
        res = admin.get("/api/runs/r1/file", params={"path": path})
        assert res.status_code == 200, path
        if path.endswith(".json"):
            assert res.headers["content-type"].startswith("application/json")
        else:
            assert res.headers["content-type"] == "application/octet-stream"
            assert res.headers["content-disposition"].startswith("attachment")
    # a member only reads the logged samples: no weights, spec or log
    assert member.get("/api/runs/r1/file", params={"path": sample}).status_code == 200
    for path in ("run.json", "spec.yaml", "stdout.log", "checkpoints/index.json", "evil.html",
                 "../r2/samples/x.png"):
        res = member.get("/api/runs/r1/file", params={"path": path})
        assert res.status_code == 403, (path, res.text)
    assert admin.get("/api/runs/r1/file", params={"path": "../r2/run.json"}).status_code == 403


def test_body_limit_and_hosted_error_messages(ws: Path):
    client = make_client(ws)
    big = "x" * (auth_module.MAX_BODY_BYTES + 1)
    res = client.put("/api/runs/r1/notes", json={"notes": big}, headers=H)
    assert res.status_code == 413 and "volumineux" in res.json()["detail"]
    assert client.put("/api/runs/r1/notes", json={"notes": "ok"}, headers=H).status_code == 200
    # ASGI level: a chunked body without Content-Length is refused before the route runs
    import asyncio

    calls: list[str] = []

    async def inner(scope, receive, send):
        calls.append(scope["path"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    middleware = auth_module.BodyLimitMiddleware(inner, limit=10)
    scope = {"type": "http", "method": "POST", "path": "/api/validate", "query_string": b"",
             "headers": [(b"transfer-encoding", b"chunked")]}
    asyncio.run(middleware(scope, receive, send))
    assert not calls and sent[0]["status"] == 411
    scope["headers"] = [(b"content-length", b"11")]
    asyncio.run(middleware(scope, receive, send))
    assert not calls and sent[-2]["status"] == 413
    scope["headers"] = [(b"content-length", b"10")]
    asyncio.run(middleware(scope, receive, send))
    assert calls == ["/api/validate"]
    scope.update(method="GET", headers=[(b"content-length", b"99")])
    asyncio.run(middleware(scope, receive, send))
    assert len(calls) == 2  # GET bodies are not limited here (no body expected)
    # hosted mode: generic 404 / 500 messages (details stay in server.log)
    settings = client.get("/api/settings", headers=H).json()
    settings["hosted"]["enabled"] = True
    assert client.put("/api/settings", json=settings, headers=H).status_code == 200
    missing = client.get("/api/runs/r1/metrics", params={"keys": "x"}, headers=H)
    assert missing.status_code == 200
    detail = client.get("/api/files", params={"path": "projects/demo/absent.yaml"}, headers=H)
    assert detail.status_code == 404 and str(ws) not in detail.json()["detail"]


# -- review fixes --------------------------------------------------------------------------------
def test_public_routes_stay_open_to_logged_in_members(ws: Path):
    member_token = add_user_session(ws, "marie", "member")
    client = TestClient(create_app(ws, TOKEN, background=False),
                        headers={"X-WMLab-Token": member_token})
    assert client.post("/api/auth/login", json={"email": "marie@example.com",
                                                "password": PASSWORD}).status_code == 200
    assert client.post("/api/auth/register", json={"email": "z@example.com", "name": "zoe",
                                                   "password": PASSWORD}).status_code == 202
    assert client.get("/api/health").json()["ok"] is True


def test_invalid_inputs_are_refused_not_crashing(ws: Path):
    client = make_client(ws)

    def post_raw(path: str, body: dict) -> object:  # lone surrogates travel as JSON escapes
        return client.post(path, content=json.dumps(body).encode("ascii"),
                           headers={"Content-Type": "application/json"})

    for email in ("a\ud800@example.com", "a@b.c\x00", "ligne\n@example.com", "x" * 300 + "@a.b"):
        res = post_raw("/api/auth/register", {"email": email, "name": "paul",
                                              "password": PASSWORD})
        assert res.status_code in (400, 422), (email, res.text)
    res = post_raw("/api/auth/register", {"email": "p@example.com", "name": "paul",
                                          "password": "mot de passe \ud800 invalide"})
    assert res.status_code in (400, 422), res.text  # refused by the model or the policy
    res = client.post("/api/auth/login", json={"email": "pas un e-mail", "password": PASSWORD})
    assert res.status_code == 401 and res.json()["detail"] == "E-mail ou mot de passe incorrect."
    log_text = (ws / ".wmlab" / "logs" / "server.log").read_text(encoding="utf-8")
    assert "pas un e-mail" not in log_text and "email=<invalide>" in log_text
    # over-long bodies are refused by validation (422), never counted as attempts
    assert client.post("/api/auth/login", json={"email": "a@b.co", "password": "x" * 300}
                       ).status_code == 422
    assert client.get("/api/users", headers=H).status_code == 200  # the file stayed clean


def test_unreadable_users_file_is_never_overwritten(ws: Path):
    store = store_of(ws)
    store.add("justin@example.com", "justin", "admin", password=PASSWORD)
    path = users_path(ws / ".wmlab")
    good = path.read_text(encoding="utf-8")
    path.write_text("users: [\n  - {name: cassé\n", encoding="utf-8")  # broken YAML
    client = make_client(ws)
    res = client.post("/api/auth/register", json={"email": "z@example.com", "name": "zoe",
                                                  "password": PASSWORD})
    assert res.status_code == 503 and "illisible" in res.json()["detail"]
    assert client.post("/api/auth/login", json={"email": "justin@example.com",
                                                "password": PASSWORD}).status_code == 401
    assert client.get("/api/users", headers=H).json() == []  # server token still works
    assert path.read_text(encoding="utf-8") == "users: [\n  - {name: cassé\n"  # untouched
    runner = CliRunner()
    listed = runner.invoke(cli_app, ["users", "list"])
    assert listed.exit_code == 1 and "illisible" in listed.output
    path.write_text(good, encoding="utf-8")  # repaired: everything works again
    assert client.post("/api/auth/login", json={"email": "justin@example.com",
                                                "password": PASSWORD}).status_code == 200
    # a legacy entry survives a rewrite of the file
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["users"].append({"name": "ancien", "role": "member", "token_sha256": "ab" * 32})
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    store.add("paul@example.com", "paul", "member", password=PASSWORD)
    rows = yaml.safe_load(path.read_text(encoding="utf-8"))["users"]
    assert [r["name"] for r in rows] == ["justin", "paul", "ancien"]
    assert store.legacy_entries() == [{"name": "ancien", "role": "member"}]


def test_session_opening_rechecks_the_account(ws: Path):
    store = store_of(ws)
    user, _ = store.add("justin@example.com", "justin", "admin", password=PASSWORD)
    other, _ = store.add("p@example.com", "paul", "member", password=PASSWORD)
    authenticated = store.authenticate("p@example.com", PASSWORD)
    store.reset_password(other.id, CLI_ACTOR)  # concurrent admin action
    with pytest.raises(UserError) as exc:
        store.open_session(authenticated, "127.0.0.1")
    assert exc.value.status == 401
    authenticated = store.authenticate("justin@example.com", PASSWORD)
    # a password reset between the check and the change: the old password is refused
    _, keep = store.open_session(authenticated, None)
    keep_id = store.resolve_session(keep)[1].id
    before = store.by_id(user.id).password_hash
    store.reset_password(user.id, CLI_ACTOR)
    with pytest.raises(UserError) as exc2:
        # the caller still holds the old password: the current check itself fails first
        store.change_password(user.id, PASSWORD, PASSWORD + "-x", keep_session=keep_id)
    assert exc2.value.status == 401 and store.by_id(user.id).password_hash != before


def test_job_errors_and_events_are_shortened_for_members(ws: Path, tmp_path: Path):
    member_token = add_user_session(ws, "marie", "member")
    cli = write_fake_cli(tmp_path)
    app = create_app(ws, TOKEN, background=True, cli_prefix=cli, poll_interval=0.05,
                     index_interval=0.2)
    trace = "Traceback (most recent call last):\n  File \"/opt/wmlab/lab/models/x.py\"\n" \
            "RuntimeError: boom"
    with TestClient(app) as client:
        job = client.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC,
                                             "smoke": True}, headers=H).json()
        wait_for(lambda: next(j for j in client.get("/api/jobs", headers=H).json()
                              if j["job_id"] == job["job_id"])["status"] == "completed")
    job_file = ws / ".wmlab" / "jobs" / f"{job['job_id']}.json"
    data = read_json(job_file)
    data["status"], data["error"] = "failed", trace
    from wmlab.tracking import atomic_write_json
    atomic_write_json(job_file, data)
    run_dir = ws / ".wmlab" / "runs" / "r1"
    events = (run_dir / "events.jsonl")
    events.write_text(events.read_text(encoding="utf-8") + json.dumps(
        {"t": 1.0, "type": "error", "message": trace, "traceback": trace}) + "\n",
        encoding="utf-8")
    client = make_client(ws)
    member = {"X-WMLab-Token": member_token}
    jobs = client.get("/api/jobs", headers=member).json()
    assert any(j["job_id"] == job["job_id"] for j in jobs)
    assert all("/opt/wmlab" not in json.dumps(j) for j in jobs)
    assert next(j for j in jobs if j["job_id"] == job["job_id"])["error"] == "RuntimeError: boom"
    assert "/opt/wmlab" not in json.dumps(client.get("/api/overview", headers=member).json())
    detail = client.get("/api/runs/r1", headers=member).json()
    assert "/opt/wmlab" not in json.dumps(detail)
    error_event = next(e for e in detail["events"] if e["type"] == "error")
    assert error_event["message"] == "RuntimeError: boom" and "traceback" not in error_event
    assert "/opt/wmlab" in json.dumps(client.get("/api/runs/r1", headers=H).json())


def test_settings_fallback_keeps_last_valid_values(ws: Path):
    client = make_client(ws)
    settings = client.get("/api/settings", headers=H).json()
    settings["hosted"].update(enabled=True, allow_registration=False)
    assert client.put("/api/settings", json=settings, headers=H).status_code == 200
    (ws / "workspace.yaml").write_text("name: [broken\n", encoding="utf-8")
    health = client.get("/api/health").json()
    assert health["hosted"] is True and health["registration"] is False
    assert client.post("/api/auth/register", json={"email": "z@example.com", "name": "zoe",
                                                   "password": PASSWORD}).status_code == 403


def test_limiter_keys_and_acquire(ws: Path):
    assert auth_module.limiter_ip_key("2001:db8::1") == "2001:db8::/64"
    assert auth_module.limiter_ip_key("2001:db8::ffff") == auth_module.limiter_ip_key("2001:db8::1")
    assert auth_module.limiter_ip_key("::ffff:10.0.0.1") == "10.0.0.1"
    assert auth_module.limiter_ip_key("10.0.0.1") == "10.0.0.1"
    assert auth_module.limiter_ip_key("testclient") == "testclient"
    limiter = auth_module.RateLimiter(2, 60)
    assert limiter.acquire("k") is None and limiter.acquire("k") is None
    assert limiter.acquire("k") is not None  # third attempt refused
    limiter.PURGE_EVERY = 2
    limiter.acquire("other")
    assert "other" in limiter._hits


# -- repository symlinks and GPU-provided identifiers --------------------------------------------
def test_repository_symlinks_are_never_followed(ws: Path):
    secret = ws.parent / "secret.txt"
    secret.write_text("SCW_SECRET_KEY=xyz\n", encoding="utf-8")
    project = ws / "projects" / "demo"
    (project / "notes.md").unlink(missing_ok=True)
    (project / "notes.md").symlink_to(secret)
    (ws / "projects" / "evil").symlink_to(ws.parent)  # a project folder pointing outside
    (ws / "datasets" / "leak.yaml").symlink_to(secret)
    client = make_client(ws)
    detail = client.get("/api/projects/demo", headers=H).json()
    assert detail["notes"] == ""
    assert "evil" not in [p["name"] for p in client.get("/api/projects", headers=H).json()]
    assert client.get("/api/projects/evil", headers=H).status_code == 404
    assert "leak" not in [d["name"] for d in client.get("/api/datasets", headers=H).json()]
    from wmlab.server import catalog

    assert catalog.read_raw(ws / "datasets" / "leak.yaml") is None
    big = ws / "datasets" / "big.yaml"
    big.write_text("a: 1\n" + "#" * (catalog.MAX_FILE_BYTES + 10), encoding="utf-8")
    assert catalog.read_raw(big) is None
    deep = ws / "datasets" / "deep.yaml"
    deep.write_text("a: " + "[" * 5000 + "]" * 5000, encoding="utf-8")
    assert catalog.read_raw(deep) is None  # RecursionError swallowed
    assert client.get("/api/datasets", headers=H).status_code == 200


def test_gpu_provided_identifiers_are_validated(ws: Path):
    from helpers_server import SCW_HOST
    from wmlab.spec import HostSpec
    from wmlab.server.executors.base import ExecContext
    from wmlab.server.executors.ssh import SshExecutor

    ctx = ExecContext(workspace=ws, data=ws / ".wmlab", runner=FakeRunner())
    executor = SshExecutor(HostSpec.model_validate(SCW_HOST), ctx)
    for bad in ("../x", "a/b", "..", "r1/../../etc"):
        with pytest.raises(ValueError):
            executor.pull("runs", bad)
    with pytest.raises(ValueError):
        executor.pull("secrets", "r1")
    with pytest.raises(ValueError):
        executor.fetch_checkpoint("r1", "../id_ed25519")
    argv = executor.pull_argv("/root/wmlab-data/runs/r1", ws / ".wmlab" / "runs" / "r1")
    assert argv[:3] == ["rsync", "-az", "--safe-links"]
