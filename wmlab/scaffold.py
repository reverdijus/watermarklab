"""Generators for projects, experiments, HPO plans and plugin skeletons.

Templates are commented in French. Existing files are never overwritten.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template
from typing import Any

import yaml

from wmlab.registry import normalize_modality
from wmlab.spec.models import DEFAULT_QUALITY_METRIC, MODALITIES, NAME_PATTERN

PLUGIN_DIRS: dict[str, str] = {
    "model": "models", "attack": "attacks", "dataset": "datasets",
    "preprocess": "preprocess", "loss": "losses", "metric": "metrics",
}
LAB_SUBDIRS = ("models", "attacks", "datasets", "losses", "metrics", "preprocess")
PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
QUALITY_THRESHOLD = {"image": 36, "audio": 25, "video": 34}


class ScaffoldError(ValueError):
    """Invalid scaffold request (French message)."""


# -- helpers --------------------------------------------------------------------------------
def _root(workspace: Path | None) -> Path:
    if workspace is not None:
        return Path(workspace).expanduser().resolve()
    from wmlab.paths import workspace_root

    return workspace_root()


def _check_name(name: str, what: str) -> str:
    if not re.match(NAME_PATTERN, name or ""):
        raise ScaffoldError(f"nom de {what} invalide '{name}' — lettres, chiffres, '_', '-' ou "
                            "'.' uniquement (64 caractères max.)")
    return name


def _check_modality(modality: str) -> str:
    if modality not in MODALITIES:
        raise ScaffoldError(f"modalité invalide '{modality}' — valeurs possibles : "
                            f"{', '.join(MODALITIES)}")
    return modality


def _write_new(path: Path, text: str) -> Path:
    if path.exists():
        raise FileExistsError(f"le fichier existe déjà : {path} (rien n'a été écrit)")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _q(text: str) -> str:
    """YAML double-quoted scalar."""
    return json.dumps(text, ensure_ascii=False)


def _camel(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[_\-.]+", name) if part) or "Plugin"


def _project_dir(root: Path, project: str) -> Path:
    project_dir = root / "projects" / project
    if not (project_dir / "project.yaml").is_file():
        names = sorted(p.parent.name for p in (root / "projects").glob("*/project.yaml"))
        raise ScaffoldError(f"projet inconnu '{project}' — disponibles : "
                            f"{', '.join(names) if names else '(aucun)'}")
    return project_dir


def _project_modality(project_dir: Path) -> str:
    try:
        data = yaml.safe_load((project_dir / "project.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ScaffoldError(f"project.yaml illisible : {exc}") from None
    modality = data.get("modality")
    if modality not in MODALITIES:
        raise ScaffoldError("project.yaml : modalité absente ou invalide")
    return modality


def _default_benchmark(root: Path, modality: str) -> str | None:
    name = f"{modality}_v1"
    return name if (root / "benchmarks" / f"{name}.yaml").is_file() else None


# -- project --------------------------------------------------------------------------------
PROJECT_TEMPLATE = Template("""\
# Projet « $title » — format : ARCHITECTURE.md §5.2
name: $name
title: $title_q
modality: $modality                  # image | audio | video (fixé pour tout le projet)
description: $description_q
message_bits: $bits                  # taille du message = nbits des modèles
default_benchmark: $benchmark        # benchmark de référence (benchmarks/<nom>.yaml) ou null
objectives:                          # axes du front de Pareto imperceptibilité × robustesse
  quality:    {metric: $quality, direction: maximize, label: "$quality_label"}
  robustness: {metric: bit_acc_mean, direction: maximize, label: "Bit acc. moyenne"}
tags: [$modality]
""")

PROJECT_NOTES = Template("""\
# $title

Journal du projet : objectifs, décisions, pistes à explorer.

## Objectif

$description

## Journal

""")


def new_project(name: str, modality: str, bits: int = 48, *, title: str | None = None,
                description: str = "", workspace: Path | None = None,
                with_baseline: bool = True) -> Path:
    """Create ``projects/<name>/`` (project.yaml, notes.md, experiments/baseline.yaml, hpo/)."""
    root = _root(workspace)
    _check_name(name, "projet")
    _check_modality(modality)
    if not isinstance(bits, int) or bits <= 0 or bits > 4096:
        raise ScaffoldError("message_bits doit être un entier entre 1 et 4096")
    project_dir = root / "projects" / name
    if (project_dir / "project.yaml").exists():
        raise FileExistsError(f"le projet '{name}' existe déjà ({project_dir / 'project.yaml'})")
    title = title or name
    quality = DEFAULT_QUALITY_METRIC[modality]
    benchmark = _default_benchmark(root, modality)
    text = PROJECT_TEMPLATE.substitute(
        name=name, title=title, title_q=_q(title), modality=modality,
        description_q=_q(description), bits=bits, benchmark=benchmark or "null",
        quality=quality, quality_label="SNR (dB)" if quality == "snr" else "PSNR (dB)",
    )
    path = _write_new(project_dir / "project.yaml", text)
    (project_dir / "experiments").mkdir(exist_ok=True)
    (project_dir / "hpo").mkdir(exist_ok=True)
    if not (project_dir / "notes.md").exists():
        _write_new(project_dir / "notes.md", PROJECT_NOTES.substitute(
            title=title, description=description or "_(à compléter)_"))
    if with_baseline and not (project_dir / "experiments" / "baseline.yaml").exists():
        new_experiment(name, "baseline", workspace=root)
    return path


# -- experiment -----------------------------------------------------------------------------
EXPERIMENT_HEADER = Template("""\
# Expérience « $name » — projet $project ($modality)
# Format : ARCHITECTURE.md §5.3
# Vérifier : wmlab validate <ce fichier>  ·  essai rapide : wmlab train <ce fichier> --smoke
name: $name
description: "À compléter : ce que cette expérience teste"
hypothesis: "À compléter : résultat attendu et pourquoi"
seed: 0

