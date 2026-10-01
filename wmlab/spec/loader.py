"""Loading YAML specs, overrides and path helpers (ARCHITECTURE.md §5)."""

from __future__ import annotations

import copy
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from wmlab.spec.errors import SpecError, format_validation_error
from wmlab.spec.models import (
    SPEC_MODELS,
    BenchmarkSpec,
    DatasetCatalogEntry,
    ExperimentSpec,
    HostSpec,
    HpoSpec,
    ProjectSpec,
)

KINDS: tuple[str, ...] = tuple(SPEC_MODELS)
YAML_SUFFIXES = (".yaml", ".yml")
_NUMERIC_RE = re.compile(r"^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$")


class OverrideError(ValueError):
    """Invalid ``--set`` override path or value."""


# -- YAML I/O -------------------------------------------------------------------------------
def read_yaml(path: str | Path) -> Any:
    """Read a YAML file (``SpecError`` with a French message on syntax errors)."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SpecError([{"loc": "", "msg": f"fichier introuvable : {path}"}], path) from None
    return parse_yaml(text, path)


def parse_yaml(text: str, path: str | Path | None = None) -> Any:
    """Parse YAML text (``SpecError`` on syntax errors)."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError([yaml_error(exc)], path) from None


def yaml_error(exc: yaml.YAMLError) -> dict[str, str]:
    """Convert a YAML syntax error into ``{"loc", "msg"}``."""
    mark = getattr(exc, "problem_mark", None)
    where = f" (ligne {mark.line + 1}, colonne {mark.column + 1})" if mark is not None else ""
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    return {"loc": "", "msg": f"YAML invalide{where} : {problem}"}


