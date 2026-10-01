"""Builtin datasets (ARCHITECTURE.md §4.3): ``synthetic``, ``image_folder``, ``audio_folder``,
``video_folder``. Items are ``{"x": Tensor (no batch dim), "id": str}``.

Folder datasets resolve ``@name`` paths through the dataset catalog, list files once (sorted,
optionally recursive, ``limit`` first files) and skip undecodable files with a warning.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, Literal

import numpy as np
import torch
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor
from torch.utils.data import Dataset

from wmlab.data.synthetic import item_generator, synthetic_audio, synthetic_image, synthetic_video
from wmlab.registry import DATASETS

IMAGE_EXTENSIONS = [".jpg", ".jpeg", ".png", ".webp"]
AUDIO_EXTENSIONS = [".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus"]
VIDEO_EXTENSIONS = [".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"]
MAX_CONSECUTIVE_FAILURES = 20


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


# -- synthetic ------------------------------------------------------------------------------
class SyntheticParams(_Params):
    modality: Literal["image", "audio", "video"] = Field(
        "image", description="Modalité générée (renseignée automatiquement depuis le projet)")
    size: int = Field(128, ge=8, le=2048, description="Côté des images/frames (pixels)")
    length: int = Field(16000, ge=64, description="Longueur audio (échantillons)")
    frames: int = Field(8, ge=1, le=512, description="Nombre de frames (vidéo)")
    num_items: int = Field(512, ge=1, description="Nombre d'items")
    seed: int = Field(0, description="Graine (un item dépend uniquement de (seed, index))")
    sample_rate: int = Field(16000, ge=1000, description="Fréquence d'échantillonnage audio (Hz)")


@DATASETS.register("synthetic", modality=["image", "audio", "video"], params=SyntheticParams)
class SyntheticDataset(Dataset):
    """Contenu procédural déterministe : formes, dégradés, textures / sons / formes en mouvement."""

    def __init__(self, modality: str = "image", size: int = 128, length: int = 16000,
                 frames: int = 8, num_items: int = 512, seed: int = 0,
                 sample_rate: int = 16000) -> None:
        if modality not in ("image", "audio", "video"):
            raise ValueError(f"modalité inconnue '{modality}' (image, audio ou video)")
        self.modality = modality
        self.size, self.length, self.frames = int(size), int(length), int(frames)
        self.num_items, self.seed, self.sample_rate = int(num_items), int(seed), int(sample_rate)

    def __len__(self) -> int:
        return self.num_items

    def __getitem__(self, index: int) -> dict[str, Any]:
        if not 0 <= index < self.num_items:
            raise IndexError(index)
        g = item_generator(self.seed, index, salt=self.modality)
        if self.modality == "image":
            x = synthetic_image(g, self.size)
        elif self.modality == "audio":
            x = synthetic_audio(g, self.length, self.sample_rate)
        else:
            x = synthetic_video(g, self.frames, self.size)
        return {"x": x, "id": f"synthetic-{self.modality}-{self.seed}-{index:06d}"}


# -- folders --------------------------------------------------------------------------------
def resolve_root(root: str) -> Path:
    """Resolve ``root`` (``@catalog`` name, ``~``, workspace-relative) to an existing folder."""
    from wmlab.data import resolve_path

    resolved = resolve_path(root)
    path = Path(resolved).expanduser()
    if not path.is_absolute() and not path.exists():
        try:
            from wmlab.paths import workspace_root

            candidate = workspace_root() / path
            if candidate.exists():
                path = candidate
        except Exception:  # noqa: BLE001 — keep the original path for the error message
            pass
    if not path.is_dir():
        hint = f" (« {root} » résolu en {path})" if str(root) != str(path) else ""
        raise FileNotFoundError(f"dossier de données introuvable : {path}{hint}")
    return path


def list_files(root: Path, extensions: list[str], recursive: bool, limit: int | None) -> list[Path]:
    """Sorted files of ``root`` whose suffix (case-insensitive) is in ``extensions``."""
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    pattern = root.rglob("*") if recursive else root.glob("*")
    files = sorted(p for p in pattern
                   if p.is_file() and p.suffix.lower() in exts and not p.name.startswith("."))
    if not files:
        raise ValueError(f"aucun fichier ({', '.join(sorted(exts))}) dans {root}"
                         f"{' (récursif)' if recursive else ''}")
    return files[:limit] if limit else files


class _FolderDataset(Dataset):
    """Common logic: file listing, ids relative to the root, skip-on-error."""

    kind = "fichier"

    def __init__(self, root: str, extensions: list[str], recursive: bool,
                 limit: int | None) -> None:
        self.root_spec = root
        self.root = resolve_root(root)
        self.files = list_files(self.root, extensions, recursive, limit)
        self._warned: set[int] = set()

    def __len__(self) -> int:
        return len(self.files)

    def _load(self, path: Path) -> Tensor:  # pragma: no cover — abstract
        raise NotImplementedError

    def __getitem__(self, index: int) -> dict[str, Any]:
        if not 0 <= index < len(self.files):
            raise IndexError(index)
        for attempt in range(min(MAX_CONSECUTIVE_FAILURES, len(self.files))):
            i = (index + attempt) % len(self.files)
            path = self.files[i]
            try:
                x = self._load(path)
            except Exception as exc:  # noqa: BLE001 — corrupted file: warn and use the next one
                if i not in self._warned:
                    self._warned.add(i)
                    warnings.warn(f"{self.kind} illisible ignoré : {path} ({type(exc).__name__}: "
                                  f"{exc})", RuntimeWarning, stacklevel=2)
                continue
            return {"x": x, "id": path.relative_to(self.root).as_posix()}
        raise RuntimeError(f"{MAX_CONSECUTIVE_FAILURES} fichiers consécutifs illisibles à partir "
                           f"de {self.files[index]} : vérifiez le dossier {self.root}")


class ImageFolderParams(_Params):
    root: str = Field(min_length=1, description="Dossier (ou « @nom » du catalogue)")
    extensions: list[str] = Field(default_factory=lambda: list(IMAGE_EXTENSIONS))
    recursive: bool = True
    limit: int | None = Field(None, gt=0)


@DATASETS.register("image_folder", modality="image", params=ImageFolderParams)
class ImageFolderDataset(_FolderDataset):
    """Images d'un dossier (RGB, [3,H,W] dans [0,1] ; tailles variables → prétraitement resize)."""

    kind = "image"

    def __init__(self, root: str, extensions: list[str] | None = None, recursive: bool = True,
                 limit: int | None = None) -> None:
        super().__init__(root, extensions or IMAGE_EXTENSIONS, recursive, limit)

    def _load(self, path: Path) -> Tensor:
        from PIL import Image, ImageOps

        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            arr = np.asarray(img, dtype=np.uint8)
        return torch.from_numpy(arr.copy()).permute(2, 0, 1).float().div(255.0)


