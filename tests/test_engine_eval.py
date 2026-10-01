"""Benchmark evaluation: EvalResult format, file + event, selectors, determinism."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from helpers_engine import BENCHMARK, make_workspace, shimmed, small_experiment
from wmlab.eval.benchmark import evaluate_checkpoint, run_benchmark, tpr_key
from wmlab.registry import MODELS, load_plugins
from wmlab.spec import BenchmarkSpec, SpecError
from wmlab.tracking import CheckpointNotFoundError, RunReader

SPEC = "projects/eng/experiments/small.yaml"
TOP_KEYS = {"run_id", "checkpoint", "benchmark", "benchmark_version", "created_at", "duration_s",
            "host", "price_eur_per_hour", "nbits", "n_items", "quality", "robustness", "summary",
            "in_run"}


@pytest.fixture(autouse=True)
def _ml_shims() -> Iterator[None]:
    """Stand-ins for ML modules that are not importable yet (removed after each test)."""
    with shimmed():
        yield


@pytest.fixture
def trained(workspace: Path) -> RunReader:
    from wmlab.train.trainer import train_from_spec

    make_workspace(workspace, experiments=[small_experiment()])
    train_from_spec(SPEC, device="cpu", run_id="eval-run")
    return RunReader(workspace / ".wmlab" / "runs" / "eval-run")


def test_evaluate_checkpoint_writes_result(trained: RunReader):
    result = evaluate_checkpoint("eval-run", "last", "eng_v1", device="cpu")
    assert set(result) == TOP_KEYS
    assert result["run_id"] == "eval-run" and result["checkpoint"] == "step_000012"
    assert result["benchmark"] == "eng_v1" and result["benchmark_version"] == 1
    assert result["created_at"].endswith("Z") and result["duration_s"] >= 0
    assert result["nbits"] == 16 and result["n_items"] == 12 and result["host"] == "local"
    assert set(result["quality"]) == {"psnr"}
    assert set(result["robustness"]) == {"none", "noise", "quant"}
    # 16 bits: tpr@1e-06 is unreachable (2^-16 ≈ 1.5e-5 > 1e-6) and left out, not written as 0
    for row in result["robustness"].values():
        assert set(row) == {"bit_acc", "tpr@0.01", "tpr@0.0001"}
        assert 0.0 <= row["bit_acc"] <= 1.0
    summary = result["summary"]
    assert set(summary) == {"psnr", "bit_acc_mean", "tpr@0.01_mean", "tpr@0.0001_mean"}
    assert result["in_run"] is False
    rob = result["robustness"]
    assert summary["bit_acc_mean"] == pytest.approx(
        (rob["noise"]["bit_acc"] + rob["quant"]["bit_acc"]) / 2)  # identity excluded
    assert summary["psnr"] == result["quality"]["psnr"]

    path = trained.dir / "eval" / "step_000012__eng_v1.json"
    assert json.loads(path.read_text()) == result
    events = [e for e in trained.events() if e["type"] == "eval"]
    assert len(events) == 1 and events[0]["checkpoint"] == "step_000012"
    assert events[0]["path"] == "eval/step_000012__eng_v1.json"
    assert trained.evals()[0]["summary"] == summary
    # the run itself was not reopened: status and end date unchanged
    assert trained.manifest()["status"] == "completed"


def test_evaluate_selectors_and_smoke(trained: RunReader):
    result = evaluate_checkpoint(str(trained.dir), "phase_end:warmup", "eng_v1", device="cpu",
                                 smoke=True)
    assert result["checkpoint"] == "step_000006" and result["n_items"] == 8
    assert result["smoke"] is True
    # a smoke evaluation never overwrites the full one: dedicated __smoke suffix
    assert (trained.dir / "eval" / "step_000006__eng_v1__smoke.json").is_file()
    assert not (trained.dir / "eval" / "step_000006__eng_v1.json").exists()
    best = evaluate_checkpoint("eval-run", "best:val/bit_acc_mean", "eng_v1", device="cpu")
    assert best["checkpoint"] in {c["name"] for c in trained.checkpoints()}


def test_evaluate_errors(trained: RunReader):
    with pytest.raises(CheckpointNotFoundError):
        evaluate_checkpoint("eval-run", "phase_end:nope", "eng_v1", device="cpu")
    with pytest.raises(SpecError, match="nope_v9"):
        evaluate_checkpoint("eval-run", "last", "nope_v9", device="cpu")
    with pytest.raises(FileNotFoundError, match="run introuvable"):
        evaluate_checkpoint("no-such-run", "last", "eng_v1", device="cpu")
    (trained.dir / "checkpoints" / "step_000012.pt").unlink()
    with pytest.raises(FileNotFoundError, match="disponible localement"):
        evaluate_checkpoint("eval-run", "last", "eng_v1", device="cpu")


def test_run_benchmark_is_deterministic(workspace: Path):
    make_workspace(workspace, experiments=[small_experiment()])
    load_plugins()
    bench = BenchmarkSpec.model_validate(BENCHMARK)
    model = MODELS.build("_eng_model", nbits=16, mode="perfect")
    first = run_benchmark(model, bench, device="cpu", nbits=16)
    second = run_benchmark(model, bench, device="cpu", nbits=16, limit=5)
    assert "run_id" not in first and "checkpoint" not in first
    assert first["n_items"] == 12 and second["n_items"] == 5
    assert first["robustness"]["none"]["bit_acc"] == 1.0
    assert first["robustness"]["quant"][tpr_key(1e-2)] == 1.0
    again = run_benchmark(model, bench, device="cpu", nbits=16)
    assert again["quality"] == first["quality"] and again["robustness"] == first["robustness"]
    blind = MODELS.build("_eng_model", nbits=16, mode="blind")
    result = run_benchmark(blind, bench, device="cpu", nbits=16)
    assert result["robustness"]["noise"][tpr_key(1e-4)] == 0.0
    assert tpr_key(1e-6) not in result["robustness"]["noise"]  # unreachable with 16 bits
    assert "tpr@1e-06_mean" not in result["summary"]
    # the limit is exact: 2^-16 ≈ 1.526e-5 — 1.6e-5 is reachable (16/16 bits), 1.5e-5 is not
    edge = run_benchmark(MODELS.build("_eng_model", nbits=16, mode="perfect"),
                         bench.model_copy(update={"fpr": [1.6e-5, 1.5e-5]}), device="cpu", nbits=16)
    assert set(edge["robustness"]["none"]) == {"bit_acc", "tpr@1.6e-05"}
    assert edge["robustness"]["none"]["tpr@1.6e-05"] == 1.0
    assert tpr_key(1e-6) == "tpr@1e-06" and tpr_key(1e-4) == "tpr@0.0001"
