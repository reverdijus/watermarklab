"""Attacks shared by several modalities: ``identity``, ``gaussian_noise``, ``combo``.

A registry name designates a single component, so multi-modal attacks dispatch on the tensor
rank: ``[B,1,T]`` is audio, ``[B,3,H,W]`` image, ``[B,F,3,H,W]`` video.
"""

from __future__ import annotations

from typing import Any

import torch
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.attacks.base import Attack, apply_attack, broadcast_per_sample, clamp_strength
from wmlab.registry import ATTACKS


@ATTACKS.register("identity", modality=None)
class Identity(Attack):
    """Aucune transformation (référence sans attaque)."""

    modality = "image"

    def __init__(self) -> None:
        super().__init__()

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        return x


class GaussianNoiseParams(BaseModel):
    """Image/video: ``std`` ; audio: ``snr_db`` (both reached at strength=1)."""

    model_config = ConfigDict(extra="forbid")

    std: float = Field(0.06, ge=0.0, le=1.0, description="Image : écart-type du bruit")
    snr_db: float = Field(20.0, ge=-20.0, le=120.0, description="Audio : SNR visé (dB)")


@ATTACKS.register("gaussian_noise", modality=["image", "audio"], params=GaussianNoiseParams)
class GaussianNoise(Attack):
    """Bruit blanc gaussien : écart-type `std` (image) ou SNR `snr_db` (audio)."""

    modality = "image"

    def __init__(self, std: float = 0.06, snr_db: float = 20.0) -> None:
        super().__init__(std=std, snr_db=snr_db)
        self.std = float(std)
        self.snr_db = float(snr_db)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        if x.ndim == 3:  # audio [B,1,T]: amplitude scaled so that SNR = snr_db at strength 1
            rms = x.pow(2).mean(dim=(1, 2)).sqrt().clamp_min(1e-4)
            std = broadcast_per_sample(s * rms * 10.0 ** (-self.snr_db / 20.0), x)
            return (x + std * torch.randn_like(x)).clamp(-1.0, 1.0)
        return (x + s * self.std * torch.randn_like(x)).clamp(0.0, 1.0)


class ComboItem(BaseModel):
    """One step of a ``combo`` attack."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class ComboParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attacks: list[ComboItem] = Field(default_factory=list,
                                     description="Attaques appliquées dans l'ordre")


@ATTACKS.register("combo", modality=None, params=ComboParams)
class Combo(Attack):
    """Enchaîne plusieurs attaques (même strength pour chacune)."""

    modality = "image"

    def __init__(self, attacks: list[dict[str, Any]] | None = None) -> None:
        items = [ComboItem.model_validate(a) for a in (attacks or [])]
        super().__init__(attacks=[item.model_dump() for item in items])
        self.steps = nn.ModuleList(ATTACKS.build(item.name, **item.params) for item in items)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        # non differentiable steps get their own straight-through estimator, so the chain
        # stays differentiable end to end
        for step in self.steps:
            x = apply_attack(step, x, strength)
        return x
