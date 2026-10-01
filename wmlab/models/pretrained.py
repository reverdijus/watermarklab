"""Base of the wrappers around official pretrained weights (baselines, ARCHITECTURE.md §4.1).

A wrapper loads the official weights in its constructor (:mod:`wmlab.weights`) and adapts the
official model to the lab contract: image ``[B,3,H,W]`` in ``[0,1]`` at any resolution, a
``{0,1}`` message of the project's ``nbits``, logits ``[B,nbits]``.

Payload: the official weights carry ``native_nbits`` bits. A project may use fewer: its bits
are written at the first free positions and the remaining positions receive a fixed filler
(pseudo-random, seeded, identical for every image) that is ignored at extraction.
``fixed_bits`` forces positions to a value and removes them from the payload (e.g. the UUID
version / variant bits frozen in InvisMark's training messages).

Evaluation-only wrappers (``trainable = False``) must be used with ``phases: []``; the
checkpoint of such a run is a *reference* (no copied weights, sha256 of the official file).
"""

from __future__ import annotations

import pickle
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

import torch
import torch.nn.functional as F
from torch import Tensor

from wmlab.models.base import WatermarkModel


def load_official_checkpoint(path: Path, *, safe_globals: Sequence[Any] = (),
                             mmap: bool = False) -> Any:
    """``torch.load`` of an official file in safe mode (``weights_only``): only tensors, plain
    containers and the explicitly allowed ``safe_globals`` are unpickled (no code execution)."""
    try:
        with torch.serialization.safe_globals(list(safe_globals)):
            return torch.load(path, map_location="cpu", weights_only=True, mmap=mmap)
    except pickle.UnpicklingError as exc:
        detail = next((line.strip() for line in str(exc).splitlines()
                       if "Unsupported global" in line or "got <class" in line), "")
        raise ValueError(f"{path.name} : chargement sûr (weights_only) impossible {detail}"
                         ) from None


class PretrainedWatermarkModel(WatermarkModel):
    """Official pretrained model adapted to the lab contract."""

    native_nbits: ClassVar[int] = 0
    trainable: ClassVar[bool] = False  # False: evaluation only (phases: []), always in eval mode

    def __init__(self, nbits: int, *, native_nbits: int | None = None,
                 fixed_bits: dict[int, int] | None = None, filler_seed: int = 0) -> None:
        super().__init__(nbits)
        native = int(native_nbits or self.native_nbits)
        fixed = {int(k): int(v) for k, v in (fixed_bits or {}).items()}
        free = [i for i in range(native) if i not in fixed]
        if self.nbits > len(free):
            detail = f" dont {len(fixed)} figé(s)" if fixed else ""
            raise ValueError(
                f"{type(self).__name__} : les poids officiels portent {native} bits{detail}, "
                f"soit {len(free)} bits utiles — le projet en demande {self.nbits} "
                f"(message_bits ≤ {len(free)})")
        generator = torch.Generator().manual_seed(int(filler_seed))
        filler = torch.randint(0, 2, (native,), generator=generator).float()
        for index, value in fixed.items():
            filler[index] = float(value)
        self.native = native
        self.register_buffer("payload_index", torch.tensor(free[:self.nbits]), persistent=False)
        self.register_buffer("filler", filler, persistent=False)
        self._weights_files: list[tuple[str, Path]] = []
        self._weights_info: list[dict[str, Any]] | None = None

    # -- payload -------------------------------------------------------------------------
    def to_native(self, msg: Tensor) -> Tensor:
        """``[B, nbits]`` project message → ``[B, native]`` official message."""
        filler = self.filler.to(device=msg.device, dtype=msg.dtype)
        out = filler.expand(msg.shape[0], -1).clone()
        return out.index_copy(1, self.payload_index.to(msg.device), msg)

    def from_native(self, logits: Tensor) -> Tensor:
        """``[B, native]`` official logits → ``[B, nbits]`` project logits."""
        return logits.index_select(1, self.payload_index.to(logits.device))

    def train(self, mode: bool = True) -> "PretrainedWatermarkModel":
        """Frozen baselines stay in eval mode (BatchNorm statistics of the official weights)."""
        return super().train(mode and self.trainable)

    # -- official weights ----------------------------------------------------------------
    def load_official(self, state: Mapping[str, Tensor]) -> None:
        """Load official weights; every parameter and buffer of the wrapper must be provided
        and no key may be left over (callers select and rename the relevant keys)."""
        missing, unexpected = self.load_state_dict(dict(state), strict=False)
        if missing or unexpected:
            raise RuntimeError(
                f"{type(self).__name__} : poids officiels incompatibles avec l'architecture — "
                f"{len(missing)} clé(s) manquante(s) {missing[:4]}, {len(unexpected)} "
                f"inattendue(s) {unexpected[:4]}")

    def weights_file(self, name: str, *, download: bool = False) -> Path:
        """Local path of a declared weights file (downloaded if absent and ``download``)."""
        from wmlab.weights import require_weights

        path = require_weights(name, download=download)
        self._weights_files.append((name, path))
        self._weights_info = None
        return path

    def external_weights(self) -> list[dict[str, Any]]:
        """Traceability of the loaded official files (name, url, license, sha256)."""
        from wmlab.weights import weights_info

        if self._weights_info is None:
            self._weights_info = [weights_info(name, path) for name, path in self._weights_files]
        return list(self._weights_info)


# -- image helpers ---------------------------------------------------------------------------
def resize(x: Tensor, size: int | tuple[int, int]) -> Tensor:
    """Bilinear resize of ``[B,C,H,W]`` (antialiased when downscaling, differentiable)."""
    hw = (size, size) if isinstance(size, int) else tuple(size)
    if tuple(x.shape[-2:]) == hw:
        return x
    down = hw[0] < x.shape[-2] or hw[1] < x.shape[-1]
    try:
        return F.interpolate(x, size=hw, mode="bilinear", align_corners=False, antialias=down)
    except (NotImplementedError, RuntimeError):  # antialias unsupported on this device
        return F.interpolate(x, size=hw, mode="bilinear", align_corners=False)


def embed_at_resolution(x: Tensor, size: int | tuple[int, int],
                        embed_small: Any) -> Tensor:
    """Watermark ``x`` through a fixed-resolution embedder: the image is resized to ``size``,
    watermarked by ``embed_small``, and the residual is upscaled back and added to ``x``
    (the full-resolution content is kept). Returns an unclamped tensor."""
    small = resize(x, size)
    residual = embed_small(small) - small
    return x + resize(residual, tuple(x.shape[-2:]))
