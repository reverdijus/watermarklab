"""Builtin attacks (ARCHITECTURE.md §4.2) and the helpers used by the trainer."""

from wmlab.attacks import audio, generic, image, video  # noqa: F401 — registration
from wmlab.attacks.base import (
    Attack,
    apply_attack,
    clamp_strength,
    configure_attack,
    lerp,
    per_sample_sign,
    per_sample_uniform,
    sample_strength,
    shared_randomness,
)
from wmlab.attacks.codecs import CodecUnavailableError, encoder_available, select_encoder

__all__ = [
    "Attack",
    "CodecUnavailableError",
    "apply_attack",
    "clamp_strength",
    "configure_attack",
    "encoder_available",
    "lerp",
    "per_sample_sign",
    "per_sample_uniform",
    "sample_strength",
    "select_encoder",
    "shared_randomness",
]
