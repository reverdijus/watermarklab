"""Hosted mode §12.3: git state, pulls, commit+push from the app, revision resolution, exports
and the per-sha SSH launch (temporary bare repository as ``origin``)."""

from __future__ import annotations

import hashlib
import shlex
import time
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from helpers_server import (
    GIT_STATE_KEYS,
    FakeRunner,
    git,
    git_clone,
    git_workspace,
    job_status_of,
    populate,
    wait_for,
    write_fake_cli,
)
from wmlab.server.app import create_app
from wmlab.server.context import ServerContext
from wmlab.server.gitops import EXPORT_KEEP, GitError, GitRepo, GitSync, lock_digest, valid_ref
from wmlab.server.jobs import JobError, JobManager
from wmlab.spec import WorkspaceSettings
from wmlab.tracking import read_json

SPEC = "projects/demo/experiments/baseline.yaml"
TOKEN = "t"


@pytest.fixture
def ws(workspace: Path) -> Path:
    populate(workspace)
    return workspace


@pytest.fixture
def repo(ws: Path) -> tuple[GitRepo, Path]:
    bare = git_workspace(ws)
    return GitRepo(ws, ws / ".wmlab"), bare


def hosted(ws: Path, **changes) -> None:
    """Enable hosted mode in workspace.yaml (committed so the tree stays clean)."""
    data = yaml.safe_load((ws / "workspace.yaml").read_text(encoding="utf-8"))
    data["hosted"] = {"enabled": True, "git_branch": "main", "auto_pull_s": 60,
                      "auto_commit": True, "public_url": None, **changes}
    (ws / "workspace.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    git(ws, "commit", "-q", "-am", "hosted")
    git(ws, "push", "-q", "origin", "main")


def other_member_commit(bare: Path, where: Path, path: str, content: str,
                        message: str = "upstream", branch: str = "main") -> str:
    if (where / ".git").is_dir():
        clone = where
    else:
        clone = git_clone(bare, where)
        if branch != "main":
            git(clone, "checkout", "-q", "-b", branch)
    (clone / path).parent.mkdir(parents=True, exist_ok=True)
    (clone / path).write_text(content, encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", message)
    git(clone, "push", "-q", "origin", f"HEAD:{branch}")
    return git(clone, "rev-parse", "HEAD")


# -- GitRepo ----------------------------------------------------------------------------------
def test_unavailable_without_repository(ws: Path):
    repo = GitRepo(ws, ws / ".wmlab")
    assert repo.available is False and repo.head() is None
    state = repo.state(False)
    assert set(state) == GIT_STATE_KEYS and state["available"] is False
    assert repo.resolve("main") is None
    assert repo.pull("main") == "aucun dépôt git dans le workspace"
    with pytest.raises(GitError):
        repo.export("0" * 40)


def test_state_and_dirty(repo: tuple[GitRepo, Path], ws: Path):
    repo, _ = repo
    state = repo.state(True)
    assert set(state) == GIT_STATE_KEYS
    assert state["available"] and state["hosted"] and state["branch"] == "main"
    assert state["sha"] == git(ws, "rev-parse", "HEAD") and state["short_sha"] == state["sha"][:7]
    assert state["dirty"] is False and state["remote"].endswith("origin.git")
    assert state["ahead"] == 0 and state["behind"] == 0
    assert state["last_pull_at"] is None and state["last_pull_error"] is None
    (ws / SPEC).write_text((ws / SPEC).read_text(encoding="utf-8") + "\n# edit\n",
                           encoding="utf-8")
    assert repo.dirty() is True
    git(ws, "checkout", "--", SPEC)
    # workspace.yaml is rewritten by the app: it never counts as dirty
    (ws / "workspace.yaml").write_text((ws / "workspace.yaml").read_text(encoding="utf-8")
                                       + "# settings\n", encoding="utf-8")
    assert repo.dirty() is False
    git(ws, "checkout", "--", "workspace.yaml")


def test_pull_ff_only(repo: tuple[GitRepo, Path], ws: Path, tmp_path: Path):
    repo, bare = repo
    sha = other_member_commit(bare, tmp_path / "clone", "benchmarks/new_v1.yaml", "name: x\n")
    assert repo.state(True)["behind"] is None or repo.state(True)["behind"] == 0  # not fetched
    assert repo.pull("main") is None
    assert repo.head() == sha and repo.last_pull_at and repo.last_pull_error is None
    assert (ws / "benchmarks" / "new_v1.yaml").is_file()
    # divergence: a local commit not in origin ⇒ ff-only refuses, error memorised
    other_member_commit(bare, tmp_path / "clone", "benchmarks/other.yaml", "name: y\n")
    (ws / "benchmarks" / "local.yaml").write_text("name: z\n", encoding="utf-8")
    git(ws, "add", "-A")
    git(ws, "commit", "-q", "-m", "local")
    error = repo.pull("main")
    assert error and "ff-only" in error and repo.last_pull_error == error
    assert repo.state(True)["ahead"] == 1 and repo.state(True)["behind"] == 1
    git(ws, "reset", "-q", "--hard", "origin/main")
    # dirty tree ⇒ no pull; wrong branch ⇒ no pull
    (ws / SPEC).write_text("name: broken\n", encoding="utf-8")
    assert "modifié" in repo.pull("main")
    git(ws, "checkout", "--", SPEC)
    assert "branche" in repo.pull("release")
    assert "invalide" in repo.pull("--upload-pack=x")


def test_gitsync_once(repo: tuple[GitRepo, Path], ws: Path, tmp_path: Path):
    repo, bare = repo
    settings = WorkspaceSettings.model_validate({"hosted": {"enabled": True, "auto_pull_s": 30}})
    sync = GitSync(repo, lambda: settings)
    sha = other_member_commit(bare, tmp_path / "clone", "datasets/d.yaml", "name: d\n")
    sync.once()
    assert repo.head() == sha
    disabled = WorkspaceSettings()
    other_member_commit(bare, tmp_path / "clone", "datasets/e.yaml", "name: e\n")
    GitSync(repo, lambda: disabled).once()
    assert repo.head() == sha  # hosted mode off: nothing pulled
    sync.start()
    assert sync.running
    sync.stop()
    assert not sync.running


def test_commit_and_push(repo: tuple[GitRepo, Path], ws: Path, tmp_path: Path):
    repo, bare = repo
    (ws / SPEC).write_text((ws / SPEC).read_text(encoding="utf-8") + "# by marie\n",
                           encoding="utf-8")
    result = repo.commit_and_push(SPEC, "marie", "main")
    assert result["pushed"] is True and result["error"] is None
    assert result["committed"] == repo.head()
    assert git(ws, "log", "-1", "--format=%s|%an|%ae") == f"app(marie): {SPEC}|marie|marie@wmlab.local"
    assert git(bare, "rev-parse", "main") == repo.head()
    assert repo.dirty() is False
    # nothing changed ⇒ no commit
    again = repo.commit_and_push(SPEC, "marie", "main")
    assert again == {"committed": None, "pushed": False, "error": None}
    # origin moved meanwhile ⇒ first push rejected, pull --rebase then second push
    other_member_commit(bare, tmp_path / "clone", "benchmarks/up.yaml", "name: up\n")
    (ws / "datasets" / "coco.yaml").write_text("name: coco\nmodality: image\npaths: {default: /x}\n",
                                               encoding="utf-8")
    rebased = repo.commit_and_push("datasets/coco.yaml", "marie", "main")
    assert rebased["pushed"] is True and rebased["error"] is None, rebased
    assert git(bare, "rev-parse", "main") == repo.head() == rebased["committed"]
    assert (ws / "benchmarks" / "up.yaml").is_file()


def test_push_failure_keeps_the_commit(repo: tuple[GitRepo, Path], ws: Path):
    repo, bare = repo
    git(ws, "remote", "set-url", "origin", str(bare.parent / "missing.git"))
    (ws / SPEC).write_text((ws / SPEC).read_text(encoding="utf-8") + "# x\n", encoding="utf-8")
    result = repo.commit_and_push(SPEC, "marie", "main")
    assert result["committed"] == repo.head() and result["pushed"] is False
    assert result["error"] and "git push" in result["error"]
    assert repo.dirty() is False  # the local commit exists, the tree is clean
    assert git(ws, "log", "-1", "--format=%s") == f"app(marie): {SPEC}"


def test_resolve_refs(repo: tuple[GitRepo, Path], ws: Path, tmp_path: Path):
    repo, bare = repo
    head = repo.head()
    assert repo.resolve("HEAD") == head and repo.resolve("main") == head
    assert repo.resolve(head) == head and repo.resolve(head[:8]) == head
    assert repo.resolve("nope") is None
    for bad in ("-x", "a b", "a..b", "$(x)", "x;y", "", "~"):
        assert not valid_ref(bad) and repo.resolve(bad) is None
    assert valid_ref("feature/x") and valid_ref("v1.0") and valid_ref("origin/main")
    # a branch pushed by a member is resolved after a fetch
    clone = git_clone(bare, tmp_path / "clone")
    git(clone, "checkout", "-q", "-b", "feature/lr")
    (clone / "datasets" / "f.yaml").write_text("name: f\n", encoding="utf-8")
    git(clone, "add", "-A")
    git(clone, "commit", "-q", "-m", "feature")
    git(clone, "push", "-q", "origin", "feature/lr")
    feature = git(clone, "rev-parse", "HEAD")
    assert repo.resolve("feature/lr") == feature
    assert repo.resolve("origin/feature/lr") == feature


def test_export_and_rotation(repo: tuple[GitRepo, Path], ws: Path):
    repo, _ = repo
    head = repo.head()
    folder = repo.export(head)
    assert folder == ws / ".wmlab" / "code" / head
    assert (folder / SPEC).is_file() and (folder / "workspace.yaml").is_file()
    assert not (folder / ".wmlab").exists() and not (folder / ".git").exists()
    assert (folder / ".wmlab-export").read_text(encoding="utf-8").startswith(head)
    assert lock_digest(folder) is None
    (ws / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    git(ws, "add", "uv.lock")
    git(ws, "commit", "-q", "-m", "lock")
    second = repo.export(repo.head())
    assert lock_digest(second) == hashlib.sha256(b"version = 1\n").hexdigest()
    # cached: same folder, no re-extraction (marker untouched)
    marker = (second / ".wmlab-export").stat().st_mtime_ns
    assert repo.export(repo.head()) == second
    assert (second / ".wmlab-export").stat().st_mtime_ns == marker
    # rotation keeps the 10 most recent exports
    shas = []
    for i in range(EXPORT_KEEP + 2):
        (ws / "datasets" / f"rot{i}.yaml").write_text(f"name: rot{i}\n", encoding="utf-8")
        git(ws, "add", "-A")
        git(ws, "commit", "-q", "-m", f"rot {i}")
        sha = repo.head()
        exported = repo.export(sha)
        time.sleep(0.01)
        os_utime = exported.stat().st_mtime
        assert os_utime
        shas.append(sha)
    remaining = {p.name for p in (ws / ".wmlab" / "code").iterdir() if p.is_dir()}
    assert len(remaining) == EXPORT_KEEP
    assert set(shas[-EXPORT_KEEP:]) <= remaining and head not in remaining
    with pytest.raises(GitError, match="sha invalide"):
        repo.export("main")
    with pytest.raises(GitError, match="git archive"):
        repo.export("0" * 40)


# -- API ----------------------------------------------------------------------------------------
def test_git_routes_and_file_commit(repo: tuple[GitRepo, Path], ws: Path, tmp_path: Path):
    _, bare = repo
    hosted(ws)
    client = TestClient(create_app(ws, TOKEN, background=False), headers={"X-WMLab-Token": TOKEN})
    state = client.get("/api/git").json()
    assert set(state) == GIT_STATE_KEYS and state["hosted"] is True and state["branch"] == "main"
    # PUT /api/files → commit + push, response enriched with ``git``
    content = (ws / SPEC).read_text(encoding="utf-8") + "# from the app\n"
    saved = client.put("/api/files", json={"path": SPEC, "content": content}).json()
    assert saved["saved"] and saved["git"]["pushed"] is True and saved["git"]["error"] is None
    assert saved["git"]["committed"] == git(bare, "rev-parse", "main")
    assert git(ws, "log", "-1", "--format=%s|%an") == f"app(admin): {SPEC}|admin"
    # a plugin file too, under the user's name (an admin account: members are read-only)
    from helpers_server import add_user_session

    marie_token = add_user_session(ws, "marie", "admin")
    plugin = "lab/metrics/mine.py"
    saved = client.put("/api/files", json={"path": plugin, "content": "# plugin\n"},
                       headers={"X-WMLab-Token": marie_token}).json()
    assert saved["git"]["pushed"] and git(ws, "log", "-1", "--format=%an|%ae") == "marie|marie@wmlab.local"
    # push failure ⇒ 200, committed but not pushed
    git(ws, "remote", "set-url", "origin", str(bare.parent / "gone.git"))
    saved = client.put("/api/files", json={"path": SPEC, "content": content + "# 2\n"}).json()
    assert saved["saved"] and saved["git"]["committed"] and saved["git"]["pushed"] is False
    assert "git push" in saved["git"]["error"]
    git(ws, "remote", "set-url", "origin", str(bare))
    assert client.get("/api/git").json()["ahead"] == 1
    # POST /api/git/pull : fast-forward from origin, 409 on a dirty tree
    git(ws, "push", "-q", "origin", "main")
    sha = other_member_commit(bare, tmp_path / "clone", "benchmarks/pulled.yaml", "name: p\n")
    pulled = client.post("/api/git/pull")
    assert pulled.status_code == 200, pulled.text
    assert pulled.json()["sha"] == sha and pulled.json()["last_pull_at"]
    (ws / SPEC).write_text("name: dirty\n", encoding="utf-8")
    denied = client.post("/api/git/pull")
    assert denied.status_code == 409 and "modifié" in denied.json()["detail"]
    git(ws, "checkout", "--", SPEC)
    # auto_commit off ⇒ git: null
    settings = client.get("/api/settings").json()
    settings["hosted"]["auto_commit"] = False
    assert client.put("/api/settings", json=settings).status_code == 200
    saved = client.put("/api/files", json={"path": SPEC, "content": content + "# 3\n"}).json()
    assert saved["git"] is None


def test_settings_are_committed_in_hosted_mode(repo: tuple[GitRepo, Path], ws: Path):
    _, bare = repo
    hosted(ws)
    client = TestClient(create_app(ws, TOKEN, background=False), headers={"X-WMLab-Token": TOKEN})
    settings = client.get("/api/settings").json()
    settings["budget"]["total_eur"] = 12345
    assert client.put("/api/settings", json=settings).status_code == 200
    assert git(ws, "log", "-1", "--format=%s") == "app(admin): workspace.yaml"
    assert git(bare, "rev-parse", "main") == git(ws, "rev-parse", "HEAD")
    assert client.get("/api/git").json()["dirty"] is False


def test_file_write_outside_hosted_mode_has_no_git(repo: tuple[GitRepo, Path], ws: Path):
    client = TestClient(create_app(ws, TOKEN, background=False), headers={"X-WMLab-Token": TOKEN})
    content = (ws / SPEC).read_text(encoding="utf-8") + "# local\n"
    saved = client.put("/api/files", json={"path": SPEC, "content": content}).json()
    assert saved["git"] is None
    state = client.get("/api/git").json()
    assert state["available"] and state["hosted"] is False and state["dirty"] is True


# -- jobs with git_ref ----------------------------------------------------------------------------
def test_local_jobs_run_head_only(repo: tuple[GitRepo, Path], ws: Path, tmp_path: Path):
    _, bare = repo
    head = git(ws, "rev-parse", "HEAD")
    other = other_member_commit(bare, tmp_path / "clone", "datasets/x.yaml", "name: x\n",
                                branch="feature")
    cli = write_fake_cli(tmp_path)
    client = TestClient(create_app(ws, TOKEN, background=False, cli_prefix=cli),
                        headers={"X-WMLab-Token": TOKEN})
    job = client.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC})
    assert job.status_code == 200, job.text
    assert job.json()["git_ref"] == "HEAD" and job.json()["git_sha"] == head
    launch = read_json(ws / ".wmlab" / "jobs" / "launch" / f"{job.json()['job_id']}.json")
    assert launch["git"]["sha"] == head and launch["git_sha"] == head
    same = client.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC,
                                          "git_ref": "main"})
    assert same.status_code == 200 and same.json()["git_ref"] == "main"
    refused = client.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC,
                                             "git_ref": "feature"})
    assert refused.status_code == 400
    assert "révision courante du serveur" in refused.json()["detail"]
    assert other[:7] in refused.json()["detail"]
    unknown = client.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC,
                                             "git_ref": "nope"})
    assert unknown.status_code == 400 and "inconnue" in unknown.json()["detail"]
    assert client.post("/api/jobs", json={"kind": "train", "host": "local", "spec_path": SPEC,
                                          "git_ref": "-x"}).status_code == 400
    for j in client.get("/api/jobs").json():
        client.post(f"/api/jobs/{j['job_id']}/cancel")


