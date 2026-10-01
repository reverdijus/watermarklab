"""Procedural, deterministic content for the ``synthetic`` dataset.

Every generator takes a ``torch.Generator`` (seeded from ``(seed, index)``) so an item is
reproducible on any machine. Content is structured on purpose (gradients, textures, shapes,
harmonic tones, chirps, moving objects) so that watermarking models face realistic statistics
rather than white noise.
"""

from __future__ import annotations

import hashlib
import math

import torch
import torch.nn.functional as F
from torch import Tensor


def item_generator(seed: int, index: int, salt: str = "") -> torch.Generator:
    """Return a CPU generator seeded deterministically from ``(seed, index)``."""
    digest = hashlib.sha256(f"{salt}:{seed}:{index}".encode()).digest()
    return torch.Generator().manual_seed(int.from_bytes(digest[:8], "little") & (2**63 - 1))


def _u(g: torch.Generator, lo: float = 0.0, hi: float = 1.0, *shape: int) -> Tensor:
    return lo + (hi - lo) * torch.rand(shape or (1,), generator=g)


def _ri(g: torch.Generator, lo: int, hi: int) -> int:
    """Random integer in ``[lo, hi]``."""
    return int(torch.randint(lo, hi + 1, (1,), generator=g))


def _color(g: torch.Generator) -> Tensor:
    return torch.rand(3, 1, 1, generator=g)


# -- images ---------------------------------------------------------------------------------
def _grid(size: int) -> tuple[Tensor, Tensor]:
    coords = (torch.arange(size, dtype=torch.float32) + 0.5) / size
    return torch.meshgrid(coords, coords, indexing="ij")  # yy, xx in (0, 1)


def _background(g: torch.Generator, size: int, yy: Tensor, xx: Tensor) -> Tensor:
    """Linear or radial two-colour gradient plus low-frequency value noise and gratings."""
    c1, c2 = _color(g), _color(g)
    if float(_u(g)) < 0.7:
        theta = float(_u(g, 0, 2 * math.pi))
        t = xx * math.cos(theta) + yy * math.sin(theta)
    else:
        cx, cy = float(_u(g)), float(_u(g))
        t = ((xx - cx) ** 2 + (yy - cy) ** 2).sqrt()
    t = (t - t.min()) / (t.max() - t.min() + 1e-6)
    img = c1 * (1 - t) + c2 * t
    # low-frequency value noise (bicubic upsampling of a coarse random grid)
    cells = _ri(g, 2, 8)
    noise = torch.rand(1, 3, cells, cells, generator=g) - 0.5
    noise = F.interpolate(noise, size=(size, size), mode="bicubic", align_corners=False)[0]
    img = img + float(_u(g, 0.05, 0.3)) * noise
    # a few oriented sinusoidal gratings (textures of various frequencies)
    for _ in range(_ri(g, 0, 3)):
        freq = float(_u(g, 1.0, size / 4.0))
        angle = float(_u(g, 0, math.pi))
        phase = float(_u(g, 0, 2 * math.pi))
        wave = torch.sin(2 * math.pi * freq * (xx * math.cos(angle) + yy * math.sin(angle)) + phase)
        img = img + float(_u(g, 0.02, 0.12)) * (_color(g) - 0.5) * 2 * wave
    return img


def _shape_mask(g: torch.Generator, size: int, yy: Tensor, xx: Tensor) -> Tensor:
    """Soft mask of a random ellipse, rectangle, triangle or ring."""
    cx, cy = float(_u(g, 0.1, 0.9)), float(_u(g, 0.1, 0.9))
    rx, ry = float(_u(g, 0.05, 0.35)), float(_u(g, 0.05, 0.35))
    angle = float(_u(g, 0, math.pi))
    dx, dy = xx - cx, yy - cy
    u = dx * math.cos(angle) + dy * math.sin(angle)
    v = -dx * math.sin(angle) + dy * math.cos(angle)
    kind = _ri(g, 0, 3)
    if kind == 0:  # ellipse
        dist = 1.0 - ((u / rx) ** 2 + (v / ry) ** 2).sqrt()
        scale = min(rx, ry)
    elif kind == 1:  # rectangle
        dist = torch.minimum(1.0 - u.abs() / rx, 1.0 - v.abs() / ry)
        scale = min(rx, ry)
    elif kind == 2:  # triangle (intersection of three half-planes)
        dists = []
        for k in range(3):
            a = angle + k * 2 * math.pi / 3
            dists.append(rx * 0.5 - (dx * math.cos(a) + dy * math.sin(a)))
        dist = torch.minimum(torch.minimum(dists[0], dists[1]), dists[2]) / (rx * 0.5)
        scale = rx * 0.5
    else:  # ring
        r = ((u / rx) ** 2 + (v / rx) ** 2).sqrt()
        width = float(_u(g, 0.15, 0.4))
        dist = width - (r - 0.8).abs()
        scale = rx * width
    sharpness = float(_u(g, 0.5, 3.0)) * size  # soft to sharp edges
    return torch.sigmoid(dist * scale * sharpness)


def _fill(g: torch.Generator, size: int, yy: Tensor, xx: Tensor) -> Tensor:
    """Flat colour, gradient or striped fill for a shape."""
    base = _color(g)
    kind = _ri(g, 0, 2)
    if kind == 0:
        return base.expand(3, size, size)
    if kind == 1:
        other = _color(g)
        t = xx if float(_u(g)) < 0.5 else yy
        return base * (1 - t) + other * t
    freq = float(_u(g, 2.0, size / 3.0))
    angle = float(_u(g, 0, math.pi))
    stripes = 0.5 + 0.5 * torch.sign(torch.sin(2 * math.pi * freq * (xx * math.cos(angle)
                                                                      + yy * math.sin(angle))))
    return base * (1 - 0.6 * stripes) + _color(g) * 0.6 * stripes


