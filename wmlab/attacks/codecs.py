"""Real codecs used by the non differentiable attacks (PIL JPEG, PyAV MP3/AAC/H.264).

Everything happens in memory (``io.BytesIO``). Outputs always have the exact input shape:
codec delays are compensated, lengths padded/truncated, odd video sizes padded then cropped.
Encoders are chosen among those really available in the installed PyAV wheel, with documented
fallbacks (H.264: libx264 → libopenh264 → h264_videotoolbox → mpeg4 ; MP3: libmp3lame → aac).
"""

from __future__ import annotations

import io
import threading
import warnings
from functools import lru_cache

import numpy as np
import torch
import torch.nn.functional as F
from torch import Tensor

H264_ENCODERS: tuple[str, ...] = ("libx264", "libopenh264", "h264_videotoolbox", "mpeg4")
MP3_ENCODERS: tuple[str, ...] = ("libmp3lame",)
AAC_ENCODERS: tuple[str, ...] = ("aac", "libfdk_aac")
AAC_PRIMING_SAMPLES = 1024  # encoder delay of AAC (not trimmed by the ADTS demuxer)
MIN_VIDEO_SIDE = 16

_warned: set[str] = set()
_warn_lock = threading.Lock()


class CodecUnavailableError(RuntimeError):
    """No usable encoder for an attack."""


def warn_once(key: str, message: str) -> None:
    """Emit ``message`` as a RuntimeWarning only once per process."""
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    warnings.warn(message, RuntimeWarning, stacklevel=3)


def _av():  # noqa: ANN202 — lazy import with a clear message
    try:
        import av
    except ImportError as exc:  # pragma: no cover — av is a dependency of the train extra
        raise CodecUnavailableError(
            "PyAV (paquet 'av') est requis pour les attaques mp3/aac/h264 : "
            "installez l'extra « train »."
        ) from exc
    return av


@lru_cache(maxsize=None)
def encoder_available(name: str) -> bool:
    """Return True if PyAV can open the encoder ``name``."""
    try:
        _av().codec.Codec(name, "w")
        return True
    except Exception:  # noqa: BLE001
        return False


def select_encoder(candidates: tuple[str, ...]) -> str | None:
    """Return the first available encoder of ``candidates``."""
    return next((name for name in candidates if encoder_available(name)), None)


# -- image ----------------------------------------------------------------------------------
def to_uint8_images(x: Tensor) -> np.ndarray:
    """``[N,3,H,W]`` float in [0,1] → ``[N,H,W,3]`` uint8 (rounded)."""
    arr = x.detach().to("cpu", torch.float32).clamp(0.0, 1.0).mul(255.0).round()
    return arr.to(torch.uint8).permute(0, 2, 3, 1).contiguous().numpy()


def from_uint8_images(arr: np.ndarray, like: Tensor) -> Tensor:
    """``[N,H,W,3]`` uint8 → float tensor ``[N,3,H,W]`` in [0,1] on ``like``'s device/dtype."""
    out = torch.from_numpy(np.ascontiguousarray(arr)).permute(0, 3, 1, 2).float().div(255.0)
    return out.to(device=like.device, dtype=like.dtype)


def jpeg_roundtrip(x: Tensor, quality: int) -> Tensor:
    """Compress/decompress every image of ``[N,3,H,W]`` with PIL JPEG at ``quality``."""
    from PIL import Image

    quality = int(min(100, max(1, round(quality))))
    images = to_uint8_images(x)
    out = np.empty_like(images)
    for i, img in enumerate(images):
        buf = io.BytesIO()
        Image.fromarray(img, mode="RGB").save(buf, format="JPEG", quality=quality)
        buf.seek(0)
        with Image.open(buf) as decoded:
            out[i] = np.asarray(decoded.convert("RGB"))
    return from_uint8_images(out, x)


# -- audio ----------------------------------------------------------------------------------
def _supported_rate(codec_name: str, sample_rate: int) -> int:
    rates = _av().codec.Codec(codec_name, "w").audio_rates
    if not rates or sample_rate in rates:
        return sample_rate
    higher = sorted(r for r in rates if r >= sample_rate)
    return higher[0] if higher else max(rates)


def _encode_decode_audio_1d(signal: np.ndarray, sample_rate: int, codec_name: str,
                            container: str, bitrate_kbps: int) -> np.ndarray:
    """Encode then decode a mono float32 signal; return the decoded signal at ``sample_rate``."""
    av = _av()
    rate = _supported_rate(codec_name, sample_rate)
    buf = io.BytesIO()
    with av.open(buf, "w", format=container) as out:
        stream = out.add_stream(codec_name, rate=rate, layout="mono")
        stream.bit_rate = int(bitrate_kbps) * 1000
        frame = av.AudioFrame.from_ndarray(signal[None, :].astype(np.float32), format="fltp",
                                           layout="mono")
        frame.sample_rate = sample_rate
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    buf.seek(0)
    chunks: list[np.ndarray] = []
    resampler = av.AudioResampler(format="fltp", layout="mono", rate=sample_rate)
    with av.open(buf, "r") as inp:
        for decoded in inp.decode(audio=0):
            for resampled in resampler.resample(decoded):
                chunks.append(resampled.to_ndarray()[0])
    for resampled in resampler.resample(None):
        chunks.append(resampled.to_ndarray()[0])
    y = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
    if container == "adts":  # the ADTS demuxer does not trim the AAC priming samples
        y = y[int(round(AAC_PRIMING_SAMPLES * sample_rate / rate)):]
    return y


