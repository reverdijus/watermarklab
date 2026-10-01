"""Audio attacks (``[B,1,T]`` in [-1,1]).

Frequencies and durations are converted with the ``sample_rate`` param (default 16000); the
engine may also set ``attack.sample_rate`` from ``data.sample_rate`` after building the attack.
"""

from __future__ import annotations

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.attacks.base import Attack, clamp_strength, lerp, per_sample_uniform
from wmlab.attacks.codecs import audio_codec_roundtrip
from wmlab.registry import ATTACKS

MP3_MAX_KBPS = 320
AAC_MAX_KBPS = 256


class _AudioParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_rate: int = Field(16000, ge=1000, le=384000,
                             description="Fréquence d'échantillonnage (Hz)")


def _check_audio(x: Tensor) -> None:
    if x.ndim != 3:
        raise ValueError(f"attaque audio : tenseur [B, 1, T] attendu, reçu {tuple(x.shape)}")


def _num_taps(sample_rate: int) -> int:
    """Odd FIR length (~101 taps at 16 kHz, i.e. a transition band of ≈ 500 Hz)."""
    taps = int(round(101 * sample_rate / 16000))
    return max(31, min(1023, taps)) | 1


def lowpass_kernel(cutoff_hz: float, sample_rate: int, device: torch.device,
                   dtype: torch.dtype) -> Tensor:
    """Hann-windowed sinc low-pass FIR, unit DC gain, shape ``[taps]``."""
    taps = _num_taps(sample_rate)
    fc = min(max(cutoff_hz / sample_rate, 1e-6), 0.5)
    # designed in float64 on the CPU (MPS has no float64), then moved
    n = torch.arange(taps, dtype=torch.float64) - (taps - 1) / 2
    h = 2 * fc * torch.sinc(2 * fc * n) * torch.hann_window(taps, periodic=False,
                                                            dtype=torch.float64)
    return (h / h.sum()).to(device=device, dtype=dtype)


