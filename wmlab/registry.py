"""Component registries and plugin discovery (ARCHITECTURE.md §3).

A registry maps a *name* to exactly one component. A component usable with several
modalities declares ``modality=[...]`` (or ``None`` for all) and dispatches internally
(e.g. on ``x.ndim``); registering the same name twice for different modalities is an error.
"""

from __future__ import annotations

import difflib
import importlib
import inspect
import pkgutil
import sys
import threading
import traceback
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence, TypeVar

from pydantic import BaseModel

T = TypeVar("T")

MODALITIES: tuple[str, ...] = ("image", "audio", "video")

# French labels used in user-facing messages: (noun, feminine?)
KIND_LABELS: dict[str, tuple[str, bool]] = {
    "model": ("modèle", False),
    "attack": ("attaque", True),
    "dataset": ("dataset", False),
    "preprocess": ("prétraitement", False),
    "loss": ("perte", True),
    "metric": ("métrique", True),
}

# Parameters never exposed in a derived params schema.
_HIDDEN_PARAMS = {"self", "nbits"}


def kind_label(kind: str) -> str:
    """Return the French noun for a registry kind."""
    return KIND_LABELS.get(kind, (kind, False))[0]


def _unknown_word(kind: str) -> str:
    return "inconnue" if KIND_LABELS.get(kind, (kind, False))[1] else "inconnu"


def normalize_modality(modality: str | Sequence[str] | None) -> tuple[str, ...] | None:
    """Normalize a modality declaration to a tuple (``None`` = all modalities)."""
    if modality is None:
        return None
    items = [modality] if isinstance(modality, str) else list(modality)
    out: list[str] = []
    for item in items:
        if item in ("all", "any", "*"):
            return None
        if item not in MODALITIES:
            raise ValueError(
                f"modalité invalide '{item}' — valeurs possibles : {', '.join(MODALITIES)}"
            )
        if item not in out:
            out.append(item)
    return tuple(out) or None


def first_paragraph(doc: str | None) -> str:
    """Return the first paragraph of a docstring, on one line."""
    if not doc:
        return ""
    cleaned = inspect.cleandoc(doc)
    paragraph = cleaned.split("\n\n", 1)[0]
    return " ".join(line.strip() for line in paragraph.splitlines()).strip()


