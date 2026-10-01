"""Datasets, preprocessing, path resolution, dataset versions and loaders."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest
import torch

from wmlab.data import (
    build_dataset,
    collate,
    dataset_version,
    make_loader,
    resolve_path,
)
from wmlab.data.datasets import decode_audio
from wmlab.plugin_test import run_plugin_test
from wmlab.registry import DATASETS, PREPROCESS
from wmlab.spec import DataSplitSpec

PREPROCESS_CASES = [
    ("resize", "image", {"size": 24}), ("resize", "video", {"size": [20, 30]}),
    ("center_crop", "image", {"size": 16}), ("center_crop", "video", {"size": 16}),
    ("random_crop", "image", {"size": 16}), ("random_crop", "video", {"size": 16}),
    ("random_resized_crop", "image", {"size": 16}), ("hflip", "image", {}),
    ("hflip", "video", {}), ("random_clip", "audio", {"seconds": 0.1}),
    ("normalize_peak", "audio", {}), ("random_gain", "audio", {}),
    ("temporal_crop", "video", {"frames": 2}),
]


def _failed(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["ok"]]


# -- synthetic ------------------------------------------------------------------------------
@pytest.mark.parametrize("modality", ["image", "audio", "video"])
def test_synthetic_plugin_test(modality: str) -> None:
    assert _failed(run_plugin_test("dataset", "synthetic", modality)) == []


def test_synthetic_is_deterministic_and_varied() -> None:
    ds = DATASETS.build("synthetic", modality="image", size=32, num_items=8, seed=3)
    again = DATASETS.build("synthetic", modality="image", size=32, num_items=8, seed=3)
    other = DATASETS.build("synthetic", modality="image", size=32, num_items=8, seed=4)
    assert torch.equal(ds[5]["x"], again[5]["x"]) and ds[5]["id"] == again[5]["id"]
    assert not torch.equal(ds[5]["x"], other[5]["x"])
    assert not torch.equal(ds[0]["x"], ds[1]["x"])
    assert float(ds[0]["x"].std()) > 0.02  # structured content, not a flat image
    with pytest.raises(IndexError):
        ds[8]


def test_synthetic_shapes_and_ranges() -> None:
    audio = DATASETS.build("synthetic", modality="audio", length=3000, num_items=2)[0]["x"]
    assert audio.shape == (1, 3000) and float(audio.abs().max()) <= 1.0
    video = DATASETS.build("synthetic", modality="video", size=24, frames=5, num_items=2)[1]["x"]
    assert video.shape == (5, 3, 24, 24) and 0.0 <= float(video.min()) <= float(video.max()) <= 1
    assert not torch.equal(video[0], video[-1])  # objects move


# -- folders --------------------------------------------------------------------------------
def _write_images(folder: Path, n: int = 3) -> None:
    from PIL import Image

    (folder / "sub").mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(0)
    for i in range(n):
        target = folder / ("sub" if i == n - 1 else "") / f"img_{i}.png"
        Image.fromarray(rng.integers(0, 255, (20 + i, 30, 3), dtype=np.uint8)).save(target)
    (folder / "broken.jpg").write_bytes(b"not a jpeg")
    (folder / "notes.txt").write_text("ignored")


def _write_wav(path: Path, seconds: float, sample_rate: int = 22050) -> None:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    signal = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(signal.tobytes())


def _write_video(path: Path, frames: int = 6, width: int = 40, height: int = 30) -> None:
    import av

    with av.open(str(path), "w") as out:
        stream = out.add_stream("mpeg4", rate=24)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        for i in range(frames):
            arr = np.full((height, width, 3), i * 30, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)


def test_image_folder(tmp_path: Path) -> None:
    _write_images(tmp_path)
    ds = DATASETS.build("image_folder", root=str(tmp_path))
    assert len(ds) == 4  # 3 images + the corrupted jpg (skipped at read time)
    with pytest.warns(RuntimeWarning, match="illisible"):
        item = ds[0]  # broken.jpg sorts first → the next file is returned
    assert item["id"] == "img_0.png" and item["x"].shape == (3, 20, 30)
    assert "sub/img_2.png" in [ds[i]["id"] for i in range(len(ds))]
    flat = DATASETS.build("image_folder", root=str(tmp_path), recursive=False, limit=2)
    assert len(flat) == 2
    results = run_plugin_test("dataset", "image_folder", "image",
                              params={"root": str(tmp_path), "extensions": [".png"]})
    assert _failed(results) == []


def test_folder_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="introuvable"):
        DATASETS.build("image_folder", root=str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="aucun fichier"):
        DATASETS.build("image_folder", root=str(tmp_path))


def test_audio_folder(tmp_path: Path) -> None:
    _write_wav(tmp_path / "a.wav", 2.0)
    _write_wav(tmp_path / "b.wav", 0.25)
    ds = DATASETS.build("audio_folder", root=str(tmp_path), sample_rate=16000, clip_seconds=0.5)
    first, short = ds[0]["x"], ds[1]["x"]
    assert first.shape == (1, 8000) and short.shape == (1, 8000)
    assert float(first.abs().max()) == pytest.approx(0.5, abs=0.05)
    assert float(short[0, 5000:].abs().max()) == 0.0  # zero padded
    full = decode_audio(tmp_path / "a.wav", 16000)
    assert full.shape[1] == pytest.approx(32000, abs=64)
    assert _failed(run_plugin_test("dataset", "audio_folder", "audio",
                                   params={"root": str(tmp_path), "clip_seconds": 0.25})) == []


def test_video_folder(tmp_path: Path) -> None:
    _write_video(tmp_path / "clip.mp4", frames=4)
    ds = DATASETS.build("video_folder", root=str(tmp_path), frames=6, size=16)
    x = ds[0]["x"]
    assert x.shape == (6, 3, 16, 16)
    assert torch.equal(x[4], x[5])  # short clip: last frame repeated
    assert float(x[3].mean()) > float(x[0].mean())
    assert _failed(run_plugin_test("dataset", "video_folder", "video",
                                   params={"root": str(tmp_path), "frames": 4, "size": 16})) == []


# -- @catalog paths -------------------------------------------------------------------------
def test_resolve_path(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (workspace / "datasets" / "coco.yaml").write_text(
        "name: coco\nmodality: image\npaths: {default: '~/data/coco', scw-l40s: /data/coco}\n",
        encoding="utf-8")
    assert resolve_path("/abs/path") == "/abs/path"
    assert resolve_path("relative/dir") == "relative/dir"
    assert resolve_path("@coco") == str(Path("~/data/coco").expanduser())
    assert resolve_path("@coco/train2017") == str(Path("~/data/coco/train2017").expanduser())
    monkeypatch.setenv("WMLAB_HOST", "scw-l40s")
    assert resolve_path("@coco") == "/data/coco"
    with pytest.raises(ValueError, match="catalogue"):
        resolve_path("@missing")
    (workspace / "datasets" / "only.yaml").write_text(
        "name: only\nmodality: image\npaths: {mac: /x}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="aucun chemin pour l'hôte 'scw-l40s'"):
        resolve_path("@only")


def test_folder_dataset_with_catalog(workspace: Path, tmp_path: Path) -> None:
    images = tmp_path / "imgs"
    _write_images(images)
    (workspace / "datasets" / "mine.yaml").write_text(
        f"name: mine\nmodality: image\npaths: {{default: '{images}'}}\n", encoding="utf-8")
    ds = build_dataset({"dataset": "image_folder", "params": {"root": "@mine",
                                                              "extensions": [".png"]},
                        "preprocess": [{"name": "resize", "params": {"size": [16, 16]}}]},
                       "image")
    assert len(ds) == 3 and ds[0]["x"].shape == (3, 16, 16)


# -- versions, build, loaders ---------------------------------------------------------------
def test_dataset_version() -> None:
    split = {"dataset": "image_folder", "params": {"root": "@coco"},
             "preprocess": [{"name": "random_crop", "params": {"size": 128}}], "limit": None}
    version = dataset_version(split)
    assert len(version) == 12 and all(c in "0123456789abcdef" for c in version)
    assert version == dataset_version(DataSplitSpec.model_validate(split))
    reordered = {"limit": None, "preprocess": split["preprocess"], "params": {"root": "@coco"},
                 "dataset": "image_folder"}
    assert version == dataset_version(reordered)
    assert version != dataset_version({**split, "limit": 10})
    assert version != dataset_version({**split, "params": {"root": "@coco2"}})


def test_build_dataset_applies_preprocess_in_order() -> None:
    split = {"dataset": "synthetic", "params": {"size": 40, "num_items": 6},
             "preprocess": [{"name": "resize", "params": {"size": 20}},
                            {"name": "center_crop", "params": {"size": 16}}], "limit": 4}
    ds = build_dataset(split, "image")
    assert len(ds) == 4 and ds[0]["x"].shape == (3, 16, 16)
    assert ds.transform.deterministic
    audio = build_dataset({"dataset": "synthetic", "params": {"length": 2000, "num_items": 2}},
                          "audio")
    assert audio[0]["x"].shape == (1, 2000)  # modality injected from the project
    with pytest.raises(ValueError, match="modalité"):
        build_dataset({"dataset": "synthetic", "params": {"modality": "audio"}}, "image")
    with pytest.raises(ValueError, match="ne supporte pas"):
        build_dataset({"dataset": "synthetic", "preprocess": [{"name": "random_clip",
                                                               "params": {"seconds": 1}}]},
                      "image")
    with pytest.raises(ValueError, match="ne supporte pas"):
        build_dataset({"dataset": "audio_folder", "params": {"root": "/tmp"}}, "image")


def test_make_loader_and_collate() -> None:
    split = {"dataset": "synthetic", "params": {"size": 16, "num_items": 10}}
    loader = make_loader(split, "image", batch_size=4, num_workers=0, shuffle=True, seed=0)
    batches = list(loader)
    assert len(batches) == 2 and batches[0]["x"].shape == (4, 3, 16, 16)
    assert len(batches[0]["id"]) == 4
    again = [b["id"] for b in make_loader(split, "image", batch_size=4, seed=0)]
    assert again == [b["id"] for b in batches]  # seeded shuffling
    with pytest.warns(RuntimeWarning, match="batch_size"):
        small = make_loader(split, "image", batch_size=32)
    assert len(list(small)) == 1
    with pytest.raises(ValueError, match="même forme"):
        collate([{"x": torch.zeros(3, 4, 4), "id": "a"}, {"x": torch.zeros(3, 5, 5), "id": "b"}])


def test_make_loader_with_workers() -> None:
    split = {"dataset": "synthetic", "params": {"size": 16, "num_items": 4},
             "preprocess": [{"name": "hflip"}]}
    loader = make_loader(split, "image", batch_size=2, num_workers=1, seed=0)
    assert [b["x"].shape for b in loader] == [(2, 3, 16, 16), (2, 3, 16, 16)]


# -- preprocessing --------------------------------------------------------------------------
@pytest.mark.parametrize("name,modality,params", PREPROCESS_CASES)
def test_preprocess_plugin_test(name: str, modality: str, params: dict) -> None:
    assert _failed(run_plugin_test("preprocess", name, modality, params=params)) == []


def test_preprocess_semantics() -> None:
    img = torch.rand(3, 30, 40)
    assert PREPROCESS.build("resize", size=15)(img).shape == (3, 15, 20)  # short side
    assert PREPROCESS.build("resize", size=[10, 12])(img).shape == (3, 10, 12)
    assert PREPROCESS.build("center_crop", size=64)(img).shape == (3, 64, 64)  # upscales
    crop = PREPROCESS.build("random_resized_crop", size=16, scale=[0.3, 1.0])
    assert crop(img).shape == (3, 16, 16) and crop.deterministic is False
    video = torch.rand(4, 3, 20, 20)
    torch.manual_seed(0)
    cropped = PREPROCESS.build("random_crop", size=8)(video)
    assert cropped.shape == (4, 3, 8, 8)
    assert torch.equal(PREPROCESS.build("hflip", p=1.0)(video), video.flip(-1))
    assert PREPROCESS.build("temporal_crop", frames=6)(video).shape == (6, 3, 20, 20)
    centred = PREPROCESS.build("temporal_crop", frames=2, random=False)
    assert centred.deterministic and torch.equal(centred(video), video[1:3])
    audio = torch.rand(1, 1000) * 0.2
    assert PREPROCESS.build("random_clip", seconds=0.01, sample_rate=16000)(audio).shape == (1,
                                                                                            160)
    assert PREPROCESS.build("random_clip", seconds=0.1)(audio).shape == (1, 1600)
    peak = PREPROCESS.build("normalize_peak", peak=0.5)(audio)
    assert float(peak.abs().max()) == pytest.approx(0.5)
    assert torch.equal(PREPROCESS.build("normalize_peak")(torch.zeros(1, 10)), torch.zeros(1, 10))


def test_decode_audio_clip_from_start(tmp_path: Path) -> None:
    _write_wav(tmp_path / "c.wav", 0.1, sample_rate=16000)
    x = decode_audio(tmp_path / "c.wav", 16000, clip_samples=800, start_seconds=0.0)
    full = decode_audio(tmp_path / "c.wav", 16000)
    assert x.shape == (1, 800) and torch.allclose(x, full[:, :800])
    centred = decode_audio(tmp_path / "c.wav", 16000, clip_samples=800)
    assert torch.allclose(centred, full[:, 400:1200])
