"""Training engine (ARCHITECTURE.md §8.1).

``train_from_spec`` is imported lazily so that importing ``wmlab.train`` stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["train_from_spec"]


def __getattr__(name: str) -> Any:
    if name == "train_from_spec":
        from wmlab.train.trainer import train_from_spec

        return train_from_spec
    raise AttributeError(f"module 'wmlab.train' has no attribute {name!r}")
