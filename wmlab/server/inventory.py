"""Checkpoint inventory of a host and retrieval verification (ARCHITECTURE.md §12.4).

One remote command lists the runs, their ``checkpoints/index.json``, the sizes of every
``*.pt`` (``find -printf``) and the studies of ``<data_dir>``; the comparison with the local
data root happens here. Results are cached in ``<data>/hosts_inventory.json``.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wmlab.ids import utc_iso
from wmlab.tracking import atomic_write_json, load_index, read_json

INVENTORY_FILE = "hosts_inventory.json"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MARKERS = ("==NODATA==", "==RUNS==", "==INDEX==", "==FILES==", "==STUDIES==", "==END==")


def listing_command(data_dir_shell: str) -> str:
    """Remote shell command listing runs, indexes, ``*.pt`` sizes and studies.

    ``data_dir_shell`` is already quoted for the remote shell. GNU ``find -printf`` (Linux).
    """
    return (
        f"cd {data_dir_shell} 2>/dev/null || {{ echo '==NODATA=='; exit 0; }}; "
        "echo '==RUNS=='; "
        "for f in runs/*/run.json; do [ -f \"$f\" ] || continue; printf '%s\\t' \"$f\"; "
        "tr -d '\\n\\r' < \"$f\"; echo; done; "
        "echo '==INDEX=='; "
        "for f in runs/*/checkpoints/index.json; do [ -f \"$f\" ] || continue; "
        "printf '%s\\t' \"$f\"; tr -d '\\n\\r' < \"$f\"; echo; done; "
        "echo '==FILES=='; "
        "find runs -path '*/checkpoints/*.pt' -type f -printf '%p\\t%s\\n' 2>/dev/null; "
        "echo '==STUDIES=='; "
        "for f in studies/*/study.json; do [ -f \"$f\" ] || continue; d=\"${f%/study.json}\"; "
        "n=0; [ -f \"$d/trials.jsonl\" ] && n=$(wc -l < \"$d/trials.jsonl\"); "
        "printf '%s\\t%s\\n' \"$f\" \"$n\"; done; "
        "echo '==END=='"
    )


@dataclass
class RemoteListing:
    """Parsed output of :func:`listing_command`."""

    runs: dict[str, dict[str, Any]] = field(default_factory=dict)      # run_id → run.json
    indexes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    files: dict[str, dict[str, int]] = field(default_factory=dict)     # run_id → {file: size}
    studies: dict[str, int] = field(default_factory=dict)              # study_id → trial rows
    data_present: bool = True
    complete: bool = False                                             # ``==END==`` seen

    @property
    def run_ids(self) -> list[str]:
        return sorted(set(self.runs) | set(self.indexes) | set(self.files))


def _run_id_of(path: str) -> str | None:
    parts = path.strip().split("/")
    if len(parts) < 3 or parts[0] != "runs" or not SAFE_ID.match(parts[1]):
        return None
    return parts[1]


def parse_listing(stdout: str) -> RemoteListing:
    """Parse the remote listing (tolerant: unreadable lines are skipped)."""
    listing = RemoteListing()
    section = None
    for raw in stdout.splitlines():
        line = raw.rstrip("\r")
        if line in MARKERS:
            section = line
            if line == "==NODATA==":
                listing.data_present = False
            elif line == "==END==":
                listing.complete = True
            continue
        if not line.strip() or section is None:
            continue
        if section == "==RUNS==":
            path, _, payload = line.partition("\t")
            run_id = _run_id_of(path)
            if run_id is None:
                continue
            data = _json(payload)
            listing.runs[run_id] = data if isinstance(data, dict) else {}
        elif section == "==INDEX==":
            path, _, payload = line.partition("\t")
            run_id = _run_id_of(path)
            if run_id is None:
                continue
            data = _json(payload)
            if isinstance(data, list):  # an unreadable index is ignored (rewritten soon)
                listing.indexes[run_id] = [e for e in data if isinstance(e, dict)]
        elif section == "==FILES==":
            path, _, size = line.rpartition("\t")
            run_id = _run_id_of(path)
            if run_id is None or "/checkpoints/" not in path or not path.endswith(".pt"):
                continue
            try:
                listing.files.setdefault(run_id, {})[path.rsplit("/", 1)[1]] = int(size)
            except ValueError:
                continue
        elif section == "==STUDIES==":
            path, _, count = line.rpartition("\t")
            parts = path.strip().split("/")
            if len(parts) != 3 or parts[0] != "studies" or not SAFE_ID.match(parts[1]):
                continue
            try:
                listing.studies[parts[1]] = int(count.strip() or 0)
            except ValueError:
                listing.studies[parts[1]] = 0
    return listing


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


def _entry_file(entry: dict[str, Any]) -> str:
    return str(entry.get("file") or f"{entry.get('name')}.pt")


def _size(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _local_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


# -- inventory ----------------------------------------------------------------------------------
def build_inventory(host: str, listing: RemoteListing, local_data: Path,
                    indexed_run_ids: set[str], *, active_jobs: int,
                    remote_data_dir: str | None) -> dict[str, Any]:
    """``HostInventory`` of a reachable SSH host from its listing and the local files."""
    runs = []
    for run_id in listing.run_ids:
        manifest = listing.runs.get(run_id) or {}
        entries = listing.indexes.get(run_id) or []
        sizes = listing.files.get(run_id) or {}
        local_ckpt = local_data / "runs" / run_id / "checkpoints"
        total = local_ok = bytes_remote = bytes_missing = 0
        for entry in entries:
            name = _entry_file(entry)
            remote_size = sizes.get(name, _size(entry.get("size_bytes")))
            total += 1
            bytes_remote += remote_size
            local_size = _local_size(local_ckpt / name)
            if local_size is not None and (local_size == remote_size or remote_size == 0):
                local_ok += 1
            else:
                bytes_missing += remote_size
        runs.append({
            "run_id": run_id,
            "project": str(manifest["project"]) if manifest.get("project") else None,
            "status": str(manifest["status"]) if manifest.get("status") else None,
            "checkpoints_total": total,
            "checkpoints_local": local_ok,
            "bytes_remote": bytes_remote,
            "bytes_missing": bytes_missing,
            "indexed": run_id in indexed_run_ids,
        })
    studies = []
    for study_id in sorted(listing.studies):
        remote_rows = listing.studies[study_id]
        local_study = local_data / "studies" / study_id
        synced = (local_study / "study.json").is_file() and \
            _line_count(local_study / "trials.jsonl") >= remote_rows
        studies.append({"study_id": study_id, "trials": remote_rows, "synced": synced})
    return {
        "host": host,
        "reachable": True,
        "error": None,
        "checked_at": utc_iso(),
        "active_jobs": active_jobs,
        "remote_data_dir": remote_data_dir,
        "runs": runs,
        "studies": studies,
        "unretrieved_checkpoints": sum(r["checkpoints_total"] - r["checkpoints_local"]
                                       for r in runs),
        "unretrieved_bytes": sum(r["bytes_missing"] for r in runs),
        "unindexed_runs": sum(1 for r in runs if not r["indexed"]),
    }


def _line_count(path: Path) -> int:
    try:
        with open(path, "rb") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def unreachable_inventory(host: str, error: str, *, active_jobs: int,
                          remote_data_dir: str | None) -> dict[str, Any]:
    return {
        "host": host, "reachable": False, "error": error, "checked_at": utc_iso(),
        "active_jobs": active_jobs, "remote_data_dir": remote_data_dir, "runs": [],
        "studies": [], "unretrieved_checkpoints": 0, "unretrieved_bytes": 0,
        "unindexed_runs": 0,
    }


def local_inventory(host: str, run_manifests: list[dict[str, Any]],
                    study_manifests: list[dict[str, Any]], local_data: Path, *,
                    active_jobs: int) -> dict[str, Any]:
    """Trivial inventory of the local host: everything is already local."""
    runs = []
    for manifest in run_manifests:
        if str(manifest.get("host") or "local") != host:
            continue
        run_id = str(manifest.get("run_id") or "")
        entries = load_index(local_data / "runs" / run_id) if run_id else []
        present = [e for e in entries
                   if _local_size(local_data / "runs" / run_id / "checkpoints"
                                  / _entry_file(e)) is not None]
        runs.append({
            "run_id": run_id,
            "project": str(manifest["project"]) if manifest.get("project") else None,
            "status": str(manifest["status"]) if manifest.get("status") else None,
            "checkpoints_total": len(present),
            "checkpoints_local": len(present),
            "bytes_remote": sum(_size(e.get("size_bytes")) for e in present),
            "bytes_missing": 0,
            "indexed": True,
        })
    studies = [{"study_id": str(m.get("study_id") or ""),
                "trials": _line_count(local_data / "studies" / str(m.get("study_id") or "")
                                      / "trials.jsonl"),
                "synced": True}
               for m in study_manifests if str(m.get("host") or "local") == host]
    return {
        "host": host, "reachable": True, "error": None, "checked_at": utc_iso(),
        "active_jobs": active_jobs, "remote_data_dir": str(local_data), "runs": runs,
        "studies": studies, "unretrieved_checkpoints": 0, "unretrieved_bytes": 0,
        "unindexed_runs": 0,
    }


# -- retrieval verification ---------------------------------------------------------------------
def verify_runs(runs_dir: Path, run_ids: list[str]) -> dict[str, Any]:
    """Check every ``checkpoints/index.json`` entry of ``run_ids`` against the local files.

    Returns ``{"checkpoints": verified, "bytes": verified bytes, "missing": ["<run>/checkpoints/<file>"…],
    "by_run": {run_id: [missing files]}}``.
    """
    verified = total_bytes = 0
    missing: list[str] = []
    by_run: dict[str, list[str]] = {}
    for run_id in dict.fromkeys(run_ids):
        run_dir = Path(runs_dir) / run_id
        for entry in load_index(run_dir):
            name = _entry_file(entry)
            expected = _size(entry.get("size_bytes"))
            actual = _local_size(run_dir / "checkpoints" / name)
            if actual is not None and (actual == expected or expected == 0):
                verified += 1
                total_bytes += actual
            else:
                missing.append(f"{run_id}/checkpoints/{name}")
                by_run.setdefault(run_id, []).append(name)
    return {"checkpoints": verified, "bytes": total_bytes, "missing": missing, "by_run": by_run}


def retrieval_result(check: dict[str, Any], *, failed: bool = False) -> dict[str, Any]:
    """``Job.retrieval`` of §12.4 from a :func:`verify_runs` result."""
    if failed:
        status = "failed"
    elif check["missing"]:
        status = "partial"
    else:
        status = "complete"
    return {"status": status, "checkpoints": int(check["checkpoints"]),
            "bytes": int(check["bytes"]), "missing": list(check["missing"])}


# -- cache ----------------------------------------------------------------------------------------
class InventoryCache:
    """``<data>/hosts_inventory.json``: last inventory per host."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict[str, Any]]:
        data = read_json(self.path)
        return {k: v for k, v in data.items() if isinstance(v, dict)} \
            if isinstance(data, dict) else {}

    def get(self, host: str) -> dict[str, Any] | None:
        return self.all().get(host)

    def set(self, host: str, inventory: dict[str, Any]) -> None:
        with self._lock:
            data = self.all()
            data[host] = inventory
            atomic_write_json(self.path, data)

    def remove(self, host: str) -> None:
        with self._lock:
            data = self.all()
            if data.pop(host, None) is not None:
                atomic_write_json(self.path, data)


def host_inventory_fields(cached: dict[str, Any] | None) -> dict[str, Any]:
    """The ``Host`` API fields derived from the cached inventory."""
    if not cached or not cached.get("reachable"):
        return {"unretrieved_checkpoints": None, "unretrieved_bytes": None,
                "inventory_at": cached.get("checked_at") if cached else None}
    return {"unretrieved_checkpoints": int(cached.get("unretrieved_checkpoints") or 0),
            "unretrieved_bytes": int(cached.get("unretrieved_bytes") or 0),
            "inventory_at": cached.get("checked_at")}


__all__ = ["INVENTORY_FILE", "InventoryCache", "RemoteListing", "build_inventory",
           "host_inventory_fields", "listing_command", "local_inventory", "parse_listing",
           "retrieval_result", "unreachable_inventory", "verify_runs"]
