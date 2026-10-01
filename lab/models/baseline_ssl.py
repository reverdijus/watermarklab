"""SSL watermarking (Fernandez et al., ICASSP 2022): watermarking in the latent space of a
self-supervised network, with the official weights (CC-BY-NC 4.0: non-commercial use).

Re-implementation of the method of facebookresearch/ssl_watermarking (``watermark_multibit``,
no code copied). Features ``f(x) = W · ResNet50_DINO(norm(x)) + b`` (DINO ResNet-50 trained
with rotations ``dino_r50_plus.pth`` + PCA-whitening layer ``out2048_yfcc_orig.pth``). Each
bit ``k`` owns a carrier ``c_k`` (orthonormal K×2048, drawn from ``carrier_seed``: it is the
secret key; the official script draws it with scipy, which gives other carriers).

Embedding = optimisation of the image (no embedder network): Adam (lr 0.01) for ``steps``
iterations on the ImageNet-normalised pixels; at each iteration the perturbation is
attenuated by an SSIM heatmap, scaled down to ``target_psnr``, one random augmentation among
none / rotation / crop / resize / blur (+ horizontal flip) is applied to the whole batch, and
the loss ``lambda_w · Σ max(0, 5 − (f·c_k)(2m_k − 1)) / K + lambda_i · ‖x − x₀‖²`` is
minimised. The result is attenuated, PSNR-clipped and rounded to 8 bits. Decoding: logits
``f(x) · c_kᵀ`` (sign ⇒ bit). The number of bits is the project's (``message_bits`` ≤ 2048).
Embedding costs ``steps`` forward/backward passes of a ResNet-50: slow, run it on GPU/MPS.
Requires torchvision (``uv sync --extra baselines``).
"""

from __future__ import annotations

import argparse
import math

import numpy as np
import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.models.pretrained import PretrainedWatermarkModel, load_official_checkpoint
from wmlab.registry import MODELS
from wmlab.weights import WeightsFile, register_weights

SOURCE = "https://github.com/facebookresearch/ssl_watermarking"
LICENSE = "CC-BY-NC 4.0"
FEATURE_DIM = 2048
MARGIN = 5.0
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)

register_weights(WeightsFile(
    name="ssl_dino_r50_plus", url="https://dl.fbaipublicfiles.com/ssl_watermarking/dino_r50_plus.pth",
    filename="dino_r50_plus.pth", size_mb=789, license=LICENSE, source=SOURCE,
    sha256="ab26d85d00cb1be8e757cf8820cf0fd8aa729ea7e21b1cf6c44875952ba8eb0f",
    commercial_use=False, description="SSL watermarking : ResNet-50 DINO (+ rotations)"))
register_weights(WeightsFile(
    name="ssl_normlayer_yfcc",
    url="https://dl.fbaipublicfiles.com/ssl_watermarking/out2048_yfcc_orig.pth",
    filename="out2048_yfcc_orig.pth", size_mb=17, license=LICENSE, source=SOURCE,
    sha256="4b256188454d8f7cf440de048df398e2a3209136a52cd7cdac834f5792f526a3",
    commercial_use=False, description="SSL watermarking : couche de blanchiment PCA (YFCC)"))


class SSLParams(BaseModel):
    """Hyper-parameters of ``ssl_watermark`` (defaults = official multi-bit settings)."""

    model_config = ConfigDict(extra="forbid")

    target_psnr: float = Field(42.0, gt=20.0, le=70.0, description="PSNR visé en dB (officiel : 42)")
    steps: int = Field(100, ge=1, le=2000, description="Itérations d'optimisation par image "
                                                       "(officiel : 100)")
    lr: float = Field(0.01, gt=0.0, le=1.0, description="Pas d'Adam sur les pixels")
    lambda_w: float = Field(5.0e4, gt=0.0, description="Poids de la perte de message")
    lambda_i: float = Field(1.0, ge=0.0, description="Poids de la perte d'image")
    augment: bool = Field(True, description="Augmentations aléatoires pendant l'optimisation "
                                            "(officiel : all)")
    carrier_seed: int = Field(0, ge=0, description="Graine des porteuses (clé secrète)")
    download: bool = Field(False, description="Télécharger les poids officiels s'ils sont "
                                              "absents de la machine")


