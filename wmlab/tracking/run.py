"""Run writer: the self-contained run folder (§6) written by the training process (§7)."""

from __future__ import annotations

import atexit
import io
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, TextIO

import yaml

from wmlab.ids import new_run_id, utc_iso
from wmlab.tracking.checkpoints import (
    CHECKPOINT_FORMAT,
    checkpoint_name,
    index_path,
    load_index,
    retag,
    select_retained,
)
from wmlab.tracking.env import capture_env as _capture_env
from wmlab.tracking.io import (
    atomic_write_json,
    atomic_write_text,
    json_line,
    read_json,
    sanitize,
    to_float,
)
from wmlab.tracking.params import flatten_params
from wmlab.tracking.samples import safe_name, write_sample
from wmlab.tracking.system import SystemSampler

FORMAT_VERSION = 1
FINAL_STATUSES = ("completed", "failed", "killed")
MAX_ERROR_CHARS = 20000


class RunTerminated(KeyboardInterrupt):
    """Raised in the main thread when the process receives SIGTERM."""


class _Tee(io.TextIOBase):
    """Duplicate writes to the original stream and to ``stdout.log``."""

    def __init__(self, original: TextIO, log: TextIO, lock: threading.Lock) -> None:
        self._original = original
        self._log = log
        self._lock = lock

    def write(self, text: str) -> int:
        try:
            self._original.write(text)
        except Exception:
            pass
        with self._lock:
            try:
                self._log.write(text)
                if "\n" in text:
                    self._log.flush()
            except Exception:
                pass
        return len(text)

    def flush(self) -> None:
        for stream in (self._original, self._log):
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return bool(getattr(self._original, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._original.fileno()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._original, "encoding", "utf-8")


def default_notes(spec: dict[str, Any], run_id: str) -> str:
    """Pre-filled ``notes.md`` (title, description, hypothesis, observations, conclusion)."""
    name = spec.get("name") or run_id
    description = (spec.get("description") or "").strip()
    hypothesis = (spec.get("hypothesis") or "").strip()
    lines = [f"# {name} — {run_id}", ""]
    if description:
        lines += [description, ""]
    lines += ["## Hypothèse", "", hypothesis or "_(à compléter)_", "",
              "## Observations", "", "", "## Conclusion", "", ""]
    return "\n".join(lines)


class Run:
    """Writer of one run folder. Create with :meth:`Run.create`."""

    HEARTBEAT_INTERVAL_S = 20.0
    SYSTEM_INTERVAL_S = 15.0
    MANIFEST_MIN_INTERVAL_S = 10.0

    def __init__(self, run_dir: Path, manifest: dict[str, Any]) -> None:
        self.dir = Path(run_dir)
        self.run_id: str = manifest["run_id"]
        self.manifest = manifest
        self._lock = threading.RLock()
        self._io_lock = threading.Lock()
        self._files: dict[str, TextIO] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._finished = False
        self._terminated = False
        self._dirty = False
        self._last_manifest_write = 0.0
        self._keep_last = 2
        self._keep_best: dict[str, str] = {}
        self._keep_phase_end = True
        self._prev_sigterm: Any = None
        self._sigterm_installed = False
        self._tee: tuple[_Tee, _Tee, TextIO, TextIO, TextIO] | None = None

    # -- creation ------------------------------------------------------------------------
    @classmethod
    def create(cls, *, project: str, experiment: str, modality: str, spec: dict[str, Any],
               spec_path: str | None = None, run_id: str | None = None,
               runs_dir: Path | None = None, kind: str = "train", study: str | None = None,
               trial: int | None = None, parent: dict[str, Any] | None = None,
               params: dict[str, Any] | None = None, overrides: dict[str, Any] | None = None,
               tags: list[str] | None = None, gpu_share: float = 1.0,
               launch_info: dict[str, Any] | None = None, capture_env: bool = True,
               monitor_system: bool = True) -> "Run":
        """Create the run folder, write the manifest/spec/notes/env and start the threads."""
        from wmlab import paths

        base = Path(runs_dir) if runs_dir is not None else paths.runs_dir()
        run_id = run_id or new_run_id(experiment)
        run_dir = base / run_id
        previous = read_json(run_dir / "run.json")
        if isinstance(previous, dict) and previous.get("status") not in (None, "queued"):
            raise FileExistsError(
                f"le run {run_id} existe déjà (statut {previous.get('status')}) : "
                "choisissez un autre run_id"
            )
        run_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        created_at = previous.get("created_at") if isinstance(previous, dict) else None
        overrides_str = {str(k): str(v) for k, v in (overrides or {}).items()}
        manifest: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "kind": kind,
            "run_id": run_id,
            "project": project,
            "experiment": experiment,
            "modality": modality,
            "spec_path": spec_path,
            "study": study,
            "trial": trial,
            "parent": parent,
            "status": "running",
            "created_at": created_at or utc_iso(now),
            "started_at": utc_iso(now),
            "ended_at": None,
            "host": paths.current_host(),
            "price_eur_per_hour": paths.current_price(),
            "gpu_share": float(gpu_share),
            "tags": [str(t) for t in (tags if tags is not None else spec.get("tags") or [])],
            "params": sanitize(params if params is not None
                               else flatten_params(spec, overrides_str)),
            "overrides": overrides_str,
            "datasets": {},
            "progress": None,
            "summary": {},
            "error": None,
        }
        run = cls(run_dir, manifest)
        run._write_manifest(force=True)
        run._write_heartbeat(now)
        atomic_write_text(run_dir / "spec.yaml",
                          yaml.safe_dump(sanitize(spec), sort_keys=False, allow_unicode=True))
        if not (run_dir / "notes.md").exists():
            atomic_write_text(run_dir / "notes.md", default_notes(spec, run_id))
        run.event("status", status="running")
        if capture_env:
            try:
                workspace = paths.workspace_root()
            except Exception:
                workspace = None
            for warning in _capture_env(run_dir / "env", workspace=workspace,
                                        launch_info=launch_info):
                run.event("warning", message=warning)
        elif launch_info:
            atomic_write_json(run_dir / "env" / "launch.json", launch_info)
        run._start_threads(monitor_system)
        run._install_sigterm()
        run._install_tee()
        atexit.register(run._atexit)
        return run

    # -- files ---------------------------------------------------------------------------
    def _append(self, name: str, row: dict[str, Any]) -> None:
        line = json_line(row) + "\n"
        with self._io_lock:
            handle = self._files.get(name)
            if handle is None or handle.closed:
                handle = open(self.dir / name, "a", encoding="utf-8")
                self._files[name] = handle
            handle.write(line)
            handle.flush()

    def _write_manifest(self, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if not force and now - self._last_manifest_write < self.MANIFEST_MIN_INTERVAL_S:
                self._dirty = True
                return
            atomic_write_json(self.dir / "run.json", self.manifest)
            self._last_manifest_write = now
            self._dirty = False

    def _write_heartbeat(self, now: float | None = None) -> None:
        try:
            atomic_write_text(self.dir / "heartbeat", f"{time.time() if now is None else now:.3f}")
        except OSError:
            pass

    # -- logging API ---------------------------------------------------------------------
    def log_metrics(self, step: int, values: dict[str, Any], phase: str | None = None) -> None:
        """Append one ``metrics.jsonl`` row; update the summary for ``val/*`` keys."""
        clean = {str(k): to_float(v) for k, v in values.items()}
        self._append("metrics.jsonl", {"t": time.time(), "step": int(step), "phase": phase,
                                       "values": clean})
        changed = False
        with self._lock:
            summary = self.manifest.setdefault("summary", {})
            for key, value in clean.items():
                if value is None:
                    continue
                if key.startswith("val/"):
                    summary[key] = value
                    summary["step"] = int(step)
                    changed = True
                direction = self._keep_best.get(key)
                if direction is not None:
                    best_key = f"best/{key}"
                    previous = summary.get(best_key)
                    better = previous is None or (value > previous if direction == "max"
                                                  else value < previous)
                    if better:
                        summary[best_key] = value
                        changed = True
        if changed:
            self._write_manifest()

    def log_system(self, values: dict[str, Any]) -> None:
        """Append one ``system.jsonl`` row."""
        row: dict[str, Any] = {"t": time.time()}
        row.update({str(k): to_float(v) for k, v in values.items()})
        self._append("system.jsonl", row)

    def event(self, type: str, **data: Any) -> None:
        """Append one ``events.jsonl`` row."""
        self._append("events.jsonl", {"t": time.time(), "type": type, **data})

    def set_progress(self, **progress: Any) -> None:
        """Merge progress fields (manifest written at most every 10 s, or on phase change)."""
        with self._lock:
            current = dict(self.manifest.get("progress") or {})
            phase_changed = "phase" in progress and progress["phase"] != current.get("phase")
            current.update(progress)
            self.manifest["progress"] = current
        self._write_manifest(force=phase_changed)

    def update_summary(self, values: dict[str, Any]) -> None:
        """Merge values into ``summary`` and write the manifest."""
        with self._lock:
            summary = self.manifest.setdefault("summary", {})
            for key, value in values.items():
                number = to_float(value)
                summary[str(key)] = number if number is not None else sanitize(value)
        self._write_manifest(force=True)

    def set_datasets(self, versions: dict[str, str]) -> None:
        """Record dataset versions (``run.json.datasets``)."""
        with self._lock:
            self.manifest["datasets"] = {str(k): str(v) for k, v in versions.items()}
        self._write_manifest(force=True)

    def set_tags(self, tags: list[str]) -> None:
        """Replace the run tags."""
        with self._lock:
            self.manifest["tags"] = [str(t) for t in tags]
        self._write_manifest(force=True)

    # -- checkpoints ---------------------------------------------------------------------
    def set_checkpoint_policy(self, keep_last: int, keep_best: dict[str, str],
                              keep_phase_end: bool = True) -> None:
        """Set the retention policy used by :meth:`save_checkpoint`."""
        self._keep_last = int(keep_last)
        self._keep_best = {str(k): ("min" if str(v) in ("min", "minimize") else "max")
                           for k, v in (keep_best or {}).items()}
        self._keep_phase_end = bool(keep_phase_end)

    def save_checkpoint(self, state: dict[str, Any], *, step: int, phase: str,
                        metrics: dict[str, Any] | None = None,
                        tags: list[str] | None = None) -> str:
        """Save ``state`` with ``torch.save``, update index/tags and apply retention."""
        import torch

        name = checkpoint_name(step)
        ckpt_dir = self.dir / "checkpoints"
        ckpt_dir.mkdir(parents=True, exist_ok=True)
        path = ckpt_dir / f"{name}.pt"
        payload = dict(state)
        payload.setdefault("format", CHECKPOINT_FORMAT)
        payload.setdefault("step", int(step))
        payload.setdefault("phase", phase)
        payload.setdefault("metrics", dict(metrics or {}))
        tmp = ckpt_dir / f".{name}.pt.tmp"
        torch.save(payload, tmp)
        os.replace(tmp, path)
        clean_metrics = {str(k): v for k, v in
                         ((k, to_float(v)) for k, v in (metrics or {}).items()) if v is not None}
        with self._lock:
            index = load_index(self.dir)
            entry = next((e for e in index if e.get("name") == name), None)
            if entry is None:
                entry = {"name": name, "file": path.name, "step": int(step), "phase": phase,
                         "metrics": {}, "tags": [], "created_at": None, "size_bytes": 0}
                index.append(entry)
            entry["phase"] = phase
            entry["metrics"] = {**(entry.get("metrics") or {}), **clean_metrics}
            entry["tags"] = list(dict.fromkeys([*(entry.get("tags") or []), *(tags or [])]))
            entry["created_at"] = utc_iso()
            entry["size_bytes"] = path.stat().st_size
            index.sort(key=lambda e: int(e.get("step", 0)))
            retag(index, self._keep_best)
            kept, deleted = select_retained(index, self._keep_last, self._keep_phase_end)
            for old in deleted:
                (ckpt_dir / str(old.get("file") or f"{old.get('name')}.pt")).unlink(missing_ok=True)
            atomic_write_json(index_path(self.dir), kept)
        self.event("checkpoint", name=name, step=int(step), phase=phase, tags=entry["tags"],
                   size_bytes=entry["size_bytes"])
        for old in deleted:
            self.event("checkpoint_deleted", name=old.get("name"), step=old.get("step"))
        return name

    # -- samples & evals -----------------------------------------------------------------
    def log_sample(self, step: int, kind: str, name: str, data: Any, *, group: str,
                   sample_rate: int | None = None, fps: int | None = None) -> str:
        """Write ``samples/<step>/<name>_<group>.<ext>``; return its run-relative path."""
        target = self.dir / "samples" / str(int(step)) / f"{safe_name(name)}_{safe_name(group)}"
        written = write_sample(target, kind, data, sample_rate=sample_rate, fps=fps)
        rel = written.relative_to(self.dir).as_posix()
        row = {"step": int(step), "kind": kind, "name": str(name), "path": rel, "group": group}
        self._append("samples/index.jsonl", row)
        self.event("sample", **row)
        return rel

    def log_eval(self, result: dict[str, Any]) -> Path:
        """Write ``eval/<checkpoint>__<benchmark>.json`` and an ``eval`` event."""
        result = dict(result)
        result.setdefault("run_id", self.run_id)
        result.setdefault("created_at", utc_iso())
        checkpoint = safe_name(result.get("checkpoint", "unknown"))
        benchmark = safe_name(result.get("benchmark", "unknown"))
        path = self.dir / "eval" / f"{checkpoint}__{benchmark}.json"
        atomic_write_json(path, result)
        self.event("eval", checkpoint=result.get("checkpoint"), benchmark=result.get("benchmark"),
                   summary=result.get("summary") or {}, path=path.relative_to(self.dir).as_posix())
        return path

    # -- lifecycle -----------------------------------------------------------------------
    @property
    def finished(self) -> bool:
        return self._finished

    def finish(self, status: str = "completed", error: str | None = None) -> None:
        """Mark the run as ended (idempotent), stop threads and flush everything."""
        if status not in FINAL_STATUSES:
            raise ValueError(f"statut final invalide '{status}' — valeurs possibles : "
                             f"{', '.join(FINAL_STATUSES)}")
        with self._lock:
            if self._finished:
                return
            self._finished = True
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.0)
        now = time.time()
        if error is not None and len(error) > MAX_ERROR_CHARS:
            error = error[:MAX_ERROR_CHARS] + "\n… (tronqué)"
        with self._lock:
            self.manifest.update(status=status, ended_at=utc_iso(now), error=error)
        self._write_heartbeat(now)
        self.event("status", status=status, **({"error": error[:2000]} if error else {}))
        self._write_manifest(force=True)
        self._restore_sigterm()
        self._uninstall_tee()
        with self._io_lock:
            for handle in self._files.values():
                try:
                    handle.close()
                except Exception:
                    pass
            self._files.clear()
        try:
            atexit.unregister(self._atexit)
        except Exception:
            pass

    def __enter__(self) -> "Run":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._finished:
            return False
        if exc_type is None:
            self.finish("completed")
        elif issubclass(exc_type, KeyboardInterrupt):
            reason = "SIGTERM reçu" if self._terminated or issubclass(exc_type, RunTerminated) \
                else "interrompu (KeyboardInterrupt)"
            self.finish("killed", reason)
        elif issubclass(exc_type, SystemExit) and getattr(exc, "code", 1) in (0, None):
            self.finish("completed")
        else:
            self.finish("failed", "".join(traceback.format_exception(exc_type, exc, tb)))
        return False

    def _atexit(self) -> None:
        if not self._finished:
            self.finish("killed" if self._terminated else "failed",
                        "SIGTERM reçu" if self._terminated
                        else "le process s'est terminé sans appeler Run.finish()")

    # -- threads -------------------------------------------------------------------------
    def _start_threads(self, monitor_system: bool) -> None:
        heartbeat = threading.Thread(target=self._heartbeat_loop, name=f"wmlab-heartbeat-{self.run_id}",
                                     daemon=True)
        self._threads.append(heartbeat)
        if monitor_system:
            self._threads.append(threading.Thread(target=self._system_loop,
                                                  name=f"wmlab-system-{self.run_id}", daemon=True))
        for thread in self._threads:
            thread.start()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.HEARTBEAT_INTERVAL_S):
            self._write_heartbeat()
            if self._dirty:
                try:
                    self._write_manifest(force=True)
                except Exception:
                    pass

    def _system_loop(self) -> None:
        try:
            sampler = SystemSampler()
        except Exception:
            return
        while not self._stop.wait(self.SYSTEM_INTERVAL_S):
            try:
                values = sampler.sample()
                if values and not self._stop.is_set():
                    self.log_system(values)
            except Exception:
                pass

    # -- signals and stdout ----------------------------------------------------------------
    def _install_sigterm(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            self._prev_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._on_sigterm)
            self._sigterm_installed = True
        except (ValueError, OSError):
            self._sigterm_installed = False

    def _on_sigterm(self, signum: int, frame: Any) -> None:
        self._terminated = True
        if not self._finished:
            raise RunTerminated("SIGTERM reçu")

    def _restore_sigterm(self) -> None:
        if not self._sigterm_installed or threading.current_thread() is not threading.main_thread():
            return
        try:
            if signal.getsignal(signal.SIGTERM) == self._on_sigterm:
                signal.signal(signal.SIGTERM, self._prev_sigterm or signal.SIG_DFL)
        except (ValueError, OSError, TypeError):
            pass
        self._sigterm_installed = False

    def _install_tee(self) -> None:
        log_path = self.dir / "stdout.log"
        try:
            if log_path.exists() and os.path.samestat(os.fstat(sys.stdout.fileno()),
                                                      os.stat(log_path)):
                return  # the launcher already redirects stdout to stdout.log
        except (OSError, ValueError, AttributeError, io.UnsupportedOperation):
            pass
        try:
            log = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            return
        lock = threading.Lock()
        out, err = sys.stdout, sys.stderr
        tee_out, tee_err = _Tee(out, log, lock), _Tee(err, log, lock)
        sys.stdout, sys.stderr = tee_out, tee_err
        self._tee = (tee_out, tee_err, out, err, log)

    def _uninstall_tee(self) -> None:
        if self._tee is None:
            return
        tee_out, tee_err, out, err, log = self._tee
        if sys.stdout is tee_out:
            sys.stdout = out
        if sys.stderr is tee_err:
            sys.stderr = err
        try:
            log.flush()
            log.close()
        except Exception:
            pass
        self._tee = None
