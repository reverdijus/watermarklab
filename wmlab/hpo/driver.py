"""HPO studies: driver process and worker processes (ARCHITECTURE.md §6.4, §8.3).

The driver creates ``<data>/studies/<study_id>/`` and an Optuna study stored in
``optuna.journal`` (``JournalStorage`` + ``JournalFileBackend``), then launches
``budget.parallel`` worker subprocesses (``wmlab hpo-worker``) sharing that journal. Each trial
is a normal training run (``train_from_spec`` with ``study``/``trial``/``gpu_share``).
``trials.jsonl`` gets one row when a trial starts (RUNNING) and one when it ends
(COMPLETE / PRUNED / FAIL); readers keep the latest row per trial number.
"""

from __future__ import annotations

import contextlib
import fcntl
import math
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from wmlab import paths
from wmlab.hpo.space import decode_params, grid_size, space_errors, suggest, trial_overrides
from wmlab.ids import new_run_id, new_study_id, parse_iso, utc_iso
from wmlab.spec import (
    HpoSpec,
    SpecError,
    dump_yaml,
    experiment_path_for_hpo,
    load_experiment,
    load_yaml,
    validate_data,
)
from wmlab.spec.loader import resolve_in_workspace
from wmlab.tracking import (
    RunReader,
    append_event,
    append_jsonl,
    atomic_write_json,
    atomic_write_text,
    read_json,
)
from wmlab.tracking.run import launch_user

FORMAT_VERSION = 1
HEARTBEAT_INTERVAL_S = 20.0
SMOKE_TRIALS = 3
SPONTANEOUS_EXIT_S = 10.0
TERMINATE_GRACE_S = 60.0
MIN_FAILURES_TO_STOP = 3


class StudyTerminated(KeyboardInterrupt):
    """Raised in the driver's main thread when it receives SIGTERM."""


def _optuna() -> Any:
    try:
        import optuna
    except ImportError:
        raise RuntimeError("le paquet Python 'optuna' est requis pour les études HPO "
                           "(dépendance optionnelle 'train' / 'server')") from None
    return optuna


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(paths.workspace_root().resolve()).as_posix()
    except Exception:
        return str(path)


def _storage(study_dir: Path) -> Any:
    optuna = _optuna()
    from optuna.storages.journal import JournalFileBackend

    backend = JournalFileBackend(str(Path(study_dir) / "optuna.journal"))
    return optuna.storages.JournalStorage(backend)


