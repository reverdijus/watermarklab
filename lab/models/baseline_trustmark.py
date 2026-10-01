"""TrustMark (Adobe, 2023): official MIT weights as a lab model (100 raw bits, no BCH).

Encoder ``Unet1`` re-implemented from adobe/trustmark ``unet.py`` (MIT, see
``THIRD_PARTY_NOTICES.md``); decoder = torchvision ResNet-50 (ResNet-18 for variant C) with a
100-way linear head (raw logits, threshold 0). The pipeline follows ``TrustMark.encode`` /
``subimage_decode``: centre square crop when the aspect ratio exceeds 2 (always for variant
P), resize to 256, ``residual = clamp(stego) − cover`` with its per-channel mean removed,
bilinear upscaling, ``clip(cover + strength · residual)`` in [-1, 1] (``strength`` ×1.25 for
P), 1 % border feathering; decoding at 245 px (224 for P) then 224 inside the ResNet.

The lab message is written on the 100 raw bits (official BCH modes reserve bits 96-99 for the
schema and use part of the rest as ECC: not applied here, like Video Seal's TrustMark baseline).
Requires torchvision (``uv sync --extra baselines``).
"""

from __future__ import annotations

from typing import Literal

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.models.pretrained import PretrainedWatermarkModel, load_official_checkpoint, resize
from wmlab.registry import MODELS
from wmlab.weights import WeightsFile, register_weights

SOURCE = "https://github.com/adobe/trustmark"
HOST = "https://cai-watermark.adobe.net/watermarking/trustmark-models/"
NATIVE_NBITS = 100
ENC_SIZE = 256
DEC_ARCH = {"Q": "resnet50", "B": "resnet50", "C": "resnet18", "P": "resnet50"}
SHA256 = {  # verified downloads (the other variants are checked by their size only)
    "encoder_Q.ckpt": "dc382c3f6b4fd568b27d6fbb763d6ffc2d2587126d84afe9d5ee95b4c5d99826",
    "decoder_Q.ckpt": "e3d9cea5406a26590735719f8f15cb10802b11852ae69047eaf4cf17214df781",
}
SIZES_MB = {"encoder": 17.3, "decoder": 47.7, "decoder_C": 22.5}

for _variant in DEC_ARCH:
    for _part in ("encoder", "decoder"):
        _file = f"{_part}_{_variant}.ckpt"
        register_weights(WeightsFile(
            name=f"trustmark_{_variant.lower()}_{_part}", url=HOST + _file, filename=_file,
            size_mb=SIZES_MB.get(f"{_part}_{_variant}", SIZES_MB[_part]), license="MIT",
            source=SOURCE, sha256=SHA256.get(_file),
            description=f"TrustMark {_variant} — {'encodeur' if _part == 'encoder' else 'décodeur'}"))


class TrustMarkParams(BaseModel):
    """Hyper-parameters of ``trustmark`` (defaults = official settings)."""

    model_config = ConfigDict(extra="forbid")

    variant: Literal["Q", "B", "C", "P"] = Field(
        "Q", description="Variante officielle : Q (qualité), B (équilibrée), C (compacte, "
                         "ResNet-18), P (perceptuelle)")
    strength: float = Field(1.0, gt=0.0, le=5.0,
                            description="WM_STRENGTH officiel (×1,25 pour la variante P)")
    download: bool = Field(False, description="Télécharger les poids officiels s'ils sont "
                                              "absents de la machine")


# -- encoder (adobe/trustmark unet.py) -------------------------------------------------------
_ACTIVATIONS = {"relu": nn.ReLU, "silu": nn.SiLU, "tanh": nn.Tanh}


class _Conv(nn.Module):
    """Conv2d + optional activation (``trustmark.unet.Conv2d``, no normalisation)."""

    def __init__(self, c_in: int, c_out: int, kernel: int, stride: int = 1, padding: int = 0,
                 activ: str = "relu") -> None:
        super().__init__()
        self.conv = nn.Conv2d(c_in, c_out, kernel, stride, padding)
        self.activ = _ACTIVATIONS[activ]() if activ in _ACTIVATIONS else None

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv(x)
        return self.activ(x) if self.activ is not None else x