def dump_yaml(data: Any) -> str:
    """Dump data as readable YAML (key order kept, unicode allowed)."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def write_text_atomic(path: str | Path, text: str) -> None:
    """Write text atomically (temporary file + rename)."""
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


# -- kind detection -------------------------------------------------------------------------
def detect_kind(path: str | Path) -> str | None:
    """Guess the spec kind of a file from its location, else from its content."""
    kind = kind_from_location(path)
    if kind is not None:
        return kind
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    return detect_kind_from_content(data)


def kind_from_location(path: str | Path) -> str | None:
    """Detect the kind from the path only (no file access)."""
    p = Path(path)
    if p.suffix.lower() not in YAML_SUFFIXES:
        return None
    parts = p.parts
    parent = parts[-2] if len(parts) >= 2 else ""
    grand = parts[-3] if len(parts) >= 3 else ""
    great = parts[-4] if len(parts) >= 4 else ""
    if p.name == "workspace.yaml":
        return "workspace"
    if p.name == "hosts.yaml":
        return "hosts"
    if p.name == "project.yaml" and grand == "projects":
        return "project"
    if parent == "experiments" and great == "projects":
        return "experiment"
    if parent == "hpo" and great == "projects":
        return "hpo"
    if parent == "benchmarks":
        return "benchmark"
    if parent == "datasets":
        return "dataset"
    return None


def detect_kind_from_content(data: Any) -> str | None:
    """Heuristic detection of the kind from parsed YAML content."""
    if not isinstance(data, dict):
        return None
    keys = set(data)
    if "hosts" in keys:
        return "hosts"
    if "phases" in keys or ("model" in keys and "loss" in keys):
        return "experiment"
    if "space" in keys or ("experiment" in keys and "objectives" in keys):
        return "hpo"
    if "attacks" in keys and ("fpr" in keys or "quality_metrics" in keys or "version" in keys):
        return "benchmark"
    if "paths" in keys:
        return "dataset"
    if "message_bits" in keys or ("modality" in keys and "objectives" in keys):
        return "project"
    if "budget" in keys or "data_dir" in keys or "default_host" in keys:
        return "workspace"
    return None


# -- loading --------------------------------------------------------------------------------
def validate_data(data: Any, kind: str, path: str | Path | None = None) -> BaseModel:
    """Validate parsed YAML data against the model of ``kind`` (``SpecError`` if invalid)."""
    if kind not in SPEC_MODELS:
        raise SpecError([{"loc": "", "msg": f"type de spécification inconnu '{kind}' — "
                                            f"valeurs possibles : {', '.join(KINDS)}"}], path)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SpecError([{"loc": "", "msg": "le fichier doit contenir un dictionnaire YAML "
                                            "(clé: valeur)"}], path, kind)
    try:
        return SPEC_MODELS[kind].model_validate(data)
    except ValidationError as exc:
        raise SpecError(format_validation_error(exc), path, kind) from None


def load_yaml(path: str | Path, kind: str | None = None) -> Any:
    """Load and validate a YAML spec; ``kind`` is detected when omitted."""
    path = Path(path)
    data = read_yaml(path)
    kind = kind or kind_from_location(path) or detect_kind_from_content(data)
    if kind is None:
        raise SpecError([{"loc": "", "msg": "type de fichier inconnu : impossible de "
                                            "déterminer s'il s'agit d'un projet, d'une "
                                            "expérience, d'un plan HPO…"}], path)
    return validate_data(data, kind, path)


# -- overrides ------------------------------------------------------------------------------
def parse_override_value(value: Any) -> Any:
    """Parse a CLI override value as YAML (numbers such as ``3e-4`` become floats)."""
    if not isinstance(value, str):
        return copy.deepcopy(value)
    if value.strip() == "":
        return ""
    try:
        parsed = yaml.safe_load(value)
    except yaml.YAMLError:
        return value
    if isinstance(parsed, str) and _NUMERIC_RE.match(parsed.strip()):
        return float(parsed)  # e.g. "3e-4", which YAML 1.1 leaves as a string
    return parsed


def _describe_keys(container: Any) -> str:
    if isinstance(container, dict):
        keys = [str(k) for k in container]
        return f"clés disponibles : {', '.join(keys) if keys else '(aucune)'}"
    if isinstance(container, list):
        return f"la liste a {len(container)} élément(s)"
    return f"valeur de type {type(container).__name__}"


def _list_index(container: list, part: str, path: str, where: str) -> int:
    if re.fullmatch(r"\d+", part):
        index = int(part)
        if index >= len(container):
            raise OverrideError(
                f"override '{path}' invalide : index {index} hors bornes "
                f"('{where or '(racine)'}' a {len(container)} élément(s))"
            )
        return index
    for i, item in enumerate(container):
        if isinstance(item, dict) and part in (item.get("name"), item.get("label")):
            return i
    raise OverrideError(
        f"override '{path}' invalide : '{part}' n'est ni un index numérique ni le nom d'un "
        f"élément de '{where or '(racine)'}' ({_describe_keys(container)})"
    )


def apply_overrides(spec_dict: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Return a deep copy of ``spec_dict`` with dotted-path overrides applied.

    List items are addressed by numeric index (or by their ``name``/``label``); a missing
    final key in a dict is created; values given as strings are parsed as YAML.
    """
    result = copy.deepcopy(spec_dict) if spec_dict is not None else {}
    for path, raw_value in (overrides or {}).items():
        parts = str(path).strip().split(".")
        if not str(path).strip() or any(p == "" for p in parts):
            raise OverrideError(f"override '{path}' invalide : chemin vide ou mal formé")
        value = parse_override_value(raw_value)
        node: Any = result
        where = ""
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if isinstance(node, dict):
                if last:
                    node[part] = value
                    break
                if part not in node:
                    raise OverrideError(
                        f"override '{path}' invalide : clé '{part}' introuvable dans "
                        f"'{where or '(racine)'}' ({_describe_keys(node)})"
                    )
                child = node[part]
            elif isinstance(node, list):
                index = _list_index(node, part, path, where)
                if last:
                    node[index] = value
                    break
                child = node[index]
            else:
                raise OverrideError(
                    f"override '{path}' invalide : '{where}' n'est ni un dictionnaire ni une "
                    f"liste ({_describe_keys(node)})"
                )
            if not isinstance(child, (dict, list)):
                raise OverrideError(
                    f"override '{path}' invalide : '{where + '.' if where else ''}{part}' vaut "
                    f"{child!r} et ne contient pas de sous-clés"
                )
            node = child
            where = f"{where}.{part}" if where else part
    return result