@contextlib.contextmanager
def _locked(study_dir: Path) -> Iterator[None]:
    """Inter-process lock guarding read-modify-write of ``study.json``."""
    with open(Path(study_dir) / ".study.lock", "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_heartbeat(study_dir: Path) -> None:
    if not Path(study_dir).is_dir():  # a deleted study folder must not be recreated
        return
    with contextlib.suppress(OSError):
        atomic_write_text(Path(study_dir) / "heartbeat", f"{time.time():.3f}")


# -- samplers, pruners, constraints ---------------------------------------------------------
def native_constraints() -> bool:
    """True when Optuna has ``Trial.set_constraint`` (v5+, ``constraints_func`` deprecated)."""
    return hasattr(_optuna().trial.Trial, "set_constraint")


def _constraints_func(n_constraints: int) -> Any:
    def constraints(trial: Any) -> list[float]:
        values = trial.user_attrs.get("constraints")
        if isinstance(values, list) and len(values) == n_constraints:
            return [float(v) for v in values]
        return [1.0] * n_constraints  # unknown ⇒ infeasible

    return constraints


def build_sampler(spec: HpoSpec, constraints_func: Any = None, worker_index: int = 0) -> Any:
    """Optuna sampler of a plan (``constraints_func``, for Optuna < 5, by tpe and nsga2).

    A ``seed`` param is offset by ``worker_index`` so that parallel workers do not draw the
    same sequence of parameters.
    """
    optuna = _optuna()
    name = spec.sampler.name
    params = dict(spec.sampler.params)
    if params.get("seed") is not None:
        params["seed"] = int(params["seed"]) + int(worker_index)
    try:
        if name == "tpe":
            if constraints_func is not None:
                params.setdefault("constraints_func", constraints_func)
            return optuna.samplers.TPESampler(**params)
        if name == "random":
            return optuna.samplers.RandomSampler(**params)
        if name == "nsga2":
            if constraints_func is not None:
                params.setdefault("constraints_func", constraints_func)
            return optuna.samplers.NSGAIISampler(**params)
        if name == "cmaes":
            try:
                import cmaes  # noqa: F401
            except ImportError:
                raise RuntimeError("le sampler 'cmaes' nécessite le paquet Python 'cmaes' "
                                   "(non installé) : utilisez tpe ou installez-le") from None
            return optuna.samplers.CmaEsSampler(**params)
        if name == "grid":
            from wmlab.hpo.space import grid_search_space

            search_space = params.pop("search_space", None) or grid_search_space(spec.space)
            return optuna.samplers.GridSampler(search_space, **params)
    except TypeError as exc:
        raise ValueError(f"paramètres invalides pour le sampler '{name}' : {exc}") from None
    raise ValueError(f"sampler inconnu '{name}'")


def build_pruner(spec: HpoSpec) -> Any:
    """Optuna pruner of a plan (no pruning for multi-objective studies)."""
    optuna = _optuna()
    name = spec.pruner.name
    if spec.multi_objective or name == "none":
        return optuna.pruners.NopPruner()
    params = dict(spec.pruner.params)
    classes = {"asha": optuna.pruners.SuccessiveHalvingPruner,
               "hyperband": optuna.pruners.HyperbandPruner,
               "median": optuna.pruners.MedianPruner}
    try:
        return classes[name](**params)
    except TypeError as exc:
        raise ValueError(f"paramètres invalides pour le pruner '{name}' : {exc}") from None


def record_constraints(trial: Any, spec: HpoSpec, metrics: dict[str, float],
                       final: bool = False) -> None:
    """Store constraint values (user attr ``constraints``; Optuna constraints when final)."""
    if not spec.constraints:
        return
    values = constraint_values(spec, metrics)
    trial.set_user_attr("constraints", values)
    if final and native_constraints():
        for i, (c, value) in enumerate(zip(spec.constraints, values)):
            trial.set_constraint(f"c{i}:{c.metric}{c.op}{c.value:g}", value)


def constraint_values(spec: HpoSpec, metrics: dict[str, float]) -> list[float]:
    """Optuna constraint values (≤ 0 ⇔ satisfied) from the latest metrics."""
    out: list[float] = []
    for c in spec.constraints:
        value = metrics.get(c.metric)
        if value is None or not math.isfinite(value):
            out.append(1.0)
        elif c.op in (">=", ">"):
            out.append(c.value - value + (1e-12 if c.op == ">" else 0.0))
        else:
            out.append(value - c.value + (1e-12 if c.op == "<" else 0.0))
    return out


# -- study state ----------------------------------------------------------------------------
def _feasible(trial: Any) -> bool:
    values = trial.user_attrs.get("constraints")
    return not values or all(float(v) <= 0.0 for v in values)


def _dominates(a: list[float], b: list[float]) -> bool:
    return all(x >= y for x, y in zip(a, b)) and any(x > y for x, y in zip(a, b))


def compute_best(trials: list[Any], spec: HpoSpec) -> list[dict[str, Any]]:
    """Best trial (mono-objective) or Pareto front, feasible trials first."""
    complete = [t for t in trials if t.state.name == "COMPLETE" and t.values]
    pool = [t for t in complete if _feasible(t)] or complete
    if not pool:
        return []
    signs = [1.0 if o.direction == "maximize" else -1.0 for o in spec.objectives]

    def signed(t: Any) -> list[float]:
        return [s * float(v) for s, v in zip(signs, t.values)]

    if len(signs) == 1:
        front = [max(pool, key=lambda t: signed(t)[0])]
    else:
        front = [t for t in pool if not any(_dominates(signed(o), signed(t)) for o in pool
                                            if o is not t)]
    return [{"trial": t.number, "values": [float(v) for v in t.values],
             "params": decode_params(t.params, spec.space), "run_id": t.user_attrs.get("run_id")}
            for t in sorted(front, key=lambda t: t.number)]


def study_stats(study: Any, spec: HpoSpec) -> dict[str, Any]:
    """Trial counts and best trials, as stored in ``study.json``."""
    trials = study.get_trials(deepcopy=False)
    states = [t.state.name for t in trials]
    return {
        "n_trials": len(trials),
        "n_complete": states.count("COMPLETE"),
        "n_pruned": states.count("PRUNED"),
        "n_failed": states.count("FAIL"),
        "n_running": states.count("RUNNING"),
        "best": compute_best(trials, spec),
    }


def update_study(study_dir: Path, study: Any = None, spec: HpoSpec | None = None,
                 **fields: Any) -> dict[str, Any]:
    """Merge fields (and fresh stats) into ``study.json`` under an inter-process lock."""
    with _locked(study_dir):
        manifest = read_json(Path(study_dir) / "study.json", default={}) or {}
        manifest.update(fields)
        if study is not None and spec is not None:
            manifest.update(study_stats(study, spec))
        atomic_write_json(Path(study_dir) / "study.json", manifest)
    return manifest


def trial_row(trial: Any, spec: HpoSpec) -> dict[str, Any]:
    """``trials.jsonl`` row of a finished trial."""
    intermediate = trial.user_attrs.get("intermediate")
    if not isinstance(intermediate, list):
        intermediate = [[int(s), float(v)] for s, v in sorted(trial.intermediate_values.items())]
    row = {
        "number": trial.number,
        "state": trial.state.name,
        "params": decode_params(trial.params, spec.space),
        "values": [float(v) for v in trial.values] if trial.values else None,
        "intermediate": intermediate,
        "run_id": trial.user_attrs.get("run_id"),
        "duration_s": trial.duration.total_seconds() if trial.duration else None,
        "t": time.time(),
    }
    if spec.constraints:
        row["constraints"] = trial.user_attrs.get("constraints")
        row["feasible"] = _feasible(trial) if trial.user_attrs.get("constraints") else None
    if trial.user_attrs.get("error"):
        row["error"] = trial.user_attrs["error"]
    return row


def _study_overrides(raw: Any) -> dict[str, str]:
    """Base-experiment overrides of a study (``wmlab hpo --set``), as ``--set`` strings."""
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


# -- worker ---------------------------------------------------------------------------------
@dataclass
class StudyContext:
    """Everything a worker needs, read back from the study folder."""

    dir: Path
    study_id: str
    manifest: dict[str, Any]
    spec: HpoSpec
    experiment_path: Path
    experiment_name: str
    base_resolved: dict[str, Any]
    smoke: bool
    started_at: float
    overrides: dict[str, str]

    @classmethod
    def load(cls, study_dir: str | Path) -> "StudyContext":
        study_dir = Path(study_dir).expanduser().resolve()
        manifest = read_json(study_dir / "study.json")
        if not isinstance(manifest, dict):
            raise FileNotFoundError(f"study.json introuvable ou illisible dans {study_dir}")
        spec = load_yaml(study_dir / "spec.yaml", "hpo")
        experiment_path = resolve_in_workspace(manifest.get("experiment_path") or "")
        if not experiment_path.is_file():
            raise FileNotFoundError(f"expérience de base introuvable : {experiment_path}")
        overrides = _study_overrides(manifest.get("overrides"))
        exp_spec, _, base_resolved = load_experiment(experiment_path, overrides or None)
        return cls(dir=study_dir, study_id=str(manifest.get("study_id") or study_dir.name),
                   manifest=manifest, spec=spec, experiment_path=experiment_path,
                   experiment_name=exp_spec.name, base_resolved=base_resolved,
                   smoke=bool(manifest.get("smoke")),
                   started_at=parse_iso(manifest.get("started_at")) or time.time(),
                   overrides=overrides)

    def budget_hours(self) -> float | None:
        """Wall-clock budget in hours (``max_hours`` and ``max_eur / price``)."""
        budget = self.spec.budget
        limits = [budget.max_hours] if budget.max_hours else []
        price = float(self.manifest.get("price_eur_per_hour") or 0.0)
        if budget.max_eur is not None and price > 0:
            limits.append(budget.max_eur / price)
        return min(limits) if limits else None

    def remaining_hours(self) -> float | None:
        hours = self.budget_hours()
        if hours is None:
            return None
        return hours - (time.time() - self.started_at) / 3600.0


def budget_reason(study: Any, ctx: StudyContext) -> str | None:
    """Reason to stop launching trials, or None."""
    trials = study.get_trials(deepcopy=False)
    states = [t.state.name for t in trials]
    n_ok = states.count("COMPLETE") + states.count("PRUNED")
    n_failed = states.count("FAIL")
    max_trials = ctx.spec.budget.max_trials
    if max_trials is not None and n_ok + states.count("RUNNING") >= max_trials:
        return f"max_trials atteint ({n_ok}/{max_trials} essais terminés)"
    remaining = ctx.remaining_hours()
    if remaining is not None and remaining <= 0:
        return "budget de temps/coût de l'étude atteint (max_hours / max_eur)"
    if n_failed >= MIN_FAILURES_TO_STOP and n_failed > n_ok:
        return f"trop d'essais en échec ({n_failed} FAIL contre {n_ok} réussis)"
    if ctx.spec.sampler.name == "grid" and len(trials) >= grid_size(ctx.spec.space):
        return "grille entièrement explorée"
    return None


class _TimeBudgetCallback:
    """Stops the study once ``max_hours`` / ``max_eur`` is reached."""

    def __init__(self, ctx: StudyContext) -> None:
        self.ctx = ctx

    def __call__(self, study: Any, trial: Any) -> None:
        remaining = self.ctx.remaining_hours()
        if remaining is not None and remaining <= 0:
            study.stop()


class _Recorder:
    """Writes ``trials.jsonl``, ``study.json`` and events when a trial ends."""

    def __init__(self, ctx: StudyContext, worker_index: int) -> None:
        self.ctx = ctx
        self.worker_index = worker_index

    def __call__(self, study: Any, trial: Any) -> None:
        row = trial_row(trial, self.ctx.spec)
        append_jsonl(self.ctx.dir / "trials.jsonl", row)
        append_event(self.ctx.dir, "trial_end", number=trial.number, state=row["state"],
                     values=row["values"], run_id=row["run_id"], worker=self.worker_index)
        update_study(self.ctx.dir, study, self.ctx.spec)
        _write_heartbeat(self.ctx.dir)
        values = ", ".join(f"{v:.4g}" for v in row["values"] or []) or "—"
        print(f"[hpo] essai {trial.number} : {row['state']} (valeurs : {values}, run "
              f"{row['run_id']})", flush=True)


def _trial_total_steps(ctx: StudyContext, overrides: dict[str, str]) -> int | None:
    """Total steps of a trial's plan (None when the overridden experiment cannot be built)."""
    try:
        from wmlab.curriculum import PhasePlan
        from wmlab.spec import ExperimentSpec, apply_overrides

        resolved = apply_overrides(ctx.base_resolved, overrides)
        if ctx.smoke:
            from wmlab.train.smoke import apply_smoke

            resolved = apply_smoke(resolved, str(ctx.manifest.get("modality") or "image"))
        return int(PhasePlan(ExperimentSpec.model_validate(resolved)).total_steps)
    except Exception:
        return None


class _Objective:
    """Optuna objective: one training run per trial."""

    def __init__(self, ctx: StudyContext, worker_index: int, device: str | None) -> None:
        self.ctx = ctx
        self.worker_index = worker_index
        self.device = device

    def _aggregate(self, metric: str, direction: str, history: list[tuple[int, float]],
                   summary: dict[str, Any]) -> float:
        if self.ctx.spec.objective_agg == "best" and history:
            values = [v for _, v in history]
            return max(values) if direction == "maximize" else min(values)
        if history:
            return history[-1][1]
        value = summary.get(metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        raise ValueError(f"métrique objectif '{metric}' absente des métriques de validation du "
                         "run (clés val/… disponibles dans le résumé du run)")

    def __call__(self, trial: Any) -> Any:
        optuna = _optuna()
        from wmlab.train.trainer import train_from_spec

        ctx, spec = self.ctx, self.ctx.spec
        sampled = suggest(trial, spec.space)
        # study-level overrides first, then the sampled values and the fidelity proxy
        overrides = {**ctx.overrides, **trial_overrides(ctx.base_resolved, sampled, spec.fidelity)}
        run_id = new_run_id(ctx.experiment_name)
        trial.set_user_attr("run_id", run_id)
        trial.set_user_attr("worker", self.worker_index)
        append_jsonl(ctx.dir / "trials.jsonl", {
            "number": trial.number, "state": "RUNNING", "params": sampled, "values": None,
            "intermediate": [], "run_id": run_id, "duration_s": None, "t": time.time()})
        append_event(ctx.dir, "trial_start", number=trial.number, run_id=run_id,
                     params=sampled, worker=self.worker_index)
        print(f"[hpo] essai {trial.number} (worker {self.worker_index}) — run {run_id} — "
              f"{sampled}", flush=True)
        metrics = {o.metric for o in spec.objectives} | {c.metric for c in spec.constraints}
        history: dict[str, list[tuple[int, float]]] = {m: [] for m in metrics}
        latest: dict[str, float] = {}
        intermediate: list[list[float]] = []
        primary = spec.objectives[0].metric
        total_steps = _trial_total_steps(ctx, overrides)

        def on_validation(step: int, values: dict[str, float]) -> None:
            if not latest and values:  # first validation: fail now, not after a full training
                missing = sorted(m for m in metrics if m not in values)
                if missing:
                    raise ValueError(
                        f"métrique(s) {', '.join(missing)} absente(s) des métriques de "
                        f"validation du run (clés disponibles : {', '.join(sorted(values))}) "
                        "— corrigez objectives/constraints du plan (wmlab validate)")
            latest.update(values)
            for metric in metrics:
                if metric in values:
                    history[metric].append((step, float(values[metric])))
            record_constraints(trial, spec, latest)
            if primary not in values:
                return
            intermediate.append([step, float(values[primary])])
            trial.set_user_attr("intermediate", list(intermediate))
            if not spec.multi_objective:
                trial.report(float(values[primary]), step)
                # the final validation is not a pruning point: the run is already complete
                if (total_steps is None or step < total_steps) and trial.should_prune():
                    record_constraints(trial, spec, latest, final=True)
                    raise optuna.TrialPruned(f"essai élagué au step {step}")

        launch = ctx.dir / "launch.json"
        try:
            summary = train_from_spec(
                ctx.experiment_path, overrides=overrides, run_id=run_id, smoke=ctx.smoke,
                device=self.device, max_hours=ctx.remaining_hours(),
                launch_file=str(launch) if launch.is_file() else None, study=ctx.study_id,
                trial=trial.number, gpu_share=1.0 / spec.budget.parallel,
                trial_callback=on_validation)
        except optuna.TrialPruned:
            raise
        except KeyboardInterrupt:
            append_jsonl(ctx.dir / "trials.jsonl", {
                "number": trial.number, "state": "FAIL", "params": sampled, "values": None,
                "intermediate": intermediate, "run_id": run_id, "duration_s": None,
                "t": time.time(), "error": "interrompu (SIGTERM)"})
            raise
        except Exception as exc:
            trial.set_user_attr("error", f"{type(exc).__name__}: {exc}"[:2000])
            raise
        status = self._run_status(run_id)
        if status != "completed":
            message = f"le run {run_id} s'est terminé avec le statut « {status} »"
            trial.set_user_attr("error", message)
            raise RuntimeError(message)
        latest.update({k: v for k, v in summary.items() if k not in latest})
        record_constraints(trial, spec, latest, final=True)
        values = [self._aggregate(o.metric, o.direction, history[o.metric], summary)
                  for o in spec.objectives]
        return values[0] if len(values) == 1 else values

    @staticmethod
    def _run_status(run_id: str) -> str:
        try:
            return str(RunReader(paths.runs_dir() / run_id).raw_manifest().get("status"))
        except FileNotFoundError:
            return "inconnu"


def run_worker(study_dir: str | Path, *, worker_index: int, device: str | None = None) -> None:
    """Run trials of an existing study until its budget is exhausted."""
    optuna = _optuna()
    ctx = StudyContext.load(study_dir)
    spec = ctx.spec
    constraints = (_constraints_func(len(spec.constraints))
                   if spec.constraints and not native_constraints() else None)
    study = optuna.load_study(study_name=ctx.study_id, storage=_storage(ctx.dir),
                              sampler=build_sampler(spec, constraints, worker_index),
                              pruner=build_pruner(spec))
    device = device or ctx.manifest.get("device")
    objective = _Objective(ctx, worker_index, device)
    callbacks: list[Any] = [_TimeBudgetCallback(ctx), _Recorder(ctx, worker_index)]
    if spec.budget.max_trials is not None:
        states = (optuna.trial.TrialState.COMPLETE, optuna.trial.TrialState.PRUNED)
        callbacks.insert(0, optuna.study.MaxTrialsCallback(spec.budget.max_trials, states=states))
    append_event(ctx.dir, "worker", worker=worker_index, action="start", pid=os.getpid())
    print(f"[hpo] worker {worker_index} démarré (pid {os.getpid()})", flush=True)
    reason = None
    try:
        while True:
            reason = budget_reason(study, ctx)
            if reason:
                break
            study.optimize(objective, n_trials=1, callbacks=callbacks, catch=(Exception,),
                           gc_after_trial=True)
    finally:
        append_event(ctx.dir, "worker", worker=worker_index, action="stop", reason=reason)
    print(f"[hpo] worker {worker_index} arrêté : {reason}", flush=True)


# -- driver ---------------------------------------------------------------------------------
class _Driver:
    """Launches the workers, relays their output and writes the study heartbeat."""

    def __init__(self, study_dir: Path, spec: HpoSpec, device: str | None) -> None:
        self.dir = study_dir
        self.spec = spec
        self.device = device
        self.procs: list[subprocess.Popen[str]] = []
        self._relays: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._log = self._open_log()

    def _open_log(self) -> Any:
        log_path = self.dir / "stdout.log"
        with contextlib.suppress(Exception):  # no real file descriptor (e.g. captured stdout)
            if log_path.exists() and os.path.samestat(os.fstat(sys.stdout.fileno()),
                                                      os.stat(log_path)):
                return None  # the launcher already redirects our stdout to stdout.log
        try:
            return open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            return None

    def emit(self, text: str) -> None:
        """Write to our stdout and to the study ``stdout.log``."""
        if not text.endswith("\n"):
            text += "\n"
        with self._lock:
            with contextlib.suppress(Exception):
                sys.stdout.write(text)
                sys.stdout.flush()
            if self._log is not None:
                with contextlib.suppress(Exception):
                    self._log.write(text)
                    self._log.flush()

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_S):
            _write_heartbeat(self.dir)

    def _relay(self, proc: subprocess.Popen[str], index: int) -> None:
        prefix = f"[w{index}] " if self.spec.budget.parallel > 1 else ""
        assert proc.stdout is not None
        for line in proc.stdout:
            self.emit(prefix + line.rstrip("\n"))

    def _spawn(self, index: int) -> subprocess.Popen[str]:
        cmd = [sys.executable, "-m", "wmlab.cli", "hpo-worker", str(self.dir),
               "--worker-index", str(index)]
        if self.device:
            cmd += ["--device", self.device]
        env = dict(os.environ)
        env.update(WMLAB_WORKSPACE=str(paths.workspace_root()), WMLAB_DATA=str(paths.data_root()),
                   PYTHONUNBUFFERED="1")
        proc = subprocess.Popen(cmd, cwd=str(paths.workspace_root()), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                bufsize=1, errors="replace")
        relay = threading.Thread(target=self._relay, args=(proc, index), daemon=True,
                                 name=f"wmlab-hpo-relay-{index}")
        relay.start()
        self._relays.append(relay)
        return proc

    def _terminate(self) -> None:
        """Stop the workers: they usually got the signal too (process group), so give them
        time to checkpoint and exit before forwarding SIGTERM, then SIGKILL as a last resort."""
        deadline = time.time() + SPONTANEOUS_EXIT_S
        for proc in self.procs:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=max(deadline - time.time(), 0.1))
        for proc in self.procs:
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.send_signal(signal.SIGTERM)
        deadline = time.time() + TERMINATE_GRACE_S
        for proc in self.procs:
            try:
                proc.wait(timeout=max(deadline - time.time(), 0.1))
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
                proc.wait()

    def run(self) -> tuple[str, str | None]:
        """Run the workers to completion; return ``(status, error)``."""
        threading.Thread(target=self._heartbeat_loop, daemon=True,
                         name="wmlab-hpo-heartbeat").start()
        previous = _install_sigterm()
        status, error = "completed", None
        try:
            for index in range(self.spec.budget.parallel):
                self.procs.append(self._spawn(index))
                append_event(self.dir, "worker", worker=index, action="spawn",
                             pid=self.procs[-1].pid)
            codes = [proc.wait() for proc in self.procs]
            failed = [(i, c) for i, c in enumerate(codes) if c != 0]
            if failed:
                status = "failed"
                error = "worker(s) en échec : " + ", ".join(f"#{i} (code {c})" for i, c in failed)
        except KeyboardInterrupt as exc:
            _ignore_sigterm()  # a repeated signal must not interrupt the shutdown below
            status = "killed"
            error = "SIGTERM reçu" if isinstance(exc, StudyTerminated) \
                else "interrompu (KeyboardInterrupt)"
            self.emit(f"[hpo] arrêt demandé : {error} — arrêt des workers…")
            self._terminate()
        finally:
            _restore_sigterm(previous)
            self._stop.set()
            for relay in self._relays:  # flush the last lines of the workers
                relay.join(timeout=5.0)
        return status, error

    def close(self) -> None:
        if self._log is not None:
            with contextlib.suppress(Exception):
                self._log.close()


