"""Builtin preprocessing ops (ARCHITECTURE.md §4.3).

An op is a picklable callable ``item_x -> item_x`` working on one item (no batch dim):
image ``[3,H,W]``, video ``[F,3,H,W]`` (image ops apply to every frame with the *same* random
parameters, keeping the clip coherent), audio ``[1,T]``. The class attribute ``deterministic``
is False when the op draws random numbers (always from torch's RNG, which the DataLoader
seeds per worker).
"""

from __future__ import annotations

import math
from typing import ClassVar

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.registry import PREPROCESS

Size = int | tuple[int, int]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Preprocess:
    """Base class of preprocessing ops."""

    deterministic: ClassVar[bool] = True

    def __call__(self, x: Tensor) -> Tensor:  # pragma: no cover — abstract
        raise NotImplementedError

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in vars(self).items())
        return f"{type(self).__name__}({fields})"


def _pair(size: int | list[int] | tuple[int, int]) -> tuple[int, int]:
    if isinstance(size, int):
        return size, size
    values = list(size)
    if len(values) != 2:
        raise ValueError(f"taille invalide {size!r} : un entier ou [hauteur, largeur]")
    return int(values[0]), int(values[1])


def _check_visual(x: Tensor) -> None:
    if x.ndim not in (3, 4):
        raise ValueError(f"prétraitement image/vidéo : [3,H,W] ou [F,3,H,W] attendu, "
                         f"reçu {tuple(x.shape)}")


def resize_to(x: Tensor, size: tuple[int, int]) -> Tensor:
    """Bilinear (anti-aliased) resize of ``[..., C, H, W]`` to ``size``."""
    h, w = x.shape[-2:]
    if (h, w) == tuple(size):
        return x
    flat = x.reshape(-1, *x.shape[-3:]).float()
    out = F.interpolate(flat, size=size, mode="bilinear", align_corners=False,
                        antialias=size[0] < h or size[1] < w)
    return out.clamp(0.0, 1.0).reshape(*x.shape[:-2], *size)


def resize_short_side(x: Tensor, short: int) -> Tensor:
    """Resize so that the short side equals ``short`` (aspect ratio kept)."""
    h, w = x.shape[-2:]
    scale = short / min(h, w)
    return resize_to(x, (max(short, round(h * scale)), max(short, round(w * scale))))


def _ensure_min_size(x: Tensor, size: tuple[int, int]) -> Tensor:
    """Upscale (aspect kept) when the image is smaller than the crop."""
    h, w = x.shape[-2:]
    if h >= size[0] and w >= size[1]:
        return x
    scale = max(size[0] / h, size[1] / w)
    return resize_to(x, (max(size[0], math.ceil(h * scale)), max(size[1], math.ceil(w * scale))))


def _randint(high: int) -> int:
    """Uniform integer in ``[0, high]``."""
    return int(torch.randint(0, high + 1, (1,))) if high > 0 else 0


# -- image / video --------------------------------------------------------------------------
class SizeParams(_Params):
    size: int | tuple[int, int] = Field(description="Côté (entier) ou [hauteur, largeur]")


@PREPROCESS.register("resize", modality=["image", "video"], params=SizeParams)
class ResizeOp(Preprocess):
    """Redimensionne : entier = petit côté (proportions gardées), [h, w] = taille exacte."""

    deterministic = True

    def __init__(self, size: Size) -> None:
        self.size = size if isinstance(size, int) else _pair(size)

    def __call__(self, x: Tensor) -> Tensor:
        _check_visual(x)
        if isinstance(self.size, int):
            return resize_short_side(x, self.size)
        return resize_to(x, self.size)


@PREPROCESS.register("center_crop", modality=["image", "video"], params=SizeParams)
class CenterCropOp(Preprocess):
    """Recadrage central (agrandit d'abord si l'image est plus petite)."""

    deterministic = True

    def __init__(self, size: Size) -> None:
        self.size = _pair(size)

    def __call__(self, x: Tensor) -> Tensor:
        _check_visual(x)
        x = _ensure_min_size(x, self.size)
        h, w = x.shape[-2:]
        top, left = (h - self.size[0]) // 2, (w - self.size[1]) // 2
        return x[..., top:top + self.size[0], left:left + self.size[1]]


@PREPROCESS.register("random_crop", modality=["image", "video"], params=SizeParams)
class RandomCropOp(Preprocess):
    """Recadrage à position aléatoire (agrandit d'abord si l'image est plus petite)."""

    deterministic = False

    def __init__(self, size: Size) -> None:
        self.size = _pair(size)

    def __call__(self, x: Tensor) -> Tensor:
        _check_visual(x)
        x = _ensure_min_size(x, self.size)
        h, w = x.shape[-2:]
        top, left = _randint(h - self.size[0]), _randint(w - self.size[1])
        return x[..., top:top + self.size[0], left:left + self.size[1]]


class RandomResizedCropParams(_Params):
    size: int | tuple[int, int] = Field(description="Côté (entier) ou [hauteur, largeur]")
    scale: tuple[float, float] = Field((0.5, 1.0), description="Fraction de surface [min, max]")
    ratio: tuple[float, float] = Field((3 / 4, 4 / 3), description="Rapport largeur/hauteur")


@PREPROCESS.register("random_resized_crop", modality=["image", "video"],
                     params=RandomResizedCropParams)
