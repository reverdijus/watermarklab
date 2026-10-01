"""Helpers for the engine tests (trainer, eval, HPO).

- ``ENGINE_PLUGIN_SOURCE``: fake ``_eng_*`` components installed as a ``lab`` plugin of the
  test workspace (also imported by HPO worker subprocesses).
- ``install_shims()``: when an ML module of §4 (``wmlab.attacks``, ``wmlab.data``…) is not
  importable yet, a minimal stand-in is put in ``sys.modules`` (tests only).
  ``shim_env()`` returns environment variables doing the same in subprocesses.
- small builders for project / experiment / HPO plan / benchmark YAML files.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib
import json
import random
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

TESTS_DIR = Path(__file__).resolve().parent

ENGINE_PLUGIN_SOURCE = '''\
"""Fake components used by the engine tests (names prefixed with _eng_)."""

import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.data import Dataset

from wmlab.registry import ATTACKS, DATASETS, LOSSES, METRICS, MODELS


@MODELS.register("_eng_model", modality="image")
class EngModel(nn.Module):
    """Tiny image model.

    mode "learn": trainable colour shift + mean-colour decoder; "perfect": the bits are
    written in the first pixel row and read back (bit accuracy 1); "blind": the extractor
    always returns zeros (bit accuracy = share of zero bits).
    """

    modality = "image"

    def __init__(self, nbits: int, hidden: int = 8, strength: float = 0.05,
                 mode: str = "learn") -> None:
        super().__init__()
        self.nbits = nbits
        self.strength = strength
        self.mode = mode
        self.encoder = nn.Sequential(nn.Linear(nbits, hidden), nn.Tanh(), nn.Linear(hidden, 3))
        self.decoder = nn.Linear(3, nbits)

    def _zero(self):
        return sum(p.sum() for p in self.parameters()) * 0.0

    def embed(self, x, msg):
        if self.mode == "perfect":
            out = x.clone()
            out[:, 0, 0, : self.nbits] = msg
            return out + self._zero()
        shift = torch.tanh(self.encoder(msg * 2 - 1))[:, :, None, None]
        return (x + self.strength * shift).clamp(0, 1)

    def extract(self, x):
        if self.mode == "perfect":
            return (x[:, 0, 0, : self.nbits] - 0.5) * 20 + self._zero()
        if self.mode == "blind":
            return torch.zeros(x.shape[0], self.nbits, device=x.device) + self._zero()
        return self.decoder((x.mean(dim=(2, 3)) - 0.5) * 10)

    def forward(self, x, msg):
        return self.embed(x, msg)

    def aux_losses(self, batch):
        return {}

    def param_groups(self):
        return {"encoder": list(self.encoder.parameters()),
                "decoder": list(self.decoder.parameters())}


@DATASETS.register("_eng_images")
class EngImages(Dataset):
    """Deterministic random images."""

    def __init__(self, num_items: int = 8, size: int = 16, seed: int = 0,
                 modality: str = "image") -> None:
        self.num_items, self.size, self.seed = num_items, size, seed

    def __len__(self):
        return self.num_items

    def __getitem__(self, i):
        g = torch.Generator().manual_seed(self.seed * 100003 + i)
        return {"x": torch.rand(3, self.size, self.size, generator=g), "id": f"{self.seed}-{i}"}


@LOSSES.register("_eng_bce")
class EngBCE(nn.Module):
    """Message BCE."""

    def forward(self, batch):
        return F.binary_cross_entropy_with_logits(batch.logits.float(), batch.msg)


@LOSSES.register("_eng_mse")
class EngMSE(nn.Module):
    """MSE between x and x_w."""

    def forward(self, batch):
        return ((batch.x_w - batch.x) ** 2).mean()


@LOSSES.register("_eng_boom")
class EngBoom(nn.Module):
    """Raises from a given global step (failure tests)."""

    def __init__(self, at_step: int = 3) -> None:
        super().__init__()
        self.at_step = at_step

    def forward(self, batch):
        if batch.step >= self.at_step:
            raise RuntimeError("boom (erreur de test)")
        return batch.x_w.sum() * 0.0


@ATTACKS.register("_eng_noise", modality=["image", "audio", "video"])
class EngNoise(nn.Module):
    """Additive gaussian noise (differentiable)."""

    differentiable = True

    def __init__(self, std: float = 0.05) -> None:
        super().__init__()
        self.std = std

    def forward(self, x, strength: float = 1.0):
        return (x + self.std * strength * torch.randn_like(x)).clamp(0, 1)


@ATTACKS.register("_eng_quant", modality="image")
class EngQuant(nn.Module):
    """Quantization (not differentiable)."""

    differentiable = False

    def __init__(self, levels: int = 8) -> None:
        super().__init__()
        self.levels = levels

    def forward(self, x, strength: float = 1.0):
        if strength <= 0:
            return x
        return torch.round(x * self.levels) / self.levels


# -- stand-ins for builtins, only when the real ones are not available --------------------
if not ATTACKS.has("identity"):
    @ATTACKS.register("identity")
    class EngIdentity(nn.Module):
        """Identity."""

        def forward(self, x, strength: float = 1.0):
            return x

if not METRICS.has("psnr"):
    @METRICS.register("psnr", modality=["image", "video"])
    def eng_psnr(x, x_w, **ctx):
        """PSNR (dB)."""
        mse = ((x - x_w) ** 2).flatten(1).mean(1).clamp_min(1e-10)
        return 10 * torch.log10(1.0 / mse)

if not METRICS.has("ssim"):
    @METRICS.register("ssim", modality=["image", "video"])
    def eng_ssim(x, x_w, **ctx):
        """Crude SSIM stand-in (1 - MSE)."""
        return 1.0 - ((x - x_w) ** 2).flatten(1).mean(1)

if not DATASETS.has("synthetic"):
    @DATASETS.register("synthetic")
    class EngSynthetic(Dataset):
        """Synthetic stand-in (image only)."""

        def __init__(self, modality: str = "image", size: int = 64, length: int = 8000,
                     frames: int = 4, num_items: int = 16, seed: int = 0) -> None:
            self.size, self.num_items, self.seed = size, num_items, seed

        def __len__(self):
            return self.num_items

        def __getitem__(self, i):
            g = torch.Generator().manual_seed(self.seed * 100003 + i)
            return {"x": torch.rand(3, self.size, self.size, generator=g), "id": str(i)}
'''


def install_engine_plugin(root: Path) -> Path:
    """Write the fake components as ``lab/engine_fakes.py`` of the workspace."""
    path = root / "lab" / "engine_fakes.py"
    path.write_text(ENGINE_PLUGIN_SOURCE, encoding="utf-8")
    return path


# -- shims for ML modules that are not importable yet -----------------------------------------
def _shim_attacks() -> dict[str, Any]:
    def apply_attack(attack: Any, x: Any, strength: float) -> Any:
        if getattr(attack, "differentiable", True):
            return attack(x, strength)
        return x + (attack(x.detach(), strength) - x).detach()

    def sample_strength(max_strength: float, random_strength: bool = True,
                        generator: Any = None) -> float:
        rng = generator if isinstance(generator, random.Random) else random
        return rng.uniform(0.0, float(max_strength)) if random_strength else float(max_strength)

    return {"apply_attack": apply_attack, "sample_strength": sample_strength}


def _shim_data() -> dict[str, Any]:
    import torch

    from wmlab.registry import DATASETS

    def build_dataset(split: Any, modality: str) -> Any:
        dataset = DATASETS.build(split.dataset, **split.params)
        if split.limit and split.limit < len(dataset):
            dataset = torch.utils.data.Subset(dataset, list(range(split.limit)))
        return dataset

    def make_loader(split: Any, modality: str, batch_size: int = 32, num_workers: int = 0,
                    shuffle: bool = True, seed: int = 0, drop_last: bool = True) -> Any:
        dataset = build_dataset(split, modality)
        return torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=shuffle,
                                           num_workers=0,
                                           drop_last=drop_last and len(dataset) >= batch_size,
                                           generator=torch.Generator().manual_seed(seed))

    def dataset_version(split: Any) -> str:
        data = split.model_dump(mode="json")
        canonical = json.dumps({k: data.get(k) for k in ("dataset", "params", "preprocess",
                                                          "limit")}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    return {"build_dataset": build_dataset, "make_loader": make_loader,
            "dataset_version": dataset_version, "resolve_path": lambda value: value}


def _shim_losses_base() -> dict[str, Any]:
    @dataclass
    class StepBatch:
        x: Any
        x_w: Any
        x_att: Any
        msg: Any
        logits: Any
        modality: str
        model: Any
        step: int
        phase: str
        extras: dict

    return {"StepBatch": StepBatch}


def _shim_metrics() -> dict[str, Any]:
    return {"default_quality_metric":
            lambda modality: {"audio": "snr"}.get(modality, "psnr")}


def _shim_robustness() -> dict[str, Any]:
    import math

    def bit_accuracy(logits: Any, msg: Any) -> Any:
        return ((logits > 0).float() == msg.float()).float().mean(dim=1)

    def detection_threshold(nbits: int, fpr: float) -> int:
        for k in range(nbits + 2):
            tail = sum(math.comb(nbits, j) for j in range(k, nbits + 1)) / 2 ** nbits
            if tail <= fpr:
                return k
        return nbits + 1

    def tpr_at_fpr(logits: Any, msg: Any, fpr: float) -> float:
        matches = ((logits > 0).float() == msg.float()).float().sum(dim=1)
        k = detection_threshold(msg.shape[1], fpr)
        return float((matches >= k).float().mean())

    return {"bit_accuracy": bit_accuracy, "detection_threshold": detection_threshold,
            "tpr_at_fpr": tpr_at_fpr}


def _shim_models() -> dict[str, Any]:
    import torch

    from wmlab.registry import MODELS

    def load_model_from_checkpoint(path: Any, device: str = "cpu") -> Any:
        payload = torch.load(path, map_location="cpu", weights_only=False)
        model = MODELS.build(payload["model_name"], nbits=payload["nbits"],
                             **payload["model_params"])
        model.load_state_dict(payload["model"])
        return model.to(device)

    return {"load_model_from_checkpoint": load_model_from_checkpoint}


SHIMS: dict[str, tuple[Any, tuple[str, ...]]] = {
    "wmlab.attacks": (_shim_attacks, ("apply_attack", "sample_strength")),
    "wmlab.data": (_shim_data, ("build_dataset", "make_loader", "dataset_version")),
    "wmlab.losses.base": (_shim_losses_base, ("StepBatch",)),
    "wmlab.metrics": (_shim_metrics, ("default_quality_metric",)),
    "wmlab.metrics.robustness": (_shim_robustness, ("bit_accuracy", "tpr_at_fpr")),
    "wmlab.models": (_shim_models, ("load_model_from_checkpoint",)),
}


def _usable(name: str, attrs: tuple[str, ...]) -> bool:
    try:
        module = importlib.import_module(name)
    except Exception:
        return False
    return all(hasattr(module, a) for a in attrs)


def missing_ml_modules() -> list[str]:
    """ML modules of §4 that are not importable (or lack the functions the engine uses)."""
    return [name for name, (_, attrs) in SHIMS.items()
            if not isinstance(sys.modules.get(name), _ShimModule) and not _usable(name, attrs)]


class _ShimModule(types.ModuleType):
    """Marker type of the test stand-ins."""


def install_shims() -> dict[str, Any]:
    """Put stand-ins in ``sys.modules`` for the missing ML modules.

    Returns the previous ``sys.modules`` entries that were replaced (``None`` when absent),
    to be given back to :func:`remove_shims`.
    """
    replaced: dict[str, Any] = {}
    for name, (factory, attrs) in SHIMS.items():
        if isinstance(sys.modules.get(name), _ShimModule) or _usable(name, attrs):
            continue
        for parent in _parents(name):
            if parent not in sys.modules or not _importable(parent):
                replaced.setdefault(parent, sys.modules.get(parent))
                stub = _ShimModule(parent, "test stand-in package")
                stub.__path__ = []
                sys.modules[parent] = stub
        module = _ShimModule(name, "test stand-in (tests/helpers_engine.py)")
        module.__path__ = []  # allow submodules (wmlab.metrics.robustness, wmlab.losses.base)
        module.__dict__.update(factory())
        replaced.setdefault(name, sys.modules.get(name))
        sys.modules[name] = module
    return replaced


def remove_shims(replaced: dict[str, Any]) -> None:
    """Undo :func:`install_shims` (other test modules must not see the stand-ins)."""
    for name, previous in replaced.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


@contextlib.contextmanager
def shimmed() -> Iterator[list[str]]:
    """Context manager installing the stand-ins for its duration; yields their names."""
    replaced = install_shims()
    try:
        yield [name for name in replaced if isinstance(sys.modules.get(name), _ShimModule)]
    finally:
        remove_shims(replaced)


def _parents(name: str) -> list[str]:
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(2, len(parts))]


def _importable(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def shim_env(tmp_dir: Path) -> dict[str, str]:
    """Environment making subprocesses install the same shims (empty if none needed)."""
    if not missing_ml_modules() and not any(isinstance(m, _ShimModule)
                                            for m in list(sys.modules.values())):
        return {}
    site_dir = tmp_dir / "shim_site"
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "sitecustomize.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(TESTS_DIR)!r})\n"
        "import helpers_engine\n"
        "helpers_engine.install_shims()\n", encoding="utf-8")
    return {"PYTHONPATH": str(site_dir)}


# -- YAML builders ------------------------------------------------------------------------------
def write_yaml(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


PROJECT: dict[str, Any] = {"name": "eng", "title": "Projet moteur", "modality": "image",
                           "message_bits": 16}

EXPERIMENT: dict[str, Any] = {
    "name": "curriculum",
    "description": "Test du moteur",
    "hypothesis": "Le warmup aide le décodeur",
    "seed": 0,
    "model": {"name": "_eng_model", "params": {"hidden": 8}},
    "data": {
        "train": {"dataset": "_eng_images", "params": {"num_items": 16, "size": 16}},
        "val": {"dataset": "_eng_images", "params": {"num_items": 8, "size": 16, "seed": 1}},
        "batch_size": 4,
        "num_workers": 0,
    },
    "optimizer": {"name": "adamw", "lr": 1.0e-2, "grad_clip": 1.0},
    "loss": {"terms": {"msg": {"name": "_eng_bce", "weight": 1.0},
                       "img": {"name": "_eng_mse", "weight": 0.5}}},
    "phases": [
        {"name": "warmup", "steps": 25, "lr": {"schedule": "linear_warmup", "warmup_steps": 30},
         "loss_weights": {"img": 0.1}, "attacks": []},
        {"name": "robust", "steps": 30, "lr": {"schedule": "cosine", "min_factor": 0.1},
         "loss_weights": {"img": {"ramp": [0.1, 1.0]}},
         "attacks": [{"name": "_eng_noise", "params": {"std": 0.05}},
                     {"name": "_eng_quant", "label": "quant", "p": 0.5}],
         "attack_strength": {"ramp": [0.0, 1.0]}, "freeze": ["decoder"], "batch_size": 2,
         "reset_optimizer": True},
    ],
    "eval": {"every_steps": 10, "val_batches": 2,
             "val_attacks": [{"label": "noise", "name": "_eng_noise"},
                             {"label": "quant", "name": "_eng_quant"}]},
    "checkpoint": {"every_steps": 10, "keep_last": 1, "keep_best": {"val/bit_acc_mean": "max"},
                   "at_phase_end": True},
    "samples": {"every_steps": 20, "count": 2},
    "resources": {"device": "cpu"},
}


def experiment_dict(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(EXPERIMENT)
    data.update(changes)
    return data


def small_experiment(name: str = "small", steps: tuple[int, int] = (6, 6),
                     **changes: Any) -> dict[str, Any]:
    """Two short phases, validation every 4 steps, no samples."""
    data = experiment_dict(name=name)
    data["phases"][0].update(steps=steps[0], lr={"schedule": "constant"})
    data["phases"][1].update(steps=steps[1], freeze=[], batch_size=None, reset_optimizer=False)
    data["eval"]["every_steps"] = 4
    data["checkpoint"] = {"every_steps": None, "keep_last": 2,
                          "keep_best": {"val/bit_acc_mean": "max"}, "at_phase_end": True}
    data["samples"] = {"every_steps": None, "count": 0}
    data.update(changes)
    return data


BENCHMARK: dict[str, Any] = {
    "name": "eng_v1",
    "version": 1,
    "modality": "image",
    "description": "Suite de test du moteur",
    "data": {"dataset": "_eng_images", "params": {"num_items": 12, "size": 16, "seed": 7}},
    "batch_size": 5,
    "seed": 0,
    "quality_metrics": ["psnr"],
    "fpr": [1.0e-2, 1.0e-4, 1.0e-6],
    "attacks": [{"label": "none", "name": "identity"},
                {"label": "noise", "name": "_eng_noise", "params": {"std": 0.1}},
                {"label": "quant", "name": "_eng_quant"}],
}


def make_workspace(root: Path, *, experiments: list[dict[str, Any]] | None = None,
                   hpo: list[dict[str, Any]] | None = None, benchmark: bool = True) -> Path:
    """Install the fake plugin and write the project, experiments, HPO plans and benchmark."""
    install_engine_plugin(root)
    project_dir = root / "projects" / PROJECT["name"]
    write_yaml(project_dir / "project.yaml", PROJECT)
    for exp in experiments or [experiment_dict()]:
        write_yaml(project_dir / "experiments" / f"{exp['name']}.yaml", exp)
    for plan in hpo or []:
        write_yaml(project_dir / "hpo" / f"{plan['name']}.yaml", plan)
    if benchmark:
        write_yaml(root / "benchmarks" / "eng_v1.yaml", BENCHMARK)
    return project_dir