def _resnet50() -> nn.Module:
    try:
        import torchvision
    except ImportError:
        raise ImportError("ssl_watermark exige torchvision : uv sync --extra baselines") from None
    model = torchvision.models.resnet50(weights=None)
    model.fc = nn.Identity()
    return model


def _carriers(nbits: int, seed: int) -> Tensor:
    """``[nbits, 2048]`` orthonormal carriers (QR of a seeded Gaussian matrix)."""
    generator = torch.Generator().manual_seed(int(seed))
    q, r = torch.linalg.qr(torch.randn(FEATURE_DIM, nbits, generator=generator,
                                       dtype=torch.float64))
    q = q * torch.sign(torch.diagonal(r))[None, :]   # unique orthonormal basis
    return q.T.float().contiguous()


def _gaussian_window(size: int = 17, sigma: float = 1.5) -> Tensor:
    x = torch.arange(size, dtype=torch.float32) - size // 2
    g = torch.exp(-x ** 2 / (2 * sigma ** 2))
    g = g / g.sum()
    return (g[:, None] @ g[None, :]).expand(3, 1, size, size).contiguous()


@MODELS.register("ssl_watermark", modality="image", params=SSLParams)
class SSLWatermark(PretrainedWatermarkModel):
    """SSL watermarking (Meta, 2022), poids officiels CC-BY-NC (usage non commercial) : optimisation de l'image dans l'espace latent d'un ResNet-50 DINO, baseline figée (évaluation seule, phases: [])."""

    def __init__(self, nbits: int, target_psnr: float = 42.0, steps: int = 100,
                 lr: float = 0.01, lambda_w: float = 5.0e4, lambda_i: float = 1.0,
                 augment: bool = True, carrier_seed: int = 0, download: bool = False) -> None:
        if not 0 < int(nbits) <= FEATURE_DIM:
            raise ValueError(f"ssl_watermark : message_bits doit être compris entre 1 et "
                             f"{FEATURE_DIM} (reçu {nbits})")
        super().__init__(nbits, native_nbits=int(nbits))
        self.target_psnr = float(target_psnr)
        self.steps = int(steps)
        self.lr = float(lr)
        self.lambda_w = float(lambda_w)
        self.lambda_i = float(lambda_i)
        self.augment = bool(augment)
        self.backbone = _resnet50()
        self.normlayer = nn.Linear(FEATURE_DIM, FEATURE_DIM)
        self.register_buffer("carrier", _carriers(self.nbits, carrier_seed), persistent=False)
        self.register_buffer("mean", torch.tensor(IMAGENET_MEAN).view(1, 3, 1, 1), persistent=False)
        self.register_buffer("std", torch.tensor(IMAGENET_STD).view(1, 3, 1, 1), persistent=False)
        self.register_buffer("window", _gaussian_window(), persistent=False)
        dino = load_official_checkpoint(
            self.weights_file("ssl_dino_r50_plus", download=download),
            safe_globals=[argparse.Namespace, np.dtype, np.dtypes.Float64DType,
                          np.dtypes.Int64DType, (np._core.multiarray.scalar,
                                                 "numpy.core.multiarray.scalar")])
        prefix = "module.backbone."
        state = {f"backbone.{k[len(prefix):]}": v for k, v in dino["teacher"].items()
                 if k.startswith(prefix)}
        norm = load_official_checkpoint(self.weights_file("ssl_normlayer_yfcc", download=download))
        state.update({"normlayer.weight": norm["weight"], "normlayer.bias": norm["bias"]})
        self.load_official(state)
        self.eval()

    # -- features and decoding -----------------------------------------------------------
    def _normalize(self, x: Tensor) -> Tensor:
        return (x - self.mean.to(x)) / self.std.to(x)

    def features(self, x_norm: Tensor) -> Tensor:
        """Whitened SSL features ``[B, 2048]`` of ImageNet-normalised images."""
        return self.normlayer(self.backbone(x_norm))

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] → logits ``f(x) · carriersᵀ`` [B,nbits]."""
        return self.features(self._normalize(x)) @ self.carrier.to(x).T

    # -- embedding by optimisation -------------------------------------------------------
    def _ssim_attenuate(self, x: Tensor, x0: Tensor) -> Tensor:
        """Keep the perturbation where the SSIM heatmap is high (textured areas)."""
        w = self.window.to(x)
        pad = w.shape[-1] // 2

        def blur(t: Tensor) -> Tensor:
            return F.conv2d(t, w, padding=pad, groups=3)

        mu1, mu2 = blur(x), blur(x0)
        s11 = blur(x * x) - mu1 ** 2
        s22 = blur(x0 * x0) - mu2 ** 2
        s12 = blur(x * x0) - mu1 * mu2
        c1, c2 = 0.01 ** 2, 0.03 ** 2
        ssim = ((2 * mu1 * mu2 + c1) * (2 * s12 + c2)) / ((mu1 ** 2 + mu2 ** 2 + c1) * (s11 + s22 + c2))
        return x0 + (x - x0) * ssim.sum(dim=1, keepdim=True).clamp_min(0)

    def _psnr_clip(self, x: Tensor, x0: Tensor) -> Tensor:
        """Scale each perturbation down so that its PSNR (8-bit pixels) is ≥ ``target_psnr``."""
        delta = 255 * (x - x0) * self.std.to(x)
        mse = delta.pow(2).mean(dim=(1, 2, 3), keepdim=True).clamp_min(1e-12)
        psnr = 20 * math.log10(255) - 10 * torch.log10(mse)
        scale = torch.where(psnr < self.target_psnr,
                            torch.sqrt(10 ** ((psnr - self.target_psnr) / 10)),
                            torch.ones_like(psnr))
        return x0 + (x - x0) * scale

    def _constrain(self, x: Tensor, x0: Tensor) -> Tensor:
        return self._psnr_clip(self._ssim_attenuate(x, x0), x0)

    @staticmethod
    def _augment(x: Tensor) -> Tensor:
        """One random differentiable augmentation for the whole batch (official ``All``)."""
        import torchvision.transforms.functional as TF

        h, w = x.shape[-2:]
        kind = int(torch.randint(0, 5, ()).item())
        if kind == 1:    # rotation, von Mises angle (≈ ±30°)
            angle = float(torch.distributions.VonMises(0.0, 1.0).sample()) * 30 / math.pi
            x = TF.rotate(x, angle, interpolation=TF.InterpolationMode.BILINEAR)
        elif kind in (2, 3):
            area = float(torch.empty(()).uniform_(0.2, 1.0)) * h * w
            ratio = math.exp(float(torch.empty(()).uniform_(math.log(3 / 4), math.log(4 / 3))))
            tw = int(round(math.sqrt(area * ratio)))
            th = int(round(math.sqrt(area / ratio)))
            if kind == 2:    # crop (no resize: the ResNet pools globally)
                i = int(torch.randint(0, max(min(h - th + 1, h - 1), 0) + 1, ()))
                j = int(torch.randint(0, max(min(w - tw + 1, w - 1), 0) + 1, ()))
                x = TF.crop(x, i, j, th, tw)
            else:            # resize of the smaller edge
                s = float(torch.empty(()).uniform_(0.2, 1.0))
                x = TF.resize(x, max(8, int(math.sqrt(s) * min(th, tw))), antialias=True)
        elif kind == 4:  # gaussian blur, odd kernel ≤ 17
            k = int(torch.randint(1, 18, ()))
            x = TF.gaussian_blur(x, [k - (1 - k % 2)] * 2)
        if bool(torch.rand(()) > 0.5):
            x = TF.hflip(x)
        return x

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] in [0,1], msg [B,nbits] → x_w (optimised, 8-bit rounded, in [0,1])."""
        self.check_message(x, msg)
        x0 = self._normalize(x.detach().float())
        signs = msg.detach().float() * 2 - 1
        carrier = self.carrier.to(x0)
        param = x0.clone().requires_grad_(True)
        optimizer = torch.optim.Adam([param], lr=self.lr)
        with torch.enable_grad():
            for _ in range(self.steps):
                constrained = self._constrain(param, x0)
                batch = self._augment(constrained) if self.augment else constrained
                dots = self.features(batch) @ carrier.T
                loss_w = torch.clamp(MARGIN - dots * signs, min=0).sum() / self.nbits
                loss_i = (param - x0).pow(2).sum()
                loss = self.lambda_w * loss_w + self.lambda_i * loss_i
                (grad,) = torch.autograd.grad(loss, param)
                param.grad = grad
                optimizer.step()
        with torch.no_grad():
            out = self._constrain(param, x0) * self.std.to(x0) + self.mean.to(x0)
            out = torch.round(255 * out.clamp(0.0, 1.0)) / 255
        return out.to(x.dtype)