def audio_codec_roundtrip(x: Tensor, sample_rate: int, codec: str, bitrate_kbps: int) -> Tensor:
    """Compress/decompress ``[B,1,T]`` with ``codec`` ("mp3" or "aac"), exact output shape."""
    if codec == "mp3":
        name = select_encoder(MP3_ENCODERS)
        container = "mp3"
        if name is None:
            name = select_encoder(AAC_ENCODERS)
            container = "adts"
            warn_once("mp3", "Encodeur MP3 (libmp3lame) indisponible dans PyAV : l'attaque mp3 "
                             "utilise AAC à la place.")
    else:
        name, container = select_encoder(AAC_ENCODERS), "adts"
    if name is None:
        raise CodecUnavailableError(f"aucun encodeur {codec} disponible dans PyAV")
    length = x.shape[-1]
    signals = x.detach().to("cpu", torch.float32).clamp(-1.0, 1.0).reshape(-1, length).numpy()
    out = np.zeros_like(signals)
    for i, signal in enumerate(signals):
        y = _encode_decode_audio_1d(signal, int(sample_rate), name, container, bitrate_kbps)
        n = min(length, y.shape[0])
        out[i, :n] = y[:n]
    result = torch.from_numpy(out).reshape(x.shape).clamp(-1.0, 1.0)
    return result.to(device=x.device, dtype=x.dtype)


# -- video ----------------------------------------------------------------------------------
def _video_encoder() -> str:
    name = select_encoder(H264_ENCODERS)
    if name is None:
        raise CodecUnavailableError("aucun encodeur vidéo (H.264 ou MPEG-4) disponible dans PyAV")
    if name != "libx264":
        warn_once("h264", f"libx264 indisponible dans PyAV : l'attaque h264 utilise '{name}' "
                          "(qualité pilotée par le débit, approximation du CRF).")
    return name


def _crf_to_bitrate(crf: float, width: int, height: int, fps: int) -> int:
    """Rough bitrate equivalent of an x264 CRF (−6 CRF ≈ ×2 bitrate)."""
    bits_per_pixel = 0.1 * 2.0 ** ((23.0 - crf) / 6.0)
    return max(20_000, int(bits_per_pixel * width * height * fps))


def _encode_decode_video(frames: np.ndarray, encoder: str, crf: float, fps: int,
                         preset: str) -> list[np.ndarray]:
    """Encode ``[F,H,W,3]`` uint8 frames then decode them (H, W even)."""
    av = _av()
    n_frames, height, width, _ = frames.shape
    buf = io.BytesIO()
    with av.open(buf, "w", format="mp4") as out:
        stream = out.add_stream(encoder, rate=fps)
        stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
        if encoder == "libx264":
            stream.options = {"crf": str(int(round(crf))), "preset": preset}
        else:
            stream.bit_rate = _crf_to_bitrate(crf, width, height, fps)
        for i, arr in enumerate(frames):
            frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
            frame.pts = i
            for packet in stream.encode(frame):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    buf.seek(0)
    with av.open(buf, "r") as inp:
        return [f.to_ndarray(format="rgb24") for f in inp.decode(video=0)]


def h264_roundtrip(x: Tensor, crf: float, fps: int = 24, preset: str = "medium") -> Tensor:
    """Compress/decompress every video of ``[B,F,3,H,W]`` (exact output shape)."""
    encoder = _video_encoder()
    b, f, c, h, w = x.shape
    pad_h = max(MIN_VIDEO_SIDE, h + h % 2) - h
    pad_w = max(MIN_VIDEO_SIDE, w + w % 2) - w
    flat = x.detach().to("cpu", torch.float32).reshape(b * f, c, h, w)
    if pad_h or pad_w:
        flat = F.pad(flat, (0, pad_w, 0, pad_h), mode="replicate")
    videos = to_uint8_images(flat).reshape(b, f, h + pad_h, w + pad_w, c)
    out = np.empty_like(videos)
    for i, video in enumerate(videos):
        decoded = _encode_decode_video(video, encoder, crf, int(fps), preset)
        if not decoded:
            raise RuntimeError("le décodage H.264 n'a produit aucune frame")
        for j in range(f):  # missing trailing frames repeat the last decoded one
            out[i, j] = decoded[min(j, len(decoded) - 1)]
    result = from_uint8_images(out.reshape(b * f, h + pad_h, w + pad_w, c), x)
    return result[:, :, :h, :w].reshape(x.shape)