def _av():  # noqa: ANN202
    try:
        import av
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PyAV (paquet 'av') est requis pour lire l'audio et la vidéo") from exc
    return av


def decode_audio(path: str | Path, sample_rate: int, clip_samples: int | None = None,
                 start_seconds: float | None = None) -> Tensor:
    """Decode an audio file to mono ``[1, T]`` at ``sample_rate`` (optionally one clip).

    With ``clip_samples``, the clip starts at ``start_seconds`` (default: centred in the file);
    shorter files are zero-padded.
    """
    av = _av()
    with av.open(str(path)) as container:
        if not container.streams.audio:
            raise ValueError("aucune piste audio")
        stream = container.streams.audio[0]
        duration = None
        if stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
        elif container.duration is not None:
            duration = container.duration / 1_000_000
        start = 0
        if clip_samples is not None:
            if start_seconds is None and duration is not None:
                start_seconds = max(0.0, duration / 2 - clip_samples / sample_rate / 2)
            start = int(round((start_seconds or 0.0) * sample_rate))
        stop = start + clip_samples if clip_samples is not None else None
        resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
        chunks: list[np.ndarray] = []
        total = 0
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                arr = out.to_ndarray()[0]
                chunks.append(arr)
                total += arr.shape[0]
            if stop is not None and total >= stop:
                break
        else:
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray()[0])
    signal = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    if clip_samples is not None:
        signal = signal[start:stop]
        if signal.shape[0] < clip_samples:
            signal = np.pad(signal, (0, clip_samples - signal.shape[0]))
    elif signal.shape[0] == 0:
        raise ValueError("fichier audio vide")
    return torch.from_numpy(signal.astype(np.float32)).clamp(-1.0, 1.0).unsqueeze(0)


