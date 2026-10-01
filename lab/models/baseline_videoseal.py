"""Video Seal 1.0 and Pixel Seal (Meta, MIT): official pretrained weights as lab models.

Architecture re-implemented in ``_meta_modules.py`` (see ``THIRD_PARTY_NOTICES.md``); the
pipeline follows ``videoseal.models.wam.Wam.embed/detect`` for images (``is_video=False``):
resize to 256×256, U-Net on the luminance (Y) channel predicts a ``tanh`` watermark, upscaled
to the input resolution, attenuated by the luminance JND map at full resolution, added with
``scaling_w = 0.2`` and clamped. Detection: resize to 256, ConvNeXt-V2 tiny, spatial mean,
linear head ``[B, 1 + 256]`` (channel 0 = detection bit, unused; bits = channels 1…256, raw
logits, threshold 0).

For video, use the builtin ``video_framewise`` with ``image_model: videoseal`` (frame by
frame; the official video mode additionally propagates the watermark between frames).
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from lab.models._meta_modules import JND, ConvnextExtractor, UnetEmbedder, rgb_to_y
from wmlab.models.pretrained import PretrainedWatermarkModel, load_official_checkpoint, resize
from wmlab.registry import MODELS
from wmlab.weights import WeightsFile, register_weights

SOURCE = "https://github.com/facebookresearch/videoseal"
IMG_SIZE = 256
NATIVE_NBITS = 256

register_weights(WeightsFile(
    name="videoseal_1.0", url="https://dl.fbaipublicfiles.com/videoseal/y_256b_img.pth",
    filename="y_256b_img.pth", size_mb=228, license="MIT", source=SOURCE,
    sha256="3d2ff2523d2a89e3532c6dfdcf693098799326e9b3e74c185e815e9baa8340a3",
    description="Video Seal 1.0 (256 bits, canal Y, JND)"))
register_weights(WeightsFile(
    name="pixelseal", url="https://dl.fbaipublicfiles.com/videoseal/pixelseal/checkpoint.pth",
    filename="checkpoint.pth", size_mb=1237, license="MIT", source=SOURCE,
    sha256="0c5665cff20eb6ce1b5aaa7d91c19dafb418bfee32d02dd3344e4ed60d9d75bd",
    description="Pixel Seal (256 bits, canal Y, JND ; checkpoint d'entraînement complet)"))


class SealParams(BaseModel):
    """Hyper-parameters of ``videoseal`` / ``pixelseal`` (defaults = official settings)."""

    model_config = ConfigDict(extra="forbid")

    scaling_w: float = Field(0.2, gt=0.0, le=5.0,
                             description="Amplitude du tatouage (réglage officiel : 0,2)")
    jnd: bool = Field(True, description="Atténuation perceptuelle JND (réglage officiel)")
    download: bool = Field(False, description="Télécharger les poids officiels s'ils sont "
                                              "absents de la machine")


class _MetaSeal(PretrainedWatermarkModel):
    """Shared implementation of the Video Seal family (256 bits, Y channel)."""

    native_nbits = NATIVE_NBITS
    weights_name: ClassVar[str] = ""
    unet_mults: ClassVar[tuple[int, ...]] = ()

    def __init__(self, nbits: int, scaling_w: float = 0.2, jnd: bool = True,
                 download: bool = False) -> None:
        super().__init__(nbits)
        self.scaling_w = float(scaling_w)
        self.embedder = UnetEmbedder(
            NATIVE_NBITS, NATIVE_NBITS, in_channels=1, out_channels=1, z_channels=16,
            num_blocks=8, activation="relu", normalization="batch",
            z_channels_mults=self.unet_mults)
        self.detector = ConvnextExtractor(NATIVE_NBITS, depths=(3, 3, 9, 3),
                                          dims=(96, 192, 384, 768))
        self.jnd = JND(in_channels=1, out_channels=1) if jnd else None
        path = self.weights_file(self.weights_name, download=download)
        state = load_official_checkpoint(path, mmap=True)["model"]
        self.load_official({k: v for k, v in state.items()
                            if k.startswith(("embedder.", "detector."))})
        self.eval()

    def _sync_frozen(self, module: nn.Module) -> None:
        """A sub-module frozen by ``phases[].freeze`` keeps the official BatchNorm statistics
        (``freeze`` stops the gradients, not the running-mean updates of train mode)."""
        if self.training:
            module.train(any(p.requires_grad for p in module.parameters()))

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] in [0,1], msg [B,nbits] → x_w (same shape, clamped to [0,1])."""
        self.check_message(x, msg)
        self._sync_frozen(self.embedder)
        small = resize(x, IMG_SIZE)
        delta = self.embedder(rgb_to_y(small), self.to_native(msg))      # [B,1,256,256]
        delta = resize(delta, tuple(x.shape[-2:]))
        if self.jnd is not None:
            delta = self.jnd.heatmaps(x) * delta
        return (x + self.scaling_w * delta).clamp(0.0, 1.0)             # Y delta on R, G, B

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] → logits [B,nbits] (detection channel dropped)."""
        self._sync_frozen(self.detector)
        return self.from_native(self.detector(resize(x, IMG_SIZE))[:, 1:])


@MODELS.register("videoseal", modality="image", params=SealParams)
class VideoSeal(_MetaSeal):
    """Video Seal 1.0 (Meta, 2024), poids officiels : 256 bits sur la luminance avec atténuation JND, baseline figée (évaluation seule, phases: [])."""

    weights_name = "videoseal_1.0"
    unet_mults = (1, 2, 4, 8)


@MODELS.register("pixelseal", modality="image", params=SealParams)
class PixelSeal(_MetaSeal):
    """Pixel Seal (Meta, 2025), poids officiels : 256 bits sur la luminance avec atténuation JND, ré-entraînable (fine-tuning depuis les poids officiels).

    Same pipeline as Video Seal with a wider U-Net (``unet_base``, channel multipliers
    2-4-8-16). Sub-modules ``embedder.`` and ``detector.`` can be frozen per phase
    (``phases[].freeze``). The official checkpoint also holds the discriminator, optimizer
    and EMA states: only ``model`` is loaded, as in ``videoseal.load("pixelseal")``.
    """

    trainable = True
    weights_name = "pixelseal"
    unet_mults = (2, 4, 8, 16)
