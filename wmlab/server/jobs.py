"""Job manager: launch, follow, cancel and re-attach the jobs started by the app.

One JSON file per job (``<data>/jobs/<job_id>.json``) is the source of truth, so jobs
survive a restart of the app: at start-up the running ones are re-attached (``os.kill(pid,
0)`` locally, ``kill -0`` over SSH). Queued jobs start when their host has a free slot
(``max_concurrent_jobs``; setup jobs are not limited).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Callable

from wmlab.curriculum import PhasePlan
from wmlab.ids import new_job_id, new_run_id, new_study_id, utc_iso
from wmlab.spec import (
    HostSpec,
    OverrideError,
    SpecError,
    benchmark_path,
    detect_kind,
    experiment_path_for_hpo,
    kind_from_location,
    load_experiment,
    load_yaml,
    project_for,
)
from wmlab.server.catalog import FileAccessError, fidelity_experiment, resolve_workspace_file
from wmlab.server.context import ServerContext
from wmlab.server.executors import (
    FINAL_STATUSES,
    UNLIMITED_KINDS,
    CommandResult,
    ExecContext,
    Executor,
    JobRecord,
    Runner,
    default_runner,
    job_log_name,
    make_executor,
)
from wmlab.server.gitops import GitError, GitRepo, lock_digest, valid_ref
from wmlab.server.inventory import (
    INVENTORY_FILE,
    InventoryCache,
    build_inventory,
    local_inventory,
    unreachable_inventory,
    verify_runs,
)
from wmlab.tracking import (
    CheckpointNotFoundError,
    atomic_write_json,
    read_heartbeat,
    read_json,
    read_jsonl,
    resolve_checkpoint,
)
from wmlab.tracking.env import git_info
from wmlab.tracking.io import tail_lines

log = logging.getLogger("wmlab.server")

RUN_FINAL = ("completed", "failed", "killed")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_DIFF_CHARS = 2_000_000
MAX_ERROR_CHARS = 4000
MIN_STEPS_FOR_RATE = 50
MIN_SECONDS_FOR_RATE = 60.0


class JobError(Exception):
    """Refused job operation; ``status`` is the HTTP status to return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def safe_id(value: str | None, what: str = "identifiant") -> str:
    """Validate a run/study id used as a folder name (no path separators)."""
    if not value or not SAFE_ID.match(value) or ".." in value:
        raise JobError(400, f"{what} invalide : {value!r}")
    return value


def _short(text: Any, limit: int = MAX_ERROR_CHARS) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else "… " + text[-limit:]