def _install_sigterm() -> Any:
    if threading.current_thread() is not threading.main_thread():
        return None

    def handler(signum: int, frame: Any) -> None:
        raise StudyTerminated("SIGTERM reçu")

    try:
        return signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        return None


def _ignore_sigterm() -> None:
    if threading.current_thread() is threading.main_thread():
        with contextlib.suppress(ValueError, OSError):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)


def _restore_sigterm(previous: Any) -> None:
    if previous is None or threading.current_thread() is not threading.main_thread():
        return
    with contextlib.suppress(ValueError, OSError, TypeError):
        signal.signal(signal.SIGTERM, previous)


def _read_launch(launch_file: str | None) -> dict[str, Any] | None:
    if not launch_file:
        return None
    data = read_json(Path(launch_file).expanduser())
    return data if isinstance(data, dict) else None


def metric_errors(spec: HpoSpec, base: Any, modality: str) -> list[dict[str, str]]:
    """Objectives/constraints that the trials would never log (a typo would otherwise fail
    every trial after its full training)."""
    from wmlab.spec.validate import experiment_val_keys

    keys = experiment_val_keys(base, modality)
    errors: list[dict[str, str]] = []
    for kind, items in (("objectives", spec.objectives), ("constraints", spec.constraints)):
        for i, item in enumerate(items):
            if item.metric not in keys:
                errors.append({"loc": f"{kind}.{i}.metric",
                               "msg": f"'{item.metric}' n'est pas une métrique produite par "
                                      f"l'expérience de base — disponibles : "
                                      f"{', '.join(sorted(keys))}"})
    return errors