def apply_experiment_overrides(raw: dict[str, Any], overrides: dict[str, Any] | None
                               ) -> dict[str, Any]:
    """Apply overrides to a raw experiment dict, falling back to its defaults-filled form.

    This allows e.g. ``resources.device=cpu`` when ``resources`` is absent from the YAML.
    """
    try:
        return apply_overrides(raw, overrides)
    except OverrideError as original:
        try:
            resolved = ExperimentSpec.model_validate(raw).model_dump(mode="json")
        except ValidationError:
            raise original from None
        return apply_overrides(resolved, overrides)


def get_path(data: Any, path: str) -> Any:
    """Return the value at a dotted path (``KeyError`` if unresolvable)."""
    node = data
    for part in str(path).split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[_list_index(node, part, path, "")]
            except OverrideError as exc:
                raise KeyError(str(exc)) from None
        else:
            raise KeyError(path)
    return node


def parse_set_options(items: list[str] | None) -> dict[str, str]:
    """Parse repeated ``k=v`` CLI options into a dict (later values win)."""
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise OverrideError(f"--set '{item}' invalide : format attendu clé=valeur")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise OverrideError(f"--set '{item}' invalide : clé vide")
        out[key] = value
    return out


# -- workspace-relative helpers ------------------------------------------------------------
def _workspace() -> Path:
    from wmlab.paths import workspace_root

    return workspace_root()


def resolve_in_workspace(path: str | Path) -> Path:
    """Resolve a path relative to the workspace (absolute paths are kept)."""
    p = Path(path).expanduser()
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    try:
        return (_workspace() / p).resolve()
    except Exception:
        return p.resolve()


def find_project_dir(spec_path: str | Path) -> Path | None:
    """Return the closest directory above ``spec_path`` containing ``project.yaml``."""
    p = resolve_in_workspace(spec_path)
    start = p if p.is_dir() else p.parent
    for candidate in (start, *start.parents):
        if (candidate / "project.yaml").is_file():
            return candidate
        if (candidate / "workspace.yaml").is_file():
            break
    return None


def project_for(spec_path: str | Path) -> tuple[Path, ProjectSpec]:
    """Return ``(project_dir, ProjectSpec)`` for a file inside ``projects/<p>/``."""
    project_dir = find_project_dir(spec_path)
    if project_dir is None:
        raise SpecError([{"loc": "", "msg": "aucun project.yaml trouvé au-dessus de ce fichier "
                                            "(attendu : projects/<projet>/...)"}], spec_path)
    return project_dir, load_yaml(project_dir / "project.yaml", "project")


def experiment_path_for_hpo(hpo_path: str | Path, hpo_spec: HpoSpec) -> Path:
    """Return the path of the base experiment of an HPO plan (may not exist)."""
    project_dir = find_project_dir(hpo_path)
    base = project_dir if project_dir is not None else resolve_in_workspace(hpo_path).parent.parent
    ref = hpo_spec.experiment
    if ref.endswith(YAML_SUFFIXES) or "/" in ref:
        candidate = Path(ref).expanduser()
        if candidate.is_absolute():
            return candidate
        return (base / candidate).resolve() if (base / candidate).exists() \
            else resolve_in_workspace(candidate)
    for suffix in YAML_SUFFIXES:
        candidate = base / "experiments" / f"{ref}{suffix}"
        if candidate.is_file():
            return candidate
    return base / "experiments" / f"{ref}.yaml"


