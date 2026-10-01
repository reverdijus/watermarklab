#!/usr/bin/env python3
"""PreToolUse hook: enforce the "never" rules of CLAUDE.md mechanically.

Reads the hook payload on stdin and blocks (exit code 2, reason on stderr):
- Edit/Write under ``.wmlab/`` (results and costs) except a run's ``notes.md``;
- Edit/Write of a published (git-tracked) ``benchmarks/*.yaml``;
- a Bash ``rm`` whose target mentions ``.wmlab``.
Anything else passes (exit 0). Never raises: on any doubt, allow.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}
RM_PATTERN = re.compile(r"(^|[;&|]\s*)(sudo\s+)?rm\s[^;&|]*\.wmlab")


def block(message: str) -> int:
    sys.stderr.write(message.rstrip() + "\n")
    return 2


def git_tracked(root: Path, rel: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--error-unmatch", str(rel)],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return rel.exists()
    return result.returncode == 0


def check_edit(root: Path, file_path: str) -> int:
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    try:
        rel = path.resolve().relative_to(root)
    except (ValueError, OSError):
        return 0
    parts = rel.parts
    if parts and parts[0] == ".wmlab":
        if rel.name == "notes.md":
            return 0
        return block(
            f"Refusé : `{rel}` est sous `.wmlab/` (source de vérité des runs, études, coûts, "
            "machines). Seul le `notes.md` d'un run ou d'une étude peut être complété ; "
            "le reste passe par la CLI `wmlab` ou par l'app (CLAUDE.md § Règles)."
        )
    if len(parts) == 2 and parts[0] == "benchmarks" and rel.suffix in (".yaml", ".yml"):
        if git_tracked(root, rel):
            stem = rel.stem
            match = re.fullmatch(r"(.+)_v(\d+)", stem)
            suggestion = f"{match.group(1)}_v{int(match.group(2)) + 1}.yaml" if match else f"{stem}_v2.yaml"
            return block(
                f"Refusé : `{rel}` est un benchmark publié (commité) ; ses résultats deviendraient "
                f"incomparables. Créer `benchmarks/{suggestion}` à la place (CLAUDE.md § Règles)."
            )
    return 0


def check_bash(command: str) -> int:
    if RM_PATTERN.search(command):
        return block(
            "Refusé : cette commande supprime quelque chose sous `.wmlab/` (runs, études, "
            "checkpoints, coûts). On ne supprime jamais de runs ni d'études (CLAUDE.md § Règles)."
        )
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    if tool in EDIT_TOOLS:
        file_path = tool_input.get("file_path")
        return check_edit(root, file_path) if file_path else 0
    if tool == "Bash":
        return check_bash(tool_input.get("command") or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
