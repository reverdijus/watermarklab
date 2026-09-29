"""Reproducibility snapshot written to ``<run>/env/`` (git, packages, hardware, command)."""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

from wmlab.tracking.io import atomic_write_json, atomic_write_text

GIT_TIMEOUT_S = 10


def git_info(workspace: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(git.json content, diff)`` from ``git -C <workspace>``, or ``(None, None)``."""
    if workspace is None or shutil.which("git") is None:
        return None, None

    def git(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(["git", "-C", str(workspace), *args], capture_output=True,
                                  text=True, timeout=GIT_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError):
            return None

    sha = git("rev-parse", "HEAD")
    if sha is None or sha.returncode != 0 or not sha.stdout.strip():
        return None, None
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    status = git("status", "--porcelain")
    remote = git("remote", "get-url", "origin")
    dirty = bool(status is not None and status.stdout.strip())
    info = {
        "sha": sha.stdout.strip(),
        "branch": branch.stdout.strip() if branch is not None and branch.returncode == 0 else None,
        "dirty": dirty,
        "remote": remote.stdout.strip() if remote is not None and remote.returncode == 0
        and remote.stdout.strip() else None,
    }
    diff_text: str | None = None
    if dirty:
        diff = git("diff", "HEAD")
        diff_text = diff.stdout if diff is not None and diff.returncode == 0 else None
    return info, diff_text


def pip_freeze_lines() -> list[str]:
    """Return ``name==version`` for every installed distribution (sorted)."""
    seen: dict[str, str] = {}
    for dist in metadata.distributions():
        try:
            name = dist.metadata["Name"]
        except Exception:
            name = None
        if name and name.lower() not in seen:
            seen[name.lower()] = f"{name}=={dist.version}"
    return [seen[k] for k in sorted(seen)]


def _ram_gb() -> float | None:
    try:
        import psutil

        return round(psutil.virtual_memory().total / 1024**3, 1)
    except Exception:
        pass
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3, 1)
    except (ValueError, OSError, AttributeError):
        return None


def _apple_chip() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True,
                             text=True, timeout=2, check=False)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def hardware_info() -> dict[str, Any]:
    """Describe the machine (torch imported lazily)."""
    info: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "torch": None,
        "cuda": None,
        "gpus": [],
        "cpu_count": os.cpu_count(),
        "ram_gb": _ram_gb(),
        "device": "cpu",
    }
    try:
        import torch
    except Exception:
        return info
    info["torch"] = torch.__version__
    info["cuda"] = torch.version.cuda
    try:
        if torch.cuda.is_available():
            info["device"] = "cuda"
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                info["gpus"].append({"name": props.name,
                                     "memory_gb": round(props.total_memory / 1024**3, 1)})
        elif torch.backends.mps.is_available():
            info["device"] = "mps"
            chip = _apple_chip() or platform.machine()
            info["gpus"].append({"name": f"{chip} (MPS)", "memory_gb": info["ram_gb"]})
    except Exception:
        pass
    return info


def command_line() -> str:
    """Return the current command line (interpreter + argv), shell-quoted."""
    return shlex.join([sys.executable, *sys.argv])


def capture_env(env_dir: Path, *, workspace: Path | None,
                launch_info: dict[str, Any] | None = None) -> list[str]:
    """Write git.json/git.diff, pip_freeze.txt, hardware.json, command.txt, launch.json.

    Returns a list of warnings (a failing item never aborts the others).
    """
    env_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    def attempt(label: str, fn: Any) -> None:
        try:
            fn()
        except Exception as exc:  # never fail a run because of the snapshot
            warnings.append(f"env/{label} : {exc}")

    def write_git() -> None:
        info, diff = git_info(workspace)
        if info is None and launch_info:
            info = launch_info.get("git") or None
            diff = launch_info.get("git_diff") or None
        if info is not None:
            atomic_write_json(env_dir / "git.json", info)
        if diff:
            atomic_write_text(env_dir / "git.diff", diff)

    attempt("git", write_git)
    attempt("pip_freeze.txt", lambda: atomic_write_text(
        env_dir / "pip_freeze.txt", "\n".join(pip_freeze_lines()) + "\n"))
    attempt("hardware.json", lambda: atomic_write_json(env_dir / "hardware.json", hardware_info()))
    attempt("command.txt", lambda: atomic_write_text(env_dir / "command.txt", command_line() + "\n"))
    if launch_info:
        attempt("launch.json", lambda: atomic_write_json(env_dir / "launch.json", launch_info))
    return warnings


def read_env(env_dir: Path) -> dict[str, Any]:
    """Read the env snapshot (``RunReader.env()`` format)."""
    def read_text(name: str) -> str | None:
        try:
            return (env_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def read_obj(name: str) -> Any:
        text = read_text(name)
        if text is None:
            return None
        try:
            return json.loads(text)
        except ValueError:
            return None

    freeze = read_text("pip_freeze.txt") or ""
    command = read_text("command.txt")
    return {
        "git": read_obj("git.json"),
        "git_diff": read_text("git.diff"),
        "hardware": read_obj("hardware.json"),
        "pip_freeze_lines": len([line for line in freeze.splitlines() if line.strip()]),
        "command": command.strip() if command is not None else None,
        "launch": read_obj("launch.json"),
    }
