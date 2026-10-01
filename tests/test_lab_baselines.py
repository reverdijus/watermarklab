"""Pretrained baselines of ``lab/models/baseline_*.py``: helpers checked against the official
formulas, and — when the official weights are present in ``.wmlab/weights`` — strict loading
and decoding on synthetic images (skipped otherwise: ``wmlab weights pull …``)."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Iterator

import numpy as np
import pytest
import torch

from wmlab.registry import DATASETS, MODELS, load_plugins

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = {  # model → weights files (see ``wmlab weights list``)
    "videoseal": ["videoseal_1.0"],
    "pixelseal": ["pixelseal"],
    "wam": ["wam_mit"],
    "trustmark": ["trustmark_q_encoder", "trustmark_q_decoder"],
    "invismark": ["invismark_paper"],
    "ssl_watermark": ["ssl_dino_r50_plus", "ssl_normlayer_yfcc"],
}


def _purge_lab() -> None:
    for name in [m for m in sys.modules if m == "lab" or m.startswith("lab.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def lab_plugins(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("WMLAB_WORKSPACE", str(ROOT))
    monkeypatch.delenv("WMLAB_DATA", raising=False)
    _purge_lab()
    report = load_plugins(ROOT)
    assert [e for e in report.errors if e["module"].startswith("lab")] == []
    yield
    _purge_lab()


def _has_weights(model: str) -> bool:
    from wmlab.weights import weights_path

    return all(weights_path(name).is_file() for name in REQUIRED[model])


def test_feather_alpha_matches_official_loop() -> None:
    from lab.models.baseline_trustmark import _feather_alpha

    h, w = 230, 170
    size = min(max(1, int(min(h, w) * 0.01)), 50)
    wm, cover = np.ones((h, w)), np.zeros((h, w))
    out = wm.copy()  # official TrustMark.feather_paste: rows, then columns (in place)
    alphas = [(i + 1) / size for i in range(size)]
    for i in range(size):
        out[i, :] = alphas[i] * wm[i, :] + (1 - alphas[i]) * cover[i, :]
        out[h - 1 - i, :] = alphas[i] * wm[h - 1 - i, :] + (1 - alphas[i]) * cover[h - 1 - i, :]
    for i in range(size):
        out[:, i] = alphas[i] * wm[:, i] + (1 - alphas[i]) * cover[:, i]
        out[:, w - 1 - i] = alphas[i] * wm[:, w - 1 - i] + (1 - alphas[i]) * cover[:, w - 1 - i]
    alpha = _feather_alpha(h, w, torch.device("cpu"), torch.float64).numpy()
    assert np.allclose(alpha, out)


def test_invismark_fixed_bits_are_those_of_uuid4() -> None:
    from lab.models.baseline_invismark import NATIVE_NBITS, UUID_FIXED_BITS

    bits = np.stack([np.unpackbits(np.frombuffer(uuid.uuid4().bytes, dtype=np.uint8))
                     for _ in range(2000)])[:, :NATIVE_NBITS]   # InvisMark utils.uuid_to_bits
    constant = {i: int(bits[0, i]) for i in range(NATIVE_NBITS) if (bits[:, i] == bits[0, i]).all()}
    assert constant == UUID_FIXED_BITS


def test_jnd_matches_official_formula() -> None:
    from lab.models._meta_modules import JND

    torch.manual_seed(0)
    imgs = torch.rand(2, 3, 24, 24)
    jnd = JND(in_channels=1, out_channels=3, blue=True)
    # official videoseal/modules/jnd.py (in-place masked assignment)
    x = 255 * (0.299 * imgs[:, 0:1] + 0.587 * imgs[:, 1:2] + 0.114 * imgs[:, 2:3])
    la = torch.nn.functional.conv2d(x, jnd.kernel_lum, padding=2) / 32
    mask = la <= 127
    la[mask] = 17 * (1 - torch.sqrt(la[mask] / 127 + 1e-5))
    la[~mask] = 3 / 128 * (la[~mask] - 127) + 3
    gx = torch.nn.functional.conv2d(x, jnd.kernel_x, padding=1)
    gy = torch.nn.functional.conv2d(x, jnd.kernel_y, padding=1)
    cm = torch.sqrt(gx ** 2 + gy ** 2)
    cm = 0.117 * 16 * cm ** 2.4 / (cm ** 2 + 26 ** 2)
    hmaps = torch.clamp_min(la + cm - 0.3 * torch.minimum(la, cm), 0).repeat(1, 3, 1, 1)
    hmaps[:, 0] *= 0.5
    hmaps[:, 1] *= 0.5
    assert torch.allclose(jnd.heatmaps(imgs), hmaps / 255, atol=1e-6)


def test_ssl_carriers_are_orthonormal_and_seeded() -> None:
    from lab.models.baseline_ssl import _carriers

    c = _carriers(30, seed=0)
    assert c.shape == (30, 2048)
    assert torch.allclose(c @ c.T, torch.eye(30), atol=1e-5)
    assert torch.equal(c, _carriers(30, seed=0)) and not torch.equal(c, _carriers(30, seed=1))


@pytest.mark.parametrize("model", sorted(REQUIRED))
def test_official_weights_load_and_decode(model: str) -> None:
    if not _has_weights(model):
        pytest.skip(f"poids officiels absents : wmlab weights pull {' '.join(REQUIRED[model])}")
    params = {"steps": 30} if model == "ssl_watermark" else {}
    net = MODELS.build(model, nbits=32, **params)   # strict loading of the official state dict
    data = DATASETS.build("synthetic", modality="image", size=256, num_items=4, seed=1)
    x = torch.stack([data[i]["x"] for i in range(4)])
    torch.manual_seed(0)
    msg = torch.randint(0, 2, (4, 32)).float()
    with torch.no_grad():
        x_w = net.embed(x, msg)
        acc = ((net.extract(x_w) > 0).float() == msg).float().mean().item()
        clean = ((net.extract(x) > 0).float() == msg).float().mean().item()
    psnr = (10 * torch.log10(1 / ((x_w - x) ** 2).mean(dim=(1, 2, 3)))).mean().item()
    assert x_w.shape == x.shape and 0.0 <= float(x_w.min()) and float(x_w.max()) <= 1.0
    assert acc >= 0.9, f"{model} : bit accuracy {acc:.3f}"
    assert clean <= 0.8, f"{model} : image non tatouée décodée ({clean:.3f})"
    assert psnr >= 35.0, f"{model} : PSNR {psnr:.1f} dB"