""")

EXPERIMENT_FOOTER = Template("""\

eval:
  every_steps: 1000                  # validation périodique (et en fin de phase)
  val_batches: 10
  val_attacks:                       # val/bit_acc/<label> ; val/bit_acc_mean = moyenne
$val_attacks
  final_benchmark: $final_benchmark

checkpoint:
  every_steps: 2000
  keep_last: 2                       # derniers checkpoints conservés
  keep_best: {"val/bit_acc_mean": max, "val/$quality": max}
  at_phase_end: true                 # un checkpoint par fin de phase (branchements init_from)

samples: {every_steps: 2000, count: 4}
resources: {device: auto, precision: auto, compile: false}   # auto : cuda > mps > cpu
init_from: null                      # ex. {run: <run_id>, checkpoint: "phase_end:warmup_decode", start_phase: 1}
tags: []
""")

EXPERIMENT_BODY: dict[str, str] = {
    "image": """\
# Architecture (liste : wmlab registry --kind model). nbits vient de project.yaml.
model:
  name: hidden
  params: {channels: 64}

data:
  train:
    dataset: synthetic               # ou image_folder avec root: "@<dataset du catalogue>"
    params: {modality: image, size: 128, num_items: 2048}
    preprocess: [{name: random_crop, params: {size: 128}}]
  val:
    dataset: synthetic
    params: {modality: image, size: 128, num_items: 128, seed: 1}
    preprocess: []
  batch_size: 32
  num_workers: 4

optimizer: {name: adamw, lr: 1.0e-3, weight_decay: 1.0e-4, grad_clip: 1.0}

# Termes de perte ; chaque phase peut moduler leurs poids (loss_weights).
loss:
  terms:
    msg: {name: message_bce, weight: 1.0}    # décodage du message
    img: {name: mse, weight: 0.7}            # imperceptibilité

# Curriculum : les phases s'enchaînent, chacune avec son LR, ses poids et ses attaques.
phases:
  - name: warmup_decode              # apprendre à décoder sans attaque
    steps: 2000
    lr: {schedule: linear_warmup, warmup_steps: 500}
    loss_weights: {img: 0.1}
    attacks: []                      # identité seulement
  - name: robustness_ramp            # attaques de plus en plus fortes
    steps: 10000
    lr: {schedule: constant}
    loss_weights: {img: {ramp: [0.1, 1.0]}}
    attacks:
      - {name: jpeg_diff, params: {quality: 50}}
      - {name: gaussian_blur, params: {sigma: 2.0}}
      - {name: crop, params: {area: 0.5}}
    attack_strength: {ramp: [0.0, 1.0]}
  - name: finetune_imperceptibility  # affiner la qualité à robustesse fixée
    steps: 3000
    lr: {schedule: cosine, min_factor: 0.01}
    loss_weights: {img: 2.0}
    attacks: [{name: jpeg, params: {quality: 50}}, {name: gaussian_blur, params: {sigma: 2.0}}]
    attack_strength: 1.0
""",
    "audio": """\
# Architecture (liste : wmlab registry --kind model). nbits vient de project.yaml.
model:
  name: audio_conv
  params: {channels: 32}

data:
  train:
    dataset: synthetic               # ou audio_folder avec root: "@<dataset du catalogue>"
    params: {modality: audio, length: 16000, num_items: 2048}
    preprocess: []
  val:
    dataset: synthetic
    params: {modality: audio, length: 16000, num_items: 128, seed: 1}
    preprocess: []
  batch_size: 32
  num_workers: 4
  sample_rate: 16000

optimizer: {name: adamw, lr: 1.0e-3, weight_decay: 1.0e-4, grad_clip: 1.0}

# Termes de perte ; chaque phase peut moduler leurs poids (loss_weights).
loss:
  terms:
    msg: {name: message_bce, weight: 1.0}    # décodage du message
    wav: {name: mse, weight: 1.0}            # fidélité de la forme d'onde
    spec: {name: stft, weight: 0.5}          # fidélité spectrale multi-résolution

# Curriculum : les phases s'enchaînent, chacune avec son LR, ses poids et ses attaques.
phases:
  - name: warmup_decode              # apprendre à décoder sans attaque
    steps: 2000
    lr: {schedule: linear_warmup, warmup_steps: 500}
    loss_weights: {wav: 0.1, spec: 0.1}
    attacks: []
  - name: robustness_ramp            # attaques de plus en plus fortes
    steps: 10000
    lr: {schedule: constant}
    loss_weights: {wav: {ramp: [0.1, 1.0]}, spec: {ramp: [0.1, 0.5]}}
    attacks:
      - {name: gaussian_noise, params: {snr_db: 20}}
      - {name: lowpass, params: {cutoff_hz: 3000}}
      - {name: amplitude, params: {gain_db: -6}}
      - {name: echo, params: {delay_ms: 100, decay: 0.3}}
    attack_strength: {ramp: [0.0, 1.0]}
  - name: finetune_imperceptibility  # affiner la qualité à robustesse fixée
    steps: 3000
    lr: {schedule: cosine, min_factor: 0.01}
    loss_weights: {wav: 2.0, spec: 1.0}
    attacks: [{name: resample, params: {rate: 8000}}, {name: gaussian_noise, params: {snr_db: 20}}]
    attack_strength: 1.0