def test_git_ref_refused_without_repository(ws: Path, tmp_path: Path):
    client = TestClient(create_app(ws, TOKEN, background=False), headers={"X-WMLab-Token": TOKEN})
    refused = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC,
                                             "git_ref": "main"})
    assert refused.status_code == 400 and "Aucun dépôt git" in refused.json()["detail"]


def test_ssh_job_by_sha(repo: tuple[GitRepo, Path], ws: Path, tmp_path: Path):
    """SSH launch of an exported revision: rsync of ``<data>/code/<sha>``, conditional
    ``uv sync`` on the uv.lock digest, launch command from ``code/<sha>``."""
    _, bare = repo
    (ws / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    git(ws, "add", "uv.lock")
    git(ws, "commit", "-q", "-m", "lock")
    git(ws, "push", "-q", "origin", "main")
    head = git(ws, "rev-parse", "HEAD")
    feature = other_member_commit(bare, tmp_path / "clone", "datasets/feat.yaml", "name: f\n",
                                  branch="feature/lr")
    runner = FakeRunner()
    client = TestClient(create_app(ws, TOKEN, background=False, runner=runner),
                        headers={"X-WMLab-Token": TOKEN})
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC,
                                         "git_ref": "feature/lr", "overrides": {"seed": "3"}})
    assert job.status_code == 200, job.text
    job = job.json()
    assert job["git_ref"] == "feature/lr" and job["git_sha"] == feature
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "running")
    export = ws / ".wmlab" / "code" / feature
    assert (export / "datasets" / "feat.yaml").is_file()
    digest = hashlib.sha256(b"version = 1\n").hexdigest()
    launch = read_json(ws / ".wmlab" / "jobs" / "launch" / f"{job['job_id']}.json")
    assert launch["git"] == {"sha": feature, "branch": "feature/lr", "dirty": False,
                             "remote": str(bare)}
    assert launch["git_diff"] is None and launch["git_sha"] == feature

    push = next(c for c in runner.rsync_calls() if c[-2] == f"{export}/")
    assert push[:3] == ["rsync", "-az", "--delete"] and ".git" in push
    assert push[-1] == f"root@51.15.0.1:/root/wmlab/code/{feature}/"
    commands = runner.ssh_commands()
    assert commands[0].startswith("mkdir -p /root/wmlab /root/wmlab-data/runs")
    assert f"mkdir -p /root/wmlab/code/{feature}" in commands
    sync = next(c for c in commands if "uv sync" in c)
    assert sync == (
        'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"; '
        f"cd /root/wmlab/code/{feature} && "
        f'if [ "$(cat /root/wmlab/.venv/.wmlab-lock 2>/dev/null)" != {digest} ]; then '
        f"UV_PROJECT_ENVIRONMENT=/root/wmlab/.venv uv sync --frozen --extra train "
        f"&& printf '%s' {digest} > /root/wmlab/.venv/.wmlab-lock; fi")
    launch_cmd = next(c for c in commands if "nohup setsid" in c)
    assert launch_cmd.startswith(
        f"cd /root/wmlab/code/{feature} && {{ PYTHONPATH=/root/wmlab/code/{feature} "
        f"WMLAB_WORKSPACE=/root/wmlab/code/{feature} WMLAB_DATA=/root/wmlab-data WMLAB_HOST=scw "
        f"WMLAB_PRICE_EUR_PER_HOUR=1.5 nohup setsid /root/wmlab/.venv/bin/python -m wmlab.cli "
        f"train {SPEC} --run-id {job['run_id']} --set seed=3 --launch-file "
        f"/root/wmlab-data/launch/{job['job_id']}.json")
    words = shlex.split(launch_cmd.split("nohup setsid ", 1)[1].split(" > ", 1)[0])
    assert words[:3] == ["/root/wmlab/.venv/bin/python", "-m", "wmlab.cli"]
    # the sha order: mkdir → rsync export → uv sync → launch
    assert commands.index(sync) < commands.index(launch_cmd)

    # hosted mode: the default revision is HEAD (export cached per sha)
    hosted(ws)
    head = git(ws, "rev-parse", "HEAD")
    second = client.post("/api/jobs", json={"kind": "eval", "host": "scw", "run_id": "r2",
                                            "checkpoint": "last", "benchmark": "image_v1"}).json()
    assert second["git_ref"] == "HEAD" and second["git_sha"] == head
    assert wait_for(lambda: job_status_of(client, second["job_id"])["status"] == "running")
    assert (ws / ".wmlab" / "code" / head / "uv.lock").is_file()
    eval_cmd = [c for c in runner.ssh_commands() if "nohup setsid" in c][-1]
    assert eval_cmd.startswith(f"cd /root/wmlab/code/{head} && {{ PYTHONPATH=/root/wmlab/code/{head} ")
    assert f"eval --run r2 --checkpoint last --benchmark image_v1" in eval_cmd
    assert client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC,
                                          "git_ref": "unknown-branch"}).status_code == 400


