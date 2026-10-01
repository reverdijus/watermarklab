"""Server context: workspace root, data locations, settings and hosts."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from wmlab.spec import HostSpec, WorkspaceSettings, load_hosts, load_yaml

log = logging.getLogger("wmlab.server")

WORKSPACE_FILE = "workspace.yaml"


def resolve_data_root(workspace: Path) -> Path:
    """Return the data root of ``workspace`` (``WMLAB_DATA`` or ``<workspace>/<data_dir>``)."""
    env = os.environ.get("WMLAB_DATA")
    if env:
        root = Path(env).expanduser()
        if not root.is_absolute():
            root = Path.cwd() / root
        return root.resolve()
    data_dir = ".wmlab"
    try:
        if (workspace / WORKSPACE_FILE).is_file():
            data_dir = load_yaml(workspace / WORKSPACE_FILE, "workspace").data_dir or data_dir
    except Exception:  # an invalid workspace.yaml must not hide the data
        pass
    root = Path(data_dir).expanduser()
    if not root.is_absolute():
        root = workspace / root
    return root.resolve()


@dataclass
class ServerContext:
    """Locations shared by every server component."""

    workspace: Path
    data: Path

    @classmethod
    def for_workspace(cls, workspace: Path) -> "ServerContext":
        """Build the context of a workspace (data folders are created)."""
        ws = Path(workspace).expanduser().resolve()
        ctx = cls(workspace=ws, data=resolve_data_root(ws))
        for folder in (ctx.runs_dir, ctx.studies_dir, ctx.jobs_dir, ctx.logs_dir):
            folder.mkdir(parents=True, exist_ok=True)
        return ctx

    @property
    def runs_dir(self) -> Path:
        return self.data / "runs"

    @property
    def studies_dir(self) -> Path:
        return self.data / "studies"

    @property
    def jobs_dir(self) -> Path:
        return self.data / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.data / "logs"

    @property
    def hosts_path(self) -> Path:
        return self.data / "hosts.yaml"

    @property
    def index_path(self) -> Path:
        return self.data / "index.sqlite"

    @property
    def settings_path(self) -> Path:
        return self.workspace / WORKSPACE_FILE

    def settings(self) -> WorkspaceSettings:
        """Load ``workspace.yaml`` (defaults if missing or invalid)."""
        try:
            if self.settings_path.is_file():
                return load_yaml(self.settings_path, "workspace")
        except Exception as exc:
            log.warning("workspace.yaml invalide, valeurs par défaut utilisées : %s", exc)
        return WorkspaceSettings()

    def hosts(self) -> list[HostSpec]:
        """Load ``hosts.yaml`` (created with the ``local`` host when missing)."""
        return load_hosts(self.hosts_path)

    def rel(self, path: Path) -> str:
        """Return ``path`` relative to the workspace (posix) when possible."""
        try:
            return Path(path).resolve().relative_to(self.workspace).as_posix()
        except ValueError:
            return str(path)
