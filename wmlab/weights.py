"""Official pretrained weights of baseline models (ARCHITECTURE.md §4.6).

Plugins declare the files they need with :func:`register_weights`. Files are cached per host
in :func:`wmlab.paths.weights_dir` (``WMLAB_WEIGHTS``, default ``<data>/weights``), never in
the repository, and are downloaded only on request: ``wmlab weights pull NAME`` or a model
built with ``download: true``.
"""

from __future__ import annotations

import hashlib
import os
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from wmlab import paths

CHUNK = 1 << 20


@dataclass(frozen=True)
class WeightsFile:
    """One official weights file (``name`` is the id used by the CLI and the models)."""

    name: str
    url: str
    filename: str
    size_mb: float
    license: str
    source: str
    sha256: str | None = None
    commercial_use: bool = True
    description: str = ""


WEIGHTS: dict[str, WeightsFile] = {}


def register_weights(entry: WeightsFile) -> WeightsFile:
    """Declare a weights file (re-registering the same entry is a no-op)."""
    known = WEIGHTS.get(entry.name)
    if known is not None and known != entry:
        raise ValueError(f"poids '{entry.name}' déjà déclarés avec une autre définition")
    if not entry.url.startswith("https://"):
        raise ValueError(f"poids '{entry.name}' : l'URL doit être en https ({entry.url})")
    WEIGHTS[entry.name] = entry
    return entry


def get_weights(name: str) -> WeightsFile:
    """Return a declared weights file (explicit error listing the known names)."""
    if name not in WEIGHTS:
        from wmlab.registry import load_plugins

        load_plugins()
    try:
        return WEIGHTS[name]
    except KeyError:
        from wmlab.registry import UnknownComponentError

        known = ", ".join(sorted(WEIGHTS)) or "aucun"
        raise UnknownComponentError(f"poids inconnus '{name}' — poids déclarés : {known}",
                                    kind="weights", name=name) from None


def weights_path(name: str) -> Path:
    """Local path of a weights file (``<weights_dir>/<name>/<filename>``, not downloaded)."""
    entry = get_weights(name)
    return paths.weights_dir() / entry.name / entry.filename


def sha256_file(path: Path) -> str:
    """Hex sha256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _license_line(entry: WeightsFile) -> str:
    usage = "" if entry.commercial_use else " — USAGE NON COMMERCIAL"
    return f"licence : {entry.license}{usage} ({entry.source})"


def require_weights(name: str, *, download: bool = False) -> Path:
    """Return the local path of ``name``; download it first if absent and ``download``."""
    path = weights_path(name)
    if path.is_file():
        return path
    if download:
        return pull_weights(name)
    entry = get_weights(name)
    raise FileNotFoundError(
        f"poids officiels '{name}' absents sur cette machine ({path}). Téléchargez-les avec "
        f"« wmlab weights pull {name} » (~{entry.size_mb:g} Mo, {_license_line(entry)}) ou "
        "mettez « download: true » dans model.params")


def pull_weights(name: str, *, force: bool = False) -> Path:
    """Download ``name`` into the weights folder (atomic: ``.part`` then rename).

    The sha256 is checked when the entry pins one; the computed digest is always printed.
    """
    entry = get_weights(name)
    path = weights_path(name)
    if path.is_file() and not force:
        print(f"{name} : déjà présent ({path})", flush=True)
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    print(f"Téléchargement de {name} (~{entry.size_mb:g} Mo) depuis {entry.url}\n"
          f"  {_license_line(entry)}", flush=True)
    request = urllib.request.Request(entry.url, headers={"User-Agent": "wmlab"})
    digest = hashlib.sha256()
    done = 0
    last_print = 0.0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            for chunk in iter(lambda: response.read(CHUNK), b""):
                out.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                now = time.time()
                if total and (now - last_print > 2.0 or done == total):
                    last_print = now
                    print(f"  {done / 1e6:.0f}/{total / 1e6:.0f} Mo "
                          f"({100.0 * done / total:.0f} %)", flush=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    got = digest.hexdigest()
    if entry.sha256 and got != entry.sha256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"{name} : somme de contrôle sha256 inattendue ({got} au lieu de "
                         f"{entry.sha256}) — fichier supprimé")
    os.replace(tmp, path)
    print(f"{name} : {done / 1e6:.1f} Mo écrits dans {path} (sha256 {got})", flush=True)
    return path


def describe_weights() -> list[dict[str, Any]]:
    """Every declared file with its local state (``available``, ``path``, ``size_mb_on_disk``)."""
    from wmlab.registry import load_plugins

    load_plugins()
    rows = []
    for name in sorted(WEIGHTS):
        path = weights_path(name)
        available = path.is_file()
        rows.append({**asdict(WEIGHTS[name]), "path": str(path), "available": available,
                     "size_mb_on_disk": round(path.stat().st_size / 1e6, 1) if available else None})
    return rows


def weights_info(name: str, path: Path) -> dict[str, Any]:
    """Traceability record of a loaded file (written into reference checkpoints)."""
    entry = get_weights(name)
    return {"name": name, "file": entry.filename, "url": entry.url, "license": entry.license,
            "sha256": sha256_file(path)}