def test_ssh_job_outside_hosted_mode_syncs_the_working_copy(repo: tuple[GitRepo, Path],
                                                            ws: Path):
    """A Mac outside hosted mode without ``git_ref``: legacy rsync of the workspace to
    ``workdir``; ``git_sha`` records HEAD, ``git_ref`` stays null."""
    runner = FakeRunner()
    head = git(ws, "rev-parse", "HEAD")
    client = TestClient(create_app(ws, TOKEN, background=False, runner=runner),
                        headers={"X-WMLab-Token": TOKEN})
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC}).json()
    assert job["git_ref"] is None and job["git_sha"] == head
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "running")
    push = runner.rsync_calls()[0]
    assert push[-2] == f"{ws}/" and push[-1] == "root@51.15.0.1:/root/wmlab/"
    launch = next(c for c in runner.ssh_commands() if "nohup setsid" in c)
    assert launch.startswith("cd /root/wmlab && { WMLAB_DATA=/root/wmlab-data ")
    assert not (ws / ".wmlab" / "code").exists()
    # the launch file keeps the real checkout state (HEAD, dirty flag)
    info = read_json(ws / ".wmlab" / "jobs" / "launch" / f"{job['job_id']}.json")
    assert info["git"]["sha"] == head and info["git_ref"] is None
    setup = client.post("/api/hosts/scw/setup").json()
    assert setup["git_ref"] is None and setup["git_sha"] == head
    assert wait_for(lambda: job_status_of(client, setup["job_id"])["status"] == "completed")
    assert runner.ssh_commands()[-1] == ("cd /root/wmlab && bash scripts/remote_bootstrap.sh "
                                         "/root/wmlab-data")