""",
    "video": """\
# Architecture (liste : wmlab registry --kind model). nbits vient de project.yaml.
model:
  name: video_framewise
  params: {image_model: hidden, image_params: {channels: 32}, aggregate: mean}

data:
  train:
    dataset: synthetic               # ou video_folder avec root: "@<dataset du catalogue>"
    params: {modality: video, size: 64, frames: 8, num_items: 1024}
    preprocess: []
  val:
    dataset: synthetic
    params: {modality: video, size: 64, frames: 8, num_items: 64, seed: 1}
    preprocess: []
  batch_size: 8
  num_workers: 4
  fps: 24

optimizer: {name: adamw, lr: 1.0e-3, weight_decay: 1.0e-4, grad_clip: 1.0}

# Termes de perte ; chaque phase peut moduler leurs poids (loss_weights).
loss:
  terms:
    msg: {name: message_bce, weight: 1.0}    # décodage du message
    vid: {name: mse, weight: 0.7}            # imperceptibilité par frame
    temp: {name: temporal, weight: 0.2}      # cohérence temporelle du résidu (anti-flicker)

# Curriculum : les phases s'enchaînent, chacune avec son LR, ses poids et ses attaques.
phases:
  - name: warmup_decode              # apprendre à décoder sans attaque
    steps: 2000
    lr: {schedule: linear_warmup, warmup_steps: 500}
    loss_weights: {vid: 0.1, temp: 0.05}
    attacks: []
  - name: robustness_ramp            # attaques de plus en plus fortes
    steps: 8000
    lr: {schedule: constant}
    loss_weights: {vid: {ramp: [0.1, 1.0]}}
    attacks:
      - {name: framewise, params: {attack: jpeg_diff, params: {quality: 50}}}
      - {name: frame_drop, params: {p: 0.2}}
      - {name: temporal_blur, params: {weight: 0.3}}
    attack_strength: {ramp: [0.0, 1.0]}
  - name: finetune_imperceptibility  # affiner la qualité à robustesse fixée
    steps: 2000
    lr: {schedule: cosine, min_factor: 0.01}
    loss_weights: {vid: 2.0}
    attacks: [{name: h264, params: {crf: 28}}, {name: frame_drop, params: {p: 0.2}}]
    attack_strength: 1.0