def default_study_notes(spec: dict[str, Any], study_id: str) -> str:
    """Pre-filled ``notes.md`` of a study (plan, hypothesis, observations, conclusion)."""
    name = spec.get("name") or study_id
    description = str(spec.get("description") or "").strip()
    hypothesis = str(spec.get("hypothesis") or "").strip()
    objectives = ", ".join(f"{o.get('metric')} ({o.get('direction')})"
                           for o in spec.get("objectives") or [] if isinstance(o, dict))
    lines = [f"# {name} — {study_id}", ""]
    if description:
        lines += [description, ""]
    lines += [f"Expérience de base : {spec.get('experiment') or '?'} — objectifs : "
              f"{objectives or '?'}", "",
              "## Hypothèse", "", hypothesis or "_(à compléter)_", "",
              "## Observations", "", "", "## Conclusion", "", ""]
    return "\n".join(lines)


def prepare_study(hpo_spec_path: str | Path, *, study_id: str | None = None,
                  smoke: bool = False, device: str | None = None,
                  launch_file: str | None = None, overrides: dict[str, str] | None = None,
                  max_hours: float | None = None, max_trials: int | None = None) -> Path:
    """Validate a plan and create the study folder + Optuna journal (no worker started).

    ``overrides`` apply to the base experiment of every trial (before the search space and
    the fidelity proxy); ``max_hours`` / ``max_trials`` cap the corresponding budget limits.
    """
    optuna = _optuna()
    path = resolve_in_workspace(hpo_spec_path)
    spec: HpoSpec = load_yaml(path, "hpo")
    experiment_path = experiment_path_for_hpo(path, spec)
    if not experiment_path.is_file():
        raise SpecError([{"loc": "experiment", "msg": f"expérience de base introuvable : "
                                                      f"{_relative(experiment_path)}"}], path, "hpo")
    overrides = _study_overrides(overrides)
    exp_spec, project, base_resolved = load_experiment(experiment_path, overrides or None)
    errors = space_errors(spec.space, base_resolved)
    errors += metric_errors(spec, exp_spec, project.modality)
    if errors:
        raise SpecError(errors, path, "hpo")
    if smoke or max_hours is not None or max_trials is not None:
        data = spec.model_dump(mode="json")
        if smoke:
            data["budget"].update(max_trials=SMOKE_TRIALS, parallel=1)
        if max_hours is not None:
            if max_hours <= 0:
                raise ValueError("--max-hours doit être strictement positif")
            current = data["budget"].get("max_hours")
            data["budget"]["max_hours"] = (min(float(current), float(max_hours))
                                           if current else float(max_hours))
        if max_trials is not None and not smoke:
            if max_trials <= 0:
                raise ValueError("--max-trials doit être strictement positif")
            current = data["budget"].get("max_trials")
            data["budget"]["max_trials"] = (min(int(current), int(max_trials))
                                            if current else int(max_trials))
        spec = validate_data(data, "hpo", path)
    build_sampler(spec)  # fail early on unavailable samplers / invalid params
    build_pruner(spec)

    study_id = study_id or new_study_id(spec.name)
    study_dir = paths.studies_dir() / study_id
    previous = read_json(study_dir / "study.json")
    if isinstance(previous, dict) and previous.get("status") not in (None, "queued"):
        raise FileExistsError(f"l'étude {study_id} existe déjà (statut {previous.get('status')}) : "
                              "choisissez un autre identifiant")
    study_dir.mkdir(parents=True, exist_ok=True)
    launch_info = _read_launch(launch_file)
    if launch_info:
        atomic_write_json(study_dir / "launch.json", launch_info)
    now = utc_iso()
    manifest = {
        "format_version": FORMAT_VERSION, "kind": "hpo", "study_id": study_id,
        "project": project.name, "plan": spec.name, "experiment": exp_spec.name,
        "spec_path": _relative(path), "experiment_path": _relative(experiment_path),
        "modality": project.modality, "status": "running",
        "created_at": (previous or {}).get("created_at") or now, "started_at": now,
        "ended_at": None, "host": paths.current_host(),
        "price_eur_per_hour": paths.current_price(),
        "objectives": [o.model_dump(mode="json") for o in spec.objectives],
        "budget": spec.budget.model_dump(mode="json"), "objective_agg": spec.objective_agg,
        "n_trials": 0, "n_complete": 0, "n_pruned": 0, "n_failed": 0, "n_running": 0,
        "best": [], "error": None, "smoke": smoke, "device": device,
        "overrides": dict(overrides), "user": launch_user(launch_info),
    }
    atomic_write_text(study_dir / "spec.yaml", dump_yaml(spec.model_dump(mode="json")))
    atomic_write_json(study_dir / "study.json", manifest)
    if not (study_dir / "notes.md").exists():  # conclusions of the study (app / skill)
        atomic_write_text(study_dir / "notes.md",
                          default_study_notes(spec.model_dump(mode="json"), study_id))
    _write_heartbeat(study_dir)
    optuna.create_study(study_name=study_id, storage=_storage(study_dir), load_if_exists=True,
                        directions=[o.direction for o in spec.objectives])
    append_event(study_dir, "status", status="running")
    return study_dir


