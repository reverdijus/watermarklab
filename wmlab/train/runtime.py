"""Runtime helpers shared by the engine: device, precision, seeds, loaders, checkpoints."""

from __future__ import annotations

import contextlib
import json
import pickle
import random
from pathlib import Path
from typing import Any, Callable, ContextManager, Iterator

import numpy as np
import torch


# -- device & precision ---------------------------------------------------------------------
def _mps_available() -> bool:
    try:
        return bool(torch.backends.mps.is_available())
    except Exception:
        return False


def resolve_device(requested: str | None = None) -> torch.device:
    """Resolve ``auto`` (cuda > mps > cpu) or validate an explicit device name."""
    name = (requested or "auto").strip().lower()
    if name == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        if _mps_available():
            return torch.device("mps")
        return torch.device("cpu")
    if name.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(f"device '{name}' demandé mais CUDA est indisponible sur cette machine "
                           "(utilisez --device auto, mps ou cpu)")
    if name == "mps" and not _mps_available():
        raise RuntimeError("device 'mps' demandé mais Metal (MPS) est indisponible sur cette "
                           "machine (utilisez --device auto ou cpu)")
    try:
        return torch.device(name)
    except RuntimeError:
        raise RuntimeError(f"device invalide '{requested}' — valeurs possibles : auto, cpu, mps, "
                           "cuda, cuda:<index>") from None


def resolve_precision(precision: str, device: torch.device) -> tuple[torch.dtype | None, str | None]:
    """Return ``(autocast dtype or None, warning)`` for a precision setting.

    ``auto`` enables bf16 autocast on CUDA only; explicit ``bf16``/``fp16`` are honoured when
    the device supports them, otherwise training falls back to fp32 with a warning.
    """
    precision = (precision or "auto").lower()
    if precision == "fp32":
        return None, None
    if precision == "auto":
        return (torch.bfloat16 if device.type == "cuda" else None), None
    dtype = torch.bfloat16 if precision == "bf16" else torch.float16
    if precision == "fp16" and device.type == "cpu":
        return None, "précision fp16 non supportée sur CPU : entraînement en fp32"
    try:
        with torch.autocast(device_type=device.type, dtype=dtype):
            pass
    except Exception as exc:  # unsupported device/dtype combination
        return None, f"précision {precision} indisponible sur {device.type} ({exc}) : fp32"
    return dtype, None


def autocast(device: torch.device, dtype: torch.dtype | None) -> ContextManager[Any]:
    """Autocast context (no-op when ``dtype`` is None)."""
    if dtype is None:
        return contextlib.nullcontext()
    return torch.autocast(device_type=device.type, dtype=dtype)


def fork_rng(device: torch.device) -> ContextManager[Any]:
    """Fork the CPU (and ``device``) RNG state: draws inside do not disturb the outside."""
    if device.type in ("cuda", "mps"):
        return torch.random.fork_rng(devices=[device.index or 0], device_type=device.type)
    return torch.random.fork_rng(devices=[])


def make_grad_scaler(device: torch.device, dtype: torch.dtype | None) -> Any:
    """``torch.amp.GradScaler`` for fp16 autocast (None for fp32/bf16, which need no scaling).

    Without it, the small gradients of an imperceptible residual underflow in float16.
    """
    if dtype != torch.float16:
        return None
    try:
        return torch.amp.GradScaler(device.type)
    except Exception as exc:  # pragma: no cover - depends on the torch build
        raise RuntimeError(f"précision fp16 : GradScaler indisponible sur {device.type} ({exc}) "
                           "— utilisez precision: bf16 ou fp32") from None


def seed_everything(seed: int) -> None:
    """Seed Python, NumPy and torch RNGs."""
    random.seed(seed)
    np.random.seed(int(seed) % (2**32))
    torch.manual_seed(int(seed))


# -- data -----------------------------------------------------------------------------------
def make_loader(split: Any, modality: str, *, batch_size: int, shuffle: bool,
                num_workers: int = 0, drop_last: bool = False, seed: int = 0) -> Any:
    """Build the dataset of a split and its loader (``wmlab.data.make_loader``).

    The dataset is available as ``loader.dataset``.
    """
    from wmlab.data import make_loader as _make_loader

    return _make_loader(split, modality, batch_size=int(batch_size), num_workers=int(num_workers),
                        shuffle=bool(shuffle), seed=int(seed), drop_last=bool(drop_last))


def limited_split(split: Any, limit: int | None) -> Any:
    """Copy of a ``DataSplitSpec`` whose ``limit`` is at most ``limit``."""
    if not limit:
        return split
    current = split.limit
    return split.model_copy(update={"limit": min(int(limit), current) if current else int(limit)})


def infinite_batches(loader: Any, what: str = "entraînement") -> Iterator[Any]:
    """Iterate over ``loader`` forever (a new epoch starts when it is exhausted)."""
    while True:
        produced = False
        for batch in loader:
            produced = True
            yield batch
        if not produced:
            raise RuntimeError(f"le dataset d'{what} ne produit aucun batch (dataset vide, ou "
                               "moins d'items que batch_size avec drop_last)")


def split_key(split: Any) -> str:
    """Canonical JSON of a data split spec (pydantic model or dict)."""
    data = split.model_dump(mode="json") if hasattr(split, "model_dump") else split
    return json.dumps(data, sort_keys=True, default=str)


# -- checkpoints ----------------------------------------------------------------------------
def to_cpu(obj: Any) -> Any:
    """Recursively move tensors to CPU (portable checkpoints)."""
    if isinstance(obj, torch.Tensor):
        return obj.detach().cpu()
    if isinstance(obj, dict):
        return {k: to_cpu(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_cpu(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(to_cpu(v) for v in obj)
    return obj


def load_checkpoint_payload(path: str | Path) -> dict[str, Any]:
    """Load a wmlab checkpoint dict on CPU with the safe loader only (``weights_only=True``).

    Checkpoints written by this tool contain tensors and plain data; a file that needs full
    unpickling is refused (it could execute code, e.g. a checkpoint altered on a remote host).
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"fichier de checkpoint introuvable : {path} (checkpoint distant "
                                "non rapatrié ?)")
    try:
        payload = torch.load(path, map_location="cpu", weights_only=True)
    except (pickle.UnpicklingError, RuntimeError, AttributeError, TypeError) as exc:
        raise ValueError(f"checkpoint {path.name} non chargeable en mode sûr (weights_only) : "
                         f"{str(exc).splitlines()[0][:200]}") from None
    if not isinstance(payload, dict) or "model" not in payload:
        raise ValueError(f"checkpoint invalide (clé 'model' absente) : {path}")
    return payload


# -- misc -----------------------------------------------------------------------------------
def call_quality_metric(fn: Callable[..., Any], x: torch.Tensor, x_w: torch.Tensor,
                        ctx: dict[str, Any]) -> torch.Tensor:
    """Call a quality metric ``(x, x_w, **ctx) -> Tensor[B]`` and return a float tensor [B]."""
    value = fn(x, x_w, **ctx)
    value = torch.as_tensor(value).detach().float().cpu()
    if value.ndim == 0:
        value = value.expand(x.shape[0])
    return value.reshape(-1)