class UnknownComponentError(KeyError):
    """KeyError with a readable French message (lists close names)."""

    def __init__(self, message: str, *, kind: str = "", name: str = "",
                 suggestions: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.name = name
        self.suggestions = suggestions or []

    def __str__(self) -> str:
        return self.message


@dataclass
class Entry:
    """A registered component."""

    name: str
    kind: str
    obj: Any
    modality: tuple[str, ...] | None
    params_model: type[BaseModel] | None
    description: str
    source: str
    builtin: bool

    def supports(self, modality: str | None) -> bool:
        """Return True if the component can be used with ``modality``."""
        return modality is None or self.modality is None or modality in self.modality

    def modality_label(self) -> str:
        """Return a human readable modality list."""
        return "toutes" if self.modality is None else ", ".join(self.modality)


class Registry:
    """Name → component registry for one kind (model, attack, dataset…)."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self._entries: dict[str, Entry] = {}
        self._lock = threading.RLock()

    # -- registration -------------------------------------------------------------------
    def register(self, name: str, *, modality: str | Sequence[str] | None = None,
                 params: type[BaseModel] | None = None, description: str | None = None,
                 override: bool = False) -> Callable[[T], T]:
        """Decorator registering a class or function under ``name``.

        Re-registering an existing name raises ``ValueError`` unless it is the same object
        (re-import of the same module/qualname) or ``override=True`` (plugins overriding a
        builtin).
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"nom de {kind_label(self.kind)} invalide : {name!r}")
        mods = normalize_modality(modality)
        if params is not None and not (isinstance(params, type) and issubclass(params, BaseModel)):
            raise TypeError("`params` doit être une classe pydantic BaseModel")

        def decorator(obj: T) -> T:
            source = getattr(obj, "__module__", None) or "?"
            entry = Entry(
                name=name,
                kind=self.kind,
                obj=obj,
                modality=mods,
                params_model=params,
                description=description if description is not None else first_paragraph(
                    getattr(obj, "__doc__", None)),
                source=source,
                builtin=source.startswith("wmlab."),
            )
            with self._lock:
                existing = self._entries.get(name)
                if existing is not None and not override and not _same_object(existing.obj, obj):
                    raise ValueError(
                        f"{kind_label(self.kind)} '{name}' déjà enregistré(e) par "
                        f"{existing.source} (modalité : {existing.modality_label()}). Un nom "
                        "désigne un seul composant : choisissez un autre nom, déclarez "
                        "modality=[...] pour un composant multi-modal, ou utilisez "
                        "override=True pour surcharger volontairement."
                    )
                self._entries[name] = entry
            return obj

        return decorator

    def unregister(self, name: str) -> None:
        """Remove ``name`` if present (mostly for tests)."""
        with self._lock:
            self._entries.pop(name, None)

    # -- lookup --------------------------------------------------------------------------
    def get(self, name: str) -> Entry:
        """Return the entry for ``name`` or raise a KeyError listing close names."""
        with self._lock:
            entry = self._entries.get(name)
            names = sorted(self._entries)
        if entry is not None:
            return entry
        close = difflib.get_close_matches(str(name), names, n=3, cutoff=0.5)
        msg = f"{kind_label(self.kind)} {_unknown_word(self.kind)} '{name}'"
        if close:
            msg += f" — vouliez-vous dire : {', '.join(close)} ?"
        msg += f" — disponibles : {', '.join(names) if names else '(aucun)'}"
        raise UnknownComponentError(msg, kind=self.kind, name=str(name), suggestions=close)

    def has(self, name: str) -> bool:
        """Return True if ``name`` is registered."""
        return name in self._entries

    def __contains__(self, name: object) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def names(self, modality: str | None = None) -> list[str]:
        """Return the sorted names compatible with ``modality``."""
        return [e.name for e in self.list(modality)]

    def list(self, modality: str | None = None) -> list[Entry]:
        """Return entries (sorted by name), filtered by modality when given."""
        with self._lock:
            entries = list(self._entries.values())
        return sorted((e for e in entries if e.supports(modality)), key=lambda e: e.name)

    # -- construction --------------------------------------------------------------------
    def validate_params(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Validate ``params`` with the entry's params model; return the kwargs to pass.

        Keys unknown to the params model (e.g. ``nbits`` for models) are passed through.
        """
        entry = self.get(name)
        if entry.params_model is None:
            return dict(params)
        fields = entry.params_model.model_fields
        known = {k: v for k, v in params.items() if k in fields}
        passthrough = {k: v for k, v in params.items() if k not in fields}
        validated = entry.params_model.model_validate(known).model_dump()
        return {**validated, **passthrough}

    def build(self, name: str, /, **params: Any) -> Any:
        """Validate ``params`` (params model if any) then call ``obj(**params)``."""
        entry = self.get(name)
        return entry.obj(**self.validate_params(name, params))

    # -- introspection -------------------------------------------------------------------
    def describe(self) -> list[dict[str, Any]]:
        """Return JSON-friendly descriptions of all entries."""
        return [describe_entry(e) for e in self.list()]

    def snapshot(self) -> dict[str, Entry]:
        """Return a shallow copy of the entries (tests)."""
        with self._lock:
            return dict(self._entries)

    def restore(self, snapshot: dict[str, Entry]) -> None:
        """Replace the entries with ``snapshot`` (tests)."""
        with self._lock:
            self._entries.clear()
            self._entries.update(snapshot)

    def __repr__(self) -> str:
        return f"Registry({self.kind!r}, {len(self)} entries)"


def _same_object(a: Any, b: Any) -> bool:
    if a is b:
        return True
    return (getattr(a, "__module__", None), getattr(a, "__qualname__", None)) == (
        getattr(b, "__module__", None), getattr(b, "__qualname__", None)
    ) and getattr(a, "__qualname__", None) is not None


# -- params schema derivation ---------------------------------------------------------------
_JSON_TYPES: dict[type, str] = {
    bool: "boolean", int: "integer", float: "number", str: "string",
    list: "array", tuple: "array", set: "array", dict: "object",
}


def _json_type_of(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = list(typing.get_args(annotation))
        non_none = [a for a in args if a is not type(None)]
        schemas = [_json_type_of(a) for a in non_none]
        if type(None) in args:
            schemas.append({"type": "null"})
        schemas = [s for s in schemas if s]
        return {"anyOf": schemas} if len(schemas) > 1 else (schemas[0] if schemas else {})
    if origin is typing.Literal:
        return {"enum": list(typing.get_args(annotation))}
    base = origin or annotation
    if isinstance(base, type):
        for py_type, json_type in _JSON_TYPES.items():
            if issubclass(base, py_type):
                return {"type": json_type}
    return {}


def _json_default(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_default(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_default(v) for k, v in value.items()}
    return repr(value)


def signature_schema(obj: Any, *, skip_leading: int = 0) -> dict[str, Any] | None:
    """Derive a simple JSON schema from a class ``__init__`` or function signature."""
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return None
    try:
        target = obj.__init__ if inspect.isclass(obj) else obj
        hints = typing.get_type_hints(target)
    except Exception:
        hints = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    positional_seen = 0
    for pname, param in sig.parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD) \
                and positional_seen < skip_leading:
            positional_seen += 1
            continue
        if pname in _HIDDEN_PARAMS or param.kind == param.POSITIONAL_ONLY:
            continue
        annotation = hints.get(pname, param.annotation)
        if isinstance(annotation, str):
            annotation = inspect.Parameter.empty
        prop = _json_type_of(annotation)
        if not prop and param.default is not inspect.Parameter.empty \
                and param.default is not None:
            prop = _json_type_of(type(param.default))
        prop = {"title": pname.replace("_", " ").capitalize(), **prop}
        if param.default is inspect.Parameter.empty:
            required.append(pname)
        else:
            prop["default"] = _json_default(param.default)
        properties[pname] = prop
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def params_schema(entry: Entry) -> dict[str, Any] | None:
    """Return the JSON schema of an entry's parameters."""
    if entry.params_model is not None:
        try:
            return entry.params_model.model_json_schema()
        except Exception:
            return None
    # metrics are called as fn(x, x_w, **ctx): the first two args are inputs, not params
    return signature_schema(entry.obj, skip_leading=2 if entry.kind == "metric" else 0)


def describe_entry(entry: Entry) -> dict[str, Any]:
    """Return the JSON description of one entry (``RegistryEntry`` in §9)."""
    return {
        "name": entry.name,
        "kind": entry.kind,
        "modality": list(entry.modality) if entry.modality is not None else None,
        "description": entry.description,
        "params_schema": params_schema(entry),
        "source": entry.source,
        "builtin": entry.builtin,
    }


MODELS = Registry("model")
ATTACKS = Registry("attack")
DATASETS = Registry("dataset")
PREPROCESS = Registry("preprocess")
LOSSES = Registry("loss")
METRICS = Registry("metric")
REGISTRIES: dict[str, Registry] = {
    "model": MODELS, "attack": ATTACKS, "dataset": DATASETS,
    "preprocess": PREPROCESS, "loss": LOSSES, "metric": METRICS,
}


def get_registry(kind: str) -> Registry:
    """Return the registry for ``kind`` (accepts plurals such as ``models``)."""
    key = kind.strip().lower()
    aliases = {"models": "model", "attacks": "attack", "datasets": "dataset",
               "losses": "loss", "metrics": "metric", "preprocessing": "preprocess"}
    key = aliases.get(key, key)
    if key not in REGISTRIES:
        raise ValueError(
            f"type de composant inconnu '{kind}' — valeurs possibles : {', '.join(REGISTRIES)}"
        )
    return REGISTRIES[key]


# -- plugin discovery -----------------------------------------------------------------------
@dataclass
class PluginReport:
    """Result of :func:`load_plugins`."""

    loaded: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"loaded": list(self.loaded), "errors": [dict(e) for e in self.errors]}