def test_ssh_job_without_lock_skips_uv_sync(repo: tuple[GitRepo, Path], ws: Path):
    hosted(ws)
    runner = FakeRunner()
    client = TestClient(create_app(ws, TOKEN, background=False, runner=runner),
                        headers={"X-WMLab-Token": TOKEN})
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC}).json()
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "running")
    assert not any("uv sync" in c for c in runner.ssh_commands())
    assert any(f"cd /root/wmlab/code/{job['git_sha']} &&" in c for c in runner.ssh_commands())


def test_launch_file_describes_the_exported_commit(repo: tuple[GitRepo, Path], ws: Path):
    """A by-revision SSH launch of HEAD on a dirty checkout: the launch file describes the
    clean export (``dirty: false``, no diff); the local host keeps the real checkout state."""
    hosted(ws)
    head = git(ws, "rev-parse", "HEAD")
    spec = ws / SPEC
    spec.write_text(spec.read_text(encoding="utf-8") + "# local edit\n", encoding="utf-8")
    runner = FakeRunner()
    client = TestClient(create_app(ws, TOKEN, background=False, runner=runner),
                        headers={"X-WMLab-Token": TOKEN})
    remote = client.post("/api/jobs", json={"kind": "train", "host": "scw",
                                            "spec_path": SPEC}).json()
    assert remote["git_ref"] == "HEAD" and remote["git_sha"] == head
    info = read_json(ws / ".wmlab" / "jobs" / "launch" / f"{remote['job_id']}.json")
    assert info["git"] == {"sha": head, "branch": "HEAD", "dirty": False,
                           "remote": info["git"]["remote"]}
    assert info["git_diff"] is None
    local = client.post("/api/jobs", json={"kind": "train", "host": "local",
                                           "spec_path": SPEC}).json()
    assert local["git_ref"] == "HEAD" and local["git_sha"] == head
    info = read_json(ws / ".wmlab" / "jobs" / "launch" / f"{local['job_id']}.json")
    assert info["git"]["dirty"] is True and "# local edit" in (info["git_diff"] or "")


