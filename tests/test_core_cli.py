"""CLI commands owned by the core: validate, registry, plugin-test, new, runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from helpers_core import benchmark_dict, experiment_dict, install_fake_plugins, make_project, write_yaml
from wmlab import __version__
from wmlab.cli import app
from wmlab.spec import load_yaml, validate_file
from wmlab.tracking import Run

runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


@pytest.fixture
def fake_ws(workspace: Path) -> Path:
    install_fake_plugins(workspace)
    make_project(workspace)
    write_yaml(workspace / "benchmarks" / "image_v1.yaml", benchmark_dict())
    return workspace


def test_version_and_help():
    result = invoke("--version")
    assert result.exit_code == 0 and __version__ in result.output
    result = invoke("--help")
    assert result.exit_code == 0 and "plugin-test" in result.output


def test_validate_file_and_workspace(fake_ws: Path):
    result = invoke("validate", "projects/demo/experiments/baseline.yaml")
    assert result.exit_code == 0, result.output
    assert "✓" in result.output and "1 fichier(s) valide(s)" in result.output
    result = invoke("validate", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["valid"] is True
    assert data["files"]["projects/demo/project.yaml"] == []
    assert "benchmarks/image_v1.yaml" in data["files"]


def test_validate_reports_errors(fake_ws: Path):
    write_yaml(fake_ws / "projects/demo/experiments/bad.yaml",
               experiment_dict(name="bad", model={"name": "_test_unet"}))
    result = invoke("validate", "projects/demo/experiments/bad.yaml")
    assert result.exit_code == 1
    assert "model.name" in result.output and "modèle inconnu '_test_unet'" in result.output
    result = invoke("validate")
    assert result.exit_code == 1 and "1 invalide(s)" in result.output
    result = invoke("validate", "projects/demo", "--json")
    assert result.exit_code == 1
    files = json.loads(result.stdout)["files"]
    assert any(path.endswith("bad.yaml") and errors for path, errors in files.items())


def test_validate_file_reports_broken_plugins(fake_ws: Path):
    """A lab/ plugin that does not import is reported (exit 1) even when explicit paths are
    validated — the experiment may silently use another plugin providing the same name."""
    (fake_ws / "lab" / "attacks").mkdir(parents=True, exist_ok=True)
    (fake_ws / "lab" / "attacks" / "__init__.py").touch()
    (fake_ws / "lab" / "attacks" / "zz_broken.py").write_text("def broken(:\n", encoding="utf-8")
    result = invoke("validate", "projects/demo/experiments/baseline.yaml")
    assert result.exit_code == 1, result.output
    assert "zz_broken.py" in result.output and "import du plugin impossible" in result.output
    assert "✓ projects/demo/experiments/baseline.yaml" in result.output
    result = invoke("validate", "projects/demo/experiments/baseline.yaml", "--json")
    files = json.loads(result.stdout)["files"]
    assert files["lab/attacks/zz_broken.py"][0]["msg"].startswith("import du plugin impossible")


def test_registry_command(fake_ws: Path):
    result = invoke("registry", "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert set(data["entries"]) == {"model", "attack", "dataset", "preprocess", "loss", "metric"}
    models = {e["name"]: e for e in data["entries"]["model"]}
    assert models["_test_model"]["modality"] == ["image"]
    assert models["_test_model"]["params_schema"]["properties"]["channels"]["default"] == 8
    assert isinstance(data["errors"], list)
    result = invoke("registry", "--kind", "metric")
    assert result.exit_code == 0 and "_test_psnr" in result.output
    assert invoke("registry", "--kind", "optimizer").exit_code == 1


def test_plugin_test_command(fake_ws: Path):
    result = invoke("plugin-test", "model", "_test_model")
    assert result.exit_code == 0, result.output
    assert "Toutes les vérifications passent" in result.output
    (fake_ws / "lab" / "bad_model.py").write_text(
        "import torch\nfrom torch import nn\nfrom wmlab.registry import MODELS\n\n"
        "@MODELS.register('_test_bad', modality='image')\n"
        "class Bad(nn.Module):\n"
        "    modality = 'image'\n"
        "    def __init__(self, nbits):\n"
        "        super().__init__(); self.nbits = nbits; self.w = nn.Parameter(torch.ones(1))\n"
        "    def embed(self, x, msg):\n        return x[..., :-1] * self.w\n"
        "    def extract(self, x):\n        return torch.zeros(x.shape[0], self.nbits)\n")
    result = invoke("plugin-test", "model", "_test_bad", "--json")
    assert result.exit_code == 1
    checks = {c["check"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["embed : forme"]["ok"] is False
    assert invoke("plugin-test", "model", "_test_nothing").exit_code == 1


def test_new_commands(workspace: Path):
    result = invoke("new", "project", "img", "--modality", "image", "--bits", "32",
                    "--title", "Image test")
    assert result.exit_code == 0, result.output
    project = load_yaml(workspace / "projects/img/project.yaml")
    assert project.message_bits == 32 and project.title == "Image test"
    assert (workspace / "projects/img/experiments/baseline.yaml").is_file()
    result = invoke("new", "project", "img", "--modality", "image")
    assert result.exit_code == 1 and "existe déjà" in result.output
    assert invoke("new", "project", "x", "--modality", "texte").exit_code == 1

    result = invoke("new", "experiment", "img", "second", "--from", "baseline")
    assert result.exit_code == 0, result.output
    assert load_yaml(workspace / "projects/img/experiments/second.yaml").name == "second"
    assert invoke("new", "experiment", "nope", "x").exit_code == 1

    result = invoke("new", "hpo", "img", "sweep", "--experiment", "baseline")
    assert result.exit_code == 0, result.output
    assert validate_file(workspace / "projects/img/hpo/sweep.yaml") == []

    result = invoke("new", "plugin", "metric", "my_metric", "--modality", "image")
    assert result.exit_code == 0, result.output
    assert (workspace / "lab/metrics/my_metric.py").is_file()
    result = invoke("plugin-test", "metric", "my_metric")
    assert result.exit_code == 0, result.output
    assert invoke("new", "plugin", "model", "Bad-Name", "--modality", "image").exit_code == 1
    assert invoke("new", "plugin", "model", "net", "--modality", "all").exit_code == 1


def test_runs_list_and_show(workspace: Path):
    run = Run.create(project="demo", experiment="baseline", modality="image",
                     spec=experiment_dict(), capture_env=False, monitor_system=False)
    run.log_metrics(10, {"val/bit_acc_mean": 0.9})
    run.set_checkpoint_policy(2, {"val/bit_acc_mean": "max"})
    run.save_checkpoint({"model": {}}, step=10, phase="warmup", metrics={"val/bit_acc_mean": 0.9})
    run.log_eval({"checkpoint": "step_000010", "benchmark": "image_v1", "summary": {"psnr": 40.0}})
    run.finish()
    other = Run.create(project="other", experiment="x", modality="audio", spec={"name": "x"},
                       capture_env=False, monitor_system=False, study="s1", trial=0)
    other.finish("failed", "boom")

    result = invoke("runs", "list", "--json")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [r["run_id"] for r in rows] == [run.run_id]  # trials hidden by default
    assert rows[0]["status"] == "completed" and "cost_eur" in rows[0]
    rows = json.loads(invoke("runs", "list", "--json", "--include-trials").stdout)
    assert len(rows) == 2
    rows = json.loads(invoke("runs", "list", "--json", "--include-trials",
                             "--status", "failed").stdout)
    assert [r["run_id"] for r in rows] == [other.run_id]
    assert json.loads(invoke("runs", "list", "--json", "--project", "nope").stdout) == []
    human = invoke("runs", "list")
    assert human.exit_code == 0 and "demo" in human.output

    fragment = run.run_id.split("-", 2)[2]
    result = invoke("runs", "show", fragment, "--json")
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["manifest"]["run_id"] == run.run_id
    assert data["summary"]["val/bit_acc_mean"] == 0.9
    assert data["checkpoints"][0]["name"] == "step_000010"
    assert data["evals"][0]["summary"]["psnr"] == 40.0
    assert "val/bit_acc_mean" in data["metric_keys"]
    human = invoke("runs", "show", run.run_id)
    assert human.exit_code == 0 and "step_000010" in human.output
    assert invoke("runs", "show", "does-not-exist").exit_code == 1


def test_delegated_commands_fail_cleanly(workspace: Path):
    result = invoke("train", "x.yaml", "--set", "novalue")
    assert result.exit_code == 1 and "clé=valeur" in result.output
    result = invoke("train", "does/not/exist.yaml")
    assert result.exit_code != 0
