# Watermark Lab — Architecture et contrats

Ce document est **la source de vérité**. Tout module (Python, desktop, skills) DOIT respecter
les noms, formats et signatures définis ici. Si un changement de contrat est nécessaire, il se
fait d'abord ici.

Langue : UI et docs utilisateur en **français** ; code, identifiants, clés de métriques et
commentaires de code en **anglais**.

---

## 0. Vue d'ensemble

```
┌──────────────────────── Mac (poste de pilotage) ────────────────────────┐
│  Watermark Lab.app (Electron)                                           │
│    ├─ renderer React (desktop/src)  ──HTTP+token──►  backend FastAPI     │
│    └─ main process : lance `python -m wmlab.cli server`                 │
│                                                                         │
│  backend (wmlab.server)                                                 │
│    ├─ index SQLite (.wmlab/index.sqlite) construit depuis les dossiers  │
│    │   de runs (source de vérité = fichiers)                             │
│    ├─ jobs : exécuteurs `local` (subprocess) et `ssh` (Scaleway)        │
│    └─ sync : rsync des dossiers de runs distants → .wmlab/runs/         │
└─────────────────────────────────────────────────────────────────────────┘
          │ ssh / rsync (code ↑, runs ↓)
          ▼
┌──────────── Serveur GPU Scaleway (ou le Mac lui-même en local) ─────────┐
│  `wmlab train|hpo|eval` → écrit un dossier de run autonome              │
│  (fonctionne même si le Mac est éteint ; rien ne dépend du réseau)      │
└─────────────────────────────────────────────────────────────────────────┘
```

Principes :
1. **Le dossier de run est la source de vérité.** Append-only (JSONL), écrit localement par le
   process d'entraînement, synchronisé ensuite. Le Mac peut être éteint pendant un run.
2. **Tout est déclaratif** : projets, expériences (avec phases de curriculum), plans d'HPO,
   benchmarks et datasets sont des YAML validés par des schémas pydantic.
3. **Modularité par registres** : architectures, attaques, datasets, prétraitements, pertes et
   métriques sont enregistrés via décorateurs ; les plugins utilisateur vivent dans `lab/` et sont
   découverts automatiquement.
4. **L'évaluation est séparée de l'entraînement** : benchmarks figés et versionnés.
5. **Le coût est une métrique** : chaque run/étude/éval porte son prix €/h et sa durée.

---

## 1. Arborescence du workspace

```
<workspace>/                      # = racine du dépôt ("studio AI")
  workspace.yaml                  # réglages du workspace (budget, data_dir…)
  ARCHITECTURE.md  CLAUDE.md  README.md  pyproject.toml  uv.lock
  wmlab/                          # package Python (SDK + moteur + serveur)
  lab/                            # plugins utilisateur (auto-découverts)
    __init__.py
    models/ attacks/ datasets/ losses/ metrics/ preprocess/   (chacun avec __init__.py)
  projects/<project>/
    project.yaml
    experiments/<experiment>.yaml
    hpo/<plan>.yaml
    notes.md                      # journal du projet (optionnel)
  benchmarks/<name>.yaml          # suites d'évaluation figées
  datasets/<name>.yaml            # catalogue des datasets (chemins par hôte)
  scripts/remote_bootstrap.sh     # préparation d'un serveur GPU
  tests/                          # pytest
  desktop/                        # app Electron + React
  .claude/skills/                 # skills Claude (wmlab-experiment, wmlab-analyse)
  .wmlab/                         # DONNÉES LOCALES (gitignored)
    runs/<run_id>/                # dossiers de runs (locaux ou synchronisés)
    studies/<study_id>/           # études HPO
    index.sqlite                  # index du serveur (reconstructible)
    hosts.yaml                    # machines (local + ssh)
    jobs/<job_id>.json            # un fichier par job lancé par l'app
    cache/                        # caches de datasets prétraités
    logs/server.log
```

### 1.1 `workspace.yaml`
```yaml
name: "Studio AI — Watermarking Lab"
budget:
  total_eur: 40000
  start: "2026-10-01"
  end: "2027-09-30"
  manual_entries: []        # [{label: str, eur: float, date: "YYYY-MM-DD", project: str|null}]
data_dir: .wmlab            # relatif au workspace
sync_interval_s: 30
default_host: local
```

### 1.2 Variables d'environnement (lues par `wmlab.paths`)
| Variable | Défaut | Rôle |
|---|---|---|
| `WMLAB_WORKSPACE` | recherche vers le haut d'un dossier contenant `workspace.yaml` depuis le cwd | racine du workspace |
| `WMLAB_DATA` | `<workspace>/<data_dir>` | racine des données (runs/, studies/, cache/) |
| `WMLAB_HOST` | `local` | nom de l'hôte courant (résolution des chemins de datasets, coût) |
| `WMLAB_PRICE_EUR_PER_HOUR` | `0` | prix de l'hôte courant, écrit dans les manifests |

`wmlab.paths` expose : `workspace_root() -> Path`, `data_root() -> Path`, `runs_dir() -> Path`,
`studies_dir() -> Path`, `cache_dir() -> Path`, `current_host() -> str`,
`current_price() -> float`, `load_workspace_settings() -> WorkspaceSettings`.

