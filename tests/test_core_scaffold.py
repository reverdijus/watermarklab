"""Scaffold templates and plugin conformance checks."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from helpers_core import install_fake_plugins
from wmlab import scaffold
from wmlab.plugin_test import run_plugin_test
from wmlab.registry import MODELS
from wmlab.spec import load_yaml, validate_file


def _module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


def failed(results: list[dict]) -> list[dict]:
    return [r for r in results if not r["ok"]]


# -- plugin_test on fake components ---------------------------------------------------------
@pytest.fixture
def fakes(workspace: Path) -> Path:
    install_fake_plugins(workspace)
    return workspace


@pytest.mark.parametrize("kind,name,modality", [
    ("model", "_test_model", None),
    ("model", "_test_audio_model", None),
    ("attack", "_test_noise", "audio"),
    ("attack", "_test_noise", "video"),
    ("attack", "_test_quant", None),
    ("preprocess", "_test_scale", "video"),
    ("preprocess", "_test_crop", None),
    ("dataset", "_test_dataset", "image"),
    ("dataset", "_test_dataset", "audio"),
    ("dataset", "_test_dataset", "video"),
    ("metric", "_test_psnr", None),
    ("metric", "_test_snr", None),
])
def test_plugin_test_passes_on_valid_components(fakes: Path, kind: str, name: str, modality):
    results = run_plugin_test(kind, name, modality)
    assert results[0]["check"] == "enregistrement" and results[0]["ok"]
    assert failed(results) == [], results
    assert all(set(r) == {"check", "ok", "detail"} for r in results)


def test_plugin_test_losses(fakes: Path):
    results = run_plugin_test("loss", "_test_mse")
    if _module_available("wmlab.losses.base"):
        assert failed(results) == [], results
    else:
        assert [r["check"] for r in failed(results)] == ["StepBatch (wmlab.losses.base)"]


def test_plugin_test_detects_problems(fakes: Path):
    import torch
    from torch import nn

    @MODELS.register("_test_broken", modality="image")
    class Broken(nn.Module):
        modality = "audio"

        def __init__(self, nbits: int) -> None:
            super().__init__()
            self.nbits = nbits
            self.w = nn.Parameter(torch.ones(1))

        def embed(self, x, msg):
            return x * self.w * 3.0  # out of range

        def extract(self, x):
            return torch.zeros(x.shape[0], self.nbits)  # no gradient

    results = {r["check"]: r for r in run_plugin_test("model", "_test_broken")}
    assert results["modalité déclarée"]["ok"] is False
    assert results["embed : plage"]["ok"] is False
    assert results["embed : forme"]["ok"] is True
    assert results["extract : forme"]["ok"] is True

    unknown = run_plugin_test("model", "_test_modl")
    assert len(unknown) == 1 and not unknown[0]["ok"]
    assert "vouliez-vous dire : _test_model" in unknown[0]["detail"]
    wrong = run_plugin_test("model", "_test_model", "audio")
    assert not wrong[-1]["ok"] and "ne supporte pas audio" in wrong[-1]["detail"]
    assert not run_plugin_test("optimizer", "adam")[0]["ok"]


# -- scaffold -------------------------------------------------------------------------------
@pytest.mark.parametrize("modality", ["image", "audio", "video"])
def test_project_experiment_hpo_templates_are_valid(workspace: Path, modality: str):
    path = scaffold.new_project(f"p-{modality}", modality, 32, title="Projet é",
                                description="Description « test »")
    project = load_yaml(path)
    assert project.modality == modality and project.message_bits == 32
    assert project.title == "Projet é" and project.description == "Description « test »"
    project_dir = path.parent
    assert (project_dir / "notes.md").is_file() and (project_dir / "hpo").is_dir()
    baseline = load_yaml(project_dir / "experiments" / "baseline.yaml")
    assert baseline.name == "baseline" and len(baseline.phases) == 3
    assert baseline.eval.every_steps == 1000 and baseline.checkpoint.keep_last == 2
    hpo_path = scaffold.new_hpo(f"p-{modality}", "sweep", "baseline")
    hpo = load_yaml(hpo_path)
    assert hpo.experiment == "baseline" and "optimizer.lr" in hpo.space
    assert validate_file(hpo_path) == []
    copy = scaffold.new_experiment(f"p-{modality}", "variant", from_experiment="baseline")
    variant = load_yaml(copy)
    assert variant.name == "variant"
    assert "copie de « baseline »" in copy.read_text(encoding="utf-8")
    assert validate_file(copy) == []


def test_experiment_copy_is_a_fresh_experiment(workspace: Path):
    """``--from`` copies the curriculum but not the identity of the source: description,
    hypothesis, tags and header are those of a new experiment (inherited tags would label
    the runs of the copy after the source)."""
    scaffold.new_project("img", "image", with_baseline=False)
    source = workspace / "projects" / "img" / "experiments" / "baseline.yaml"
    text = scaffold.experiment_template("baseline", "img", "image")
    text = text.replace('description: "À compléter : ce que cette expérience teste"',
                        'description: "La baseline de référence"')
    text = text.replace('hypothesis: "À compléter : résultat attendu et pourquoi"',
                        "hypothesis: >-\n  Une hypothèse\n  sur deux lignes")
    text = text.replace("tags: []", "tags: [baseline, image]")
    source.write_text(text, encoding="utf-8")
    copy = scaffold.new_experiment("img", "variant", from_experiment="baseline")
    variant, base = load_yaml(copy), load_yaml(source)
    assert variant.name == "variant" and variant.tags == []
    assert variant.description != base.description and "À compléter" in variant.description
    assert variant.hypothesis != base.hypothesis and "À compléter" in variant.hypothesis
    assert variant.model == base.model and variant.phases == base.phases
    assert variant.loss == base.loss and variant.eval == base.eval
    head = copy.read_text(encoding="utf-8").splitlines()[0]
    assert "variant" in head and "baseline" in head and "Expérience « baseline »" not in head
    assert validate_file(copy) == []


def test_default_benchmark_is_used_when_present(workspace: Path):
    (workspace / "benchmarks" / "image_v1.yaml").write_text("name: image_v1\n")
    path = scaffold.new_project("img", "image")
    assert load_yaml(path).default_benchmark == "image_v1"
    exp = load_yaml(path.parent / "experiments" / "baseline.yaml")
    assert exp.eval.final_benchmark.benchmark == "image_v1"


def test_scaffold_never_overwrites(workspace: Path):
    scaffold.new_project("img", "image")
    with pytest.raises(FileExistsError):
        scaffold.new_project("img", "image")
    with pytest.raises(FileExistsError):
        scaffold.new_experiment("img", "baseline")
    scaffold.new_plugin("metric", "my_metric", "image")
    before = (workspace / "lab/metrics/my_metric.py").read_text()
    with pytest.raises(FileExistsError):
        scaffold.new_plugin("metric", "my_metric", "audio")
    assert (workspace / "lab/metrics/my_metric.py").read_text() == before
    with pytest.raises(scaffold.ScaffoldError, match="nom de projet invalide"):
        scaffold.new_project("bad name", "image")
    with pytest.raises(scaffold.ScaffoldError, match="projet inconnu"):
        scaffold.new_hpo("nope", "x", "baseline")
    with pytest.raises(scaffold.ScaffoldError, match="expérience inconnue"):
        scaffold.new_hpo("img", "x", "nope")


@pytest.mark.parametrize("modality", ["image", "audio", "video"])
def test_plugin_templates(workspace: Path, modality: str):
    created = {kind: scaffold.new_plugin(kind, f"my_{kind}", modality)
               for kind in scaffold.PLUGIN_DIRS}
    for kind, path in created.items():
        assert path == workspace / "lab" / scaffold.PLUGIN_DIRS[kind] / f"my_{kind}.py"
        source = path.read_text()
        ast.parse(source)
        assert "TODO" in source and f'"my_{kind}"' in source
        if kind != "metric":  # params models: strict (extra="forbid") and documented fields
            assert 'model_config = ConfigDict(extra="forbid")' in source, kind
            assert "ConfigDict" in source.split("class ")[0]
            fields = [line for line in source.splitlines()
                      if line.startswith("    ") and "Field(" in line and "= Field(" in line]
            assert fields and all("description=" in line for line in fields), (kind, fields)
    for sub in scaffold.LAB_SUBDIRS:
        assert (workspace / "lab" / sub / "__init__.py").is_file()

    runnable = ["dataset", "preprocess", "metric"]
    if _module_available("wmlab.losses.base"):
        runnable.append("loss")
    if _module_available("wmlab.models.base"):
        runnable.append("model")
    if _module_available("wmlab.attacks.base"):
        runnable.append("attack")
    for kind in runnable:
        results = run_plugin_test(kind, f"my_{kind}", modality)
        assert failed(results) == [], (kind, results)


def test_multi_modality_plugins(workspace: Path):
    path = scaffold.new_plugin("preprocess", "my_iv", "image,video")
    assert 'modality=["image", "video"]' in path.read_text()
    path = scaffold.new_plugin("metric", "my_any", "all")
    assert "modality=None" in path.read_text()
    assert failed(run_plugin_test("metric", "my_any", "audio")) == []
    with pytest.raises(scaffold.ScaffoldError, match="modalité précise"):
        scaffold.new_plugin("dataset", "ds", "all")
    with pytest.raises(scaffold.ScaffoldError, match="type de plugin inconnu"):
        scaffold.new_plugin("optimizer", "x", "image")


def _install_contract_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Minimal §4 base classes, used only while the real engine modules are absent."""
    import sys
    import types
    from dataclasses import dataclass

    from torch import nn

    class WatermarkModel(nn.Module):
        modality = "image"

        def __init__(self, nbits: int) -> None:
            super().__init__()
            self.nbits = nbits

        def forward(self, x, msg):
            return self.embed(x, msg)

        def aux_losses(self, batch):
            return {}

        def param_groups(self):
            return {"all": list(self.parameters())}

    class Attack(nn.Module):
        differentiable = True

        def __init__(self, **params) -> None:
            super().__init__()
            self.params = params

    @dataclass
    class StepBatch:
        x: object
        x_w: object
        x_att: object
        msg: object
        logits: object
        modality: str
        model: object
        step: int
        phase: str
        extras: dict

    stubs = {"wmlab.models.base": {"WatermarkModel": WatermarkModel},
             "wmlab.attacks.base": {"Attack": Attack},
             "wmlab.losses.base": {"StepBatch": StepBatch}}
    for module_name, attrs in stubs.items():
        if _module_available(module_name):
            continue
        package = module_name.rsplit(".", 1)[0]
        if package not in sys.modules:
            monkeypatch.setitem(sys.modules, package, types.ModuleType(package))
        module = types.ModuleType(module_name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, module_name, module)


@pytest.mark.parametrize("modality", ["image", "audio", "video"])
def test_engine_templates_against_contract(workspace: Path, monkeypatch, modality: str):
    _install_contract_stubs(monkeypatch)
    for kind in ("model", "attack", "loss"):
        scaffold.new_plugin(kind, f"tpl_{kind}", modality)
        results = run_plugin_test(kind, f"tpl_{kind}", modality)
        assert failed(results) == [], (kind, results)
