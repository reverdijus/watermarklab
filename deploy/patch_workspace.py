#!/usr/bin/env python3
"""Minimal text patch of workspace.yaml for hosted mode (ARCHITECTURE.md §12.1).

Rewrites only the top-level ``hosted:`` block (or appends one), keeping every other line,
comment and blank line untouched. Standard library only: runs with any python3.

    patch_workspace.py --file workspace.yaml --branch main [--public-url https://lab.example.com]
                       [--auto-pull-s 60] [--auto-commit true|false] [--disable] [--dry-run]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HOSTED_KEYS = ("enabled", "git_branch", "auto_pull_s", "auto_commit", "public_url")
DEFAULTS = {"enabled": "false", "git_branch": "main", "auto_pull_s": "60",
            "auto_commit": "true", "public_url": "null"}
TOP_LEVEL = re.compile(r"^[A-Za-z_][\w-]*\s*:")
CHILD = re.compile(r"^(\s+)([A-Za-z_][\w-]*)\s*:\s*(.*?)\s*(#.*)?$")


def yaml_scalar(value: str) -> str:
    """Quote a string unless it is a YAML literal we write ourselves."""
    if value in ("true", "false", "null") or re.fullmatch(r"-?\d+(\.\d+)?", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_block(values: dict[str, str]) -> list[str]:
    lines = ["hosted:"]
    comments = {"enabled": "true sur le serveur central (postes des membres : false)",
                "git_branch": "branche suivie par le serveur (git pull --ff-only)",
                "auto_pull_s": "0 = désactivé",
                "auto_commit": "PUT /api/files → commit + push",
                "public_url": "affichée dans l'UI"}
    for key in HOSTED_KEYS:
        lines.append(f"  {key}: {values[key]}    # {comments[key]}")
    return lines


def patch(text: str, updates: dict[str, str]) -> str:
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(r"^hosted\s*:", l)), None)
    values = dict(DEFAULTS)
    if start is None:
        values.update(updates)
        out = list(lines)
        if out and out[-1].strip():
            out.append("")
        out.extend(render_block(values))
        return "\n".join(out) + "\n"
    # The block spans every following indented (or blank) line; trailing blank lines are
    # given back so the spacing before the next key is preserved.
    end = start + 1
    while end < len(lines) and (lines[end].strip() == "" or lines[end][0] in " \t"):
        end += 1
    while end > start + 1 and lines[end - 1].strip() == "":
        end -= 1
    inline = re.match(r"^hosted\s*:\s*\{(.*)\}\s*(#.*)?$", lines[start])
    if inline:  # flow style: hosted: {enabled: false, ...}
        for part in inline.group(1).split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                values[k.strip()] = v.strip().strip("'\"") or "null"
        end = start + 1
    else:
        for line in lines[start + 1:end]:
            m = CHILD.match(line)
            if m and m.group(2) in HOSTED_KEYS:
                raw = m.group(3)
                values[m.group(2)] = yaml_scalar(raw.strip("'\"")) if raw else "null"
    values.update(updates)
    out = lines[:start] + render_block(values) + lines[end:]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--branch", help="hosted.git_branch")
    ap.add_argument("--public-url", help="hosted.public_url")
    ap.add_argument("--auto-pull-s", type=int)
    ap.add_argument("--auto-commit", choices=["true", "false"])
    ap.add_argument("--disable", action="store_true", help="hosted.enabled: false (défaut : true)")
    ap.add_argument("--dry-run", action="store_true", help="affiche le résultat sans écrire")
    args = ap.parse_args()
    if not args.file.is_file():
        print(f"Erreur : {args.file} introuvable", file=sys.stderr)
        return 1
    updates = {"enabled": "false" if args.disable else "true"}
    if args.branch:
        updates["git_branch"] = yaml_scalar(args.branch)
    if args.public_url:
        updates["public_url"] = yaml_scalar(args.public_url)
    if args.auto_pull_s is not None:
        updates["auto_pull_s"] = str(args.auto_pull_s)
    if args.auto_commit:
        updates["auto_commit"] = args.auto_commit
    original = args.file.read_text(encoding="utf-8")
    result = patch(original, updates)
    if args.dry_run:
        sys.stdout.write(result)
        return 0
    if result != original:
        tmp = args.file.with_suffix(args.file.suffix + ".tmp")
        tmp.write_text(result, encoding="utf-8")
        tmp.replace(args.file)
        print(f"{args.file} : bloc hosted mis à jour")
    else:
        print(f"{args.file} : déjà à jour")
    return 0


if __name__ == "__main__":
    sys.exit(main())
