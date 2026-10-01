"""Builtin watermarking models (ARCHITECTURE.md §4.1) and checkpoint loading."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import torch

from wmlab.models import audio_conv, hidden, video_framewise  # noqa: F401 — registration
from wmlab.models.base import MODALITY_RANGES, WatermarkModel, clamp_to_modality
from wmlab.models.layers import configure_cpu_backends
from wmlab.registry import MODELS

configure_cpu_backends()

CHECKPOINT_FORMAT = "wmlab-ckpt-v1"
_COMPILED_PREFIX = "_orig_mod."

__all__ = [
    "CHECKPOINT_FORMAT",
    "MODALITY_RANGES",
    "WatermarkModel",
    "build_model",
    "clamp_to_modality",
    "external_weights_of",
    "load_checkpoint_dict",
    "load_checkpoint_weights",
    "load_model_from_checkpoint",
]


def build_model(name: str, nbits: int, params: dict[str, Any] | None = None) -> WatermarkModel:
    """Build a registered model (loads ``lab`` plugins when ``name`` is unknown)."""
    if not MODELS.has(name):
        from wmlab.registry import load_plugins

        load_plugins()
    return MODELS.build(name, nbits=nbits, **(params or {}))


def load_checkpoint_dict(path: str | Path, map_location: str | torch.device = "cpu"
                         ) -> dict[str, Any]:
    """Load a ``wmlab-ckpt-v1`` checkpoint dict (safe ``weights_only`` load first)."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"checkpoint introuvable : {path}")
    try:
        # wmlab checkpoints only hold tensors and plain data: the safe loader is enough, and
        # there is no fallback to ``weights_only=False`` (a file brought back from a remote
        # host must never be able to execute code on this machine)
        state = torch.load(path, map_location=map_location, weights_only=True)
    except (pickle.UnpicklingError, RuntimeError, AttributeError, TypeError) as exc:
        raise ValueError(f"{path.name} n'est pas un checkpoint Watermark Lab chargeable en mode "
                         f"sûr (weights_only) : {str(exc).splitlines()[0][:200]}") from None
    if not isinstance(state, dict) or "model" not in state:
        raise ValueError(f"{path.name} n'est pas un checkpoint Watermark Lab (clé 'model' absente)")
    fmt = state.get("format", CHECKPOINT_FORMAT)
    if fmt != CHECKPOINT_FORMAT:
        raise ValueError(f"format de checkpoint non supporté : {fmt!r} (attendu {CHECKPOINT_FORMAT})")
    return state


def _clean_state_dict(state_dict: dict[str, Any]) -> dict[str, Any]:
    """Strip the ``torch.compile`` prefix if present."""
    if state_dict and all(k.startswith(_COMPILED_PREFIX) for k in state_dict):
        return {k[len(_COMPILED_PREFIX):]: v for k, v in state_dict.items()}
    return state_dict


def external_weights_of(model: Any) -> list[dict[str, Any]]:
    """Official weights files loaded by a pretrained wrapper (``[]`` for other models)."""
    fn = getattr(model, "external_weights", None)
    return list(fn() or []) if callable(fn) else []


def load_checkpoint_weights(model: Any, state: dict[str, Any]) -> None:
    """Load the weights of a checkpoint dict into ``model``.

    A *reference* checkpoint (evaluation-only run of a pretrained wrapper: empty ``model``,
    ``external_weights`` listed) keeps the official weights loaded by the constructor, after
    checking that the local files are the ones the run used (sha256).
    """
    if not state["model"] and state.get("external_weights"):
        expected = {w["name"]: w.get("sha256") for w in state["external_weights"]}
        found = {w["name"]: w.get("sha256") for w in external_weights_of(model)}
        for name, digest in expected.items():
            if found.get(name) != digest:
                raise RuntimeError(
                    f"poids officiels '{name}' différents de ceux du run (sha256 "
                    f"{found.get(name) or 'absent'} au lieu de {digest}) : résultats non "
                    "reproductibles avec ces fichiers")
        return
    model.load_state_dict(_clean_state_dict(state["model"]))


def load_model_from_checkpoint(path: str | Path, device: str | torch.device = "cpu"
                               ) -> WatermarkModel:
    """Rebuild the model of a checkpoint (§6.2), load its weights, move it to ``device`` (eval)."""
    state = load_checkpoint_dict(path, map_location="cpu")
    missing = [k for k in ("model_name", "nbits") if k not in state]
    if missing:
        raise ValueError(f"checkpoint incomplet ({Path(path).name}) : clés manquantes "
                         f"{', '.join(missing)}")
    model = build_model(state["model_name"], int(state["nbits"]), state.get("model_params") or {})
    try:
        load_checkpoint_weights(model, state)
    except RuntimeError as exc:
        raise RuntimeError(
            f"les poids de {Path(path).name} ne correspondent pas au modèle "
            f"'{state['model_name']}' reconstruit avec ses paramètres : {exc}"
        ) from exc
    return model.to(device).eval()
