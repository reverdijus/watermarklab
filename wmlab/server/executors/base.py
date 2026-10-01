"""Job model and executor interface (local subprocess, SSH/Scaleway)."""

from __future__ import annotations

import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Protocol

from wmlab.spec import HostSpec

JOB_KINDS = ("train", "hpo", "eval", "setup", "retrieve")
UNLIMITED_KINDS = ("setup", "retrieve")   # not counted in ``max_concurrent_jobs``
ACTIVE_STATUSES = ("queued", "launching", "running")
FINAL_STATUSES = ("completed", "failed", "killed", "lost")
API_FIELDS = ("job_id", "kind", "host", "target", "overrides", "status", "run_id", "study_id",
              "created_at", "started_at", "ended_at", "max_hours", "smoke", "error", "pid",
              "last_sync_at", "user", "git_ref", "git_sha", "retrieval", "result")


@dataclass
class JobRecord:
    """A job launched by the app, persisted in ``<data>/jobs/<job_id>.json``."""

    job_id: str
    kind: str
    host: str
    target: str
    overrides: dict[str, str] = field(default_factory=dict)
    status: str = "queued"
    run_id: str | None = None
    study_id: str | None = None
    created_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    max_hours: float | None = None
    smoke: bool = False
    error: str | None = None
    pid: int | None = None
    last_sync_at: str | None = None
    user: str | None = None                             # who launched it (§12.2)
    git_ref: str | None = None                          # requested revision (§12.3)
    git_sha: str | None = None                          # resolved commit
    retrieval: dict[str, Any] | None = None             # end-of-job checkpoint sync (§12.4)
    result: dict[str, Any] | None = None                # ``retrieve`` jobs
    # -- internal fields (not part of the API) --
    code_dir: str | None = None                         # local export of ``git_sha`` (ssh)
    lock_sha256: str | None = None                      # sha256 of the exported uv.lock
    spec_path: str | None = None
    checkpoint: str | None = None
    benchmark: str | None = None
    init_from: dict[str, Any] | None = None
    args: list[str] = field(default_factory=list)      # CLI arguments after "-m wmlab.cli"
    launch_file: str | None = None                      # local launch file
    log_path: str | None = None                         # local log (local jobs)
    remote_log: str | None = None                       # remote log (ssh jobs)
    exit_code: int | None = None
    cancel_requested: bool = False
    proc_start: float | None = None                     # process create time (pid reuse guard)
    remote_start: str | None = None                     # ``ps -o lstart=`` of a remote process
    started_epoch: float | None = None
    last_poll: float | None = None

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def to_api(self) -> dict[str, Any]:
        """The ``Job`` shape of §9."""
        return {name: getattr(self, name) for name in API_FIELDS}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class CommandResult:
    """Outcome of a command run through a :data:`Runner`."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, n: int = 20) -> str:
        text = "\n".join(t for t in (self.stderr.strip(), self.stdout.strip()) if t)
        return "\n".join(text.splitlines()[-n:])


class Runner(Protocol):
    """Runs a command given as an argument list (never through a local shell)."""

    def __call__(self, argv: list[str], *, timeout: float | None = None) -> CommandResult: ...


def default_runner(argv: list[str], *, timeout: float | None = None) -> CommandResult:
    """Run ``argv`` with :func:`subprocess.run` (captured output, no shell)."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    except FileNotFoundError:
        return CommandResult(127, "", f"commande introuvable : {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"délai dépassé ({timeout:.0f} s) : {' '.join(argv[:3])} …")
    except OSError as exc:
        return CommandResult(126, "", f"exécution impossible : {exc}")
    return CommandResult(done.returncode, done.stdout or "", done.stderr or "")


@dataclass
class LaunchResult:
    """Process started by an executor."""

    pid: int
    handle: Any = None              # subprocess.Popen for local jobs
    log_path: str | None = None
    remote_log: str | None = None
    proc_start: float | None = None
    remote_start: str | None = None  # ``ps -o lstart=`` line of a remote process


@dataclass
class ProcessState:
    """Liveness of a job process (``alive=None``: unknown, e.g. host unreachable)."""

    alive: bool | None
    exit_code: int | None = None
    detail: str | None = None


@dataclass
class ExecContext:
    """What executors need from the server."""

    workspace: Path
    data: Path
    runner: Runner = default_runner
    cli_prefix: list[str] | None = None          # local: [python, "-m", "wmlab.cli"]
    remote_cli_prefix: list[str] | None = None   # ssh: [python, "-m", "wmlab.cli"]

    def local_cli_prefix(self) -> list[str]:
        if self.cli_prefix:
            return list(self.cli_prefix)
        venv_python = self.workspace / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else sys.executable
        return [python, "-m", "wmlab.cli"]


def job_log_name(job: JobRecord) -> tuple[str, str]:
    """``(folder relative to the data root, file name)`` of the process output of a job."""
    if job.kind == "train" and job.run_id:
        return f"runs/{job.run_id}", "stdout.log"
    if job.kind == "hpo" and job.study_id:
        return f"studies/{job.study_id}", "stdout.log"
    return "jobs/logs", f"{job.job_id}.log"


class Executor(ABC):
    """Starts, watches and stops the processes of one host."""

    def __init__(self, host: HostSpec, ctx: ExecContext) -> None:
        self.host = host
        self.ctx = ctx

    @property
    def runner(self) -> Callable[..., CommandResult]:
        return self.ctx.runner

    @abstractmethod
    def launch(self, job: JobRecord) -> LaunchResult:
        """Start the job process (detached) and return its pid."""

    @abstractmethod
    def poll(self, job: JobRecord, handle: Any = None) -> ProcessState:
        """Tell whether the job process is still alive."""

    @abstractmethod
    def cancel(self, job: JobRecord, handle: Any = None) -> None:
        """Send SIGTERM to the process group of the job."""

    def sync(self, job: JobRecord) -> None:
        """Bring the job's run/study folders back to the local data root (no-op locally)."""

    def fetch_checkpoint(self, run_id: str, file: str) -> None:
        """Copy a checkpoint file of a run to the local data root (no-op locally)."""

    def retrieve(self, job: JobRecord) -> dict[str, Any] | None:
        """Full sync (``*.pt`` included) of the job's folders + size check (§12.4).

        ``None`` for hosts where nothing has to be brought back (local)."""
        return None

    @abstractmethod
    def setup(self, job: JobRecord) -> CommandResult:
        """Prepare the host (code sync + bootstrap script)."""

    @abstractmethod
    def test(self) -> dict[str, Any]:
        """``{ok, latency_ms, output, error}``."""

    def cli_args(self, job: JobRecord, launch_file: str | None) -> list[str]:
        """CLI arguments of a job, with ``--launch-file`` for train/hpo."""
        args = list(job.args)
        if job.kind in ("train", "hpo") and launch_file:
            args += ["--launch-file", launch_file]
        return args
