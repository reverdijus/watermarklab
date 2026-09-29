"""Flattened run parameters (``run.json.params``, §6.1)."""

from __future__ import annotations

from typing import Any

from wmlab.spec.loader import get_path, parse_override_value

_SCALARS = (str, int, float, bool, type(None))


def _is_scalar(value: Any) -> bool:
    return isinstance(value, _SCALARS)


def _add_scalars(out: dict[str, Any], prefix: str, node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _add_scalars(out, f"{prefix}.{key}", value)
    elif _is_scalar(node):
        out[prefix] = node


def flatten_params(spec_dict: dict[str, Any], overrides: dict[str, Any] | None = None
                   ) -> dict[str, Any]:
    """Return the dotted scalars of ``model``, ``optimizer``, ``data.batch_size``,
    ``loss.terms.*.weight``, ``phases.*.steps``, ``seed`` and of every override key."""
    out: dict[str, Any] = {}
    spec = spec_dict or {}
    _add_scalars(out, "model", spec.get("model") or {})
    _add_scalars(out, "optimizer", spec.get("optimizer") or {})
    data = spec.get("data") or {}
    if isinstance(data, dict) and _is_scalar(data.get("batch_size")) and "batch_size" in data:
        out["data.batch_size"] = data["batch_size"]
    terms = ((spec.get("loss") or {}).get("terms") or {}) if isinstance(spec.get("loss"), dict) else {}
    for term, term_spec in terms.items():
        if isinstance(term_spec, dict) and _is_scalar(term_spec.get("weight")) \
                and "weight" in term_spec:
            out[f"loss.terms.{term}.weight"] = term_spec["weight"]
    for i, phase in enumerate(spec.get("phases") or []):
        if isinstance(phase, dict) and "steps" in phase:
            out[f"phases.{i}.steps"] = phase["steps"]
    if "seed" in spec and _is_scalar(spec["seed"]):
        out["seed"] = spec["seed"]
    for key, raw in (overrides or {}).items():
        try:
            value = get_path(spec, key)
        except KeyError:
            value = parse_override_value(raw)
        out[key] = value if _is_scalar(value) else str(raw)
    return out
