"""Datasets, preprocessing and loaders (ARCHITECTURE.md §4.3).

Public API: :func:`resolve_path`, :func:`build_dataset`, :func:`dataset_version`,
:func:`collate`, :func:`make_loader`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import random
import sys
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import torch
from torch import Tensor
from torch.utils.data import DataLoader, Dataset

from wmlab.data import datasets, preprocess  # noqa: F401 — registration
from wmlab.registry import DATASETS, PREPROCESS

__all__ = [
    "SplitDataset",
    "build_dataset",
    "collate",
    "dataset_version",
    "make_loader",
    "resolve_path",
]


class DatasetPathError(ValueError):
    """An ``@name`` dataset reference cannot be resolved."""


def resolve_path(value: str) -> str:
    """Resolve ``@name[/sub/path]`` through ``datasets/<name>.yaml``; other values unchanged.

    The catalog path is ``paths[WMLAB_HOST]`` or else ``paths["default"]``, with ``~`` expanded.
    """
    if not isinstance(value, str) or not value.startswith("@"):
        return value
    from wmlab.paths import current_host, workspace_root
    from wmlab.spec import SpecError, load_dataset_entry

    name, _, rest = value[1:].partition("/")
    if not name:
        raise DatasetPathError(f"référence de dataset invalide : {value!r} (attendu « @nom »)")
    try:
        entry = load_dataset_entry(name)
    except SpecError as exc:
        raise DatasetPathError(f"dataset '@{name}' : {exc}") from None
    except FileNotFoundError as exc:
        raise DatasetPathError(f"dataset '@{name}' : {exc}") from None
    host = current_host()
    base = entry.paths.get(host) or entry.paths.get("default")
    if not base:
        try:
            where = f" ({workspace_root() / 'datasets' / (name + '.yaml')})"
        except Exception:  # noqa: BLE001
            where = ""
        raise DatasetPathError(
            f"dataset '@{name}' : aucun chemin pour l'hôte '{host}' ni 'default' dans le "
            f"catalogue{where} — hôtes définis : {', '.join(entry.paths)}"
        )
    path = Path(base).expanduser()
    if rest:
        path = path / rest
    return str(path)


def _split_dict(split_spec: Any) -> dict[str, Any]:
    """Normalize a ``DataSplitSpec`` (pydantic or dict) to a validated plain dict."""
    from wmlab.spec import DataSplitSpec

    if isinstance(split_spec, DataSplitSpec):
        return split_spec.model_dump()
    if isinstance(split_spec, Mapping):
        return DataSplitSpec.model_validate(dict(split_spec)).model_dump()
    if hasattr(split_spec, "model_dump"):
        return DataSplitSpec.model_validate(split_spec.model_dump()).model_dump()
    raise TypeError(f"split de données invalide : {type(split_spec).__name__} "
                    "(DataSplitSpec ou dict attendu)")


def dataset_version(split_spec: Any) -> str:
    """12 hex chars of sha256 of the canonical JSON ``{dataset, params, preprocess, limit}``.

    ``@name`` paths are hashed unresolved, so the version is identical on every host.
    """
    split = _split_dict(split_spec)
    canonical = {
        "dataset": split["dataset"],
        "params": split.get("params") or {},
        "preprocess": [{"name": op["name"], "params": op.get("params") or {}}
                       for op in split.get("preprocess") or []],
        "limit": split.get("limit"),
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class Compose:
    """Apply preprocessing ops in order (picklable)."""

    def __init__(self, ops: list[Callable[[Tensor], Tensor]]) -> None:
        self.ops = list(ops)

    def __call__(self, x: Tensor) -> Tensor:
        for op in self.ops:
            x = op(x)
        return x

    @property
    def deterministic(self) -> bool:
        return all(getattr(op, "deterministic", False) for op in self.ops)


class SplitDataset(Dataset):
    """A built dataset with its preprocessing chain and optional ``limit``."""

    def __init__(self, base: Dataset, transform: Compose, limit: int | None = None,
                 name: str = "") -> None:
        self.base = base
        self.transform = transform
        self.name = name
        size = len(base)  # type: ignore[arg-type]
        self.length = min(size, limit) if limit else size

    def __len__(self) -> int:
        return self.length

    def __getitem__(self, index: int) -> dict[str, Any]:
        if not 0 <= index < self.length:
            raise IndexError(index)
        item = self.base[index]
        if not isinstance(item, dict) or "x" not in item:
            raise TypeError(f"le dataset '{self.name}' doit renvoyer un dict {{'x', 'id'}}, "
                            f"reçu {type(item).__name__}")
        x = item["x"]
        if not isinstance(x, Tensor):
            x = torch.as_tensor(x)
        out = dict(item)
        out["x"] = self.transform(x.float())
        out["id"] = str(item.get("id", index))
        return out


def _accepts_param(entry: Any, name: str) -> bool:
    """True if a registered component accepts the keyword ``name``."""
    if entry.params_model is not None:
        return name in entry.params_model.model_fields
    try:
        return name in inspect.signature(entry.obj).parameters
    except (TypeError, ValueError):
        return False


def build_dataset(split_spec: Any, modality: str) -> SplitDataset:
    """Build the dataset of a split and apply its ``preprocess`` list in order."""
    split = _split_dict(split_spec)
    name = split["dataset"]
    if not DATASETS.has(name):
        from wmlab.registry import load_plugins

        load_plugins()
    entry = DATASETS.get(name)
    if not entry.supports(modality):
        raise ValueError(f"le dataset '{name}' ne supporte pas la modalité {modality} "
                         f"(supporte : {entry.modality_label()})")
    params = dict(split.get("params") or {})
    if _accepts_param(entry, "modality"):
        declared = params.setdefault("modality", modality)
        if declared != modality:
            raise ValueError(f"dataset '{name}' : modalité '{declared}' différente de celle du "
                             f"projet ({modality})")
    base = DATASETS.build(name, **params)
    ops = []
    for op in split.get("preprocess") or []:
        op_entry = PREPROCESS.get(op["name"])
        if not op_entry.supports(modality):
            raise ValueError(f"le prétraitement '{op['name']}' ne supporte pas la modalité "
                             f"{modality} (supporte : {op_entry.modality_label()})")
        ops.append(PREPROCESS.build(op["name"], **(op.get("params") or {})))
    return SplitDataset(base, Compose(ops), split.get("limit"), name=name)


def collate(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Stack ``x`` into a batch and gather ``id`` (and any other keys) into lists."""
    if not items:
        raise ValueError("lot vide")
    shapes = {tuple(item["x"].shape) for item in items}
    if len(shapes) > 1:
        raise ValueError(
            "les items d'un lot n'ont pas la même forme "
            f"({', '.join(str(s) for s in sorted(shapes)[:4])}) : ajoutez un prétraitement "
            "(resize + center_crop, random_crop, random_clip, temporal_crop…)"
        )
    batch: dict[str, Any] = {"x": torch.stack([item["x"] for item in items]),
                             "id": [item["id"] for item in items]}
    for key in items[0]:
        if key not in batch:
            batch[key] = [item.get(key) for item in items]
    return batch


