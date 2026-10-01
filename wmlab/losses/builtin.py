"""Builtin loss terms (ARCHITECTURE.md §4.4).

Imperceptibility terms compare ``x_w`` to ``x``; ``message_bce`` compares the logits extracted
from the attacked signal to the message.
"""

from __future__ import annotations

from typing import Literal

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.losses.base import Loss, StepBatch
from wmlab.metrics.quality import load_lpips, ssim_per_image
from wmlab.registry import LOSSES


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


@LOSSES.register("message_bce", modality=None)
class MessageBCE(Loss):
    """Entropie croisée binaire entre les logits extraits (après attaque) et le message."""

    def forward(self, batch: StepBatch) -> Tensor:
        return F.binary_cross_entropy_with_logits(batch.logits, batch.msg.to(batch.logits.dtype))


@LOSSES.register("mse", modality=None)
class MSELoss(Loss):
    """Erreur quadratique moyenne entre le signal tatoué et l'original."""

    def forward(self, batch: StepBatch) -> Tensor:
        return F.mse_loss(batch.x_w, batch.x)


@LOSSES.register("l1", modality=None)
class L1Loss(Loss):
    """Erreur absolue moyenne entre le signal tatoué et l'original."""

    def forward(self, batch: StepBatch) -> Tensor:
        return F.l1_loss(batch.x_w, batch.x)


@LOSSES.register("ssim", modality=["image", "video"])
class SSIMLoss(Loss):
    """1 − SSIM entre le signal tatoué et l'original (vidéo : moyenne sur les frames)."""

    def forward(self, batch: StepBatch) -> Tensor:
        x, x_w = batch.x, batch.x_w
        if x.ndim == 5:
            x, x_w = x.flatten(0, 1), x_w.flatten(0, 1)
        return 1.0 - ssim_per_image(x, x_w).mean()


class STFTParams(_Params):
    fft_sizes: list[int] = Field(default_factory=lambda: [512, 1024, 2048], min_length=1,
                                 description="Tailles de FFT (une résolution par taille)")
    hop_ratio: float = Field(0.25, gt=0.0, le=1.0, description="Pas = taille × hop_ratio")


@LOSSES.register("stft", modality="audio", params=STFTParams)
class MultiResolutionSTFTLoss(Loss):
    """STFT multi-résolution (convergence spectrale + L1 des log-magnitudes)."""

    def __init__(self, fft_sizes: list[int] | None = None, hop_ratio: float = 0.25) -> None:
        super().__init__()
        self.fft_sizes = [int(n) for n in (fft_sizes or [512, 1024, 2048])]
        self.hop_ratio = float(hop_ratio)

    @staticmethod
    def _magnitude(signal: Tensor, n_fft: int, hop: int) -> Tensor:
        window = torch.hann_window(n_fft, device=signal.device, dtype=signal.dtype)
        spec = torch.stft(signal, n_fft, hop_length=hop, window=window, return_complex=True)
        return (spec.real.pow(2) + spec.imag.pow(2) + 1e-10).sqrt()

    def forward(self, batch: StepBatch) -> Tensor:
        x = batch.x.reshape(-1, batch.x.shape[-1]).float()
        x_w = batch.x_w.reshape(-1, batch.x_w.shape[-1]).float()
        length = x.shape[-1]
        total = x_w.new_zeros(())
        for n_fft in self.fft_sizes:
            n_fft = min(n_fft, 2 ** max(1, (length - 1).bit_length() - 1))  # reflect pad < T
            hop = max(1, int(n_fft * self.hop_ratio))
            ref = self._magnitude(x, n_fft, hop)
            est = self._magnitude(x_w, n_fft, hop)
            convergence = torch.linalg.vector_norm(est - ref, dim=(-2, -1)) / \
                torch.linalg.vector_norm(ref, dim=(-2, -1)).clamp_min(1e-5)
            log_mag = (est.log() - ref.log()).abs().mean(dim=(-2, -1))
            total = total + convergence.mean() + log_mag.mean()
        return total / len(self.fft_sizes)


class TemporalParams(_Params):
    p: Literal[1, 2] = Field(1, description="Norme (1 = L1, 2 = L2)")


@LOSSES.register("temporal", modality="video", params=TemporalParams)
class TemporalLoss(Loss):
    """Cohérence temporelle du résidu (x_w − x) : pénalise ses variations d'une frame à l'autre."""

    def __init__(self, p: int = 1) -> None:
        super().__init__()
        self.p = int(p)

    def forward(self, batch: StepBatch) -> Tensor:
        residual = batch.x_w - batch.x
        if residual.ndim != 5:
            raise ValueError("la perte 'temporal' attend une vidéo [B, F, 3, H, W]")
        if residual.shape[1] < 2:
            return residual.sum() * 0.0
        diff = residual[:, 1:] - residual[:, :-1]
        return diff.abs().mean() if self.p == 1 else diff.pow(2).mean()


class LPIPSParams(_Params):
    net: Literal["alex", "vgg", "squeeze"] = Field("alex", description="Réseau de LPIPS")


@LOSSES.register("lpips", modality=["image", "video"], params=LPIPSParams)
class LPIPSLoss(Loss):
    """Distance perceptuelle LPIPS (optionnelle : paquets lpips + torchvision, extra « quality »)."""

    def __init__(self, net: str = "alex") -> None:
        super().__init__()
        self.net_name = net
        load_lpips(net, "cpu")  # fails early with a clear ImportError when unavailable

    def forward(self, batch: StepBatch) -> Tensor:
        x, x_w = batch.x, batch.x_w
        if x.ndim == 5:
            x, x_w = x.flatten(0, 1), x_w.flatten(0, 1)
        net = load_lpips(self.net_name, x_w.device)  # frozen, cached per device (not a submodule)
        return net(x_w * 2.0 - 1.0, x * 2.0 - 1.0).mean()
