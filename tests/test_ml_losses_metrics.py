"""Loss terms, quality metrics and robustness statistics."""

from __future__ import annotations

import importlib.util
import math
from fractions import Fraction
from math import comb

import pytest
import torch

import wmlab.losses  # noqa: F401 — registration
import wmlab.metrics  # noqa: F401 — registration
from wmlab.losses import Loss, StepBatch
from wmlab.metrics import default_quality_metric
from wmlab.metrics.quality import psnr, si_snr, snr, ssim
from wmlab.metrics.robustness import (
    bit_accuracy,
    detection_threshold,
    false_positive_rate,
    tpr_at_fpr,
)
from wmlab.plugin_test import run_plugin_test
from wmlab.registry import LOSSES, METRICS, MODELS

LOSS_CASES = [("message_bce", m) for m in ("image", "audio", "video")] + \
    [("mse", m) for m in ("image", "audio", "video")] + [("l1", "image"), ("ssim", "image"),
                                                         ("ssim", "video"), ("stft", "audio"),
                                                         ("temporal", "video")]
METRIC_CASES = [("psnr", "image"), ("psnr", "video"), ("ssim", "image"), ("ssim", "video"),
                ("snr", "audio"), ("si_snr", "audio"), ("flicker", "video")]
HAS_LPIPS = importlib.util.find_spec("lpips") is not None
HAS_PYSTOI = importlib.util.find_spec("pystoi") is not None


def _failed(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["ok"]]


@pytest.mark.parametrize("name,modality", LOSS_CASES)
def test_loss_plugin_test(name: str, modality: str) -> None:
    assert _failed(run_plugin_test("loss", name, modality)) == []


@pytest.mark.parametrize("name,modality", METRIC_CASES)
def test_metric_plugin_test(name: str, modality: str) -> None:
    assert _failed(run_plugin_test("metric", name, modality)) == []


def test_only_quality_metrics_are_registered() -> None:
    assert set(METRICS.names()) >= {"psnr", "ssim", "snr", "si_snr", "stoi", "lpips", "flicker"}
    assert not METRICS.has("bit_accuracy") and not METRICS.has("tpr_at_fpr")
    assert METRICS.names("audio") == ["si_snr", "snr", "stoi"]


def test_default_quality_metric() -> None:
    assert [default_quality_metric(m) for m in ("image", "audio", "video")] == ["psnr", "snr",
                                                                                "psnr"]
    with pytest.raises(ValueError, match="modalité inconnue"):
        default_quality_metric("text")


def _batch(x: torch.Tensor, x_w: torch.Tensor, modality: str, logits: torch.Tensor | None = None,
           msg: torch.Tensor | None = None) -> StepBatch:
    msg = msg if msg is not None else torch.zeros(x.shape[0], 4)
    logits = logits if logits is not None else torch.zeros(x.shape[0], 4)
    return StepBatch(x=x, x_w=x_w, x_att=x_w, msg=msg, logits=logits, modality=modality,
                     model=MODELS.build("hidden", nbits=4, channels=4), step=0, phase="p",
                     extras={})


def test_loss_values() -> None:
    x = torch.zeros(2, 3, 8, 8)
    x_w = torch.full_like(x, 0.1)
    assert float(LOSSES.build("mse")(_batch(x, x_w, "image"))) == pytest.approx(0.01)
    assert float(LOSSES.build("l1")(_batch(x, x_w, "image"))) == pytest.approx(0.1)
    same = torch.rand(2, 3, 16, 16)
    assert float(LOSSES.build("ssim")(_batch(same, same.clone(), "image"))) == pytest.approx(
        0.0, abs=1e-5)
    logits = torch.tensor([[10.0, -10.0, 10.0, -10.0]])
    msg = torch.tensor([[1.0, 0.0, 1.0, 0.0]])
    bce = LOSSES.build("message_bce")(_batch(torch.zeros(1, 3, 4, 4), torch.zeros(1, 3, 4, 4),
                                             "image", logits, msg))
    assert float(bce) < 1e-3
    assert isinstance(LOSSES.build("mse"), Loss)


def test_stft_loss() -> None:
    torch.manual_seed(0)
    x = torch.randn(2, 1, 4000) * 0.1
    loss = LOSSES.build("stft", fft_sizes=[256, 512, 8192])
    assert float(loss(_batch(x, x.clone(), "audio"))) == pytest.approx(0.0, abs=1e-4)
    small = float(loss(_batch(x, x + 0.001 * torch.randn_like(x), "audio")))
    large = float(loss(_batch(x, x + 0.05 * torch.randn_like(x), "audio")))
    assert 0 < small < large


def test_temporal_loss() -> None:
    x = torch.zeros(1, 4, 3, 8, 8)
    static = x + 0.1  # constant residual over time → no temporal penalty
    assert float(LOSSES.build("temporal")(_batch(x, static, "video"))) == 0.0
    flicker = x.clone()
    flicker[:, 1::2] += 0.1
    assert float(LOSSES.build("temporal", p=2)(_batch(x, flicker, "video"))) == pytest.approx(0.01)