### 1.3 Identifiants
- `run_id` : `YYYYMMDD-HHMMSS-<slug>-<4hex>` (UTC ; slug = nom d'expérience, `[a-z0-9-]`, ≤ 32 car.)
- `study_id` : même format avec le nom du plan HPO
- `job_id` : `job-YYYYMMDD-HHMMSS-<4hex>`
- Fonctions : `wmlab.ids.new_run_id(slug)`, `new_study_id(slug)`, `new_job_id()`.

Dates dans les manifests : ISO 8601 UTC avec `Z` (`2026-10-01T12:00:00Z`).
Champ `t` dans les JSONL : epoch en secondes (float).

---

## 2. Conventions de données (tenseurs)

| Modalité | Tenseur batch | Plage | Métadonnées |
|---|---|---|---|
| image | `[B, 3, H, W]` float32 | `[0, 1]` | — |
| audio | `[B, 1, T]` float32 (mono) | `[-1, 1]` | `sample_rate` (défaut 16000) |
| video | `[B, F, 3, H, W]` float32 | `[0, 1]` | `fps` (défaut 24) |

Message : `msg` float32 `[B, nbits]` à valeurs dans `{0., 1.}`.
Logits d'extraction : `[B, nbits]`, `> 0` ⇒ bit 1.
`nbits` vient de `project.yaml: message_bits`.

Un item de dataset est un `dict` : `{"x": Tensor (sans dim batch), "id": str}`.

---

## 3. Registres et plugins (`wmlab.registry`)

```python
class Entry:  # dataclass
    name: str; kind: str; obj: Any
    modality: tuple[str, ...] | None        # None = toutes modalités
    params_model: type[pydantic.BaseModel] | None
    description: str                        # défaut : 1er paragraphe du docstring
    source: str                             # module d'origine
    builtin: bool                           # True si défini dans wmlab.*

class Registry:
    def __init__(self, kind: str): ...
    def register(self, name: str, *, modality: str | Sequence[str] | None = None,
                 params: type[BaseModel] | None = None,
                 description: str | None = None) -> Callable[[T], T]
        # Ré-enregistrer un nom existant lève ValueError, sauf si c'est le même objet
        # (ré-import) ; un plugin `lab/` peut surcharger un builtin avec `override=True`.
    def get(self, name: str) -> Entry            # KeyError explicite (liste des noms proches)
    def has(self, name: str) -> bool
    def build(self, name: str, /, **params) -> Any
        # valide `params` avec params_model (si présent) puis appelle obj(**params)
    def list(self, modality: str | None = None) -> list[Entry]
    def describe(self) -> list[dict]
        # [{name, kind, modality: list|None, description, params_schema: dict|None, source, builtin}]
        # params_schema = JSON Schema du params_model, sinon dérivé de la signature __init__/fonction

MODELS     = Registry("model")
ATTACKS    = Registry("attack")
DATASETS   = Registry("dataset")
PREPROCESS = Registry("preprocess")
LOSSES     = Registry("loss")
METRICS    = Registry("metric")
REGISTRIES = {"model": MODELS, "attack": ATTACKS, "dataset": DATASETS,
              "preprocess": PREPROCESS, "loss": LOSSES, "metric": METRICS}

def load_plugins(workspace: Path | None = None, *, include_builtins: bool = True) -> PluginReport
    # 1) importe wmlab.builtins (qui importe wmlab.models, wmlab.attacks, wmlab.data,
    #    wmlab.losses, wmlab.metrics) ; 2) ajoute le workspace à sys.path et importe
    #    récursivement le package `lab` (pkgutil.walk_packages). Idempotent.
    # Les erreurs d'import sont collectées, jamais levées.
class PluginReport: loaded: list[str]; errors: list[dict]   # {"module": str, "error": str}
```

Exemple de plugin utilisateur :
```python
# lab/models/my_unet.py
from pydantic import BaseModel
from wmlab.registry import MODELS
from wmlab.models.base import WatermarkModel

class MyUNetParams(BaseModel):
    channels: int = 64
    depth: int = 4

@MODELS.register("my_unet", modality="image", params=MyUNetParams)
class MyUNet(WatermarkModel):
    """U-Net embedder + CNN extractor."""
    modality = "image"
    def __init__(self, nbits: int, channels: int = 64, depth: int = 4): ...
    def embed(self, x, msg): ...
    def extract(self, x): ...
```

---

## 4. Interfaces des composants

### 4.1 Modèle — `wmlab.models.base.WatermarkModel(nn.Module)`
```python
class WatermarkModel(nn.Module):
    modality: ClassVar[str] = "image"
    def __init__(self, nbits: int): super().__init__(); self.nbits = nbits
    def embed(self, x: Tensor, msg: Tensor) -> Tensor      # même forme/plage que x (clampé)
    def extract(self, x: Tensor) -> Tensor                 # logits [B, nbits]
    def forward(self, x: Tensor, msg: Tensor) -> Tensor:   # = embed
    def aux_losses(self, batch: "StepBatch") -> dict[str, Tensor]:   # défaut {}
    def param_groups(self) -> dict[str, list[nn.Parameter]]:        # défaut {"all": params}
```
Construction par le trainer : `MODELS.build(spec.model.name, nbits=project.message_bits, **spec.model.params)`.
`nbits` ne figure JAMAIS dans `model.params`.
Helpers : `wmlab.models.load_model_from_checkpoint(path, device="cpu") -> WatermarkModel`.

Modèles builtin (noms exacts) :
| nom | modalité | description |
|---|---|---|
| `hidden` | image | encodeur/décodeur conv à la HiDDeN (params: `channels=64`, `enc_blocks=4`, `dec_blocks=7`, `strength=1.0`) |
| `audio_conv` | audio | encodeur résiduel 1D + détecteur conv 1D façon AudioSeal-lite (params: `channels=32`, `blocks=4`, `strength=1.0`) |
| `video_framewise` | video | applique un modèle image frame par frame, agrège les logits sur les frames (params: `image_model="hidden"`, `image_params={}`, `aggregate="mean"`) |

### 4.2 Attaque — `wmlab.attacks.base.Attack(nn.Module)`
```python
class Attack(nn.Module):
    modality: ClassVar[str]
    differentiable: ClassVar[bool] = True
    def __init__(self, **params)                       # params = sévérité à strength=1
    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor   # strength∈[0,1] ; 0 ⇒ identité
```
- La sévérité est **interpolée** par `strength` (ex. JPEG `quality=50` : q = 100 − s·(100−50)).
- Les benchmarks utilisent `strength=1`. En entraînement, la force effective par batch est
  `U(0, s_phase)` si `random_strength: true` (défaut), sinon `s_phase`.
- Attaques non différentiables : le trainer applique un **straight-through estimator** via
  `wmlab.attacks.apply_attack(attack, x, strength)` :
  `x + (attack(x.detach(), strength) - x).detach()` si `not attack.differentiable`.
- Une attaque doit préserver la forme du tenseur (redimensionner/rogner puis revenir à la taille).

Attaques builtin (noms exacts) :
- **image** : `identity`, `jpeg` (réel, PIL, non diff., `quality=50`), `jpeg_diff` (approximation
  différentiable DCT, `quality=50`), `gaussian_noise` (`std=0.06`), `gaussian_blur` (`sigma=2.0`),
  `resize` (`scale=0.5`, down puis up), `crop` (`area=0.5`, crop central-aléatoire puis resize),
  `rotate` (`degrees=10`), `brightness` (`delta=0.3`), `contrast` (`factor=0.5`),
  `saturation` (`factor=0.5`), `combo` (`attacks=[{name, params}]`, application séquentielle).
- **audio** : `identity`, `gaussian_noise` (`snr_db=20`), `lowpass` (`cutoff_hz=3000`),
  `highpass` (`cutoff_hz=500`), `resample` (`rate=8000`), `amplitude` (`gain_db=-6`),
  `echo` (`delay_ms=100`, `decay=0.3`), `time_shift` (`max_ms=50`), `mp3` (réel via PyAV,
  non diff., `bitrate_kbps=64`), `aac` (réel, `bitrate_kbps=64`).
- **video** : `identity`, `framewise` (`attack="jpeg"`, `params={}` : applique une attaque image à
  chaque frame), `h264` (réel via PyAV/libx264, non diff., `crf=28`), `frame_drop` (`p=0.2`,
  remplace par la frame précédente), `temporal_blur` (`weight=0.3`).

### 4.3 Dataset et prétraitements
- `DATASETS.build(name, **params) -> torch.utils.data.Dataset` renvoyant des items `{"x","id"}`.
- Un paramètre de chemin commençant par `@` (ex. `root: "@coco2017"`) est résolu via le catalogue
  `datasets/coco2017.yaml` → `paths[WMLAB_HOST]` sinon `paths["default"]` (avec `~` étendu).
  Fonction : `wmlab.data.resolve_path(value: str) -> str`.
- Prétraitements : `PREPROCESS.build(name, **params)` renvoie un callable `item_x -> item_x`
  (tenseur sans dim batch). Attribut de classe `deterministic: bool` (True si pas d'aléa).
- `wmlab.data.build_dataset(split_spec: DataSplitSpec, modality: str) -> Dataset` applique la
  liste `preprocess` dans l'ordre.
- **Version d'un dataset** : `wmlab.data.dataset_version(split_spec) -> str` =
  12 premiers hex de sha256 du JSON canonique `{dataset, params (chemins @ non résolus),
  preprocess, limit}`. Écrite dans `run.json.datasets`.

Datasets builtin : `synthetic` (`modality`, `size=128` [image/vidéo], `length=16000` [audio],
`frames=8` [vidéo], `num_items=512`, `seed=0` ; contenu structuré déterministe : formes,
dégradés, textures / mélanges de sinusoïdes + bruit / formes en mouvement), `image_folder`
(`root`, `extensions=[".jpg",".jpeg",".png",".webp"]`, `recursive=True`, `limit=None`),
`audio_folder` (`root`, `sample_rate=16000`, `clip_seconds=1.0`, `limit=None` ; décodage et
rééchantillonnage via PyAV), `video_folder` (`root`, `frames=16`, `size=128`, `limit=None` ;
PyAV).

Prétraitements builtin : image/vidéo : `resize` (`size`), `center_crop` (`size`),
`random_crop` (`size`), `random_resized_crop` (`size`, `scale=[0.5,1.0]`), `hflip` (`p=0.5`) ;
audio : `random_clip` (`seconds`, `sample_rate=16000`), `normalize_peak` (`peak=0.9`),
`random_gain` (`db=6`) ; vidéo : `temporal_crop` (`frames`). Les ops image s'appliquent
frame par frame sur la vidéo.

### 4.4 Pertes — `LOSSES`
Une perte est une classe `nn.Module` construite avec ses params et appelée avec un `StepBatch` :
```python
@dataclass
class StepBatch:
    x: Tensor; x_w: Tensor; x_att: Tensor; msg: Tensor; logits: Tensor
    modality: str; model: WatermarkModel; step: int; phase: str
    extras: dict            # ex. {"sample_rate": 16000}
```
`loss(batch: StepBatch) -> Tensor` (scalaire). Builtin : `message_bce`, `mse`, `l1`,
`ssim` (image/vidéo : 1−SSIM), `stft` (audio, multi-résolution), `temporal` (vidéo : cohérence
temporelle du résidu), `lpips` (image, optionnel : dépend de l'extra `quality`).

### 4.5 Métriques — `METRICS`
Fonctions de qualité `(x, x_w, **ctx) -> Tensor[B]` : `psnr`, `ssim` (image/vidéo),
`snr`, `si_snr` (audio), `stoi` (audio, optionnel), `lpips` (optionnel), `flicker` (vidéo).
Robustesse (module `wmlab.metrics.robustness`) :
- `bit_accuracy(logits, msg) -> Tensor[B]`
- `detection_threshold(nbits, fpr) -> int` : plus petit k tel que P(Binom(n, ½) ≥ k) ≤ fpr (exact)
- `tpr_at_fpr(logits, msg, fpr) -> float`

Métrique de qualité par défaut par modalité : image `psnr`, audio `snr`, vidéo `psnr`.

---

## 5. Specs YAML (schémas pydantic dans `wmlab.spec`)

Tous les modèles pydantic utilisent `model_config = ConfigDict(extra="forbid")` sauf mention.
Chargement : `wmlab.spec.load_yaml(path, kind) -> Spec`, `wmlab.spec.detect_kind(path)`.
Types clés : `ProjectSpec`, `ExperimentSpec`, `PhaseSpec`, `HpoSpec`, `BenchmarkSpec`,
`DatasetCatalogEntry`, `WorkspaceSettings`, `HostSpec`.

### 5.1 Valeurs planifiées (`ScheduledValue`)
Un nombre, ou :
- `{ramp: [a, b], shape: linear|cosine|exp, start: 0.0, end: 1.0}` : interpolation de a à b entre
  les fractions `start`/`end` de la phase (constante avant/après).
- `{piecewise: [[frac, value], ...]}` : interpolation linéaire par morceaux (frac ∈ [0,1]).
Fonction : `wmlab.curriculum.resolve_value(v, progress: float) -> float` (progress ∈ [0,1]).

### 5.2 `project.yaml`
```yaml
name: image-hidden
title: "Image — HiDDeN baseline"
modality: image
description: "Premier schéma image, sert à valider le pipeline"
message_bits: 48
default_benchmark: image_v1
objectives:
  quality:    {metric: psnr, direction: maximize, label: "PSNR (dB)", constraint: ">= 38"}
  robustness: {metric: bit_acc_mean, direction: maximize, label: "Bit acc. moyenne"}
tags: [image, baseline]
```

### 5.3 Expérience `experiments/<name>.yaml`
```yaml
name: baseline
description: "HiDDeN + curriculum 3 phases"
hypothesis: "Un warmup sans attaque évite le blocage du décodeur à 50 %"
seed: 0
model:
  name: hidden
  params: {channels: 64}
data:
  train:
    dataset: synthetic               # ou image_folder avec root: "@coco2017"
    params: {modality: image, size: 128, num_items: 2048}
    preprocess: [{name: random_crop, params: {size: 128}}]
    limit: null
  val:
    dataset: synthetic
    params: {modality: image, size: 128, num_items: 128, seed: 1}
    preprocess: []
  batch_size: 32
  num_workers: 4
  sample_rate: 16000                 # audio
  fps: 24                            # vidéo
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
    attacks: []                       # identité seulement
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
  final_benchmark: {benchmark: image_v1, checkpoints: ["last", "best:val/bit_acc_mean"]}   # ou null
checkpoint:
  every_steps: 2000
  keep_last: 2
  keep_best: {"val/bit_acc_mean": max, "val/psnr": max}
  at_phase_end: true
samples: {every_steps: 2000, count: 4}
resources: {device: auto, precision: auto, compile: false}
init_from: null        # {run: <run_id>, checkpoint: "phase_end:robustness_ramp"|"best:<metric>"|"last"|<nom>,
                       #  start_phase: <nom ou index>, load_optimizer: false}
tags: []
```

`PhaseSpec` : `name` (unique), `steps` (>0), `lr` (`LRScheduleSpec`), `loss_weights`
(dict terme → ScheduledValue ; les termes absents gardent `loss.terms.<t>.weight`),
`attacks` (liste d'`AttackEntry {name, params={}, p=1.0, label=None}`), `attack_strength`
(ScheduledValue, défaut 1.0), `random_strength` (bool, défaut true), `freeze` (liste de
préfixes de noms de paramètres à geler), `batch_size` (override optionnel), `data` (override
optionnel `{train: DataSplitSpec}` ⇒ le trainer reconstruit le loader en début de phase),
`reset_optimizer` (bool, défaut false).

Tirage des attaques : à chaque step, une entrée est tirée avec probabilité ∝ `p`
(une liste vide ⇒ identité).

`LRScheduleSpec` : `schedule: constant|linear_warmup|cosine|linear|wsd|exp`,
`warmup_steps=0`, `factor=1.0` (multiplicateur crête appliqué à `optimizer.lr`),
`min_factor=0.0`, `decay_fraction=0.2` (wsd : fraction finale en décroissance),
`gamma=0.999` (exp). Le warmup (si `warmup_steps>0`) s'applique à TOUS les schedules.
Fonction : `wmlab.curriculum.lr_factor(sched, step_in_phase, phase_steps) -> float`.

`wmlab.curriculum.PhasePlan(spec: ExperimentSpec)` :
- `.total_steps`, `.phases`, `.bounds -> list[(name, start, end)]`
- `.locate(global_step) -> PhaseCursor(index, phase, step_in_phase, progress, start, end)`
- `.preview(points_per_phase=50) -> dict` au format `PhasePlanPreview` (§9).

Overrides : `wmlab.spec.apply_overrides(spec_dict, {"optimizer.lr": "3e-4", "phases.1.steps": "5000",
"loss.terms.img.weight": "0.5"}) -> dict` (chemins pointés, index de liste numériques,
valeurs parsées en YAML). Syntaxe CLI : `--set optimizer.lr=3e-4` (répétable).

### 5.4 Plan HPO `hpo/<name>.yaml`
```yaml
name: lr_and_weights
experiment: baseline                 # expérience de base du même projet
description: "LR et poids de la perte image"
hypothesis: "..."
sampler: {name: tpe, params: {}}     # tpe | random | nsga2 | cmaes | grid
pruner: {name: asha, params: {min_resource: 1000, reduction_factor: 3}}   # asha | hyperband | median | none
objectives:
  - {metric: val/psnr, direction: maximize}
  - {metric: val/bit_acc_mean, direction: maximize}   # >1 objectif ⇒ multi-objectif (pas de pruning)
constraints: [{metric: val/psnr, op: ">=", value: 36}]
space:
  optimizer.lr: {type: float, low: 1.0e-5, high: 1.0e-2, log: true}
  loss.terms.img.weight: {type: float, low: 0.1, high: 5.0}
  phases.0.steps: {type: int, low: 500, high: 3000, step: 500}
  model.params.channels: {type: categorical, choices: [32, 64]}
fidelity: {steps_scale: 0.25, overrides: {}}   # proxy basse fidélité appliqué à chaque essai
budget: {max_trials: 40, max_hours: 12, max_eur: 200, parallel: 2}
objective_agg: last                  # last | best
```

### 5.5 Benchmark `benchmarks/<name>.yaml`
```yaml
name: image_v1
version: 1
modality: image
description: "Suite image v1 — ne jamais modifier : créer image_v2"
data:
  dataset: synthetic
  params: {modality: image, size: 256, num_items: 64, seed: 1234}
  preprocess: []
  limit: 64
batch_size: 16
seed: 0
quality_metrics: [psnr, ssim]
fpr: [1.0e-2, 1.0e-4, 1.0e-6]
attacks:
  - {label: none, name: identity}
  - {label: jpeg_q50, name: jpeg, params: {quality: 50}}
```

### 5.6 Catalogue `datasets/<name>.yaml`
```yaml
name: coco2017
modality: image
description: "COCO 2017 train (118k images)"
source: "https://cocodataset.org"
license: "Images : conditions Flickr ; annotations CC BY 4.0"
paths: {default: "~/datasets/coco/train2017", scw-l40s: "/data/coco/train2017"}
```

### 5.7 `.wmlab/hosts.yaml`
```yaml
hosts:
  - {name: local, kind: local, price_eur_per_hour: 0, max_concurrent_jobs: 1}
  - name: scw-l40s
    kind: ssh
    ssh_target: "root@51.15.0.1"        # ou alias de ~/.ssh/config
    workdir: /root/wmlab                # code synchronisé ici
    data_dir: /root/wmlab-data          # WMLAB_DATA distant
    python: ".venv/bin/python"          # relatif à workdir, ou absolu
    price_eur_per_hour: 1.4
    gpu: "L40S 48GB"
    max_concurrent_jobs: 2
    notes: ""
```

---

## 6. Format du dossier de run (format_version 1)

```
<data>/runs/<run_id>/
  run.json              # manifest (réécrit atomiquement : tmp + rename)
  spec.yaml             # ExperimentSpec entièrement résolue (overrides appliqués)
  metrics.jsonl         # {"t", "step", "phase", "values": {clé: float}}
  system.jsonl          # {"t", "gpu_util", "gpu_mem_gb", "cpu_percent", "ram_gb", ...} (clés optionnelles)
  events.jsonl          # {"t", "type", ...} types: status | phase_start | phase_end | checkpoint
                        #   | checkpoint_deleted | eval | sample | note | error | warning
  heartbeat             # epoch float, réécrit toutes les 20 s
  stdout.log            # sortie du process (tee)
  notes.md              # hypothèse (pré-remplie depuis la spec) + conclusion
  env/
    git.json            # {"sha", "branch", "dirty": bool, "remote"} (peut venir du launch file)
    git.diff            # diff non commité (si dirty)
    pip_freeze.txt
    hardware.json       # {"hostname", "platform", "python", "torch", "cuda", "gpus": [{name, memory_gb}], "cpu_count", "ram_gb", "device"}
    command.txt         # ligne de commande
    launch.json         # (optionnel) écrit par l'app : job_id, host, overrides, launched_at, git
  checkpoints/
    index.json          # [Checkpoint] (réécrit atomiquement)
    <name>.pt           # name = "step_000123" ; format §6.2
  samples/
    index.jsonl         # {"step", "kind": image|audio|video, "name", "path", "group"}
    <step>/<idx>_<group>.<png|wav|mp4>
  eval/
    <checkpoint>__<benchmark>.json   # EvalResult §6.3
```

### 6.1 `run.json`
```json
{
  "format_version": 1,
  "kind": "train",
  "run_id": "20261001-120000-baseline-3fa2",
  "project": "image-hidden", "experiment": "baseline", "modality": "image",
  "spec_path": "projects/image-hidden/experiments/baseline.yaml",
  "study": null, "trial": null,
  "parent": null,
  "status": "running",
  "created_at": "...Z", "started_at": "...Z", "ended_at": null,
  "host": "local", "price_eur_per_hour": 0.0, "gpu_share": 1.0,
  "tags": [],
  "params": {"optimizer.lr": 0.001, "model.name": "hidden"},
  "overrides": {"optimizer.lr": "1e-3"},
  "datasets": {"train": "a1b2c3d4e5f6", "val": "0f9e8d7c6b5a"},
  "progress": {"step": 1200, "total_steps": 15000, "phase": "robustness_ramp", "phase_index": 1, "n_phases": 3},
  "summary": {"val/bit_acc_mean": 0.93, "best/val/bit_acc_mean": 0.94},
  "error": null
}
```
- `status` ∈ `queued|running|completed|failed|killed`. Le statut dérivé `lost` est calculé par
  les lecteurs : `running` et heartbeat plus vieux que 600 s.
- `parent` : `{"run_id", "checkpoint", "start_phase"}` pour un run branché (`init_from`).
- `params` : aplatissement pointé des scalaires de `model`, `optimizer`, `data.batch_size`,
  `loss.terms.*.weight`, `phases.*.steps`, `seed`, plus toutes les clés d'override.
- `summary` : dernière valeur de chaque métrique `val/*` + `best/<clé>` pour les clés de
  `checkpoint.keep_best` + `step`.
- Coût : `wmlab.tracking.cost_eur(manifest, now=None)` = heures(started→ended|heartbeat|now)
  × `price_eur_per_hour` × `gpu_share`.

### 6.2 Checkpoints
`torch.save` d'un dict :
`{"format": "wmlab-ckpt-v1", "model": state_dict, "optimizer": state_dict|None, "step": int,
"phase": str, "spec": dict, "model_name": str, "model_params": dict, "nbits": int,
"modality": str, "metrics": dict}`.

`checkpoints/index.json` : liste de
`{"name", "file", "step", "phase", "metrics": {}, "tags": [], "created_at", "size_bytes"}`.
Tags : `last`, `best:<metric>`, `phase_end:<phase>`. Rétention : on garde les `keep_last`
derniers, le meilleur pour chaque `keep_best`, tous les `phase_end:*` ; les autres fichiers sont
supprimés (événement `checkpoint_deleted`).
Sélecteurs acceptés partout (CLI, init_from, API) : `last`, `best:<metric>`, `phase_end:<phase>`,
ou un nom exact. Fonction : `wmlab.tracking.resolve_checkpoint(run_dir, selector) -> dict`.

### 6.3 `EvalResult` (`eval/<checkpoint>__<benchmark>.json`)
```json
{
  "run_id": "...", "checkpoint": "step_015000", "benchmark": "image_v1", "benchmark_version": 1,
  "created_at": "...Z", "duration_s": 42.0, "host": "local", "price_eur_per_hour": 0.0,
  "nbits": 48, "n_items": 64,
  "quality": {"psnr": 38.2, "ssim": 0.97},
  "robustness": {"jpeg_q50": {"bit_acc": 0.91, "tpr@0.01": 0.99, "tpr@0.0001": 0.95, "tpr@1e-06": 0.9}},
  "summary": {"psnr": 38.2, "ssim": 0.97, "bit_acc_mean": 0.91, "tpr@1e-06_mean": 0.9}
}
```
`bit_acc_mean` / `tpr@<fpr>_mean` = moyenne sur les attaques **hors** `identity`.
Clé TPR : `f"tpr@{fpr:g}"` (ex. `tpr@0.01`, `tpr@0.0001`, `tpr@1e-06`).

### 6.4 Dossier d'étude HPO `<data>/studies/<study_id>/`
```
study.json      # {format_version, kind: "hpo", study_id, project, plan, experiment, spec_path, status,
                #  created_at, started_at, ended_at, host, price_eur_per_hour, objectives: [{metric, direction}],
                #  budget: {...}, n_trials, best: [{trial, values, params, run_id}], error}
spec.yaml       # HpoSpec résolue
optuna.journal  # optuna JournalStorage(JournalFileBackend) — source de vérité des essais
trials.jsonl    # {"number", "state", "params", "values", "intermediate": [[step, v]], "run_id", "duration_s", "t"}
events.jsonl  heartbeat  stdout.log
```
Chaque essai est un run normal (`runs/<run_id>` avec `study`, `trial`, `gpu_share = 1/parallel`).
Le coût d'une étude est porté par `study.json` ; les runs d'essai sont exclus des totaux.

### 6.5 Métriques : clés normalisées
- Entraînement : `train/loss`, `train/loss/<term>`, `train/bit_acc`, `train/lr`,
  `train/weight/<term>`, `train/attack_strength`, `train/step_time_s`, `train/data_wait_frac`,
  `train/items_per_s`.
- Validation : `val/loss`, `val/<quality_metric>` (ex. `val/psnr`, `val/ssim`, `val/snr`),
  `val/bit_acc` (sans attaque), `val/bit_acc/<attack_label>`, `val/bit_acc_mean` (moyenne des
  attaques de `eval.val_attacks`).
- Système : clés libres de `system.jsonl` (`gpu_util`, `gpu_mem_gb`, `cpu_percent`, `ram_gb`).

---

## 7. SDK de tracking (`wmlab.tracking`)

```python
class Run:
    @classmethod
    def create(cls, *, project: str, experiment: str, modality: str, spec: dict,
               spec_path: str | None = None, run_id: str | None = None, runs_dir: Path | None = None,
               kind: str = "train", study: str | None = None, trial: int | None = None,
               parent: dict | None = None, params: dict | None = None, overrides: dict | None = None,
               tags: list[str] | None = None, gpu_share: float = 1.0,
               launch_info: dict | None = None, capture_env: bool = True,
               monitor_system: bool = True) -> "Run"
    dir: Path; run_id: str
    def log_metrics(self, step: int, values: dict[str, float], phase: str | None = None) -> None
    def log_system(self, values: dict[str, float]) -> None
    def event(self, type: str, **data) -> None
    def set_progress(self, **progress) -> None           # throttle l'écriture du manifest (≤ 1/10 s)
    def update_summary(self, values: dict[str, float]) -> None
    def set_datasets(self, versions: dict[str, str]) -> None
    def set_checkpoint_policy(self, keep_last: int, keep_best: dict[str, str], keep_phase_end: bool = True) -> None
    def save_checkpoint(self, state: dict, *, step: int, phase: str, metrics: dict | None = None,
                        tags: list[str] | None = None) -> str       # renvoie le nom
    def log_sample(self, step: int, kind: str, name: str, data, *, group: str,
                   sample_rate: int | None = None, fps: int | None = None) -> str
    def log_eval(self, result: dict) -> Path
    def finish(self, status: str = "completed", error: str | None = None) -> None
    def __enter__/__exit__   # exception ⇒ finish("failed", traceback) ; KeyboardInterrupt/SIGTERM ⇒ "killed"

class RunReader:
    def __init__(self, run_dir: Path)
    def manifest(self) -> dict            # avec "status" dérivé (lost) et "cost_eur"
    def metrics(self, keys: list[str] | None = None, max_points: int | None = None) -> dict[str, dict]
        # {key: {"steps": [...], "values": [...]}} ; sous-échantillonnage LTTB ou uniforme
    def metric_keys(self) -> list[str]
    def system(self, max_points: int | None = None) -> dict       # {"t": [...], "series": {k: [...]}}
    def events(self, limit: int | None = None) -> list[dict]
    def checkpoints(self) -> list[dict]
    def samples(self) -> list[dict]
    def evals(self) -> list[dict]
    def notes(self) -> str
    def set_notes(self, text: str) -> None
    def logs(self, tail: int = 500) -> list[str]
    def spec_yaml(self) -> str
    def env(self) -> dict                 # {"git", "git_diff", "hardware", "pip_freeze_lines", "command", "launch"}

def list_runs(runs_dir: Path) -> list[Path]
def cost_eur(manifest: dict, now: float | None = None) -> float
def resolve_checkpoint(run_dir: Path, selector: str) -> dict       # entrée d'index + "path" absolu
def read_jsonl(path: Path) -> list[dict]      # tolère une dernière ligne partielle
def atomic_write_json(path: Path, obj) -> None
```
Threads : heartbeat (20 s) et moniteur système (15 s : `nvidia-smi --query-gpu=...` si présent,
sinon psutil ; jamais bloquant). Gestion de `SIGTERM` : statut `killed`.

---

## 8. Moteur (`wmlab.train`, `wmlab.eval`, `wmlab.hpo`)

### 8.1 Trainer — `wmlab.train.trainer`
```python
def train_from_spec(spec_path: str | Path, *, overrides: dict[str, str] | None = None,
                    run_id: str | None = None, smoke: bool = False, device: str | None = None,
                    max_hours: float | None = None, launch_file: str | None = None,
                    study: str | None = None, trial: int | None = None, gpu_share: float = 1.0,
                    trial_callback: Callable[[int, dict], None] | None = None) -> dict   # summary
```
Boucle : `PhasePlan` → pour chaque step : phase courante, lr = base × `lr_factor`, poids des
pertes résolus, tirage d'attaque + force, `x_w = model.embed(x, msg)`,
`x_att = apply_attack(att, x_w, s)`, `logits = model.extract(x_att)`, pertes pondérées +
`aux_losses`, backward, clip, step. Logs `train/*` toutes les 20 steps. Validation toutes
`eval.every_steps` et en fin de phase. Checkpoints (`every_steps`, fin de phase, final).
Échantillons. `final_benchmark` à la fin. `trial_callback(step, val_metrics)` appelé après chaque
validation (pour le pruning Optuna ; peut lever `optuna.TrialPruned`).
- `smoke=True` : données remplacées par `synthetic` de même modalité (image 64 px, audio 8000
  échantillons, vidéo 4 frames 64 px), `batch_size=4`, `num_workers=0`, chaque phase ≤ 6 steps,
  validation toutes les 4 steps sur 2 batches, benchmark final limité à 8 items.
- `max_hours` : watchdog ⇒ checkpoint `last` puis `finish("killed", "max_hours reached")`.
- Device `auto` : cuda > mps > cpu. Précision `auto` : bf16 autocast sur CUDA, fp32 sinon.
- `init_from` : charge les poids (et optionnellement l'optimiseur) depuis
  `<runs>/<run>/checkpoints/…` ; démarre à `start_phase` (step global = somme des phases
  précédentes) ; `run.json.parent` renseigné.

### 8.2 Évaluation — `wmlab.eval.benchmark`
```python
def run_benchmark(model, bench: BenchmarkSpec, *, device, nbits, limit: int | None = None) -> dict  # EvalResult sans ids
def evaluate_checkpoint(run_id_or_dir: str, checkpoint: str, benchmark: str, *,
                        device: str | None = None, smoke: bool = False) -> dict   # écrit eval/…json + event
```

### 8.3 HPO — `wmlab.hpo.driver`
```python
def run_study(hpo_spec_path: str | Path, *, study_id: str | None = None, smoke: bool = False,
              device: str | None = None, launch_file: str | None = None) -> dict
def run_worker(study_dir: str | Path, *, worker_index: int, device: str | None = None) -> None
```
Le driver crée le dossier d'étude, puis lance `budget.parallel` workers (sous-process
`wmlab hpo-worker`) partageant le `JournalFileBackend`. Chaque worker boucle
`study.optimize(objective)` jusqu'au budget (`max_trials` via `MaxTrialsCallback`, `max_hours`
et `max_eur/price` via callback de temps). Objectif : lance `train_from_spec` en processus avec
les overrides tirés + `fidelity`, `trial_callback` pour `trial.report/should_prune` (mono-objectif
uniquement). Contraintes via `constraints_func` (attribut utilisateur `constraints`).
`smoke=True` : 3 essais, parallel 1, trainer en smoke.

### 8.4 CLI — `wmlab.cli` (Typer ; point d'entrée `main()`)
```
wmlab server   [--port 8765] [--token TOKEN] [--host 127.0.0.1] [--workspace PATH] [--no-sync]
wmlab train    SPEC [--set k=v]... [--run-id ID] [--smoke] [--device D] [--max-hours H] [--launch-file F]
wmlab hpo      SPEC [--study-id ID] [--smoke] [--device D] [--launch-file F]
wmlab hpo-worker STUDY_DIR --worker-index N [--device D]
wmlab eval     --run RUN --checkpoint SEL --benchmark NAME [--device D] [--smoke]
wmlab validate [PATH]...          # sans argument : tout le workspace ; code retour ≠ 0 si erreur
wmlab registry [--kind K] [--json]
wmlab plugin-test KIND NAME [--modality M]     # vérifie un plugin (formes, plages, gradients)
wmlab new project NAME --modality M [--bits 48]
wmlab new experiment PROJECT NAME [--from EXPERIMENT]
wmlab new plugin KIND NAME --modality M        # génère un squelette dans lab/<kind>s/
wmlab new hpo PROJECT NAME --experiment EXP
wmlab runs list [--project P] [--status S] [--json]
wmlab runs show RUN [--json]      # manifest + summary + checkpoints + evals (pour l'analyse)
wmlab index                       # reconstruit l'index SQLite
```

---

## 9. API HTTP (backend FastAPI, `wmlab.server`)

- Écoute `127.0.0.1` uniquement. Chaque requête `/api/*` exige l'en-tête `X-WMLab-Token`
  (ou `?token=` pour les fichiers média). CORS : toutes origines (le token protège).
- `GET /` sert le build du frontend (`desktop/dist`) s'il existe (usage navigateur).
- Erreurs : statut 4xx/5xx + `{"detail": str}`.
- Toutes les réponses sont du JSON en `snake_case`. Types ci-dessous en TypeScript.

```ts
type Modality = "image" | "audio" | "video";
type RunStatus = "queued" | "running" | "completed" | "failed" | "killed" | "lost";
type JobStatus = "queued" | "launching" | "running" | "completed" | "failed" | "killed" | "lost";
type Direction = "maximize" | "minimize";
type Series = { steps: number[]; values: number[] };

interface Objective { metric: string; direction: Direction; label?: string | null; constraint?: string | null }
interface ProjectSummary {
  name: string; title: string; modality: Modality; description: string; message_bits: number;
  default_benchmark: string | null; tags: string[];
  objectives: { quality: Objective; robustness: Objective };
  n_experiments: number; n_hpo_plans: number; n_runs: number; n_running: number;
  last_activity: string | null; cost_eur: number;
  best: { run_id: string; checkpoint: string | null; quality: number | null; robustness: number | null; source: "benchmark" | "val" } | null;
}
interface ValidationError { loc: string; msg: string }
interface ExperimentInfo {
  name: string; path: string; description: string; hypothesis: string; model: string;
  n_phases: number; total_steps: number; valid: boolean; errors: ValidationError[]; n_runs: number;
}
interface HpoPlanInfo {
  name: string; path: string; experiment: string; description: string; n_params: number;
  objectives: Objective[]; valid: boolean; errors: ValidationError[]; n_studies: number;
}
interface PhasePreview {
  name: string; start: number; end: number; steps: number;
  lr: [number, number][];                     // [step global, facteur]
  attack_strength: [number, number][];
  loss_weights: Record<string, [number, number][]>;
  attacks: string[];                          // labels/noms
}
interface PhasePlanPreview { total_steps: number; phases: PhasePreview[] }

interface Progress { step: number; total_steps: number; phase: string; phase_index: number; n_phases: number }
interface RunSummary {
  run_id: string; project: string; experiment: string; modality: Modality; status: RunStatus;
  host: string; created_at: string; started_at: string | null; ended_at: string | null;
  duration_s: number | null; cost_eur: number; study: string | null; trial: number | null;
  parent: { run_id: string; checkpoint: string; start_phase?: string | number | null } | null;
  tags: string[]; progress: Progress | null; summary: Record<string, number>;
  params: Record<string, string | number | boolean | null>; error: string | null;
}
interface Checkpoint {
  name: string; step: number; phase: string; metrics: Record<string, number>; tags: string[];
  created_at: string; size_bytes: number; available_locally: boolean;
}
interface EvalResult {
  run_id: string; checkpoint: string; benchmark: string; benchmark_version: number; created_at: string;
  duration_s: number | null; nbits: number; n_items: number;
  quality: Record<string, number>;
  robustness: Record<string, Record<string, number>>;   // attaque → {bit_acc, "tpr@…"}
  summary: Record<string, number>;
}
interface Sample { step: number; kind: "image" | "audio" | "video"; name: string; path: string; group: string }
interface RunEvent { t: number; type: string; [k: string]: unknown }
interface RunDetail extends RunSummary {
  spec: Record<string, unknown>; spec_yaml: string; spec_path: string | null;
  env: { git: Record<string, unknown> | null; git_diff: string | null; hardware: Record<string, unknown> | null;
         pip_freeze_lines: number; command: string | null; launch: Record<string, unknown> | null };
  plan: PhasePlanPreview | null; events: RunEvent[]; checkpoints: Checkpoint[]; evals: EvalResult[];
  notes: string; samples: Sample[]; metric_keys: string[]; price_eur_per_hour: number | null;
  datasets: Record<string, string>; overrides: Record<string, string>;
}
interface Trial {
  number: number; state: "COMPLETE" | "PRUNED" | "FAIL" | "RUNNING" | "WAITING";
  params: Record<string, unknown>; values: number[] | null; intermediate: [number, number][];
  run_id: string | null; duration_s: number | null;
}
interface StudySummary {
  study_id: string; project: string; plan: string; experiment: string; status: RunStatus; host: string;
  created_at: string; ended_at: string | null; n_trials: number; n_complete: number; n_pruned: number;
  n_failed: number; n_running: number; objectives: Objective[];
  best: { trial: number; values: number[]; params: Record<string, unknown>; run_id: string | null }[];
  cost_eur: number; budget: { max_trials: number | null; max_hours: number | null; max_eur: number | null; parallel: number };
}
interface StudyDetail extends StudySummary {
  spec: Record<string, unknown>; trials: Trial[]; importances: Record<string, number> | null;
  pareto: number[];                       // numéros d'essais sur le front
}
interface LeaderRow {
  run_id: string; project: string; experiment: string; checkpoint: string | null; benchmark: string | null;
  source: "benchmark" | "val"; created_at: string;
  quality: Record<string, number>; summary: Record<string, number>;
  robustness: Record<string, Record<string, number>>;
}
interface Leaderboard {
  benchmarks: string[]; quality_metrics: string[]; robustness_metrics: string[]; attacks: string[];
  rows: LeaderRow[];
}
interface RegistryEntry {
  name: string; kind: string; modality: string[] | null; description: string;
  params_schema: Record<string, unknown> | null; source: string; builtin: boolean;
}
interface BenchmarkInfo {
  name: string; path: string; modality: Modality; version: number; description: string;
  attacks: { label: string; name: string; params: Record<string, unknown> }[];
  quality_metrics: string[]; fpr: number[]; n_items: number | null; valid: boolean; errors: ValidationError[];
}
interface DatasetInfo {
  name: string; path: string; modality: Modality; description: string; license: string; source: string;
  paths: Record<string, string>; used_by: string[];   // chemins d'expériences qui l'utilisent
  valid: boolean; errors: ValidationError[];
}
interface Host {
  name: string; kind: "local" | "ssh"; ssh_target?: string | null; workdir?: string | null;
  data_dir?: string | null; python?: string | null; price_eur_per_hour: number; gpu?: string | null;
  max_concurrent_jobs: number; notes?: string | null;
}
interface Job {
  job_id: string; kind: "train" | "hpo" | "eval" | "setup"; host: string; target: string;
  overrides: Record<string, string>; status: JobStatus; run_id: string | null; study_id: string | null;
  created_at: string; started_at: string | null; ended_at: string | null; max_hours: number | null;
  smoke: boolean; error: string | null; pid: number | null; last_sync_at: string | null;
}
interface CostItem { kind: "run" | "study" | "eval" | "manual"; id: string; project: string | null;
  host: string | null; started_at: string | null; hours: number; eur: number; label?: string }
interface Costs {
  total_eur: number; spent_eur: number; remaining_eur: number; running_eur_per_hour: number;
  by_project: { project: string; eur: number; gpu_hours: number }[];
  by_host: { host: string; eur: number; gpu_hours: number }[];
  by_month: { month: string; eur: number; gpu_hours: number }[];
  items: CostItem[];
  budget: { total_eur: number; start: string | null; end: string | null };
}
interface WorkspaceSettings {
  name: string; budget: { total_eur: number; start: string | null; end: string | null;
    manual_entries: { label: string; eur: number; date: string; project: string | null }[] };
  data_dir: string; sync_interval_s: number; default_host: string;
}
interface Overview {
  workspace: { name: string; path: string };
  budget: { total_eur: number; spent_eur: number; remaining_eur: number; running_eur_per_hour: number };
  counts: { projects: number; runs: number; running: number; studies: number; gpu_hours: number };
  running: RunSummary[]; recent: RunSummary[]; projects: ProjectSummary[]; jobs: Job[];
}
```

| Méthode | Route | Réponse |
|---|---|---|
| GET | `/api/health` | `{ok: true, version, workspace}` |
| GET | `/api/overview` | `Overview` |
| GET | `/api/projects` | `ProjectSummary[]` |
| POST | `/api/projects` body `{name, title, modality, message_bits, description}` | `ProjectSummary` |
| GET | `/api/projects/{name}` | `{summary: ProjectSummary, spec, experiments: ExperimentInfo[], hpo_plans: HpoPlanInfo[], notes: string}` |
| POST | `/api/projects/{name}/experiments` body `{name, from?: string}` | `{path}` |
| POST | `/api/projects/{name}/hpo` body `{name, experiment}` | `{path}` |
| GET | `/api/files?path=` | `{path, content, kind, validation: {valid, errors}}` |
| PUT | `/api/files` body `{path, content}` | `{saved: true, validation}` (uniquement sous `projects/`, `benchmarks/`, `datasets/`, `lab/` ; anti path traversal) |
| POST | `/api/validate` body `{content, kind?, path?}` | `{valid, errors, plan: PhasePlanPreview \| null}` |
| GET | `/api/registry` | `{entries: Record<kind, RegistryEntry[]>, errors: {module, error}[]}` |
| GET | `/api/runs?project=&experiment=&status=&study=&host=&include_trials=false&limit=500` | `RunSummary[]` (plus récents d'abord) |
| GET | `/api/runs/{id}` | `RunDetail` |
| GET | `/api/runs/{id}/metrics?keys=a,b&max_points=1000` | `{series: Record<string, Series>, phases: {name, start, end}[]}` (keys vide ⇒ toutes) |
| GET | `/api/runs/{id}/system?max_points=500` | `{t: number[], series: Record<string, number[]>}` |
| GET | `/api/runs/{id}/logs?tail=500` | `{lines: string[]}` |
| GET | `/api/runs/{id}/file?path=&token=` | binaire (confiné au dossier du run) |
| PUT | `/api/runs/{id}/notes` body `{notes}` | `{ok: true}` |
| PUT | `/api/runs/{id}/tags` body `{tags}` | `{ok: true}` |
| POST | `/api/runs/{id}/stop` | `Job \| {ok: true}` |
| POST | `/api/runs/{id}/checkpoints/{name}/fetch` | `{ok: true}` (rapatrie un .pt distant) |
| GET | `/api/compare?runs=a,b&keys=k1,k2&max_points=500` | `{runs: RunSummary[], series: Record<run_id, Record<key, Series>>}` |
| GET | `/api/studies?project=` | `StudySummary[]` |
| GET | `/api/studies/{id}` | `StudyDetail` |
| GET | `/api/leaderboard?project=&benchmark=&source=benchmark\|val` | `Leaderboard` |
| GET | `/api/benchmarks` | `BenchmarkInfo[]` |
| GET | `/api/datasets` | `DatasetInfo[]` |
| GET/PUT | `/api/hosts` | `Host[]` (PUT reçoit `Host[]`) |
| POST | `/api/hosts/{name}/test` | `{ok, latency_ms, output, error}` |
| POST | `/api/hosts/{name}/setup` | `Job` |
| GET | `/api/jobs?status=` | `Job[]` |
| POST | `/api/jobs` body `{kind, host, spec_path?, run_id?, checkpoint?, benchmark?, overrides?, max_hours?, smoke?, init_from?: {run, checkpoint, start_phase}}` | `Job` |
| POST | `/api/jobs/estimate` body `{spec_path, host, overrides?}` | `{total_steps, est_hours: number \| null, est_eur: number \| null, basis: string}` |
| POST | `/api/jobs/{id}/cancel` | `Job` |
| POST | `/api/jobs/{id}/sync` | `Job` |
| GET | `/api/costs` | `Costs` |
| GET | `/api/costs.csv` | CSV (séparateur `;`, en-têtes FR) |
| GET/PUT | `/api/settings` | `WorkspaceSettings` |

### 9.1 Serveur : organisation interne
- `wmlab/server/app.py` : `create_app(workspace: Path, token: str | None, sync: bool = True) -> FastAPI`
  et `serve(port, token, host, workspace, sync)`.
- `wmlab/server/db.py` : SQLite (stdlib `sqlite3`, WAL). Tables `runs`, `studies`, `evals`,
  `checkpoints` (index reconstructible depuis les fichiers).
- `wmlab/server/indexer.py` : scan incrémental (mtime) de `runs/` et `studies/`, thread
  périodique (5 s) + `reindex()` à la demande.
- `wmlab/server/jobs.py` : `JobManager` (fichiers `.wmlab/jobs/<job_id>.json`), démarrage,
  annulation, rattachement au redémarrage (les jobs survivent à la fermeture de l'app).
- `wmlab/server/executors/{base,local,ssh}.py` :
  - `local` : `subprocess.Popen([...python, "-m", "wmlab.cli", ...], start_new_session=True)`,
    env `WMLAB_WORKSPACE/WMLAB_DATA/WMLAB_HOST/WMLAB_PRICE_EUR_PER_HOUR`, sortie vers
    `<data>/runs/<run_id>/stdout.log` (ou `studies/…`).
  - `ssh` : `ssh -o BatchMode=yes -o ConnectTimeout=10 <target>` (utilise ~/.ssh/config, clés
    de l'utilisateur ; aucun secret stocké), `rsync -az --delete` du code (exclusions : `.git`,
    `.venv`, `.wmlab`, `desktop`, `node_modules`, `__pycache__`), écriture du launch file,
    lancement détaché `nohup setsid … & echo $!`, annulation `kill -TERM -<pgid>`,
    sync descendante `rsync -az --exclude 'checkpoints/*.pt'` des dossiers de runs/études vers
    `.wmlab/runs|studies/`. Tous les arguments passent par `shlex.quote`.
- `wmlab/server/costs.py` : agrégation (runs hors essais + études + évals + entrées manuelles).
- Le launch file (`env/launch.json`) contient `git` (sha, branch, dirty, remote) + `git_diff`
  capturés **sur le Mac** au lancement (le serveur distant n'a pas `.git`).

---

## 10. Desktop (`desktop/`)

- Electron (main/preload en CommonJS non compilé : `electron/main.cjs`, `electron/preload.cjs`),
  renderer React 19 + TypeScript + Vite + Tailwind CSS v4 (`@tailwindcss/vite`),
  `@tanstack/react-query`, `react-router` (HashRouter), ECharts (wrapper maison),
  CodeMirror 6 (`@uiw/react-codemirror` + `@codemirror/lang-yaml`), `lucide-react`.
- Main process : résout le workspace (`WMLAB_WORKSPACE` → `resources/workspace.json` →
  config userData → dialogue de choix), lance `<workspace>/.venv/bin/python -m wmlab.cli server
  --port <libre> --token <aléatoire> --workspace <ws>`, attend `/api/health`, expose
  `window.wmlab = {apiBase, token, platform}` via preload (contextBridge), tue le backend à la
  fermeture (les jobs d'entraînement, eux, continuent). Fenêtre `titleBarStyle: "hiddenInset"`.
- Hors Electron (navigateur servi par FastAPI) : `apiBase = window.location.origin`, token lu
  depuis `?token=` puis `sessionStorage`.
- Scripts npm : `dev` (vite + electron), `build` (tsc + vite build), `start` (electron sur le build),
  `dist` (build + electron-builder mac `dir`, produit `release/mac-arm64/Watermark Lab.app`).
- Design « à la Claude » : épuré, fond chaud, sidebar à gauche, titres en serif système
  (`ui-serif` = New York sur macOS), UI en `system-ui`, accent argile. Tokens CSS :

| token | clair | sombre |
|---|---|---|
| `--bg` | `#FAF9F5` | `#262624` |
| `--sidebar` | `#F3F1EA` | `#1F1E1D` |
| `--surface` | `#FFFFFF` | `#30302E` |
| `--surface-2` | `#F5F4EF` | `#393937` |
| `--border` | `#E6E3D9` | `#44433F` |
| `--text` | `#1F1E1D` | `#F5F4EE` |
| `--muted` | `#6E6C64` | `#A6A49B` |
| `--accent` | `#C6613F` | `#D97757` |
| `--accent-soft` | `#F5E6DE` | `#4A3129` |
| `--success` | `#3E7B57` | `#6DBB8A` |
| `--warning` | `#A86B12` | `#E0A94A` |
| `--danger` | `#B3413A` | `#E27D74` |
| `--info` | `#44689E` | `#86A8DA` |

Pages (routes HashRouter) : `/` Accueil · `/projects/:name` (onglets Vue d'ensemble, Expériences,
Runs, HPO, Classement) · `/projects/:name/experiments/:exp` (éditeur YAML + timeline des phases +
validation + lancement) · `/projects/:name/hpo/:plan` (éditeur plan HPO) · `/runs` ·
`/runs/:id` · `/studies/:id` · `/compare?runs=` · `/leaderboard` · `/library` (registres) ·
`/datasets` · `/benchmarks` · `/hosts` · `/budget` · `/settings`.
Bouton global « Nouveau lancement » (dialogue : projet → expérience ou plan HPO → machine →
overrides → durée max → estimation € → lancer).

---

## 11. Skills Claude (`.claude/skills/`)
- `wmlab-experiment` : créer une architecture / attaque / dataset / perte / métrique / plan de
  curriculum / plan HPO / benchmark compatibles, les valider (`wmlab validate`,
  `wmlab plugin-test`, `wmlab train --smoke`).
- `wmlab-analyse` : analyser runs et études (`wmlab runs show --json`, fichiers de run),
  rédiger les conclusions dans `notes.md`, proposer les prochains plans en tenant compte du budget.
