"""Small, robust file helpers shared by writers and readers."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any


def sanitize(obj: Any) -> Any:
    """Return a JSON-safe copy of ``obj`` (NaN/inf → None, tensors/arrays → Python)."""
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "tolist"):  # numpy arrays/scalars, torch tensors
        try:
            if hasattr(obj, "detach"):
                obj = obj.detach().cpu()
            return sanitize(obj.tolist())
        except Exception:
            pass
    if hasattr(obj, "item"):
        try:
            return sanitize(obj.item())
        except Exception:
            pass
    return str(obj)


def to_float(value: Any) -> float | None:
    """Convert a metric value (number, 0-d tensor, numpy scalar) to a finite float or None."""
    try:
        if hasattr(value, "detach"):
            value = value.detach().cpu()
        if hasattr(value, "item"):
            value = value.item()
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (temporary file in the same folder + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, obj: Any) -> None:
    """Write JSON atomically (NaN/inf are written as null)."""
    atomic_write_text(Path(path), json.dumps(sanitize(obj), indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    """Read a JSON file; return ``default`` if missing or unreadable."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file; skip blank/invalid lines (e.g. a partial last line)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def json_line(obj: Any) -> str:
    """Serialize one JSONL line (no trailing newline)."""
    return json.dumps(sanitize(obj), ensure_ascii=False, separators=(",", ":"))


def tail_lines(path: Path, n: int, chunk_size: int = 65536) -> list[str]:
    """Return the last ``n`` lines of a text file without reading it entirely."""
    if n <= 0:
        return []
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            data = b""
            pos = size
            while pos > 0 and data.count(b"\n") <= n:
                read = min(chunk_size, pos)
                pos -= read
                handle.seek(pos)
                data = handle.read(read) + data
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if pos > 0 and lines:
        lines = lines[1:]  # first line may be truncated
    return [_last_segment(line) for line in lines[-n:]]


def _last_segment(line: str) -> str:
    """Keep the final state of a carriage-return progress line."""
    line = line.rstrip("\r")
    if "\r" in line:
        segments = [s for s in line.split("\r") if s]
        return segments[-1] if segments else ""
    return line
