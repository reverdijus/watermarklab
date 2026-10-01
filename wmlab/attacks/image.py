"""Image attacks (``[B,3,H,W]`` in [0,1]; any leading dims are accepted and flattened).

Severity is interpolated by ``strength`` between the identity (0 ⇒ ``x`` returned unchanged)
and the params (1). Random parameters (crop position, rotation sign…) are drawn per sample
with :func:`per_sample_uniform`, so they are shared across the frames of a video inside
``shared_randomness`` (see the ``framewise`` video attack).
"""

from __future__ import annotations

import math

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.attacks.base import (
    Attack,
    autocast_disabled,
    broadcast_per_sample,
    clamp_strength,
    lerp,
    per_sample_sign,
    per_sample_uniform,
)
from wmlab.attacks.codecs import jpeg_roundtrip
from wmlab.registry import ATTACKS

LUMA = (0.299, 0.587, 0.114)


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _as_images(x: Tensor) -> Tensor:
    """View ``[..., 3, H, W]`` as ``[N, 3, H, W]``."""
    if x.ndim < 3:
        raise ValueError(f"attaque image : tenseur [..., 3, H, W] attendu, reçu {tuple(x.shape)}")
    return x.reshape(-1, *x.shape[-3:])


def _grayscale(x: Tensor) -> Tensor:
    r, g, b = x[:, 0:1], x[:, 1:2], x[:, 2:3]
    return LUMA[0] * r + LUMA[1] * g + LUMA[2] * b


# -- JPEG (real) ----------------------------------------------------------------------------
class JpegParams(_Params):
    quality: int = Field(50, ge=1, le=100, description="Qualité JPEG à strength=1")


@ATTACKS.register("jpeg", modality="image", params=JpegParams)
class Jpeg(Attack):
    """Compression JPEG réelle (PIL), qualité interpolée de 100 à `quality`."""

    modality = "image"
    differentiable = False

    def __init__(self, quality: int = 50) -> None:
        super().__init__(quality=quality)
        self.quality = int(quality)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        quality = round(lerp(100.0, self.quality, s))
        return jpeg_roundtrip(_as_images(x), quality).reshape(x.shape)


# -- JPEG (differentiable approximation) ----------------------------------------------------
_LUMA_TABLE = [
    [16, 11, 10, 16, 24, 40, 51, 61], [12, 12, 14, 19, 26, 58, 60, 55],
    [14, 13, 16, 24, 40, 57, 69, 56], [14, 17, 22, 29, 51, 87, 80, 62],
    [18, 22, 37, 56, 68, 109, 103, 77], [24, 35, 55, 64, 81, 104, 113, 92],
    [49, 64, 78, 87, 103, 121, 120, 101], [72, 92, 95, 98, 112, 100, 103, 99],
]
_CHROMA_TABLE = [
    [17, 18, 24, 47, 99, 99, 99, 99], [18, 21, 26, 66, 99, 99, 99, 99],
    [24, 26, 56, 99, 99, 99, 99, 99], [47, 66, 99, 99, 99, 99, 99, 99],
] + [[99] * 8 for _ in range(4)]


def quantization_table(base: list[list[int]], quality: float) -> Tensor:
    """IJG scaling of a standard quantization table (entries clamped to [1, 255])."""
    quality = min(100.0, max(1.0, float(quality)))
    scale = 5000.0 / quality if quality < 50 else 200.0 - 2.0 * quality
    table = torch.floor((torch.tensor(base, dtype=torch.float32) * scale + 50.0) / 100.0)
    return table.clamp(1.0, 255.0)


def dct_matrix(n: int = 8) -> Tensor:
    """Orthonormal DCT-II matrix ``D`` (coefficients = D @ block @ D.T)."""
    k = torch.arange(n, dtype=torch.float64)[:, None]
    i = torch.arange(n, dtype=torch.float64)[None, :]
    d = torch.cos((2 * i + 1) * k * math.pi / (2 * n)) * math.sqrt(2.0 / n)
    d[0] /= math.sqrt(2.0)
    return d.float()


def diff_round(x: Tensor) -> Tensor:
    """Exact rounding in the forward pass, identity gradient (straight-through)."""
    return x + (torch.round(x) - x).detach()


