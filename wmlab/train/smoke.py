"""Smoke mode: tiny synthetic data and a handful of steps (ARCHITECTURE.md §8.1)."""

from __future__ import annotations

import copy
from typing import Any

SMOKE_MAX_STEPS = 6
SMOKE_BATCH_SIZE = 4
SMOKE_EVAL_EVERY = 4
SMOKE_VAL_BATCHES = 2
SMOKE_BENCH_ITEMS = 8
SMOKE_MAX_SAMPLES = 2
SMOKE_TRAIN_ITEMS = 16
SMOKE_VAL_ITEMS = 8


def smoke_split(modality: str, *, num_items: int, seed: int) -> dict[str, Any]:
    """Synthetic split of the given modality (image 64 px, audio 8000 samples, video 4×64 px)."""
    params: dict[str, Any] = {"modality": modality, "num_items": num_items, "seed": seed}
    if modality == "audio":
        params["length"] = 8000
    elif modality == "video":
        params.update(frames=4, size=64)
    else:
        params["size"] = 64
    return {"dataset": "synthetic", "params": params, "preprocess": [], "limit": None}


def apply_smoke(resolved: dict[str, Any], modality: str) -> dict[str, Any]:
    """Return a copy of a resolved experiment dict turned into a smoke test."""
    spec = copy.deepcopy(resolved)
    data = spec.setdefault("data", {})
    data["train"] = smoke_split(modality, num_items=SMOKE_TRAIN_ITEMS, seed=0)
    data["val"] = smoke_split(modality, num_items=SMOKE_VAL_ITEMS, seed=1)
    data["batch_size"] = SMOKE_BATCH_SIZE
    data["num_workers"] = 0
    for phase in spec.get("phases") or []:
        steps = min(int(phase.get("steps") or 1), SMOKE_MAX_STEPS)
        phase["steps"] = steps
        phase["batch_size"] = None
        phase["data"] = None
        lr = phase.get("lr")
        if isinstance(lr, dict) and int(lr.get("warmup_steps") or 0) > steps // 2:
            lr["warmup_steps"] = steps // 2
    evaluation = spec.setdefault("eval", {})
    evaluation["every_steps"] = SMOKE_EVAL_EVERY
    evaluation["val_batches"] = SMOKE_VAL_BATCHES
    samples = spec.setdefault("samples", {})
    samples["count"] = min(int(samples.get("count", SMOKE_MAX_SAMPLES) or 0), SMOKE_MAX_SAMPLES)
    resources = spec.setdefault("resources", {})
    resources["compile"] = False
    return spec
