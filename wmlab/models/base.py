"""Base class of every watermarking architecture (ARCHITECTURE.md §4.1).

A model embeds a binary message into a signal (``embed``) and recovers it as logits
(``extract``). Tensor conventions (§2): image ``[B,3,H,W]`` in ``[0,1]``, audio ``[B,1,T]``
in ``[-1,1]``, video ``[B,F,3,H,W]`` in ``[0,1]``; messages ``[B,nbits]`` in ``{0,1}``;
logits ``[B,nbits]`` with ``> 0`` meaning bit 1.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import torch
from torch import Tensor, nn

if TYPE_CHECKING:  # pragma: no cover
    from wmlab.losses.base import StepBatch

MODALITY_RANGES: dict[str, tuple[float, float]] = {
    "image": (0.0, 1.0),
    "audio": (-1.0, 1.0),
    "video": (0.0, 1.0),
}


def clamp_to_modality(x: Tensor, modality: str) -> Tensor:
    """Clamp ``x`` to the value range of ``modality``."""
    lo, hi = MODALITY_RANGES[modality]
    return x.clamp(lo, hi)


def signed_message(msg: Tensor) -> Tensor:
    """Map a ``{0,1}`` message to ``{-1,+1}`` (float, same device)."""
    return msg.float() * 2.0 - 1.0


class WatermarkModel(nn.Module):
    """Embedder + extractor pair; subclasses implement ``embed`` and ``extract``."""

    modality: ClassVar[str] = "image"

    def __init__(self, nbits: int) -> None:
        super().__init__()
        if int(nbits) <= 0:
            raise ValueError(f"nbits doit être un entier strictement positif (reçu {nbits!r})")
        self.nbits = int(nbits)

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """Return the watermarked signal (same shape and range as ``x``, clamped)."""
        raise NotImplementedError(f"{type(self).__name__}.embed n'est pas implémenté")

    def extract(self, x: Tensor) -> Tensor:
        """Return message logits ``[B, nbits]`` (``> 0`` ⇒ bit 1)."""
        raise NotImplementedError(f"{type(self).__name__}.extract n'est pas implémenté")

    def forward(self, x: Tensor, msg: Tensor) -> Tensor:
        """Alias of :meth:`embed`."""
        return self.embed(x, msg)

    def aux_losses(self, batch: "StepBatch") -> dict[str, Tensor]:
        """Extra model-specific loss terms added by the trainer (default: none)."""
        return {}

    def param_groups(self) -> dict[str, list[nn.Parameter]]:
        """Named parameter groups (default: a single ``all`` group)."""
        return {"all": [p for p in self.parameters()]}

    def check_message(self, x: Tensor, msg: Tensor) -> None:
        """Raise a readable error when ``msg`` does not match ``x`` / ``nbits``."""
        if msg.ndim != 2 or msg.shape[1] != self.nbits or msg.shape[0] != x.shape[0]:
            raise ValueError(
                f"message de forme {tuple(msg.shape)} incompatible : attendu "
                f"[{x.shape[0]}, {self.nbits}] (batch, nbits)"
            )

    @torch.no_grad()
    def num_parameters(self) -> int:
        """Total number of parameters."""
        return sum(p.numel() for p in self.parameters())