def finish_study(study_dir: Path, status: str, error: str | None = None) -> dict[str, Any]:
    """Write the final state of a study (status, counts, best) and return ``study.json``."""
    optuna = _optuna()
    ctx_spec: HpoSpec = load_yaml(Path(study_dir) / "spec.yaml", "hpo")
    manifest = read_json(Path(study_dir) / "study.json", default={}) or {}
    study_id = str(manifest.get("study_id") or Path(study_dir).name)
    study = optuna.load_study(study_name=study_id, storage=_storage(study_dir))
    stats = study_stats(study, ctx_spec)
    if status == "completed" and stats["n_trials"] and not stats["n_complete"] \
            and not stats["n_pruned"]:
        status, error = "failed", "aucun essai n'a abouti (voir trials.jsonl et les runs)"
    manifest = update_study(study_dir, study, ctx_spec, status=status, ended_at=utc_iso(),
                            error=error)
    append_event(study_dir, "status", status=status, **({"error": error} if error else {}))
    _write_heartbeat(study_dir)
    return manifest


def run_study(hpo_spec_path: str | Path, *, study_id: str | None = None, smoke: bool = False,
              device: str | None = None, launch_file: str | None = None,
              overrides: dict[str, str] | None = None, max_hours: float | None = None,
              max_trials: int | None = None) -> dict[str, Any]:
    """Create and run an HPO study; block until every worker exits; return a summary.

    ``overrides`` (``wmlab hpo --set``) apply to the base experiment of every trial;
    ``max_hours`` / ``max_trials`` cap the study budget. A study stopped by SIGTERM is
    finalized (status ``killed``) then ``StudyTerminated`` (a ``KeyboardInterrupt``) is raised.
    """
    study_dir = prepare_study(hpo_spec_path, study_id=study_id, smoke=smoke, device=device,
                              launch_file=launch_file, overrides=overrides, max_hours=max_hours,
                              max_trials=max_trials)
    manifest = read_json(study_dir / "study.json", default={}) or {}
    spec: HpoSpec = load_yaml(study_dir / "spec.yaml", "hpo")
    driver = _Driver(study_dir, spec, device)
    status, error = "failed", None
    try:
        driver.emit(f"Étude {study_dir.name} — plan {spec.name} ({manifest.get('project')}/"
                    f"{manifest.get('experiment')}) — {spec.budget.parallel} worker(s)"
                    + (" — mode smoke" if smoke else ""))
        driver.emit(f"Dossier : {study_dir}")
        status, error = driver.run()
    except BaseException:
        error = traceback.format_exc()
        raise
    finally:
        manifest = finish_study(study_dir, status, error)
        driver.emit(f"Étude {study_dir.name} terminée : {manifest.get('status')} — "
                    f"{manifest.get('n_complete')} complet(s), {manifest.get('n_pruned')} "
                    f"élagué(s), {manifest.get('n_failed')} en échec")
        driver.close()
    if manifest.get("status") == "killed":  # same exit path as an interrupted training run
        raise StudyTerminated(str(manifest.get("error") or "étude interrompue"))
    return {
        "study_id": study_dir.name, "status": manifest.get("status"),
        "study_dir": str(study_dir), "n_trials": manifest.get("n_trials"),
        "n_complete": manifest.get("n_complete"), "n_pruned": manifest.get("n_pruned"),
        "n_failed": manifest.get("n_failed"), "best": manifest.get("best"),
        "error": manifest.get("error"),
    }