class _PadConv(nn.Module):
    """Zero padding + conv + optional activation (``trustmark.unet.Conv2dBlock``)."""

    def __init__(self, channels: int, activ: str) -> None:
        super().__init__()
        self.pad = nn.ZeroPad2d(1)
        self.conv = nn.Conv2d(channels, channels, 3, 1)
        self.activation = _ACTIVATIONS[activ]() if activ in _ACTIVATIONS else None

    def forward(self, x: Tensor) -> Tensor:
        x = self.conv(self.pad(x))
        return self.activation(x) if self.activation is not None else x


class _ResBlock(nn.Module):
    def __init__(self, channels: int, activ: str) -> None:
        super().__init__()
        self.model = nn.Sequential(_PadConv(channels, activ), _PadConv(channels, "none"))

    def forward(self, x: Tensor) -> Tensor:
        return x + self.model(x)


class _ResBlocks(nn.Module):
    def __init__(self, num_blocks: int, channels: int, activ: str) -> None:
        super().__init__()
        self.model = nn.Sequential(*[_ResBlock(channels, activ) for _ in range(num_blocks)])

    def forward(self, x: Tensor) -> Tensor:
        return self.model(x)


class _DecBlock(nn.Module):
    def __init__(self, c_in: int, skip: int, activ: str) -> None:
        super().__init__()
        c_out = c_in // 2
        self.conv1 = _Conv(c_in, c_out, 2, 1, 0, activ=activ)
        self.conv2 = _Conv(c_out + skip, c_out, 3, 1, 1, activ=activ)

    def forward(self, x: Tensor, skip: Tensor) -> Tensor:
        x = self.conv1(F.pad(F.interpolate(x, scale_factor=2.0, mode="nearest"), (0, 1, 0, 1)))
        return self.conv2(torch.cat([x, skip], dim=1))


class _Secret2Image(nn.Module):
    def __init__(self, secret_len: int, resolution: int = ENC_SIZE) -> None:
        super().__init__()
        self.dense = nn.Linear(secret_len, 16 * 16 * 3)
        self.factor = resolution // 16

    def forward(self, secret: Tensor) -> Tensor:
        x = self.dense(secret).view(-1, 3, 16, 16)
        return torch.relu(F.interpolate(x, scale_factor=float(self.factor), mode="nearest"))