def _fr(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


class JobManager:
    """Creates jobs, starts them through executors and follows them until they end."""

    def __init__(self, ctx: ServerContext, *, runner: Runner | None = None,
                 cli_prefix: list[str] | None = None, remote_cli_prefix: list[str] | None = None,
                 sync: bool = True, poll_interval: float = 2.0, repo: GitRepo | None = None,
                 snapshot: Callable[[], Any] | None = None,
                 reindex: Callable[[], Any] | None = None) -> None:
        self.ctx = ctx
        self.exec_ctx = ExecContext(ctx.workspace, ctx.data, runner or default_runner,
                                    cli_prefix, remote_cli_prefix)
        self.sync_enabled = sync
        self.poll_interval = poll_interval
        self.repo = repo if repo is not None else GitRepo(ctx.workspace, ctx.data)
        self.snapshot = snapshot          # index snapshot provider (set by the server)
        self.reindex = reindex            # forces an index scan after a retrieval
        self.inventory_cache = InventoryCache(ctx.data / INVENTORY_FILE)
        self._jobs: dict[str, JobRecord] = {}
        self._handles: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.launch_dir = ctx.jobs_dir / "launch"
        self.logs_dir = ctx.jobs_dir / "logs"
        self.load()

    # -- persistence -----------------------------------------------------------------------
    def load(self) -> None:
        """Load (re-attach) the jobs of ``<data>/jobs``."""
        with self._lock:
            for path in sorted(self.ctx.jobs_dir.glob("job-*.json")):
                data = read_json(path)
                if not isinstance(data, dict) or not data.get("job_id"):
                    continue
                try:
                    job = JobRecord.from_dict(data)
                except TypeError:
                    log.warning("fichier de job illisible : %s", path)
                    continue
                if job.status == "launching":
                    if job.pid is not None:
                        job.status = "running"
                    else:
                        job.status, job.ended_at = "lost", utc_iso()
                        job.error = "Le serveur s'est arrêté pendant le lancement du job."
                    self._save(job)
                elif job.status == "running" and job.kind in UNLIMITED_KINDS:
                    # setup/retrieve run inside the server process: gone with it
                    job.status, job.ended_at = "lost", utc_iso()
                    job.error = "Le serveur s'est arrêté pendant l'opération."
                    self._save(job)
                self._jobs[job.job_id] = job

    def _save(self, job: JobRecord) -> None:
        atomic_write_json(self.ctx.jobs_dir / f"{job.job_id}.json", job.to_dict())

    # -- lookups ---------------------------------------------------------------------------
    def hosts(self) -> dict[str, HostSpec]:
        return {h.name: h for h in self.ctx.hosts()}

    def host(self, name: str | None, *, allow_decommissioned: bool = False) -> HostSpec:
        hosts = self.hosts()
        name = name or self.ctx.settings().default_host
        if name not in hosts:
            raise JobError(400, f"Machine inconnue « {name} » — disponibles : "
                                f"{', '.join(hosts) or '(aucune)'}")
        host = hosts[name]
        if host.decommissioned and not allow_decommissioned:
            raise JobError(400, f"La machine « {name} » a été retirée le "
                                f"{host.decommissioned_at} : aucun job ne peut y être lancé.")
        return host

    def active_jobs(self, host: str) -> list[JobRecord]:
        """Jobs of ``host`` that are queued, launching or running (any kind)."""
        with self._lock:
            return [j for j in self._jobs.values() if j.host == host and j.active]

    def executor(self, host: HostSpec) -> Executor:
        return make_executor(host, self.exec_ctx)

    def list(self, status: str | None = None) -> list[JobRecord]:
        with self._lock:
            jobs = list(self._jobs.values())
        if status:
            wanted = {s.strip() for s in status.split(",") if s.strip()}
            jobs = [j for j in jobs if j.status in wanted]
        return sorted(jobs, key=lambda j: (j.created_at, j.job_id), reverse=True)

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise JobError(404, f"Job introuvable : {job_id}")
        return job

    def find_active(self, *, run_id: str | None = None, study_id: str | None = None
                    ) -> JobRecord | None:
        with self._lock:
            for job in self._jobs.values():
                if not job.active:
                    continue
                if run_id is not None and job.run_id == run_id and job.kind == "train":
                    return job
                if study_id is not None and job.study_id == study_id:
                    return job
        return None

    # -- submission ------------------------------------------------------------------------
    def submit(self, *, kind: str, host: str | None = None, spec_path: str | None = None,
               run_id: str | None = None, checkpoint: str | None = None,
               benchmark: str | None = None, overrides: dict[str, Any] | None = None,
               max_hours: float | None = None, smoke: bool = False,
               init_from: dict[str, Any] | None = None, git_ref: str | None = None,
               user: str | None = None) -> JobRecord:
        """Validate a launch request, create the job (``queued``) and schedule it."""
        if kind not in ("train", "hpo", "eval"):
            raise JobError(400, f"Type de job invalide « {kind} » — valeurs possibles : train, "
                                "hpo, eval (l'installation passe par /api/hosts/<nom>/setup)")
        host_spec = self.host(host)
        if max_hours is not None and (not math.isfinite(max_hours) or max_hours <= 0):
            raise JobError(400, "La durée maximale doit être un nombre strictement positif.")
        job = JobRecord(job_id=new_job_id(), kind=kind, host=host_spec.name, target="",
                        overrides={str(k): str(v) for k, v in (overrides or {}).items()},
                        created_at=utc_iso(), max_hours=max_hours, smoke=bool(smoke),
                        user=user or None)
        self._prepare_revision(job, host_spec, git_ref)
        if kind == "train":
            self._prepare_train(job, host_spec, spec_path, init_from)
        elif kind == "hpo":
            self._prepare_hpo(job, spec_path)
        else:
            self._prepare_eval(job, host_spec, run_id, checkpoint, benchmark)
        if kind in ("train", "hpo"):
            job.launch_file = self._write_launch_file(job, host_spec)
        with self._lock:
            self._jobs[job.job_id] = job
            self._save(job)
        log.info("job %s créé (%s sur %s : %s, révision %s, par %s)", job.job_id, job.kind,
                 job.host, job.target, (job.git_sha or "-")[:7], job.user or "-")
        self._schedule()
        return job

    def _prepare_revision(self, job: JobRecord, host: HostSpec, git_ref: str | None) -> None:
        """Resolve the requested revision (§12.3): ``Job.git_ref`` / ``Job.git_sha``.

        SSH hosts run the export of the resolved commit (``code/<sha>``) in hosted mode or
        when a revision is requested; otherwise (a Mac outside hosted mode) the working copy
        is synchronised as before and ``git_sha`` only records HEAD (``git_ref`` stays
        ``None``). The local host always runs the server checkout (HEAD), so another
        revision is refused.
        """
        ref = (git_ref or "").strip() or None
        if ref is not None and not valid_ref(ref):
            raise JobError(400, f"Révision git invalide : {ref!r}")
        if not self.repo.available:
            if ref is not None:
                raise JobError(400, "Aucun dépôt git dans le workspace : la révision ne peut "
                                    "pas être choisie (le code courant sera synchronisé).")
            return
        head = self.repo.head()
        if host.kind == "local":
            if ref is not None:
                sha = self.repo.resolve(ref)
                if sha is None:
                    raise JobError(400, f"Révision git inconnue : « {ref} » (poussez votre "
                                        "branche puis indiquez son nom ou un sha).")
                if sha != head:
                    raise JobError(400, "La machine locale exécute la révision courante du "
                                        f"serveur ({(head or '')[:7]}) : « {ref} » "
                                        f"({sha[:7]}) ne peut pas y être lancée.")
            job.git_ref, job.git_sha = ref or "HEAD", head
            return
        if ref is None and not self.hosted():
            job.git_sha = head  # legacy: rsync of the working copy, HEAD for information
            return
        ref = ref or "HEAD"
        sha = head if ref == "HEAD" else self.repo.resolve(ref)
        if sha is None:
            raise JobError(400, f"Révision git inconnue : « {ref} » (poussez votre branche "
                                "puis indiquez son nom ou un sha).")
        job.git_ref, job.git_sha = ref, sha

    def hosted(self) -> bool:
        try:
            return bool(self.ctx.settings().hosted.enabled)
        except Exception:
            return False

    def _export_code(self, job: JobRecord) -> None:
        """Export ``job.git_sha`` into ``<data>/code/<sha>`` (ssh jobs launched by
        revision, at launch time)."""
        if not job.git_ref or not job.git_sha or job.code_dir:
            return
        try:
            folder = self.repo.export(job.git_sha)
        except GitError as exc:
            raise RuntimeError(f"Export de la révision {job.git_sha[:7]} impossible : {exc}")                 from None
        job.code_dir, job.lock_sha256 = str(folder), lock_digest(folder)

    def setup_host(self, name: str, user: str | None = None) -> JobRecord:
        """Create a ``setup`` job (code sync + ``scripts/remote_bootstrap.sh``)."""
        host = self.host(name)
        if host.kind != "ssh":
            raise JobError(400, f"« {host.name} » est la machine locale : aucune installation "
                                "n'est nécessaire.")
        job = JobRecord(job_id=new_job_id(), kind="setup", host=host.name, target=host.name,
                        created_at=utc_iso(), user=user or None)
        if self.repo.available:  # git mode (hosted): bootstrap from the export of HEAD
            job.git_sha = self.repo.head()
            job.git_ref = "HEAD" if self.hosted() else None
        with self._lock:
            self._jobs[job.job_id] = job
            self._save(job)
        self._schedule()
        return job

    def retrieve_host(self, name: str, user: str | None = None) -> JobRecord:
        """Create a ``retrieve`` job: full rsync of every run/study of the host (§12.4)."""
        host = self.host(name, allow_decommissioned=True)
        if host.kind != "ssh":
            raise JobError(400, f"« {host.name} » est la machine locale : rien à rapatrier.")
        with self._lock:
            for job in self._jobs.values():
                if job.kind == "retrieve" and job.host == host.name and job.active:
                    raise JobError(409, f"Un rapatriement de « {host.name} » est déjà en cours "
                                        f"({job.job_id}).")
        job = JobRecord(job_id=new_job_id(), kind="retrieve", host=host.name,
                        target=host.name, created_at=utc_iso(), user=user or None)
        with self._lock:
            self._jobs[job.job_id] = job
            self._save(job)
        self._schedule()
        return job

    def _spec_file(self, spec_path: str | None, expected: str | None) -> tuple[Path, str, str]:
        if not spec_path:
            raise JobError(400, "spec_path est requis (chemin du fichier YAML dans le workspace).")
        try:
            path, rel = resolve_workspace_file(self.ctx.workspace, spec_path,
                                               data_root=self.ctx.data)
        except FileAccessError as exc:
            raise JobError(exc.status, exc.message) from None
        if not path.is_file():
            raise JobError(404, f"Fichier introuvable : {rel}")
        kind = kind_from_location(path) or detect_kind(path)
        if expected is not None and kind != expected:
            label = {"experiment": "une expérience", "hpo": "un plan HPO"}[expected]
            raise JobError(400, f"« {rel} » n'est pas {label} (type détecté : {kind or 'inconnu'}).")
        return path, rel, kind or ""

    def _prepare_train(self, job: JobRecord, host: HostSpec, spec_path: str | None,
                       init_from: dict[str, Any] | None) -> None:
        path, rel, _ = self._spec_file(spec_path, "experiment")
        if init_from:
            init = {"run": safe_id(str(init_from.get("run") or ""), "run parent"),
                    "checkpoint": str(init_from.get("checkpoint") or "last"),
                    "start_phase": init_from.get("start_phase")}
            if init_from.get("resume"):
                init["resume"] = True
                init["start_phase"] = None  # resume restarts at the checkpoint's own step
            self._check_parent(init, host)
            job.init_from = init
            job.overrides["init_from"] = json.dumps(init, ensure_ascii=False)
        try:
            spec, _, _ = load_experiment(path, job.overrides)
        except (SpecError, OverrideError) as exc:
            raise JobError(400, f"Expérience invalide : {exc}") from None
        job.spec_path = job.target = rel
        job.run_id = new_run_id(spec.name)
        args = ["train", rel, "--run-id", job.run_id]
        for key, value in job.overrides.items():
            args += ["--set", f"{key}={value}"]
        if job.smoke:
            args.append("--smoke")
        if job.max_hours is not None:
            args += ["--max-hours", f"{job.max_hours:g}"]
        job.args = args

    def _check_parent(self, init: dict[str, Any], host: HostSpec) -> None:
        run_dir = self.ctx.runs_dir / init["run"]
        manifest = read_json(run_dir / "run.json")
        if not isinstance(manifest, dict):
            raise JobError(400, f"Run parent introuvable : {init['run']} (absent de "
                                f"{self.ctx.runs_dir}).")
        parent_host = str(manifest.get("host") or "local")
        try:
            entry = resolve_checkpoint(run_dir, init["checkpoint"])
        except CheckpointNotFoundError as exc:
            raise JobError(400, f"Checkpoint parent invalide : {exc}") from None
        if host.kind == "local":
            if not entry.get("available_locally"):
                raise JobError(400, (
                    f"Le checkpoint « {entry.get('name')} » du run parent {init['run']} n'est "
                    f"pas présent sur « {host.name} » (run entraîné sur « {parent_host} ») : "
                    f"rapatriez-le depuis l'onglet Checkpoints ou lancez sur « {parent_host} »."))
        elif parent_host != host.name:
            raise JobError(400, (
                f"Le run parent {init['run']} a été entraîné sur « {parent_host} » : il doit "
                f"être présent sur la même machine que le nouvel entraînement. Lancez sur "
                f"« {parent_host} »."))

    def _prepare_hpo(self, job: JobRecord, spec_path: str | None) -> None:
        path, rel, _ = self._spec_file(spec_path, "hpo")
        try:
            spec = load_yaml(path, "hpo")
            if job.overrides:  # they apply to the base experiment of every trial
                load_experiment(experiment_path_for_hpo(path, spec), job.overrides)
        except (SpecError, OverrideError) as exc:
            raise JobError(400, f"Plan HPO invalide : {exc}") from None
        job.spec_path = job.target = rel
        job.study_id = new_study_id(spec.name)
        args = ["hpo", rel, "--study-id", job.study_id]
        for key, value in job.overrides.items():
            args += ["--set", f"{key}={value}"]
        if job.smoke:
            args.append("--smoke")
        if job.max_hours is not None:  # caps budget.max_hours of the study
            args += ["--max-hours", f"{job.max_hours:g}"]
        job.args = args

    def _prepare_eval(self, job: JobRecord, host: HostSpec, run_id: str | None,
                      checkpoint: str | None, benchmark: str | None) -> None:
        if not run_id or not checkpoint or not benchmark:
            raise JobError(400, "run_id, checkpoint et benchmark sont requis pour une évaluation.")
        run_id = safe_id(run_id, "run")
        benchmark = safe_id(benchmark, "nom de benchmark")  # a name, never a path
        run_dir = self.ctx.runs_dir / run_id
        manifest = read_json(run_dir / "run.json")
        if not isinstance(manifest, dict):
            raise JobError(404, f"Run introuvable : {run_id}")
        if benchmark_path(benchmark, self.ctx.workspace) is None:
            raise JobError(400, f"Benchmark inconnu « {benchmark} » (fichier "
                                f"benchmarks/{benchmark}.yaml introuvable).")
        try:
            entry = resolve_checkpoint(run_dir, checkpoint)
        except CheckpointNotFoundError as exc:
            raise JobError(400, str(exc)) from None
        run_host = str(manifest.get("host") or "local")
        if host.kind == "local":
            if not entry.get("available_locally"):
                raise JobError(400, (
                    f"Le checkpoint « {entry.get('name')} » n'est pas présent sur « {host.name} » "
                    f"(run entraîné sur « {run_host} ») : rapatriez-le d'abord ou évaluez sur "
                    f"« {run_host} »."))
        elif run_host != host.name:
            raise JobError(400, f"Le run {run_id} a été entraîné sur « {run_host} » : évaluez-le "
                                f"sur cette machine (ou rapatriez le checkpoint et évaluez en local).")
        job.overrides = {}
        job.run_id, job.checkpoint, job.benchmark = run_id, checkpoint, benchmark
        job.target = f"{run_id}/{checkpoint}@{benchmark}"
        job.args = ["eval", "--run", run_id, "--checkpoint", checkpoint, "--benchmark", benchmark]
        if job.smoke:
            job.args.append("--smoke")

    def _write_launch_file(self, job: JobRecord, host: HostSpec) -> str:
        git, diff = git_info(self.ctx.workspace)
        by_revision = host.kind == "ssh" and bool(job.git_ref and job.git_sha)
        if git is not None and by_revision:
            # the job runs an exported commit (`code/<sha>`, §12.3), not the (possibly
            # dirty) checkout: even when the revision is HEAD the export carries no local edit
            git = {"sha": job.git_sha, "branch": job.git_ref, "dirty": False,
                   "remote": git.get("remote")}
            diff = None
        if diff and len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n… (tronqué)"
        info = {
            "job_id": job.job_id, "kind": job.kind, "host": job.host,
            "spec_path": job.spec_path, "overrides": dict(job.overrides),
            "init_from": job.init_from, "smoke": job.smoke, "max_hours": job.max_hours,
            "launched_at": utc_iso(), "git": git, "git_diff": diff, "user": job.user,
            "git_ref": job.git_ref, "git_sha": job.git_sha,
        }
        path = self.launch_dir / f"{job.job_id}.json"
        atomic_write_json(path, info)
        return str(path)

    # -- scheduling & launch ---------------------------------------------------------------
    def _schedule(self) -> None:
        """Start queued jobs whose host has a free slot."""
        with self._lock:
            queued = sorted((j for j in self._jobs.values() if j.status == "queued"),
                            key=lambda j: (j.created_at, j.job_id))
            if not queued:
                return
            try:
                hosts = self.hosts()
            except Exception as exc:
                log.warning("hosts.yaml illisible, jobs en attente : %s", exc)
                return
            for job in queued:
                host = hosts.get(job.host)
                if host is None:
                    self._finish(job, "failed", f"Machine inconnue « {job.host} » (supprimée de "
                                                "hosts.yaml ?).")
                    continue
                if job.kind not in UNLIMITED_KINDS:
                    busy = sum(1 for j in self._jobs.values()
                               if j.host == job.host and j.kind not in UNLIMITED_KINDS
                               and j.status in ("launching", "running"))
                    if busy >= host.max_concurrent_jobs:
                        continue
                job.status = "launching"
                self._save(job)
                threading.Thread(target=self._launch, args=(job.job_id, host), daemon=True,
                                 name=f"wmlab-launch-{job.job_id}").start()

    def _launch(self, job_id: str, host: HostSpec) -> None:
        job = self.get(job_id)
        executor = self.executor(host)
        try:
            if job.kind == "retrieve":
                self._run_retrieve(job, executor)
                return
            if host.kind == "ssh":
                self._export_code(job)
            if job.kind == "setup":
                self._run_setup(job, executor)
                return
            result = executor.launch(job)
            with self._lock:
                job.pid, job.proc_start = result.pid, result.proc_start
                job.remote_start = result.remote_start
                job.log_path, job.remote_log = result.log_path, result.remote_log
                job.started_at, job.started_epoch = utc_iso(), time.time()
                job.status = "running"
                if result.handle is not None:
                    self._handles[job.job_id] = result.handle
                self._save(job)
                cancel = job.cancel_requested
            log.info("job %s démarré (pid %s)", job.job_id, job.pid)
            if cancel:
                executor.cancel(job, result.handle)
        except Exception as exc:
            log.exception("échec du lancement du job %s", job_id)
            with self._lock:
                self._finish(job, "failed", f"Échec du lancement : {exc}")
        finally:
            self._schedule()

    def _run_setup(self, job: JobRecord, executor: Executor) -> None:
        with self._lock:
            job.status, job.started_at, job.started_epoch = "running", utc_iso(), time.time()
            self._save(job)
        try:
            result = executor.setup(job)
        except Exception as exc:
            result = CommandResult(1, "", str(exc))
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"{job.job_id}.log"
        log_path.write_text(f"{result.stdout}\n{result.stderr}", encoding="utf-8")
        with self._lock:
            job.log_path = str(log_path)
            if job.status in FINAL_STATUSES:
                self._save(job)
                return  # cancelled meanwhile
            if result.ok:
                self._finish(job, "completed", None)
            else:
                self._finish(job, "failed", f"Installation échouée : "
                                            f"{result.tail(15) or f'code {result.returncode}'}")

    def _run_retrieve(self, job: JobRecord, executor: Executor) -> None:
        """Bring back every run and study of the host, verify the checkpoints, reindex."""
        with self._lock:
            job.status, job.started_at, job.started_epoch = "running", utc_iso(), time.time()
            job.result = {"runs": 0, "studies": 0, "checkpoints": 0, "bytes": 0, "missing": []}
            self._save(job)
        error: str | None = None
        run_ids: list[str] = []
        try:
            listing = executor.listing()  # type: ignore[attr-defined]
            run_ids = listing.run_ids
            for study_id in sorted(listing.studies):
                if job.cancel_requested:
                    break
                executor.pull("studies", study_id, full=True)  # type: ignore[attr-defined]
                job.result["studies"] += 1
                self._save(job)
            for run_id in run_ids:
                if job.cancel_requested:
                    break
                executor.pull("runs", run_id, full=True)  # type: ignore[attr-defined]
                job.result["runs"] += 1
                self._save(job)
            check = verify_runs(self.ctx.runs_dir, run_ids)
            if check["missing"] and not job.cancel_requested:
                for run_id in check["by_run"]:
                    executor.pull("runs", run_id, full=True)  # type: ignore[attr-defined]
                check = verify_runs(self.ctx.runs_dir, run_ids)
        except Exception as exc:
            log.exception("rapatriement depuis %s interrompu", job.host)
            error = str(exc)
            check = verify_runs(self.ctx.runs_dir, run_ids)
        if self.reindex is not None:
            try:
                self.reindex()
            except Exception:
                log.exception("réindexation après rapatriement impossible")
        with self._lock:
            job.result.update(checkpoints=check["checkpoints"], bytes=check["bytes"],
                              missing=check["missing"])
            if job.status in FINAL_STATUSES:
                self._save(job)
                return
            if job.cancel_requested:
                self._finish(job, "killed", "Rapatriement interrompu depuis l'application.")
            elif error:
                self._finish(job, "failed", f"Rapatriement échoué : {error}")
            else:
                self._finish(job, "completed", None)
        try:  # the host card shows what is still on the machine
            self.inventory(job.host)
        except Exception as exc:
            log.warning("inventaire de %s après rapatriement impossible : %s", job.host, exc)

    def _finish(self, job: JobRecord, status: str, error: str | None) -> None:
        job.status, job.error, job.ended_at = status, _short(error), utc_iso()
        self._handles.pop(job.job_id, None)
        self._save(job)
        log.info("job %s terminé : %s", job.job_id, status)

    # -- monitoring ------------------------------------------------------------------------
    def tick(self) -> None:
        """Poll running jobs once (local each tick, SSH every ``sync_interval_s``)."""
        with self._lock:
            running = [j for j in self._jobs.values()
                       if j.status == "running" and j.kind not in UNLIMITED_KINDS]
        if running:
            try:
                hosts = self.hosts()
                interval = float(self.ctx.settings().sync_interval_s)
            except Exception as exc:
                log.warning("suivi des jobs impossible : %s", exc)
                hosts, interval = {}, 30.0
            now = time.time()
            for job in running:
                host = hosts.get(job.host)
                if host is None:
                    continue
                if host.kind == "ssh":
                    if not self.sync_enabled:
                        continue
                    if job.last_poll is not None and now - job.last_poll < interval:
                        continue
                try:
                    self._refresh(job, host)
                except Exception:
                    log.exception("suivi du job %s impossible", job.job_id)
        self._schedule()

    def _refresh(self, job: JobRecord, host: HostSpec) -> None:
        executor = self.executor(host)
        state = executor.poll(job, self._handles.get(job.job_id))
        job.last_poll = time.time()
        if state.alive is None:
            log.warning("état du job %s inconnu : %s", job.job_id, state.detail)
            return
        if host.kind == "ssh":
            if state.alive:
                self._sync_files(job, executor)
            else:  # end of job: full sync (*.pt included) + size check (§12.4)
                self._retrieve(job, executor)
        with self._lock:
            if job.status != "running":
                return
            if state.alive:
                if host.kind == "ssh":
                    self._save(job)  # keeps last_sync_at
                return
            job.exit_code = state.exit_code
            status, error = self._outcome(job)
            self._finish(job, status, error)
            self._close_manifests(job, status, error)

    def _retrieve(self, job: JobRecord, executor: Executor) -> None:
        try:
            job.retrieval = executor.retrieve(job)
            job.last_sync_at = utc_iso()
        except Exception as exc:
            log.warning("rapatriement du job %s impossible : %s", job.job_id, exc)
            job.retrieval = {"status": "failed", "checkpoints": 0, "bytes": 0, "missing": []}
        if job.retrieval and job.retrieval.get("status") != "complete":
            log.warning("job %s : rapatriement %s (%d fichier(s) manquant(s))", job.job_id,
                        job.retrieval["status"], len(job.retrieval.get("missing") or []))

    def _close_manifests(self, job: JobRecord, status: str, error: str | None) -> None:
        """Write the final status into run.json / study.json (and the trial runs of a study)
        when the process is confirmed dead but never wrote one — the job manager is the
        authority then; the heartbeat rule (``lost`` after 600 s) stays for runs outside
        the app."""
        if job.kind == "train":
            paths = [self.ctx.runs_dir / str(job.run_id) / "run.json"]
        elif job.kind == "hpo":
            study_dir = self.ctx.studies_dir / str(job.study_id)
            paths = [study_dir / "study.json"]
            for row in read_jsonl(study_dir / "trials.jsonl"):
                if row.get("run_id"):
                    paths.append(self.ctx.runs_dir / str(row["run_id"]) / "run.json")
        else:
            return
        for path in dict.fromkeys(paths):
            manifest = read_json(path)
            if not isinstance(manifest, dict) or manifest.get("status") != "running":
                continue
            heartbeat = read_heartbeat(path.parent)
            manifest["status"] = status if status in ("failed", "lost", "killed") else "failed"
            manifest["ended_at"] = utc_iso(heartbeat if heartbeat else None)
            manifest["error"] = error or "Le process s'est arrêté sans écrire de statut final."
            try:
                atomic_write_json(path, manifest)
            except OSError as exc:
                log.warning("statut final de %s non écrit : %s", path, exc)
                continue
            log.info("%s : statut « %s » écrit (process disparu)", path.parent.name, status)

    def _sync_files(self, job: JobRecord, executor: Executor) -> None:
        try:
            executor.sync(job)
            job.last_sync_at = utc_iso()
        except Exception as exc:
            log.warning("synchronisation du job %s impossible : %s", job.job_id, exc)

    def _log_tail(self, job: JobRecord, n: int = 15) -> str:
        path = Path(job.log_path) if job.log_path else None
        if path is None:
            folder, name = job_log_name(job)
            path = self.ctx.data / folder / name
        return "\n".join(tail_lines(path, n)).strip()

    def _outcome(self, job: JobRecord) -> tuple[str, str | None]:
        """Final status of a job whose process is gone."""
        tail = self._log_tail(job)
        if job.kind in ("train", "hpo"):
            folder = (self.ctx.runs_dir / str(job.run_id) / "run.json" if job.kind == "train"
                      else self.ctx.studies_dir / str(job.study_id) / "study.json")
            manifest = read_json(folder)
            status = manifest.get("status") if isinstance(manifest, dict) else None
            if status in RUN_FINAL:
                return status, manifest.get("error")  # type: ignore[union-attr]
            if job.cancel_requested:
                return "killed", "Annulé depuis l'application."
            if not isinstance(manifest, dict) and job.exit_code not in (None, 0):
                return "failed", tail or f"Le process s'est terminé avec le code {job.exit_code}."
            message = "Le process s'est arrêté sans écrire de statut final."
            return "lost", f"{message}\n{tail}" if tail else message
        if job.cancel_requested:
            return "killed", "Annulé depuis l'application."
        if job.exit_code == 0:
            return "completed", None
        if job.exit_code is not None:
            return "failed", tail or f"Le process s'est terminé avec le code {job.exit_code}."
        if job.kind == "eval" and self._eval_recorded(job):
            return "completed", None
        return "lost", "Le process s'est arrêté ; résultat inconnu."

    def _eval_recorded(self, job: JobRecord) -> bool:
        started = (job.started_epoch or 0.0) - 1.0
        for event in read_jsonl(self.ctx.runs_dir / str(job.run_id) / "events.jsonl"):
            if event.get("type") == "eval" and event.get("benchmark") == job.benchmark \
                    and float(event.get("t") or 0.0) >= started:
                return True
        return False

    # -- user actions ----------------------------------------------------------------------
    def cancel(self, job_id: str) -> JobRecord:
        """Cancel a job (SIGTERM to its process group; queued jobs are simply dropped)."""
        with self._lock:
            job = self.get(job_id)
            if job.status in FINAL_STATUSES:
                return job
            if job.status == "queued":
                self._finish(job, "killed", "Annulé avant le démarrage.")
                return job
            job.cancel_requested = True
            self._save(job)
            if job.status == "launching":
                return job  # killed as soon as the launch returns
            if job.kind == "setup":
                self._finish(job, "killed", "Installation interrompue (le script distant peut "
                                            "continuer).")
                return job
            if job.kind == "retrieve":
                return job  # the retrieval loop stops after the current transfer
            handle = self._handles.get(job.job_id)
        host = self.hosts().get(job.host)
        if host is None:
            raise JobError(400, f"Machine inconnue « {job.host} » : arrêt impossible.")
        try:
            self.executor(host).cancel(job, handle)
        except Exception as exc:
            raise JobError(502, f"Arrêt impossible : {exc}") from None
        log.info("job %s : SIGTERM envoyé", job.job_id)
        return job

    def sync_job(self, job_id: str) -> JobRecord:
        """Refresh a job now (poll + rsync of its folders for SSH hosts; a finished job
        gets its full retrieval again, which repairs a ``partial`` one)."""
        job = self.get(job_id)
        host = self.hosts().get(job.host)
        if host is None or job.kind in UNLIMITED_KINDS:
            return job
        if job.status == "running":
            self._refresh(job, host)
        elif job.status in FINAL_STATUSES and host.kind == "ssh":
            self._retrieve(job, self.executor(host))
            with self._lock:
                self._save(job)
        self._schedule()
        return job

    # -- host inventory (§12.4) ------------------------------------------------------------
    def inventory(self, name: str) -> dict[str, Any]:
        """Compute (and cache) the ``HostInventory`` of a host."""
        host = self.host(name, allow_decommissioned=True)
        active = len(self.active_jobs(host.name))
        snap = self.snapshot() if self.snapshot is not None else None
        runs = list(getattr(snap, "runs", []) or [])
        studies = list(getattr(snap, "studies", []) or [])
        if host.kind != "ssh":
            result = local_inventory(host.name, runs, studies, self.ctx.data, active_jobs=active)
        else:
            executor = self.executor(host)
            try:
                listing = executor.listing()  # type: ignore[attr-defined]
            except Exception as exc:
                result = unreachable_inventory(host.name, str(exc), active_jobs=active,
                                               remote_data_dir=executor.data_dir)  # type: ignore[attr-defined]
            else:
                indexed = {str(m.get("run_id")) for m in runs}
                result = build_inventory(host.name, listing, self.ctx.data, indexed,
                                         active_jobs=active,
                                         remote_data_dir=executor.data_dir)  # type: ignore[attr-defined]
        self.inventory_cache.set(host.name, result)
        return result

    def host_view(self, host: HostSpec) -> dict[str, Any]:
        """``Host`` of the API: the spec plus the cached inventory fields and active jobs."""
        from wmlab.server.inventory import host_inventory_fields

        view = host.model_dump(mode="json")
        view.update(host_inventory_fields(self.inventory_cache.get(host.name)))
        view["active_jobs"] = len(self.active_jobs(host.name))
        return view

    def fetch_checkpoint(self, run_id: str, name: str) -> None:
        """Bring a checkpoint file back from the host of its run (no-op if present)."""
        run_dir = self.ctx.runs_dir / safe_id(run_id, "run")
        manifest = read_json(run_dir / "run.json")
        if not isinstance(manifest, dict):
            raise JobError(404, f"Run introuvable : {run_id}")
        try:
            entry = resolve_checkpoint(run_dir, name)
        except CheckpointNotFoundError as exc:
            raise JobError(404, str(exc)) from None
        if entry.get("available_locally"):
            return
        host_name = str(manifest.get("host") or "local")
        host = self.hosts().get(host_name)
        if host is None:
            raise JobError(400, f"Machine « {host_name} » inconnue : impossible de rapatrier le "
                                "checkpoint (ajoutez-la dans Machines).")
        if host.kind != "ssh":
            raise JobError(404, f"Le fichier du checkpoint « {entry.get('name')} » n'existe plus "
                                "(supprimé par la politique de rétention).")
        try:
            self.executor(host).fetch_checkpoint(run_dir.name, Path(str(entry["path"])).name)
        except Exception as exc:
            raise JobError(502, str(exc)) from None

    def stop_run(self, run_id: str) -> JobRecord | None:
        """Stop a running run: through its job, else SIGTERM to its local process."""
        run_dir = self.ctx.runs_dir / safe_id(run_id, "run")
        manifest = read_json(run_dir / "run.json")
        job = self.find_active(run_id=run_id)
        if job is not None:
            return self.cancel(job.job_id)
        if not isinstance(manifest, dict):
            raise JobError(404, f"Run introuvable : {run_id}")
        if manifest.get("study"):
            raise JobError(409, f"Ce run est un essai de l'étude {manifest['study']} : arrêtez "
                                "l'étude depuis sa page.")
        if manifest.get("status") != "running":
            raise JobError(409, f"Le run {run_id} n'est pas en cours "
                                f"(statut : {manifest.get('status')}).")
        host_name = str(manifest.get("host") or "local")
        try:
            host = self.hosts().get(host_name)
        except Exception:
            host = None
        if host is not None and host.kind != "local":
            raise JobError(409, f"Le run {run_id} tourne sur « {host_name} » hors de "
                                "l'application : aucun process local à arrêter (arrêtez-le "
                                "sur cette machine).")
        pid = run_process_pid(manifest)
        if pid is None:
            pid = find_run_process(run_id)
        if pid is None:
            raise JobError(409, "Aucun process local trouvé pour ce run (lancé sur une autre "
                                "machine ou hors de l'application ?).")
        # only the trainer itself (it checkpoints and closes its loaders on SIGTERM); a run
        # started outside the app may share its process group with an unrelated shell
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError) as exc:
            raise JobError(409, f"Arrêt impossible du process {pid} : {exc}") from None
        log.info("run %s : SIGTERM envoyé au process %s", run_id, pid)
        return None

    # -- estimate --------------------------------------------------------------------------
    def estimate(self, spec_path: str, host_name: str | None, overrides: dict[str, Any] | None,
                 runs: list[dict[str, Any]]) -> dict[str, Any]:
        """``{total_steps, est_hours, est_eur, basis}`` from the throughput of past runs."""
        host = self.host(host_name)
        path, _, kind = self._spec_file(spec_path, None)
        overrides = {str(k): str(v) for k, v in (overrides or {}).items()}
        cap_hours = cap_eur = None
        try:
            if kind == "experiment":
                spec, project, _ = load_experiment(path, overrides)
                total_steps = PhasePlan(spec).total_steps
                what = f"« {spec.name} »"
            elif kind == "hpo":
                hpo = load_yaml(path, "hpo")
                spec = fidelity_experiment(path, hpo)
                _, project = project_for(path)
                per_trial = PhasePlan(spec).total_steps
                trials = hpo.budget.max_trials
                total_steps = per_trial * trials if trials else per_trial
                cap_hours, cap_eur = hpo.budget.max_hours, hpo.budget.max_eur
                what = f"« {spec.name} » ({trials or '?'} essais × {per_trial} steps)"
            else:
                raise JobError(400, "Estimation possible uniquement pour une expérience ou un "
                                    "plan HPO.")
        except (SpecError, OverrideError, FileNotFoundError) as exc:
            raise JobError(400, f"Spécification invalide : {exc}") from None
        rate, basis = throughput(runs, project.name, spec.name, project.modality, host.name)
        if rate is None:
            return {"total_steps": total_steps, "est_hours": None, "est_eur": None,
                    "basis": basis}
        hours = total_steps / rate / 3600.0
        notes = [basis, f"{total_steps} steps pour {what}."]
        if cap_hours is not None and hours > cap_hours:
            hours = float(cap_hours)
            notes.append(f"Plafonné par le budget de l'étude ({_fr(cap_hours, 1)} h).")
        eur = hours * host.price_eur_per_hour
        if cap_eur is not None and eur > cap_eur:
            eur = float(cap_eur)
            if host.price_eur_per_hour > 0:
                hours = eur / host.price_eur_per_hour
            notes.append(f"Plafonné par le budget de l'étude ({_fr(cap_eur)} €).")
        return {"total_steps": total_steps, "est_hours": round(hours, 3),
                "est_eur": round(eur, 2), "basis": " ".join(notes)}

    # -- background thread -----------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(self.poll_interval):
                try:
                    self.tick()
                except Exception:
                    log.exception("échec du suivi des jobs")

        self._schedule()
        self._thread = threading.Thread(target=loop, name="wmlab-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop following jobs (the job processes themselves keep running)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None


def _run_rate(manifest: dict[str, Any]) -> float | None:
    progress = manifest.get("progress") if isinstance(manifest.get("progress"), dict) else {}
    step = progress.get("step")
    duration = manifest.get("duration_s")
    if not isinstance(step, (int, float)) or not isinstance(duration, (int, float)):
        return None
    if step < MIN_STEPS_FOR_RATE or duration < MIN_SECONDS_FOR_RATE:
        return None
    return float(step) / float(duration)


def throughput(runs: list[dict[str, Any]], project: str, experiment: str, modality: str,
               host: str) -> tuple[float | None, str]:
    """Median steps/s of comparable past runs on ``host`` and a French explanation."""
    candidates = [m for m in runs if not m.get("study") and m.get("host") == host]
    same = [r for r in (_run_rate(m) for m in candidates
                        if m.get("project") == project and m.get("experiment") == experiment)
            if r is not None]
    if same:
        rate = statistics.median(same)
        return rate, (f"Débit médian de {len(same)} run(s) de « {experiment} » sur « {host} » : "
                      f"{_fr(rate)} steps/s.")
    similar = [r for r in (_run_rate(m) for m in candidates if m.get("modality") == modality)
               if r is not None]
    if similar:
        rate = statistics.median(similar)
        return rate, (f"Aucun run de « {experiment} » sur « {host} » : débit médian de "
                      f"{len(similar)} run(s) {modality} sur cette machine ({_fr(rate)} steps/s).")
    return None, (f"Aucun run {modality} comparable sur « {host} » : estimation impossible "
                  "(lancez d'abord un run de quelques centaines de steps sur cette machine).")


def _is_wmlab_cmdline(cmdline: list[str]) -> bool:
    """True for ``… -m wmlab.cli …`` or the ``wmlab`` console script (exact tokens)."""
    for i, part in enumerate(cmdline):
        if part == "-m" and i + 1 < len(cmdline) and cmdline[i + 1] == "wmlab.cli":
            return True
        if Path(part).name == "wmlab" and i == 0:
            return True
    return False


def run_process_pid(manifest: dict[str, Any]) -> int | None:
    """pid recorded in ``run.json`` by the training process, if that process is still it.

    The manifest ``pid`` is checked against the live process table (``psutil``): same
    creation time (pid reuse guard) and a ``wmlab`` command line.
    """
    pid = manifest.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or pid == os.getpid():
        return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        start = manifest.get("proc_start")
        if isinstance(start, (int, float)) and not isinstance(start, bool) \
                and abs(float(proc.create_time()) - float(start)) > 2.0:
            return None  # the pid now belongs to another process
        if not _is_wmlab_cmdline(list(proc.cmdline() or [])):
            return None
    except Exception:
        return None
    return int(pid)


def find_run_process(run_id: str) -> int | None:
    """pid of a local ``wmlab … train --run-id <run_id>`` process (exact argv tokens).

    Fallback for manifests without ``pid``; a substring match would also hit unrelated
    processes mentioning the run id (``tail -f …/<run_id>/stdout.log``, editors, shells).
    """
    try:
        import psutil
    except ImportError:
        return None
    own = os.getpid()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = list(proc.info.get("cmdline") or [])
        except Exception:
            continue
        if proc.info.get("pid") == own or not _is_wmlab_cmdline(cmdline):
            continue
        if "train" not in cmdline:
            continue
        for i, part in enumerate(cmdline):
            if part == "--run-id" and i + 1 < len(cmdline) and cmdline[i + 1] == run_id:
                return int(proc.info["pid"])
            if part == f"--run-id={run_id}":
                return int(proc.info["pid"])
    return None


__all__ = ["JobError", "JobManager", "find_run_process", "run_process_pid", "safe_id",
           "throughput"]
