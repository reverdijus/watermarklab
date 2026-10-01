"""Audio watermarking model in the spirit of AudioSeal, reduced to a light 1D conv U-Net.

Embedder: strided residual conv encoder (``blocks`` halvings of the time axis), message
injected at the bottleneck (sum of per-bit embeddings as in AudioSeal, plus a coarse learned
map over ``segments`` time segments), transposed-conv decoder with skip connections, output
residual bounded by ``strength * tanh``.

Detector: strided residual conv stack projected to ``nbits`` channels, pooled over time
(global mean + ``segments`` adaptive segments), hence length independent.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.models.base import WatermarkModel, signed_message
from wmlab.models.layers import adaptive_avg_pool1d
from wmlab.registry import MODELS

MAX_CHANNEL_MULT = 8
MSG_MAP_CHANNELS = 8


class AudioConvParams(BaseModel):
    """Hyper-parameters of ``audio_conv`` (``model.params``)."""

    model_config = ConfigDict(extra="forbid")

    channels: int = Field(32, ge=4, le=256, description="Canaux du premier niveau")
    blocks: int = Field(4, ge=1, le=8, description="Niveaux de sous-échantillonnage (×2 chacun)")
    strength: float = Field(1.0, gt=0.0, le=10.0,
                            description="Amplitude maximale du résidu (tanh × strength)")
    segments: int = Field(8, ge=1, le=64, description="Segments temporels du pooling adaptatif "
                                                      "(1 = pooling global seul)")


def _channels(base: int, level: int) -> int:
    return base * min(2 ** level, MAX_CHANNEL_MULT)


def _dilation(level: int) -> int:
    """Alternate dilations 1 / 3 across levels (larger receptive field at no cost)."""
    return 3 if level % 2 else 1


class ResUnit1d(nn.Module):
    """Dilated residual unit (ELU → conv k3 → ELU → conv k1)."""

    def __init__(self, channels: int, dilation: int = 1) -> None:
        super().__init__()
        self.block = nn.Sequential(
            nn.ELU(),
            nn.Conv1d(channels, channels, 3, padding=dilation, dilation=dilation),
            nn.ELU(),
            nn.Conv1d(channels, channels, 1),
        )

    def forward(self, x: Tensor) -> Tensor:
        return x + self.block(x)


class AudioEmbedder(nn.Module):
    """Signal + message → residual in ``[-1, 1]``."""

    def __init__(self, nbits: int, channels: int, blocks: int, segments: int) -> None:
        super().__init__()
        self.nbits = nbits
        self.segments = segments
        self.conv_in = nn.Conv1d(1, channels, 7, padding=3)
        self.down = nn.ModuleList()
        for level in range(blocks):
            c_in, c_out = _channels(channels, level), _channels(channels, level + 1)
            self.down.append(nn.Sequential(
                ResUnit1d(c_in, dilation=_dilation(level)), nn.ELU(),
                nn.Conv1d(c_in, c_out, 4, stride=2, padding=1)))
        bottleneck = _channels(channels, blocks)
        self.msg_embedding = nn.Embedding(2 * nbits, bottleneck)
        self.msg_map = nn.Linear(nbits, MSG_MAP_CHANNELS * segments)
        self.fuse = nn.Conv1d(bottleneck + MSG_MAP_CHANNELS, bottleneck, 3, padding=1)
        self.up = nn.ModuleList()
        for level in reversed(range(blocks)):
            c_in, c_out = _channels(channels, level + 1), _channels(channels, level)
            self.up.append(nn.ModuleDict({
                "upsample": nn.Sequential(nn.ELU(), nn.ConvTranspose1d(c_in, c_out, 4, stride=2,
                                                                       padding=1)),
                # no refinement at full resolution (cheap), skip connections carry the detail
                "refine": ResUnit1d(c_out, dilation=_dilation(level)) if level > 0
                else nn.Identity(),
            }))
        self.conv_out = nn.Sequential(nn.ELU(), nn.Conv1d(channels, 1, 7, padding=3))

    def forward(self, x: Tensor, msg: Tensor) -> Tensor:
        h = self.conv_in(x)
        skips = [h]
        for down in self.down:
            h = down(h)
            skips.append(h)
        bits = msg.long().clamp(0, 1)
        index = 2 * torch.arange(self.nbits, device=x.device) + bits  # [B, nbits]
        h = h + self.msg_embedding(index).sum(dim=1)[:, :, None]
        coarse = self.msg_map(signed_message(msg).to(x.dtype)).view(
            x.shape[0], MSG_MAP_CHANNELS, self.segments)
        coarse = F.interpolate(coarse, size=h.shape[-1], mode="linear", align_corners=False)
        h = self.fuse(torch.cat([h, coarse], dim=1))
        skips.pop()
        for up in self.up:
            h = up["refine"](up["upsample"](h) + skips.pop())
        return torch.tanh(self.conv_out(h))


class AudioDetector(nn.Module):
    """Signal → message logits (length independent)."""

    def __init__(self, nbits: int, channels: int, blocks: int, segments: int) -> None:
        super().__init__()
        self.segments = segments
        layers: list[nn.Module] = [nn.Conv1d(1, channels, 7, padding=3)]
        for level in range(blocks):
            c_in, c_out = _channels(channels, level), _channels(channels, level + 1)
            layers += [ResUnit1d(c_in, dilation=_dilation(level)), nn.ELU(),
                       nn.Conv1d(c_in, c_out, 4, stride=2, padding=1)]
        layers += [nn.ELU(), nn.Conv1d(_channels(channels, blocks), nbits, 1)]
        self.features = nn.Sequential(*layers)
        n_cells = 1 + (segments if segments > 1 else 0)
        self.head = nn.Linear(nbits * n_cells, nbits)

    def forward(self, x: Tensor) -> Tensor:
        feats = self.features(x)
        pooled = [feats.mean(dim=-1)]
        if self.segments > 1:
            pooled.append(adaptive_avg_pool1d(feats, self.segments).flatten(1))
        return self.head(torch.cat(pooled, dim=1))


@MODELS.register("audio_conv", modality="audio", params=AudioConvParams)
class AudioConvModel(WatermarkModel):
    """Encodeur résiduel 1D + détecteur conv 1D façon AudioSeal-lite (indépendant de la durée)."""

    modality = "audio"

    def __init__(self, nbits: int, channels: int = 32, blocks: int = 4, strength: float = 1.0,
                 segments: int = 8) -> None:
        super().__init__(nbits)
        self.strength = float(strength)
        self.hop = 2 ** blocks
        self.encoder = AudioEmbedder(self.nbits, channels, blocks, segments)
        self.decoder = AudioDetector(self.nbits, channels, blocks, segments)

    def _pad(self, x: Tensor) -> tuple[Tensor, int]:
        length = x.shape[-1]
        target = max(self.hop, -(-length // self.hop) * self.hop)
        return (F.pad(x, (0, target - length)) if target != length else x), length

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,1,T] in [-1,1], msg [B,nbits] → x_w (same shape, clamped to [-1,1])."""
        self.check_message(x, msg)
        padded, length = self._pad(x)
        residual = self.encoder(padded, msg)[..., :length]
        return (x + self.strength * residual).clamp(-1.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,1,T] → logits [B,nbits] (any length; very short inputs are zero-padded)."""
        if x.shape[-1] < self.hop:
            x = F.pad(x, (0, self.hop - x.shape[-1]))
        return self.decoder(x)
