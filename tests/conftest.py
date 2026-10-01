"""Shared pytest fixtures.

- ``workspace``: an isolated workspace in ``tmp_path`` (workspace.yaml, projects/, datasets/,
  benchmarks/, lab/__init__.py) with ``WMLAB_WORKSPACE``/``WMLAB_DATA`` pointing to it and the
  cwd set to it. User plugins imported from its ``lab`` package are forgotten afterwards.
- registries are restored after every test (components registered by a test or by a ``lab``
  plugin disappear; builtin components imported during the test stay registered).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterator

import pytest

from wmlab.registry import REGISTRIES

WORKSPACE_YAML = """\
name: "Workspace de test"
budget:
  total_eur: 1000
  start: "2026-10-01"
  end: "2027-09-30"
  manual_entries: []
data_dir: .wmlab
sync_interval_s: 30
default_host: local
"""


@pytest.fixture(autouse=True)
def _restore_registries() -> Iterator[None]:
    snapshots = {kind: registry.snapshot() for kind, registry in REGISTRIES.items()}
    yield
    for kind, registry in REGISTRIES.items():
        restored = dict(snapshots[kind])
        for name, entry in registry.snapshot().items():
            if name not in restored and entry.builtin:
                restored[name] = entry  # builtin modules are imported once per process
        registry.restore(restored)


def _forget_lab(root: Path) -> None:
    """Drop the ``lab`` modules imported from ``root`` and its sys.path entry."""
    for name in [m for m in sys.modules if m == "lab" or m.startswith("lab.")]:
        module = sys.modules[name]
        location = getattr(module, "__file__", None) or next(iter(getattr(module, "__path__", [])), "")
        if location and str(root) in str(location):
            del sys.modules[name]
    while str(root) in sys.path:
        sys.path.remove(str(root))


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated workspace root (see module docstring)."""
    root = (tmp_path / "workspace").resolve()
    for sub in ("projects", "datasets", "benchmarks", "lab"):
        (root / sub).mkdir(parents=True)
    (root / "workspace.yaml").write_text(WORKSPACE_YAML, encoding="utf-8")
    (root / "lab" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setenv("WMLAB_WORKSPACE", str(root))
    monkeypatch.setenv("WMLAB_DATA", str(root / ".wmlab"))
    monkeypatch.delenv("WMLAB_HOST", raising=False)
    monkeypatch.delenv("WMLAB_PRICE_EUR_PER_HOUR", raising=False)
    monkeypatch.chdir(root)
    yield root
    _forget_lab(root)
