"""SSH executor (Scaleway GPU servers): rsync of the code, detached launch, rsync of runs.

Every remote command is a single string built with ``shlex.quote`` on each element; the
local side always runs argument lists (``ssh``/``rsync``), never a local shell. No secret
is stored: ``ssh`` uses ``~/.ssh/config`` and the user's keys (``BatchMode=yes``).
"""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import Any

import logging

from wmlab.server.executors.base import (
    CommandResult,
    Executor,
    JobRecord,
    LaunchResult,
    ProcessState,
    job_log_name,
)
from wmlab.server.inventory import (
    RemoteListing,
    listing_command,
    parse_listing,
    retrieval_result,
    verify_runs,
)
from wmlab.tracking import read_json, read_jsonl

log = logging.getLogger("wmlab.server")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")  # ids coming from a GPU

SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
CODE_EXCLUDES = (".git", ".venv", ".wmlab", "desktop", "node_modules", "__pycache__")
PULL_EXCLUDES = ("checkpoints/*.pt", "checkpoints/.*.tmp")
FULL_PULL_EXCLUDES = ("checkpoints/.*.tmp",)
FINAL_RUN_STATUSES = ("completed", "failed", "killed")
DEFAULT_WORKDIR = "~/wmlab"
DEFAULT_PYTHON = ".venv/bin/python"
VENV_DIR = ".venv"
LOCK_MARKER = ".wmlab-lock"
REMOTE_PATH_EXPORT = 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"'
TIMEOUT_SHORT_S = 30.0
TIMEOUT_SYNC_S = 900.0
TIMEOUT_SETUP_S = 3600.0
TIMEOUT_LISTING_S = 120.0


