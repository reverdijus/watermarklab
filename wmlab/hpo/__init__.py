"""Hyper-parameter optimisation studies (ARCHITECTURE.md §8.3).

Functions are imported lazily so that importing ``wmlab.hpo`` stays cheap.
"""

from __future__ import annotations

from typing import Any

__all__ = ["run_study", "run_worker"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from wmlab.hpo import driver

        return getattr(driver, name)
    raise AttributeError(f"module 'wmlab.hpo' has no attribute {name!r}")