""",
}

VAL_ATTACKS: dict[str, str] = {
    "image": ('    - {label: jpeg50, name: jpeg, params: {quality: 50}}\n'
              '    - {label: blur, name: gaussian_blur}'),
    "audio": ('    - {label: noise20, name: gaussian_noise, params: {snr_db: 20}}\n'
              '    - {label: lowpass3k, name: lowpass, params: {cutoff_hz: 3000}}'),
    "video": ('    - {label: h264, name: h264, params: {crf: 28}}\n'
              '    - {label: frame_drop, name: frame_drop}'),
}


def experiment_template(name: str, project: str, modality: str,
                        benchmark: str | None = None) -> str:
    """Return the commented YAML of a new experiment for ``modality``."""
    final = (f'{{benchmark: {benchmark}, checkpoints: [last, "best:val/bit_acc_mean"]}}'
             if benchmark else "null                # ex. {benchmark: image_v1, checkpoints: [last]}")
    return (EXPERIMENT_HEADER.substitute(name=name, project=project, modality=modality)
            + EXPERIMENT_BODY[modality]
            + EXPERIMENT_FOOTER.substitute(val_attacks=VAL_ATTACKS[modality],
                                           final_benchmark=final,
                                           quality=DEFAULT_QUALITY_METRIC[modality]))


def new_experiment(project: str, name: str, *, from_experiment: str | None = None,
                   workspace: Path | None = None) -> Path:
    """Create ``projects/<project>/experiments/<name>.yaml`` (template or copy)."""
    root = _root(workspace)
    _check_name(name, "expérience")
    project_dir = _project_dir(root, project)
    target = project_dir / "experiments" / f"{name}.yaml"
    if target.exists():
        raise FileExistsError(f"l'expérience '{name}' existe déjà ({target})")
    modality = _project_modality(project_dir)
    if from_experiment:
        source = project_dir / "experiments" / f"{Path(from_experiment).stem}.yaml"
        if not source.is_file():
            available = sorted(p.stem for p in (project_dir / "experiments").glob("*.yaml"))
            raise ScaffoldError(f"expérience source inconnue '{from_experiment}' — disponibles : "
                                f"{', '.join(available) if available else '(aucune)'}")
        return _write_new(target, copy_experiment_text(source.read_text(encoding="utf-8"),
                                                       source.stem, name, project, modality))
    try:
        project_data = yaml.safe_load((project_dir / "project.yaml").read_text("utf-8")) or {}
    except (OSError, yaml.YAMLError):
        project_data = {}
    benchmark = project_data.get("default_benchmark") or _default_benchmark(root, modality)
    return _write_new(target, experiment_template(name, project, modality, benchmark))


_COPY_HEADER = Template("""\
# Expérience « $name » — projet $project ($modality) — copie de « $source »
# Format : ARCHITECTURE.md §5.3
# Vérifier : wmlab validate <ce fichier>  ·  essai rapide : wmlab train <ce fichier> --smoke
""")
_TOP_LEVEL_KEY_RE = re.compile(r"^(name|description|hypothesis|tags):")


def copy_experiment_text(text: str, source: str, name: str, project: str, modality: str) -> str:
    """Text of a new experiment copied from ``source``: fresh header, ``name``, placeholders for
    ``description``/``hypothesis`` and empty ``tags`` (the copy is a new experiment: inherited
    words would pass unnoticed and inherited tags would label its runs after the source)."""
    lines = text.splitlines()
    # drop the leading comment header of the source (replaced by the copy header)
    body_start = 0
    while body_start < len(lines) and (not lines[body_start].strip()
                                       or lines[body_start].lstrip().startswith("#")):
        body_start += 1
    body = lines[body_start:]
    seen: set[str] = set()
    out: list[str] = []
    i = 0
    while i < len(body):
        line = body[i]
        match = _TOP_LEVEL_KEY_RE.match(line)
        if match is None:
            out.append(line)
            i += 1
            continue
        key = match.group(1)
        seen.add(key)
        i += 1
        # skip the continuation lines of a block scalar / multi-line value (indented)
        while i < len(body) and body[i][:1] in (" ", "\t") and body[i].strip():
            i += 1
        if key == "name":
            out.append(f"name: {name}")
        elif key == "description":
            out.append(f"description: \"À compléter : ce que cette expérience teste (copie de "
                       f"« {source} »)\"")
        elif key == "hypothesis":
            out.append("hypothesis: \"À compléter : résultat attendu et pourquoi\"")
        else:
            out.append(f"tags: []                            # tags propres à cette expérience "
                       f"(ceux de « {source} » ne sont pas hérités)")
    missing = []
    if "name" not in seen:
        missing.append(f"name: {name}")
    if "description" not in seen:
        missing.append(f"description: \"À compléter : ce que cette expérience teste (copie de "
                       f"« {source} »)\"")
    if "hypothesis" not in seen:
        missing.append("hypothesis: \"À compléter : résultat attendu et pourquoi\"")
    header = _COPY_HEADER.substitute(name=name, project=project, modality=modality,
                                     source=source)
    return header + "\n".join(missing + out).rstrip("\n") + "\n"


# -- HPO plan -------------------------------------------------------------------------------
HPO_TEMPLATE = Template("""\
# Plan HPO « $name » — explore des hyperparamètres de l'expérience « $experiment »
# Format : ARCHITECTURE.md §5.4  ·  essai rapide : wmlab hpo <ce fichier> --smoke
# Méthodologie (hyperparamètres scientifiques / de nuisance, pruner, fidélité) : skill
# wmlab-experiment, references/hpo-guide.md.
name: $name
experiment: $experiment
description: "À compléter : ce que l'on cherche à optimiser"
hypothesis: "À compléter"
sampler: {name: tpe, params: {n_startup_trials: 8}}   # tpe | random | nsga2 | cmaes | grid
# ASHA compare les essais à steps égaux : il ne décide qu'aux paliers min_resource × 3^k
# (les autres validations ne comptent pas). min_resource = fin de la 1re phase (warmup de
# décodage : $min_resource steps en basse fidélité) : avant, le décodeur n'a pas accroché et
# l'élagage serait un tirage au sort. Ne pas mettre la durée des phases dans `space` avec asha
# (comparaison biaisée) : hyperband ou none dans ce cas.
pruner: {name: asha, params: {min_resource: $min_resource, reduction_factor: 3}}   # asha | hyperband | median | none
objectives:                          # plusieurs objectifs ⇒ front de Pareto (sans pruning)
  - {metric: $robustness, direction: maximize}
constraints:                         # essais hors contrainte exclus du meilleur résultat
  - {metric: val/$quality, op: ">=", value: $threshold}
space:                               # chemins pointés dans l'expérience de base
  optimizer.lr: {type: float, low: 1.0e-4, high: 3.0e-3, log: true}
$term_line  # phases.0.steps: {type: int, low: $steps_low, high: $steps_high, step: $steps_low}   # durée : avec pruner hyperband/none seulement
# Proxy basse fidélité appliqué à CHAQUE essai : steps_scale multiplie les steps ET les
# warmup_steps de chaque phase ; pas de benchmark final (on évaluera le meilleur essai relancé
# en pleine fidélité).
fidelity:
  steps_scale: $steps_scale
  overrides:
    eval.every_steps: $eval_every        # ≥ 3 validations par essai pour le pruner
    eval.final_benchmark: null