def test_ssh_job_fails_when_uv_sync_fails(repo: tuple[GitRepo, Path], ws: Path):
    hosted(ws)
    (ws / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    git(ws, "add", "uv.lock")
    git(ws, "commit", "-q", "-m", "lock")

    class Runner(FakeRunner):
        def __call__(self, argv, *, timeout=None):
            if argv[0] == "ssh" and "uv sync" in argv[-1]:
                self.calls.append(list(argv))
                from wmlab.server.executors import CommandResult

                return CommandResult(127, "", "bash: uv: command not found")
            return super().__call__(argv, timeout=timeout)

    runner = Runner()
    client = TestClient(create_app(ws, TOKEN, background=False, runner=runner),
                        headers={"X-WMLab-Token": TOKEN})
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC}).json()
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "failed")
    error = job_status_of(client, job["job_id"])["error"]
    assert "environnement Python" in error and "Préparer" in error
    assert not any("nohup setsid" in c for c in runner.ssh_commands())


def test_setup_job_in_git_mode(repo: tuple[GitRepo, Path], ws: Path):
    hosted(ws)
    runner = FakeRunner()
    head = git(ws, "rev-parse", "HEAD")
    client = TestClient(create_app(ws, TOKEN, background=False, runner=runner),
                        headers={"X-WMLab-Token": TOKEN})
    job = client.post("/api/hosts/scw/setup").json()
    assert job["git_ref"] == "HEAD" and job["git_sha"] == head
    assert wait_for(lambda: job_status_of(client, job["job_id"])["status"] == "completed")
    export = ws / ".wmlab" / "code" / head
    assert (export / "scripts" / "remote_bootstrap.sh").is_file() or not (
        ws / "scripts").exists()
    commands = runner.ssh_commands()
    assert commands[0] == f"mkdir -p /root/wmlab /root/wmlab/code/{head}"
    assert commands[-1] == (f"cd /root/wmlab/code/{head} && UV_PROJECT_ENVIRONMENT=/root/wmlab/.venv "
                            "bash scripts/remote_bootstrap.sh /root/wmlab-data")
    push = runner.rsync_calls()[0]
    assert push[-2] == f"{export}/" and push[-1] == f"root@51.15.0.1:/root/wmlab/code/{head}/"


def test_export_failure_fails_the_job(repo: tuple[GitRepo, Path], ws: Path):
    ctx = ServerContext.for_workspace(ws)
    manager = JobManager(ctx, runner=FakeRunner())
    job = manager.submit(kind="train", host="scw", spec_path=SPEC, git_ref="main")
    assert job.git_sha and job.git_ref == "main"
    # the export cache is not writable ⇒ the launch fails cleanly
    (ws / ".wmlab" / "code").write_text("not a directory", encoding="utf-8")
    manager._schedule()
    assert wait_for(lambda: manager.get(job.job_id).status == "failed")
    assert "Échec du lancement" in manager.get(job.job_id).error
    with pytest.raises(JobError):
        manager.submit(kind="train", host="scw", spec_path=SPEC, git_ref="nope")
