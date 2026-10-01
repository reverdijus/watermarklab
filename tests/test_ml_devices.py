"""Every builtin component runs on the accelerator available on this machine (MPS/CUDA)."""

from __future__ import annotations

import pytest
import torch

import wmlab.attacks  # noqa: F401 — registration
import wmlab.losses  # noqa: F401
import wmlab.metrics  # noqa: F401
import wmlab.models  # noqa: F401
from wmlab.attacks import apply_attack
from wmlab.losses import StepBatch
from wmlab.registry import ATTACKS, LOSSES, METRICS, MODELS

if torch.cuda.is_available():
    DEVICE = "cuda"
elif torch.backends.mps.is_available():
    DEVICE = "mps"
else:
    DEVICE = None

pytestmark = pytest.mark.skipif(DEVICE is None, reason="aucun accélérateur (CUDA/MPS)")
SHAPES = {"image": (2, 3, 33, 45), "audio": (2, 1, 3001), "video": (2, 3, 3, 18, 22)}
OPTIONAL = {"lpips", "stoi"}


def _input(modality: str) -> torch.Tensor:
    x = torch.rand(*SHAPES[modality])
    return ((x * 2 - 1) * 0.5 if modality == "audio" else x).to(DEVICE)


@pytest.mark.parametrize("modality", ["image", "audio", "video"])
def test_attacks_on_device(modality: str) -> None:
    for entry in ATTACKS.list(modality):
        if not entry.builtin:
            continue
        attack = ATTACKS.build(entry.name)
        x = _input(modality).requires_grad_(True)
        out = apply_attack(attack, x, 0.8)
        assert out.device.type == DEVICE and out.shape == x.shape, entry.name
        out.sum().backward()
        assert x.grad is not None, entry.name


@pytest.mark.parametrize("name", ["hidden", "audio_conv", "video_framewise"])
def test_models_on_device(name: str) -> None:
    model = MODELS.build(name, nbits=8).to(DEVICE)
    modality = model.modality
    x = _input(modality)
    msg = torch.randint(0, 2, (x.shape[0], 8), device=DEVICE).float()
    logits = model.extract(model.embed(x, msg))
    assert logits.shape == (x.shape[0], 8)
    logits.sum().backward()


@pytest.mark.parametrize("modality", ["image", "audio", "video"])
def test_losses_and_metrics_on_device(modality: str) -> None:
    x = _input(modality)
    x_w = (x + 0.01 * torch.randn_like(x)).requires_grad_(True)
    batch = StepBatch(x=x, x_w=x_w, x_att=x_w, msg=torch.ones(2, 8, device=DEVICE),
                      logits=torch.zeros(2, 8, device=DEVICE), modality=modality,
                      model=MODELS.build("hidden", nbits=8, channels=4), step=0, phase="p")
    for entry in LOSSES.list(modality):
        if entry.builtin and entry.name not in OPTIONAL:
            value = LOSSES.build(entry.name)(batch)
            assert value.numel() == 1 and bool(torch.isfinite(value)), entry.name
    for entry in METRICS.list(modality):
        if entry.builtin and entry.name not in OPTIONAL:
            values = entry.obj(x, x_w.detach(), sample_rate=16000, fps=24)
            assert values.shape == (2,) and bool(torch.isfinite(values).all()), entry.name