budget: {max_trials: 20, max_hours: 6, max_eur: 50, parallel: 1}
objective_agg: last                  # last | best (valeur retenue pour chaque essai)
""")

STEPS_SCALE = 0.25
MSE_LIKE = ("mse", "l1")


def _nice_steps(value: float) -> int:
    """Round a step count down to a readable multiple (50, 10 or 1)."""
    value = max(1, int(value))
    for unit in (50, 10):
        if value >= 2 * unit:
            return value - value % unit
    return value


def _hpo_term_line(exp: dict[str, Any]) -> str:
    """Search-space line of the first non-message loss term — on a path that has an effect.

    ``loss.terms.<t>.weight`` is overridden by ``phases.<i>.loss_weights.<t>`` (§5.3): when
    every phase sets the term, the last phase's value (end of its ramp) is searched instead.
    """
    terms: dict[str, Any] = ((exp.get("loss") or {}).get("terms") or {}) if isinstance(exp, dict) else {}
    term = next((t for t, sp in terms.items()
                 if isinstance(sp, dict) and sp.get("name") != "message_bce"), None)
    if term is None:
        return ""
    loss_name = str(terms[term].get("name") or "")
    bounds = ("low: 10.0, high: 1000.0, log: true" if loss_name in MSE_LIKE
              else "low: 0.1, high: 10.0, log: true")
    phases = exp.get("phases") if isinstance(exp.get("phases"), list) else []
    overridden = phases and all(isinstance(p, dict) and term in (p.get("loss_weights") or {})
                                for p in phases)
    if overridden:
        last = len(phases) - 1
        value = (phases[-1].get("loss_weights") or {}).get(term)
        path = f"phases.{last}.loss_weights.{term}"
        if isinstance(value, dict) and isinstance(value.get("ramp"), list):
            path += ".ramp.1"
        comment = f"poids « {term} » de la dernière phase (loss.terms.{term}.weight est écrasé par les phases)"
    else:
        path = f"loss.terms.{term}.weight"
        comment = f"poids de base du terme « {term} »"
    return f"  {path}: {{type: float, {bounds}}}   # {comment}\n"


def new_hpo(project: str, name: str, experiment: str, *, workspace: Path | None = None) -> Path:
    """Create ``projects/<project>/hpo/<name>.yaml`` for a base experiment."""
    root = _root(workspace)
    _check_name(name, "plan HPO")
    project_dir = _project_dir(root, project)
    exp_path = project_dir / "experiments" / f"{experiment}.yaml"
    if not exp_path.is_file():
        available = sorted(p.stem for p in (project_dir / "experiments").glob("*.yaml"))
        raise ScaffoldError(f"expérience inconnue '{experiment}' — disponibles : "
                            f"{', '.join(available) if available else '(aucune)'}")
    target = project_dir / "hpo" / f"{name}.yaml"
    if target.exists():
        raise FileExistsError(f"le plan HPO '{name}' existe déjà ({target})")
    modality = _project_modality(project_dir)
    try:
        exp = yaml.safe_load(exp_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        exp = {}
    if not isinstance(exp, dict):
        exp = {}
    phases = exp.get("phases") if isinstance(exp.get("phases"), list) else []
    first = phases[0] if phases and isinstance(phases[0], dict) else {}
    first_steps = first.get("steps")
    base = int(first_steps) if isinstance(first_steps, int) and first_steps > 0 else 2000
    steps_low = max(base // 4, 1)
    first_scaled = max(1, round(base * STEPS_SCALE))
    every = (exp.get("eval") or {}).get("every_steps") if isinstance(exp.get("eval"), dict) else None
    total = sum(int(p.get("steps") or 0) for p in phases if isinstance(p, dict)
                and isinstance(p.get("steps"), int)) or base
    eval_every = _nice_steps(min(int(every) if isinstance(every, int) and every > 0 else total,
                                 total * STEPS_SCALE / 4))
    has_val_attacks = bool(((exp.get("eval") or {}).get("val_attacks"))
                           if isinstance(exp.get("eval"), dict) else False)
    text = HPO_TEMPLATE.substitute(
        name=name, experiment=experiment,
        robustness="val/bit_acc_mean" if has_val_attacks else "val/bit_acc",
        quality=DEFAULT_QUALITY_METRIC[modality], threshold=QUALITY_THRESHOLD[modality],
        term_line=_hpo_term_line(exp), steps_low=steps_low, steps_high=steps_low * 8,
        min_resource=first_scaled, steps_scale=STEPS_SCALE, eval_every=eval_every,
    )
    return _write_new(target, text)


# -- plugins --------------------------------------------------------------------------------
MODEL_CORE: dict[str, str] = {
    "image": '''\
        # TODO : remplacer par votre encodeur (image + message → résidu)
        self.encoder = nn.Sequential(
            nn.Conv2d(3 + nbits, channels, 3, padding=1), nn.GELU(),
            nn.Conv2d(channels, channels, 3, padding=1), nn.GELU(),
            nn.Conv2d(channels, 3, 1),
        )
        # TODO : remplacer par votre décodeur (image attaquée → logits [B, nbits])
        self.decoder = nn.Sequential(
            nn.Conv2d(3, channels, 3, padding=1), nn.GELU(),
            nn.Conv2d(channels, channels, 3, stride=2, padding=1), nn.GELU(),
            nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Linear(channels, nbits),
        )

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] dans [0,1], msg [B,nbits] dans {0,1} → x_w de même forme (clampé)."""
        b, _, h, w = x.shape
        m = (msg * 2 - 1)[:, :, None, None].expand(b, self.nbits, h, w)
        residual = torch.tanh(self.encoder(torch.cat([x, m], dim=1)))
        return (x + self.scale * self.strength * residual).clamp(0.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] (éventuellement attaquée) → logits [B,nbits] (> 0 ⇒ bit 1)."""
        return self.decoder(x)
''',
    "audio": '''\
        # TODO : remplacer par votre encodeur (signal + message → résidu)
        self.encoder = nn.Sequential(
            nn.Conv1d(1 + nbits, channels, 7, padding=3), nn.GELU(),
            nn.Conv1d(channels, channels, 7, padding=3), nn.GELU(),
            nn.Conv1d(channels, 1, 1),
        )
        # TODO : remplacer par votre détecteur (signal attaqué → logits [B, nbits])
        self.decoder = nn.Sequential(
            nn.Conv1d(1, channels, 7, padding=3), nn.GELU(),
            nn.Conv1d(channels, channels, 7, stride=4, padding=3), nn.GELU(),
            nn.AdaptiveAvgPool1d(1), nn.Flatten(), nn.Linear(channels, nbits),
        )

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,1,T] dans [-1,1], msg [B,nbits] dans {0,1} → x_w de même forme (clampé)."""
        b, _, t = x.shape
        m = (msg * 2 - 1)[:, :, None].expand(b, self.nbits, t)
        residual = torch.tanh(self.encoder(torch.cat([x, m], dim=1)))
        return (x + self.scale * self.strength * residual).clamp(-1.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,1,T] (éventuellement attaqué) → logits [B,nbits] (> 0 ⇒ bit 1)."""
        return self.decoder(x)
''',
    "video": '''\
        # TODO : remplacer par votre encodeur (frame + message → résidu), appliqué frame par frame
        self.encoder = nn.Sequential(
            nn.Conv2d(3 + nbits, channels, 3, padding=1), nn.GELU(),
            nn.Conv2d(channels, channels, 3, padding=1), nn.GELU(),
            nn.Conv2d(channels, 3, 1),
        )
        # TODO : remplacer par votre décodeur ; les logits sont moyennés sur les frames
        self.decoder = nn.Sequential(
            nn.Conv2d(3, channels, 3, padding=1), nn.GELU(),
            nn.Conv2d(channels, channels, 3, stride=2, padding=1), nn.GELU(),
            nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Linear(channels, nbits),
        )

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,F,3,H,W] dans [0,1], msg [B,nbits] → x_w de même forme (clampé)."""
        b, f, c, h, w = x.shape
        frames = x.reshape(b * f, c, h, w)
        m = (msg * 2 - 1).repeat_interleave(f, dim=0)[:, :, None, None].expand(b * f, self.nbits, h, w)
        residual = torch.tanh(self.encoder(torch.cat([frames, m], dim=1)))
        out = (frames + self.scale * self.strength * residual).clamp(0.0, 1.0)
        return out.reshape(b, f, c, h, w)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,F,3,H,W] → logits [B,nbits] (moyenne des logits par frame)."""
        b, f, c, h, w = x.shape
        return self.decoder(x.reshape(b * f, c, h, w)).reshape(b, f, self.nbits).mean(dim=1)
''',
}

MODEL_TEMPLATE = Template('''\
"""$cls — architecture de watermarking $modality (plugin utilisateur).

