"""Video attacks (``[B,F,3,H,W]`` in [0,1])."""

from __future__ import annotations

from typing import Any

import torch
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.attacks.base import (
    Attack,
    apply_attack,
    clamp_strength,
    lerp,
    per_sample_uniform,
    shared_randomness,
)
from wmlab.attacks.codecs import h264_roundtrip
from wmlab.registry import ATTACKS


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_video(x: Tensor) -> None:
    if x.ndim != 5:
        raise ValueError(f"attaque vidéo : tenseur [B, F, 3, H, W] attendu, reçu {tuple(x.shape)}")


class FramewiseParams(_Params):
    attack: str = Field("jpeg", min_length=1, description="Attaque image appliquée à chaque frame")
    params: dict[str, Any] = Field(default_factory=dict, description="Paramètres de l'attaque image")


@ATTACKS.register("framewise", modality="video", params=FramewiseParams)
class Framewise(Attack):
    """Applique une attaque image à chaque frame (paramètres aléatoires communs à la vidéo)."""

    modality = "video"

    def __init__(self, attack: str = "jpeg", params: dict[str, Any] | None = None) -> None:
        super().__init__(attack=attack, params=dict(params or {}))
        entry = ATTACKS.get(attack)
        if not entry.supports("image"):
            raise ValueError(f"framewise : l'attaque '{attack}' n'est pas une attaque image "
                             f"(modalité : {entry.modality_label()})")
        self.inner = ATTACKS.build(attack, **(params or {}))

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        _check_video(x)
        b, f = x.shape[:2]
        with shared_randomness(f):
            # a non differentiable inner attack gets a straight-through gradient here
            out = apply_attack(self.inner, x.reshape(b * f, *x.shape[2:]), s)
        return out.reshape(x.shape)


class H264Params(_Params):
    crf: int = Field(28, ge=0, le=51, description="CRF x264 à strength=1 (0 = sans perte)")
    preset: str = Field("medium", description="Preset x264 (ultrafast … veryslow)")
    fps: int = Field(24, ge=1, le=240, description="Cadence d'encodage (images/s)")


@ATTACKS.register("h264", modality="video", params=H264Params)
class H264(Attack):
    """Compression H.264 réelle (PyAV/libx264), CRF interpolé de 0 à `crf`."""

    modality = "video"
    differentiable = False

    def __init__(self, crf: int = 28, preset: str = "medium", fps: int = 24) -> None:
        super().__init__(crf=crf, preset=preset, fps=fps)
        self.crf = int(crf)
        self.preset = preset
        self.fps = int(fps)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        _check_video(x)
        return h264_roundtrip(x, lerp(0.0, self.crf, s), fps=self.fps, preset=self.preset)


class FrameDropParams(_Params):
    p: float = Field(0.2, ge=0.0, le=1.0, description="Probabilité de perte d'une frame")


@ATTACKS.register("frame_drop", modality="video", params=FrameDropParams)
class FrameDrop(Attack):
    """Perte de frames (probabilité `p`) : une frame perdue est remplacée par la précédente."""

    modality = "video"

    def __init__(self, p: float = 0.2) -> None:
        super().__init__(p=p)
        self.p = float(p)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0 or self.p == 0.0:
            return x
        _check_video(x)
        b, f = x.shape[:2]
        dropped = per_sample_uniform(b * f, "cpu").view(b, f) < s * self.p
        dropped[:, 0] = False  # the first frame is always kept
        index = torch.arange(f).expand(b, f).clone()
        for j in range(1, f):
            index[:, j] = torch.where(dropped[:, j], index[:, j - 1], index[:, j])
        index = index.to(x.device).view(b, f, 1, 1, 1).expand_as(x)
        return torch.gather(x, 1, index)


class TemporalBlurParams(_Params):
    weight: float = Field(0.3, ge=0.0, le=1.0, description="Poids des frames voisines")


@ATTACKS.register("temporal_blur", modality="video", params=TemporalBlurParams)
class TemporalBlur(Attack):
    """Flou temporel : mélange chaque frame avec ses voisines (poids `weight`)."""

    modality = "video"

    def __init__(self, weight: float = 0.3) -> None:
        super().__init__(weight=weight)
        self.weight = float(weight)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0 or self.weight == 0.0:
            return x
        _check_video(x)
        w = s * self.weight
        prev = torch.cat([x[:, :1], x[:, :-1]], dim=1)
        nxt = torch.cat([x[:, 1:], x[:, -1:]], dim=1)
        return ((1.0 - w) * x + 0.5 * w * (prev + nxt)).clamp(0.0, 1.0)