def _jpeg_plane(plane: Tensor, table: Tensor, dct: Tensor) -> Tensor:
    """Block DCT → quantize/round → dequantize → inverse DCT of ``[N,1,H,W]`` (0-255 scale)."""
    n, _, h, w = plane.shape
    ph, pw = (-h) % 8, (-w) % 8
    padded = F.pad(plane, (0, pw, 0, ph), mode="replicate") if ph or pw else plane
    hh, ww = h + ph, w + pw
    blocks = (padded - 128.0).reshape(n, hh // 8, 8, ww // 8, 8).permute(0, 1, 3, 2, 4)
    coeffs = dct @ blocks @ dct.T
    coeffs = diff_round(coeffs / table) * table
    blocks = dct.T @ coeffs @ dct
    out = blocks.permute(0, 1, 3, 2, 4).reshape(n, 1, hh, ww) + 128.0
    return out[:, :, :h, :w]


def jpeg_differentiable(x: Tensor, quality: float) -> Tensor:
    """Differentiable JPEG: YCbCr, 4:2:0 chroma subsampling, 8×8 DCT, standard tables."""
    device, dtype = x.device, x.dtype
    dct = dct_matrix().to(device=device, dtype=dtype)
    luma_q = quantization_table(_LUMA_TABLE, quality).to(device=device, dtype=dtype)
    chroma_q = quantization_table(_CHROMA_TABLE, quality).to(device=device, dtype=dtype)
    rgb = x * 255.0
    r, g, b = rgb[:, 0:1], rgb[:, 1:2], rgb[:, 2:3]
    y = 0.299 * r + 0.587 * g + 0.114 * b
    cb = -0.168736 * r - 0.331264 * g + 0.5 * b + 128.0
    cr = 0.5 * r - 0.418688 * g - 0.081312 * b + 128.0
    h, w = x.shape[-2:]
    y = _jpeg_plane(y, luma_q, dct)
    chroma = torch.cat([cb, cr], dim=0)
    if h > 1 and w > 1:
        ph, pw = h % 2, w % 2
        if ph or pw:
            chroma = F.pad(chroma, (0, pw, 0, ph), mode="replicate")
        small = F.avg_pool2d(chroma, 2)
        small = _jpeg_plane(small, chroma_q, dct)
        chroma = F.interpolate(small, scale_factor=2, mode="bilinear",
                               align_corners=False)[:, :, :h, :w]
    else:
        chroma = _jpeg_plane(chroma, chroma_q, dct)
    cb, cr = chroma.chunk(2, dim=0)
    cb, cr = cb - 128.0, cr - 128.0
    r = y + 1.402 * cr
    g = y - 0.344136 * cb - 0.714136 * cr
    b = y + 1.772 * cb
    return (torch.cat([r, g, b], dim=1) / 255.0).clamp(0.0, 1.0)


@ATTACKS.register("jpeg_diff", modality="image", params=JpegParams)
class JpegDiff(Attack):
    """Approximation différentiable du JPEG (DCT 8×8, tables standard, arrondi straight-through)."""

    modality = "image"

    def __init__(self, quality: int = 50) -> None:
        super().__init__(quality=quality)
        self.quality = int(quality)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        quality = lerp(100.0, self.quality, s)
        with autocast_disabled(x):  # 0-255 DCT maths would lose precision in bf16/fp16
            out = jpeg_differentiable(_as_images(x).float(), quality)
        return out.to(x.dtype).reshape(x.shape)


# -- filtering ------------------------------------------------------------------------------
def gaussian_kernel1d(sigma: float, device: torch.device, dtype: torch.dtype) -> Tensor:
    """Normalized 1D Gaussian kernel of size ``2*ceil(3σ)+1``."""
    radius = max(1, int(math.ceil(3.0 * sigma)))
    t = torch.arange(-radius, radius + 1, device=device, dtype=dtype)
    kernel = torch.exp(-0.5 * (t / sigma) ** 2)
    return kernel / kernel.sum()


def gaussian_blur(x: Tensor, sigma: float) -> Tensor:
    """Separable Gaussian blur of ``[N,C,H,W]`` (replicate borders)."""
    kernel = gaussian_kernel1d(sigma, x.device, x.dtype)
    radius = kernel.numel() // 2
    c = x.shape[1]
    kx = kernel.view(1, 1, 1, -1).repeat(c, 1, 1, 1)
    ky = kernel.view(1, 1, -1, 1).repeat(c, 1, 1, 1)
    out = F.conv2d(F.pad(x, (radius, radius, 0, 0), mode="replicate"), kx, groups=c)
    return F.conv2d(F.pad(out, (0, 0, radius, radius), mode="replicate"), ky, groups=c)


class GaussianBlurParams(_Params):
    sigma: float = Field(2.0, gt=0.0, le=50.0, description="Écart-type du flou (pixels)")


@ATTACKS.register("gaussian_blur", modality="image", params=GaussianBlurParams)
class GaussianBlur(Attack):
    """Flou gaussien, sigma interpolé de 0 à `sigma`."""

    modality = "image"

    def __init__(self, sigma: float = 2.0) -> None:
        super().__init__(sigma=sigma)
        self.sigma = float(sigma)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        sigma = s * self.sigma
        if s == 0.0 or sigma < 1e-3:
            return x
        return gaussian_blur(_as_images(x), sigma).reshape(x.shape)


# -- geometry -------------------------------------------------------------------------------
class ResizeParams(_Params):
    scale: float = Field(0.5, gt=0.0, le=4.0, description="Facteur de redimensionnement")


@ATTACKS.register("resize", modality="image", params=ResizeParams)
class Resize(Attack):
    """Redimensionnement (anti-aliasé) par `scale` puis retour à la taille d'origine."""

    modality = "image"

    def __init__(self, scale: float = 0.5) -> None:
        super().__init__(scale=scale)
        self.scale = float(scale)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        images = _as_images(x)
        h, w = images.shape[-2:]
        factor = lerp(1.0, self.scale, s)
        size = (max(1, round(h * factor)), max(1, round(w * factor)))
        if size == (h, w):
            return x
        small = F.interpolate(images, size=size, mode="bilinear", align_corners=False,
                              antialias=size[0] < h or size[1] < w)
        out = F.interpolate(small, size=(h, w), mode="bilinear", align_corners=False)
        return out.clamp(0.0, 1.0).reshape(x.shape)


class CropParams(_Params):
    area: float = Field(0.5, gt=0.0, le=1.0, description="Fraction de surface conservée")


@ATTACKS.register("crop", modality="image", params=CropParams)
class Crop(Attack):
    """Recadrage (position aléatoire) conservant `area` de la surface, puis retour à la taille."""

    modality = "image"

    def __init__(self, area: float = 0.5) -> None:
        super().__init__(area=area)
        self.area = float(area)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        images = _as_images(x)
        n = images.shape[0]
        ratio = math.sqrt(lerp(1.0, self.area, s))
        if ratio >= 1.0:
            return x
        slack = 1.0 - ratio
        theta = torch.zeros(n, 2, 3, device=x.device, dtype=x.dtype)
        theta[:, 0, 0] = ratio
        theta[:, 1, 1] = ratio
        theta[:, 0, 2] = per_sample_uniform(n, x.device, -slack, slack).to(x.dtype)
        theta[:, 1, 2] = per_sample_uniform(n, x.device, -slack, slack).to(x.dtype)
        grid = F.affine_grid(theta, list(images.shape), align_corners=False)
        out = F.grid_sample(images, grid, mode="bilinear", padding_mode="border",
                            align_corners=False)
        return out.clamp(0.0, 1.0).reshape(x.shape)


class RotateParams(_Params):
    degrees: float = Field(10.0, ge=0.0, le=180.0, description="Angle de rotation (±)")


@ATTACKS.register("rotate", modality="image", params=RotateParams)
class Rotate(Attack):
    """Rotation de ±`degrees` (sens aléatoire), coins remplis en noir."""

    modality = "image"

    def __init__(self, degrees: float = 10.0) -> None:
        super().__init__(degrees=degrees)
        self.degrees = float(degrees)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0 or self.degrees == 0.0:
            return x
        images = _as_images(x)
        n, _, h, w = images.shape
        angle = per_sample_sign(n, x.device).to(x.dtype) * math.radians(s * self.degrees)
        cos, sin = torch.cos(angle), torch.sin(angle)
        theta = torch.zeros(n, 2, 3, device=x.device, dtype=x.dtype)
        theta[:, 0, 0] = cos
        theta[:, 0, 1] = -sin * h / w
        theta[:, 1, 0] = sin * w / h
        theta[:, 1, 1] = cos
        grid = F.affine_grid(theta, list(images.shape), align_corners=False)
        out = F.grid_sample(images, grid, mode="bilinear", padding_mode="zeros",
                            align_corners=False)
        return out.clamp(0.0, 1.0).reshape(x.shape)


# -- colour ---------------------------------------------------------------------------------
class BrightnessParams(_Params):
    delta: float = Field(0.3, ge=0.0, le=1.0, description="Décalage de luminosité (±)")


@ATTACKS.register("brightness", modality="image", params=BrightnessParams)
class Brightness(Attack):
    """Décalage de luminosité de ±`delta` (sens aléatoire par image)."""

    modality = "image"

    def __init__(self, delta: float = 0.3) -> None:
        super().__init__(delta=delta)
        self.delta = float(delta)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        images = _as_images(x)
        shift = per_sample_sign(images.shape[0], x.device) * (s * self.delta)
        return (images + broadcast_per_sample(shift, images)).clamp(0.0, 1.0).reshape(x.shape)


class FactorParams(_Params):
    factor: float = Field(0.5, ge=0.0, le=5.0, description="Facteur (1 = inchangé)")


@ATTACKS.register("contrast", modality="image", params=FactorParams)
class Contrast(Attack):
    """Contraste multiplié par `factor` autour de la luminance moyenne."""

    modality = "image"

    def __init__(self, factor: float = 0.5) -> None:
        super().__init__(factor=factor)
        self.factor = float(factor)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        images = _as_images(x)
        f = lerp(1.0, self.factor, s)
        mean = _grayscale(images).mean(dim=(1, 2, 3), keepdim=True)
        return ((images - mean) * f + mean).clamp(0.0, 1.0).reshape(x.shape)


@ATTACKS.register("saturation", modality="image", params=FactorParams)
class Saturation(Attack):
    """Saturation multipliée par `factor` (0 = niveaux de gris)."""

    modality = "image"

    def __init__(self, factor: float = 0.5) -> None:
        super().__init__(factor=factor)
        self.factor = float(factor)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        images = _as_images(x)
        f = lerp(1.0, self.factor, s)
        gray = _grayscale(images)
        return ((images - gray) * f + gray).clamp(0.0, 1.0).reshape(x.shape)
