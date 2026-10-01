"""Declarative files of the workspace: projects, experiments, HPO plans, benchmarks, datasets.

Also holds the workspace file access used by ``/api/files`` (path traversal protection,
size limit, writable roots) and the validation / phase-plan preview of ``/api/validate``.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from wmlab.curriculum import PhasePlan
from wmlab.spec import (
    SPEC_MODELS,
    BenchmarkSpec,
    DatasetCatalogEntry,
    ExperimentSpec,
    HpoSpec,
    ProjectSpec,
    apply_experiment_overrides,
    default_objectives,
    detect_kind,
    detect_kind_from_content,
    experiment_path_for_hpo,
    kind_from_location,
    validate_content,
    validate_file,
)
from wmlab.spec.loader import YAML_SUFFIXES
from wmlab.spec.models import MODALITIES
from wmlab.server.views import objective_view

log = logging.getLogger("wmlab.server")

FORBIDDEN_PARTS = frozenset({".wmlab", ".venv", ".git", "node_modules"})
WRITABLE_ROOTS = ("projects", "benchmarks", "datasets", "lab")
MAX_FILE_BYTES = 1024 * 1024


class FileAccessError(Exception):
    """Refused file access; ``status`` is the HTTP status to return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# -- YAML helpers -----------------------------------------------------------------------------
def _regular_file(path: Path) -> bool:
    """True for a plain file reached without symlink (a symlink committed in the repository
    must never make the server read ``/etc/wmlab/env`` or the SSH key)."""
    try:
        return path.is_file() and not path.is_symlink() and not any(
            parent.is_symlink() for parent in path.parents if parent != path.anchor)
    except OSError:
        return False


def read_raw(path: Path) -> Any:
    """Parsed YAML content of ``path`` (``None`` if unreadable, invalid, a symlink, above
    ``MAX_FILE_BYTES`` or pathological — deep nesting)."""
    try:
        if not _regular_file(path) or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError, RecursionError, MemoryError):
        return None


def _raw_dict(path: Path) -> dict[str, Any]:
    data = read_raw(path)
    return data if isinstance(data, dict) else {}


def yaml_files(folder: Path) -> list[Path]:
    if not folder.is_dir() or folder.is_symlink():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and not p.is_symlink() and p.suffix in YAML_SUFFIXES)


def _errors(path: Path) -> list[dict[str, str]]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return [{"loc": "", "msg": "fichier trop volumineux (1 Mo maximum)"}]
        return [{"loc": str(e.get("loc", "")), "msg": str(e.get("msg", ""))}
                for e in validate_file(path)]
    except (Exception, RecursionError) as exc:  # validation must never break a listing
        return [{"loc": "", "msg": f"validation impossible : {type(exc).__name__}"}]


def _validation(errors: list[dict[str, str]]) -> dict[str, Any]:
    return {"valid": not errors, "errors": errors}


# -- projects ---------------------------------------------------------------------------------
def project_dirs(workspace: Path) -> list[Path]:
    projects = workspace / "projects"
    if not projects.is_dir():
        return []
    return sorted(p for p in projects.iterdir()
                  if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
                  and _regular_file(p / "project.yaml"))


def project_dir(workspace: Path, name: str) -> Path | None:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    folder = workspace / "projects" / name
    return folder if not folder.is_symlink() and _regular_file(folder / "project.yaml") else None


def project_data(folder: Path) -> dict[str, Any]:
    """Project spec as a dict (validated when possible, best effort otherwise)."""
    raw = _raw_dict(folder / "project.yaml")
    try:
        return ProjectSpec.model_validate(raw).model_dump(mode="json")
    except Exception:
        pass
    modality = raw.get("modality") if raw.get("modality") in MODALITIES else "image"
    objectives = default_objectives(modality)
    if isinstance(raw.get("objectives"), dict):
        for key in ("quality", "robustness"):
            if isinstance(raw["objectives"].get(key), dict):
                objectives[key] = {**objectives[key], **raw["objectives"][key]}
    bits = raw.get("message_bits")
    return {
        "name": folder.name,
        "title": str(raw.get("title") or folder.name),
        "modality": modality,
        "description": str(raw.get("description") or ""),
        "message_bits": bits if isinstance(bits, int) and not isinstance(bits, bool) else 48,
        "default_benchmark": raw.get("default_benchmark")
        if isinstance(raw.get("default_benchmark"), str) else None,
        "objectives": objectives,
        "tags": [str(t) for t in raw.get("tags") or []] if isinstance(raw.get("tags"), list)
        else [],
    }


