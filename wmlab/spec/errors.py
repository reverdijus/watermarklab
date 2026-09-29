"""Readable (French) validation errors."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

# loc items inserted by pydantic for unions / tagged unions / wrappers
_INTERNAL_LOC = re.compile(
    r"^(~.*|int|float|str|bool|none|list\[.*\]|dict\[.*\]|tuple\[.*\]|function-.*|"
    r"tagged-union\[.*\]|union\[.*\]|lax-.*|nullable\[.*\]|constrained-.*|json-or-python\[.*\]|"
    r"[A-Z][A-Za-z]*(Spec|Value|Entry|Attack|Term|Settings|Objective|Objectives|Constraint|"
    r"Space|File))$"
)

_TYPE_MESSAGES: dict[str, str] = {
    "missing": "champ requis manquant",
    "extra_forbidden": "champ inconnu (non autorisé ici)",
    "int_type": "doit être un entier",
    "int_parsing": "doit être un entier",
    "int_from_float": "doit être un entier (sans décimale)",
    "float_type": "doit être un nombre",
    "float_parsing": "doit être un nombre",
    "bool_type": "doit être un booléen (true/false)",
    "bool_parsing": "doit être un booléen (true/false)",
    "string_type": "doit être une chaîne de caractères",
    "list_type": "doit être une liste",
    "tuple_type": "doit être une liste",
    "dict_type": "doit être un dictionnaire (clé: valeur)",
    "model_type": "doit être un dictionnaire (clé: valeur)",
    "model_attributes_type": "doit être un dictionnaire (clé: valeur)",
    "none_required": "doit être null",
    "finite_number": "doit être un nombre fini",
}


class SpecError(ValueError):
    """Invalid YAML spec; ``errors`` is a list of ``{"loc", "msg"}``."""

    def __init__(self, errors: list[dict[str, str]], path: str | Path | None = None,
                 kind: str | None = None) -> None:
        self.errors = errors
        self.path = str(path) if path is not None else None
        self.kind = kind
        super().__init__(self._format())

    def _format(self) -> str:
        where = f" ({self.path})" if self.path else ""
        lines = [f"spécification invalide{where} :"]
        for err in self.errors[:20]:
            loc = err.get("loc") or "(racine)"
            lines.append(f"  - {loc} : {err.get('msg')}")
        if len(self.errors) > 20:
            lines.append(f"  … et {len(self.errors) - 20} autre(s) erreur(s)")
        return "\n".join(lines)


def join_loc(parts: Iterable[Any]) -> str:
    """Join loc parts into a dotted path, dropping pydantic-internal items."""
    return ".".join(str(p) for p in parts if not (isinstance(p, str) and _INTERNAL_LOC.match(p)))


def _short_input(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def translate_error(err: dict[str, Any]) -> str:
    """Translate one pydantic error dict into a French message."""
    etype = err.get("type", "")
    ctx = err.get("ctx") or {}
    value = err.get("input")
    if etype in ("value_error", "assertion_error"):
        msg = str(ctx.get("error") or err.get("msg", ""))
        return re.sub(r"^(Value error|Assertion failed), ", "", msg)
    if etype == "literal_error":
        expected = str(ctx.get("expected", "")).replace(" or ", ", ")
        return f"valeur invalide {_short_input(value)} — valeurs possibles : {expected}"
    if etype == "enum":
        return f"valeur invalide {_short_input(value)} — valeurs possibles : {ctx.get('expected')}"
    if etype == "greater_than":
        return f"doit être > {ctx.get('gt')} (reçu : {_short_input(value)})"
    if etype == "greater_than_equal":
        return f"doit être ≥ {ctx.get('ge')} (reçu : {_short_input(value)})"
    if etype == "less_than":
        return f"doit être < {ctx.get('lt')} (reçu : {_short_input(value)})"
    if etype == "less_than_equal":
        return f"doit être ≤ {ctx.get('le')} (reçu : {_short_input(value)})"
    if etype == "too_short":
        min_len = ctx.get("min_length", 1)
        return f"doit contenir au moins {min_len} élément(s)"
    if etype == "too_long":
        return f"doit contenir au plus {ctx.get('max_length')} élément(s)"
    if etype == "string_pattern_mismatch":
        return (f"format invalide {_short_input(value)} — lettres, chiffres, '_', '-' ou '.' "
                "uniquement (commençant par une lettre ou un chiffre)")
    if etype == "string_too_short":
        return "ne doit pas être vide"
    if etype in _TYPE_MESSAGES:
        msg = _TYPE_MESSAGES[etype]
        if etype not in ("missing", "extra_forbidden"):
            msg += f" (reçu : {_short_input(value)})"
        return msg
    if etype.startswith("union_tag") or etype == "scheduled_value":
        return str(err.get("msg", ""))
    return str(err.get("msg", etype))


def format_validation_error(exc: ValidationError, prefix: str = "") -> list[dict[str, str]]:
    """Convert a pydantic ``ValidationError`` into ``[{"loc", "msg"}]`` (deduplicated)."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for err in exc.errors(include_url=False):
        loc = join_loc(err.get("loc", ()))
        if prefix:
            loc = f"{prefix}.{loc}" if loc else prefix
        msg = translate_error(err)
        if (loc, msg) in seen:
            continue
        seen.add((loc, msg))
        out.append({"loc": loc, "msg": msg})
    return out
