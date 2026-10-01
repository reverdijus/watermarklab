#!/usr/bin/env python3
"""PostToolUse hook: re-run ``wmlab validate`` after a spec YAML or a ``lab/`` plugin is edited.

Watched paths: ``projects/**/*.yaml``, ``benchmarks/*.yaml``, ``datasets/*.yaml``,
``workspace.yaml`` and ``lab/**/*.py`` (a plugin that fails to import, or a duplicate
registry name, only shows up in a full validation). On failure the error lines are sent
back to Claude (exit code 2). Silent (exit 0) when the venv is missing or the file is not watched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

YAML_DIRS = {"projects", "benchmarks", "datasets"}


def watched(rel: Path) -> bool:
    if rel.name == "workspace.yaml" and len(rel.parts) == 1:
        return True
    top = rel.parts[0] if rel.parts else ""
    if top in YAML_DIRS and rel.suffix in (".yaml", ".yml"):
        return True
    return top == "lab" and rel.suffix == ".py"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return 0
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    try:
        rel = path.resolve().relative_to(root)
    except (ValueError, OSError):
        return 0
    if not watched(rel):
        return 0
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        return 0
    try:
        result = subprocess.run(
            [str(python), "-m", "wmlab.cli", "validate"],
            cwd=root, capture_output=True, text=True, timeout=80,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write("wmlab validate : délai dépassé (80 s) ; relancer à la main.\n")
        return 2
    if result.returncode == 0:
        return 0
    lines = [l for l in (result.stdout + result.stderr).splitlines() if not l.startswith("✓")]
    sys.stderr.write(
        f"`wmlab validate` échoue après la modification de `{rel}` — corriger avant de continuer :\n"
        + "\n".join(lines[-60:]) + "\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
