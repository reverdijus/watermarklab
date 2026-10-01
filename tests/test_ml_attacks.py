"""Builtin attacks: contract (shape, strength=0 identity), codecs, STE gradients, helpers."""

from __future__ import annotations

import random
import warnings

import numpy as np
import pytest
import torch

import wmlab.attacks  # noqa: F401 — registration
from wmlab.attacks import apply_attack, codecs, sample_strength, shared_randomness
from wmlab.attacks.base import per_sample_uniform
from wmlab.data.synthetic import item_generator, synthetic_audio, synthetic_image
from wmlab.plugin_test import run_plugin_test
from wmlab.registry import ATTACKS

IMAGE = ["identity", "jpeg", "jpeg_diff", "gaussian_noise", "gaussian_blur", "resize", "crop",
         "rotate", "brightness", "contrast", "saturation", "combo"]
AUDIO = ["identity", "gaussian_noise", "lowpass", "highpass", "resample", "amplitude", "echo",
         "time_shift", "mp3", "aac"]
VIDEO = ["identity", "framewise", "h264", "frame_drop", "temporal_blur"]
CASES = [(n, "image") for n in IMAGE] + [(n, "audio") for n in AUDIO] + [(n, "video") for n in VIDEO]
NON_DIFFERENTIABLE = {"jpeg", "mp3", "aac", "h264"}
DEFAULTS = {
    "jpeg": {"quality": 50}, "jpeg_diff": {"quality": 50}, "gaussian_blur": {"sigma": 2.0},
    "resize": {"scale": 0.5}, "crop": {"area": 0.5}, "rotate": {"degrees": 10.0},
    "brightness": {"delta": 0.3}, "contrast": {"factor": 0.5}, "saturation": {"factor": 0.5},
    "lowpass": {"cutoff_hz": 3000.0}, "highpass": {"cutoff_hz": 500.0}, "resample": {"rate": 8000},
    "amplitude": {"gain_db": -6.0}, "echo": {"delay_ms": 100.0, "decay": 0.3},
    "time_shift": {"max_ms": 50.0}, "mp3": {"bitrate_kbps": 64}, "aac": {"bitrate_kbps": 64},
    "framewise": {"attack": "jpeg", "params": {}}, "h264": {"crf": 28},
    "frame_drop": {"p": 0.2}, "temporal_blur": {"weight": 0.3},
}


def _failed(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["ok"]]


def _sample(modality: str, shape_hw: tuple[int, int] = (32, 32)) -> torch.Tensor:
    if modality == "image":
        return torch.stack([synthetic_image(item_generator(0, i), 32) for i in range(2)])
    if modality == "audio":
        return torch.stack([synthetic_audio(item_generator(0, i), 4000) for i in range(2)])
    return torch.rand(2, 3, 3, *shape_hw)


def test_registry_lists_every_contract_attack() -> None:
    for name, modality in CASES:
        assert ATTACKS.has(name), name
        assert ATTACKS.get(name).supports(modality), (name, modality)


@pytest.mark.parametrize("name", sorted(DEFAULTS))
def test_contract_defaults(name: str) -> None:
    defaults = ATTACKS.get(name).params_model().model_dump()
    for key, value in DEFAULTS[name].items():
        assert defaults[key] == value, (name, key)
    assert ATTACKS.get("gaussian_noise").params_model().model_dump() == {"std": 0.06,
                                                                          "snr_db": 20.0}


@pytest.mark.parametrize("name,modality", CASES)
def test_plugin_test_passes(name: str, modality: str) -> None:
    assert _failed(run_plugin_test("attack", name, modality)) == []


@pytest.mark.parametrize("name,modality", CASES)
def test_strength_zero_is_exact_identity_and_shape_kept(name: str, modality: str) -> None:
    attack = ATTACKS.build(name)
    x = _sample(modality)
    assert attack(x, 0.0) is x
    assert attack(x, strength=0) is x
    out = attack(x, 1.0)
    assert out.shape == x.shape and out.dtype == x.dtype and bool(torch.isfinite(out).all())
    assert getattr(attack, "differentiable") is (name not in NON_DIFFERENTIABLE)


