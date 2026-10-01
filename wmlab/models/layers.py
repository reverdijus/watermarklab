"""Small device-portable building blocks shared by the builtin models.

``torch.nn.functional.adaptive_avg_pool*`` is not implemented on MPS when the input size is not
a multiple of the output size; the versions below are exact (same bins as PyTorch), work on
every device and are differentiable.

:func:`configure_cpu_backends` disables NNPACK, whose CPU convolutions are 10-25× slower than
the default kernels for 1D signals (and slower for images) on recent PyTorch builds.
"""

from __future__ import annotations

import torch
from torch import Tensor


def configure_cpu_backends() -> None:
    """Disable NNPACK (pathologically slow CPU convolutions, notably for ``Conv1d``)."""
    try:
        if torch.backends.nnpack.is_available():
            torch.backends.nnpack.set_flags(False)
    except Exception:  # noqa: BLE001 — purely a performance tweak
        pass


def _adaptive_avg_dim(x: Tensor, dim: int, out: int) -> Tensor:
    """Adaptive average pooling of ``x`` along ``dim`` to ``out`` bins."""
    n = x.shape[dim]
    if out == n:
        return x
    if n % out == 0:
        shape = list(x.shape)
        shape[dim:dim + 1] = [out, n // out]
        return x.reshape(shape).mean(dim=dim + 1)
    starts = torch.tensor([(i * n) // out for i in range(out)], device=x.device)
    ends = torch.tensor([-(-((i + 1) * n) // out) for i in range(out)], device=x.device)
    zero_shape = list(x.shape)
    zero_shape[dim] = 1
    csum = torch.cat([x.new_zeros(zero_shape), x.cumsum(dim=dim)], dim=dim)
    sums = csum.index_select(dim, ends) - csum.index_select(dim, starts)
    counts_shape = [1] * x.ndim
    counts_shape[dim] = out
    counts = (ends - starts).to(x.dtype).reshape(counts_shape)
    return sums / counts


def adaptive_avg_pool1d(x: Tensor, out: int) -> Tensor:
    """``[B, C, T]`` → ``[B, C, out]`` (exact adaptive average pooling)."""
    return _adaptive_avg_dim(x, x.ndim - 1, out)


def adaptive_avg_pool2d(x: Tensor, out: int | tuple[int, int]) -> Tensor:
    """``[..., H, W]`` → ``[..., oh, ow]`` (exact adaptive average pooling)."""
    oh, ow = (out, out) if isinstance(out, int) else out
    return _adaptive_avg_dim(_adaptive_avg_dim(x, x.ndim - 2, oh), x.ndim - 1, ow)
