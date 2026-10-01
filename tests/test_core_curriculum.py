"""Scheduled values, learning-rate factors and phase plans (exact values)."""

from __future__ import annotations

import math

import pytest

from helpers_core import experiment_dict
from wmlab.curriculum import PhasePlan, lr_factor, progress_of, resolve_value
from wmlab.spec import ExperimentSpec, LRScheduleSpec


def test_resolve_numbers():
    assert resolve_value(3, 0.7) == 3.0
    assert resolve_value(0.25, 0.0) == 0.25
    assert resolve_value("0.5", 1.0) == 0.5


def test_resolve_linear_ramp():
    ramp = {"ramp": [0.0, 1.0]}
    assert resolve_value(ramp, 0.0) == 0.0
    assert resolve_value(ramp, 0.25) == 0.25
    assert resolve_value(ramp, 1.0) == 1.0
    assert resolve_value(ramp, 1.5) == 1.0  # progress is clamped


def test_resolve_windowed_ramp():
    ramp = {"ramp": [0.1, 1.0], "start": 0.5, "end": 1.0}
    assert resolve_value(ramp, 0.25) == pytest.approx(0.1)
    assert resolve_value(ramp, 0.5) == pytest.approx(0.1)
    assert resolve_value(ramp, 0.75) == pytest.approx(0.55)
    assert resolve_value(ramp, 1.0) == pytest.approx(1.0)
    step = {"ramp": [0.0, 1.0], "start": 0.5, "end": 0.5}
    assert resolve_value(step, 0.49) == 0.0 and resolve_value(step, 0.5) == 1.0


def test_resolve_shapes():
    assert resolve_value({"ramp": [0, 1], "shape": "cosine"}, 0.5) == pytest.approx(0.5)
    assert resolve_value({"ramp": [0, 1], "shape": "cosine"}, 0.25) == pytest.approx(
        (1 - math.cos(math.pi / 4)) / 2)
    assert resolve_value({"ramp": [1, 100], "shape": "exp"}, 0.5) == pytest.approx(10.0)
    assert resolve_value({"ramp": [0, 1], "shape": "exp"}, 0.5) == pytest.approx(31 / 1023)
    assert resolve_value({"ramp": [0, 1], "shape": "exp"}, 1.0) == pytest.approx(1.0)


def test_resolve_piecewise():
    tri = {"piecewise": [[0, 0], [0.5, 1], [1, 0]]}
    assert resolve_value(tri, 0.25) == pytest.approx(0.5)
    assert resolve_value(tri, 0.5) == pytest.approx(1.0)
    assert resolve_value(tri, 0.75) == pytest.approx(0.5)
    inner = {"piecewise": [[0.2, 1], [0.8, 3]]}
    assert resolve_value(inner, 0.1) == 1.0
    assert resolve_value(inner, 0.5) == pytest.approx(2.0)
    assert resolve_value(inner, 0.9) == 3.0


def test_resolve_accepts_models():
    spec = ExperimentSpec.model_validate(experiment_dict())
    assert resolve_value(spec.phases[1].loss_weights["img"], 1.0) == pytest.approx(1.0)


def test_progress_convention():
    assert progress_of(0, 10) == 0.0 and progress_of(9, 10) == 1.0
    assert progress_of(0, 1) == 0.0


