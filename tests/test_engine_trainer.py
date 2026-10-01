"""Trainer: phases, curriculum values, validation, checkpoints, branching, watchdog, failures."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterator

import pytest
import torch

from helpers_engine import experiment_dict, make_workspace, shimmed, small_experiment
from wmlab.curriculum import lr_factor, resolve_value
from wmlab.spec import LRScheduleSpec, SpecError
from wmlab.tracking import RunReader, list_runs, read_jsonl
from wmlab.train.smoke import apply_smoke
from wmlab.train.trainer import train_from_spec

SPEC = "projects/eng/experiments/{}.yaml"


@pytest.fixture(autouse=True)
def _ml_shims() -> Iterator[None]:
    """Stand-ins for ML modules that are not importable yet (removed after each test)."""
    with shimmed():
        yield


@pytest.fixture
def eng(workspace: Path) -> Path:
    make_workspace(workspace, experiments=[experiment_dict(), small_experiment()])
    return workspace


def _only_run(workspace: Path) -> RunReader:
    runs = list_runs(workspace / ".wmlab" / "runs")
    assert len(runs) == 1
    return RunReader(runs[0])


def _rows(reader: RunReader, key: str) -> dict[int, dict]:
    return {r["step"]: r for r in read_jsonl(reader.dir / "metrics.jsonl") if key in r["values"]}


def test_curriculum_run(eng: Path):
    summary = train_from_spec(SPEC.format("curriculum"), device="cpu")
    reader = _only_run(eng)
    manifest = reader.manifest()
    assert manifest["status"] == "completed" and manifest["error"] is None
    assert manifest["spec_path"] == "projects/eng/experiments/curriculum.yaml"
    assert manifest["progress"] == {"step": 55, "total_steps": 55, "phase": "robust",
                                    "phase_index": 1, "n_phases": 2}
    assert set(manifest["datasets"]) == {"train", "val"}
    assert all(len(v) == 12 for v in manifest["datasets"].values())
    assert manifest["params"]["optimizer.lr"] == 0.01 and manifest["params"]["phases.1.steps"] == 30

    # train/* logged every 20 steps and at each phase end, with the curriculum values of
    # the step just computed (index = step - 1)
    train = _rows(reader, "train/lr")
    assert sorted(train) == [20, 25, 40, 55]
    assert train[20]["phase"] == "warmup" and train[40]["phase"] == "robust"
    warm = LRScheduleSpec(schedule="linear_warmup", warmup_steps=30)
    cosine = LRScheduleSpec(schedule="cosine", min_factor=0.1)
    expected_lr = {20: lr_factor(warm, 19, 25), 25: lr_factor(warm, 24, 25),
                   40: lr_factor(cosine, 14, 30), 55: lr_factor(cosine, 29, 30)}
    ramp = {"ramp": [0.1, 1.0]}
    expected_img = {20: 0.1, 25: 0.1, 40: resolve_value(ramp, 14 / 29), 55: 1.0}
    expected_strength = {20: 1.0, 25: 1.0, 40: 14 / 29, 55: 1.0}
    for step, row in train.items():
        values = row["values"]
        assert values["train/lr"] == pytest.approx(0.01 * expected_lr[step])
        assert values["train/weight/img"] == pytest.approx(expected_img[step])
        assert values["train/weight/msg"] == pytest.approx(1.0)
        assert values["train/attack_strength"] == pytest.approx(expected_strength[step])
        for key in ("train/loss", "train/loss/msg", "train/loss/img", "train/bit_acc",
                    "train/step_time_s", "train/data_wait_frac", "train/items_per_s"):
            assert math.isfinite(values[key]), key
        assert 0.0 <= values["train/data_wait_frac"] <= 1.0
    assert expected_lr[20] == pytest.approx(20 / 30) and expected_lr[55] == pytest.approx(0.1)

    # validation every 10 steps and at phase ends
    val = _rows(reader, "val/bit_acc_mean")
    assert sorted(val) == [10, 20, 25, 30, 40, 50, 55]
    keys = set(val[55]["values"])
    assert {"val/loss", "val/psnr", "val/ssim", "val/bit_acc", "val/bit_acc/noise",
            "val/bit_acc/quant", "val/bit_acc_mean"} <= keys
    last = val[55]["values"]
    assert last["val/bit_acc_mean"] == pytest.approx(
        (last["val/bit_acc/noise"] + last["val/bit_acc/quant"]) / 2)
    assert summary["step"] == 55 and summary["val/bit_acc_mean"] == last["val/bit_acc_mean"]
    assert summary["best/val/bit_acc_mean"] == max(r["values"]["val/bit_acc_mean"]
                                                  for r in val.values())

    # checkpoints: tags, retention (keep_last=1 + best + phase ends), metrics = last validation
    index = {c["name"]: c for c in reader.checkpoints()}
    assert index["step_000025"]["tags"] == ["phase_end:warmup"]
    assert set(index["step_000055"]["tags"]) >= {"phase_end:robust", "last"}
    best = [c for c in index.values() if "best:val/bit_acc_mean" in c["tags"]]
    assert len(best) == 1
    assert set(index) == {"step_000025", "step_000055", best[0]["name"]}
    assert all(c["available_locally"] for c in index.values())
    assert index["step_000025"]["metrics"]["val/bit_acc_mean"] == \
        val[25]["values"]["val/bit_acc_mean"]
    files = {p.name for p in (reader.dir / "checkpoints").glob("*.pt")}
    assert files == {f"{name}.pt" for name in index}
    events = reader.events()
    deleted = {e["name"] for e in events if e["type"] == "checkpoint_deleted"}
    assert deleted and not deleted & set(index)

    ckpt = torch.load(reader.dir / "checkpoints" / "step_000055.pt", weights_only=False)
    assert ckpt["format"] == "wmlab-ckpt-v1" and ckpt["step"] == 55 and ckpt["phase"] == "robust"
    assert ckpt["model_name"] == "_eng_model" and ckpt["nbits"] == 16
    assert ckpt["modality"] == "image" and ckpt["model_params"] == {"hidden": 8}
    assert ckpt["metrics"]["val/bit_acc_mean"] == last["val/bit_acc_mean"]
    assert ckpt["spec"]["phases"][1]["name"] == "robust"
    # decoder frozen in phase "robust", optimizer reset at its start
    first = torch.load(reader.dir / "checkpoints" / "step_000025.pt", weights_only=False)
    assert torch.equal(first["model"]["decoder.weight"], ckpt["model"]["decoder.weight"])
    assert not torch.equal(first["model"]["encoder.0.weight"], ckpt["model"]["encoder.0.weight"])
    state = ckpt["optimizer"]["state"]
    assert float(state[0]["step"]) == 30 and 4 not in state
    assert [g["name"] for g in ckpt["optimizer"]["param_groups"]] == ["encoder", "decoder"]

    # events, samples, logs
    phases = [(e["type"], e["phase"], e["step"]) for e in events
              if e["type"] in ("phase_start", "phase_end")]
    assert phases == [("phase_start", "warmup", 0), ("phase_end", "warmup", 25),
                      ("phase_start", "robust", 25), ("phase_end", "robust", 55)]
    statuses = [e["status"] for e in events if e["type"] == "status"]
    assert statuses == ["running", "completed"]
    samples = reader.samples()
    assert {s["step"] for s in samples} == {20, 40, 55}
    assert {s["group"] for s in samples} == {"original", "watermarked", "diff"}
    assert len(samples) == 3 * 3 * 2
    assert all((reader.dir / s["path"]).is_file() for s in samples)
    log = (reader.dir / "stdout.log").read_text()
    assert "batch_size=2" in log and "step 55/55" in log and "validation step 10" in log
    assert (reader.dir / "spec.yaml").is_file() and (reader.dir / "env").is_dir()


def test_init_from_start_phase(eng: Path):
    parent_summary = train_from_spec(SPEC.format("small"), device="cpu", run_id="parent-run")
    assert parent_summary["step"] == 12
    parent_dir = eng / ".wmlab" / "runs" / "parent-run"
    parent_ckpt = torch.load(parent_dir / "checkpoints" / "step_000006.pt", weights_only=False)

    overrides = {"init_from": json.dumps({"run": "parent-run", "checkpoint": "phase_end:warmup",
                                          "start_phase": "robust", "load_optimizer": True})}
    seen: list[int] = []
    train_from_spec(SPEC.format("small"), overrides=overrides, device="cpu", run_id="child-run",
                    trial_callback=lambda step, values: seen.append(step))
    child = RunReader(eng / ".wmlab" / "runs" / "child-run")
    manifest = child.manifest()
    assert manifest["status"] == "completed"
    assert manifest["parent"]["run_id"] == "parent-run"
    assert manifest["parent"]["checkpoint"] == "step_000006"
    assert manifest["parent"]["start_phase"] == "robust"
    steps = sorted({r["step"] for r in read_jsonl(child.dir / "metrics.jsonl")})
    assert steps[0] > 6 and steps[-1] == 12
    assert seen == [8, 12]
    starts = [(e["phase"], e["step"]) for e in child.events() if e["type"] == "phase_start"]
    assert starts == [("robust", 6)]
    assert {c["name"] for c in child.checkpoints()} >= {"step_000012"}
    assert "step_000006" not in {c["name"] for c in child.checkpoints()}
    # the branch really started from the parent weights: a zero-LR copy keeps them
    frozen = {"init_from": json.dumps({"run": "parent-run", "checkpoint": "step_000006",
                                        "start_phase": 1}), "optimizer.lr": "1e-12",
              "optimizer.weight_decay": "0"}
    train_from_spec(SPEC.format("small"), overrides=frozen, device="cpu", run_id="copy-run")
    copy_ckpt = torch.load(eng / ".wmlab/runs/copy-run/checkpoints/step_000012.pt",
                           weights_only=False)
    for key, value in parent_ckpt["model"].items():
        assert torch.allclose(value, copy_ckpt["model"][key], atol=1e-6), key


def test_best_checkpoint_measured_at_its_own_step(workspace: Path):
    """checkpoint.every_steps (4) not aligned with eval.every_steps (3): a periodic checkpoint
    never carries the metrics of another step, and each improving validation gets a
    checkpoint so that ``best:<metric>`` designates the weights that were measured."""
    exp = small_experiment(name="misaligned", steps=(6, 6))
    exp["eval"]["every_steps"] = 3
    exp["checkpoint"] = {"every_steps": 4, "keep_last": 1,
                         "keep_best": {"val/bit_acc_mean": "max", "val/psnr": "max"},
                         "at_phase_end": False}
    make_workspace(workspace, experiments=[exp])
    summary = train_from_spec(SPEC.format("misaligned"), device="cpu")
    reader = _only_run(workspace)
    val = _rows(reader, "val/bit_acc_mean")
    assert sorted(val) == [3, 6, 9, 12]
    index = {c["name"]: c for c in reader.checkpoints()}
    events = [e for e in reader.events() if e["type"] == "checkpoint"]
    saved = {e["step"]: e for e in events}
    # periodic checkpoints (4, 8) were written without validation metrics
    for step in (4, 8):
        assert step in saved and saved[step]["step"] == step
    for entry in index.values():
        if entry["step"] % 3:
            assert entry["metrics"] == {}, entry
    for metric in ("val/bit_acc_mean", "val/psnr"):
        best = [c for c in index.values() if f"best:{metric}" in c["tags"]]
        assert len(best) == 1, metric
        step = best[0]["step"]
        assert step in val and best[0]["metrics"][metric] == val[step]["values"][metric]
        assert summary[f"best/{metric}"] == pytest.approx(best[0]["metrics"][metric])
    files = {p.name for p in (reader.dir / "checkpoints").glob("*.pt")}
    assert files == {f"{name}.pt" for name in index}


def test_validation_draws_are_seeded(workspace: Path):
    """The attacks of a validation use their own RNG state: whatever happened to the global
    RNG before, two validations of the same weights give the same numbers."""
    from wmlab.registry import load_plugins
    from wmlab.spec import load_experiment
    from wmlab.tracking import Run
    from wmlab.train.trainer import Trainer

    make_workspace(workspace, experiments=[small_experiment()])
    load_plugins()
    spec, project, resolved = load_experiment(SPEC.format("small"))
    run = Run.create(project="eng", experiment="small", modality="image", spec=resolved,
                     capture_env=False, monitor_system=False)
    try:
        trainer = Trainer(spec, project, resolved, run, device=torch.device("cpu"))
        trainer.setup()
        cur = trainer.plan.locate(0)
        outer = torch.rand(3)
        first = trainer.validate(cur, 0)
        torch.rand(1000)  # global RNG moves on (training draws, other attacks…)
        second = trainer.validate(cur, 0)
        assert first == second and first["val/bit_acc/noise"] == second["val/bit_acc/noise"]
        # the forked state does not disturb the training stream either
        torch.manual_seed(0)
        expected = torch.rand(6)
        torch.manual_seed(0)
        torch.rand(3)
        trainer.validate(cur, 0)
        assert torch.equal(torch.rand(3), expected[3:])
        del outer
    finally:
        run.finish("completed")


def test_bit_acc_mean_excludes_identity_and_warns_without_attacks(workspace: Path):
    with_identity = small_experiment(name="ident")
    with_identity["eval"]["val_attacks"] = [{"label": "none", "name": "identity"},
                                            {"label": "quant", "name": "_eng_quant"}]
    none = small_experiment(name="noattack")
    none["eval"]["val_attacks"] = []
    make_workspace(workspace, experiments=[with_identity, none])
    train_from_spec(SPEC.format("ident"), device="cpu", run_id="ident")
    reader = RunReader(workspace / ".wmlab" / "runs" / "ident")
    rows = _rows(reader, "val/bit_acc_mean")
    for row in rows.values():
        values = row["values"]
        assert values["val/bit_acc_mean"] == pytest.approx(values["val/bit_acc/quant"])
        assert "val/bit_acc/none" in values
    assert not [e for e in reader.events() if e["type"] == "warning"
                and "val_attacks" in e["message"]]
    train_from_spec(SPEC.format("noattack"), device="cpu", run_id="noattack")
    reader = RunReader(workspace / ".wmlab" / "runs" / "noattack")
    warnings = [e["message"] for e in reader.events() if e["type"] == "warning"]
    assert any("val_attacks" in w and "val/bit_acc_mean" in w for w in warnings)
    for row in _rows(reader, "val/bit_acc_mean").values():
        assert row["values"]["val/bit_acc_mean"] == row["values"]["val/bit_acc"]


def test_fp16_uses_grad_scaler(workspace: Path):
    from wmlab.train import runtime

    if not torch.backends.mps.is_available() and not torch.cuda.is_available():
        pytest.skip("fp16 autocast needs a GPU (mps/cuda)")
    device = runtime.resolve_device("auto")
    assert runtime.make_grad_scaler(device, None) is None
    assert runtime.make_grad_scaler(device, torch.bfloat16) is None
    scaler = runtime.make_grad_scaler(device, torch.float16)
    assert scaler is not None and scaler.is_enabled()
    exp = small_experiment(name="half", resources={"device": device.type, "precision": "fp16"})
    make_workspace(workspace, experiments=[exp])
    summary = train_from_spec(SPEC.format("half"))
    reader = _only_run(workspace)
    assert reader.manifest()["status"] == "completed" and summary["step"] == 12
    assert "GradScaler" in "\n".join(reader.logs())
    assert all(math.isfinite(r["values"]["train/loss"]) for r in _rows(reader, "train/loss").values())


def test_resume_continues_at_checkpoint_step(eng: Path):
    """``init_from.resume: true`` restarts the curriculum at the checkpoint's own step (no
    re-warmup, optimizer state loaded), e.g. after a ``max_hours`` stop; a plain init_from on
    a mid-phase checkpoint warns that the plan replays from a phase start."""
    train_from_spec(SPEC.format("small"), device="cpu", run_id="stopped", max_hours=1e-9)
    stopped = RunReader(eng / ".wmlab" / "runs" / "stopped")
    assert stopped.manifest()["status"] == "killed"
    # simulate a longer interrupted run: checkpoint at step 8 of 12 (mid "robust" phase)
    train_from_spec(SPEC.format("small"), device="cpu", run_id="parent-run")
    parent = eng / ".wmlab" / "runs" / "parent-run"
    parent_ckpt = torch.load(parent / "checkpoints" / "step_000012.pt", weights_only=True)
    assert parent_ckpt["optimizer"] is not None
    # build a mid-phase checkpoint by copying the index entry of step 12 under step 8
    import json as _json

    from wmlab.tracking import atomic_write_json, load_index
    index = load_index(parent)
    entry = next(e for e in index if e["name"] == "step_000012")
    payload = dict(parent_ckpt)
    payload["step"] = 8
    torch.save(payload, parent / "checkpoints" / "step_000008.pt")
    index.append({**entry, "name": "step_000008", "file": "step_000008.pt", "step": 8,
                  "tags": []})
    atomic_write_json(parent / "checkpoints" / "index.json", index)

    resumed = {"init_from": _json.dumps({"run": "parent-run", "checkpoint": "step_000008",
                                         "resume": True})}
    seen: list[int] = []
    train_from_spec(SPEC.format("small"), overrides=resumed, device="cpu", run_id="resumed",
                    trial_callback=lambda step, values: seen.append(step))
    child = RunReader(eng / ".wmlab" / "runs" / "resumed")
    manifest = child.manifest()
    assert manifest["status"] == "completed"
    assert manifest["parent"] == {"run_id": "parent-run", "checkpoint": "step_000008",
                                  "start_phase": "robust", "selector": "step_000008",
                                  "resume": True}
    starts = [(e["phase"], e["step"]) for e in child.events() if e["type"] == "phase_start"]
    assert starts == [("robust", 8)]  # no replay of the phase from its start (6)
    steps = sorted({r["step"] for r in read_jsonl(child.dir / "metrics.jsonl")})
    assert steps[0] > 8 and steps[-1] == 12 and seen == [12]
    assert manifest["progress"]["step"] == 12
    warnings = [e["message"] for e in child.events() if e["type"] == "warning"]
    assert not any("repart au step" in w for w in warnings)
    # a plain init_from on the same mid-phase checkpoint replays the phase, with a warning
    plain = {"init_from": _json.dumps({"run": "parent-run", "checkpoint": "step_000008",
                                       "start_phase": "robust"})}
    train_from_spec(SPEC.format("small"), overrides=plain, device="cpu", run_id="replayed")
    replayed = RunReader(eng / ".wmlab" / "runs" / "replayed")
    starts = [(e["phase"], e["step"]) for e in replayed.events() if e["type"] == "phase_start"]
    assert starts == [("robust", 6)]
    warnings = [e["message"] for e in replayed.events() if e["type"] == "warning"]
    assert any("resume: true" in w and "step 8" in w for w in warnings)
    # resume and start_phase are exclusive; a finished checkpoint has nothing to resume
    with pytest.raises(SpecError, match="start_phase"):
        train_from_spec(SPEC.format("small"), device="cpu", overrides={"init_from": _json.dumps(
            {"run": "parent-run", "checkpoint": "step_000008", "resume": True,
             "start_phase": "robust"})})
    with pytest.raises(ValueError, match="rien à reprendre"):
        train_from_spec(SPEC.format("small"), device="cpu", overrides={"init_from": _json.dumps(
            {"run": "parent-run", "checkpoint": "last", "resume": True})})


def test_init_from_errors(eng: Path):
    bad = {"init_from": json.dumps({"run": "missing-run"})}
    with pytest.raises(FileNotFoundError, match="missing-run"):
        train_from_spec(SPEC.format("small"), overrides=bad, device="cpu")
    assert list_runs(eng / ".wmlab" / "runs") == []


def test_max_hours_kills_with_last_checkpoint(eng: Path):
    summary = train_from_spec(SPEC.format("small"), device="cpu", max_hours=1e-9)
    reader = _only_run(eng)
    manifest = reader.manifest()
    assert manifest["status"] == "killed" and manifest["error"] == "max_hours reached"
    assert manifest["ended_at"] is not None
    checkpoints = reader.checkpoints()
    assert [c["name"] for c in checkpoints] == ["step_000001"]
    assert "last" in checkpoints[0]["tags"]
    assert isinstance(summary, dict)


def test_exception_marks_run_failed(eng: Path):
    overrides = {"loss.terms.boom": json.dumps({"name": "_eng_boom", "params": {"at_step": 3}})}
    with pytest.raises(RuntimeError, match="boom"):
        train_from_spec(SPEC.format("small"), overrides=overrides, device="cpu")
    manifest = _only_run(eng).manifest()
    assert manifest["status"] == "failed"
    assert "boom" in manifest["error"] and "Traceback" in manifest["error"]


def test_unknown_component_fails_run(eng: Path):
    with pytest.raises(KeyError, match="_eng_nope"):
        train_from_spec(SPEC.format("small"), overrides={"model.name": "_eng_nope"},
                        device="cpu")
    manifest = _only_run(eng).manifest()
    assert manifest["status"] == "failed" and "_eng_nope" in manifest["error"]


def test_spec_error_creates_no_run(eng: Path):
    with pytest.raises(SpecError):
        train_from_spec(SPEC.format("small"), overrides={"phases.0.steps": "0"}, device="cpu")
    assert list_runs(eng / ".wmlab" / "runs") == []


def test_pruned_trial_is_killed(eng: Path):
    optuna = pytest.importorskip("optuna")

    def prune(step: int, values: dict) -> None:
        assert "val/bit_acc_mean" in values
        raise optuna.TrialPruned("test")

    with pytest.raises(optuna.TrialPruned):
        train_from_spec(SPEC.format("small"), device="cpu", study="s1", trial=3,
                        gpu_share=0.5, trial_callback=prune)
    manifest = _only_run(eng).manifest()
    assert manifest["status"] == "killed" and manifest["error"] == "pruned"
    assert manifest["study"] == "s1" and manifest["trial"] == 3
    assert manifest["gpu_share"] == 0.5


def test_smoke_transform_and_final_benchmark(eng: Path):
    resolved = experiment_dict()
    smoke = apply_smoke(resolved, "image")
    assert all(p["steps"] <= 6 for p in smoke["phases"])
    assert smoke["data"]["batch_size"] == 4 and smoke["data"]["num_workers"] == 0
    assert smoke["data"]["train"]["dataset"] == "synthetic"
    assert smoke["data"]["train"]["params"]["size"] == 64
    assert smoke["eval"]["every_steps"] == 4 and smoke["eval"]["val_batches"] == 2
    assert smoke["phases"][1]["batch_size"] is None
    assert apply_smoke(resolved, "audio")["data"]["val"]["params"]["length"] == 8000
    assert apply_smoke(resolved, "video")["data"]["val"]["params"]["frames"] == 4

    final = json.dumps({"benchmark": "eng_v1", "checkpoints": ["last", "best:val/bit_acc_mean",
                                                                "phase_end:nope"]})
    launch = eng / "launch.json"
    launch.write_text(json.dumps({"job_id": "job-1", "host": "local"}), encoding="utf-8")
    train_from_spec(SPEC.format("curriculum"), overrides={"eval.final_benchmark": final},
                    smoke=True, device="cpu", launch_file=str(launch))
    reader = _only_run(eng)
    manifest = reader.manifest()
    assert manifest["status"] == "completed" and "smoke" in manifest["tags"]
    assert manifest["progress"]["total_steps"] == 12
    assert json.loads((reader.dir / "env" / "launch.json").read_text())["job_id"] == "job-1"
    val_steps = sorted(_rows(reader, "val/bit_acc_mean"))
    assert val_steps == [4, 6, 8, 12]
    evals = reader.evals()
    assert evals, "final benchmark missing"
    assert {e["benchmark"] for e in evals} == {"eng_v1"}
    assert all(e["n_items"] == 8 and e["smoke"] for e in evals)
    assert any(e["type"] == "eval" for e in reader.events())
    errors = [e for e in reader.events() if e["type"] == "error"]
    assert len(errors) == 1 and "phase_end:nope" in errors[0]["message"]


def test_sigterm_checkpoints_and_kills(eng: Path, tmp_path: Path):
    import os
    import signal
    import subprocess
    import sys
    import time

    from helpers_engine import shim_env, write_yaml

    long = small_experiment(name="long", steps=(100000, 10))
    long["eval"]["every_steps"] = 100000
    write_yaml(eng / "projects" / "eng" / "experiments" / "long.yaml", long)
    env = {**os.environ, **shim_env(tmp_path), "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen([sys.executable, "-m", "wmlab.cli", "train", SPEC.format("long"),
                             "--device", "cpu", "--run-id", "sig-run"], cwd=eng, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    run_dir = eng / ".wmlab" / "runs" / "sig-run"
    deadline = time.time() + 60
    try:
        while time.time() < deadline and proc.poll() is None:
            if any("train/loss" in r["values"] for r in read_jsonl(run_dir / "metrics.jsonl")):
                break
            time.sleep(0.2)
        assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
        proc.send_signal(signal.SIGTERM)
        output, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0, output
    manifest = RunReader(run_dir).manifest()
    assert manifest["status"] == "killed" and manifest["error"] == "SIGTERM reçu", output
    checkpoints = RunReader(run_dir).checkpoints()
    assert len(checkpoints) == 1 and checkpoints[0]["step"] >= 20
    assert "last" in checkpoints[0]["tags"] and checkpoints[0]["available_locally"]