@pytest.mark.parametrize("name", ["jpeg", "jpeg_diff", "gaussian_blur", "resize", "crop",
                                  "rotate", "saturation"])
def test_image_attacks_accept_odd_sizes(name: str) -> None:
    x = torch.rand(2, 3, 17, 23)
    assert ATTACKS.build(name)(x, 0.7).shape == x.shape


@pytest.mark.parametrize("size", [(17, 23), (32, 32), (7, 9)])
def test_h264_odd_sizes(size: tuple[int, int]) -> None:
    x = torch.rand(1, 4, 3, *size)
    assert ATTACKS.build("h264")(x).shape == x.shape


def test_severity_grows_with_strength() -> None:
    x = _sample("image")
    for name in ("jpeg", "jpeg_diff", "gaussian_blur", "resize", "contrast"):
        attack = ATTACKS.build(name)
        err_half = float((attack(x, 0.3) - x).abs().mean())
        err_full = float((attack(x, 1.0) - x).abs().mean())
        assert 0.0 < err_half < err_full, name


def test_jpeg_diff_is_close_to_real_jpeg() -> None:
    x = torch.stack([synthetic_image(item_generator(3, i), 64) for i in range(4)])
    for quality in (90, 50, 20):
        real = ATTACKS.build("jpeg", quality=quality)(x)
        approx = ATTACKS.build("jpeg_diff", quality=quality)(x)
        gap = 10 * torch.log10(1.0 / (real - approx).pow(2).mean())
        loss = 10 * torch.log10(1.0 / (real - x).pow(2).mean())
        assert float(gap) > float(loss) + 8.0, quality  # much closer to real JPEG than to x


def test_jpeg_diff_gradient() -> None:
    x = torch.rand(1, 3, 16, 16, requires_grad=True)
    ATTACKS.build("jpeg_diff")(x).sum().backward()
    assert x.grad is not None and float(x.grad.abs().sum()) > 0


@pytest.mark.parametrize("name,modality", [("jpeg", "image"), ("h264", "video"),
                                           ("mp3", "audio")])
def test_straight_through_gradient(name: str, modality: str) -> None:
    attack = ATTACKS.build(name)
    x = _sample(modality).clone().requires_grad_(True)
    out = apply_attack(attack, x, 1.0)
    assert torch.allclose(out.detach(), attack(x.detach(), 1.0), atol=1e-6)
    weights = torch.randn_like(out)
    (out * weights).sum().backward()
    assert x.grad is not None and float(x.grad.abs().sum()) > 0
    assert torch.allclose(x.grad, weights)  # identity gradient


def test_apply_attack_differentiable_and_zero() -> None:
    blur = ATTACKS.build("gaussian_blur")
    x = torch.rand(1, 3, 16, 16, requires_grad=True)
    assert apply_attack(blur, x, 0.0) is x
    assert apply_attack(None, x, 1.0) is x
    apply_attack(blur, x, 1.0).sum().backward()
    assert x.grad is not None


def _lag(reference: np.ndarray, test: np.ndarray, max_lag: int = 1200) -> int:
    best, best_lag = -2.0, 0
    for lag in range(-max_lag, max_lag + 1):
        a, b = (reference[:len(reference) - lag], test[lag:]) if lag >= 0 else \
            (reference[-lag:], test[:len(test) + lag])
        corr = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-12))
        if corr > best:
            best, best_lag = corr, lag
    return best_lag


