"""HiDDeN-style image watermarking model (Zhu et al., 2018), lightweight variant.

Embedder: conv features of the image are concatenated with the image, the message replicated
spatially (HiDDeN) and a learned coarse message map (``grid × grid``, bilinearly upsampled to
the image size), fused by a conv block and projected to an RGB residual bounded by
``strength * tanh``.

Extractor: conv stack (strided after the first blocks, for speed and receptive field) projected
to ``nbits`` channels, then pooled *adaptively*: global average (translation invariant, robust to
crops) concatenated with a ``grid × grid`` average (coarse layout, which makes the first
hundreds of steps learn much faster). Both poolings are resolution independent; ``grid=1``
gives the original HiDDeN global pooling.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.models.base import WatermarkModel, signed_message
from wmlab.models.layers import adaptive_avg_pool2d
from wmlab.registry import MODELS

MAX_DOWNSAMPLING = 3  # the extractor halves the resolution at most 3 times
MSG_MAP_CHANNELS = 8  # channels of the learned coarse message map


class HiddenParams(BaseModel):
    """Hyper-parameters of ``hidden`` (``model.params``)."""

    model_config = ConfigDict(extra="forbid")

    channels: int = Field(64, ge=4, le=512, description="Canaux des couches convolutives")
    enc_blocks: int = Field(4, ge=1, le=16, description="Blocs conv de l'embedder")
    dec_blocks: int = Field(7, ge=1, le=16, description="Blocs conv de l'extracteur")
    strength: float = Field(1.0, gt=0.0, le=10.0,
                            description="Amplitude maximale du résidu (tanh × strength)")
    grid: int = Field(4, ge=1, le=16, description="Grille du pooling adaptatif de "
                                                  "l'extracteur (1 = pooling global seul)")


def conv_bn_relu(c_in: int, c_out: int, stride: int = 1) -> nn.Sequential:
    """3×3 convolution + BatchNorm + ReLU (HiDDeN building block)."""
    return nn.Sequential(
        nn.Conv2d(c_in, c_out, 3, stride=stride, padding=1, bias=False),
        nn.BatchNorm2d(c_out),
        nn.ReLU(inplace=True),
    )


class HiddenEncoder(nn.Module):
    """Image + message → residual in ``[-1, 1]`` (scaled by ``strength`` in the model)."""

    def __init__(self, nbits: int, channels: int, blocks: int, grid: int) -> None:
        super().__init__()
        self.nbits = nbits
        self.grid = grid
        layers = [conv_bn_relu(3, channels)]
        layers += [conv_bn_relu(channels, channels) for _ in range(blocks - 1)]
        self.features = nn.Sequential(*layers)
        self.msg_map = nn.Linear(nbits, MSG_MAP_CHANNELS * grid * grid)
        self.fuse = conv_bn_relu(channels + 3 + nbits + MSG_MAP_CHANNELS, channels)
        self.to_rgb = nn.Conv2d(channels, 3, 1)

    def forward(self, x: Tensor, msg: Tensor) -> Tensor:
        b, _, h, w = x.shape
        x_in = x * 2.0 - 1.0
        signed = signed_message(msg).to(x.dtype)
        replicated = signed[:, :, None, None].expand(b, self.nbits, h, w)
        coarse = self.msg_map(signed).view(b, MSG_MAP_CHANNELS, self.grid, self.grid)
        coarse = F.interpolate(coarse, size=(h, w), mode="bilinear", align_corners=False)
        fused = self.fuse(torch.cat([self.features(x_in), x_in, replicated, coarse], dim=1))
        return torch.tanh(self.to_rgb(fused))


class HiddenDecoder(nn.Module):
    """Image → message logits (resolution independent adaptive pooling)."""

    def __init__(self, nbits: int, channels: int, blocks: int, grid: int) -> None:
        super().__init__()
        self.grid = grid
        layers = [conv_bn_relu(3, channels)]
        downsampled = 0
        for i in range(blocks - 1):
            stride = 2 if i % 2 == 1 and downsampled < MAX_DOWNSAMPLING else 1
            downsampled += int(stride == 2)
            layers.append(conv_bn_relu(channels, channels, stride))
        layers.append(conv_bn_relu(channels, nbits))
        self.features = nn.Sequential(*layers)
        n_cells = 1 + (grid * grid if grid > 1 else 0)
        self.head = nn.Linear(nbits * n_cells, nbits)

    def forward(self, x: Tensor) -> Tensor:
        feats = self.features(x * 2.0 - 1.0)
        pooled = [feats.mean(dim=(2, 3))]
        if self.grid > 1:
            pooled.append(adaptive_avg_pool2d(feats, self.grid).flatten(1))
        return self.head(torch.cat(pooled, dim=1))


@MODELS.register("hidden", modality="image", params=HiddenParams)
class HiddenModel(WatermarkModel):
    """Encodeur/décodeur convolutif à la HiDDeN (résidu borné, extracteur à pooling adaptatif)."""

    modality = "image"

    def __init__(self, nbits: int, channels: int = 64, enc_blocks: int = 4,
                 dec_blocks: int = 7, strength: float = 1.0, grid: int = 4) -> None:
        super().__init__(nbits)
        self.strength = float(strength)
        self.encoder = HiddenEncoder(self.nbits, channels, enc_blocks, grid)
        self.decoder = HiddenDecoder(self.nbits, channels, dec_blocks, grid)

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] in [0,1], msg [B,nbits] → x_w (same shape, clamped to [0,1])."""
        self.check_message(x, msg)
        residual = self.encoder(x, msg)
        return (x + self.strength * residual).clamp(0.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] → logits [B,nbits]."""
        return self.decoder(x)
