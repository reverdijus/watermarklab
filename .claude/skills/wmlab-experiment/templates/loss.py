"""Gabarit de perte pour Watermark Lab — à copier dans lab/losses/<nom>.py.

Contrat (ARCHITECTURE.md §4.4) : une perte est un ``nn.Module`` construit avec ses params et
appelé avec un ``StepBatch`` ; elle renvoie un **scalaire**.

    @dataclass
    class StepBatch:
        x: Tensor        # original            (image [B,3,H,W] / audio [B,1,T] / vidéo [B,F,3,H,W])
        x_w: Tensor      # tatoué = model.embed(x, msg)
        x_att: Tensor    # tatoué puis attaqué
        msg: Tensor      # [B, nbits] dans {0,1}
        logits: Tensor   # model.extract(x_att), [B, nbits]
        modality: str; model: WatermarkModel; step: int; phase: str
        extras: dict     # ex. {"sample_rate": 16000, "fps": 24}

Le poids vient du YAML (``loss.terms.<terme>.weight``, modulable par phase via
``phases[].loss_weights.<terme>``) : ne pas le coder en dur ici.

Exemple : variation totale du résidu (x_w − x) — pénalise les motifs haute fréquence,
plus visibles et plus fragiles à la compression.

Utilisation :  loss: {terms: {tv: {name: residual_tv, weight: 10.0, params: {p: 1}}}}   # TV du résidu ~1e-3 : poids ~10
Vérifier : .venv/bin/python -m wmlab.cli plugin-test loss residual_tv
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.registry import LOSSES

if TYPE_CHECKING:  # pragma: no cover
    from wmlab.losses.base import StepBatch


class ResidualTVParams(BaseModel):
    """Paramètres exposés dans le YAML (loss.terms.<terme>.params)."""

    model_config = ConfigDict(extra="forbid")

    p: Literal[1, 2] = Field(1, description="1 = TV anisotrope (L1), 2 = TV quadratique")


@LOSSES.register("residual_tv", modality=["image", "video", "audio"], params=ResidualTVParams)
class ResidualTV(nn.Module):
    """Variation totale du résidu de tatouage (lissage : moins visible, plus robuste)."""

    def __init__(self, p: int = 1) -> None:
        super().__init__()
        self.p = p

    def _norm(self, diff: Tensor) -> Tensor:
        return diff.abs().mean() if self.p == 1 else diff.pow(2).mean()

    def forward(self, batch: "StepBatch") -> Tensor:
        r = batch.x_w - batch.x
        if r.ndim == 5:                          # video [B,F,3,H,W] → frames [B*F,3,H,W]
            r = r.flatten(0, 1)
        if r.ndim == 3:                          # audio [B,1,T]: 1-D variation
            return self._norm(r[..., 1:] - r[..., :-1])
        return self._norm(r[..., 1:, :] - r[..., :-1, :]) + self._norm(r[..., :, 1:] - r[..., :, :-1])