Contrat : ARCHITECTURE.md §4.1 (embed : même forme/plage que x ; extract : logits [B, nbits]).
Vérifier : wmlab plugin-test model $name
Utiliser : model: {name: $name, params: {channels: 32}} dans une expérience.
"""

from __future__ import annotations

import torch
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.models.base import WatermarkModel
from wmlab.registry import MODELS


class ${cls}Params(BaseModel):
    """Hyperparamètres exposés dans le YAML (model.params) — ne jamais y mettre nbits."""

    model_config = ConfigDict(extra="forbid")  # une clé inconnue dans le YAML = erreur

    channels: int = Field(32, gt=0, description="Largeur des convolutions (canaux)")
    strength: float = Field(1.0, gt=0, description="Amplitude du résidu : ↑ robustesse, ↓ qualité")


@MODELS.register("$name", modality="$modality", params=${cls}Params)
class $cls(WatermarkModel):
    """TODO : décrire l'architecture en une phrase (affichée dans la bibliothèque)."""

    modality = "$modality"

    def __init__(self, nbits: int, channels: int = 32, strength: float = 1.0) -> None:
        super().__init__(nbits)
        self.strength = strength
        self.scale = 0.05  # amplitude maximale du résidu (imperceptibilité)
$core''')

ATTACK_RANGE = {"image": "0.0, 1.0", "audio": "-1.0, 1.0", "video": "0.0, 1.0"}

ATTACK_TEMPLATE = Template('''\
"""$cls — attaque $modality (plugin utilisateur).

Contrat : ARCHITECTURE.md §4.2 — forward(x, strength) conserve la forme de x ;
strength ∈ [0, 1] interpole la sévérité (0 ⇒ identité, 1 ⇒ sévérité des params).
Vérifier : wmlab plugin-test attack $name
"""

from __future__ import annotations

import torch
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.attacks.base import Attack
from wmlab.registry import ATTACKS


class ${cls}Params(BaseModel):
    """Sévérité à strength=1."""

    model_config = ConfigDict(extra="forbid")  # une clé inconnue dans le YAML = erreur

    amount: float = Field(0.05, ge=0, description="Sévérité de l'attaque à strength = 1")


@ATTACKS.register("$name", modality="$modality", params=${cls}Params)
class $cls(Attack):
    """TODO : décrire l'attaque en une phrase (affichée dans la bibliothèque)."""

    modality = "$modality"
    differentiable = True  # False ⇒ le trainer applique un straight-through estimator

    def __init__(self, amount: float = 0.05) -> None:
        super().__init__(amount=amount)
        self.amount = amount

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        if strength <= 0:
            return x
        amount = self.amount * float(strength)  # sévérité interpolée
        # TODO : remplacer par votre transformation (même forme que x en sortie)
        return (x + amount * torch.randn_like(x)).clamp($range)
