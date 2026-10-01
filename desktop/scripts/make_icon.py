"""Generate desktop/build/icon.png (1024x1024) for Watermark Lab.

macOS-style rounded square (≈10 % transparent margins), clay background with a
soft vertical gradient, and a white glyph: a seal (circle) crossed by a
stylised sine wave — the watermark hidden in the signal.

Run with the workspace venv:  ../.venv/bin/python scripts/make_icon.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZE = 1024
SS = 4  # supersampling factor for smooth edges
MARGIN = 100  # Apple icon grid: 824 px body on a 1024 canvas
RADIUS = 185  # corner radius of the body
TOP = (0xD4, 0x72, 0x50)  # lighter clay
BOTTOM = (0xB5, 0x55, 0x36)  # deeper clay
WHITE = (255, 255, 255, 255)


def _gradient(size: int) -> Image.Image:
    """Vertical clay gradient."""
    grad = Image.new("RGBA", (1, size))
    for y in range(size):
        t = y / (size - 1)
        grad.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    return grad.resize((size, size))


def _body_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    m = MARGIN * SS
    ImageDraw.Draw(mask).rounded_rectangle((m, m, size - m, size - m), radius=RADIUS * SS, fill=255)
    return mask


def _wave_points(cx: float, cy: float, half_width: float, amplitude: float, periods: float, n: int = 600) -> list[tuple[float, float]]:
    """Sine wave whose amplitude fades at both ends (Hann window)."""
    pts = []
    for i in range(n + 1):
        u = i / n
        x = cx - half_width + 2 * half_width * u
        window = math.sin(math.pi * u) ** 1.5
        y = cy - amplitude * window * math.sin(2 * math.pi * periods * u)
        pts.append((x, y))
    return pts


def _stroke(draw: ImageDraw.ImageDraw, pts: list[tuple[float, float]], width: float, fill=WHITE) -> None:
    """Smooth round-capped stroke drawn by stamping discs along the path."""
    r = width / 2
    step = max(1.0, r / 6)
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        for k in range(n):
            t = k / n
            x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
            draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)
    x, y = pts[-1]
    draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def make_icon(out: Path) -> None:
    size = SIZE * SS
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    body = _gradient(size)
    mask = _body_mask(size)

    # Soft top highlight inside the body.
    highlight = Image.new("L", (size, size), 0)
    ImageDraw.Draw(highlight).ellipse((-size * 0.2, -size * 0.75, size * 1.2, size * 0.45), fill=34)
    highlight = highlight.filter(ImageFilter.GaussianBlur(60 * SS))
    body = Image.composite(Image.new("RGBA", (size, size), (255, 255, 255, 255)), body, highlight)

    canvas.paste(body, (0, 0), mask)

    # Glyph layer.
    glyph = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glyph)
    cx = cy = size / 2
    ring_r = 250 * SS
    ring_w = 30 * SS
    draw.ellipse((cx - ring_r, cy - ring_r, cx + ring_r, cy + ring_r), outline=WHITE, width=ring_w)
    # Wave crossing the seal, extending beyond it.
    wave = _wave_points(cx, cy, half_width=335 * SS, amplitude=118 * SS, periods=2.0)
    # Inner dotted ring: the "seal" (dots too close to the wave are skipped).
    dots = 36
    dot_r = 7 * SS
    inner = ring_r - 58 * SS
    clearance = 39 * SS + dot_r
    for k in range(dots):
        a = 2 * math.pi * k / dots
        x, y = cx + inner * math.cos(a), cy + inner * math.sin(a)
        if min(math.hypot(x - wx, y - wy) for wx, wy in wave) < clearance:
            continue
        draw.ellipse((x - dot_r, y - dot_r, x + dot_r, y + dot_r), fill=(255, 255, 255, 150))
    # Knock the ring out slightly around the wave for legibility.
    gap = Image.new("L", (size, size), 0)
    gd = ImageDraw.Draw(gap)
    _stroke(gd, wave, 78 * SS, fill=255)
    glyph.putalpha(ImageChops.subtract(glyph.getchannel("A"), gap))
    draw = ImageDraw.Draw(glyph)
    _stroke(draw, wave, 36 * SS)

    # Subtle shadow under the glyph.
    shadow = Image.new("RGBA", (size, size), (70, 25, 10, 0))
    shadow.putalpha(glyph.getchannel("A").point(lambda a: int(a * 0.28)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(10 * SS))
    shadow = ImageChops.offset(shadow, 0, 8 * SS)
    shadow_masked = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    shadow_masked.paste(shadow, (0, 0), mask)
    canvas = Image.alpha_composite(canvas, shadow_masked)
    canvas = Image.alpha_composite(canvas, glyph)

    icon = canvas.resize((SIZE, SIZE), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    icon.save(out, "PNG", optimize=True)
    print(f"Icône écrite : {out} ({SIZE}x{SIZE})")


if __name__ == "__main__":
    make_icon(Path(__file__).resolve().parent.parent / "build" / "icon.png")
