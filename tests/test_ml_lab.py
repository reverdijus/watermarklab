"""The commented examples of the workspace ``lab/`` package pass the conformance checks."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest
import torch

from wmlab.plugin_test import run_plugin_test
from wmlab.registry import ATTACKS, MODELS, load_plugins

ROOT = Path(__file__).resolve().parent.parent


def _purge_lab() -> None:
    for name in [m for m in sys.modules if m == "lab" or m.startswith("lab.")]:
        del sys.modules[name]


@pytest.fixture(autouse=True)
def fresh_lab() -> Iterator[None]:
    """Force a real import of ``lab`` (the registries are restored after every test)."""
    _purge_lab()
    yield
    _purge_lab()


def test_lab_package_layout() -> None:
    for sub in ("models", "attacks", "datasets", "losses", "metrics", "preprocess"):
        assert (ROOT / "lab" / sub / "__init__.py").is_file()
    report = load_plugins(ROOT)
    assert [e for e in report.errors if e["module"].startswith("lab")] == []
    assert "lab.models.example_residual_unet" in report.loaded
    assert "lab.attacks.example_pixelate" in report.loaded


def test_example_model_passes_plugin_test() -> None:
    results = run_plugin_test("model", "example_unet", workspace=ROOT)
    assert [r for r in results if not r["ok"]] == []
    entry = MODELS.get("example_unet")
    assert entry.builtin is False and entry.modality == ("image",)


def test_example_attack_passes_plugin_test() -> None:
    results = run_plugin_test("attack", "pixelate", workspace=ROOT)
    assert [r for r in results if not r["ok"]] == []


def test_examples_behave() -> None:
    load_plugins(ROOT)
    model = MODELS.build("example_unet", nbits=8, channels=8, depth=2, strength=0.1).eval()
    x = torch.rand(2, 3, 30, 45)  # odd, non multiple of 4
    with torch.no_grad():
        x_w = model.embed(x, torch.ones(2, 8))
    assert x_w.shape == x.shape and float((x_w - x).abs().max()) <= 0.1 + 1e-6
    assert model.extract(x_w).shape == (2, 8)
    attack = ATTACKS.build("pixelate", block=4)
    img = torch.rand(1, 3, 16, 16)
    out = attack(img)
    assert attack(img, 0.0) is img and out.shape == img.shape
    assert torch.equal(out[..., :4, :4], out[..., :1, :1].expand(1, 3, 4, 4))  # 4×4 blocks
