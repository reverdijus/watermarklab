"""HPO search space: suggestions, grid, trial overrides and fidelity (ARCHITECTURE.md §5.4)."""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import ValidationError

from wmlab.spec import (
    ExperimentSpec,
    FidelitySpec,
    OverrideError,
    ParamSpace,
    apply_overrides,
    format_validation_error,
    parse_override_value,
)

_PRIMITIVES = (type(None), bool, int, float, str)


def _encoded_choices(choices: list[Any]) -> tuple[list[Any], bool]:
    """Optuna only accepts primitive choices: others are encoded as JSON strings."""
    if all(isinstance(c, _PRIMITIVES) for c in choices):
        return list(choices), False
    return [json.dumps(c, sort_keys=True) for c in choices], True


def suggest(trial: Any, space: dict[str, ParamSpace]) -> dict[str, Any]:
    """Sample every dotted path of ``space`` with an Optuna trial."""
    values: dict[str, Any] = {}
    for name, p in space.items():
        if p.type == "float":
            values[name] = trial.suggest_float(name, float(p.low), float(p.high),
                                               step=float(p.step) if p.step else None, log=p.log)
        elif p.type == "int":
            values[name] = trial.suggest_int(name, int(p.low), int(p.high),
                                             step=int(p.step or 1), log=p.log)
        else:
            choices, encoded = _encoded_choices(list(p.choices or []))
            value = trial.suggest_categorical(name, choices)
            values[name] = json.loads(value) if encoded else value
    return values


def decode_params(params: dict[str, Any], space: dict[str, ParamSpace]) -> dict[str, Any]:
    """Decode Optuna params (JSON-encoded categorical choices) into spec values."""
    out: dict[str, Any] = {}
    for name, value in params.items():
        p = space.get(name)
        if p is not None and p.type == "categorical" and isinstance(value, str):
            _, encoded = _encoded_choices(list(p.choices or []))
            if encoded:
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
        out[name] = value
    return out


def grid_search_space(space: dict[str, ParamSpace]) -> dict[str, list[Any]]:
    """Grid of every parameter (floats need a ``step``)."""
    grid: dict[str, list[Any]] = {}
    for name, p in space.items():
        if p.type == "categorical":
            grid[name] = _encoded_choices(list(p.choices or []))[0]
        elif p.type == "int":
            grid[name] = list(range(int(p.low), int(p.high) + 1, int(p.step or 1)))
        else:
            if not p.step:
                raise ValueError(f"sampler grid : le paramètre '{name}' (float) doit définir un "
                                 "'step' pour pouvoir être discrétisé")
            n = int(math.floor((float(p.high) - float(p.low)) / float(p.step) + 1e-9)) + 1
            grid[name] = [round(float(p.low) + i * float(p.step), 12) for i in range(n)]
    return grid


def grid_size(space: dict[str, ParamSpace]) -> int:
    """Number of points of the grid."""
    size = 1
    for values in grid_search_space(space).values():
        size *= max(len(values), 1)
    return size


def to_override_value(value: Any) -> str:
    """Encode a sampled value as a ``--set`` string that parses back to the same value."""
    if isinstance(value, str):
        return value if parse_override_value(value) == value else json.dumps(value)
    return json.dumps(value)


def trial_overrides(base_resolved: dict[str, Any], sampled: dict[str, Any],
                    fidelity: FidelitySpec) -> dict[str, str]:
    """Overrides of one trial: sampled values, then ``steps_scale`` on every phase (``steps``
    and ``lr.warmup_steps``, rounded, at least 1 step — a warmup kept whole would exceed
    the shortened phase), then ``fidelity.overrides`` (which win)."""
    overrides = {name: to_override_value(value) for name, value in sampled.items()}
    scale = float(fidelity.steps_scale)
    if scale != 1.0:
        current = apply_overrides(base_resolved, overrides)
        for i, phase in enumerate(current.get("phases") or []):
            overrides[f"phases.{i}.steps"] = str(max(1, round(int(phase["steps"]) * scale)))
            warmup = int((phase.get("lr") or {}).get("warmup_steps") or 0)
            if warmup > 0:
                overrides[f"phases.{i}.lr.warmup_steps"] = str(max(1, round(warmup * scale)))
    for name, value in (fidelity.overrides or {}).items():
        overrides[str(name)] = value if isinstance(value, str) else to_override_value(value)
    return overrides


def _representative(p: ParamSpace) -> Any:
    if p.type == "categorical":
        return (p.choices or [None])[0]
    return int(p.low) if p.type == "int" else float(p.low)


def space_errors(space: dict[str, ParamSpace], base_resolved: dict[str, Any]
                 ) -> list[dict[str, str]]:
    """Check that every path of the space applies to the base experiment.

    Returns ``[{"loc", "msg"}]`` (empty when valid), in the format of ``SpecError``.
    """
    errors: list[dict[str, str]] = []
    sample: dict[str, str] = {}
    for name, p in space.items():
        value = to_override_value(_representative(p))
        try:
            apply_overrides(base_resolved, {name: value})
        except OverrideError as exc:
            errors.append({"loc": f"space.{name}", "msg": str(exc)})
            continue
        sample[name] = value
    if not errors:
        try:
            ExperimentSpec.model_validate(apply_overrides(base_resolved, sample))
        except ValidationError as exc:
            for error in format_validation_error(exc):
                errors.append({"loc": "space", "msg": "l'expérience obtenue avec les bornes "
                               f"de l'espace est invalide — {error['loc'] or '(racine)'} : "
                               f"{error['msg']}"})
    return errors
