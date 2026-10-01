"""Git operations of the hosted mode (ARCHITECTURE.md §12.3).

``GitRepo`` wraps ``git -C <workspace>``: state (``GET /api/git``), fast-forward pulls,
commit + push of files saved from the app, resolution of a requested revision into a commit
sha and the export of that commit (``git archive``) into ``<data>/code/<sha>`` (cache of the
10 most recent exports). ``GitSync`` is the periodic pull thread. Every command is an
argument list (no shell) with a timeout; git's own errors are returned as messages, never
raised through the API except as ``GitError``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from wmlab.ids import utc_iso

log = logging.getLogger("wmlab.server")

GIT_TIMEOUT_S = 60.0
FETCH_TIMEOUT_S = 300.0
EXPORT_KEEP = 10
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
CODE_DIR = "code"
SETTINGS_FILE = "workspace.yaml"   # tracked but rewritten by PUT /api/settings: never "dirty"


class GitError(Exception):
    """A git operation refused or failed (French message)."""


def valid_ref(ref: str) -> bool:
    """A revision the user may pass to ``git rev-parse``: branch, tag, ``origin/x`` or sha
    (letters, digits, ``.``, ``_``, ``/``, ``-``; never an option, never ``..``)."""
    return bool(ref) and bool(REF_RE.match(ref)) and ".." not in ref


def lock_digest(code_dir: Path) -> str | None:
    """SHA-256 of ``uv.lock`` in an export (``None`` when the file is absent)."""
    path = Path(code_dir) / "uv.lock"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class GitRepo:
    """``git`` commands of the workspace checkout (``available`` is False without a repo)."""

    def __init__(self, workspace: Path, data: Path, *, timeout: float = GIT_TIMEOUT_S) -> None:
        self.workspace = Path(workspace)
        self.data = Path(data)
        self.code_root = self.data / CODE_DIR
        self.timeout = timeout
        self.last_pull_at: str | None = None
        self.last_pull_error: str | None = None
        self._lock = threading.RLock()
        self._available: bool | None = None

    # -- plumbing --------------------------------------------------------------------------
    def run(self, *args: str, timeout: float | None = None, binary: bool = False
            ) -> subprocess.CompletedProcess:
        argv = ["git", "-C", str(self.workspace), *args]
        try:
            return subprocess.run(argv, capture_output=True, text=not binary,
                                  timeout=timeout or self.timeout, check=False,
                                  stdin=subprocess.DEVNULL,
                                  env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                       "LC_ALL": "C"})
        except FileNotFoundError:
            return subprocess.CompletedProcess(argv, 127, "" if not binary else b"",
                                               "git introuvable" if not binary else b"")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "" if not binary else b"",
                                               f"délai dépassé ({timeout or self.timeout:.0f} s)"
                                               if not binary else b"")
        except OSError as exc:
            return subprocess.CompletedProcess(argv, 126, "" if not binary else b"",
                                               str(exc) if not binary else b"")

    def out(self, *args: str, timeout: float | None = None) -> str | None:
        """stdout of a successful command (stripped), else ``None``."""
        done = self.run(*args, timeout=timeout)
        if done.returncode != 0:
            return None
        return str(done.stdout).strip()

    @staticmethod
    def _error(done: subprocess.CompletedProcess) -> str:
        text = "\n".join(t for t in (str(done.stderr).strip(), str(done.stdout).strip()) if t)
        lines = [line for line in text.splitlines() if line.strip()]
        return "\n".join(lines[-4:]) or f"code {done.returncode}"

    @property
    def available(self) -> bool:
        if self._available is None:
            self._available = shutil.which("git") is not None and \
                self.out("rev-parse", "--is-inside-work-tree") == "true"
        return self._available

    # -- state -----------------------------------------------------------------------------
    def head(self) -> str | None:
        return self.out("rev-parse", "HEAD") if self.available else None

    def branch(self) -> str | None:
        name = self.out("rev-parse", "--abbrev-ref", "HEAD") if self.available else None
        return None if name in (None, "", "HEAD") else name

    def dirty(self) -> bool:
        """Uncommitted changes, ``workspace.yaml`` excluded: the app rewrites that file
        (``PUT /api/settings``) and it must never block the automatic pull."""
        status = self.out("status", "--porcelain", "--", ".", f":!{SETTINGS_FILE}")
        return bool(status)

    def remote(self) -> str | None:
        return self.out("remote", "get-url", "origin") or None

    def ahead_behind(self, branch: str | None) -> tuple[int | None, int | None]:
        if not branch or not valid_ref(branch):
            return None, None
        counts = self.out("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}")
        if not counts:
            return None, None
        try:
            ahead, behind = (int(v) for v in counts.split())
        except ValueError:
            return None, None
        return ahead, behind

    def state(self, hosted: bool) -> dict[str, Any]:
        """``GitState`` of §12.3."""
        if not self.available:
            return {"available": False, "hosted": hosted, "branch": None, "sha": None,
                    "short_sha": None, "dirty": False, "remote": None, "ahead": None,
                    "behind": None, "last_pull_at": self.last_pull_at,
                    "last_pull_error": self.last_pull_error}
        sha = self.head()
        branch = self.branch()
        ahead, behind = self.ahead_behind(branch)
        return {
            "available": True, "hosted": hosted, "branch": branch, "sha": sha,
            "short_sha": sha[:7] if sha else None, "dirty": self.dirty(),
            "remote": self.remote(), "ahead": ahead, "behind": behind,
            "last_pull_at": self.last_pull_at, "last_pull_error": self.last_pull_error,
        }

    # -- pull ------------------------------------------------------------------------------
    def pull(self, branch: str) -> str | None:
        """``git fetch origin <branch>`` + ``git merge --ff-only``; returns the error text
        (``None`` on success). Never merges into a dirty tree or another branch."""
        with self._lock:
            error = self._pull(branch)
            self.last_pull_error = error
            if error is None:
                self.last_pull_at = utc_iso()
            return error

    def _pull(self, branch: str) -> str | None:
        if not self.available:
            return "aucun dépôt git dans le workspace"
        if not valid_ref(branch):
            return f"nom de branche invalide : {branch!r}"
        if self.dirty():
            return ("arbre de travail modifié (fichiers non commités) : le pull automatique "
                    "est suspendu")
        current = self.branch()
        if current != branch:
            return (f"le serveur est sur la branche « {current or 'détachée'} », pas sur "
                    f"« {branch} » : pull ignoré")
        fetched = self.run("fetch", "--quiet", "origin", branch, timeout=FETCH_TIMEOUT_S)
        if fetched.returncode != 0:
            return f"git fetch origin {branch} : {self._error(fetched)}"
        merged = self.run("merge", "--ff-only", "--quiet", f"origin/{branch}")
        if merged.returncode != 0:
            return f"git merge --ff-only origin/{branch} : {self._error(merged)}"
        return None

    # -- commit & push ---------------------------------------------------------------------
    def commit_and_push(self, rel_path: str, user: str, branch: str) -> dict[str, Any]:
        """``git add`` + ``commit -m "app(<user>): <path>"`` + ``push origin <branch>``.

        A failed push leaves the local commit (``pushed: false`` + ``error``); a rejected
        push is retried once after ``git pull --rebase``.
        """
        result: dict[str, Any] = {"committed": None, "pushed": False, "error": None}
        user = re.sub(r"[<>\s]+", "-", user.strip()) or "app"
        identity = ["-c", f"user.name={user}", "-c", f"user.email={user}@wmlab.local"]
        with self._lock:
            added = self.run("add", "--", rel_path)
            if added.returncode != 0:
                result["error"] = f"git add : {self._error(added)}"
                return result
            staged = self.run("diff", "--cached", "--quiet", "--", rel_path)
            if staged.returncode == 0:
                return result  # nothing changed: no commit, nothing to push
            committed = self.run(*identity, "commit", "--quiet", "-m",
                                 f"app({user}): {rel_path}",
                                 f"--author={user} <{user}@wmlab.local>", "--", rel_path)
            if committed.returncode != 0:
                result["error"] = f"git commit : {self._error(committed)}"
                return result
            result["committed"] = self.head()
            if not valid_ref(branch):
                result["error"] = f"nom de branche invalide : {branch!r}"
                return result
            pushed = self.run("push", "--quiet", "origin", f"HEAD:{branch}",
                              timeout=FETCH_TIMEOUT_S)
            if pushed.returncode == 0:
                result["pushed"] = True
                return result
            first = self._error(pushed)
            rebased = self.run(*identity, "pull", "--rebase", "--quiet", "origin", branch,
                               timeout=FETCH_TIMEOUT_S)
            if rebased.returncode != 0:
                self.run("rebase", "--abort")
                result["error"] = (f"git push : {first} — puis git pull --rebase : "
                                   f"{self._error(rebased)}")
                return result
            result["committed"] = self.head()
            pushed = self.run("push", "--quiet", "origin", f"HEAD:{branch}",
                              timeout=FETCH_TIMEOUT_S)
            if pushed.returncode == 0:
                result["pushed"] = True
            else:
                result["error"] = f"git push (2e tentative) : {self._error(pushed)}"
            return result

    # -- revisions -------------------------------------------------------------------------
    def resolve(self, ref: str) -> str | None:
        """Commit sha of ``ref`` (branch, tag, sha, ``origin/x``…), fetching from origin
        when it is unknown locally; ``None`` when it cannot be resolved."""
        if not self.available or not valid_ref(ref):
            return None
        sha = self._rev_parse(ref)
        if sha is None and self.remote():
            self.run("fetch", "--quiet", "--prune", "origin", timeout=FETCH_TIMEOUT_S)
            sha = self._rev_parse(ref)
        return sha

    def _rev_parse(self, ref: str) -> str | None:
        for candidate in (ref, f"origin/{ref}"):
            sha = self.out("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}")
            if sha and SHA_RE.match(sha):
                return sha
        return None

    # -- exports ---------------------------------------------------------------------------
    def export_dir(self, sha: str) -> Path:
        return self.code_root / sha

    def export(self, sha: str) -> Path:
        """``git archive <sha>`` extracted into ``<data>/code/<sha>`` (cached, LRU of 10)."""
        if not SHA_RE.match(sha or ""):
            raise GitError(f"sha invalide : {sha!r}")
        target = self.export_dir(sha)
        with self._lock:
            if (target / ".wmlab-export").is_file():
                os.utime(target, None)  # most recently used
                return target
            self.code_root.mkdir(parents=True, exist_ok=True)
            archive = self.run("archive", "--format=tar", sha, binary=True,
                               timeout=FETCH_TIMEOUT_S)
            if archive.returncode != 0:
                stderr = archive.stderr.decode("utf-8", "replace") if isinstance(
                    archive.stderr, bytes) else str(archive.stderr)
                raise GitError(f"git archive {sha[:7]} : {stderr.strip()[-300:] or 'échec'}")
            tmp = Path(tempfile.mkdtemp(prefix=f".export-{sha[:7]}-", dir=self.code_root))
            try:
                with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
                    tar.extractall(tmp, filter="data")
                (tmp / ".wmlab-export").write_text(f"{sha}\n{utc_iso()}\n", encoding="utf-8")
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                os.replace(tmp, target)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
            self._rotate(keep=EXPORT_KEEP, protect={target})
            return target

    def _rotate(self, keep: int, protect: set[Path]) -> None:
        exports = []
        for entry in self.code_root.iterdir():
            if entry.is_dir() and SHA_RE.match(entry.name):
                try:
                    exports.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
        exports.sort(reverse=True)
        for _, folder in exports[keep:]:
            if folder in protect:
                continue
            shutil.rmtree(folder, ignore_errors=True)
            log.info("export git supprimé (rotation) : %s", folder.name)


class GitSync:
    """Periodic ``git pull --ff-only`` of the hosted server (§12.3)."""

    def __init__(self, repo: GitRepo, settings: Callable[[], Any]) -> None:
        self.repo = repo
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def once(self) -> None:
        """One iteration (pull when hosted mode is on and auto-pull enabled)."""
        try:
            hosted = self.settings().hosted
        except Exception:
            return
        if not hosted.enabled or hosted.auto_pull_s <= 0 or not self.repo.available:
            return
        error = self.repo.pull(hosted.git_branch)
        if error:
            log.warning("pull automatique : %s", error)

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.once()
                except Exception:
                    log.exception("échec du pull automatique")
                try:
                    interval = float(self.settings().hosted.auto_pull_s) or 60.0
                except Exception:
                    interval = 60.0
                self._stop.wait(max(5.0, interval))

        self._thread = threading.Thread(target=loop, name="wmlab-gitsync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None


__all__ = ["CODE_DIR", "EXPORT_KEEP", "GitError", "GitRepo", "GitSync", "lock_digest",
           "valid_ref"]
