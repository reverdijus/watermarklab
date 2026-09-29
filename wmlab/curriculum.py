"""Curriculum: scheduled values, learning-rate factors and phase plans (§5.1, §5.3).

Convention: inside a phase of ``N`` steps, step ``s`` (0-based) has
``progress = s / (N - 1)`` (0 for a 1-step phase), so the last step reaches the target.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from wmlab.spec.models import (
    ExperimentSpec,
    LRScheduleSpec,
    PhaseSpec,
    PiecewiseValue,
    RampValue,
)


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


def progress_of(step_in_phase: int, phase_steps: int) -> float:
    """Return the progress in [0, 1] of a step inside a phase."""
    if phase_steps <= 1:
        return 0.0
    return _clamp01(step_in_phase / (phase_steps - 1))


def _interpolate(a: float, b: float, t: float, shape: str) -> float:
    if shape == "cosine":
        return a + (b - a) * (1.0 - math.cos(math.pi * t)) / 2.0
    if shape == "exp":
        if a > 0 and b > 0 or a < 0 and b < 0:
            return a * (b / a) ** t  # geometric (log-space) interpolation
        return a + (b - a) * (2.0 ** (10.0 * t) - 1.0) / 1023.0  # exponential ease-in
    return a + (b - a) * t


def resolve_value(v: Any, progress: float) -> float:
    """Resolve a ``ScheduledValue`` (number, ramp or piecewise) at ``progress`` ∈ [0, 1]."""
    p = _clamp01(progress)
    if isinstance(v, bool):
        raise TypeError("valeur planifiée invalide : booléen")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return float(v)
    if isinstance(v, dict):
        v = RampValue.model_validate(v) if "ramp" in v else PiecewiseValue.model_validate(v)
    if isinstance(v, RampValue):
        a, b = float(v.ramp[0]), float(v.ramp[1])
        if p <= v.start:
            return a if p < v.start or v.end > v.start else b
        if p >= v.end:
            return b
        t = (p - v.start) / (v.end - v.start)
        return _interpolate(a, b, t, v.shape)
    if isinstance(v, PiecewiseValue):
        points = [(float(f), float(val)) for f, val in v.piecewise]
        if p <= points[0][0]:
            return points[0][1]
        if p >= points[-1][0]:
            return points[-1][1]
        for (f0, v0), (f1, v1) in zip(points, points[1:]):
            if f0 <= p <= f1:
                if f1 == f0:
                    return v1
                return v0 + (v1 - v0) * (p - f0) / (f1 - f0)
        return points[-1][1]
    raise TypeError(f"valeur planifiée invalide : {v!r}")


def lr_factor(sched: LRScheduleSpec | dict[str, Any], step_in_phase: int, phase_steps: int) -> float:
    """Return the multiplier of ``optimizer.lr`` at ``step_in_phase`` of a phase.

    A linear warmup from ``factor/W`` to ``factor`` over the first ``W = warmup_steps`` steps
    applies to every schedule; the schedule then runs over the remaining steps, from
    ``factor`` down to ``min_factor`` (both are multipliers of ``optimizer.lr``).
    """
    if isinstance(sched, dict):
        sched = LRScheduleSpec.model_validate(sched)
    s = max(int(step_in_phase), 0)
    n = max(int(phase_steps), 1)
    peak = float(sched.factor)
    floor = float(sched.min_factor)
    warmup = int(sched.warmup_steps)
    if warmup > 0 and s < warmup:
        return peak * (s + 1) / warmup
    remaining = n - warmup
    t = _clamp01((s - warmup) / (remaining - 1)) if remaining > 1 else 0.0
    kind = sched.schedule
    if kind in ("constant", "linear_warmup"):
        return peak
    if kind == "cosine":
        return floor + (peak - floor) * (1.0 + math.cos(math.pi * t)) / 2.0
    if kind == "linear":
        return peak + (floor - peak) * t
    if kind == "wsd":
        decay = float(sched.decay_fraction)
        if decay <= 0.0 or t <= 1.0 - decay:
            return peak
        u = (t - (1.0 - decay)) / decay
        return peak + (floor - peak) * _clamp01(u)
    if kind == "exp":
        return max(floor, peak * float(sched.gamma) ** (s - warmup))
    raise ValueError(f"schedule de LR inconnu '{kind}'")


@dataclass(frozen=True)
class PhaseCursor:
    """Position of a global step inside the phase plan."""

    index: int
    phase: PhaseSpec
    step_in_phase: int
    progress: float
    start: int
    end: int

    @property
    def name(self) -> str:
        return self.phase.name


class PhasePlan:
    """Sequence of curriculum phases of an experiment."""

    def __init__(self, spec: ExperimentSpec | dict[str, Any]) -> None:
        if isinstance(spec, dict):
            spec = ExperimentSpec.model_validate(spec)
        self.spec = spec
        self.phases: list[PhaseSpec] = list(spec.phases)
        self.bounds: list[tuple[str, int, int]] = []
        start = 0
        for phase in self.phases:
            self.bounds.append((phase.name, start, start + phase.steps))
            start += phase.steps
        self.total_steps: int = start

    def __len__(self) -> int:
        return len(self.phases)

    def phase_index(self, phase: str | int) -> int:
        """Return the index of a phase given by name or index."""
        if isinstance(phase, int) and not isinstance(phase, bool):
            if not 0 <= phase < len(self.phases):
                raise ValueError(f"index de phase {phase} hors bornes (0 à {len(self.phases) - 1})")
            return phase
        for i, p in enumerate(self.phases):
            if p.name == phase:
                return i
        raise ValueError(f"phase inconnue '{phase}' — phases : "
                         f"{', '.join(p.name for p in self.phases)}")

    def phase_start(self, phase: str | int) -> int:
        """Return the global step at which a phase starts."""
        return self.bounds[self.phase_index(phase)][1]

    def locate(self, global_step: int) -> PhaseCursor:
        """Return the phase cursor of ``global_step`` (clamped to the last step)."""
        if global_step < 0:
            raise ValueError("global_step doit être ≥ 0")
        for index, (_, start, end) in enumerate(self.bounds):
            if global_step < end:
                break
        else:
            index = len(self.bounds) - 1
            _, start, end = self.bounds[index]
            global_step = end - 1
        phase = self.phases[index]
        step_in_phase = global_step - start
        return PhaseCursor(index, phase, step_in_phase,
                           progress_of(step_in_phase, phase.steps), start, end)

    def lr_factor(self, global_step: int) -> float:
        """Learning-rate factor at ``global_step``."""
        cur = self.locate(global_step)
        return lr_factor(cur.phase.lr, cur.step_in_phase, cur.phase.steps)

    def loss_weights(self, global_step: int) -> dict[str, float]:
        """Weight of every loss term at ``global_step`` (phase override or base weight)."""
        cur = self.locate(global_step)
        weights: dict[str, float] = {}
        for term, spec in self.spec.loss.terms.items():
            value = cur.phase.loss_weights.get(term, spec.weight)
            weights[term] = resolve_value(value, cur.progress)
        return weights

    def attack_strength(self, global_step: int) -> float:
        """Attack strength ceiling at ``global_step``."""
        cur = self.locate(global_step)
        return resolve_value(cur.phase.attack_strength, cur.progress)

    def preview(self, points_per_phase: int = 50) -> dict[str, Any]:
        """Return a ``PhasePlanPreview`` (§9): per-phase ``[global step, value]`` series."""
        points = max(int(points_per_phase), 2)
        phases: list[dict[str, Any]] = []
        for phase, (name, start, end) in zip(self.phases, self.bounds):
            n = phase.steps
            if n <= points:
                local_steps = list(range(n))
            else:
                local_steps = sorted({round(i * (n - 1) / (points - 1)) for i in range(points)})
            lr_series: list[list[float]] = []
            strength_series: list[list[float]] = []
            weight_series: dict[str, list[list[float]]] = {t: [] for t in self.spec.loss.terms}
            for s in local_steps:
                progress = progress_of(s, n)
                g = start + s
                lr_series.append([g, lr_factor(phase.lr, s, n)])
                strength_series.append([g, resolve_value(phase.attack_strength, progress)])
                for term, term_spec in self.spec.loss.terms.items():
                    value = phase.loss_weights.get(term, term_spec.weight)
                    weight_series[term].append([g, resolve_value(value, progress)])
            phases.append({
                "name": name,
                "start": start,
                "end": end,
                "steps": n,
                "lr": lr_series,
                "attack_strength": strength_series,
                "loss_weights": weight_series,
                "attacks": [a.label or a.name for a in phase.attacks],
            })
        return {"total_steps": self.total_steps, "phases": phases}
