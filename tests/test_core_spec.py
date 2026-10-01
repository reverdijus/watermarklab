"""Specs: schemas, loading, overrides, semantic validation, paths and ids."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from helpers_core import (
    benchmark_dict,
    experiment_dict,
    hpo_dict,
    install_fake_plugins,
    make_project,
    write_yaml,
)
from wmlab import ids, paths
from wmlab.spec import (
    ExperimentSpec,
    HostSpec,
    OverrideError,
    ProjectSpec,
    SpecError,
    WorkspaceSettings,
    apply_overrides,
    detect_kind,
    experiment_path_for_hpo,
    load_benchmark,
    load_experiment,
    load_hosts,
    load_yaml,
    parse_set_options,
    project_for,
    save_hosts,
    validate_content,
    validate_file,
    validate_workspace,
)

ARCH_EXAMPLE = """
name: baseline
description: "HiDDeN + curriculum 3 phases"
hypothesis: "Un warmup sans attaque évite le blocage du décodeur à 50 %"
seed: 0
model:
  name: hidden
  params: {channels: 64}
data:
  train:
    dataset: synthetic
    params: {modality: image, size: 128, num_items: 2048}
    preprocess: [{name: random_crop, params: {size: 128}}]
    limit: null
  val:
    dataset: synthetic
    params: {modality: image, size: 128, num_items: 128, seed: 1}
    preprocess: []
  batch_size: 32
  num_workers: 4
  sample_rate: 16000
  fps: 24
optimizer: {name: adamw, lr: 1.0e-3, weight_decay: 1.0e-4, betas: [0.9, 0.999], grad_clip: 1.0}
loss:
  terms:
    msg: {name: message_bce, weight: 1.0}
    img: {name: mse, weight: 0.7}
phases:
  - name: warmup_decode
    steps: 2000
    lr: {schedule: linear_warmup, warmup_steps: 500}
    loss_weights: {img: 0.1}
    attacks: []
  - name: robustness_ramp
    steps: 10000
    lr: {schedule: constant}
    loss_weights: {img: {ramp: [0.1, 1.0]}}
    attacks:
      - {name: jpeg_diff, params: {quality: 50}, p: 1.0}
      - {name: gaussian_blur, params: {sigma: 2.0}, p: 1.0}
      - {name: crop, params: {area: 0.5}, p: 1.0}
    attack_strength: {ramp: [0.0, 1.0]}
  - name: finetune_imperceptibility
    steps: 3000
    lr: {schedule: cosine, min_factor: 0.01}
    loss_weights: {img: 2.0}
    attacks: [{name: jpeg, params: {quality: 50}}, {name: gaussian_blur, params: {sigma: 2.0}}]
    attack_strength: 1.0
eval:
  every_steps: 1000
  val_batches: 10
  val_attacks: [{label: jpeg50, name: jpeg, params: {quality: 50}}, {label: blur, name: gaussian_blur}]
  final_benchmark: {benchmark: image_v1, checkpoints: ["last", "best:val/bit_acc_mean"]}
checkpoint:
  every_steps: 2000
  keep_last: 2
  keep_best: {"val/bit_acc_mean": max, "val/psnr": max}
  at_phase_end: true
