"""Builtin watermarking models: contract checks, resolution independence, checkpoints, learning."""

from __future__ import annotations

from pathlib import Path

import pytest
import torch
import torch.nn.functional as F

import wmlab.models  # noqa: F401 — registration
from wmlab.data import build_dataset
from wmlab.models import load_model_from_checkpoint
from wmlab.models.base import WatermarkModel
from wmlab.models.layers import adaptive_avg_pool1d, adaptive_avg_pool2d
from wmlab.plugin_test import run_plugin_test
from wmlab.registry import MODELS


def _failed(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["ok"]]


@pytest.mark.parametrize("name", ["hidden", "audio_conv", "video_framewise"])
def test_plugin_test_passes(name: str) -> None:
    assert _failed(run_plugin_test("model", name)) == []


def test_registered_params_match_contract() -> None:
    hidden = MODELS.get("hidden").params_model().model_dump()
    assert {k: hidden[k] for k in ("channels", "enc_blocks", "dec_blocks", "strength")} == {
        "channels": 64, "enc_blocks": 4, "dec_blocks": 7, "strength": 1.0}
    audio = MODELS.get("audio_conv").params_model().model_dump()
    assert {k: audio[k] for k in ("channels", "blocks", "strength")} == {
        "channels": 32, "blocks": 4, "strength": 1.0}
    video = MODELS.get("video_framewise").params_model().model_dump()
    assert video == {"image_model": "hidden", "image_params": {}, "aggregate": "mean"}
    for name in ("hidden", "audio_conv", "video_framewise"):
        schema = MODELS.describe()
        entry = next(e for e in schema if e["name"] == name)
        assert "nbits" not in entry["params_schema"]["properties"]


def test_unknown_param_is_rejected() -> None:
    with pytest.raises(Exception, match="chanels"):
        MODELS.build("hidden", nbits=8, chanels=16)


@pytest.mark.parametrize("size", [(32, 32), (48, 80), (33, 17)])
def test_hidden_any_resolution(size: tuple[int, int]) -> None:
    model = MODELS.build("hidden", nbits=12, channels=8, enc_blocks=2, dec_blocks=3).eval()
    x = torch.rand(2, 3, *size)
    msg = torch.randint(0, 2, (2, 12)).float()
    with torch.no_grad():
        x_w = model.embed(x, msg)
        assert x_w.shape == x.shape and 0.0 <= float(x_w.min()) and float(x_w.max()) <= 1.0
        assert model.extract(x_w).shape == (2, 12)


@pytest.mark.parametrize("length", [4000, 4001, 7, 16000])
def test_audio_any_length(length: int) -> None:
    model = MODELS.build("audio_conv", nbits=8, channels=4, blocks=3).eval()
    x = (torch.rand(2, 1, length) * 2 - 1) * 0.5
    msg = torch.randint(0, 2, (2, 8)).float()
    with torch.no_grad():
        x_w = model.embed(x, msg)
        assert x_w.shape == x.shape and float(x_w.abs().max()) <= 1.0
        assert model.extract(x_w).shape == (2, 8)


def test_strength_bounds_the_residual() -> None:
    torch.manual_seed(0)
    model = MODELS.build("hidden", nbits=8, channels=8, enc_blocks=1, dec_blocks=1,
                         strength=0.01).eval()
    x = torch.full((2, 3, 16, 16), 0.5)
    with torch.no_grad():
        x_w = model.embed(x, torch.ones(2, 8))
    assert float((x_w - x).abs().max()) <= 0.01 + 1e-6


def test_video_framewise_uses_inner_image_model() -> None:
    model = MODELS.build("video_framewise", nbits=8, image_model="hidden",
                         image_params={"channels": 8, "enc_blocks": 1, "dec_blocks": 2},
                         aggregate="median")
    assert model.image.nbits == 8 and model.image.encoder.features[0][0].out_channels == 8
    x = torch.rand(2, 3, 3, 16, 16)
    msg = torch.randint(0, 2, (2, 8)).float()
    x_w = model.embed(x, msg)
    assert x_w.shape == x.shape
    assert model.extract(x_w).shape == (2, 8)
    with pytest.raises(ValueError, match="pas un modèle image"):
        MODELS.build("video_framewise", nbits=8, image_model="audio_conv")


def test_message_shape_is_checked() -> None:
    model = MODELS.build("hidden", nbits=8, channels=8)
    with pytest.raises(ValueError, match="nbits"):
        model.embed(torch.rand(2, 3, 16, 16), torch.ones(2, 5))