class RandomResizedCropOp(Preprocess):
    """Recadrage aléatoire (surface ∈ scale, proportions ∈ ratio) redimensionné à `size`."""

    deterministic = False

    def __init__(self, size: Size, scale: tuple[float, float] = (0.5, 1.0),
                 ratio: tuple[float, float] = (3 / 4, 4 / 3)) -> None:
        self.size = _pair(size)
        self.scale = (float(scale[0]), float(scale[1]))
        self.ratio = (float(ratio[0]), float(ratio[1]))
        if not 0 < self.scale[0] <= self.scale[1] <= 1:
            raise ValueError("scale doit vérifier 0 < min ≤ max ≤ 1")

    def _window(self, h: int, w: int) -> tuple[int, int, int, int]:
        area = h * w
        log_ratio = (math.log(self.ratio[0]), math.log(self.ratio[1]))
        for _ in range(10):  # torchvision's rejection sampling
            target = area * float(torch.empty(1).uniform_(*self.scale))
            aspect = math.exp(float(torch.empty(1).uniform_(*log_ratio)))
            cw = round(math.sqrt(target * aspect))
            ch = round(math.sqrt(target / aspect))
            if 0 < cw <= w and 0 < ch <= h:
                return _randint(h - ch), _randint(w - cw), ch, cw
        side = min(h, w)  # fallback: central square crop
        return (h - side) // 2, (w - side) // 2, side, side

    def __call__(self, x: Tensor) -> Tensor:
        _check_visual(x)
        h, w = x.shape[-2:]
        top, left, ch, cw = self._window(h, w)
        return resize_to(x[..., top:top + ch, left:left + cw], self.size)


class FlipParams(_Params):
    p: float = Field(0.5, ge=0.0, le=1.0, description="Probabilité de retournement")


@PREPROCESS.register("hflip", modality=["image", "video"], params=FlipParams)
class HFlipOp(Preprocess):
    """Retournement horizontal avec probabilité `p`."""

    deterministic = False

    def __init__(self, p: float = 0.5) -> None:
        self.p = float(p)

    def __call__(self, x: Tensor) -> Tensor:
        _check_visual(x)
        if float(torch.rand(1)) < self.p:
            return x.flip(-1)
        return x


# -- audio ----------------------------------------------------------------------------------
def _check_audio(x: Tensor) -> None:
    if x.ndim != 2:
        raise ValueError(f"prétraitement audio : [1, T] attendu, reçu {tuple(x.shape)}")


class RandomClipParams(_Params):
    seconds: float = Field(gt=0.0, description="Durée de l'extrait (s)")
    sample_rate: int = Field(16000, ge=1000, description="Fréquence d'échantillonnage (Hz)")


@PREPROCESS.register("random_clip", modality="audio", params=RandomClipParams)
class RandomClipOp(Preprocess):
    """Extrait aléatoire de `seconds` secondes (complété par du silence si trop court)."""

    deterministic = False

    def __init__(self, seconds: float, sample_rate: int = 16000) -> None:
        self.seconds = float(seconds)
        self.sample_rate = int(sample_rate)
        self.length = max(1, round(self.seconds * self.sample_rate))

    def __call__(self, x: Tensor) -> Tensor:
        _check_audio(x)
        n = x.shape[-1]
        if n < self.length:
            return F.pad(x, (0, self.length - n))
        start = _randint(n - self.length)
        return x[..., start:start + self.length]


class NormalizePeakParams(_Params):
    peak: float = Field(0.9, gt=0.0, le=1.0, description="Crête visée")


@PREPROCESS.register("normalize_peak", modality="audio", params=NormalizePeakParams)
class NormalizePeakOp(Preprocess):
    """Normalise la crête à `peak` (les silences restent inchangés)."""

    deterministic = True

    def __init__(self, peak: float = 0.9) -> None:
        self.peak = float(peak)

    def __call__(self, x: Tensor) -> Tensor:
        _check_audio(x)
        current = float(x.abs().max()) if x.numel() else 0.0
        if current < 1e-6:
            return x
        return (x * (self.peak / current)).clamp(-1.0, 1.0)


class RandomGainParams(_Params):
    db: float = Field(6.0, ge=0.0, le=60.0, description="Gain aléatoire dans ±db")


@PREPROCESS.register("random_gain", modality="audio", params=RandomGainParams)
class RandomGainOp(Preprocess):
    """Gain aléatoire uniforme dans ±`db` dB (écrêté dans [-1, 1])."""

    deterministic = False

    def __init__(self, db: float = 6.0) -> None:
        self.db = float(db)

    def __call__(self, x: Tensor) -> Tensor:
        _check_audio(x)
        gain_db = float(torch.empty(1).uniform_(-self.db, self.db)) if self.db > 0 else 0.0
        return (x * 10.0 ** (gain_db / 20.0)).clamp(-1.0, 1.0)


# -- video ----------------------------------------------------------------------------------
class TemporalCropParams(_Params):
    frames: int = Field(ge=1, description="Nombre de frames conservées")
    random: bool = Field(True, description="Début aléatoire (sinon centré)")


@PREPROCESS.register("temporal_crop", modality="video", params=TemporalCropParams)
class TemporalCropOp(Preprocess):
    """Sous-séquence de `frames` frames (début aléatoire ou centré ; répète la dernière si court)."""

    deterministic = False

    def __init__(self, frames: int, random: bool = True) -> None:
        self.frames = int(frames)
        self.random = bool(random)
        self.deterministic = not self.random

    def __call__(self, x: Tensor) -> Tensor:
        if x.ndim != 4:
            raise ValueError(f"temporal_crop : [F, 3, H, W] attendu, reçu {tuple(x.shape)}")
        f = x.shape[0]
        if f < self.frames:
            return torch.cat([x, x[-1:].expand(self.frames - f, *x.shape[1:])], dim=0)
        slack = f - self.frames
        start = _randint(slack) if self.random else slack // 2
        return x[start:start + self.frames]