samples: {every_steps: 2000, count: 4}
resources: {device: auto, precision: auto, compile: false}
init_from: null
tags: []
"""


def errors_by_loc(errors: list[dict]) -> dict[str, str]:
    return {e["loc"]: e["msg"] for e in errors}


# -- schemas --------------------------------------------------------------------------------
def test_architecture_example_is_valid():
    spec = ExperimentSpec.model_validate(yaml.safe_load(ARCH_EXAMPLE))
    assert spec.total_steps == 15000
    assert spec.eval.val_attacks[1].label == "blur"
    assert spec.phases[1].attack_strength.ramp == (0.0, 1.0)
    assert spec.eval.final_benchmark.checkpoints == ["last", "best:val/bit_acc_mean"]


def test_experiment_defaults():
    spec = ExperimentSpec.model_validate({
        "name": "mini", "model": {"name": "m"},
        "data": {"train": {"dataset": "d"}, "val": {"dataset": "d"}},
        "loss": {"terms": {"msg": {"name": "l"}}},
        "phases": [{"name": "p", "steps": 5}],
    })
    assert spec.eval.every_steps == 1000 and spec.eval.val_batches == 10
    assert spec.checkpoint.keep_last == 2 and spec.checkpoint.at_phase_end is True
    assert spec.samples.count == 4 and spec.data.num_workers == 4
    assert spec.resources.device == "auto" and spec.optimizer.lr == 1e-3
    assert spec.phases[0].attack_strength == 1.0 and spec.phases[0].random_strength is True
    assert spec.phases[0].lr.schedule == "constant" and spec.tags == []


def test_experiment_schema_errors_are_readable():
    data = experiment_dict()
    data["phases"][0]["steps"] = 0
    data["phases"][1]["name"] = "warmup"
    data["optimizer"]["lrr"] = 0.1
    data["phases"][1]["attack_strength"] = "fort"
    errors = validate_content(data, kind="experiment", semantic=False)
    by_loc = errors_by_loc(errors)
    assert "doit être > 0" in by_loc["phases.0.steps"]
    assert by_loc["optimizer.lrr"].startswith("champ inconnu")
    assert "valeur planifiée invalide" in by_loc["phases.1.attack_strength"]
    duplicate = experiment_dict()
    duplicate["phases"][1]["name"] = "warmup"
    assert "noms de phases en double : warmup" in validate_content(duplicate, "experiment",
                                                                   semantic=False)[0]["msg"]


def test_nbits_forbidden_in_model_params():
    data = experiment_dict(model={"name": "_test_model", "params": {"nbits": 8}})
    errors = validate_content(data, kind="experiment", semantic=False)
    assert "nbits" in errors[0]["msg"]


def test_tags_are_strings_and_project_defaults():
    spec = ExperimentSpec.model_validate(experiment_dict(tags=["a", 2026]))
    assert spec.tags == ["a", "2026"]
    project = ProjectSpec.model_validate({"name": "p", "modality": "audio"})
    assert project.title == "p" and project.message_bits == 48
    assert project.objectives.quality.metric == "snr"
    assert project.objectives.robustness.metric == "bit_acc_mean"
    with pytest.raises(Exception):
        ProjectSpec.model_validate({"name": "p", "modality": "image",
                                    "objectives": {"quality": {"metric": "psnr",
                                                               "constraint": "environ 38"}}})


def test_hpo_param_space_rules():
    good = hpo_dict()
    assert validate_content(good, "hpo", semantic=False) == []
    bad = hpo_dict(space={"optimizer.lr": {"type": "float", "low": 0, "high": 1, "log": True},
                          "seed": {"type": "categorical"}}, budget={"max_trials": None})
    by_loc = errors_by_loc(validate_content(bad, "hpo", semantic=False))
    assert "log" in by_loc["space.optimizer.lr"]
    assert "choices" in by_loc["space.seed"]
    assert "au moins une limite" in by_loc["budget"]


def test_hosts_validation():
    assert HostSpec(name="local", kind="local").price_eur_per_hour == 0
    with pytest.raises(Exception, match="ssh_target"):
        HostSpec(name="gpu", kind="ssh")


def test_workspace_settings_dates():
    settings = WorkspaceSettings.model_validate(
        yaml.safe_load("budget: {total_eur: 10, start: 2026-10-01, manual_entries: "
                       "[{label: disque, eur: 5, date: 2026-11-02}]}"))
    assert settings.budget.start == "2026-10-01"
    assert settings.budget.manual_entries[0].date == "2026-11-02"
    assert settings.data_dir == ".wmlab" and settings.default_host == "local"


# -- loading --------------------------------------------------------------------------------
def test_detect_kind(workspace: Path, tmp_path: Path):
    project_dir = make_project(workspace)
    assert detect_kind(project_dir / "project.yaml") == "project"
    assert detect_kind(project_dir / "experiments" / "baseline.yaml") == "experiment"
    assert detect_kind(workspace / "projects" / "demo" / "hpo" / "x.yaml") == "hpo"
    assert detect_kind(workspace / "benchmarks" / "b.yaml") == "benchmark"
    assert detect_kind(workspace / "datasets" / "d.yaml") == "dataset"
    assert detect_kind(workspace / "workspace.yaml") == "workspace"
    loose = write_yaml(tmp_path / "loose.yaml", hpo_dict())
    assert detect_kind(loose) == "hpo"
    loose_exp = write_yaml(tmp_path / "exp.yaml", experiment_dict())
    assert detect_kind(loose_exp) == "experiment"
    unknown = write_yaml(tmp_path / "x.yaml", {"foo": 1})
    assert detect_kind(unknown) is None


def test_load_yaml_errors(tmp_path: Path):
    path = tmp_path / "projects" / "p" / "project.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("name: p\nmodality: [image\n")
    with pytest.raises(SpecError, match="YAML invalide"):
        load_yaml(path)
    path.write_text("name: p\nmodality: text\n")
    with pytest.raises(SpecError) as info:
        load_yaml(path)
    assert info.value.errors[0]["loc"] == "modality"
    assert "valeurs possibles" in info.value.errors[0]["msg"]


def test_apply_overrides():
    spec = experiment_dict()
    out = apply_overrides(spec, {
        "optimizer.lr": "3e-4",
        "phases.1.steps": "5000",
        "loss.terms.img.weight": "0.5",
        "phases.warmup.attack_strength": "0.3",
        "model.params.depth": "4",
        "eval.val_attacks.0.params": "{std: 0.1}",
        "tags": "[a, b]",
        "seed": "true",
    })
    assert out["optimizer"]["lr"] == pytest.approx(3e-4)
    assert out["phases"][1]["steps"] == 5000
    assert out["loss"]["terms"]["img"]["weight"] == 0.5
    assert out["phases"][0]["attack_strength"] == 0.3
    assert out["model"]["params"]["depth"] == 4
    assert out["eval"]["val_attacks"][0]["params"] == {"std": 0.1}
    assert out["tags"] == ["a", "b"]
    assert out["seed"] is True
    assert spec["optimizer"]["lr"] == 1e-3 and "depth" not in spec["model"]["params"]


@pytest.mark.parametrize("key,match", [
    ("phases.5.steps", "hors bornes"),
    ("optimizer.missing.lr", "clé 'missing' introuvable"),
    ("seed.value", "ne contient pas de sous-clés"),
    ("phases.unknown.steps", "ni un index numérique"),
    ("", "chemin vide"),
])
def test_apply_overrides_errors(key: str, match: str):
    with pytest.raises(OverrideError, match=match):
        apply_overrides(experiment_dict(), {key: "1"})


def test_parse_set_options():
    assert parse_set_options(["a.b=1", "c=x=y", "a.b=2"]) == {"a.b": "2", "c": "x=y"}
    with pytest.raises(OverrideError, match="clé=valeur"):
        parse_set_options(["novalue"])


def test_load_experiment_and_project_for(workspace: Path):
    project_dir = make_project(workspace)
    exp_path = project_dir / "experiments" / "baseline.yaml"
    spec, project, resolved = load_experiment(exp_path, {"optimizer.lr": "2e-3"})
    assert spec.optimizer.lr == pytest.approx(2e-3) and project.name == "demo"
    assert resolved["optimizer"]["lr"] == pytest.approx(2e-3)
    assert resolved["eval"]["every_steps"] == 5 and resolved["samples"]["count"] == 4
    assert ExperimentSpec.model_validate(resolved) == spec
    # relative path from the workspace
    spec2, _, _ = load_experiment("projects/demo/experiments/baseline.yaml")
    assert spec2.name == "baseline"
    found_dir, found = project_for(exp_path)
    assert found_dir == project_dir and found.modality == "image"
    with pytest.raises(SpecError):
        load_experiment(exp_path, {"phases.0.steps": "-1"})
    # keys only present through defaults can be overridden too
    spec3, _, resolved3 = load_experiment(exp_path, {"resources.device": "cpu",
                                                     "samples.count": "2"})
    assert spec3.resources.device == "cpu" and resolved3["samples"]["count"] == 2
    with pytest.raises(OverrideError):
        load_experiment(exp_path, {"resources.nope.x": "1"})


def test_experiment_path_for_hpo(workspace: Path):
    project_dir = make_project(workspace)
    hpo_path = write_yaml(project_dir / "hpo" / "sweep.yaml", hpo_dict())
    spec = load_yaml(hpo_path)
    assert experiment_path_for_hpo(hpo_path, spec) == project_dir / "experiments" / "baseline.yaml"


def test_load_benchmark(workspace: Path):
    write_yaml(workspace / "benchmarks" / "image_v1.yaml", benchmark_dict())
    assert load_benchmark("image_v1").attacks[0].label == "none"
    assert load_benchmark(workspace / "benchmarks" / "image_v1.yaml").version == 1
    with pytest.raises(SpecError, match="benchmark inconnu 'nope' — disponibles : image_v1"):
        load_benchmark("nope")


def test_hosts_roundtrip(workspace: Path):
    hosts = load_hosts()
    assert [h.name for h in hosts] == ["local"]
    assert (workspace / ".wmlab" / "hosts.yaml").is_file()
    save_hosts([*hosts, {"name": "scw-l40s", "kind": "ssh", "ssh_target": "root@1.2.3.4",
                         "price_eur_per_hour": 1.4, "max_concurrent_jobs": 2}])
    loaded = load_hosts()
    assert [h.name for h in loaded] == ["local", "scw-l40s"]
    assert loaded[1].price_eur_per_hour == 1.4
    with pytest.raises(SpecError):
        save_hosts([{"name": "x", "kind": "ssh"}])


# -- semantic validation --------------------------------------------------------------------
@pytest.fixture
def fake_ws(workspace: Path) -> Path:
    install_fake_plugins(workspace)
    make_project(workspace)
    write_yaml(workspace / "benchmarks" / "image_v1.yaml", benchmark_dict())
    write_yaml(workspace / "datasets" / "coco.yaml",
               {"name": "coco", "modality": "image", "paths": {"default": "~/data/coco"}})
    return workspace


EXP_PATH = "projects/demo/experiments/baseline.yaml"


def test_semantic_valid_experiment(fake_ws: Path):
    assert validate_file(fake_ws / EXP_PATH) == []
    data = experiment_dict(eval={"val_attacks": [{"name": "_test_noise"}],
                                 "final_benchmark": {"benchmark": "image_v1",
                                                     "checkpoints": ["last",
                                                                     "best:val/bit_acc_mean",
                                                                     "phase_end:robust"]}},
                           init_from={"run": "r", "start_phase": "robust"})
    data["data"]["train"]["params"]["root"] = "@coco"
    assert validate_content(data, path=EXP_PATH) == []


def test_semantic_unknown_and_incompatible_components(fake_ws: Path):
    data = experiment_dict(model={"name": "_test_unet"})
    data["phases"][1]["attacks"].append({"name": "_test_quant", "params": {"levls": 4}})
    data["loss"]["terms"]["img"]["name"] = "_test_nope"
    by_loc = errors_by_loc(validate_content(data, path=EXP_PATH))
    assert by_loc["model.name"].startswith(
        "modèle inconnu '_test_unet' — vouliez-vous dire")
    assert "disponibles : _test_model" in by_loc["model.name"]
    assert "_test_audio_model" not in by_loc["model.name"]  # filtered by modality
    assert "paramètre inconnu 'levls'" in by_loc["phases.1.attacks.2.params.levls"]
    assert "vouliez-vous dire 'levels'" in by_loc["phases.1.attacks.2.params.levls"]
    assert by_loc["loss.terms.img.name"].startswith("perte inconnue '_test_nope'")

    audio = experiment_dict(model={"name": "_test_audio_model"})
    by_loc = errors_by_loc(validate_content(audio, path=EXP_PATH))
    assert "incompatible avec la modalité image" in by_loc["model.name"]


def test_semantic_params_and_references(fake_ws: Path):
    data = experiment_dict(model={"name": "_test_model", "params": {"channels": 0}},
                           init_from={"run": "r", "start_phase": "nope"},
                           eval={"val_attacks": [{"label": "a", "name": "_test_noise"},
                                                 {"label": "a", "name": "_test_quant"}],
                                 "final_benchmark": {"benchmark": "video_v9",
                                                     "checkpoints": ["best:val/psnr",
                                                                     "phase_end:x"]}})
    data["data"]["train"]["params"]["root"] = "@imagenet"
    data["data"]["val"]["params"]["modality"] = "audio"
    data["data"]["train"]["preprocess"].append({"name": "_test_crop"})
    data["phases"][0]["loss_weights"]["perceptual"] = 1.0
    data["checkpoint"]["keep_best"]["train/loss"] = "min"
    by_loc = errors_by_loc(validate_content(data, path=EXP_PATH))
    assert "doit être > 0" in by_loc["model.params.channels"]
    assert "phase inconnue 'nope'" in by_loc["init_from.start_phase"]
    assert "label en double 'a'" in by_loc["eval.val_attacks.1.label"]
    assert "benchmark inconnu 'video_v9' — disponibles : image_v1" in \
        by_loc["eval.final_benchmark.benchmark"]
    assert "keep_best" in by_loc["eval.final_benchmark.checkpoints.0"]
    assert "phase inconnue 'x'" in by_loc["eval.final_benchmark.checkpoints.1"]
    assert "dataset '@imagenet' absent du catalogue" in by_loc["data.train.params.root"]
    assert "disponibles : coco" in by_loc["data.train.params.root"]
    assert "différente de celle du projet" in by_loc["data.val.params.modality"]
    assert "paramètre requis manquant" in by_loc["data.train.preprocess.1.params.size"]
    assert "terme de perte inconnu 'perceptual'" in by_loc["phases.0.loss_weights.perceptual"]
    assert "val/" in by_loc["checkpoint.keep_best.train/loss"]


def test_semantic_name_must_match_file(fake_ws: Path):
    data = experiment_dict(name="other")
    assert "nom du fichier 'baseline'" in errors_by_loc(
        validate_content(data, path=EXP_PATH))["name"]


def test_semantic_hpo(fake_ws: Path):
    hpo_path = "projects/demo/hpo/sweep.yaml"
    assert validate_content(hpo_dict(), path=hpo_path) == []
    bad = hpo_dict(experiment="nope")
    assert "expérience de base introuvable 'nope' — disponibles : baseline" in \
        errors_by_loc(validate_content(bad, path=hpo_path))["experiment"]
    bad = hpo_dict(objectives=[{"metric": "psnr"}],
                   space={"optimizer.lrr": {"type": "float", "low": 0.1, "high": 1.0},
                          "phases.9.steps": {"type": "int", "low": 1, "high": 5},
                          "phases.0.steps": {"type": "float", "low": 0.5, "high": 5.0},
                          "model.params.channls": {"type": "int", "low": 1, "high": 5},
                          "model.params.channels": {"type": "int", "low": 1, "high": 5}},
                   fidelity={"overrides": {"optimizer.nope.x": 1}})
    by_loc = errors_by_loc(validate_content(bad, path=hpo_path))
    assert "val/" in by_loc["objectives.0.metric"]
    assert "introuvable" in by_loc["space.optimizer.lrr"]
    assert "hors bornes" in by_loc["space.phases.9.steps"]
    assert "rend l'expérience invalide" in by_loc["space.phases.0.steps"]
    assert "space.model.params.channls" in by_loc
    assert "space.model.params.channels" not in by_loc
    assert "fidelity.overrides.optimizer.nope.x" in by_loc


def test_semantic_hpo_metrics_paths_and_fidelity(fake_ws: Path):
    """Objective/constraint names are checked against the metrics the base experiment logs;
    dead search dimensions and warmups longer than their (scaled) phase are errors."""
    hpo_path = "projects/demo/hpo/sweep.yaml"
    typo = hpo_dict(objectives=[{"metric": "val/bit_acc/noize", "direction": "maximize"}],
                    constraints=[{"metric": "val/psnrr", "op": ">=", "value": 30}])
    by_loc = errors_by_loc(validate_content(typo, path=hpo_path))
    assert "n'est pas une métrique produite" in by_loc["objectives.0.metric"]
    assert "val/bit_acc/noise" in by_loc["objectives.0.metric"]  # suggestion + list
    assert "vouliez-vous dire 'val/psnr'" in by_loc["constraints.0.metric"]
    for metric in ("val/loss", "val/bit_acc", "val/bit_acc_mean", "val/bit_acc/noise",
                   "val/psnr", "val/ssim"):
        assert validate_content(hpo_dict(objectives=[{"metric": metric, "direction": "maximize"}]),
                                path=hpo_path) == [], metric
    # loss.terms.img.weight is overridden by every phase of the base experiment: no effect
    dead = hpo_dict(space={"loss.terms.img.weight": {"type": "float", "low": 0.1, "high": 2.0}})
    by_loc = errors_by_loc(validate_content(dead, path=hpo_path))
    assert "chemin sans effet" in by_loc["space.loss.terms.img.weight"]
    assert "phases.1.loss_weights.img.ramp.1" in by_loc["space.loss.terms.img.weight"]
    dead = hpo_dict(fidelity={"overrides": {"loss.terms.img.weight": 3}})
    assert "chemin sans effet" in errors_by_loc(validate_content(dead, path=hpo_path))[
        "fidelity.overrides.loss.terms.img.weight"]
    # msg has no phase override: searching its base weight is meaningful
    alive = hpo_dict(space={"loss.terms.msg.weight": {"type": "float", "low": 0.1, "high": 2.0}})
    assert validate_content(alive, path=hpo_path) == []
    # fidelity: steps_scale also scales warmup_steps (10 × 0.2 = 2 steps, warmup 2 × 0.2 → 1)
    scaled = hpo_dict(fidelity={"steps_scale": 0.2})
    assert validate_content(scaled, path=hpo_path) == []
    # …but an explicit override that leaves the warmup longer than the phase is reported
    bad = hpo_dict(fidelity={"steps_scale": 0.2, "overrides": {"phases.0.lr.warmup_steps": 5}})
    by_loc = errors_by_loc(validate_content(bad, path=hpo_path))
    assert "warmup_steps (5) > steps (2)" in by_loc["fidelity.phases.0.lr.warmup_steps"]


def test_semantic_warmup_longer_than_phase(fake_ws: Path):
    path = "projects/demo/experiments/baseline.yaml"
    exp = experiment_dict()
    exp["phases"][0]["lr"] = {"schedule": "linear_warmup", "warmup_steps": 10}  # == steps: ok
    assert validate_content(exp, path=path) == []
    exp["phases"][0]["lr"] = {"schedule": "cosine", "warmup_steps": 11}
    by_loc = errors_by_loc(validate_content(exp, path=path))
    assert "warmup_steps (11) > steps (10)" in by_loc["phases.0.lr.warmup_steps"]


def test_semantic_unreachable_fpr(fake_ws: Path):
    """A benchmark FPR below 2^-message_bits is impossible: tpr@<fpr> would always be 0."""
    from wmlab.spec.validate import fpr_reachable

    assert fpr_reachable(16, 1e-4) and not fpr_reachable(16, 1e-6)
    assert fpr_reachable(48, 1e-6) and not fpr_reachable(8, 1e-4)
    write_yaml(fake_ws / "benchmarks" / "image_v2.yaml",
               benchmark_dict(name="image_v2", version=2, fpr=[1e-2, 1e-4, 1e-6]))
    project = yaml.safe_load((fake_ws / "projects/demo/project.yaml").read_text())
    project["default_benchmark"] = "image_v2"  # 16 bits
    by_loc = errors_by_loc(validate_content(project, path="projects/demo/project.yaml"))
    assert "FPR inatteignable avec 16 bits : 1e-06" in by_loc["default_benchmark"]
    project["message_bits"] = 32
    assert validate_content(project, path="projects/demo/project.yaml") == []
    exp = experiment_dict(eval={"every_steps": 5, "val_attacks": [],
                                "final_benchmark": {"benchmark": "image_v2"}})
    by_loc = errors_by_loc(validate_content(exp, path="projects/demo/experiments/baseline.yaml"))
    assert "inatteignable" in by_loc["eval.final_benchmark.benchmark"]
    # an invalid benchmark file names its first error
    (fake_ws / "benchmarks" / "image_v3.yaml").write_text("name: image_v3\nmodality: image\n",
                                                          encoding="utf-8")
    exp["eval"]["final_benchmark"] = {"benchmark": "image_v3"}
    by_loc = errors_by_loc(validate_content(exp, path="projects/demo/experiments/baseline.yaml"))
    assert by_loc["eval.final_benchmark.benchmark"].startswith("le benchmark 'image_v3' est invalide")
    assert "data" in by_loc["eval.final_benchmark.benchmark"]


def test_semantic_benchmark_and_project(fake_ws: Path):
    bench = benchmark_dict(attacks=[{"label": "x", "name": "_test_noise"},
                                    {"label": "x", "name": "_test_quant"},
                                    {"name": "_test_blur"}],
                           quality_metrics=["_test_psnr", "_test_snr"])
    by_loc = errors_by_loc(validate_content(bench, path="benchmarks/image_v1.yaml"))
    assert "label en double 'x'" in by_loc["attacks.1.label"]
    assert by_loc["attacks.2.name"].startswith("attaque inconnue '_test_blur'")
    assert "incompatible avec la modalité image" in by_loc["quality_metrics.1"]

    project = yaml.safe_load((fake_ws / "projects/demo/project.yaml").read_text())
    project["default_benchmark"] = "image_v1"
    assert validate_content(project, path="projects/demo/project.yaml") == []
    project["name"] = "autre"
    project["objectives"]["quality"]["metric"] = "_test_snr"
    by_loc = errors_by_loc(validate_content(project, path="projects/demo/project.yaml"))
    assert "dossier du projet 'demo'" in by_loc["name"]
    assert "incompatible" in by_loc["objectives.quality.metric"]


def test_validate_content_yaml_text_and_unknown_kind(fake_ws: Path):
    errors = validate_content("name: [oops", path=EXP_PATH)
    assert errors[0]["msg"].startswith("YAML invalide (ligne")
    assert "type de fichier inconnu" in validate_content("foo: 1")[0]["msg"]


def test_validate_workspace(fake_ws: Path):
    (fake_ws / "projects" / "empty").mkdir()
    write_yaml(fake_ws / "projects/demo/experiments/broken.yaml",
               experiment_dict(name="broken", phases="pas une liste"))
    (fake_ws / "lab" / "bad_plugin.py").write_text("import does_not_exist\n")
    results = validate_workspace()
    assert results["workspace.yaml"] == []
    assert results["projects/demo/project.yaml"] == []
    assert results[EXP_PATH] == []
    assert results["benchmarks/image_v1.yaml"] == []
    assert results["datasets/coco.yaml"] == []
    assert "manquant" in results["projects/empty/project.yaml"][0]["msg"]
    assert results["projects/demo/experiments/broken.yaml"][0]["loc"] == "phases"
    assert "does_not_exist" in results["lab/bad_plugin.py"][0]["msg"]


# -- paths & ids ----------------------------------------------------------------------------
def test_paths(workspace: Path, monkeypatch):
    assert paths.workspace_root() == workspace
    assert paths.data_root() == workspace / ".wmlab"
    for fn, name in ((paths.runs_dir, "runs"), (paths.studies_dir, "studies"),
                     (paths.cache_dir, "cache"), (paths.jobs_dir, "jobs"),
                     (paths.logs_dir, "logs")):
        assert fn() == workspace / ".wmlab" / name and fn().is_dir()
    assert paths.load_workspace_settings().budget.total_eur == 1000
    assert paths.current_host() == "local" and paths.current_price() == 0.0
    monkeypatch.setenv("WMLAB_HOST", "scw-l40s")
    monkeypatch.setenv("WMLAB_PRICE_EUR_PER_HOUR", "1.4")
    assert paths.current_host() == "scw-l40s" and paths.current_price() == 1.4
    monkeypatch.delenv("WMLAB_DATA")
    (workspace / "workspace.yaml").write_text("data_dir: ../elsewhere\n")
    assert paths.data_root() == (workspace.parent / "elsewhere").resolve()
    (workspace / "workspace.yaml").unlink()
    assert paths.load_workspace_settings() == WorkspaceSettings()
    monkeypatch.setenv("WMLAB_WORKSPACE", str(workspace / "missing"))
    with pytest.raises(paths.WorkspaceNotFoundError, match="WMLAB_WORKSPACE"):
        paths.workspace_root()


def test_workspace_search_upwards(tmp_path: Path, monkeypatch):
    (tmp_path / "ws" / "a" / "b").mkdir(parents=True)
    (tmp_path / "ws" / "workspace.yaml").write_text("name: x\n")
    monkeypatch.delenv("WMLAB_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path / "ws" / "a" / "b")
    assert paths.workspace_root() == (tmp_path / "ws").resolve()
    assert paths._search_upwards(tmp_path) is None


def test_ids():
    run_id = ids.new_run_id("Baseline Été #2 — très long nom d'expérience vraiment")
    assert re.fullmatch(r"\d{8}-\d{6}-[a-z0-9-]{1,32}-[0-9a-f]{4}", run_id)
    assert "baseline-ete-2" in run_id
    assert re.fullmatch(r"\d{8}-\d{6}-lr-sweep-[0-9a-f]{4}", ids.new_study_id("lr_sweep"))
    assert re.fullmatch(r"job-\d{8}-\d{6}-[0-9a-f]{4}", ids.new_job_id())
    assert ids.slugify("!!!") == "run"
    assert ids.utc_iso(0) == "1970-01-01T00:00:00Z"
    assert ids.parse_iso("2026-10-01T12:00:00Z") == ids.parse_iso("2026-10-01T14:00:00+02:00")
    assert ids.parse_iso(ids.utc_iso(1_790_000_000)) == 1_790_000_000
    assert ids.parse_iso("pas une date") is None and ids.parse_iso(None) is None