@pytest.mark.parametrize("sched,steps,expected", [
    ({"schedule": "constant"}, 10, {0: 1.0, 5: 1.0, 9: 1.0}),
    ({"schedule": "linear_warmup", "warmup_steps": 4}, 10, {0: 0.25, 1: 0.5, 3: 1.0, 4: 1.0, 9: 1.0}),
    ({"schedule": "cosine"}, 11, {0: 1.0, 5: 0.5, 10: 0.0}),
    ({"schedule": "cosine", "min_factor": 0.1}, 11, {0: 1.0, 5: 0.55, 10: 0.1}),
    ({"schedule": "cosine", "warmup_steps": 2}, 12, {0: 0.5, 1: 1.0, 2: 1.0, 11: 0.0}),
    ({"schedule": "linear", "factor": 2.0}, 5, {0: 2.0, 2: 1.0, 4: 0.0}),
    ({"schedule": "wsd"}, 11, {0: 1.0, 8: 1.0, 9: 0.5, 10: 0.0}),
    ({"schedule": "wsd", "decay_fraction": 0.0}, 11, {10: 1.0}),
    ({"schedule": "exp", "gamma": 0.5, "min_factor": 0.1}, 10, {0: 1.0, 1: 0.5, 2: 0.25, 4: 0.1}),
    ({"schedule": "constant", "factor": 2.0, "warmup_steps": 2}, 10, {0: 1.0, 1: 2.0, 5: 2.0}),
])
def test_lr_factor(sched: dict, steps: int, expected: dict[int, float]):
    spec = LRScheduleSpec.model_validate(sched)
    for step, value in expected.items():
        assert lr_factor(spec, step, steps) == pytest.approx(value), (sched, step)
        assert lr_factor(sched, step, steps) == pytest.approx(value)


def test_phase_plan_bounds_and_locate():
    plan = PhasePlan(experiment_dict())
    assert plan.total_steps == 30 and len(plan) == 2
    assert plan.bounds == [("warmup", 0, 10), ("robust", 10, 30)]
    cur = plan.locate(0)
    assert (cur.index, cur.name, cur.step_in_phase, cur.progress, cur.start, cur.end) == \
        (0, "warmup", 0, 0.0, 0, 10)
    assert plan.locate(9).progress == 1.0 and plan.locate(9).index == 0
    assert plan.locate(10).index == 1 and plan.locate(10).step_in_phase == 0
    assert plan.locate(29).progress == 1.0
    last = plan.locate(500)
    assert last.index == 1 and last.step_in_phase == 19
    with pytest.raises(ValueError):
        plan.locate(-1)
    assert plan.phase_start("robust") == 10 and plan.phase_index(1) == 1
    with pytest.raises(ValueError, match="phase inconnue"):
        plan.phase_index("nope")


def test_phase_plan_values():
    plan = PhasePlan(ExperimentSpec.model_validate(experiment_dict()))
    assert plan.loss_weights(0) == {"msg": 1.0, "img": pytest.approx(0.1)}
    assert plan.loss_weights(10)["img"] == pytest.approx(0.1)
    assert plan.loss_weights(29)["img"] == pytest.approx(1.0)
    assert plan.attack_strength(0) == 1.0
    assert plan.attack_strength(10) == 0.0 and plan.attack_strength(29) == 1.0
    assert plan.lr_factor(0) == 0.5 and plan.lr_factor(1) == 1.0 and plan.lr_factor(20) == 1.0


def test_phase_plan_preview():
    preview = PhasePlan(experiment_dict()).preview(points_per_phase=5)
    assert preview["total_steps"] == 30
    first, second = preview["phases"]
    assert {k: first[k] for k in ("name", "start", "end", "steps")} == \
        {"name": "warmup", "start": 0, "end": 10, "steps": 10}
    assert [p[0] for p in first["lr"]] == [0, 2, 4, 7, 9]
    assert first["lr"][0] == [0, 0.5] and first["lr"][1] == [2, 1.0]
    assert first["attacks"] == [] and second["attacks"] == ["_test_noise", "quant"]
    assert set(second["loss_weights"]) == {"msg", "img"}
    assert second["loss_weights"]["img"][0] == [10, pytest.approx(0.1)]
    assert second["loss_weights"]["img"][-1] == [29, pytest.approx(1.0)]
    assert second["attack_strength"][0] == [10, 0.0] and second["attack_strength"][-1] == [29, 1.0]
    full = PhasePlan(experiment_dict()).preview(points_per_phase=50)
    assert len(full["phases"][0]["lr"]) == 10