@pytest.mark.skipif(HAS_LPIPS, reason="lpips installé : l'erreur ne peut pas être testée")
def test_lpips_missing_gives_clear_error() -> None:
    with pytest.raises(ImportError, match="lpips"):
        LOSSES.build("lpips")
    with pytest.raises(ImportError, match="quality"):
        METRICS.get("lpips").obj(torch.rand(1, 3, 8, 8), torch.rand(1, 3, 8, 8))


@pytest.mark.skipif(HAS_PYSTOI, reason="pystoi installé")
def test_stoi_missing_gives_clear_error() -> None:
    with pytest.raises(ImportError, match="pystoi"):
        METRICS.get("stoi").obj(torch.rand(1, 1, 100), torch.rand(1, 1, 100))


# -- quality metrics ------------------------------------------------------------------------
def test_psnr_known_values() -> None:
    x = torch.zeros(2, 3, 4, 4)
    x_w = x.clone()
    x_w[0] += 0.1   # MSE 0.01 → 20 dB
    x_w[1] += 0.01  # MSE 1e-4 → 40 dB
    assert psnr(x, x_w).tolist() == pytest.approx([20.0, 40.0], abs=1e-4)
    assert float(psnr(x, x.clone())[0]) == 100.0  # identical → capped
    video = torch.zeros(1, 2, 3, 4, 4)
    video_w = video.clone()
    video_w[0, 0] += 0.1
    video_w[0, 1] += 0.01
    assert float(psnr(video, video_w)[0]) == pytest.approx(30.0, abs=1e-4)  # mean over frames


def test_ssim_properties() -> None:
    torch.manual_seed(0)
    x = torch.rand(2, 3, 32, 32)
    assert ssim(x, x.clone()).tolist() == pytest.approx([1.0, 1.0], abs=1e-5)
    noisy = (x + 0.2 * torch.randn_like(x)).clamp(0, 1)
    slightly = (x + 0.02 * torch.randn_like(x)).clamp(0, 1)
    assert float(ssim(x, noisy).mean()) < float(ssim(x, slightly).mean()) < 1.0
    tiny = torch.rand(1, 3, 5, 7)
    assert ssim(tiny, tiny.clone()).shape == (1,)


def test_snr_and_si_snr() -> None:
    t = torch.linspace(0, 1, 1600)
    x = torch.sin(2 * math.pi * 50 * t).view(1, 1, -1)
    noise = torch.randn_like(x)
    noise = noise / noise.pow(2).sum().sqrt() * x.pow(2).sum().sqrt() * 0.1  # 20 dB
    assert float(snr(x, x + noise)[0]) == pytest.approx(20.0, abs=1e-3)
    assert float(si_snr(x, 0.5 * x)[0]) >= 99.0      # pure gain: scale invariant
    assert float(snr(x, 0.5 * x)[0]) == pytest.approx(6.02, abs=0.01)


# -- robustness -----------------------------------------------------------------------------
def _brute_force_threshold(n: int, fpr: float) -> int:
    for k in range(0, n + 2):
        tail = sum(comb(n, i) for i in range(k, n + 1))
        if Fraction(tail, 2 ** n) <= Fraction(fpr):
            return k
    raise AssertionError


@pytest.mark.parametrize("n", [1, 2, 5, 8, 13, 16, 24])
@pytest.mark.parametrize("fpr", [0.5, 0.1, 0.01, 1e-3, 1e-6])
def test_detection_threshold_matches_brute_force(n: int, fpr: float) -> None:
    assert detection_threshold(n, fpr) == _brute_force_threshold(n, fpr)


def test_detection_threshold_properties() -> None:
    assert detection_threshold(48, 1.0) == 0
    assert detection_threshold(8, 2 ** -8) == 8
    assert detection_threshold(8, 2 ** -8 * 0.999) == 9  # impossible to reach the target FPR
    k = detection_threshold(256, 1e-6)
    assert false_positive_rate(256, k) <= 1e-6 < false_positive_rate(256, k - 1)
    assert detection_threshold(4096, 1e-12) <= 4096
    for bad in ((0, 0.1), (8, 0.0), (8, 1.5)):
        with pytest.raises(ValueError):
            detection_threshold(*bad)


def test_bit_accuracy_and_tpr() -> None:
    msg = torch.tensor([[1.0, 0.0, 1.0, 1.0], [0.0, 0.0, 1.0, 1.0]])
    logits = torch.tensor([[2.0, -1.0, 3.0, 0.5], [1.0, -2.0, 1.0, -1.0]])
    assert bit_accuracy(logits, msg).tolist() == [1.0, 0.5]
    # 4 bits, fpr 1/16 → k = 4: only the perfect item is detected
    assert tpr_at_fpr(logits, msg, 1 / 16) == 0.5
    assert tpr_at_fpr(logits, msg, 1.0) == 1.0
    assert tpr_at_fpr(logits, msg, 0.01) == 0.0