def synthetic_image(g: torch.Generator, size: int, n_shapes: tuple[int, int] = (2, 8)) -> Tensor:
    """Return a structured image ``[3, size, size]`` in ``[0, 1]``."""
    yy, xx = _grid(size)
    img = _background(g, size, yy, xx)
    for _ in range(_ri(g, *n_shapes)):
        mask = _shape_mask(g, size, yy, xx)
        img = img * (1 - mask) + _fill(g, size, yy, xx) * mask
    if float(_u(g)) < 0.3:  # fine-grained sensor-like noise on some images
        img = img + float(_u(g, 0.005, 0.03)) * torch.randn(3, size, size, generator=g)
    return img.clamp(0.0, 1.0).float()


# -- audio ----------------------------------------------------------------------------------
def _envelope(g: torch.Generator, length: int) -> Tensor:
    """Random segment with attack/release ramps (values in [0, 1])."""
    start = _ri(g, 0, max(0, length - 1))
    dur = _ri(g, max(1, length // 8), length)
    t = torch.arange(length, dtype=torch.float32)
    attack = max(1.0, float(_u(g, 0.002, 0.2)) * dur)
    release = max(1.0, float(_u(g, 0.002, 0.3)) * dur)
    rise = ((t - start) / attack).clamp(0, 1)
    fall = ((start + dur - t) / release).clamp(0, 1)
    return rise * fall


def synthetic_audio(g: torch.Generator, length: int, sample_rate: int = 16000) -> Tensor:
    """Return a mixture of harmonic tones, chirps, AM tones and noise bursts ``[1, T]``."""
    t = torch.arange(length, dtype=torch.float32) / sample_rate
    nyquist = sample_rate / 2.0
    out = torch.zeros(length)
    for _ in range(_ri(g, 2, 5)):
        kind = _ri(g, 0, 3)
        amp = float(_u(g, 0.1, 1.0))
        env = _envelope(g, length)
        if kind == 0:  # harmonic tone
            f0 = float(_u(g, 80.0, min(1000.0, nyquist / 4)))
            sig = torch.zeros(length)
            for h in range(1, _ri(g, 1, 6) + 1):
                if f0 * h < 0.9 * nyquist:
                    sig += torch.sin(2 * math.pi * f0 * h * t + float(_u(g, 0, 6.28))) / h
        elif kind == 1:  # linear chirp
            f_a = float(_u(g, 50.0, 0.4 * nyquist))
            f_b = float(_u(g, 50.0, 0.4 * nyquist))
            duration = max(length / sample_rate, 1e-3)
            phase = 2 * math.pi * (f_a * t + (f_b - f_a) * t ** 2 / (2 * duration))
            sig = torch.sin(phase)
        elif kind == 2:  # amplitude-modulated tone
            fc = float(_u(g, 200.0, 0.4 * nyquist))
            fm = float(_u(g, 1.0, 12.0))
            sig = torch.sin(2 * math.pi * fc * t) * (0.6 + 0.4 * torch.sin(2 * math.pi * fm * t))
        else:  # smoothed noise burst (low-passed by a moving average)
            width = _ri(g, 1, 16)
            noise = torch.randn(1, 1, length + width - 1, generator=g)
            sig = F.avg_pool1d(noise, width, stride=1)[0, 0] * math.sqrt(width)
            sig = sig / (sig.abs().max() + 1e-6)
        out += amp * env * sig
    out += float(_u(g, 0.001, 0.02)) * torch.randn(length, generator=g)  # background noise
    peak = float(_u(g, 0.3, 0.9))
    out = out / (out.abs().max() + 1e-6) * peak
    return out.clamp(-1.0, 1.0).unsqueeze(0).float()


# -- video ----------------------------------------------------------------------------------
def synthetic_video(g: torch.Generator, frames: int, size: int) -> Tensor:
    """Return ``[F, 3, size, size]``: textured background with 1-3 moving shapes."""
    yy, xx = _grid(size)
    background = synthetic_image(g, size, n_shapes=(0, 3))
    video = background.unsqueeze(0).repeat(frames, 1, 1, 1)
    for _ in range(_ri(g, 1, 3)):
        color = _color(g)
        rx, ry = float(_u(g, 0.06, 0.25)), float(_u(g, 0.06, 0.25))
        x0, y0 = float(_u(g, 0.2, 0.8)), float(_u(g, 0.2, 0.8))
        vx, vy = float(_u(g, -0.06, 0.06)), float(_u(g, -0.06, 0.06))
        square = float(_u(g)) < 0.5
        for f in range(frames):
            cx = _bounce(x0 + vx * f)
            cy = _bounce(y0 + vy * f)
            if square:
                dist = torch.minimum(1 - (xx - cx).abs() / rx, 1 - (yy - cy).abs() / ry)
            else:
                dist = 1 - (((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2).sqrt()
            mask = torch.sigmoid(dist * min(rx, ry) * size * 2.0)
            video[f] = video[f] * (1 - mask) + color * mask
    drift = float(_u(g, -0.05, 0.05))  # slow global illumination change
    ramp = torch.linspace(0, drift, frames).view(frames, 1, 1, 1)
    return (video + ramp).clamp(0.0, 1.0).float()


def _bounce(p: float) -> float:
    """Reflect a coordinate into [0, 1] (objects bounce on the borders)."""
    p = p % 2.0
    return 2.0 - p if p > 1.0 else p