def shell_path(path: str) -> str:
    """Quote a remote path for the remote shell, keeping a leading ``~`` expandable."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def rsync_path(path: str) -> str:
    """Remote path for rsync (``~/x`` → ``x``, i.e. relative to the remote home)."""
    if path == "~":
        return "."
    return path[2:] if path.startswith("~/") else path


def _join(base: str, *parts: str) -> str:
    return "/".join([base.rstrip("/"), *parts])


class SshExecutor(Executor):
    """Runs jobs on a remote host through ``ssh`` / ``rsync``."""

    # -- host settings ---------------------------------------------------------------------
    @property
    def target(self) -> str:
        return str(self.host.ssh_target)

    @property
    def workdir(self) -> str:
        return self.host.workdir or DEFAULT_WORKDIR

    @property
    def data_dir(self) -> str:
        return self.host.data_dir or _join(self.workdir, ".wmlab")

    @property
    def python(self) -> str:
        return self.host.python or DEFAULT_PYTHON

    @property
    def venv(self) -> str:
        """Shared virtualenv of the per-sha layout (§12.3)."""
        return _join(self.workdir, VENV_DIR)

    def code_remote(self, sha: str) -> str:
        return _join(self.workdir, "code", sha)

    def python_absolute(self) -> str:
        """``python`` of the host as an absolute (or ``~``) path, for the per-sha layout
        where the working directory is not ``workdir``."""
        python = self.python
        if python.startswith(("/", "~")):
            return python
        return _join(self.workdir, python)

    def cli_prefix(self) -> list[str]:
        return list(self.ctx.remote_cli_prefix or [self.python, "-m", "wmlab.cli"])

    def cli_prefix_shell(self, by_sha: bool) -> str:
        """The interpreter part of a launch command, quoted for the remote shell."""
        if self.ctx.remote_cli_prefix:
            return " ".join(shlex.quote(a) for a in self.ctx.remote_cli_prefix)
        python = self.python_absolute() if by_sha else self.python
        return f"{shell_path(python)} -m wmlab.cli"

    # -- command builders ------------------------------------------------------------------
    def ssh_argv(self, remote_command: str) -> list[str]:
        return ["ssh", *SSH_OPTIONS, "--", self.target, remote_command]

    def rsh(self) -> str:
        return shlex.join(["ssh", *SSH_OPTIONS])

    def remote_spec(self, path: str) -> str:
        """``target:path`` for rsync; the path is quoted for the remote shell (rsync hands
        it to that shell: a space or ``;`` would otherwise split or execute)."""
        return f"{self.target}:{shell_path(rsync_path(path))}"

    def code_excludes(self) -> list[str]:
        excludes = list(CODE_EXCLUDES)
        try:  # a data root inside the workspace but not named .wmlab
            rel = self.ctx.data.resolve().relative_to(self.ctx.workspace.resolve())
            if rel.parts and rel.parts[0] not in excludes:
                excludes.append(rel.parts[0])
        except ValueError:
            pass
        return excludes

    def push_code_argv(self, source: Path | None = None, remote_dir: str | None = None
                       ) -> list[str]:
        """rsync of the workspace (or of a git export) to ``workdir`` (or ``code/<sha>``)."""
        argv = ["rsync", "-az", "--delete"]
        for pattern in self.code_excludes():
            argv += ["--exclude", pattern]
        return argv + ["-e", self.rsh(), f"{source or self.ctx.workspace}/",
                       f"{self.remote_spec(remote_dir or self.workdir)}/"]

    def mkdir_command(self, *paths: str) -> str:
        return "mkdir -p " + " ".join(shell_path(p) for p in paths)

    def remote_launch_file(self, job: JobRecord) -> str:
        return _join(self.data_dir, "launch", f"{job.job_id}.json")

    def remote_log(self, job: JobRecord) -> str:
        folder, name = job_log_name(job)
        return _join(self.data_dir, folder, name)

    def launch_command(self, job: JobRecord, launch_file: str | None) -> str:
        """``cd <workdir> && { ENV nohup setsid <python> -m wmlab.cli … > log 2>&1 & echo $!;
        ps -o lstart= -p $!; }`` — the start time guards later polls against pid reuse.

        With ``job.code_dir`` (git export, §12.3) the working directory is
        ``<workdir>/code/<sha>`` and ``PYTHONPATH``/``WMLAB_WORKSPACE`` point to it, so the
        ``wmlab`` package of that revision is the one imported whatever the editable install
        of the shared venv.
        """
        by_sha = bool(job.code_dir and job.git_sha)
        cwd = self.code_remote(str(job.git_sha)) if by_sha else self.workdir
        env = ([f"PYTHONPATH={shell_path(cwd)}", f"WMLAB_WORKSPACE={shell_path(cwd)}"]
               if by_sha else []) + [
            f"WMLAB_DATA={shell_path(self.data_dir)}",
            f"WMLAB_HOST={shlex.quote(self.host.name)}",
            f"WMLAB_PRICE_EUR_PER_HOUR={shlex.quote(str(self.host.price_eur_per_hour))}",
        ]
        args = " ".join(shlex.quote(a) for a in self.cli_args(job, None))
        command = f"{self.cli_prefix_shell(by_sha)} {args}"
        if launch_file and job.kind in ("train", "hpo"):
            command += f" --launch-file {shell_path(launch_file)}"
        return (f"cd {shell_path(cwd)} && {{ {' '.join(env)} nohup setsid {command} "
                f"> {shell_path(self.remote_log(job))} 2>&1 < /dev/null & echo $!; "
                f"ps -o lstart= -p $! 2>/dev/null || true; }}")

    def sync_env_command(self, sha: str, lock_sha256: str) -> str:
        """``uv sync --frozen --extra train`` in ``code/<sha>`` into the shared venv, only
        when the SHA-256 of ``uv.lock`` differs from the ``.venv/.wmlab-lock`` marker."""
        marker = shell_path(_join(self.venv, LOCK_MARKER))
        digest = shlex.quote(lock_sha256)
        return (f"{REMOTE_PATH_EXPORT}; cd {shell_path(self.code_remote(sha))} && "
                f"if [ \"$(cat {marker} 2>/dev/null)\" != {digest} ]; then "
                f"UV_PROJECT_ENVIRONMENT={shell_path(self.venv)} uv sync --frozen --extra train "
                f"&& printf '%s' {digest} > {marker}; fi")

    def pull_argv(self, remote_dir: str, local_dir: Path, full: bool = False) -> list[str]:
        # --safe-links: a symlink made on the GPU that points outside the run folder (towards
        # /etc/wmlab/env, the SSH key…) is never recreated on the server
        argv = ["rsync", "-az", "--safe-links"]
        for pattern in FULL_PULL_EXCLUDES if full else PULL_EXCLUDES:
            argv += ["--exclude", pattern]
        if (local_dir / "notes.md").exists():
            argv += ["--exclude", "notes.md"]  # keep notes edited in the app
        return argv + ["-e", self.rsh(), f"{self.remote_spec(remote_dir)}/", f"{local_dir}/"]

    # -- helpers ---------------------------------------------------------------------------
    def run(self, argv: list[str], timeout: float = TIMEOUT_SHORT_S) -> CommandResult:
        return self.runner(argv, timeout=timeout)

    def _check(self, result: CommandResult, what: str) -> CommandResult:
        if not result.ok:
            detail = result.tail(8) or f"code {result.returncode}"
            raise RuntimeError(f"{what} sur {self.host.name} a échoué : {detail}")
        return result

    def push_code(self) -> None:
        self._check(self.run(self.ssh_argv(self.mkdir_command(self.workdir))),
                    "La création du dossier de travail")
        self._check(self.run(self.push_code_argv(), timeout=TIMEOUT_SYNC_S),
                    "La synchronisation du code (rsync)")

    def push_export(self, job: JobRecord) -> str:
        """rsync the local git export of the job to ``<workdir>/code/<sha>`` and make sure
        the shared venv matches its ``uv.lock``; returns the remote code directory."""
        sha = str(job.git_sha)
        remote = self.code_remote(sha)
        self._check(self.run(self.ssh_argv(self.mkdir_command(remote))),
                    "La création du dossier du code distant")
        self._check(self.run(self.push_code_argv(Path(str(job.code_dir)), remote),
                             timeout=TIMEOUT_SYNC_S),
                    f"La synchronisation du code (révision {sha[:7]})")
        if job.lock_sha256:
            result = self.run(self.ssh_argv(self.sync_env_command(sha, job.lock_sha256)),
                              timeout=TIMEOUT_SETUP_S)
            if not result.ok:
                detail = result.tail(8) or f"code {result.returncode}"
                if "uv: command not found" in detail or "uv : commande introuvable" in detail:
                    detail += " — lancez la préparation de la machine (Machines → Préparer)"
                raise RuntimeError(f"La mise à jour de l'environnement Python sur "
                                   f"{self.host.name} a échoué : {detail}")
        return remote

    # -- executor API ----------------------------------------------------------------------
    def launch(self, job: JobRecord) -> LaunchResult:
        log_dir = self.remote_log(job).rsplit("/", 1)[0]
        launch_dir = _join(self.data_dir, "launch")
        self._check(self.run(self.ssh_argv(self.mkdir_command(
            self.workdir, _join(self.data_dir, "runs"), _join(self.data_dir, "studies"),
            launch_dir, log_dir))), "La préparation des dossiers distants")
        if job.code_dir and job.git_sha:
            self.push_export(job)
        else:
            self._check(self.run(self.push_code_argv(), timeout=TIMEOUT_SYNC_S),
                        "La synchronisation du code (rsync)")
        remote_launch: str | None = None
        if job.launch_file and job.kind in ("train", "hpo"):
            remote_launch = self.remote_launch_file(job)
            self._check(self.run(["rsync", "-az", "-e", self.rsh(), job.launch_file,
                                  self.remote_spec(remote_launch)]),
                        "La copie du fichier de lancement")
        result = self._check(self.run(self.ssh_argv(self.launch_command(job, remote_launch))),
                             "Le lancement distant")
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        pid_index = next((i for i, line in enumerate(lines) if line.isdigit()), None)
        if pid_index is None:
            raise RuntimeError(f"Le lancement distant n'a pas renvoyé de pid "
                               f"(sortie : {result.stdout.strip()[:200]!r})")
        remote_start = lines[pid_index + 1] if pid_index + 1 < len(lines) else None
        return LaunchResult(pid=int(lines[pid_index]), remote_log=self.remote_log(job),
                            remote_start=remote_start)

    def poll(self, job: JobRecord, handle: Any = None) -> ProcessState:
        if job.pid is None:
            return ProcessState(alive=False)
        pid = int(job.pid)
        result = self.run(self.ssh_argv(
            f"kill -0 {pid} 2>/dev/null && {{ echo alive; ps -o lstart= -p {pid} 2>/dev/null; }} "
            "|| echo dead"))
        if result.returncode == 255 or not result.ok:
            return ProcessState(alive=None, detail=result.tail(3))
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if "alive" in lines:
            start = next((line for line in lines[lines.index("alive") + 1:]), None)
            if job.remote_start and start and start != job.remote_start:
                # same pid, another process (host rebooted / pid reused): the job is gone
                return ProcessState(alive=False, detail="pid réutilisé par un autre process")
            return ProcessState(alive=True)
        if "dead" in lines:
            return ProcessState(alive=False)
        return ProcessState(alive=None, detail=result.stdout.strip()[:200])

    def cancel(self, job: JobRecord, handle: Any = None) -> None:
        if job.pid is None:
            return
        pid = int(job.pid)
        if job.remote_start and self.poll(job).alive is False:
            return  # never signal a process group that is not ours any more
        self._check(self.run(self.ssh_argv(
            f"kill -TERM -{pid} 2>/dev/null || kill -TERM {pid}")), "L'arrêt du process distant")

    def pull(self, kind: str, item_id: str, full: bool = False) -> CommandResult:
        """rsync ``<data_dir>/<kind>/<id>/`` → ``<local data>/<kind>/<id>/`` (no .pt unless
        ``full``).

        Partial transfers (rsync codes 23/24, e.g. folder not created yet) are tolerated.
        """
        if kind not in ("runs", "studies") or not SAFE_ID.match(item_id) or ".." in item_id:
            raise ValueError(f"identifiant distant refusé : {kind}/{item_id!r}")
        local = self.ctx.data / kind / item_id
        local.mkdir(parents=True, exist_ok=True)
        result = self.run(self.pull_argv(_join(self.data_dir, kind, item_id), local, full),
                          timeout=TIMEOUT_SYNC_S)
        if not result.ok and result.returncode not in (23, 24):
            self._check(result, f"La synchronisation de {kind}/{item_id}")
        return result

    def job_folders(self, job: JobRecord) -> tuple[list[str], list[str]]:
        """``(run_ids, study_ids)`` of a job (trial runs read from the local trials.jsonl)."""
        if job.kind in ("train", "eval") and job.run_id:
            return [job.run_id], []
        if job.kind == "hpo" and job.study_id:
            trials = read_jsonl(self.ctx.data / "studies" / job.study_id / "trials.jsonl")
            runs = list(dict.fromkeys(str(t["run_id"]) for t in trials
                                      if t.get("run_id") and SAFE_ID.match(str(t["run_id"]))))
            return runs, [job.study_id]
        return [], []

    def retrieve(self, job: JobRecord) -> dict[str, Any] | None:
        """End-of-job retrieval (§12.4): full rsync of the job's folders, then every
        ``checkpoints/index.json`` entry must exist locally with the same size (one more
        rsync of the runs with a mismatch)."""
        if job.kind not in ("train", "hpo", "eval"):
            return None
        failed = False
        run_ids: list[str] = []
        try:
            run_ids, study_ids = self.job_folders(job)
            for study_id in study_ids:
                self.pull("studies", study_id, full=True)
            if study_ids:
                run_ids, _ = self.job_folders(job)  # trials known after the study sync
            for run_id in run_ids:
                self.pull("runs", run_id, full=True)
            check = verify_runs(self.ctx.data / "runs", run_ids)
            if check["missing"]:
                for run_id in check["by_run"]:
                    self.pull("runs", run_id, full=True)
                check = verify_runs(self.ctx.data / "runs", run_ids)
        except Exception as exc:
            log.warning("rapatriement du job %s incomplet : %s", job.job_id, exc)
            failed = True
            check = verify_runs(self.ctx.data / "runs", run_ids)
        return retrieval_result(check, failed=failed)

    def listing(self) -> RemoteListing:
        """Runs, indexes, ``*.pt`` sizes and studies of the remote data root (one command)."""
        result = self.run(self.ssh_argv(listing_command(shell_path(self.data_dir))),
                          timeout=TIMEOUT_LISTING_S)
        if result.returncode == 255:
            raise RuntimeError(f"Connexion SSH impossible à {self.target} : "
                               f"{result.tail(3) or 'code 255'}")
        if not result.ok:
            raise RuntimeError(f"L'inventaire de {self.host.name} a échoué : "
                               f"{result.tail(5) or f'code {result.returncode}'}")
        listing = parse_listing(result.stdout)
        if listing.data_present and not listing.complete:
            raise RuntimeError(f"L'inventaire de {self.host.name} est incomplet (sortie "
                               "tronquée) : réessayez.")
        return listing

    def sync(self, job: JobRecord) -> None:
        if job.kind in ("train", "eval") and job.run_id:
            self.pull("runs", job.run_id)
        elif job.kind == "hpo" and job.study_id:
            self.pull("studies", job.study_id)
            trials = read_jsonl(self.ctx.data / "studies" / job.study_id / "trials.jsonl")
            for run_id in dict.fromkeys(str(t["run_id"]) for t in trials
                                        if t.get("run_id") and SAFE_ID.match(str(t["run_id"]))):
                manifest = read_json(self.ctx.data / "runs" / run_id / "run.json")
                if isinstance(manifest, dict) and manifest.get("status") in FINAL_RUN_STATUSES:
                    continue  # finished trials are already complete locally
                self.pull("runs", run_id)

    def fetch_checkpoint(self, run_id: str, file: str) -> None:
        if not SAFE_ID.match(run_id) or ".." in run_id or not SAFE_ID.match(file) \
                or ".." in file:
            raise ValueError(f"identifiant refusé : {run_id!r}/{file!r}")
        local = self.ctx.data / "runs" / run_id / "checkpoints" / file
        local.parent.mkdir(parents=True, exist_ok=True)
        remote = _join(self.data_dir, "runs", run_id, "checkpoints", file)
        self._check(self.run(["rsync", "-az", "--safe-links", "-e", self.rsh(),
                              self.remote_spec(remote), str(local)], timeout=TIMEOUT_SYNC_S),
                    "Le rapatriement du checkpoint")

    def setup(self, job: JobRecord) -> CommandResult:
        if job.code_dir and job.git_sha:  # git mode: bootstrap from the export of HEAD
            sha = str(job.git_sha)
            remote = self.code_remote(sha)
            self._check(self.run(self.ssh_argv(self.mkdir_command(self.workdir, remote))),
                        "La création du dossier de travail")
            self._check(self.run(self.push_code_argv(Path(str(job.code_dir)), remote),
                                 timeout=TIMEOUT_SYNC_S),
                        f"La synchronisation du code (révision {sha[:7]})")
            command = (f"cd {shell_path(remote)} && UV_PROJECT_ENVIRONMENT={shell_path(self.venv)} "
                       f"bash scripts/remote_bootstrap.sh {shell_path(self.data_dir)}")
            return self.run(self.ssh_argv(command), timeout=TIMEOUT_SETUP_S)
        self.push_code()
        command = (f"cd {shell_path(self.workdir)} && bash scripts/remote_bootstrap.sh "
                   f"{shell_path(self.data_dir)}")
        return self.run(self.ssh_argv(command), timeout=TIMEOUT_SETUP_S)

    def test_command(self) -> str:
        return ("echo ok; nvidia-smi --query-gpu=name,memory.total,utilization.gpu "
                f"--format=csv,noheader || true; df -h {shell_path(self.workdir)} | tail -1")

    def test(self) -> dict[str, Any]:
        started = time.perf_counter()
        result = self.run(self.ssh_argv(self.test_command()))
        latency = round((time.perf_counter() - started) * 1000, 1)
        ok = result.ok and result.stdout.lstrip().startswith("ok")
        error = None
        if not ok:
            error = result.tail(6) or f"ssh a échoué (code {result.returncode})"
            if result.returncode == 255:
                error = f"Connexion SSH impossible à {self.target} : {error}"
        return {"ok": ok, "latency_ms": latency if ok else None,
                "output": result.stdout.strip() or None, "error": error}