def fir_filter(x: Tensor, kernel: Tensor) -> Tensor:
    """Zero-phase-centred FIR filtering of ``[B,1,T]`` (same length, zero padding)."""
    taps = kernel.numel()
    return F.conv1d(x, kernel.view(1, 1, taps), padding=taps // 2)


def lowpass(x: Tensor, cutoff_hz: float, sample_rate: int) -> Tensor:
    """Low-pass ``x`` at ``cutoff_hz``."""
    return fir_filter(x, lowpass_kernel(cutoff_hz, sample_rate, x.device, x.dtype))


class LowpassParams(_AudioParams):
    cutoff_hz: float = Field(3000.0, gt=0.0, description="Fréquence de coupure (Hz)")


@ATTACKS.register("lowpass", modality="audio", params=LowpassParams)
class Lowpass(Attack):
    """Filtre passe-bas (FIR), coupure interpolée de Nyquist à `cutoff_hz`."""

    modality = "audio"

    def __init__(self, cutoff_hz: float = 3000.0, sample_rate: int = 16000) -> None:
        super().__init__(cutoff_hz=cutoff_hz, sample_rate=sample_rate)
        self.cutoff_hz = float(cutoff_hz)
        self.sample_rate = int(sample_rate)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        _check_audio(x)
        nyquist = self.sample_rate / 2.0
        cutoff = lerp(nyquist, min(self.cutoff_hz, nyquist), s)
        if cutoff >= nyquist * 0.999:
            return x
        return lowpass(x, cutoff, self.sample_rate).clamp(-1.0, 1.0)


class HighpassParams(_AudioParams):
    cutoff_hz: float = Field(500.0, gt=0.0, description="Fréquence de coupure (Hz)")


@ATTACKS.register("highpass", modality="audio", params=HighpassParams)
class Highpass(Attack):
    """Filtre passe-haut (FIR), coupure interpolée de 0 à `cutoff_hz`."""

    modality = "audio"

    def __init__(self, cutoff_hz: float = 500.0, sample_rate: int = 16000) -> None:
        super().__init__(cutoff_hz=cutoff_hz, sample_rate=sample_rate)
        self.cutoff_hz = float(cutoff_hz)
        self.sample_rate = int(sample_rate)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        _check_audio(x)
        cutoff = min(s * self.cutoff_hz, self.sample_rate / 2.0)
        if cutoff < 1.0:
            return x
        kernel = -lowpass_kernel(cutoff, self.sample_rate, x.device, x.dtype)
        kernel[kernel.numel() // 2] += 1.0  # spectral inversion: delta − low-pass
        return fir_filter(x, kernel).clamp(-1.0, 1.0)


class ResampleParams(_AudioParams):
    rate: int = Field(8000, ge=500, description="Fréquence intermédiaire (Hz)")


@ATTACKS.register("resample", modality="audio", params=ResampleParams)
class Resample(Attack):
    """Rééchantillonnage vers `rate` (anti-repliement) puis retour à la fréquence d'origine."""

    modality = "audio"

    def __init__(self, rate: int = 8000, sample_rate: int = 16000) -> None:
        super().__init__(rate=rate, sample_rate=sample_rate)
        self.rate = int(rate)
        self.sample_rate = int(sample_rate)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        _check_audio(x)
        rate = lerp(float(self.sample_rate), float(self.rate), s)
        length = x.shape[-1]
        n_low = max(1, round(length * rate / self.sample_rate))
        if n_low == length:
            return x
        if rate < self.sample_rate:
            x = lowpass(x, 0.5 * rate, self.sample_rate)
        low = F.interpolate(x, size=n_low, mode="linear", align_corners=False)
        out = F.interpolate(low, size=length, mode="linear", align_corners=False)
        if rate < self.sample_rate:
            out = lowpass(out, 0.5 * rate, self.sample_rate)
        return out.clamp(-1.0, 1.0)


class AmplitudeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gain_db: float = Field(-6.0, ge=-60.0, le=24.0, description="Gain (dB)")


@ATTACKS.register("amplitude", modality="audio", params=AmplitudeParams)
class Amplitude(Attack):
    """Gain de `gain_db` dB (écrêté dans [-1, 1])."""

    modality = "audio"

    def __init__(self, gain_db: float = -6.0) -> None:
        super().__init__(gain_db=gain_db)
        self.gain_db = float(gain_db)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        return (x * 10.0 ** (s * self.gain_db / 20.0)).clamp(-1.0, 1.0)


class EchoParams(_AudioParams):
    delay_ms: float = Field(100.0, gt=0.0, le=5000.0, description="Retard de l'écho (ms)")
    decay: float = Field(0.3, ge=0.0, le=1.0, description="Atténuation de l'écho")


@ATTACKS.register("echo", modality="audio", params=EchoParams)
class Echo(Attack):
    """Écho unique (retard `delay_ms`, amplitude `decay`), crête d'origine conservée."""

    modality = "audio"

    def __init__(self, delay_ms: float = 100.0, decay: float = 0.3,
                 sample_rate: int = 16000) -> None:
        super().__init__(delay_ms=delay_ms, decay=decay, sample_rate=sample_rate)
        self.delay_ms = float(delay_ms)
        self.decay = float(decay)
        self.sample_rate = int(sample_rate)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0 or self.decay == 0.0:
            return x
        _check_audio(x)
        length = x.shape[-1]
        delay = min(length, max(1, round(self.delay_ms * self.sample_rate / 1000.0)))
        delayed = F.pad(x, (delay, 0))[..., :length]
        y = x + s * self.decay * delayed
        peak_x = x.abs().amax(dim=-1, keepdim=True)
        peak_y = y.abs().amax(dim=-1, keepdim=True).clamp_min(1e-8)
        y = y * torch.where(peak_y > peak_x, peak_x / peak_y, torch.ones_like(peak_y))
        return y.clamp(-1.0, 1.0)


class TimeShiftParams(_AudioParams):
    max_ms: float = Field(50.0, ge=0.0, le=10000.0, description="Décalage maximal (ms, ±)")


@ATTACKS.register("time_shift", modality="audio", params=TimeShiftParams)
class TimeShift(Attack):
    """Décalage temporel aléatoire dans ±`max_ms` (complété par du silence)."""

    modality = "audio"

    def __init__(self, max_ms: float = 50.0, sample_rate: int = 16000) -> None:
        super().__init__(max_ms=max_ms, sample_rate=sample_rate)
        self.max_ms = float(max_ms)
        self.sample_rate = int(sample_rate)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        _check_audio(x)
        b, c, length = x.shape
        max_shift = min(length, round(s * self.max_ms * self.sample_rate / 1000.0))
        if max_shift == 0:
            return x
        shifts = torch.round(per_sample_uniform(b, x.device, -max_shift - 0.5, max_shift + 0.5))
        index = torch.arange(length, device=x.device)[None, :] - shifts.long()[:, None]
        valid = (index >= 0) & (index < length)
        gathered = torch.gather(x, 2, index.clamp(0, length - 1)[:, None, :].expand(b, c, length))
        return gathered * valid[:, None, :].to(x.dtype)


class CodecParams(_AudioParams):
    bitrate_kbps: int = Field(64, ge=8, le=320, description="Débit à strength=1 (kbit/s)")


@ATTACKS.register("mp3", modality="audio", params=CodecParams)
class Mp3(Attack):
    """Compression MP3 réelle (PyAV/libmp3lame), débit interpolé de 320 kbit/s à `bitrate_kbps`."""

    modality = "audio"
    differentiable = False

    def __init__(self, bitrate_kbps: int = 64, sample_rate: int = 16000) -> None:
        super().__init__(bitrate_kbps=bitrate_kbps, sample_rate=sample_rate)
        self.bitrate_kbps = int(bitrate_kbps)
        self.sample_rate = int(sample_rate)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        _check_audio(x)
        bitrate = round(lerp(MP3_MAX_KBPS, min(self.bitrate_kbps, MP3_MAX_KBPS), s))
        return audio_codec_roundtrip(x, self.sample_rate, "mp3", bitrate)


@ATTACKS.register("aac", modality="audio", params=CodecParams)
class Aac(Attack):
    """Compression AAC réelle (PyAV), débit interpolé de 256 kbit/s à `bitrate_kbps`."""

    modality = "audio"
    differentiable = False

    def __init__(self, bitrate_kbps: int = 64, sample_rate: int = 16000) -> None:
        super().__init__(bitrate_kbps=bitrate_kbps, sample_rate=sample_rate)
        self.bitrate_kbps = int(bitrate_kbps)
        self.sample_rate = int(sample_rate)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)
        if s == 0.0:
            return x
        _check_audio(x)
        bitrate = round(lerp(AAC_MAX_KBPS, min(self.bitrate_kbps, AAC_MAX_KBPS), s))
        return audio_codec_roundtrip(x, self.sample_rate, "aac", bitrate)