_LOAD_LOCK = threading.RLock()
_last_report: PluginReport | None = None


def last_report() -> PluginReport | None:
    """Return the report of the last :func:`load_plugins` call, if any."""
    return _last_report


def short_traceback(limit: int = 3) -> str:
    """Return the current exception with its last ``limit`` frames."""
    return traceback.format_exc(limit=-limit).strip()


def _purge_lab_modules() -> None:
    """Forget previously imported ``lab`` modules and the entries they registered."""
    for mod_name in [m for m in sys.modules if m == "lab" or m.startswith("lab.")]:
        del sys.modules[mod_name]
    for registry in REGISTRIES.values():
        for entry in registry.list():
            if entry.source == "lab" or entry.source.startswith("lab."):
                registry.unregister(entry.name)


def _import_tree(module_name: str, report: PluginReport) -> None:
    try:
        module = importlib.import_module(module_name)
    except Exception:
        report.errors.append({"module": module_name, "error": short_traceback()})
        return
    report.loaded.append(module_name)
    path = getattr(module, "__path__", None)
    if not path:
        return
    for info in sorted(pkgutil.iter_modules(path, prefix=module_name + "."),
                       key=lambda i: i.name):
        _import_tree(info.name, report)


def load_plugins(workspace: Path | None = None, *, include_builtins: bool = True) -> PluginReport:
    """Import builtin components then the workspace ``lab`` package (recursively).

    Idempotent; import errors are collected in the report, never raised.
    """
    global _last_report
    report = PluginReport()
    with _LOAD_LOCK:
        if include_builtins:
            try:
                from wmlab import builtins as _builtins

                errors = _builtins.load()
                failed = {e["module"] for e in errors}
                report.loaded.extend(m for m in _builtins.BUILTIN_MODULES if m not in failed)
                report.errors.extend(errors)
            except Exception:
                report.errors.append({"module": "wmlab.builtins", "error": short_traceback()})

        if workspace is None:
            try:
                from wmlab.paths import workspace_root

                workspace = workspace_root()
            except Exception as exc:
                report.errors.append({"module": "lab", "error": str(exc)})
                _last_report = report
                return report
        workspace = Path(workspace).expanduser().resolve()
        lab_dir = workspace / "lab"
        if not lab_dir.is_dir():
            _last_report = report
            return report

        ws_str = str(workspace)
        if ws_str in sys.path:
            sys.path.remove(ws_str)
        sys.path.insert(0, ws_str)

        current = sys.modules.get("lab")
        if current is not None:
            current_paths = [str(Path(p).resolve()) for p in getattr(current, "__path__", [])]
            if str(lab_dir.resolve()) not in current_paths:
                _purge_lab_modules()
        importlib.invalidate_caches()
        _import_tree("lab", report)
        _last_report = report
        return report
