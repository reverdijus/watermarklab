"""InvisMark (Microsoft, WACV 2025): pretrained weights (``paper.ckpt``) as a lab model.

Encoder re-implemented from microsoft/InvisMark ``model.py`` (MIT, see
``THIRD_PARTY_NOTICES.md``); decoder = torchvision ConvNeXt-base with a LayerNorm2d → Flatten →
Linear(1024 → 100) head. The official ``Extractor`` ends with a sigmoid (threshold 0.5): its
input, the raw logit, is returned here (threshold 0). Pipeline of ``Watermark._encode``:
image in [-1, 1], resize to 256, ``diff = encoder(x256, msg) − x256`` upscaled to the input
size, ``clamp(x + diff, -1, 1)``; decoding on the 256×256 resize.

Payload: the checkpoint (``enc_mode: uuid``, 100 bits) was trained on UUIDv4 truncated to
their first 100 bits (``np.unpackbits``, MSB first). Six of them are constant in every
training message — bits 48-51 (version nibble ``0100``) and 64-65 (RFC 4122 variant ``10``) —
and the decoder only outputs those values: the real capacity is 94 bits.
``uuid_payload: true`` forces them at embedding and removes them from the payload (project
``message_bits`` ≤ 94); by default the project bits use all 100 positions, which measures the
raw behaviour (~50 % on the 6 frozen positions).

Weights: unofficial Hugging Face mirror ``shelock/watermark-invismark-model`` (the official
OneDrive link has no stable URL); ``torch.load`` in safe mode with a stub for the pickled
``configs.ModelConfig``. Requires torchvision (``uv sync --extra baselines``).
"""

from __future__ import annotations

import sys
import types

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.models.pretrained import PretrainedWatermarkModel, load_official_checkpoint, resize
from wmlab.registry import MODELS
from wmlab.weights import WeightsFile, register_weights

NATIVE_NBITS = 100
IMG_SIZE = 256
UUID_FIXED_BITS = {48: 0, 49: 1, 50: 0, 51: 0, 64: 1, 65: 0}

register_weights(WeightsFile(
    name="invismark_paper",
    url="https://huggingface.co/shelock/watermark-invismark-model/resolve/main/paper.ckpt",
    filename="paper.ckpt", size_mb=1234, license="non précisée (code MIT ; miroir non officiel)",
    source="https://github.com/microsoft/InvisMark",
    sha256="409f4259a88865ccc0cb75884810e6a6435a8235050333c1b04c8d539a54d9bb",
    description="InvisMark, checkpoint du papier (100 bits dont 6 figés par les UUID)"))


class InvisMarkParams(BaseModel):
    """Hyper-parameters of ``invismark``."""

    model_config = ConfigDict(extra="forbid")

    uuid_payload: bool = Field(
        False, description="Forcer les 6 bits figés des UUID (48-51, 64-65) et les retirer "
                           "du message : capacité réelle de 94 bits")
    download: bool = Field(False, description="Télécharger les poids s'ils sont absents de la "
                                              "machine")


class _ModelConfigStub:
    """Stand-in for the pickled ``configs.ModelConfig`` (plain attributes only)."""

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)


def _config_stub() -> type:
    module = sys.modules.get("configs")
    if module is None or not hasattr(module, "ModelConfig"):
        module = types.ModuleType("configs")
        _ModelConfigStub.__module__ = "configs"
        _ModelConfigStub.__qualname__ = _ModelConfigStub.__name__ = "ModelConfig"
        module.ModelConfig = _ModelConfigStub  # type: ignore[attr-defined]
        sys.modules["configs"] = module
    return module.ModelConfig  # type: ignore[no-any-return]


# -- encoder (microsoft/InvisMark model.py, config of the paper checkpoint) ------------------
class _Conv(nn.Module):
    def __init__(self, c_in: int, c_out: int, kernel: int, stride: int = 1, padding: int = 0,
                 activ: str | None = "relu") -> None:
        super().__init__()
        self.conv = nn.Conv2d(c_in, c_out, kernel, stride, padding)
        self.activ = {"relu": nn.ReLU, "silu": nn.SiLU, "tanh": nn.Tanh}[activ]() if activ else None

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv(x)
        return self.activ(x) if self.activ is not None else x