def load_experiment(spec_path: str | Path, overrides: dict[str, Any] | None = None
                    ) -> tuple[ExperimentSpec, ProjectSpec, dict[str, Any]]:
    """Load an experiment with overrides; return ``(spec, project, resolved_dict)``.

    ``resolved_dict`` is the fully resolved spec (defaults filled, JSON types), as written
    to ``spec.yaml`` in the run folder.
    """
    path = resolve_in_workspace(spec_path)
    raw = read_yaml(path)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SpecError([{"loc": "", "msg": "le fichier doit contenir un dictionnaire YAML"}], path)
    data = apply_experiment_overrides(raw, overrides)
    spec = validate_data(data, "experiment", path)
    _, project = project_for(path)
    return spec, project, spec.model_dump(mode="json")


def list_benchmarks(root: Path | None = None) -> list[str]:
    """Return the names of the benchmark files of the workspace."""
    root = root or _workspace()
    folder = root / "benchmarks"
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.iterdir() if p.suffix in YAML_SUFFIXES)


def benchmark_path(name: str, root: Path | None = None) -> Path | None:
    """Return the path of ``benchmarks/<name>.yaml`` if it exists."""
    root = root or _workspace()
    for suffix in YAML_SUFFIXES:
        candidate = root / "benchmarks" / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def load_benchmark(name_or_path: str | Path) -> BenchmarkSpec:
    """Load a benchmark by name (``benchmarks/<name>.yaml``) or by path."""
    text = str(name_or_path)
    candidate = Path(text).expanduser()
    if text.endswith(YAML_SUFFIXES) or candidate.is_file():
        return load_yaml(resolve_in_workspace(candidate), "benchmark")
    path = benchmark_path(text)
    if path is None:
        available = list_benchmarks()
        raise SpecError([{"loc": "", "msg": f"benchmark inconnu '{text}' — disponibles : "
                                            f"{', '.join(available) if available else '(aucun)'}"}])
    return load_yaml(path, "benchmark")


def dataset_catalog_path(name: str, root: Path | None = None) -> Path | None:
    """Return the path of ``datasets/<name>.yaml`` if it exists."""
    root = root or _workspace()
    for suffix in YAML_SUFFIXES:
        candidate = root / "datasets" / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def load_dataset_entry(name: str, root: Path | None = None) -> DatasetCatalogEntry:
    """Load the catalog entry ``datasets/<name>.yaml`` (name without ``@``)."""
    name = name.lstrip("@")
    path = dataset_catalog_path(name, root)
    if path is None:
        raise SpecError([{"loc": "", "msg": f"dataset '@{name}' absent du catalogue "
                                            f"(fichier datasets/{name}.yaml introuvable)"}])
    return load_yaml(path, "dataset")


# -- hosts ----------------------------------------------------------------------------------
DEFAULT_LOCAL_HOST: dict[str, Any] = {
    "name": "local", "kind": "local", "price_eur_per_hour": 0.0, "max_concurrent_jobs": 1,
}


def _hosts_file(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    from wmlab.paths import hosts_path

    return hosts_path()


def load_hosts(path: str | Path | None = None) -> list[HostSpec]:
    """Load ``.wmlab/hosts.yaml`` (created with the ``local`` host when missing)."""
    file = _hosts_file(path)
    if not file.is_file():
        hosts = [HostSpec.model_validate(DEFAULT_LOCAL_HOST)]
        save_hosts(hosts, file)
        return hosts
    data = read_yaml(file) or {}
    if isinstance(data, list):
        data = {"hosts": data}
    hosts = validate_data(data, "hosts", file).hosts
    if not any(h.kind == "local" for h in hosts) and not any(h.name == "local" for h in hosts):
        hosts.insert(0, HostSpec.model_validate(DEFAULT_LOCAL_HOST))
    return hosts


def save_hosts(hosts: list[HostSpec | dict[str, Any]], path: str | Path | None = None) -> Path:
    """Validate and write the hosts file atomically; return its path."""
    file = _hosts_file(path)
    payload = {"hosts": [h.model_dump(mode="json") if isinstance(h, BaseModel) else h
                         for h in hosts]}
    validated = validate_data(payload, "hosts", file)
    dumped = {"hosts": [h.model_dump(mode="json", exclude_none=True) for h in validated.hosts]}
    write_text_atomic(file, dump_yaml(dumped))
    return file
