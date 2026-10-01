"""HPO: search space helpers, smoke study with worker subprocesses, pruning, Pareto front."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterator

import pytest

optuna = pytest.importorskip("optuna")

from helpers_engine import (  # noqa: E402
    experiment_dict,
    make_workspace,
    shim_env,
    shimmed,
    small_experiment,
)
from wmlab.hpo.driver import (  # noqa: E402
    StudyContext,
    budget_reason,
    constraint_values,
    finish_study,
    prepare_study,
    run_study,
    run_worker,
)
from wmlab.hpo.space import (  # noqa: E402
    grid_search_space,
    space_errors,
    suggest,
    to_override_value,
    trial_overrides,
)
from wmlab.spec import FidelitySpec, HpoSpec, ParamSpace, SpecError, parse_override_value  # noqa: E402
from wmlab.tracking import RunReader, StudyReader, read_jsonl  # noqa: E402

PLAN: dict[str, Any] = {
    "name": "sweep",
    "experiment": "small",
    "sampler": {"name": "random", "params": {"seed": 0}},
    "pruner": {"name": "median", "params": {"n_startup_trials": 1}},
    "objectives": [{"metric": "val/bit_acc_mean", "direction": "maximize"}],
    "constraints": [{"metric": "val/psnr", "op": ">=", "value": 10}],
    "space": {
        "optimizer.lr": {"type": "float", "low": 1.0e-4, "high": 1.0e-1, "log": True},
        "loss.terms.img.weight": {"type": "float", "low": 0.1, "high": 2.0},
        "phases.0.steps": {"type": "int", "low": 2, "high": 6, "step": 2},
        "model.params.mode": {"type": "categorical", "choices": ["perfect", "blind"]},
    },
    "fidelity": {"steps_scale": 0.5},
    "budget": {"max_trials": 10, "parallel": 2},
}


def plan(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(PLAN)
    data.update(changes)
    return data


@pytest.fixture(autouse=True)
def _ml_shims() -> Iterator[None]:
    """Stand-ins for ML modules that are not importable yet (removed after each test)."""
    with shimmed():
        yield


@pytest.fixture
def hpo_ws(workspace: Path) -> Path:
    prune = plan(name="prune", sampler={"name": "tpe", "params": {"seed": 0}},
                 constraints=[], space={"model.params.mode": PLAN["space"]["model.params.mode"]},
                 fidelity={}, budget={"max_trials": 2, "parallel": 1})
    multi = plan(name="multi", objectives=[{"metric": "val/psnr", "direction": "maximize"},
                                           {"metric": "val/bit_acc_mean",
                                            "direction": "maximize"}],
                 constraints=[{"metric": "val/psnr", "op": ">=", "value": 1000}],
                 space=prune["space"], fidelity={}, objective_agg="best",
                 budget={"max_trials": 2, "parallel": 1})
    make_workspace(workspace, experiments=[small_experiment()], hpo=[plan(), prune, multi])
    return workspace


def _hpo_path(name: str) -> str:
    return f"projects/eng/hpo/{name}.yaml"


# -- search space ---------------------------------------------------------------------------
def test_space_helpers():
    space = {k: ParamSpace.model_validate(v) for k, v in PLAN["space"].items()}
    trial = optuna.trial.FixedTrial({"optimizer.lr": 0.001, "loss.terms.img.weight": 0.5,
                                     "phases.0.steps": 4, "model.params.mode": "blind"})
    sampled = suggest(trial, space)
    assert sampled == {"optimizer.lr": 0.001, "loss.terms.img.weight": 0.5,
                       "phases.0.steps": 4, "model.params.mode": "blind"}
    base = small_experiment()
    overrides = trial_overrides(base, sampled, FidelitySpec(steps_scale=0.25,
                                                            overrides={"seed": 3}))
    assert overrides["phases.0.steps"] == "1"   # round(4 × 0.25)
    assert overrides["phases.1.steps"] == "2"   # round(6 × 0.25), at least 1
    assert overrides["seed"] == "3" and overrides["model.params.mode"] == "blind"
    assert "phases.0.lr.warmup_steps" not in overrides  # no warmup in the base: nothing to scale
    assert parse_override_value(overrides["optimizer.lr"]) == 0.001
    # steps_scale also shortens the LR warmups (else the warmup outlasts its shortened phase)
    with_warmup = experiment_dict()  # phase 0: 25 steps, warmup 30
    scaled = trial_overrides(with_warmup, {}, FidelitySpec(steps_scale=0.2))
    assert scaled["phases.0.steps"] == "5" and scaled["phases.0.lr.warmup_steps"] == "6"
    assert "phases.1.lr.warmup_steps" not in scaled
    explicit = trial_overrides(with_warmup, {}, FidelitySpec(
        steps_scale=0.2, overrides={"phases.0.lr.warmup_steps": 2}))
    assert explicit["phases.0.lr.warmup_steps"] == "2"  # fidelity.overrides win
    for value in (1e-05, 0.5, 3, True, None, "adamw", "true", [1, 2], {"a": 1}):
        assert parse_override_value(to_override_value(value)) == value
    grid = grid_search_space({"a": ParamSpace(type="float", low=0.1, high=0.3, step=0.1),
                              "b": ParamSpace(type="int", low=1, high=5, step=2),
                              "c": ParamSpace(type="categorical", choices=[[1], [2]])})
    assert grid == {"a": [0.1, 0.2, 0.3], "b": [1, 3, 5], "c": ["[1]", "[2]"]}
    with pytest.raises(ValueError, match="step"):
        grid_search_space({"x": ParamSpace(type="float", low=0.0, high=1.0)})
    errors = space_errors({"optimizer.nope.lr": space["optimizer.lr"],
                           "phases.9.steps": space["phases.0.steps"]}, base)
    assert [e["loc"] for e in errors] == ["space.optimizer.nope.lr", "space.phases.9.steps"]


def test_constraint_values():
    spec = HpoSpec.model_validate(plan(constraints=[
        {"metric": "val/psnr", "op": ">=", "value": 30},
        {"metric": "val/loss", "op": "<", "value": 1.0},
        {"metric": "val/missing", "op": ">", "value": 0}]))
    values = constraint_values(spec, {"val/psnr": 35.0, "val/loss": 2.0})
    assert values[0] == pytest.approx(-5.0) and values[1] > 0 and values[2] == 1.0


def test_invalid_plan_creates_no_study(hpo_ws: Path):
    bad = plan(name="bad", space={"optimizer.nope": {"type": "float", "low": 0, "high": 1}})
    path = hpo_ws / "projects" / "eng" / "hpo" / "bad.yaml"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(SpecError, match="optimizer.nope"):
        run_study(path)
    assert not any((hpo_ws / ".wmlab" / "studies").glob("*/study.json"))


def test_unknown_metric_fails_before_training(hpo_ws: Path):
    """A typo in the objective is caught when the study is prepared (no trial trained)."""
    bad = plan(name="typo", objectives=[{"metric": "val/bit_acc/jpg50", "direction": "maximize"}])
    path = hpo_ws / "projects" / "eng" / "hpo" / "typo.yaml"
    path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(SpecError, match="val/bit_acc/jpg50"):
        run_study(path)
    assert not any((hpo_ws / ".wmlab" / "studies").glob("*/study.json"))
    assert not any((hpo_ws / ".wmlab" / "runs").glob("*/run.json"))


# -- smoke study with worker subprocesses ---------------------------------------------------
def test_smoke_study_subprocess(hpo_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key, value in shim_env(tmp_path).items():
        monkeypatch.setenv(key, value)
    result = run_study(_hpo_path("sweep"), study_id="study-smoke", smoke=True, device="cpu")
    assert result["status"] == "completed", result
    assert result["n_trials"] == 3 and result["n_complete"] + result["n_pruned"] == 3
    study_dir = hpo_ws / ".wmlab" / "studies" / "study-smoke"
    assert result["study_dir"] == str(study_dir)
    reader = StudyReader(study_dir)
    manifest = reader.manifest()
    assert manifest["kind"] == "hpo" and manifest["status"] == "completed"
    assert manifest["project"] == "eng" and manifest["plan"] == "sweep"
    assert manifest["experiment"] == "small" and manifest["smoke"] is True
    assert manifest["spec_path"] == "projects/eng/hpo/sweep.yaml"
    assert manifest["budget"]["max_trials"] == 3 and manifest["budget"]["parallel"] == 1
    assert manifest["objectives"] == [{"metric": "val/bit_acc_mean", "direction": "maximize"}]
    assert manifest["ended_at"] and manifest["n_trials"] == 3
    assert (study_dir / "optuna.journal").is_file() and (study_dir / "heartbeat").is_file()
    assert reader.spec()["budget"]["max_trials"] == 3

    trials = reader.trials()
    assert [t["number"] for t in trials] == [0, 1, 2]
    for trial in trials:
        assert trial["state"] in ("COMPLETE", "PRUNED")
        assert set(trial["params"]) == set(PLAN["space"])
        assert trial["intermediate"] and trial["duration_s"] > 0
        run = RunReader(hpo_ws / ".wmlab" / "runs" / trial["run_id"]).manifest()
        assert run["study"] == "study-smoke" and run["trial"] == trial["number"]
        assert run["gpu_share"] == 1.0 and "smoke" in run["tags"]
        assert run["params"]["model.params.mode"] == trial["params"]["model.params.mode"]
        assert run["overrides"]["phases.1.steps"] == "3"   # fidelity: 6 × 0.5
        if trial["state"] == "COMPLETE":
            assert run["status"] == "completed"
            assert trial["values"] == [run["summary"]["val/bit_acc_mean"]]
            assert trial["intermediate"][-1][1] == trial["values"][0]
            assert trial["feasible"] is True
        else:
            assert run["status"] == "killed" and run["error"] == "pruned"
    rows = read_jsonl(study_dir / "trials.jsonl")
    assert [r["state"] for r in rows].count("RUNNING") == 3
    best = manifest["best"]
    assert len(best) == 1 and best[0]["run_id"]
    complete = [t for t in trials if t["state"] == "COMPLETE"]
    assert best[0]["values"][0] == max(t["values"][0] for t in complete)
    events = reader.events()
    assert [e["status"] for e in events if e["type"] == "status"] == ["running", "completed"]
    assert sum(1 for e in events if e["type"] == "trial_end") == 3
    log = (study_dir / "stdout.log").read_text()
    assert "[hpo] essai 0" in log and "worker 0" in log


# -- in-process worker: pruning and Pareto front --------------------------------------------
def _enqueue(study_dir: Path, *modes: str) -> None:
    ctx = StudyContext.load(study_dir)
    from wmlab.hpo.driver import _storage

    study = optuna.load_study(study_name=ctx.study_id, storage=_storage(study_dir))
    for mode in modes:
        study.enqueue_trial({"model.params.mode": mode})


def test_worker_prunes_bad_trial(hpo_ws: Path):
    study_dir = prepare_study(_hpo_path("prune"), study_id="study-prune", device="cpu")
    _enqueue(study_dir, "perfect", "blind")
    run_worker(study_dir, worker_index=0, device="cpu")
    trials = StudyReader(study_dir).trials()
    assert [(t["number"], t["state"]) for t in trials] == [(0, "COMPLETE"), (1, "PRUNED")]
    assert trials[0]["values"] == [1.0]
    assert trials[1]["intermediate"][0][0] == 4 and trials[1]["intermediate"][0][1] < 1.0
    # Optuna keeps the last reported value of a pruned trial
    assert trials[1]["values"] == [trials[1]["intermediate"][-1][1]]
    pruned_run = RunReader(hpo_ws / ".wmlab" / "runs" / trials[1]["run_id"]).manifest()
    assert pruned_run["status"] == "killed" and pruned_run["error"] == "pruned"
    manifest = finish_study(study_dir, "completed")
    assert manifest["status"] == "completed"
    assert (manifest["n_complete"], manifest["n_pruned"], manifest["n_failed"]) == (1, 1, 0)
    assert manifest["best"] == [{"trial": 0, "values": [1.0],
                                 "params": {"model.params.mode": "perfect"},
                                 "run_id": trials[0]["run_id"]}]
    ctx = StudyContext.load(study_dir)
    from wmlab.hpo.driver import _storage

    study = optuna.load_study(study_name=ctx.study_id, storage=_storage(study_dir))
    assert budget_reason(study, ctx).startswith("max_trials")


def test_study_overrides_and_max_hours(hpo_ws: Path):
    """`wmlab hpo --set k=v --max-hours H`: overrides reach every trial, budget is capped."""
    study_dir = prepare_study(_hpo_path("prune"), study_id="study-set", device="cpu",
                              overrides={"seed": "7", "phases.1.steps": "3"}, max_hours=0.25)
    manifest = StudyReader(study_dir).manifest()
    assert manifest["overrides"] == {"seed": "7", "phases.1.steps": "3"}
    assert manifest["budget"]["max_hours"] == 0.25
    reader = StudyReader(study_dir)
    assert reader.notes().startswith("# prune — study-set") and "## Conclusion" in reader.notes()
    assert "val/bit_acc_mean (maximize)" in reader.notes()
    reader.set_notes("conclusion")
    assert reader.notes() == "conclusion"
    ctx = StudyContext.load(study_dir)
    assert ctx.overrides == {"seed": "7", "phases.1.steps": "3"}
    assert ctx.base_resolved["seed"] == 7 and ctx.base_resolved["phases"][1]["steps"] == 3
    _enqueue(study_dir, "perfect")
    run_worker(study_dir, worker_index=0, device="cpu")
    trial = StudyReader(study_dir).trials()[0]
    run = RunReader(hpo_ws / ".wmlab" / "runs" / trial["run_id"]).manifest()
    assert run["overrides"]["seed"] == "7" and run["overrides"]["phases.1.steps"] == "3"
    assert run["overrides"]["model.params.mode"] == "perfect"  # sampled value still applied
    assert run["params"]["seed"] == 7
    with pytest.raises(Exception, match="nope"):
        prepare_study(_hpo_path("prune"), study_id="study-bad-set", device="cpu",
                      overrides={"optimizer.nope": "1"})
    assert not (hpo_ws / ".wmlab" / "studies" / "study-bad-set").exists()


def test_worker_multi_objective_pareto(hpo_ws: Path):
    study_dir = prepare_study(_hpo_path("multi"), study_id="study-multi", device="cpu")
    _enqueue(study_dir, "perfect", "blind")
    run_worker(study_dir, worker_index=0, device="cpu")
    trials = StudyReader(study_dir).trials()
    assert [t["state"] for t in trials] == ["COMPLETE", "COMPLETE"]  # no pruning
    for trial in trials:
        assert len(trial["values"]) == 2 and trial["feasible"] is False
        # objective_agg: best ⇒ best value over the validations of the run (intermediate
        # values track the first objective, val/psnr)
        assert trial["values"][0] == max(v for _, v in trial["intermediate"])
    from wmlab.hpo.driver import _storage, native_constraints

    if native_constraints():  # Optuna ≥ 5: constraints stored with Trial.set_constraint
        study = optuna.load_study(study_name="study-multi", storage=_storage(study_dir))
        for frozen in study.trials:
            assert list(frozen.constraints) == ["c0:val/psnr>=1000"]
            assert frozen.constraints["c0:val/psnr>=1000"] > 0
    manifest = finish_study(study_dir, "completed")
    # both infeasible ⇒ fallback on all complete trials; perfect/blind trade PSNR for bits
    assert sorted(b["trial"] for b in manifest["best"]) == [0, 1]


def test_sigterm_kills_study(hpo_ws: Path, tmp_path: Path):
    import os
    import signal
    import subprocess
    import sys
    import time

    from helpers_engine import write_yaml

    long = small_experiment(name="long", steps=(200000, 10))
    long["eval"]["every_steps"] = 100000
    write_yaml(hpo_ws / "projects" / "eng" / "experiments" / "long.yaml", long)
    write_yaml(hpo_ws / "projects" / "eng" / "hpo" / "longplan.yaml",
               plan(name="longplan", experiment="long", constraints=[], fidelity={},
                    space={"optimizer.lr": PLAN["space"]["optimizer.lr"]},
                    budget={"max_trials": 3, "parallel": 1}))
    env = {**os.environ, **shim_env(tmp_path)}
    proc = subprocess.Popen([sys.executable, "-m", "wmlab.cli", "hpo", _hpo_path("longplan"),
                             "--study-id", "sig-study", "--device", "cpu"], cwd=hpo_ws, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    study_dir = hpo_ws / ".wmlab" / "studies" / "sig-study"
    deadline = time.time() + 90
    try:
        while time.time() < deadline and proc.poll() is None:
            rows = read_jsonl(study_dir / "trials.jsonl")
            if rows and any("train/loss" in r["values"] for r in read_jsonl(
                    hpo_ws / ".wmlab" / "runs" / rows[0]["run_id"] / "metrics.jsonl")):
                break
            time.sleep(0.2)
        assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
        os.killpg(proc.pid, signal.SIGTERM)  # like the job executors (whole process group)
        output, _ = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert proc.returncode != 0, output
    manifest = StudyReader(study_dir).manifest()
    assert manifest["status"] == "killed" and manifest["error"] == "SIGTERM reçu", output
    trials = StudyReader(study_dir).trials()
    assert [t["state"] for t in trials] == ["FAIL"] and manifest["n_failed"] == 1
    run = RunReader(hpo_ws / ".wmlab" / "runs" / trials[0]["run_id"])
    assert run.manifest()["status"] == "killed"
    checkpoints = run.checkpoints()
    assert len(checkpoints) == 1 and "last" in checkpoints[0]["tags"]  # emergency checkpoint