@pytest.mark.parametrize("codec", ["mp3", "aac"])
@pytest.mark.parametrize("sample_rate", [16000, 44100])
def test_audio_codecs_are_aligned(codec: str, sample_rate: int) -> None:
    x = synthetic_audio(item_generator(7, 0), sample_rate // 4, sample_rate)[None]
    y = ATTACKS.build(codec, bitrate_kbps=128, sample_rate=sample_rate)(x)
    assert y.shape == x.shape
    assert _lag(x[0, 0].numpy(), y[0, 0].numpy()) == 0


def test_mp3_falls_back_to_aac(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codecs, "MP3_ENCODERS", ("nonexistent_mp3",))
    monkeypatch.setattr(codecs, "_warned", set())
    x = synthetic_audio(item_generator(1, 0), 4000)[None]
    with pytest.warns(RuntimeWarning, match="AAC"):
        y = ATTACKS.build("mp3")(x)
    assert y.shape == x.shape and _lag(x[0, 0].numpy(), y[0, 0].numpy()) == 0


def test_h264_fallback_encoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codecs, "H264_ENCODERS", ("nonexistent_h264", "mpeg4"))
    monkeypatch.setattr(codecs, "_warned", set())
    x = torch.rand(1, 4, 3, 18, 22)
    with pytest.warns(RuntimeWarning, match="mpeg4"):
        y = ATTACKS.build("h264")(x)
    assert y.shape == x.shape
    monkeypatch.setattr(codecs, "H264_ENCODERS", ("nonexistent_h264",))
    with pytest.raises(codecs.CodecUnavailableError):
        ATTACKS.build("h264")(x)


def test_h264_quality_depends_on_crf() -> None:
    x = torch.stack([torch.stack([synthetic_image(item_generator(0, i), 32)] * 4)
                     for i in range(2)])
    mild = (ATTACKS.build("h264", crf=10)(x) - x).abs().mean()
    harsh = (ATTACKS.build("h264", crf=45)(x) - x).abs().mean()
    assert float(mild) < float(harsh)


def test_combo_and_framewise() -> None:
    combo = ATTACKS.build("combo", attacks=[{"name": "jpeg", "params": {"quality": 30}},
                                            {"name": "gaussian_blur"}])
    assert combo.differentiable and len(combo.steps) == 2
    x = torch.rand(2, 3, 16, 16, requires_grad=True)
    combo(x, 1.0).sum().backward()  # STE on the jpeg step keeps the chain differentiable
    assert x.grad is not None
    with pytest.raises(KeyError, match="inconnue"):
        ATTACKS.build("combo", attacks=[{"name": "nope"}])
    with pytest.raises(ValueError, match="pas une attaque image"):
        ATTACKS.build("framewise", attack="mp3")


def test_framewise_shares_random_parameters_across_frames() -> None:
    frame = torch.rand(3, 24, 24)
    video = frame.expand(2, 5, 3, 24, 24).clone()
    video[1] = torch.rand(3, 24, 24)
    torch.manual_seed(0)
    out = ATTACKS.build("framewise", attack="crop", params={"area": 0.4})(video)
    for f in range(1, 5):  # identical frames get the identical crop
        assert torch.allclose(out[0, f], out[0, 0])
    with shared_randomness(3):
        values = per_sample_uniform(6, "cpu")
    assert torch.equal(values[:3], values[:1].expand(3)) and torch.equal(values[3:],
                                                                          values[3:4].expand(3))


def test_frame_drop_repeats_previous_frames() -> None:
    video = torch.arange(6, dtype=torch.float32).view(1, 6, 1, 1, 1).expand(1, 6, 3, 2, 2)
    torch.manual_seed(1)
    out = ATTACKS.build("frame_drop", p=1.0)(video)
    assert torch.equal(out, video[:, :1].expand_as(video))


def test_sample_strength() -> None:
    assert sample_strength(0.7, random_strength=False) == 0.7
    assert sample_strength(1.5, random_strength=False) == 1.0
    assert sample_strength(0.0) == 0.0
    rng = random.Random(0)
    draws = [sample_strength(0.5, True, rng) for _ in range(200)]
    assert all(0.0 <= d <= 0.5 for d in draws) and max(draws) > 0.4
    g = torch.Generator().manual_seed(0)
    first = sample_strength(1.0, True, g)
    assert first == sample_strength(1.0, True, torch.Generator().manual_seed(0))


def test_audio_attack_rejects_images() -> None:
    with pytest.raises(ValueError, match="audio"):
        ATTACKS.build("lowpass")(torch.rand(2, 3, 8, 8))


def test_no_warning_for_standard_codecs() -> None:
    if not codecs.encoder_available("libx264") or not codecs.encoder_available("libmp3lame"):
        pytest.skip("encodeurs standard absents de ce wheel PyAV")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ATTACKS.build("mp3")(_sample("audio"))
        ATTACKS.build("h264")(_sample("video"))
