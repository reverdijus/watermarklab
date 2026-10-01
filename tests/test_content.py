"""Content shipped with the repo: reference projects, benchmarks, dataset catalog, Claude
skills (docs + templates) and the remote bootstrap script.

Checks that need builtin components not importable yet are skipped, never failed.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from wmlab.registry import load_plugins
from wmlab.spec import ExperimentSpec, HpoSpec, load_yaml, validate_content, validate_workspace

REPO = Path(__file__).resolve().parent.parent
SKILLS = REPO / ".claude" / "skills"
EXP_SKILL = SKILLS / "wmlab-experiment"
ANA_SKILL = SKILLS / "wmlab-analyse"

PROJECT_FILES = {
    "image-hidden": ["experiments/baseline.yaml", "experiments/resolution_curriculum.yaml",
                     "experiments/wsd_branch.yaml", "hpo/lr_weights.yaml", "hpo/pareto.yaml"],
    "audio-seal-lite": ["experiments/baseline.yaml", "hpo/lr.yaml"],
    "video-framewise": ["experiments/baseline.yaml"],
}
BENCHMARK_LABELS = {
    "image_v1": ["none", "jpeg_q50", "jpeg_q80", "blur_s2", "noise_006", "crop_50", "resize_50",
                 "rotate_10", "brightness_03", "contrast_05", "combo_jpeg_crop"],
    "audio_v1": ["none", "mp3_64k", "aac_64k", "noise_20db", "lowpass_3k", "highpass_500",
                 "resample_8k", "echo_100ms", "time_shift_50ms", "amplitude_m6db"],
    "video_v1": ["none", "h264_crf23", "h264_crf28", "frame_drop_20", "framewise_jpeg50",
                 "temporal_blur"],
}
DATASETS = ["coco2017", "div2k_valid", "librispeech_clean100", "voxpopuli_en", "fma_small", "sav"]


def my_spec_files() -> list[Path]:
    files = [REPO / "projects" / p / "project.yaml" for p in PROJECT_FILES]
    files += [REPO / "projects" / p / f for p, fs in PROJECT_FILES.items() for f in fs]
    files += [REPO / "benchmarks" / f"{b}.yaml" for b in BENCHMARK_LABELS]
    files += [REPO / "datasets" / f"{d}.yaml" for d in DATASETS]
    return files


def builtins_ready() -> bool:
    """True when every builtin component package imports (semantic checks meaningful)."""
    report = load_plugins(REPO)
    return not [e for e in report.errors if e["module"].startswith("wmlab")]


# -- specs ----------------------------------------------------------------------------------
@pytest.mark.parametrize("path", my_spec_files(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_spec_schema_valid(path: Path) -> None:
    assert path.is_file()
    errors = validate_content(path.read_text(encoding="utf-8"), path=path, semantic=False)
    assert errors == []


def test_specs_semantically_valid() -> None:
    if not builtins_ready():
        pytest.skip("composants builtin pas encore tous importables")
    results = validate_workspace(REPO)
    mine = {p.relative_to(REPO).as_posix() for p in my_spec_files()}
    errors = {path: errs for path, errs in results.items() if path in mine and errs}
    assert errors == {}


def test_no_absolute_paths_and_no_nbits_in_specs() -> None:
    for path in my_spec_files():
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if "experiments" in path.parts:
            assert "nbits" not in data["model"].get("params", {})
        if "datasets" in path.parts:
            continue
        text = yaml.safe_dump(data)
        assert not re.search(r"root: ['\"]?/", text), path


def test_image_baseline_runs_fast_on_mac() -> None:
    spec = load_yaml(REPO / "projects/image-hidden/experiments/baseline.yaml", "experiment")
    assert isinstance(spec, ExperimentSpec)
    assert [p.name for p in spec.phases] == ["warmup_decode", "robustness_ramp",
                                             "finetune_imperceptibility"]
    assert spec.total_steps == 1500
    assert spec.data.batch_size == 16 and spec.data.num_workers == 2
    assert spec.data.train.dataset == "synthetic" and spec.data.train.params["size"] == 64
    assert spec.eval.every_steps == 250 and spec.checkpoint.every_steps == 500
    assert spec.samples.every_steps == 500
    assert spec.eval.final_benchmark is not None
    assert spec.eval.final_benchmark.benchmark == "image_v1"
    assert spec.eval.final_benchmark.checkpoints == ["last"]
    assert "@coco2017" in (REPO / "projects/image-hidden/experiments/baseline.yaml").read_text()


def test_resolution_and_wsd_experiments() -> None:
    res = load_yaml(REPO / "projects/image-hidden/experiments/resolution_curriculum.yaml")
    sizes = [p.data.train.params["size"] for p in res.phases if p.data is not None]
    assert res.data.train.params["size"] == 64 and sizes == [128]
    wsd = load_yaml(REPO / "projects/image-hidden/experiments/wsd_branch.yaml")
    assert any(p.lr.schedule == "wsd" for p in wsd.phases)
    assert wsd.init_from is None and wsd.checkpoint.at_phase_end
    assert "phase_end:stable" in (REPO / "projects/image-hidden/experiments/wsd_branch.yaml").read_text()


def test_hpo_plans() -> None:
    mono = load_yaml(REPO / "projects/image-hidden/hpo/lr_weights.yaml", "hpo")
    assert isinstance(mono, HpoSpec) and not mono.multi_objective
    assert mono.objectives[0].metric == "val/bit_acc_mean"
    assert [c.metric for c in mono.constraints] == ["val/psnr"]
    # median: decisions at every validation after n_warmup_steps — ASHA with 900-step trials
    # and min_resource 300 only decided once, at the end of the decoding warmup (see plan)
    assert (mono.sampler.name, mono.pruner.name) == ("tpe", "median")
    assert mono.pruner.params["n_warmup_steps"] >= 450
    assert mono.fidelity.steps_scale == 0.5
    assert mono.space["phases.1.loss_weights.img.ramp.1"].high <= 150
    assert mono.space["phases.2.loss_weights.img"].high <= 300
    audio_plan = load_yaml(REPO / "projects/audio-seal-lite/hpo/lr.yaml", "hpo")
    assert audio_plan.pruner.name == "median" and audio_plan.pruner.params["n_warmup_steps"] > 300
    # the decoding warmup keeps its full length under low fidelity (a 75-step warmup leaves
    # every trial stuck at ~0.5 bit accuracy: measured, see the plan's comment)
    for plan_path in ("projects/image-hidden/hpo/lr_weights.yaml",
                      "projects/image-hidden/hpo/pareto.yaml",
                      "projects/audio-seal-lite/hpo/lr.yaml"):
        plan = load_yaml(REPO / plan_path, "hpo")
        base = load_yaml(REPO / "projects" / plan_path.split("/")[1] / "experiments"
                         / f"{plan.experiment}.yaml")
        assert plan.fidelity.overrides["phases.0.steps"] == base.phases[0].steps
        assert plan.fidelity.overrides["phases.0.lr.warmup_steps"] == base.phases[0].lr.warmup_steps
    assert {"optimizer.lr", "phases.1.attack_strength.ramp.1"} <= set(mono.space)
    pareto = load_yaml(REPO / "projects/image-hidden/hpo/pareto.yaml", "hpo")
    assert pareto.multi_objective and pareto.sampler.name == "nsga2"
    assert [o.metric for o in pareto.objectives] == ["val/psnr", "val/bit_acc_mean"]


def test_modalities_bits_and_losses() -> None:
    expected = {"image-hidden": ("image", 48, "psnr", "image_v1"),
                "audio-seal-lite": ("audio", 16, "snr", "audio_v2"),
                "video-framewise": ("video", 32, "psnr", "video_v1")}
    for name, (modality, bits, quality, bench) in expected.items():
        project = load_yaml(REPO / "projects" / name / "project.yaml", "project")
        assert (project.modality, project.message_bits) == (modality, bits)
        assert project.objectives.quality.metric == quality
        assert project.objectives.robustness.metric == "bit_acc_mean"
        assert project.default_benchmark == bench
    # audio_v1 asked for tpr@1e-06 with a 16-bit message (2^-16 ≈ 1.5e-5): structurally 0.
    # audio_v2 keeps its data and attacks with reachable FPRs; v1 stays published (untouched).
    from wmlab.spec.validate import fpr_reachable

    v1 = load_yaml(REPO / "benchmarks" / "audio_v1.yaml", "benchmark")
    v2 = load_yaml(REPO / "benchmarks" / "audio_v2.yaml", "benchmark")
    assert not all(fpr_reachable(16, f) for f in v1.fpr)
    assert all(fpr_reachable(16, f) for f in v2.fpr) and v2.version == 2
    assert [a.model_dump() for a in v2.attacks] == [a.model_dump() for a in v1.attacks]
    assert v2.data == v1.data and v2.quality_metrics == v1.quality_metrics
    audio = load_yaml(REPO / "projects/audio-seal-lite/experiments/baseline.yaml")
    assert audio.eval.final_benchmark.benchmark == "audio_v2"
    assert {t.name for t in audio.loss.terms.values()} == {"message_bce", "stft", "mse"}
    video = load_yaml(REPO / "projects/video-framewise/experiments/baseline.yaml")
    assert video.model.name == "video_framewise"
    assert video.data.train.params["frames"] == 8 and video.data.train.params["size"] == 64
    assert {t.name for t in video.loss.terms.values()} == {"message_bce", "mse", "temporal"}
    names = {a.name for p in video.phases for a in p.attacks}
    assert {"framewise", "h264", "frame_drop"} <= names


@pytest.mark.parametrize("name", list(BENCHMARK_LABELS))
def test_benchmarks(name: str) -> None:
    bench = load_yaml(REPO / "benchmarks" / f"{name}.yaml", "benchmark")
    assert bench.version == 1 and bench.name == name
    assert [a.label for a in bench.attacks] == BENCHMARK_LABELS[name]
    assert bench.attacks[0].name == "identity"
    assert bench.data.dataset == "synthetic" and bench.data.params["num_items"] == 64
    assert "seed" in bench.data.params and bench.data.limit == 64
    assert "ne jamais modifier" in bench.description


@pytest.mark.parametrize("name", DATASETS)
def test_dataset_catalog(name: str) -> None:
    text = (REPO / "datasets" / f"{name}.yaml").read_text(encoding="utf-8")
    entry = load_yaml(REPO / "datasets" / f"{name}.yaml", "dataset")
    assert entry.source.startswith("https://")
    assert entry.license.strip()
    assert entry.paths["default"].startswith("~/datasets/")
    assert re.search(r"#\s*scw-[\w-]+:", text), "exemple commenté d'hôte Scaleway attendu"


# -- skills ---------------------------------------------------------------------------------
def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert match, f"frontmatter YAML absent : {path}"
    return yaml.safe_load(match.group(1))


@pytest.mark.parametrize("skill", [EXP_SKILL, ANA_SKILL], ids=lambda p: p.name)
def test_skill_frontmatter_and_links(skill: Path) -> None:
    meta = frontmatter(skill / "SKILL.md")
    assert meta["name"] == skill.name
    assert 200 < len(meta["description"]) <= 1024
    body = (skill / "SKILL.md").read_text(encoding="utf-8")
    for ref in set(re.findall(r"`((?:references|templates)/[\w.-]+)`", body)):
        assert (skill / ref).is_file(), f"{skill.name}: lien cassé {ref}"


def test_experiment_skill_files() -> None:
    for ref in ("interfaces.md", "spec-reference.md", "curriculum-patterns.md", "hpo-guide.md",
                "checklist.md"):
        assert (EXP_SKILL / "references" / ref).is_file()
    for tpl in ("model.py", "attack.py", "dataset.py", "loss.py"):
        path = EXP_SKILL / "templates" / tpl
        compile(path.read_text(encoding="utf-8"), str(path), "exec")   # syntax only, no .pyc
    assert (ANA_SKILL / "references" / "diagnostics.md").is_file()


def test_yaml_templates_schema_valid() -> None:
    for name, kind in (("experiment.yaml", "experiment"), ("hpo.yaml", "hpo"),
                       ("benchmark.yaml", "benchmark")):
        text = (EXP_SKILL / "templates" / name).read_text(encoding="utf-8")
        assert validate_content(text, kind=kind, semantic=False) == [], name


def test_yaml_snippets_in_curriculum_patterns_parse() -> None:
    text = (EXP_SKILL / "references" / "curriculum-patterns.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.S)
    assert len(blocks) >= 8
    for block in blocks:
        yaml.safe_load(block)


def test_yaml_templates_semantically_valid(workspace: Path) -> None:
    if not builtins_ready():
        pytest.skip("composants builtin pas encore tous importables")
    for folder in ("benchmarks", "datasets"):
        for file in (REPO / folder).glob("*.yaml"):
            shutil.copy(file, workspace / folder / file.name)
    project = workspace / "projects" / "demo"
    (project / "experiments").mkdir(parents=True)
    (project / "hpo").mkdir()
    shutil.copy(REPO / "projects/image-hidden/project.yaml", project / "project.yaml")
    (project / "project.yaml").write_text(
        (project / "project.yaml").read_text().replace("name: image-hidden", "name: demo"))
    exp = (EXP_SKILL / "templates" / "experiment.yaml").read_text()
    (project / "experiments" / "baseline.yaml").write_text(
        exp.replace("name: my_experiment", "name: baseline"))
    hpo = (EXP_SKILL / "templates" / "hpo.yaml").read_text()
    (project / "hpo" / "my_plan.yaml").write_text(hpo)
    shutil.copy(EXP_SKILL / "templates" / "benchmark.yaml", workspace / "benchmarks" / "image_v2.yaml")
    results = validate_workspace(workspace)
    assert {p: e for p, e in results.items() if e} == {}


# -- Python templates through plugin-test -----------------------------------------------------
def install_template(workspace: Path, template: str, kind_dir: str, name: str) -> None:
    folder = workspace / "lab" / kind_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "__init__.py").touch()
    shutil.copy(EXP_SKILL / "templates" / template, folder / f"{name}.py")


def assert_plugin_ok(kind: str, name: str, workspace: Path, **kwargs) -> None:
    from wmlab.plugin_test import run_plugin_test

    results = run_plugin_test(kind, name, workspace=workspace, **kwargs)
    failed = [r for r in results if not r["ok"]]
    assert results and not failed, failed


def test_model_template_passes_plugin_test(workspace: Path) -> None:
    pytest.importorskip("wmlab.models.base")
    install_template(workspace, "model.py", "models", "unet_wm")
    assert_plugin_ok("model", "unet_wm", workspace)


def test_attack_templates_pass_plugin_test(workspace: Path) -> None:
    pytest.importorskip("wmlab.attacks.base")
    install_template(workspace, "attack.py", "attacks", "my_attacks")
    assert_plugin_ok("attack", "gamma", workspace)
    assert_plugin_ok("attack", "webp", workspace)


def test_templates_coexist_with_shipped_lab_examples(workspace: Path) -> None:
    """Copied as-is next to lab/attacks/example_pixelate.py and lab/models/example_residual_unet.py,
    the templates must not reuse a registry name: a duplicate makes the whole module fail to
    import (and every other component of the file disappears)."""
    pytest.importorskip("wmlab.attacks.base")
    for kind_dir, example in (("attacks", "example_pixelate.py"), ("models", "example_residual_unet.py")):
        folder = workspace / "lab" / kind_dir
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").touch()
        shutil.copy(REPO / "lab" / kind_dir / example, folder / example)
    install_template(workspace, "attack.py", "attacks", "zz_attack_template")
    install_template(workspace, "model.py", "models", "zz_model_template")

    from wmlab.registry import ATTACKS, MODELS

    report = load_plugins(workspace)
    assert [e for e in report.errors if e["module"].startswith("lab.")] == []
    assert {"pixelate", "gamma", "webp"} <= set(ATTACKS.names())
    assert {"example_unet", "unet_wm"} <= set(MODELS.names())
    assert ATTACKS.get("gamma").source == "lab.attacks.zz_attack_template"
    assert MODELS.get("unet_wm").source == "lab.models.zz_model_template"


def test_loss_template_passes_plugin_test(workspace: Path) -> None:
    pytest.importorskip("wmlab.losses.base")
    install_template(workspace, "loss.py", "losses", "residual_tv")
    for modality in ("image", "audio", "video"):
        assert_plugin_ok("loss", "residual_tv", workspace, modality=modality)


def test_dataset_template_passes_plugin_test(workspace: Path, tmp_path: Path) -> None:
    data = pytest.importorskip("wmlab.data")
    if not hasattr(data, "resolve_path"):
        pytest.skip("wmlab.data.resolve_path pas encore disponible")
    import numpy as np
    from PIL import Image

    images = tmp_path / "images"
    (images / "sub").mkdir(parents=True)
    rng = np.random.default_rng(0)
    for i in range(12):
        target = images / ("sub" if i % 2 else "") / f"{i:02d}.png"
        Image.fromarray((rng.random((20, 24, 3)) * 255).astype("uint8")).save(target)
    install_template(workspace, "dataset.py", "datasets", "image_split_folder")
    assert_plugin_ok("dataset", "image_split_folder", workspace, params={"root": str(images)})

    from wmlab.registry import DATASETS

    build = lambda split: DATASETS.build("image_split_folder", root=str(images), split=split,  # noqa: E731
                                         val_fraction=0.3)
    train, val, full = build("train"), build("val"), build("all")
    train_ids = {train[i]["id"] for i in range(len(train))}
    val_ids = {val[i]["id"] for i in range(len(val))}
    assert not train_ids & val_ids and len(train_ids | val_ids) == len(full) == 12
    assert full[0]["x"].shape == (3, 20, 24)


# -- remote bootstrap -------------------------------------------------------------------------
FAKE_UV = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "uv 0.0.0-test"; exit 0; fi
if [ "$1" = "sync" ]; then
  mkdir -p .venv/bin && cp "$FAKE_PYTHON" .venv/bin/python && chmod +x .venv/bin/python
  echo "fake uv sync $*"; exit 0
fi
exit 2
"""
FAKE_PYTHON = """#!/bin/sh
if [ "$1" = "-" ]; then
  cat >/dev/null
  echo "  torch           : 2.x (CUDA 12.4)"
  echo "CUDA_OK=${FAKE_CUDA:-1}"
  exit 0
fi
if [ "$1" = "-m" ]; then echo "wmlab 0.1.0"; exit 0; fi
exit 3
"""
FAKE_NVIDIA_SMI = """#!/bin/sh
if [ -z "$1" ]; then exit 0; fi
echo "NVIDIA L40S, 46068 MiB"
"""


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def bootstrap_env(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A fake workdir with the script and stub commands (never touches the real venv)."""
    workdir = tmp_path / "wmlab"
    (workdir / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "remote_bootstrap.sh", workdir / "scripts" / "remote_bootstrap.sh")
    (workdir / "pyproject.toml").write_text("[project]\nname = 'fake'\n")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _exe(stubs / "uv", FAKE_UV)
    _exe(stubs / "nvidia-smi", FAKE_NVIDIA_SMI)
    _exe(tmp_path / "fake_python", FAKE_PYTHON)
    for tool in ("curl", "rsync"):
        _exe(stubs / tool, "#!/bin/sh\nexit 0\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": f"{stubs}:/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home),
           "FAKE_PYTHON": str(tmp_path / "fake_python"), "LANG": "C.UTF-8"}
    return workdir, env


def run_bootstrap(workdir: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "scripts/remote_bootstrap.sh", *args], cwd=workdir, env=env,
                          capture_output=True, text=True, timeout=60)


def test_bootstrap_script_syntax() -> None:
    script = REPO / "scripts" / "remote_bootstrap.sh"
    assert os.access(script, os.X_OK)
    text = script.read_text()
    assert "set -euo pipefail" in text and "uv sync --extra train" in text
    # pinned installer version, never ``curl … | sh``; optional sha256 check of the installer
    assert 'https://astral.sh/uv/${UV_VERSION}/install.sh' in text and "| sh\n" not in text
    assert 'UV_VERSION="${UV_VERSION:-' in text and "UV_INSTALLER_SHA256" in text
    subprocess.run(["bash", "-n", str(script)], check=True)


def test_bootstrap_installs_pinned_uv(bootstrap_env: tuple[Path, dict[str, str]],
                                      tmp_path: Path) -> None:
    """Without uv: the versioned installer is downloaded to a file and its checksum checked."""
    workdir, env = bootstrap_env
    stubs = Path(env["PATH"].split(":")[0])
    (stubs / "uv").unlink()
    log = tmp_path / "curl.log"
    installer = "#!/bin/sh\necho installed\n"
    _exe(stubs / "curl", f'#!/bin/sh\necho "$@" >> {log}\nprintf %s "$INSTALLER" > "$4"\n')
    env = {**env, "INSTALLER": installer, "UV_VERSION": "0.12.19"}
    result = run_bootstrap(workdir, env)
    assert result.returncode != 0  # the fake installer does not install uv
    assert "https://astral.sh/uv/0.12.19/install.sh" in log.read_text()
    assert "non vérifié" in result.stdout + result.stderr
    import hashlib

    wrong = {**env, "UV_INSTALLER_SHA256": "0" * 64}
    result = run_bootstrap(workdir, wrong)
    assert result.returncode != 0 and "somme de contrôle" in result.stderr
    right = {**env, "UV_INSTALLER_SHA256": hashlib.sha256(installer.encode()).hexdigest()}
    result = run_bootstrap(workdir, right)
    assert "installateur uv vérifié" in result.stdout


def test_bootstrap_is_idempotent(bootstrap_env: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    workdir, env = bootstrap_env
    data = tmp_path / "wmlab-data"
    for _ in range(2):
        result = run_bootstrap(workdir, env, str(data))
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Serveur prêt" in result.stdout and "L40S" in result.stdout
    assert (data / "runs").is_dir() and (data / "studies").is_dir()
    assert (workdir / ".venv" / "bin" / "python").is_file()


def test_bootstrap_uses_wmlab_data_then_default(bootstrap_env: tuple[Path, dict[str, str]],
                                                tmp_path: Path) -> None:
    workdir, env = bootstrap_env
    result = run_bootstrap(workdir, {**env, "WMLAB_DATA": str(tmp_path / "from-env")})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "from-env" / "runs").is_dir()
    assert run_bootstrap(workdir, env).returncode == 0
    assert (workdir / ".wmlab" / "runs").is_dir()


def test_bootstrap_requires_gpu(bootstrap_env: tuple[Path, dict[str, str]]) -> None:
    workdir, env = bootstrap_env
    (Path(env["PATH"].split(":")[0]) / "nvidia-smi").unlink()
    result = run_bootstrap(workdir, env)
    assert result.returncode != 0 and "nvidia-smi" in result.stderr
    assert not (workdir / ".venv").exists()          # stopped before uv sync
    allowed = run_bootstrap(workdir, {**env, "WMLAB_ALLOW_NO_GPU": "1"})
    assert allowed.returncode == 0, allowed.stderr


def test_bootstrap_fails_when_torch_has_no_cuda(bootstrap_env: tuple[Path, dict[str, str]]) -> None:
    workdir, env = bootstrap_env
    result = run_bootstrap(workdir, {**env, "FAKE_CUDA": "0"})
    assert result.returncode != 0 and "torch.cuda.is_available()" in result.stderr


# -- documentation vs CLI ----------------------------------------------------------------------
def cli_commands_and_options() -> dict[str, list[str]]:
    """``{"runs list": ["--include-trials", ...], ...}`` from the Typer app (``--help`` excluded)."""
    from typer.main import get_command

    from wmlab.cli import app

    def walk(cmd, prefix: list[str]):
        subcommands = getattr(cmd, "commands", None)
        if subcommands:
            for name, sub in subcommands.items():
                yield from walk(sub, prefix + [name])
        else:
            opts = {o for p in cmd.params for o in getattr(p, "opts", []) if o.startswith("--")}
            yield " ".join(prefix), sorted(opts - {"--help"})

    return dict(walk(get_command(app), []))


def cli_doc_block(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"```\n(.*?)```", text[text.index(heading):], re.S)
    assert match, f"{path.name} : bloc de commandes introuvable sous « {heading} »"
    return match.group(1)


@pytest.mark.parametrize("doc, heading", [("README.md", "## 7. Ligne de commande"),
                                          ("ARCHITECTURE.md", "### 8.4 CLI")])
def test_cli_tables_list_every_command_and_option(doc: str, heading: str) -> None:
    """The CLI tables of README §7 and ARCHITECTURE §8.4 must show every command with all
    its options (they are what the skills and users copy from)."""
    block = cli_doc_block(REPO / doc, heading)
    lines = block.splitlines()
    missing: list[str] = []
    for command, options in cli_commands_and_options().items():
        line = next((l for l in lines if l.startswith(f"wmlab {command} ") or l == f"wmlab {command}"), None)
        if line is None:
            missing.append(f"{command} (commande absente)")
            continue
        missing += [f"{command} {opt}" for opt in options if opt not in line]
    assert missing == [], f"{doc} : options CLI non documentées : {missing}"


def test_docs_describe_real_behaviours() -> None:
    """Facts checked against the code elsewhere in the suite, restated in the docs."""
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    arch = (REPO / "ARCHITECTURE.md").read_text(encoding="utf-8")
    exp_skill = (EXP_SKILL / "SKILL.md").read_text(encoding="utf-8")
    ana_skill = (ANA_SKILL / "SKILL.md").read_text(encoding="utf-8")
    # samples: the trainer exports original / watermarked / diff (audio: residual), never "attacked"
    assert "attaqué)" not in readme and "différence amplifiée" in readme
    for yaml_file in ("projects/image-hidden/experiments/baseline.yaml",
                      "projects/audio-seal-lite/experiments/baseline.yaml",
                      "projects/video-framewise/experiments/baseline.yaml"):
        line = next(l for l in (REPO / yaml_file).read_text(encoding="utf-8").splitlines()
                    if l.startswith("samples:"))
        assert "attaqu" not in line, yaml_file
    # registry description = whole first paragraph of the docstring (wmlab.registry.first_paragraph)
    assert "1re phrase du docstring" not in exp_skill and "1er" in exp_skill
    assert "1re phrase" not in (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    # names already taken by the shipped lab/ examples are spelled out for the skill
    assert "pixelate" in exp_skill and "example_unet" in exp_skill and "--modality M" in exp_skill
    # single Node bound in both READMEs (Electron 44 requires >= 22.12)
    assert "Node.js ≥ 22.12" in readme
    assert "Node ≥ 22.12" in (REPO / "desktop" / "README.md").read_text(encoding="utf-8")
    # HPO study format and public health route documented as implemented
    assert "n_pruned" in arch and "feasible" in arch and "__smoke.json" in arch
    assert "`GET /api/health` est public" in arch and "list_studies" in arch and "StudyReader" in arch
    # analysis skill: MPS has no gpu_util, notes.md is excluded from the pull rsync
    assert "proc_ram_gb" in ana_skill and "--exclude notes.md" in ana_skill