''')

DATASET_ITEM = {
    "image": ('size: int = Field(64, gt=0, description="Côté des images (pixels)")', "size: int = 64",
              "torch.rand(3, self.size, self.size, generator=g)", "[3, H, W] dans [0, 1]"),
    "audio": ('length: int = Field(16000, gt=0, description="Longueur des clips (échantillons)")',
              "length: int = 16000",
              "torch.rand(1, self.length, generator=g) * 1.8 - 0.9", "[1, T] dans [-1, 1]"),
    "video": ('size: int = Field(64, gt=0, description="Côté des frames (pixels)")\n'
              '    frames: int = Field(8, gt=0, description="Frames par clip")',
              "size: int = 64, frames: int = 8",
              "torch.rand(self.frames, 3, self.size, self.size, generator=g)",
              "[F, 3, H, W] dans [0, 1]"),
}

DATASET_TEMPLATE = Template('''\
"""$cls — dataset $modality (plugin utilisateur).

Contrat : ARCHITECTURE.md §4.3 — chaque item est {"x": tenseur $item_doc, "id": str}.
Un chemin commençant par "@" (ex. root: "@coco2017") est résolu via datasets/<nom>.yaml.
Vérifier : wmlab plugin-test dataset $name
"""

from __future__ import annotations

import torch
from pydantic import BaseModel, ConfigDict, Field
from torch.utils.data import Dataset

from wmlab.registry import DATASETS


class ${cls}Params(BaseModel):
    """Paramètres exposés dans le YAML (data.train.params)."""

    model_config = ConfigDict(extra="forbid")  # une clé inconnue dans le YAML = erreur

    root: str | None = Field(None, description="Dossier des fichiers (ou « @<dataset> » du catalogue)")
    num_items: int = Field(64, gt=0, description="Nombre d'items")
    $param_field
    seed: int = Field(0, description="Graine des données procédurales")


@DATASETS.register("$name", modality="$modality", params=${cls}Params)
class $cls(Dataset):
    """TODO : décrire le dataset en une phrase (affichée dans la bibliothèque)."""

    def __init__(self, root: str | None = None, num_items: int = 64, $param_sig,
                 seed: int = 0) -> None:
        self.root = None
        if root:
            from wmlab.data import resolve_path

            self.root = resolve_path(root)
        self.num_items = num_items
$assign        self.seed = seed
        # TODO : lister les fichiers de self.root ; ici, données procédurales de démonstration

    def __len__(self) -> int:
        return self.num_items

    def __getitem__(self, index: int) -> dict:
        g = torch.Generator().manual_seed(self.seed * 1_000_003 + index)
        x = $item_expr
        return {"x": x.float(), "id": f"{index:06d}"}
''')

PREPROCESS_BODY = {
    "image": ("gamma", 'float = Field(1.0, gt=0, description="Exposant de la correction gamma")',
              "x.clamp(0.0, 1.0) ** self.gamma", "correction gamma"),
    "video": ("gamma", 'float = Field(1.0, gt=0, description="Exposant de la correction gamma")',
              "x.clamp(0.0, 1.0) ** self.gamma", "correction gamma (frame par frame)"),
    "audio": ("gain", 'float = Field(1.0, gt=0, description="Gain linéaire appliqué au signal")',
              "(x * self.gain).clamp(-1.0, 1.0)", "gain constant"),
}

PREPROCESS_TEMPLATE = Template('''\
"""$cls — prétraitement $modality (plugin utilisateur).

Contrat : ARCHITECTURE.md §4.3 — callable item_x → item_x (tenseur SANS dimension batch).
Vérifier : wmlab plugin-test preprocess $name
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.registry import PREPROCESS


class ${cls}Params(BaseModel):
    """Paramètres exposés dans le YAML (preprocess: [{name: $name, params: {...}}])."""

    model_config = ConfigDict(extra="forbid")  # une clé inconnue dans le YAML = erreur

    $param: $param_type


@PREPROCESS.register("$name", modality=$modality_decl, params=${cls}Params)
class $cls:
    """TODO : décrire le prétraitement en une phrase (exemple : $example)."""

    deterministic = True  # False si le prétraitement utilise de l'aléa

    def __init__(self, $param: float = 1.0) -> None:
        self.$param = $param

    def __call__(self, x: Tensor) -> Tensor:
        # TODO : remplacer par votre transformation
        return $expr
''')

LOSS_TEMPLATE = Template('''\
"""$cls — terme de perte (plugin utilisateur).

Contrat : ARCHITECTURE.md §4.4 — module construit avec ses params, appelé avec un StepBatch
(x, x_w, x_att, msg, logits, modality, model, step, phase, extras) et renvoyant un scalaire.
Utiliser : loss: {terms: {mon_terme: {name: $name, weight: 1.0}}}
Vérifier : wmlab plugin-test loss $name
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.registry import LOSSES

if TYPE_CHECKING:
    from wmlab.losses.base import StepBatch