def test_adaptive_pooling_matches_torch() -> None:
    x = torch.randn(2, 3, 13, 17, dtype=torch.float64)
    assert torch.allclose(adaptive_avg_pool2d(x, 4), F.adaptive_avg_pool2d(x, 4))
    assert torch.allclose(adaptive_avg_pool2d(x, (3, 5)), F.adaptive_avg_pool2d(x, (3, 5)))
    y = torch.randn(2, 4, 1001, dtype=torch.float64)
    assert torch.allclose(adaptive_avg_pool1d(y, 7), F.adaptive_avg_pool1d(y, 7))


def _save_checkpoint(path: Path, model: WatermarkModel, name: str, params: dict,
                     prefix: str = "") -> None:
    state = {prefix + k: v for k, v in model.state_dict().items()}
    torch.save({"format": "wmlab-ckpt-v1", "model": state, "optimizer": None, "step": 10,
                "phase": "warmup", "spec": {"model": {"name": name, "params": params}},
                "model_name": name, "model_params": params, "nbits": model.nbits,
                "modality": model.modality, "metrics": {"val/bit_acc": 0.5}}, path)


@pytest.mark.parametrize("prefix", ["", "_orig_mod."])
def test_load_model_from_checkpoint(tmp_path: Path, prefix: str) -> None:
    params = {"channels": 8, "enc_blocks": 2, "dec_blocks": 2}
    model = MODELS.build("hidden", nbits=10, **params)
    with torch.no_grad():
        for p in model.parameters():
            p.add_(0.01)
    path = tmp_path / "step_000010.pt"
    _save_checkpoint(path, model, "hidden", params, prefix)
    loaded = load_model_from_checkpoint(path)
    assert isinstance(loaded, WatermarkModel) and not loaded.training
    for key, value in model.state_dict().items():
        assert torch.equal(loaded.state_dict()[key], value)


def test_load_model_from_checkpoint_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="introuvable"):
        load_model_from_checkpoint(tmp_path / "missing.pt")
    params = {"channels": 8}
    path = tmp_path / "bad.pt"
    _save_checkpoint(path, MODELS.build("hidden", nbits=10, **params), "hidden",
                     {"channels": 16})
    with pytest.raises(RuntimeError, match="ne correspondent pas"):
        load_model_from_checkpoint(path)
    torch.save({"weights": {}}, tmp_path / "other.pt")
    with pytest.raises(ValueError, match="pas un checkpoint"):
        load_model_from_checkpoint(tmp_path / "other.pt")


def test_checkpoint_needing_unpickling_is_refused(tmp_path: Path) -> None:
    """No fallback to ``weights_only=False``: an object that requires arbitrary unpickling
    (here a lambda-free but non-tensor class instance) must never be loaded."""
    from wmlab.train.runtime import load_checkpoint_payload

    class Payload:  # a plain class: the safe loader refuses it, a full load would build it
        def __reduce__(self):
            return (print, ("code exécuté par torch.load !",))

    path = tmp_path / "evil.pt"
    torch.save({"format": "wmlab-ckpt-v1", "model": {}, "extra": Payload()}, path)
    with pytest.raises(ValueError, match="mode sûr"):
        load_model_from_checkpoint(path)
    with pytest.raises(ValueError, match="mode sûr"):
        load_checkpoint_payload(path)


def test_hidden_learns_quickly() -> None:
    """≤ 150 steps, 32 px synthetic images, no attack, Adam 1e-3 ⇒ bit accuracy > 0.8.

    ~10 s on an M4 (MPS); the CPU fallback uses a narrower model to stay fast. The synthetic
    images are generated once (build_dataset) and batches are drawn from memory.
    """
    use_mps = torch.backends.mps.is_available()
    device = "mps" if use_mps else "cpu"
    torch.manual_seed(0)
    nbits, batch_size = 32, 32

    def materialize(seed: int, n: int) -> torch.Tensor:
        ds = build_dataset({"dataset": "synthetic",
                            "params": {"size": 32, "num_items": n, "seed": seed}}, "image")
        return torch.stack([ds[i]["x"] for i in range(len(ds))]).to(device)

    train, val = materialize(0, 512), materialize(1, 128)
    model = MODELS.build("hidden", nbits=nbits, channels=64 if use_mps else 32).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    for _ in range(150):
        x = train[torch.randint(0, train.shape[0], (batch_size,))]
        msg = torch.randint(0, 2, (batch_size, nbits)).float().to(device)
        loss = F.binary_cross_entropy_with_logits(model.extract(model.embed(x, msg)), msg)
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        optimizer.step()
    model.eval()
    with torch.no_grad():
        msg = torch.randint(0, 2, (val.shape[0], nbits)).float().to(device)
        logits = model.extract(model.embed(val, msg))
        accuracy = ((logits > 0).float() == msg).float().mean().item()
    assert accuracy > 0.8, f"bit accuracy {accuracy:.3f} après 150 steps ({device})"
