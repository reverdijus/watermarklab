"""Loss interface (ARCHITECTURE.md §4.4): a module built with its params, called on a StepBatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from torch import Tensor, nn

if TYPE_CHECKING:  # pragma: no cover
    from wmlab.models.base import WatermarkModel


@dataclass
class StepBatch:
    """Everything a loss term may need for one training/validation step."""

    x: Tensor          # original signal
    x_w: Tensor        # watermarked signal
    x_att: Tensor      # attacked watermarked signal
    msg: Tensor        # [B, nbits] in {0, 1}
    logits: Tensor     # [B, nbits] extracted from x_att
    modality: str
    model: "WatermarkModel"
    step: int
    phase: str
    extras: dict[str, Any] = field(default_factory=dict)  # e.g. {"sample_rate": 16000}


class Loss(nn.Module):
    """Base class of loss terms: ``forward(batch) -> scalar Tensor``."""

    def forward(self, batch: StepBatch) -> Tensor:  # pragma: no cover — abstract
        raise NotImplementedError(f"{type(self).__name__}.forward n'est pas implémenté")
