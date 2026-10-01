"""Gabarit d'architecture IMAGE pour Watermark Lab — à copier dans lab/models/<nom>.py.

Contrat (ARCHITECTURE.md §4.1) :
- ``__init__(self, nbits, **params)`` : ``nbits`` vient de project.yaml (message_bits), JAMAIS
  de model.params ; les autres params sont exposés via le modèle pydantic ci-dessous.
- ``embed(x, msg)`` : x ``[B,3,H,W]`` dans [0,1], msg ``[B,nbits]`` dans {0,1} → même forme,
  même plage (clampé).
- ``extract(x)`` : ``[B,3,H,W]`` → logits ``[B,nbits]`` (> 0 ⇒ bit 1, pas de sigmoïde).
- ``forward = embed`` (hérité), ``aux_losses`` et ``param_groups`` optionnels.

Ce gabarit reprend EXACTEMENT les conventions de l'exemple commenté livré dans le workspace,
lab/models/example_residual_unet.py (nom de registre ``example_unet`` : U-Net résiduel,
message injecté au goulot, skips par ConvTranspose + concaténation, GroupNorm, padding pour
accepter toute taille, extracteur à pooling global). L'exemple est la référence lisible ;
ce fichier est la version compacte à renommer (``unet_wm`` → votre nom) et à modifier.
Si ``example_unet`` suffit tel quel, l'utiliser directement dans l'expérience (il est dans le
registre et la bibliothèque de l'app) plutôt que d'en créer une copie.

Utilisation dans une expérience :
    model: {name: unet_wm, params: {channels: 32, depth: 3, strength: 1.0}}
Vérifier : .venv/bin/python -m wmlab.cli plugin-test model unet_wm

Audio / vidéo : voir references/interfaces.md (Conv1d sur [B,1,T] ; vidéo = reshape
[B*F,3,H,W] puis moyenne des logits sur F) ou « wmlab new plugin model <nom> --modality M ».
"""

from __future__ import annotations

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.models.base import WatermarkModel
from wmlab.registry import MODELS


class UNetWMParams(BaseModel):
    """Hyperparamètres exposés dans le YAML (model.params). Défauts = config recommandée."""

    model_config = ConfigDict(extra="forbid")

    channels: int = Field(32, ge=4, le=512, description="Canaux au premier niveau du U-Net")
    depth: int = Field(3, ge=1, le=5, description="Nombre de sous-échantillonnages (×2)")
    strength: float = Field(1.0, gt=0.0, le=10.0, description="Amplitude du résidu (tanh × strength)")


def conv_block(c_in: int, c_out: int) -> nn.Sequential:
    """Two 3×3 convs + GroupNorm + GELU (GroupNorm: independent of the batch size, unlike BatchNorm)."""
    groups = min(8, c_out)
    return nn.Sequential(
        nn.Conv2d(c_in, c_out, 3, padding=1), nn.GroupNorm(groups, c_out), nn.GELU(),
        nn.Conv2d(c_out, c_out, 3, padding=1), nn.GroupNorm(groups, c_out), nn.GELU(),
    )


class UNetEmbedder(nn.Module):
    """Image + message → bounded residual in [-1, 1] (message injected at the bottleneck)."""

    def __init__(self, nbits: int, channels: int, depth: int) -> None:
        super().__init__()
        self.depth = depth
        widths = [channels * 2 ** i for i in range(depth + 1)]      # e.g. [32, 64, 128, 256]
        self.stem = conv_block(3, widths[0])
        self.down = nn.ModuleList(
            nn.Sequential(nn.Conv2d(widths[i], widths[i + 1], 4, stride=2, padding=1),
                          conv_block(widths[i + 1], widths[i + 1]))
            for i in range(depth))
        self.msg_proj = nn.Linear(nbits, widths[-1])               # bits (±1) → bottleneck channels
        self.up = nn.ModuleList(nn.ConvTranspose2d(widths[i + 1], widths[i], 2, stride=2)
                                for i in reversed(range(depth)))
        self.merge = nn.ModuleList(conv_block(2 * widths[i], widths[i])
                                   for i in reversed(range(depth)))
        self.to_residual = nn.Conv2d(widths[0], 3, 1)
        nn.init.normal_(self.to_residual.weight, std=1e-3)        # start close to the identity (high PSNR)
        nn.init.zeros_(self.to_residual.bias)

    def forward(self, x: Tensor, msg: Tensor) -> Tensor:
        h, w = x.shape[-2:]
        # Pad to a multiple of 2**depth so that any input size (odd included) is accepted,
        # then crop the residual back to (h, w).
        multiple = 2 ** self.depth
        feats = F.pad(x * 2.0 - 1.0, (0, (-w) % multiple, 0, (-h) % multiple), mode="replicate")
        skips = []
        feats = self.stem(feats)
        for down in self.down:
            skips.append(feats)
            feats = down(feats)
        feats = feats + self.msg_proj(msg * 2.0 - 1.0)[:, :, None, None]   # broadcast over H×W
        for up, merge in zip(self.up, self.merge):
            feats = merge(torch.cat([up(feats), skips.pop()], dim=1))
        return torch.tanh(self.to_residual(feats))[..., :h, :w]


class ConvExtractor(nn.Module):
    """Image → logits; global average pooling makes it resolution independent."""

    def __init__(self, nbits: int, channels: int, depth: int) -> None:
        super().__init__()
        layers: list[nn.Module] = [conv_block(3, channels)]
        width = channels
        for _ in range(depth):
            layers += [nn.Conv2d(width, 2 * width, 3, stride=2, padding=1), nn.GELU()]
            width *= 2
        self.features = nn.Sequential(*layers)
        self.head = nn.Linear(width, nbits)

    def forward(self, x: Tensor) -> Tensor:
        return self.head(self.features(x * 2.0 - 1.0).mean(dim=(2, 3)))


@MODELS.register("unet_wm", modality="image", params=UNetWMParams)
class UNetWM(WatermarkModel):
    """Embedder U-Net (message injecté au goulot) + extracteur convolutif à pooling global."""

    modality = "image"

    def __init__(self, nbits: int, channels: int = 32, depth: int = 3,
                 strength: float = 1.0) -> None:
        super().__init__(nbits)
        self.strength = float(strength)
        # Submodule names = parameter prefixes usable in `phases[].freeze` ("embedder.", "extractor.").
        self.embedder = UNetEmbedder(self.nbits, channels, depth)
        self.extractor = ConvExtractor(self.nbits, channels, depth)

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] in [0,1], msg [B,nbits] → x_w (same shape, clamped to [0,1])."""
        if msg.shape != (x.shape[0], self.nbits):
            raise ValueError(f"message de forme {tuple(msg.shape)} : attendu [{x.shape[0]}, {self.nbits}]")
        residual = self.embedder(x, msg.to(x.dtype))
        return (x + self.strength * residual).clamp(0.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] (possibly attacked) → logits [B,nbits]."""
        return self.extractor(x)

    def param_groups(self) -> dict[str, list[nn.Parameter]]:
        """Named groups (useful to freeze or inspect embedder / extractor separately)."""
        return {"embedder": list(self.embedder.parameters()),
                "extractor": list(self.extractor.parameters())}

    # Optional: model-specific regularisation added by the trainer to the total loss.
    # def aux_losses(self, batch: "StepBatch") -> dict[str, Tensor]:
    #     return {"residual_l1": 1e-3 * (batch.x_w - batch.x).abs().mean()}
