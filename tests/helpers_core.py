"""Helpers for the core tests: fake ``_test_*`` components and small YAML builders."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

FAKE_PLUGIN_SOURCE = '''\
"""Fake components used by the core tests (names prefixed with _test_)."""

import math

import torch
import torch.nn.functional as F
from pydantic import BaseModel, Field
from torch import nn
from torch.utils.data import Dataset

from wmlab.registry import ATTACKS, DATASETS, LOSSES, METRICS, MODELS, PREPROCESS


class TestModelParams(BaseModel):
    channels: int = Field(8, gt=0)


@MODELS.register("_test_model", modality="image", params=TestModelParams)
class FakeImageModel(nn.Module):
    """Fake image model.

    Second paragraph, not part of the description.
    """

    modality = "image"

    def __init__(self, nbits: int, channels: int = 8) -> None:
        super().__init__()
        self.nbits = nbits
        self.enc = nn.Conv2d(3 + nbits, 3, 1)
        self.dec = nn.Linear(3, nbits)

    def embed(self, x, msg):
        b, _, h, w = x.shape
        m = msg[:, :, None, None].expand(b, self.nbits, h, w)
        return (x + 0.01 * torch.tanh(self.enc(torch.cat([x, m], 1)))).clamp(0, 1)

    def extract(self, x):
        return self.dec(x.mean(dim=(2, 3)))

    def forward(self, x, msg):
        return self.embed(x, msg)


@MODELS.register("_test_audio_model", modality="audio")
class FakeAudioModel(nn.Module):
    """Fake audio model (params derived from the signature)."""

    modality = "audio"

    def __init__(self, nbits: int, hidden: int = 4, scale: float = 0.01) -> None:
        super().__init__()
        self.nbits = nbits
        self.scale = scale
        self.enc = nn.Conv1d(1 + nbits, 1, 1)
        self.dec = nn.Linear(1, nbits)

    def embed(self, x, msg):
        b, _, t = x.shape
        m = msg[:, :, None].expand(b, self.nbits, t)
        return (x + self.scale * torch.tanh(self.enc(torch.cat([x, m], 1)))).clamp(-1, 1)

    def extract(self, x):
        return self.dec(x.mean(dim=2))

    def forward(self, x, msg):
        return self.embed(x, msg)


class NoiseParams(BaseModel):
    std: float = Field(0.05, ge=0)


@ATTACKS.register("_test_noise", modality=["image", "audio", "video"], params=NoiseParams)
class FakeNoise(nn.Module):
    """Additive gaussian noise."""

    differentiable = True

    def __init__(self, std: float = 0.05) -> None:
        super().__init__()
        self.std = std

    def forward(self, x, strength: float = 1.0):
        if strength <= 0:
            return x
        out = x + self.std * strength * torch.randn_like(x)
        return out.clamp(-1, 1) if x.ndim == 3 else out.clamp(0, 1)


@ATTACKS.register("_test_quant", modality="image")
class FakeQuant(nn.Module):
    """Quantization (not differentiable)."""

    differentiable = False

    def __init__(self, levels: int = 8) -> None:
        super().__init__()
        self.levels = levels

    def forward(self, x, strength: float = 1.0):
        if strength <= 0:
            return x
        return torch.round(x * self.levels) / self.levels


class DatasetParams(BaseModel):
    modality: str = "image"
    num_items: int = Field(8, gt=0)
    size: int = Field(16, gt=0)
    root: str | None = None


@DATASETS.register("_test_dataset", params=DatasetParams)
class FakeDataset(Dataset):
    """Random items for any modality."""

    def __init__(self, modality: str = "image", num_items: int = 8, size: int = 16,
                 root: str | None = None) -> None:
        self.modality, self.num_items, self.size = modality, num_items, size

    def __len__(self):
        return self.num_items

    def __getitem__(self, i):
        g = torch.Generator().manual_seed(i)
        if self.modality == "audio":
            x = torch.rand(1, 800, generator=g) * 2 - 1
        elif self.modality == "video":
            x = torch.rand(2, 3, self.size, self.size, generator=g)
        else:
            x = torch.rand(3, self.size, self.size, generator=g)
        return {"x": x, "id": f"{i:04d}"}


@PREPROCESS.register("_test_scale", modality=["image", "video"])
class FakeScale:
    """Multiply then clamp."""

    deterministic = True

    def __init__(self, factor: float = 1.0) -> None:
        self.factor = factor

    def __call__(self, x):
        return (x * self.factor).clamp(0, 1)


@PREPROCESS.register("_test_crop", modality="image")
class FakeCrop:
    """Top-left crop (required param without default)."""

    deterministic = True

    def __init__(self, size: int) -> None:
        self.size = size

    def __call__(self, x):
        return x[..., : self.size, : self.size]


@LOSSES.register("_test_mse")
class FakeMSE(nn.Module):
    """MSE between x and x_w."""

    def forward(self, batch):
        return ((batch.x_w - batch.x) ** 2).mean()


@LOSSES.register("_test_bce")
class FakeBCE(nn.Module):
    """Message BCE."""

    def forward(self, batch):
        return F.binary_cross_entropy_with_logits(batch.logits, batch.msg)


@METRICS.register("_test_psnr", modality=["image", "video"])
def fake_psnr(x, x_w, **ctx):
    """PSNR in dB."""
    mse = ((x - x_w) ** 2).flatten(1).mean(1).clamp_min(1e-10)
    return 10 * torch.log10(1.0 / mse)


@METRICS.register("_test_snr", modality="audio")
def fake_snr(x, x_w, **ctx):
    """SNR in dB."""
    noise = ((x - x_w) ** 2).flatten(1).sum(1).clamp_min(1e-10)
    return 10 * torch.log10((x ** 2).flatten(1).sum(1).clamp_min(1e-10) / noise)
'''


def install_fake_plugins(root: Path) -> Path:
    """Write the fake components as a ``lab`` plugin of the workspace."""
    path = root / "lab" / "core_fakes.py"
    path.write_text(FAKE_PLUGIN_SOURCE, encoding="utf-8")
    return path


def write_yaml(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def project_dict(name: str = "demo", modality: str = "image", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"name": name, "title": "Projet de test", "modality": modality,
                            "message_bits": 16}
    if modality == "image":
        data["objectives"] = {"quality": {"metric": "_test_psnr"},
                              "robustness": {"metric": "bit_acc_mean"}}
    else:
        data["objectives"] = {"quality": {"metric": "_test_snr"},
                              "robustness": {"metric": "bit_acc_mean"}}
    data.update(extra)
    return data


EXPERIMENT: dict[str, Any] = {
    "name": "baseline",
    "description": "Expérience de test",
    "hypothesis": "Le warmup sans attaque aide le décodeur",
    "seed": 0,
    "model": {"name": "_test_model", "params": {"channels": 8}},
    "data": {
        "train": {"dataset": "_test_dataset", "params": {"modality": "image", "num_items": 8},
                  "preprocess": [{"name": "_test_scale", "params": {"factor": 1.0}}]},
        "val": {"dataset": "_test_dataset", "params": {"modality": "image", "num_items": 4}},
        "batch_size": 4,
    },
    "optimizer": {"name": "adamw", "lr": 1.0e-3},
    "loss": {"terms": {"msg": {"name": "_test_bce", "weight": 1.0},
                       "img": {"name": "_test_mse", "weight": 0.5}}},
    "phases": [
        {"name": "warmup", "steps": 10, "lr": {"schedule": "linear_warmup", "warmup_steps": 2},
         "loss_weights": {"img": 0.1}, "attacks": []},
        {"name": "robust", "steps": 20, "loss_weights": {"img": {"ramp": [0.1, 1.0]}},
         "attacks": [{"name": "_test_noise", "params": {"std": 0.05}},
                     {"name": "_test_quant", "label": "quant"}],
         "attack_strength": {"ramp": [0.0, 1.0]}},
    ],
    "eval": {"every_steps": 5, "val_attacks": [{"label": "noise", "name": "_test_noise"}]},
    "checkpoint": {"keep_best": {"val/bit_acc_mean": "max"}},
}


def experiment_dict(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(EXPERIMENT)
    data.update(changes)
    return data


def make_project(root: Path, name: str = "demo", modality: str = "image",
                 experiment: dict[str, Any] | None = None, **extra: Any) -> Path:
    """Create ``projects/<name>/project.yaml`` (+ ``experiments/baseline.yaml``)."""
    project_dir = root / "projects" / name
    write_yaml(project_dir / "project.yaml", project_dict(name, modality, **extra))
    (project_dir / "hpo").mkdir(parents=True, exist_ok=True)
    if experiment is not False:
        exp = experiment or experiment_dict()
        write_yaml(project_dir / "experiments" / f"{exp['name']}.yaml", exp)
    return project_dir


HPO: dict[str, Any] = {
    "name": "sweep",
    "experiment": "baseline",
    "objectives": [{"metric": "val/bit_acc_mean", "direction": "maximize"}],
    "constraints": [{"metric": "val/psnr", "op": ">=", "value": 30}],
    "space": {
        "optimizer.lr": {"type": "float", "low": 1.0e-4, "high": 1.0e-2, "log": True},
        "phases.1.loss_weights.img.ramp.1": {"type": "float", "low": 0.1, "high": 2.0},
        "phases.0.steps": {"type": "int", "low": 5, "high": 20, "step": 5},
        "model.params.channels": {"type": "categorical", "choices": [4, 8]},
    },
    "budget": {"max_trials": 4, "parallel": 1},
}


def hpo_dict(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(HPO)
    data.update(changes)
    return data


BENCHMARK: dict[str, Any] = {
    "name": "image_v1",
    "version": 1,
    "modality": "image",
    "description": "Suite de test",
    "data": {"dataset": "_test_dataset", "params": {"modality": "image", "num_items": 4}},
    "quality_metrics": ["_test_psnr"],
    "fpr": [1.0e-2, 1.0e-4],   # reachable with the 16-bit test project (2^-16 ≈ 1.5e-5)
    "attacks": [{"label": "none", "name": "_test_noise", "params": {"std": 0.0}},
                {"label": "quant", "name": "_test_quant"}],
}


def benchmark_dict(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(BENCHMARK)
    data.update(changes)
    return data
