"""Loss terms (ARCHITECTURE.md §4.4): ``StepBatch``, the ``Loss`` base class and builtins."""

from wmlab.losses import builtin  # noqa: F401 — registration
from wmlab.losses.base import Loss, StepBatch

__all__ = ["Loss", "StepBatch"]