class _DecBlock(nn.Module):
    def __init__(self, c_in: int, skip: int) -> None:
        super().__init__()
        self.conv1 = _Conv(c_in, c_in // 2, 2, 1, 0)
        self.conv2 = _Conv(c_in // 2 + skip, c_in // 2, 3, 1, 1)

    def forward(self, x: Tensor, skip: Tensor) -> Tensor:
        x = self.conv1(F.pad(F.interpolate(x, scale_factor=2.0, mode="nearest"), (0, 1, 0, 1)))
        return self.conv2(torch.cat([x, skip], dim=1))


class _Watermark2Image(nn.Module):
    """Linear → 3×16×16 → nearest ×8 (128 px) → zero padding 64 → ReLU (``num_repeats = 1``)."""

    def __init__(self, nbits: int, resolution: int = IMG_SIZE, hidden: int = 16) -> None:
        super().__init__()
        self.transform = nn.Sequential(nn.Linear(nbits, hidden * hidden * 3))
        self.hidden = hidden
        self.factor = resolution // hidden // 2
        self.pad = resolution // 4

    def forward(self, msg: Tensor) -> Tensor:
        x = self.transform(msg).view(-1, 3, self.hidden, self.hidden)
        x = F.interpolate(x, scale_factor=float(self.factor), mode="nearest")
        return torch.relu(F.pad(x, (self.pad,) * 4))


class InvisMarkEncoder(nn.Module):
    """U-Net encoder: image in [-1,1] (256×256) + message → watermarked image (``tanh``)."""

    def __init__(self, nbits: int = NATIVE_NBITS, width: int = 32, levels: int = 4) -> None:
        super().__init__()
        self.watermark2image = _Watermark2Image(nbits)
        self.pre = _Conv(6, width, 3, 1, 1)
        self.enc = nn.ModuleList()
        ch = width
        for _ in range(levels):
            self.enc.append(_Conv(ch, ch * 2, 3, 2, 1))
            ch *= 2
        self.dec = nn.ModuleList()
        for i in range(levels):
            self.dec.append(_DecBlock(ch, ch // 2 if i < levels - 1 else ch // 2 + 6))
            ch //= 2
        self.post = nn.Sequential(_Conv(ch, ch, 3, 1, 1, activ=None),
                                  _Conv(ch, ch // 2, 1, 1, 0, activ="silu"),
                                  _Conv(ch // 2, 3, 1, 1, 0, activ="tanh"))

    def forward(self, image: Tensor, msg: Tensor) -> Tensor:
        inputs = torch.cat([image, self.watermark2image(msg)], dim=1)
        skips = []
        x = self.pre(inputs)
        for block in self.enc:
            skips.append(x)
            x = block(x)
        for i, (block, skip) in enumerate(zip(self.dec, reversed(skips))):
            x = block(x, skip if i < len(self.dec) - 1 else torch.cat([skip, inputs], dim=1))
        return self.post(x)


class _LayerNorm2d(nn.LayerNorm):
    def forward(self, x: Tensor) -> Tensor:
        x = F.layer_norm(x.permute(0, 2, 3, 1), self.normalized_shape, self.weight, self.bias,
                         self.eps)
        return x.permute(0, 3, 1, 2)


def _convnext_extractor(nbits: int) -> nn.Module:
    try:
        import torchvision
    except ImportError:
        raise ImportError("invismark exige torchvision : uv sync --extra baselines") from None
    model = torchvision.models.convnext_base(weights=None)
    model.classifier = nn.Sequential(_LayerNorm2d(1024, eps=1e-6), nn.Flatten(1),
                                     nn.Linear(1024, nbits))
    return model


@MODELS.register("invismark", modality="image", params=InvisMarkParams)
class InvisMark(PretrainedWatermarkModel):
    """InvisMark (Microsoft, 2025), poids du papier : 100 bits dont 6 figés par l'entraînement sur des UUID (94 utiles), baseline figée (évaluation seule, phases: [])."""

    native_nbits = NATIVE_NBITS

    def __init__(self, nbits: int, uuid_payload: bool = False, download: bool = False) -> None:
        super().__init__(nbits, fixed_bits=UUID_FIXED_BITS if uuid_payload else None)
        self.encoder = InvisMarkEncoder()
        self.extractor = _convnext_extractor(NATIVE_NBITS)
        path = self.weights_file("invismark_paper", download=download)
        ckpt = load_official_checkpoint(path, safe_globals=[_config_stub()], mmap=True)
        state = {f"encoder.{k}": v for k, v in ckpt["encoder_state_dict"].items()}
        state.update({k: v for k, v in ckpt["decoder_state_dict"].items()
                      if k.startswith("extractor.")})
        self.load_official(state)
        self.eval()

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] in [0,1], msg [B,nbits] → x_w (same shape, clamped to [0,1])."""
        self.check_message(x, msg)
        image = x * 2 - 1
        small = resize(image, IMG_SIZE)
        diff = resize(self.encoder(small, self.to_native(msg)) - small, tuple(x.shape[-2:]))
        return ((image + diff).clamp(-1, 1) + 1) / 2

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] → logits [B,nbits] (pre-sigmoid)."""
        return self.from_native(self.extractor(resize(x, IMG_SIZE) * 2 - 1))
