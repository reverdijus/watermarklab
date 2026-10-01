"""Local executor: detached ``python -m wmlab.cli`` subprocesses on this Mac."""

from __future__ import annotations

import os
import platform
import shutil
import signal
import socket
import subprocess
import time
from typing import Any

from wmlab.server.executors.base import (
    CommandResult,
    Executor,
    JobRecord,
    LaunchResult,
    ProcessState,
    job_log_name,
)


def _process_start(pid: int) -> float | None:
    try:
        import psutil

        return float(psutil.Process(pid).create_time())
    except Exception:
        return None


def _is_zombie(pid: int) -> bool:
    try:
        import psutil

        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except Exception:
        return False


def pid_alive(pid: int, proc_start: float | None = None) -> bool:
    """True if ``pid`` exists (and is the same process when ``proc_start`` is known)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:  # reap it if it is a finished child of ours
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        pass
    if _is_zombie(pid):
        return False
    if proc_start is not None:
        current = _process_start(pid)
        if current is not None and abs(current - proc_start) > 2.0:
            return False  # pid reused by another process
    return True


def terminate_group(pid: int) -> None:
    """SIGTERM to the process group of ``pid`` (the process itself as a fallback)."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


class LocalExecutor(Executor):
    """Runs jobs as detached subprocesses (``start_new_session=True``)."""

    SECRET_VARS = ("WMLAB_TOKEN", "SCW_SECRET_KEY")

    def environment(self) -> dict[str, str]:
        """Environment of a job: the server secrets never reach the training process (which
        imports user plugins)."""
        env = {k: v for k, v in os.environ.items() if k not in self.SECRET_VARS}
        env.update({
            "WMLAB_WORKSPACE": str(self.ctx.workspace),
            "WMLAB_DATA": str(self.ctx.data),
            "WMLAB_HOST": self.host.name,
            "WMLAB_PRICE_EUR_PER_HOUR": str(self.host.price_eur_per_hour),
            "PYTHONUNBUFFERED": "1",
        })
        return env

    def argv(self, job: JobRecord) -> list[str]:
        return [*self.ctx.local_cli_prefix(), *self.cli_args(job, job.launch_file)]

    def launch(self, job: JobRecord) -> LaunchResult:
        folder, name = job_log_name(job)
        log_path = self.ctx.data / folder / name
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as out:
            proc = subprocess.Popen(self.argv(job), cwd=str(self.ctx.workspace),
                                    env=self.environment(), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        return LaunchResult(pid=proc.pid, handle=proc, log_path=str(log_path),
                            proc_start=_process_start(proc.pid))

    def poll(self, job: JobRecord, handle: Any = None) -> ProcessState:
        if handle is not None:
            code = handle.poll()
            return ProcessState(alive=code is None, exit_code=code)
        if job.pid is None:
            return ProcessState(alive=False)
        return ProcessState(alive=pid_alive(job.pid, job.proc_start))

    def cancel(self, job: JobRecord, handle: Any = None) -> None:
        if job.pid is not None and self.poll(job, handle).alive:
            terminate_group(job.pid)

    def setup(self, job: JobRecord) -> CommandResult:
        return CommandResult(0, "Rien à installer : le Mac utilise le workspace local.", "")

    def test(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            from wmlab.tracking.env import hardware_info

            info = hardware_info()
        except Exception as exc:
            return {"ok": False, "latency_ms": None, "output": None,
                    "error": f"informations machine indisponibles : {exc}"}
        gpus = ", ".join(g.get("name", "?") for g in info.get("gpus") or []) or "aucun"
        try:
            free_gb = shutil.disk_usage(self.ctx.data).free / 1024**3
            disk = f"{free_gb:.1f} Go libres ({self.ctx.data})"
        except OSError:
            disk = "inconnu"
        lines = [
            f"Machine : {info.get('hostname') or socket.gethostname()} ({platform.machine()})",
            f"Système : {info.get('platform')}",
            f"Python {info.get('python')} · torch {info.get('torch') or 'non installé'} · "
            f"device {info.get('device')}",
            f"GPU : {gpus}",
            f"CPU : {info.get('cpu_count')} cœurs · RAM : {info.get('ram_gb')} Go",
            f"Disque : {disk}",
        ]
        return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "output": "\n".join(lines), "error": None}
