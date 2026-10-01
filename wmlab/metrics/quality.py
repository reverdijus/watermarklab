"""Quality (imperceptibility) metrics: ``(x, x_w, **ctx) -> Tensor[B]`` (ARCHITECTURE.md §4.5).

Higher is better for ``psnr``, ``ssim``, ``snr``, ``si_snr``, ``stoi``; lower is better for
``lpips`` and ``flicker``. Video metrics average over frames. Optional metrics import their
dependency lazily and raise a clear error when it is missing.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Any

import torch
import torch.nn.functional as F
from torch import Tensor

MAX_DB = 100.0  # cap for identical signals (keeps JSON finite)
_SSIM_CACHE: dict[tuple[int, str, torch.dtype], Tensor] = {}
_LPIPS_NETS: dict[tuple[str, str], Any] = {}
_LPIPS_LOCK = threading.Lock()


def _frames(x: Tensor) -> tuple[Tensor, int]:
    """``[B,F,C,H,W]`` → ``([B*F,C,H,W], F)``; images are returned as is with F=1."""
    if x.ndim == 5:
        return x.reshape(-1, *x.shape[2:]), x.shape[1]
    return x, 1


def psnr(x: Tensor, x_w: Tensor, data_range: float = 1.0, **ctx: Any) -> Tensor:
    """Peak signal-to-noise ratio (dB) per item; video: mean of per-frame PSNR."""
    a, f = _frames(x.float())
    b, _ = _frames(x_w.float())
    mse = (a - b).pow(2).flatten(1).mean(dim=1)
    value = 10.0 * torch.log10(data_range ** 2 / mse.clamp_min(1e-12))
    return value.clamp(max=MAX_DB).view(-1, f).mean(dim=1)


def _gaussian_window(size: int, device: torch.device, dtype: torch.dtype,
                     sigma: float = 1.5) -> Tensor:
    key = (size, str(device), dtype)
    window = _SSIM_CACHE.get(key)
    if window is None:
        t = torch.arange(size, dtype=torch.float64) - (size - 1) / 2
        g = torch.exp(-0.5 * (t / sigma) ** 2)
        g = g / g.sum()
        window = (g[:, None] * g[None, :]).to(device=device, dtype=dtype)
        _SSIM_CACHE[key] = window
    return window


def _autocast_disabled(x: Tensor) -> contextlib.AbstractContextManager:
    try:
        return torch.autocast(device_type=x.device.type, enabled=False)
    except (RuntimeError, ValueError):
        return contextlib.nullcontext()


def ssim_per_image(x: Tensor, y: Tensor, data_range: float = 1.0, window: int = 11) -> Tensor:
    """Mean SSIM of ``[N,C,H,W]`` pairs (Gaussian 11×11, σ=1.5), differentiable, ``[N]``.

    Computed in float32 even under autocast (variances suffer from cancellation in bf16).
    """
    with _autocast_disabled(x):
        return _ssim_fp32(x.float(), y.float(), data_range, window)


def _ssim_fp32(x: Tensor, y: Tensor, data_range: float, window: int) -> Tensor:
    n, c, h, w = x.shape
    size = min(window, h, w)
    size -= (size + 1) % 2  # odd window no larger than the image
    kernel = _gaussian_window(max(size, 1), x.device, x.dtype).expand(c, 1, -1, -1)
    c1, c2 = (0.01 * data_range) ** 2, (0.03 * data_range) ** 2

    def blur(t: Tensor) -> Tensor:
        return F.conv2d(t, kernel, groups=c)

    mu_x, mu_y = blur(x), blur(y)
    sigma_x = blur(x * x) - mu_x ** 2
    sigma_y = blur(y * y) - mu_y ** 2
    sigma_xy = blur(x * y) - mu_x * mu_y
    num = (2 * mu_x * mu_y + c1) * (2 * sigma_xy + c2)
    den = (mu_x ** 2 + mu_y ** 2 + c1) * (sigma_x + sigma_y + c2)
    return (num / den).flatten(1).mean(dim=1)


def ssim(x: Tensor, x_w: Tensor, data_range: float = 1.0, **ctx: Any) -> Tensor:
    """Structural similarity per item (1 = identical); video: mean over frames."""
    a, f = _frames(x)
    b, _ = _frames(x_w)
    return ssim_per_image(a, b, data_range).view(-1, f).mean(dim=1)


def snr(x: Tensor, x_w: Tensor, **ctx: Any) -> Tensor:
    """Signal-to-noise ratio (dB) of ``x_w`` w.r.t. the original ``x``, per item."""
    a, b = x.float().flatten(1), x_w.float().flatten(1)
    signal = a.pow(2).sum(dim=1).clamp_min(1e-12)
    noise = (a - b).pow(2).sum(dim=1).clamp_min(1e-12)
    return (10.0 * torch.log10(signal / noise)).clamp(-MAX_DB, MAX_DB)


def si_snr(x: Tensor, x_w: Tensor, **ctx: Any) -> Tensor:
    """Scale-invariant SNR (dB) of ``x_w`` w.r.t. ``x`` (zero-mean signals), per item."""
    ref = x.float().flatten(1)
    est = x_w.float().flatten(1)
    ref = ref - ref.mean(dim=1, keepdim=True)
    est = est - est.mean(dim=1, keepdim=True)
    scale = (est * ref).sum(dim=1, keepdim=True) / ref.pow(2).sum(dim=1, keepdim=True).clamp_min(
        1e-12)
    target = scale * ref
    noise = est - target
    ratio = target.pow(2).sum(dim=1).clamp_min(1e-12) / noise.pow(2).sum(dim=1).clamp_min(1e-12)
    return (10.0 * torch.log10(ratio)).clamp(-MAX_DB, MAX_DB)


def stoi(x: Tensor, x_w: Tensor, sample_rate: int = 16000, **ctx: Any) -> Tensor:
    """Short-time objective intelligibility (0-1, optional dependency ``pystoi``)."""
    try:
        from pystoi import stoi as _stoi
    except ImportError as exc:
        raise ImportError(
            "la métrique 'stoi' nécessite le paquet optionnel 'pystoi' (extra « quality » : "
            "uv sync --extra quality)"
        ) from exc
    ref = x.detach().float().cpu().reshape(x.shape[0], -1).numpy()
    est = x_w.detach().float().cpu().reshape(x.shape[0], -1).numpy()
    values = [float(_stoi(r, e, int(sample_rate), extended=False)) for r, e in zip(ref, est)]
    return torch.tensor(values, dtype=torch.float32)


def load_lpips(net: str = "alex", device: str | torch.device = "cpu") -> Any:
    """Return a cached frozen LPIPS network (optional dependency ``lpips``)."""
    try:
        import lpips as lpips_lib
    except ImportError as exc:
        raise ImportError(
            "LPIPS nécessite les paquets optionnels 'lpips' et 'torchvision' (extra « quality » : "
            "uv sync --extra quality)"
        ) from exc
    key = (net, str(device))
    with _LPIPS_LOCK:
        model = _LPIPS_NETS.get(key)
        if model is None:
            model = lpips_lib.LPIPS(net=net, verbose=False).to(device).eval()
            for p in model.parameters():
                p.requires_grad_(False)
            _LPIPS_NETS[key] = model
    return model


def lpips(x: Tensor, x_w: Tensor, net: str = "alex", **ctx: Any) -> Tensor:
    """Learned perceptual distance (lower = closer), per item; video: mean over frames."""
    a, f = _frames(x.float())
    b, _ = _frames(x_w.float())
    model = load_lpips(net, a.device)
    with torch.no_grad():
        dist = model(a * 2.0 - 1.0, b * 2.0 - 1.0).flatten(1).mean(dim=1)
    return dist.view(-1, f).mean(dim=1)


def flicker(x: Tensor, x_w: Tensor, **ctx: Any) -> Tensor:
    """Temporal flicker added by the watermark: mean |Δt(x_w − x)| per video (lower = better)."""
    if x.ndim != 5:
        raise ValueError(f"flicker : vidéo [B, F, 3, H, W] attendue, reçu {tuple(x.shape)}")
    residual = x_w.float() - x.float()
    if residual.shape[1] < 2:
        return torch.zeros(x.shape[0], device=x.device)
    return (residual[:, 1:] - residual[:, :-1]).abs().flatten(1).mean(dim=1)
