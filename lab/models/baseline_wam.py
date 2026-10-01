"""Watermark Anything (Meta, ICLR 2025): official MIT weights (``wam_mit.pth``) as a lab model.

Architecture re-implemented in ``_meta_modules.py`` (see ``THIRD_PARTY_NOTICES.md``); the
pipeline follows ``watermark_anything.models.wam.Wam.embed/detect``. Images are
ImageNet-normalised, resized to 256×256, the VAE embedder predicts a ``tanh`` watermark that
is upscaled and added with ``scaling_w = 2.0`` in normalised space, then attenuated by the
luminance JND map (``jnd_1_3_blue``: R and G halved). In pixel units:
``x_w = x + jnd(x) · 2.0 · wm · std``.

Detection: SAM ViT-B/16 + pixel decoder → per-pixel ``[1 + 32]`` logits at 256×256
(channel 0: watermark mask). ``decoding: masked`` averages the bit logits over the pixels
predicted as watermarked (mask logit > 0, all pixels if none), as the official ``semihard``
inference; ``mean`` averages over the whole image (Video Seal's WAM baseline). Threshold 0
(the official helper compares the averaged logits with 0.5, a quirk not reproduced).
"""

from __future__ import annotations

from typing import Literal

import torch
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from lab.models._meta_modules import (
    IMAGENET_MEAN,
    IMAGENET_STD,
    JND,
    SegmentationExtractor,
    VAEEmbedder,
)
from wmlab.models.pretrained import PretrainedWatermarkModel, load_official_checkpoint, resize
from wmlab.registry import MODELS
from wmlab.weights import WeightsFile, register_weights

IMG_SIZE = 256
NATIVE_NBITS = 32

register_weights(WeightsFile(
    name="wam_mit", url="https://dl.fbaipublicfiles.com/watermark_anything/wam_mit.pth",
    filename="wam_mit.pth", size_mb=378, license="MIT",
    source="https://github.com/facebookresearch/watermark-anything",
    sha256="90ef232384e023bd63245eb0c131abd69d2afc7b8f17a71ccedceb542bf009e2",
    description="Watermark Anything, entraîné sur SA-1B (32 bits ; la version COCO est CC-BY-NC)"))


class WamParams(BaseModel):
    """Hyper-parameters of ``wam`` (defaults = official settings)."""

    model_config = ConfigDict(extra="forbid")

    scaling_w: float = Field(2.0, gt=0.0, le=10.0,
                             description="Amplitude du tatouage (réglage officiel : 2,0)")
    jnd: bool = Field(True, description="Atténuation perceptuelle JND (réglage officiel)")
    decoding: Literal["masked", "mean"] = Field(
        "masked", description="Agrégation des bits par pixel : zone détectée (officiel) ou "
                              "image entière")
    download: bool = Field(False, description="Télécharger les poids officiels s'ils sont "
                                              "absents de la machine")


@MODELS.register("wam", modality="image", params=WamParams)
class WatermarkAnything(PretrainedWatermarkModel):
    """Watermark Anything (Meta, 2025), poids officiels MIT : 32 bits décodés par pixel puis agrégés sur la zone détectée, baseline figée (évaluation seule, phases: [])."""

    native_nbits = NATIVE_NBITS

    def __init__(self, nbits: int, scaling_w: float = 2.0, jnd: bool = True,
                 decoding: str = "masked", download: bool = False) -> None:
        super().__init__(nbits)
        if decoding not in ("masked", "mean"):
            raise ValueError(f"decoding inconnu '{decoding}' — valeurs possibles : masked, mean")
        self.scaling_w = float(scaling_w)
        self.decoding = decoding
        self.embedder = VAEEmbedder(NATIVE_NBITS)
        self.detector = SegmentationExtractor(NATIVE_NBITS, img_size=IMG_SIZE)
        self.jnd = JND(in_channels=1, out_channels=3, blue=True) if jnd else None
        self.register_buffer("mean", torch.tensor(IMAGENET_MEAN).view(1, 3, 1, 1),
                             persistent=False)
        self.register_buffer("std", torch.tensor(IMAGENET_STD).view(1, 3, 1, 1),
                             persistent=False)
        state = load_official_checkpoint(self.weights_file("wam_mit", download=download))
        self.load_official({k: v for k, v in state.items()
                            if k.startswith(("embedder.", "detector."))})
        self.eval()

    def _normalize(self, x: Tensor) -> Tensor:
        return (x - self.mean.to(x)) / self.std.to(x)

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] in [0,1], msg [B,nbits] → x_w (same shape, clamped to [0,1])."""
        self.check_message(x, msg)
        wm = self.embedder(self._normalize(resize(x, IMG_SIZE)), self.to_native(msg))
        delta = self.scaling_w * resize(wm, tuple(x.shape[-2:])) * self.std.to(x)
        if self.jnd is not None:
            delta = self.jnd.heatmaps(x) * delta
        return (x + delta).clamp(0.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] → logits [B,nbits] aggregated over the pixels."""
        preds = self.detector(self._normalize(resize(x, IMG_SIZE)))     # [B, 1+32, 256, 256]
        bits = preds[:, 1:]
        mean = bits.mean(dim=(-2, -1))
        if self.decoding == "mean":
            return self.from_native(mean)
        mask = (preds[:, :1] > 0).to(bits.dtype)
        count = mask.sum(dim=(-2, -1))
        masked = (bits * mask).sum(dim=(-2, -1)) / count.clamp_min(1.0)
        return self.from_native(torch.where(count > 0, masked, mean))
