"""Base class and helpers shared by every attack (ARCHITECTURE.md §4.2).

- ``Attack.forward(x, strength)`` keeps the shape of ``x``; ``strength ∈ [0, 1]`` interpolates the
  severity between the identity (0 ⇒ ``x`` returned unchanged) and the params (1).
- ``apply_attack`` wraps non differentiable attacks in a straight-through estimator.
- ``sample_strength`` draws the per-batch strength used by the trainer.
- ``per_sample_uniform`` draws per-sample random parameters; inside
  :func:`shared_randomness` the draws are shared by consecutive groups of samples (all the
  frames of a video get the same crop, rotation…).
"""

from __future__ import annotations

import contextlib
import contextvars
import random
from typing import Any, ClassVar, Iterator

import torch
from torch import Tensor, nn

_GROUP_SIZE: contextvars.ContextVar[int] = contextvars.ContextVar("wmlab_attack_group", default=1)


class Attack(nn.Module):
    """Signal transformation used during training (robustness) and in benchmarks."""

    modality: ClassVar[str] = "image"
    differentiable: ClassVar[bool] = True

    def __init__(self, **params: Any) -> None:
        super().__init__()
        self.params = dict(params)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:  # pragma: no cover
        raise NotImplementedError(f"{type(self).__name__}.forward n'est pas implémenté")

    def extra_repr(self) -> str:
        return ", ".join(f"{k}={v!r}" for k, v in self.params.items())


def clamp_strength(strength: float) -> float:
    """Clamp a strength to ``[0, 1]`` (NaN ⇒ 0)."""
    s = float(strength)
    if s != s or s <= 0.0:
        return 0.0
    return 1.0 if s >= 1.0 else s


def lerp(identity: float, target: float, strength: float) -> float:
    """Interpolate a severity parameter: ``identity`` at strength 0, ``target`` at strength 1."""
    return identity + clamp_strength(strength) * (target - identity)


def apply_attack(attack: nn.Module | None, x: Tensor, strength: float = 1.0) -> Tensor:
    """Apply ``attack``; non differentiable attacks get a straight-through gradient.

    ``x + (attack(x.detach(), s) - x).detach()`` when ``attack.differentiable`` is False.
    """
    if attack is None or clamp_strength(strength) == 0.0:
        return x
    if getattr(attack, "differentiable", True):
        return attack(x, strength)
    attacked = attack(x.detach(), strength)
    return x + (attacked.to(device=x.device, dtype=x.dtype) - x).detach()


def configure_attack(attack: nn.Module | None, *, sample_rate: int | None = None,
                     fps: int | None = None, params: dict[str, Any] | None = None) -> nn.Module | None:
    """Propagate the data ``sample_rate`` / ``fps`` to an attack built from YAML params.

    Attacks expose ``sample_rate`` (audio) or ``fps`` (video) with a default value; the engine
    calls this after ``ATTACKS.build`` so that projects at another rate stay consistent. A value
    given explicitly in ``params`` wins. Nested attacks (``combo``, ``framewise``) are handled
    through ``nn.Module.modules()``.
    """
    if attack is None or not isinstance(attack, nn.Module):
        return attack
    params = params or {}
    for name, value in (("sample_rate", sample_rate), ("fps", fps)):
        if value is None or name in params:
            continue
        for module in attack.modules():
            current = getattr(module, name, None)
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                setattr(module, name, int(value))
                if isinstance(getattr(module, "params", None), dict) and name in module.params:
                    module.params[name] = int(value)
    return attack


def sample_strength(phase_strength: float, random_strength: bool = True,
                    generator: torch.Generator | random.Random | None = None) -> float:
    """Return the effective strength of a training step.

    ``U(0, phase_strength)`` when ``random_strength`` (default), else ``phase_strength``;
    the result is clamped to ``[0, 1]``.
    """
    s = clamp_strength(phase_strength)
    if not random_strength or s == 0.0:
        return s
    if isinstance(generator, random.Random):
        u = generator.random()
    elif isinstance(generator, torch.Generator):
        u = float(torch.rand(1, generator=generator))
    else:
        u = float(torch.rand(1))
    return u * s


@contextlib.contextmanager
def shared_randomness(group_size: int) -> Iterator[None]:
    """Share per-sample random draws inside consecutive groups of ``group_size`` samples."""
    token = _GROUP_SIZE.set(max(1, int(group_size)))
    try:
        yield
    finally:
        _GROUP_SIZE.reset(token)


def per_sample_uniform(n: int, device: torch.device | str, low: float = 0.0,
                       high: float = 1.0) -> Tensor:
    """Draw ``n`` uniform values in ``[low, high)`` (shared inside :func:`shared_randomness`)."""
    group = _GROUP_SIZE.get()
    if group > 1 and n % group == 0:
        values = torch.rand(n // group).repeat_interleave(group)
    else:
        values = torch.rand(n)
    return (low + (high - low) * values).to(device)


def per_sample_sign(n: int, device: torch.device | str) -> Tensor:
    """Draw ``n`` random signs ``±1`` (shared inside :func:`shared_randomness`)."""
    return torch.where(per_sample_uniform(n, device) < 0.5, -1.0, 1.0)


def autocast_disabled(x: Tensor) -> contextlib.AbstractContextManager:
    """Context disabling mixed precision on ``x``'s device (precision-sensitive maths)."""
    try:
        return torch.autocast(device_type=x.device.type, enabled=False)
    except (RuntimeError, ValueError):  # device type without autocast support
        return contextlib.nullcontext()


def broadcast_per_sample(values: Tensor, like: Tensor) -> Tensor:
    """Reshape ``values [B]`` to broadcast against ``like [B, ...]``."""
    return values.view(-1, *([1] * (like.ndim - 1))).to(dtype=like.dtype)
