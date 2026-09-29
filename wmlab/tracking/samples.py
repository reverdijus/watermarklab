"""Encoding of samples: image → PNG, audio → WAV PCM16, video → MP4 (H.264) or GIF."""

from __future__ import annotations

import re
import wave
from pathlib import Path
from typing import Any

import numpy as np

DEFAULT_EXT = {"image": "png", "audio": "wav", "video": "mp4"}


def safe_name(text: Any) -> str:
    """Return a filesystem-safe token."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text)).strip("-") or "x"


def to_numpy(data: Any) -> np.ndarray:
    """Convert a torch tensor / array-like to numpy (keeps integer dtypes)."""
    if hasattr(data, "detach"):
        tensor = data.detach().cpu()
        if tensor.is_floating_point():
            tensor = tensor.float()
        return tensor.numpy()
    return np.asarray(data)


def _to_uint8(arr: np.ndarray) -> np.ndarray:
    if arr.dtype == np.uint8:
        return arr
    if np.issubdtype(arr.dtype, np.floating):
        arr = np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
        return (np.clip(arr, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return np.clip(arr, 0, 255).astype(np.uint8)


def image_hwc(data: Any) -> np.ndarray:
    """Return an ``[H, W, C]`` (or ``[H, W]``) uint8 array from ``[C,H,W]``/``[H,W,C]``/``[H,W]``."""
    arr = to_numpy(data)
    if arr.ndim == 4 and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim == 3:
        if arr.shape[0] in (1, 3):  # canonical wmlab layout [3, H, W]
            arr = arr.transpose(1, 2, 0)
        elif arr.shape[-1] not in (1, 3, 4):
            raise ValueError(f"image attendue [3,H,W] ou [H,W,3], reçu la forme {tuple(arr.shape)}")
        if arr.shape[2] == 1:
            arr = arr[:, :, 0]
    elif arr.ndim != 2:
        raise ValueError(f"image attendue [3,H,W] ou [H,W,3], reçu la forme {tuple(arr.shape)}")
    return np.ascontiguousarray(_to_uint8(arr))


def write_image(path: Path, data: Any) -> Path:
    """Write a PNG."""
    from PIL import Image

    Image.fromarray(image_hwc(data)).save(path, format="PNG")
    return path


def write_audio(path: Path, data: Any, sample_rate: int = 16000) -> Path:
    """Write a PCM16 WAV from ``[T]`` or ``[C, T]`` (float in [-1, 1] or int16)."""
    arr = to_numpy(data)
    if arr.ndim == 1:
        arr = arr[None, :]
    if arr.ndim != 2:
        raise ValueError(f"audio attendu [1,T] ou [T], reçu la forme {tuple(arr.shape)}")
    if arr.shape[0] > arr.shape[1]:  # [T, C]
        arr = arr.T
    if arr.dtype != np.int16:
        arr = np.nan_to_num(arr.astype(np.float32), nan=0.0, posinf=1.0, neginf=-1.0)
        arr = (np.clip(arr, -1.0, 1.0) * 32767.0).round().astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(int(arr.shape[0]))
        handle.setsampwidth(2)
        handle.setframerate(int(sample_rate))
        handle.writeframes(np.ascontiguousarray(arr.T).astype("<i2").tobytes())
    return path


def video_frames(data: Any) -> np.ndarray:
    """Return ``[F, H, W, 3]`` uint8 frames from ``[F, 3, H, W]`` or ``[F, H, W, 3]``."""
    arr = to_numpy(data)
    if arr.ndim == 5 and arr.shape[0] == 1:
        arr = arr[0]
    if arr.ndim != 4:
        raise ValueError(f"vidéo attendue [F,3,H,W], reçu la forme {tuple(arr.shape)}")
    if arr.shape[1] in (1, 3) and arr.shape[3] not in (1, 3):
        arr = arr.transpose(0, 2, 3, 1)
    frames = _to_uint8(arr)
    if frames.shape[3] == 1:
        frames = np.repeat(frames, 3, axis=3)
    return np.ascontiguousarray(frames[..., :3])


def _write_mp4(path: Path, frames: np.ndarray, fps: int) -> None:
    import av

    height = frames.shape[1] - frames.shape[1] % 2
    width = frames.shape[2] - frames.shape[2] % 2
    if height < 2 or width < 2:
        raise ValueError("vidéo trop petite pour H.264")
    frames = np.ascontiguousarray(frames[:, :height, :width])
    container = av.open(str(path), mode="w", format="mp4", options={"movflags": "+faststart"})
    try:
        stream = container.add_stream("libx264", rate=int(fps))
        stream.width, stream.height = width, height
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "20"}
        for frame in frames:
            for packet in stream.encode(av.VideoFrame.from_ndarray(frame, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


def _write_gif(path: Path, frames: np.ndarray, fps: int) -> None:
    from PIL import Image

    images = [Image.fromarray(f) for f in frames]
    images[0].save(path, format="GIF", save_all=True, append_images=images[1:],
                   duration=max(int(1000 / max(fps, 1)), 20), loop=0)


def write_video(path_no_ext: Path, data: Any, fps: int = 24) -> Path:
    """Write an MP4 (H.264 via PyAV); fall back to an animated GIF if encoding fails."""
    frames = video_frames(data)
    mp4 = path_no_ext.with_suffix(".mp4")
    try:
        _write_mp4(mp4, frames, fps)
        return mp4
    except Exception:
        mp4.unlink(missing_ok=True)
    gif = path_no_ext.with_suffix(".gif")
    _write_gif(gif, frames, fps)
    return gif


def write_sample(path_no_ext: Path, kind: str, data: Any, *, sample_rate: int | None = None,
                 fps: int | None = None) -> Path:
    """Encode ``data`` according to ``kind`` and return the written file path."""
    if kind not in DEFAULT_EXT:
        raise ValueError(f"type d'échantillon invalide '{kind}' — valeurs possibles : "
                         "image, audio, video")
    path_no_ext.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (bytes, bytearray, memoryview)):
        path = path_no_ext.with_suffix("." + DEFAULT_EXT[kind])
        path.write_bytes(bytes(data))
        return path
    if kind == "image":
        return write_image(path_no_ext.with_suffix(".png"), data)
    if kind == "audio":
        return write_audio(path_no_ext.with_suffix(".wav"), data, sample_rate or 16000)
    return write_video(path_no_ext, data, fps or 24)
