"""Video model applying a registered image model frame by frame.

The same message is embedded in every frame; the extractor runs on each frame and aggregates
the per-frame logits over time (mean by default, or median which is more robust to a few
destroyed frames).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.models.base import WatermarkModel
from wmlab.registry import MODELS


class VideoFramewiseParams(BaseModel):
    """Hyper-parameters of ``video_framewise`` (``model.params``)."""

    model_config = ConfigDict(extra="forbid")

    image_model: str = Field("hidden", min_length=1,
                             description="Nom du modèle image appliqué à chaque frame")
    image_params: dict[str, Any] = Field(default_factory=dict,
                                         description="Paramètres du modèle image")
    aggregate: Literal["mean", "median"] = Field(
        "mean", description="Agrégation des logits sur les frames")


@MODELS.register("video_framewise", modality="video", params=VideoFramewiseParams)
class VideoFramewiseModel(WatermarkModel):
    """Applique un modèle image frame par frame et agrège les logits sur les frames."""

    modality = "video"

    def __init__(self, nbits: int, image_model: str = "hidden",
                 image_params: dict[str, Any] | None = None, aggregate: str = "mean") -> None:
        super().__init__(nbits)
        entry = MODELS.get(image_model)
        if not entry.supports("image"):
            raise ValueError(
                f"video_framewise : le modèle '{image_model}' n'est pas un modèle image "
                f"(modalité : {entry.modality_label()})"
            )
        if aggregate not in ("mean", "median"):
            raise ValueError(f"agrégation inconnue '{aggregate}' — valeurs possibles : mean, median")
        self.image_model_name = image_model
        self.aggregate = aggregate
        self.image = MODELS.build(image_model, nbits=self.nbits, **(image_params or {}))

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,F,3,H,W] in [0,1], msg [B,nbits] → x_w (same shape, clamped to [0,1])."""
        self.check_message(x, msg)
        b, f = x.shape[:2]
        frames = x.reshape(b * f, *x.shape[2:])
        out = self.image.embed(frames, msg.repeat_interleave(f, dim=0))
        return out.reshape(x.shape).clamp(0.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,F,3,H,W] → logits [B,nbits] aggregated over frames."""
        b, f = x.shape[:2]
        logits = self.image.extract(x.reshape(b * f, *x.shape[2:])).reshape(b, f, self.nbits)
        if self.aggregate == "median":
            return logits.median(dim=1).values
        return logits.mean(dim=1)
