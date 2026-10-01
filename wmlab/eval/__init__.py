"""Evaluation on frozen benchmarks (ARCHITECTURE.md §8.2).

Functions are imported lazily so that importing ``wmlab.eval`` stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["evaluate_checkpoint", "run_benchmark"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from wmlab.eval import benchmark

        return getattr(benchmark, name)
    raise AttributeError(f"module 'wmlab.eval' has no attribute {name!r}")