class AudioFolderParams(_Params):
    root: str = Field(min_length=1, description="Dossier (ou « @nom » du catalogue)")
    sample_rate: int = Field(16000, ge=1000, description="Fréquence cible (Hz)")
    clip_seconds: float | None = Field(1.0, gt=0, description="Durée des extraits (centrés) ; "
                                                              "null = fichier entier")
    limit: int | None = Field(None, gt=0)
    extensions: list[str] = Field(default_factory=lambda: list(AUDIO_EXTENSIONS))
    recursive: bool = True


@DATASETS.register("audio_folder", modality="audio", params=AudioFolderParams)
class AudioFolderDataset(_FolderDataset):
    """Fichiers audio d'un dossier (PyAV : décodage, mono, rééchantillonnage), [1,T] dans [-1,1]."""

    kind = "fichier audio"

    def __init__(self, root: str, sample_rate: int = 16000, clip_seconds: float | None = 1.0,
                 limit: int | None = None, extensions: list[str] | None = None,
                 recursive: bool = True) -> None:
        super().__init__(root, extensions or AUDIO_EXTENSIONS, recursive, limit)
        self.sample_rate = int(sample_rate)
        self.clip_samples = None if clip_seconds is None else max(1, round(clip_seconds
                                                                           * sample_rate))

    def _load(self, path: Path) -> Tensor:
        return decode_audio(path, self.sample_rate, self.clip_samples)


def decode_video(path: str | Path, frames: int, size: int) -> Tensor:
    """Decode the first ``frames`` frames, short side resized to ``size`` then centre-cropped.

    Returns ``[frames, 3, size, size]`` in [0,1]; short videos repeat their last frame.
    """
    av = _av()
    out: list[np.ndarray] = []
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError("aucune piste vidéo")
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for frame in container.decode(stream):
            w, h = frame.width, frame.height
            scale = size / min(w, h)
            new_w, new_h = max(size, round(w * scale)), max(size, round(h * scale))
            arr = frame.reformat(width=new_w, height=new_h, format="rgb24").to_ndarray()
            top, left = (new_h - size) // 2, (new_w - size) // 2
            out.append(arr[top:top + size, left:left + size])
            if len(out) >= frames:
                break
    if not out:
        raise ValueError("aucune frame décodée")
    while len(out) < frames:
        out.append(out[-1])
    video = torch.from_numpy(np.stack(out)).permute(0, 3, 1, 2).float().div(255.0)
    return video.contiguous()


class VideoFolderParams(_Params):
    root: str = Field(min_length=1, description="Dossier (ou « @nom » du catalogue)")
    frames: int = Field(16, ge=1, le=1024, description="Frames lues au début de chaque vidéo")
    size: int = Field(128, ge=8, le=4096, description="Côté des frames après recadrage central")
    limit: int | None = Field(None, gt=0)
    extensions: list[str] = Field(default_factory=lambda: list(VIDEO_EXTENSIONS))
    recursive: bool = True


@DATASETS.register("video_folder", modality="video", params=VideoFolderParams)
class VideoFolderDataset(_FolderDataset):
    """Vidéos d'un dossier (PyAV), [F,3,size,size] dans [0,1] (redimensionnées + recadrées)."""

    kind = "vidéo"

    def __init__(self, root: str, frames: int = 16, size: int = 128, limit: int | None = None,
                 extensions: list[str] | None = None, recursive: bool = True) -> None:
        super().__init__(root, extensions or VIDEO_EXTENSIONS, recursive, limit)
        self.frames, self.size = int(frames), int(size)

    def _load(self, path: Path) -> Tensor:
        return decode_video(path, self.frames, self.size)

