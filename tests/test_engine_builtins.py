"""Smoke runs with the builtin components (skipped while they are not importable)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from helpers_engine import missing_ml_modules, write_yaml
from wmlab.registry import load_plugins

pytestmark = pytest.mark.skipif(bool(missing_ml_modules()),
                                reason="composants ML builtin (§4) pas encore importables")

CASES: dict[str, dict[str, Any]] = {
    "image": {"model": {"name": "hidden", "params": {"channels": 8, "enc_blocks": 2,
                                                     "dec_blocks": 2}},
              "loss": {"msg": {"name": "message_bce"}, "img": {"name": "mse", "weight": 0.7}},
              "train_attacks": [{"name": "jpeg_diff", "params": {"quality": 50}},
                                {"name": "crop", "params": {"area": 0.5}},
                                {"name": "jpeg", "params": {"quality": 50}}],
              "val_attacks": [{"label": "jpeg50", "name": "jpeg", "params": {"quality": 50}},
                              {"label": "blur", "name": "gaussian_blur"}],
              "bench_params": {"modality": "image", "size": 64, "num_items": 16, "seed": 3},
              "bench_attacks": [{"label": "none", "name": "identity"},
                                {"label": "jpeg_q50", "name": "jpeg", "params": {"quality": 50}}],
              "quality": ["psnr", "ssim"], "sample_ext": ".png"},
    "audio": {"model": {"name": "audio_conv", "params": {"channels": 8, "blocks": 2}},
              "loss": {"msg": {"name": "message_bce"}, "wav": {"name": "l1", "weight": 1.0}},
              "train_attacks": [{"name": "gaussian_noise", "params": {"snr_db": 20}},
                                {"name": "lowpass"}],
              "val_attacks": [{"label": "noise", "name": "gaussian_noise"}],
              "bench_params": {"modality": "audio", "length": 8000, "num_items": 16, "seed": 3},
              "bench_attacks": [{"label": "none", "name": "identity"},
                                {"label": "amp", "name": "amplitude"}],
              "quality": ["snr"], "sample_ext": ".wav"},
    "video": {"model": {"name": "video_framewise",
                        "params": {"image_model": "hidden",
                                   "image_params": {"channels": 8, "enc_blocks": 2,
                                                    "dec_blocks": 2}}},
              "loss": {"msg": {"name": "message_bce"}, "img": {"name": "mse"}},
              "train_attacks": [{"name": "frame_drop"}, {"name": "temporal_blur"}],
              "val_attacks": [{"label": "drop", "name": "frame_drop"}],
              "bench_params": {"modality": "video", "size": 64, "frames": 4, "num_items": 16,
                               "seed": 3},
              "bench_attacks": [{"label": "none", "name": "identity"},
                                {"label": "drop", "name": "frame_drop"}],
              "quality": ["psnr"], "sample_ext": ".mp4"},
}


def _setup(root: Path, modality: str) -> str:
    case = CASES[modality]
    project = root / "projects" / f"bi-{modality}"
    write_yaml(project / "project.yaml", {"name": f"bi-{modality}", "modality": modality,
                                          "message_bits": 16})
    split = {"dataset": "synthetic", "params": {"modality": modality, "num_items": 8}}
    write_yaml(project / "experiments" / "baseline.yaml", {
        "name": "baseline",
        "model": case["model"],
        "data": {"train": split, "val": split, "batch_size": 4},
        "optimizer": {"lr": 1.0e-3},
        "loss": {"terms": case["loss"]},
        "phases": [
            {"name": "warmup", "steps": 100, "lr": {"schedule": "linear_warmup",
                                                    "warmup_steps": 50}},
            {"name": "robust", "steps": 100, "attacks": case["train_attacks"],
             "attack_strength": {"ramp": [0.2, 1.0]}},
        ],
        "eval": {"every_steps": 50, "val_attacks": case["val_attacks"],
                 "final_benchmark": {"benchmark": f"bi_{modality}_v1",
                                     "checkpoints": ["last", "best:val/bit_acc_mean"]}},
        "checkpoint": {"keep_best": {"val/bit_acc_mean": "max"}},
        "samples": {"every_steps": 1000, "count": 1},
        "resources": {"device": "cpu"},
    })
    write_yaml(root / "benchmarks" / f"bi_{modality}_v1.yaml", {
        "name": f"bi_{modality}_v1", "version": 1, "modality": modality,
        "data": {"dataset": "synthetic", "params": case["bench_params"]},
        "batch_size": 4, "quality_metrics": case["quality"], "attacks": case["bench_attacks"],
    })
    return f"projects/bi-{modality}/experiments/baseline.yaml"


@pytest.mark.parametrize("modality", ["image", "audio", "video"])
def test_builtin_smoke(workspace: Path, modality: str):
    from wmlab.tracking import RunReader, list_runs
    from wmlab.train.trainer import train_from_spec

    report = load_plugins()
    assert not [e for e in report.errors if e["module"].startswith("wmlab")], report.errors
    spec = _setup(workspace, modality)
    summary = train_from_spec(spec, smoke=True, device="cpu")
    reader = RunReader(list_runs(workspace / ".wmlab" / "runs")[0])
    manifest = reader.manifest()
    assert manifest["status"] == "completed", manifest["error"]
    assert manifest["progress"]["total_steps"] == 12
    quality = CASES[modality]["quality"][0]
    assert f"val/{quality}" in summary and "val/bit_acc_mean" in summary
    evals = reader.evals()
    assert evals and all(e["n_items"] == 8 for e in evals)
    assert set(evals[0]["quality"]) == set(CASES[modality]["quality"])
    samples = reader.samples()
    assert samples and any(s["path"].endswith(CASES[modality]["sample_ext"]) for s in samples)
    assert all((reader.dir / s["path"]).is_file() for s in samples)