def _seed_worker(worker_id: int) -> None:
    """Seed Python's ``random`` from torch's per-worker seed (numpy is not used by the ops)."""
    random.seed(torch.initial_seed() % 2**32)


def _fast_worker_shutdown() -> None:
    """Shorten torch's worker status-check interval on macOS (default 5 s).

    Each DataLoader worker spawns a ``torch_shm_manager`` that inherits the worker's exit
    sentinel, so ``Process.join(timeout=5)`` waits the full 5 s per worker at shutdown even
    though the worker already exited: 20 s lost at the end of every run (train + val loaders,
    2 workers each) and between HPO trials. The parent only uses the constant for these joins
    and for polling the data queue, so 1 s is safe.
    """
    if sys.platform != "darwin":
        return
    try:
        from torch.utils.data import _utils

        if float(getattr(_utils, "MP_STATUS_CHECK_INTERVAL", 0.0)) > 1.0:
            _utils.MP_STATUS_CHECK_INTERVAL = 1.0
    except Exception:  # private torch detail: never fail because of it
        pass


def shutdown_loader(loader: Any) -> None:
    """Stop the worker processes of a ``DataLoader`` now (instead of at garbage collection)."""
    iterator = getattr(loader, "_iterator", None)
    shutdown = getattr(iterator, "_shutdown_workers", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:
            pass
    if iterator is not None:
        try:
            loader._iterator = None
        except Exception:
            pass


def make_loader(split_spec: Any, modality: str, batch_size: int = 32, num_workers: int = 0,
                shuffle: bool = True, seed: int = 0, drop_last: bool = True) -> DataLoader:
    """Build the split's dataset and wrap it in a seeded ``DataLoader`` using :func:`collate`."""
    if num_workers > 0:
        _fast_worker_shutdown()
    dataset = build_dataset(split_spec, modality)
    if len(dataset) == 0:
        raise ValueError(f"le dataset '{dataset.name}' est vide")
    if drop_last and len(dataset) < batch_size:
        warnings.warn(f"dataset '{dataset.name}' : {len(dataset)} items < batch_size "
                      f"{batch_size} — le dernier lot incomplet est conservé", RuntimeWarning,
                      stacklevel=2)
        drop_last = False
    generator = torch.Generator()
    generator.manual_seed(int(seed))
    return DataLoader(
        dataset,
        batch_size=int(batch_size),
        shuffle=shuffle,
        num_workers=int(num_workers),
        collate_fn=collate,
        drop_last=drop_last,
        generator=generator,
        worker_init_fn=_seed_worker if num_workers > 0 else None,
        persistent_workers=num_workers > 0,
    )
