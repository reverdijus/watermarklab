"""Workspace and data locations (ARCHITECTURE.md §1).

Environment variables:
- ``WMLAB_WORKSPACE``: workspace root (default: search upwards for ``workspace.yaml``).
- ``WMLAB_DATA``: data root (default: ``<workspace>/<data_dir>``).
- ``WMLAB_HOST``: current host name (default ``local``).
- ``WMLAB_PRICE_EUR_PER_HOUR``: price of the current host (default ``0``).
- ``WMLAB_WEIGHTS``: official pretrained weights folder (default: ``<data>/weights``).
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from wmlab.spec import WorkspaceSettings

WORKSPACE_FILE = "workspace.yaml"
DEFAULT_DATA_DIR = ".wmlab"


class WorkspaceNotFoundError(RuntimeError):
    """Raised when no workspace root can be located."""


def _search_upwards(start: Path) -> Path | None:
    """Return the first directory (start or a parent) containing ``workspace.yaml``."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / WORKSPACE_FILE).is_file():
            return candidate
    return None


def workspace_root() -> Path:
    """Return the workspace root directory.

    Order: ``WMLAB_WORKSPACE``, then an upward search from the cwd, then an upward
    search from the installed package location (the package lives in the workspace).
    """
    env = os.environ.get("WMLAB_WORKSPACE")
    if env:
        root = Path(env).expanduser().resolve()
        if not root.is_dir():
            raise WorkspaceNotFoundError(
                f"WMLAB_WORKSPACE pointe vers un dossier inexistant : {root}"
            )
        return root
    found = _search_upwards(Path.cwd()) or _search_upwards(Path(__file__).resolve().parent.parent)
    if found is None:
        raise WorkspaceNotFoundError(
            "Workspace introuvable : aucun fichier workspace.yaml trouvé dans le dossier "
            "courant ni ses parents. Placez-vous dans le workspace ou définissez "
            "WMLAB_WORKSPACE=<chemin du workspace>."
        )
    return found


def load_workspace_settings(root: Path | None = None) -> "WorkspaceSettings":
    """Load ``workspace.yaml`` (defaults when the file is absent)."""
    from wmlab.spec import WorkspaceSettings, load_yaml

    root = root or workspace_root()
    path = root / WORKSPACE_FILE
    if not path.is_file():
        return WorkspaceSettings()
    return load_yaml(path, "workspace")


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_root() -> Path:
    """Return the data root (runs/, studies/, cache/, jobs/, logs/), creating it if needed."""
    env = os.environ.get("WMLAB_DATA")
    if env:
        root = Path(env).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
    else:
        ws = workspace_root()
        try:
            data_dir = load_workspace_settings(ws).data_dir or DEFAULT_DATA_DIR
        except Exception:  # invalid workspace.yaml must not hide the data
            data_dir = DEFAULT_DATA_DIR
        root = Path(data_dir).expanduser()
        if not root.is_absolute():
            root = ws / root
    return _ensure_dir(root.resolve())


def runs_dir() -> Path:
    """Return ``<data>/runs`` (created if needed)."""
    return _ensure_dir(data_root() / "runs")


def studies_dir() -> Path:
    """Return ``<data>/studies`` (created if needed)."""
    return _ensure_dir(data_root() / "studies")


def cache_dir() -> Path:
    """Return ``<data>/cache`` (created if needed)."""
    return _ensure_dir(data_root() / "cache")


def weights_dir() -> Path:
    """Return the official pretrained weights folder (``WMLAB_WEIGHTS``, default
    ``<data>/weights``; created if needed)."""
    env = os.environ.get("WMLAB_WEIGHTS")
    if env:
        return _ensure_dir(Path(env).expanduser().resolve())
    return _ensure_dir(data_root() / "weights")


def jobs_dir() -> Path:
    """Return ``<data>/jobs`` (created if needed)."""
    return _ensure_dir(data_root() / "jobs")


def logs_dir() -> Path:
    """Return ``<data>/logs`` (created if needed)."""
    return _ensure_dir(data_root() / "logs")


def hosts_path() -> Path:
    """Return the path of ``<data>/hosts.yaml`` (not created)."""
    return data_root() / "hosts.yaml"


def index_path() -> Path:
    """Return the path of the server SQLite index ``<data>/index.sqlite``."""
    return data_root() / "index.sqlite"


def current_host() -> str:
    """Return the current host name (``WMLAB_HOST``, default ``local``)."""
    return os.environ.get("WMLAB_HOST") or "local"


def current_price() -> float:
    """Return the current host price in €/h (``WMLAB_PRICE_EUR_PER_HOUR``, default 0)."""
    raw = os.environ.get("WMLAB_PRICE_EUR_PER_HOUR", "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        warnings.warn(
            f"WMLAB_PRICE_EUR_PER_HOUR invalide ({raw!r}) : prix considéré nul.", stacklevel=2
        )
        return 0.0
    return max(value, 0.0)