class Unet1(nn.Module):
    """TrustMark encoder: cover in [-1,1] (256×256) + secret → stego image (``tanh``)."""

    def __init__(self, secret_len: int = NATIVE_NBITS, width: int = 32, ndown: int = 4,
                 nmiddle: int = 1, activ: str = "silu") -> None:
        super().__init__()
        self.secret2image = _Secret2Image(secret_len)
        self.pre = _Conv(6, width, 3, 1, 1, activ=activ)
        self.enc = nn.ModuleList()
        ch = width
        for _ in range(ndown):
            self.enc.append(_Conv(ch, ch * 2, 3, 2, 1, activ="relu"))
            ch *= 2
        self.middle = nn.ModuleList(_ResBlocks(nmiddle, ch, activ) for _ in range(nmiddle))
        self.dec = nn.ModuleList()
        for i in range(ndown):
            self.dec.append(_DecBlock(ch, ch // 2 if i < ndown - 1 else ch // 2 + 6, activ))
            ch //= 2
        self.post = nn.Sequential(_Conv(ch, ch, 3, 1, 1, activ=activ),
                                  _Conv(ch, ch // 2, 1, 1, 0, activ="silu"),
                                  _Conv(ch // 2, 3, 1, 1, 0, activ="tanh"))

    def forward(self, image: Tensor, secret: Tensor) -> Tensor:
        inputs = torch.cat([image, self.secret2image(secret)], dim=1)
        skips = []
        x = self.pre(inputs)
        for block in self.enc:
            skips.append(x)
            x = block(x)
        for block in self.middle:
            x = block(x)
        for i, (block, skip) in enumerate(zip(self.dec, reversed(skips))):
            x = block(x, skip if i < len(self.dec) - 1 else torch.cat([skip, inputs], dim=1))
        return self.post(x)


class SecretDecoder(nn.Module):
    """TrustMark decoder: torchvision ResNet with a ``secret_len``-way head (raw logits)."""

    def __init__(self, arch: str, secret_len: int = NATIVE_NBITS, resolution: int = 224) -> None:
        super().__init__()
        try:
            import torchvision
        except ImportError:
            raise ImportError("trustmark exige torchvision : uv sync --extra baselines") from None
        self.resolution = resolution
        self.decoder = getattr(torchvision.models, arch)(weights=None)
        self.decoder.fc = nn.Linear(self.decoder.fc.in_features, secret_len)

    def forward(self, image: Tensor) -> Tensor:
        if min(image.shape[-2:]) > self.resolution:
            image = F.interpolate(image, size=(self.resolution, self.resolution),
                                  mode="bilinear", align_corners=False)
        return self.decoder(image)


def _square_region(h: int, w: int, aspect_limit: float) -> tuple[int, int, int, int]:
    """``(top, left, height, width)`` processed by TrustMark (centre square if too elongated)."""
    if max(h, w) / min(h, w) > aspect_limit:
        size = min(h, w)
        return (h - size) // 2, (w - size) // 2, size, size
    return 0, 0, h, w


def _feather_alpha(h: int, w: int, device: torch.device, dtype: torch.dtype) -> Tensor:
    """Blending weight of the watermarked region (``TrustMark.feather_paste``, 1 % border)."""
    size = min(max(1, int(min(h, w) * 0.01)), 50)
    ramp = torch.arange(1, size + 1, device=device, dtype=dtype) / size
    size = min(size, h, w)
    alpha = torch.ones(h, w, device=device, dtype=dtype)
    alpha[:size, :] = ramp[:size, None]
    alpha[h - size:, :] = ramp[:size].flip(0)[:, None]
    alpha[:, :size] = ramp[None, :size]      # columns last: they win at the corners
    alpha[:, w - size:] = ramp[:size].flip(0)[None, :]
    return alpha


@MODELS.register("trustmark", modality="image", params=TrustMarkParams)
class TrustMark(PretrainedWatermarkModel):
    """TrustMark (Adobe, 2023), poids officiels MIT : 100 bits bruts, encodeur U-Net et décodeur ResNet, baseline figée (évaluation seule, phases: [])."""

    native_nbits = NATIVE_NBITS

    def __init__(self, nbits: int, variant: str = "Q", strength: float = 1.0,
                 download: bool = False) -> None:
        super().__init__(nbits)
        if variant not in DEC_ARCH:
            raise ValueError(f"variante TrustMark inconnue '{variant}' — valeurs possibles : "
                             f"{', '.join(DEC_ARCH)}")
        self.variant = variant
        self.strength = float(strength) * (1.25 if variant == "P" else 1.0)
        self.aspect_limit = 0.0 if variant == "P" else 2.0
        self.dec_size = 224 if variant == "P" else 245
        self.encoder = Unet1()
        self.decoder = SecretDecoder(DEC_ARCH[variant])
        state: dict[str, Tensor] = {}
        for part in ("encoder", "decoder"):
            path = self.weights_file(f"trustmark_{variant.lower()}_{part}", download=download)
            loaded = load_official_checkpoint(path)
            loaded = loaded.get("state_dict", loaded)
            state.update({k: v for k, v in loaded.items() if k.startswith(f"{part}.")})
        self.load_official(state)  # official files are float16: copied into float32 params
        self.eval()

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] in [0,1], msg [B,nbits] → x_w (same shape, clamped to [0,1])."""
        self.check_message(x, msg)
        top, left, h, w = _square_region(*x.shape[-2:], self.aspect_limit)
        cover = x[..., top:top + h, left:left + w] * 2 - 1
        small = resize(cover, ENC_SIZE)
        residual = self.encoder(small, self.to_native(msg)).clamp(-1, 1) - small
        residual = residual - residual.mean(dim=(2, 3), keepdim=True)   # no colour shift
        residual = F.interpolate(residual, size=(h, w), mode="bilinear", align_corners=False)
        stego = (cover + self.strength * residual).clamp(-1, 1)
        alpha = _feather_alpha(h, w, x.device, x.dtype)
        region = (alpha * stego + (1 - alpha) * cover + 1) / 2
        out = x.clone()
        out[..., top:top + h, left:left + w] = region
        return out.clamp(0.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] → logits [B,nbits]."""
        top, left, h, w = _square_region(*x.shape[-2:], self.aspect_limit)
        region = resize(x[..., top:top + h, left:left + w], self.dec_size)
        return self.from_native(self.decoder(region * 2 - 1))