class ${cls}Params(BaseModel):
    """Paramètres exposés dans le YAML (loss.terms.<terme>.params)."""

    model_config = ConfigDict(extra="forbid")  # une clé inconnue dans le YAML = erreur

    scale: float = Field(1.0, gt=0, description="Facteur multiplicatif du terme")


@LOSSES.register("$name", modality=$modality_decl, params=${cls}Params)
class $cls(nn.Module):
    """TODO : décrire la perte en une phrase (affichée dans la bibliothèque)."""

    def __init__(self, scale: float = 1.0) -> None:
        super().__init__()
        self.scale = scale

    def forward(self, batch: "StepBatch") -> Tensor:
        # TODO : exemple = L1 entre original et tatoué (imperceptibilité)
        return self.scale * (batch.x_w - batch.x).abs().mean()
''')

METRIC_TEMPLATE = Template('''\
"""$func — métrique de qualité (plugin utilisateur).

Contrat : ARCHITECTURE.md §4.5 — fonction (x, x_w, **ctx) -> Tensor[B] (un score par élément).
ctx peut contenir sample_rate (audio) et fps (vidéo).
Vérifier : wmlab plugin-test metric $name
"""

from __future__ import annotations

from torch import Tensor

from wmlab.registry import METRICS


@METRICS.register("$name", modality=$modality_decl)
def $func(x: Tensor, x_w: Tensor, **ctx) -> Tensor:
    """TODO : décrire la métrique en une phrase (plus haut = meilleur ?)."""
    dims = tuple(range(1, x.ndim))
    # TODO : exemple = -L1 moyen par élément du batch
    return -(x_w - x).abs().mean(dim=dims)
''')


def plugin_template(kind: str, name: str, modality: str | None) -> str:
    """Return the source of a plugin skeleton compliant with §4."""
    cls = _camel(name)
    mods = normalize_modality(modality.split(",") if isinstance(modality, str) else modality)
    first = mods[0] if mods else "image"
    decl = ("None" if mods is None else
            f'"{mods[0]}"' if len(mods) == 1 else "[" + ", ".join(f'"{m}"' for m in mods) + "]")
    if kind == "model":
        return MODEL_TEMPLATE.substitute(cls=cls, name=name, modality=first,
                                         core=MODEL_CORE[first])
    if kind == "attack":
        return ATTACK_TEMPLATE.substitute(cls=cls, name=name, modality=first,
                                          range=ATTACK_RANGE[first])
    if kind == "dataset":
        field, sig, expr, doc = DATASET_ITEM[first]
        assign = "".join(f"        self.{p.split(':')[0].strip()} = {p.split(':')[0].strip()}\n"
                         for p in sig.split(", "))
        return DATASET_TEMPLATE.substitute(cls=cls, name=name, modality=first, param_field=field,
                                           param_sig=sig, item_expr=expr, item_doc=doc,
                                           assign=assign)
    if kind == "preprocess":
        param, ptype, expr, example = PREPROCESS_BODY[first]
        return PREPROCESS_TEMPLATE.substitute(cls=cls, name=name, modality=first,
                                              modality_decl=decl, param=param, param_type=ptype,
                                              expr=expr, example=example)
    if kind == "loss":
        return LOSS_TEMPLATE.substitute(cls=cls, name=name, modality_decl=decl)
    if kind == "metric":
        return METRIC_TEMPLATE.substitute(func=name, name=name, modality_decl=decl)
    raise ScaffoldError(f"type de plugin inconnu '{kind}' — valeurs possibles : "
                        f"{', '.join(PLUGIN_DIRS)}")


def ensure_lab_package(root: Path) -> None:
    """Create ``lab/__init__.py`` and the per-kind sub-packages if missing."""
    lab = root / "lab"
    lab.mkdir(exist_ok=True)
    init = lab / "__init__.py"
    if not init.exists():
        init.write_text('"""Plugins utilisateur Watermark Lab (découverts automatiquement)."""\n',
                        encoding="utf-8")
    for sub in LAB_SUBDIRS:
        (lab / sub).mkdir(exist_ok=True)
        sub_init = lab / sub / "__init__.py"
        if not sub_init.exists():
            sub_init.write_text("", encoding="utf-8")


def new_plugin(kind: str, name: str, modality: str | None, *, workspace: Path | None = None
               ) -> Path:
    """Create ``lab/<kind>s/<name>.py`` from a commented skeleton."""
    root = _root(workspace)
    kind = kind.strip().lower()
    if kind not in PLUGIN_DIRS:
        raise ScaffoldError(f"type de plugin inconnu '{kind}' — valeurs possibles : "
                            f"{', '.join(PLUGIN_DIRS)}")
    if not PLUGIN_NAME_RE.match(name or ""):
        raise ScaffoldError(f"nom de plugin invalide '{name}' — minuscules, chiffres et '_' "
                            "(identifiant Python commençant par une lettre)")
    try:
        mods = normalize_modality(modality.split(",") if isinstance(modality, str) and
                                  "," in modality else modality)
    except ValueError as exc:
        raise ScaffoldError(str(exc)) from None
    if mods is None and kind in ("model", "attack", "dataset"):
        raise ScaffoldError(f"une modalité précise est requise pour un plugin de type {kind} "
                            f"({', '.join(MODALITIES)})")
    ensure_lab_package(root)
    target = root / "lab" / PLUGIN_DIRS[kind] / f"{name}.py"
    decl = None if mods is None else ",".join(mods)
    return _write_new(target, plugin_template(kind, name, decl))