def project_objectives(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objectives = data.get("objectives") if isinstance(data.get("objectives"), dict) else {}
    return {"quality": objective_view(objectives.get("quality")),
            "robustness": objective_view(objectives.get("robustness"))}


def project_notes(folder: Path) -> str:
    path = folder / "notes.md"
    try:
        if not _regular_file(path) or path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# -- experiments & HPO plans ------------------------------------------------------------------
def _count_steps(raw: dict[str, Any]) -> tuple[int, int]:
    phases = raw.get("phases") if isinstance(raw.get("phases"), list) else []
    total = 0
    for phase in phases:
        steps = phase.get("steps") if isinstance(phase, dict) else None
        if isinstance(steps, int) and not isinstance(steps, bool) and steps > 0:
            total += steps
    return len(phases), total


def _spec_name(raw: dict[str, Any], path: Path) -> str:
    name = raw.get("name")
    return name if isinstance(name, str) and name else path.stem


def experiment_info(workspace: Path, path: Path, run_counts: dict[str, int]) -> dict[str, Any]:
    """``ExperimentInfo`` of an experiment file (``run_counts``: experiment name → runs)."""
    errors = _errors(path)
    raw = _raw_dict(path)
    try:
        spec = ExperimentSpec.model_validate(raw)
        n_phases, total_steps = len(spec.phases), PhasePlan(spec).total_steps
        model = spec.model.name
    except Exception:
        n_phases, total_steps = _count_steps(raw)
        model_raw = raw.get("model")
        model = str(model_raw.get("name") or "") if isinstance(model_raw, dict) else ""
    return {
        "name": path.stem,
        "path": path.relative_to(workspace).as_posix(),
        "description": str(raw.get("description") or ""),
        "hypothesis": str(raw.get("hypothesis") or ""),
        "model": model,
        "n_phases": n_phases,
        "total_steps": total_steps,
        "valid": not errors,
        "errors": errors,
        "n_runs": run_counts.get(_spec_name(raw, path), 0),
    }


def hpo_info(workspace: Path, path: Path, study_counts: dict[str, int]) -> dict[str, Any]:
    """``HpoPlanInfo`` of an HPO plan file (``study_counts``: plan name → studies)."""
    errors = _errors(path)
    raw = _raw_dict(path)
    objectives = raw.get("objectives") if isinstance(raw.get("objectives"), list) else []
    space = raw.get("space") if isinstance(raw.get("space"), dict) else {}
    return {
        "name": path.stem,
        "path": path.relative_to(workspace).as_posix(),
        "experiment": str(raw.get("experiment") or ""),
        "description": str(raw.get("description") or ""),
        "n_params": len(space),
        "objectives": [objective_view(o) for o in objectives if isinstance(o, dict)],
        "valid": not errors,
        "errors": errors,
        "n_studies": study_counts.get(_spec_name(raw, path), 0),
    }


# -- benchmarks & datasets --------------------------------------------------------------------
def benchmark_info(workspace: Path, path: Path) -> dict[str, Any]:
    errors = _errors(path)
    raw = _raw_dict(path)
    try:
        spec = BenchmarkSpec.model_validate(raw)
        data = spec.model_dump(mode="json")
    except Exception:
        data = raw
    attacks = []
    for attack in data.get("attacks") or []:
        if isinstance(attack, dict) and attack.get("name"):
            attacks.append({"label": str(attack.get("label") or attack["name"]),
                            "name": str(attack["name"]),
                            "params": attack.get("params") if isinstance(attack.get("params"), dict)
                            else {}})
    split = data.get("data") if isinstance(data.get("data"), dict) else {}
    params = split.get("params") if isinstance(split.get("params"), dict) else {}
    n_items = split.get("limit") if isinstance(split.get("limit"), int) else params.get("num_items")
    version = data.get("version")
    modality = data.get("modality")
    fpr = data.get("fpr") if isinstance(data.get("fpr"), list) else []
    return {
        "name": path.stem,
        "path": path.relative_to(workspace).as_posix(),
        "modality": modality if modality in MODALITIES else "image",
        "version": version if isinstance(version, int) and not isinstance(version, bool) else 1,
        "description": str(data.get("description") or ""),
        "attacks": attacks,
        "quality_metrics": [str(m) for m in data.get("quality_metrics") or []]
        if isinstance(data.get("quality_metrics"), list) else [],
        "fpr": [float(v) for v in fpr if isinstance(v, (int, float)) and not isinstance(v, bool)],
        "n_items": n_items if isinstance(n_items, int) and not isinstance(n_items, bool) else None,
        "valid": not errors,
        "errors": errors,
    }


def _references(value: Any, ref: str) -> bool:
    if isinstance(value, str):
        return value == ref or value.startswith(ref + "/")
    if isinstance(value, dict):
        return any(_references(v, ref) for v in value.values())
    if isinstance(value, list):
        return any(_references(v, ref) for v in value)
    return False


def dataset_users(workspace: Path) -> dict[str, list[str]]:
    """``{dataset name: [experiment paths using "@name"]}`` (benchmarks included)."""
    files = [p for folder in project_dirs(workspace) for p in yaml_files(folder / "experiments")]
    files += yaml_files(workspace / "benchmarks")
    users: dict[str, list[str]] = {}
    for path in yaml_files(workspace / "datasets"):
        ref = f"@{path.stem}"
        users[path.stem] = [p.relative_to(workspace).as_posix() for p in files
                            if _references(read_raw(p), ref)]
    return users


def dataset_info(workspace: Path, path: Path, used_by: list[str]) -> dict[str, Any]:
    errors = _errors(path)
    raw = _raw_dict(path)
    try:
        data = DatasetCatalogEntry.model_validate(raw).model_dump(mode="json")
    except Exception:
        data = raw
    modality = data.get("modality")
    paths = data.get("paths") if isinstance(data.get("paths"), dict) else {}
    return {
        "name": path.stem,
        "path": path.relative_to(workspace).as_posix(),
        "modality": modality if modality in MODALITIES else "image",
        "description": str(data.get("description") or ""),
        "license": str(data.get("license") or ""),
        "source": str(data.get("source") or ""),
        "paths": {str(k): str(v) for k, v in paths.items()},
        "used_by": used_by,
        "valid": not errors,
        "errors": errors,
    }


# -- validation & phase plan preview ----------------------------------------------------------
def scale_phase_steps(raw: dict[str, Any], scale: float) -> dict[str, Any]:
    """Multiply every phase length by ``scale`` (at least 1 step), as the HPO fidelity proxy."""
    data = copy.deepcopy(raw)
    if scale == 1.0:
        return data
    for phase in data.get("phases") or []:
        if isinstance(phase, dict) and isinstance(phase.get("steps"), (int, float)):
            phase["steps"] = max(1, int(round(phase["steps"] * scale)))
    return data


def fidelity_experiment(hpo_path: Path, spec: HpoSpec) -> ExperimentSpec:
    """Base experiment of an HPO plan with the fidelity proxy applied."""
    base_path = experiment_path_for_hpo(hpo_path, spec)
    raw = read_raw(base_path)
    if not isinstance(raw, dict):
        raise FileNotFoundError(f"expérience de base introuvable : {base_path}")
    data = scale_phase_steps(raw, spec.fidelity.steps_scale)
    data = apply_experiment_overrides(data, dict(spec.fidelity.overrides))
    return ExperimentSpec.model_validate(data)


def plan_preview(content: str | dict[str, Any], kind: str | None,
                 path: Path | None) -> dict[str, Any] | None:
    """``PhasePlanPreview`` of an experiment (or of the fidelity-scaled base of an HPO plan)."""
    try:
        data = yaml.safe_load(content) if isinstance(content, str) else content
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    kind = kind or (kind_from_location(path) if path is not None else None) \
        or detect_kind_from_content(data)
    try:
        if kind == "experiment":
            return PhasePlan(ExperimentSpec.model_validate(data)).preview()
        if kind == "hpo" and path is not None:
            return PhasePlan(fidelity_experiment(path, HpoSpec.model_validate(data))).preview()
    except Exception:
        return None
    return None


def validate_payload(workspace: Path, content: str, kind: str | None,
                     path: str | None) -> dict[str, Any]:
    """``{valid, errors, plan}`` for ``/api/validate``."""
    if kind is not None and kind not in SPEC_MODELS:
        return {"valid": False, "plan": None,
                "errors": [{"loc": "", "msg": f"type de spécification inconnu '{kind}' — "
                                              f"valeurs possibles : {', '.join(SPEC_MODELS)}"}]}
    abs_path: Path | None = None
    if path:
        try:  # the path only gives context (project, base experiment): keep it in the workspace
            abs_path, _ = resolve_workspace_file(workspace, path)
        except FileAccessError:
            abs_path = None
    try:
        errors = [{"loc": str(e.get("loc", "")), "msg": str(e.get("msg", ""))}
                  for e in validate_content(content, kind, abs_path)]
    except Exception as exc:
        log.exception("validation impossible (%s, %s)", kind, path)
        errors = [{"loc": "", "msg": f"validation impossible : {type(exc).__name__}: {exc}"}]
    return {"valid": not errors, "errors": errors, "plan": plan_preview(content, kind, abs_path)}


# -- workspace files --------------------------------------------------------------------------
def resolve_workspace_file(workspace: Path, path: str, *, write: bool = False,
                           data_root: Path | None = None) -> tuple[Path, str]:
    """Return ``(absolute path, workspace-relative posix path)`` or raise FileAccessError.

    The path is resolved (symlinks included) and must stay inside the workspace, outside
    ``.wmlab``/``.venv``/``.git``/``node_modules`` (and the data root); writes are limited to
    ``projects/``, ``benchmarks/``, ``datasets/`` and ``lab/``.
    """
    if not path or not str(path).strip() or "\x00" in str(path):
        raise FileAccessError(400, "Chemin de fichier manquant ou invalide.")
    root = workspace.resolve()
    candidate = Path(str(path).strip()).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise FileAccessError(403, f"Accès refusé : « {path} » est hors du workspace.")
    resolved = _canonical_case(root, resolved)  # APFS is case-insensitive: ``.GIT`` is ``.git``
    rel = resolved.relative_to(root)
    if any(part.lower() in FORBIDDEN_PARTS for part in rel.parts):
        raise FileAccessError(403, f"Accès refusé : « {rel.as_posix()} » est dans un dossier "
                                   "interne (.wmlab, .venv, .git, node_modules).")
    if data_root is not None and _same_or_inside(resolved, data_root.resolve()):
        raise FileAccessError(403, "Accès refusé : les données de l'application ne sont pas "
                                   "modifiables par cette route.")
    if write and rel.parts[0] not in WRITABLE_ROOTS:
        raise FileAccessError(403, "Écriture refusée : seuls les fichiers sous projects/, "
                                   "benchmarks/, datasets/ et lab/ sont modifiables.")
    return resolved, rel.as_posix()


def _canonical_case(root: Path, resolved: Path) -> Path:
    """``resolved`` (inside ``root``) with each component spelled as it exists on disk.

    ``Path.resolve()`` keeps the case typed by the caller; on a case-insensitive file system
    ``.GIT/config`` or ``.Wmlab/runs`` would otherwise escape the name-based checks.
    """
    current = root
    for part in resolved.relative_to(root).parts:
        names = _dir_names(current)
        if part not in names and (current / part).exists():  # case-insensitive hit
            part = next((n for n in names if n.lower() == part.lower()), part)
        current = current / part
    return current


def _dir_names(folder: Path) -> set[str]:
    try:
        return set(os.listdir(folder))
    except OSError:
        return set()


def _same_or_inside(path: Path, base: Path) -> bool:
    """True if ``path`` is ``base`` or below it (case-insensitive on the names)."""
    if path.is_relative_to(base):
        return True
    a, b = [p.lower() for p in path.parts], [p.lower() for p in base.parts]
    return a[:len(b)] == b


def file_kind(path: Path) -> str:
    """Spec kind of a file (``wmlab.spec.detect_kind``), else a coarse type."""
    if path.suffix in YAML_SUFFIXES:
        try:
            kind = detect_kind(path)
        except Exception:
            kind = None
        return kind or "yaml"
    return {".py": "python", ".md": "markdown", ".json": "json", ".sh": "shell"}.get(
        path.suffix.lower(), "text")


def file_validation(path: Path, content: str, kind: str) -> dict[str, Any]:
    """Validation of a spec file (schema + semantics) or syntax check of a Python file."""
    if kind in SPEC_MODELS:
        try:
            errors = [{"loc": str(e.get("loc", "")), "msg": str(e.get("msg", ""))}
                      for e in validate_content(content, kind, path)]
        except Exception as exc:
            log.exception("validation impossible (%s, %s)", kind, path)
            errors = [{"loc": "", "msg": f"validation impossible : {type(exc).__name__}: {exc}"}]
        return _validation(errors)
    if kind == "yaml":
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            loc = f"ligne {mark.line + 1}" if mark is not None else ""
            return _validation([{"loc": loc, "msg": f"YAML invalide : "
                                                    f"{getattr(exc, 'problem', None) or exc}"}])
    if kind == "python":
        try:
            compile(content, str(path), "exec")
        except SyntaxError as exc:
            return _validation([{"loc": f"ligne {exc.lineno}" if exc.lineno else "",
                                 "msg": f"erreur de syntaxe Python : {exc.msg}"}])
    return _validation([])


def read_text_file(path: Path) -> str:
    """Read a UTF-8 text file (≤ 1 Mo) or raise FileAccessError."""
    if not path.is_file():
        raise FileAccessError(404, "Fichier introuvable.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise FileAccessError(413, "Fichier trop volumineux (1 Mo maximum).")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise FileAccessError(415, "Fichier binaire ou non UTF-8 : lecture impossible.") from None
