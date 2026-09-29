"""Import the builtin component packages so that they register themselves.

Each package is imported separately: a broken or missing package never prevents the
others (nor user plugins) from loading.
"""

from __future__ import annotations

import importlib
import traceback

BUILTIN_MODULES: tuple[str, ...] = (
    "wmlab.models",
    "wmlab.attacks",
    "wmlab.data",
    "wmlab.losses",
    "wmlab.metrics",
)


def load() -> list[dict[str, str]]:
    """Import every builtin package; return ``[{"module", "error"}]`` for failures."""
    errors: list[dict[str, str]] = []
    for name in BUILTIN_MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            errors.append({"module": name, "error": traceback.format_exc(limit=-3).strip()})
    return errors


IMPORT_ERRORS: list[dict[str, str]] = load()
