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
    hosts_inventory.json          # dernier inventaire par machine (§12.4, cache)
    users.yaml                    # comptes du mode hébergé (e-mail, rôle, mot de passe haché, §12.2 ; mode 0600)
    sessions.json                 # sessions de connexion (empreintes SHA-256, expiration, §12.2 ; mode 0600)
    jobs/<job_id>.json            # un fichier par job lancé par l'app
    jobs/launch/<job_id>.json     # launch files (copiés dans env/launch.json du run)
    jobs/logs/<job_id>.log        # sortie des jobs setup / retrieve
    code/<sha>/                   # exports `git archive` des révisions lancées (§12.3, 10 conservés)
    cache/                        # caches de datasets prétraités
    weights/<nom>/<fichier>       # poids officiels des baselines pré-entraînées (§4.6, wmlab weights pull)
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
| `WMLAB_WEIGHTS` | `<data>/weights` | dossier des poids officiels des baselines pré-entraînées (§4.6) |

`wmlab.paths` expose : `workspace_root() -> Path`, `data_root() -> Path`, `runs_dir() -> Path`,
`studies_dir() -> Path`, `cache_dir() -> Path`, `current_host() -> str`,
`current_price() -> float`, `weights_dir() -> Path`,
`load_workspace_settings() -> WorkspaceSettings`.

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
| `hidden` | image | encodeur/décodeur conv à la HiDDeN (params: `channels=64`, `enc_blocks=4`, `dec_blocks=7`, `strength=1.0`, `grid=4` : grille du pooling adaptatif de l'extracteur, 1 = pooling global seul) |
| `audio_conv` | audio | encodeur résiduel 1D + détecteur conv 1D façon AudioSeal-lite (params: `channels=32`, `blocks=4`, `strength=1.0`, `segments=8` : segments temporels du pooling adaptatif, 1 = pooling global seul) |
| `video_framewise` | video | applique un modèle image frame par frame, agrège les logits sur les frames (params: `image_model="hidden"`, `image_params={}`, `aggregate="mean"`) |

**Baselines pré-entraînées** — `wmlab.models.pretrained.PretrainedWatermarkModel(WatermarkModel)` :
enveloppe d'un modèle publié qui charge ses **poids officiels** dans son constructeur (§4.6) et
l'adapte au contrat (image `[0,1]` de toute taille, message `{0,1}` du projet, logits).
```python
class PretrainedWatermarkModel(WatermarkModel):
    native_nbits: ClassVar[int]          # longueur du message des poids officiels
    trainable: ClassVar[bool] = False    # False : baseline figée (phases: [] obligatoire, toujours en eval)
    def __init__(self, nbits, *, native_nbits=None, fixed_bits: dict[int, int] | None = None, filler_seed=0)
    def to_native(self, msg) -> Tensor           # [B,nbits] → [B,native] (bits du projet + bourrage fixe)
    def from_native(self, logits) -> Tensor      # [B,native] → [B,nbits]
    def weights_file(self, name, *, download=False) -> Path   # fichier déclaré, mémorisé pour la traçabilité
    def load_official(self, state) -> None       # chargement strict (aucune clé manquante ni en trop)
    def external_weights(self) -> list[dict]     # [{name, file, url, license, sha256}]
def load_official_checkpoint(path, *, safe_globals=(), mmap=False)   # torch.load weights_only + liste blanche
```
- **Charge utile** : `nbits ≤ native_nbits − len(fixed_bits)`. Les bits du projet occupent les
  premières positions libres ; les autres reçoivent un bourrage pseudo-aléatoire fixe (graine
  `filler_seed`, identique pour toutes les images) ignoré au décodage. `fixed_bits` force des
  positions et les retire de la charge utile.
- Une baseline figée (`trainable = False`) ne peut pas être entraînée : `wmlab validate` exige
  `phases: []` ; `plugin-test` ne vérifie pas ses gradients.
- Plugins livrés (`lab/models/baseline_*.py`, code tiers : `lab/models/THIRD_PARTY_NOTICES.md`) :
  `videoseal` (256 bits), `pixelseal` (256 bits, **ré-entraînable**), `wam` (32 bits), `trustmark`
  (100 bits, variantes Q/B/C/P), `invismark` (100 bits dont 6 figés, `uuid_payload`),
  `ssl_watermark` (nbits libre ≤ 2048, poids CC-BY-NC). TrustMark, InvisMark et SSL exigent
  torchvision (`uv sync --extra baselines`).

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
  non diff., `bitrate_kbps=64`), `aac` (réel, `bitrate_kbps=64`). Les attaques audio qui
  dépendent du temps (`lowpass`, `highpass`, `resample`, `echo`, `time_shift`, `mp3`, `aac`)
  ont aussi `sample_rate=16000`.
- **video** : `identity`, `framewise` (`attack="jpeg"`, `params={}` : applique une attaque image à
  chaque frame), `h264` (réel via PyAV/libx264, non diff., `crf=28`, `preset="medium"`,
  `fps=24`), `frame_drop` (`p=0.2`, remplace par la frame précédente), `temporal_blur`
  (`weight=0.3`).
- `gaussian_noise` est un seul composant multi-modal (`std` pour l'image, `snr_db` pour
  l'audio). Les params réels et leurs bornes : `wmlab registry --kind attack --json`.

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
`frames=8` [vidéo], `num_items=512`, `seed=0`, `sample_rate=16000` [audio] ; contenu
structuré déterministe : formes, dégradés, textures / mélanges de sinusoïdes + bruit / formes
en mouvement), `image_folder` (`root`, `extensions=[".jpg",".jpeg",".png",".webp"]`,
`recursive=True`, `limit=None`), `audio_folder` (`root`, `sample_rate=16000`,
`clip_seconds=1.0`, `limit=None`, `extensions=[".wav",".flac",".mp3",".ogg",".m4a",".aac",".opus"]`,
`recursive=True` ; décodage et rééchantillonnage via PyAV), `video_folder` (`root`,
`frames=16`, `size=128`, `limit=None`, `extensions=[".mp4",".mov",".mkv",".avi",".webm",".m4v"]`,
`recursive=True` ; PyAV).

Prétraitements builtin : image/vidéo : `resize` (`size`), `center_crop` (`size`),
`random_crop` (`size`), `random_resized_crop` (`size`, `scale=[0.5,1.0]`,
`ratio=[0.75,1.333]`), `hflip` (`p=0.5`) ; audio : `random_clip` (`seconds`,
`sample_rate=16000`), `normalize_peak` (`peak=0.9`), `random_gain` (`db=6`) ; vidéo :
`temporal_crop` (`frames`, `random=True` : début aléatoire, sinon centré). Les ops image
s'appliquent frame par frame sur la vidéo.

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

### 4.6 Poids officiels — `wmlab.weights`
Les plugins déclarent les fichiers de poids publiés qu'ils chargent ; ces fichiers ne sont
jamais dans le dépôt : un dossier par hôte, `paths.weights_dir()` (`WMLAB_WEIGHTS`, défaut
`<data>/weights`), chemin `<weights_dir>/<name>/<filename>`.
```python
@dataclass(frozen=True)
class WeightsFile:
    name: str; url: str; filename: str; size_mb: float; license: str; source: str
    sha256: str | None = None; commercial_use: bool = True; description: str = ""
register_weights(entry) -> WeightsFile           # nom unique (ré-enregistrement identique toléré), URL https
get_weights(name) -> WeightsFile                 # UnknownComponentError (liste des noms)
weights_path(name) -> Path                       # sans téléchargement
require_weights(name, *, download=False) -> Path # absent : FileNotFoundError en français (commande pull)
pull_weights(name, *, force=False) -> Path       # téléchargement atomique (.part puis rename), sha256 vérifié si connu
describe_weights() -> list[dict]                 # + available, path, size_mb_on_disk
weights_info(name, path) -> dict                 # {name, file, url, license, sha256} (checkpoints de référence)
```
Un téléchargement n'a lieu que sur demande explicite : `wmlab weights pull NAME…` ou
`download: true` dans `model.params` d'un modèle pré-entraîné.

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
                       #  start_phase: <nom ou index>, load_optimizer: false,
                       #  resume: false}   # true : reprise au step du checkpoint (sans start_phase)
tags: []
```

**Évaluation seule** : `phases: []` (baseline pré-entraînée). Le trainer ne construit pas
d'optimiseur ; il valide au step 0, écrit le checkpoint `step_000000` (phase `eval`, tag
`last`), exporte les échantillons et lance `final_benchmark`. Incompatible avec `init_from` ;
une telle expérience ne peut pas servir de base à un plan HPO. `data.train` reste obligatoire
(inutilisé).

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
                                     # (steps_scale multiplie `steps` ET `lr.warmup_steps` de chaque phase ;
                                     #  les overrides s'appliquent ensuite)
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
    scaleway: {zone: fr-par-2, server_id: "11111111-2222-3333-4444-555555555555"}   # optionnel (§12.4–12.5) ; jamais de clé
    decommissioned_at: null             # renseigné par POST /api/hosts/{name}/decommission (§12.4)
```

---

## 6. Format du dossier de run (format_version 1)

```
<data>/runs/<run_id>/
  run.json              # manifest (réécrit atomiquement : tmp + rename)
  spec.yaml             # ExperimentSpec entièrement résolue (overrides appliqués)
  metrics.jsonl         # {"t", "step", "phase", "values": {clé: float}}
  system.jsonl          # {"t", "gpu_util", "gpu_mem_gb", "cpu_percent", "ram_gb", "proc_ram_gb"} — clés optionnelles :
                        #   gpu_util/gpu_mem_gb via nvidia-smi (CUDA) ; sur MPS : gpu_mem_gb seulement (pas de gpu_util)
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
    <step>/<idx>_<group>.<png|wav|mp4>   # groupes écrits par le trainer : original, watermarked,
                        #   diff (|x_w − x| × 10, image/vidéo) ou residual ((x_w − x) × 10, audio) ;
                        #   vidéo : + frame (png de la frame centrale tatouée). Pas de groupe « attaqué ».
  eval/
    <checkpoint>__<benchmark>.json   # EvalResult §6.3
    <checkpoint>__<benchmark>__smoke.json   # idem avec "smoke": true (eval --smoke ou train --smoke :
                                            #   8 items, jamais écrasé par / n'écrase pas une évaluation complète, hors classement)
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
  les lecteurs : `running` et heartbeat plus vieux que 600 s. Pour un run lancé par l'app dont le
  process a disparu sans écrire de statut final, le `JobManager` écrit lui-même `status`
  (`lost`, ou `failed`/`killed` selon l'issue du job), `ended_at` (dernier heartbeat) et `error`.
- `pid`, `proc_start` : pid du process qui a écrit le run et son heure de création (psutil,
  `null` sans psutil) ; `POST /api/runs/{id}/stop` n'envoie SIGTERM qu'à ce process (même
  hôte, même heure de création, ligne de commande `wmlab`), jamais à un autre.
- `parent` : `{"run_id", "checkpoint", "start_phase", "selector", "resume"}` pour un run branché
  ou repris (`init_from`).
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
**Checkpoint de référence** (évaluation seule d'un modèle pré-entraîné) : `"model": {}` et
`"external_weights": [{name, file, url, license, sha256}]` — les poids officiels ne sont pas
recopiés dans le run ; au rechargement (`load_model_from_checkpoint`, `init_from`, benchmark),
le modèle est reconstruit depuis `model_name`/`model_params` (ce qui charge les poids
officiels) après vérification des sha256 (`wmlab.models.load_checkpoint_weights`). Tout
modèle pré-entraîné porte aussi `external_weights` dans ses checkpoints d'entraînement.

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
Clé TPR : `f"tpr@{fpr:g}"` (ex. `tpr@0.01`, `tpr@0.0001`, `tpr@1e-06`). Un FPR inatteignable
avec `nbits` bits (`fpr < 2**-nbits` : même `nbits/nbits` bits corrects dépassent ce FPR) n'est
pas écrit (clé absente, pas un 0) ; `wmlab validate` refuse un tel benchmark comme
`default_benchmark` / `final_benchmark` d'un projet. `"in_run": true` marque le benchmark
final écrit par le trainer (temps GPU déjà compris dans la durée du run : non facturé à part ;
les évaluations des essais d'une étude ne le sont pas non plus). Une évaluation
`--smoke` porte en plus `"smoke": true` (fichier `…__smoke.json`, ignorée par le classement).

### 6.4 Dossier d'étude HPO `<data>/studies/<study_id>/`
```
study.json      # {format_version, kind: "hpo", study_id, project, plan, experiment, spec_path, experiment_path,
                #  modality, status, created_at, started_at, ended_at, host, price_eur_per_hour, device, smoke,
                #  overrides (--set), objectives: [{metric, direction}], objective_agg, budget: {...},
                #  n_trials, n_complete, n_pruned, n_failed, n_running,
                #  best: [{trial, values, params, run_id}] (essais COMPLETE seulement), error}
spec.yaml       # HpoSpec résolue
optuna.journal  # optuna JournalStorage(JournalFileBackend) — source de vérité des essais
trials.jsonl    # {"number", "state": RUNNING|COMPLETE|PRUNED|FAIL, "params", "values", "intermediate": [[step, v]],
                #  "constraints": [float] | null, "feasible": bool | null, "run_id", "duration_s", "t"}
                #  (append-only : un essai a une ligne RUNNING puis une ligne finale ; StudyReader.trials()
                #  garde la dernière ligne de chaque numéro)
notes.md        # hypothèse (pré-remplie depuis le plan) + observations + conclusion de l'étude
events.jsonl  heartbeat  stdout.log
```
Chaque essai est un run normal (`runs/<run_id>` avec `study`, `trial`, `gpu_share = 1/parallel`).
Le coût d'une étude est porté par `study.json` ; les runs d'essai sont exclus des totaux.

### 6.5 Métriques : clés normalisées
- Entraînement : `train/loss`, `train/loss/<term>`, `train/bit_acc`, `train/lr`,
  `train/weight/<term>`, `train/attack_strength`, `train/step_time_s`, `train/data_wait_frac`,
  `train/items_per_s`, `train/grad_norm` (norme du gradient avant clipping ; présente si
  `optimizer.grad_clip` est défini).
- Validation : `val/loss`, `val/<quality_metric>` (ex. `val/psnr`, `val/ssim`, `val/snr`),
  `val/bit_acc` (sans attaque), `val/bit_acc/<attack_label>`, `val/bit_acc_mean` (moyenne des
  attaques de `eval.val_attacks`).
- Système : clés libres de `system.jsonl` (`gpu_util`, `gpu_mem_gb`, `cpu_percent`, `ram_gb`,
  `proc_ram_gb`) ; `gpu_util` n'existe qu'avec `nvidia-smi` (absent sur Mac/MPS).

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
    def set_tags(self, tags: list[str]) -> None            # remplace run.json.tags (manifest réécrit)
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

class StudyReader:                     # lecture seule d'un dossier d'étude §6.4
    def __init__(self, study_dir: Path)
    dir: Path; study_id: str
    def exists(self) -> bool
    def manifest(self, now: float | None = None) -> dict   # study.json + "status" dérivé (lost), "heartbeat", "duration_s", "cost_eur"
    def raw_manifest(self) -> dict
    def trials(self) -> list[dict]        # dernière ligne de trials.jsonl par numéro d'essai, triées
    def events(self, limit: int | None = None) -> list[dict]
    def logs(self, tail: int = 500) -> list[str]
    def spec_yaml(self) -> str
    def spec(self) -> dict

def list_runs(runs_dir: Path) -> list[Path]            # dossiers contenant run.json, plus récents d'abord
def list_studies(studies_dir: Path) -> list[Path]      # dossiers contenant study.json, plus récents d'abord
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
`aux_losses`, backward, clip, step (précision `fp16` : `torch.amp.GradScaler`). Logs `train/*`
toutes les 20 steps. Validation toutes `eval.every_steps` et en fin de phase, messages et
tirages d'attaque seedés (`seed + 1`) dans un état RNG forké : chaque validation voit les mêmes
tirages ; `val/bit_acc_mean` exclut `identity`. Checkpoints (`every_steps`, fin de phase,
final, et à chaque validation qui améliore une métrique de `keep_best` : un checkpoint ne porte
que les métriques mesurées à son propre step, `best:<m>` désigne donc les poids mesurés).
Échantillons. `final_benchmark` à la fin. `trial_callback(step, val_metrics)` appelé après chaque
validation (pour le pruning Optuna ; peut lever `optuna.TrialPruned`).
- `smoke=True` : données remplacées par `synthetic` de même modalité (image 64 px, audio 8000
  échantillons, vidéo 4 frames 64 px), `batch_size=4`, `num_workers=0`, chaque phase ≤ 6 steps,
  validation toutes les 4 steps sur 2 batches, benchmark final limité à 8 items.
- `max_hours` : watchdog ⇒ checkpoint `last` puis `finish("killed", "max_hours reached")`.
- Device `auto` : cuda > mps > cpu. Précision `auto` : bf16 autocast sur CUDA, fp32 sinon.
- `phases: []` (évaluation seule, §5.3) : `Trainer.evaluate_only()` — validation au step 0,
  checkpoint (de référence pour un modèle pré-entraîné), échantillons, `final_benchmark`.
- `init_from` : charge les poids (et optionnellement l'optimiseur) depuis
  `<runs>/<run>/checkpoints/…` ; démarre à `start_phase` (step global = somme des phases
  précédentes ; avertissement si le checkpoint n'est pas à ce step) ; `run.json.parent`
  renseigné. `resume: true` (incompatible avec `start_phase`) reprend au step du checkpoint
  (phase localisée dans le plan, optimiseur chargé, RNG re-seedés à `seed + 1000003 × step`).

### 8.2 Évaluation — `wmlab.eval.benchmark`
```python
def run_benchmark(model, bench: BenchmarkSpec, *, device, nbits, limit: int | None = None,
                  sample_rate: int | None = None, fps: int | None = None) -> dict  # EvalResult sans ids
                  # sample_rate / fps : contexte passé aux attaques et métriques (audio / vidéo)
def evaluate_checkpoint(run_id_or_dir: str, checkpoint: str, benchmark: str, *,
                        device: str | None = None, smoke: bool = False) -> dict   # écrit eval/…json + event
```

### 8.3 HPO — `wmlab.hpo.driver`
```python
def run_study(hpo_spec_path: str | Path, *, study_id: str | None = None, smoke: bool = False,
              device: str | None = None, launch_file: str | None = None,
              overrides: dict[str, str] | None = None,     # wmlab hpo --set : appliqués à l'expérience de base de chaque essai
              max_hours: float | None = None,              # plafonne budget.max_hours
              max_trials: int | None = None) -> dict       # plafonne budget.max_trials
def run_worker(study_dir: str | Path, *, worker_index: int, device: str | None = None) -> None
```
Le driver crée le dossier d'étude, puis lance `budget.parallel` workers (sous-process
`wmlab hpo-worker`) partageant le `JournalFileBackend`. Chaque worker boucle
`study.optimize(objective)` jusqu'au budget (`max_trials` via `MaxTrialsCallback`, `max_hours`
et `max_eur/price` via callback de temps). Objectif : lance `train_from_spec` en processus avec
les overrides tirés + `fidelity`, `trial_callback` pour `trial.report/should_prune` (mono-objectif
uniquement). Contraintes via `constraints_func` (attribut utilisateur `constraints`).
`smoke=True` : 3 essais, parallel 1, trainer en smoke. La validation finale d'un essai n'est
jamais un point d'élagage (`trial.should_prune()` n'est consulté que si `step < total_steps`) :
un essai entraîné jusqu'au bout est COMPLETE.

### 8.4 CLI — `wmlab.cli` (Typer ; point d'entrée `main()`)
```
wmlab server   [--port 8765] [--token TOKEN] [--host 127.0.0.1] [--workspace PATH] [--no-sync]
wmlab train    SPEC [--set k=v]... [--run-id ID] [--smoke] [--device D] [--max-hours H] [--launch-file F]
wmlab hpo      SPEC [--set k=v]... [--study-id ID] [--smoke] [--device D] [--max-hours H] [--max-trials N] [--launch-file F]
                                  # --set : expérience de base de chaque essai ; --max-hours / --max-trials plafonnent budget.*
wmlab hpo-worker STUDY_DIR --worker-index N [--device D]
wmlab eval     --run RUN --checkpoint SEL --benchmark NAME [--device D] [--smoke]
wmlab validate [PATH]... [--json] # sans argument : tout le workspace, import des plugins compris ; code retour ≠ 0 si erreur
wmlab registry [--kind K] [--json]
wmlab plugin-test KIND NAME [--modality M] [--param k=v]... [--json]   # vérifie un plugin (formes, plages, gradients ; pas de gradients pour une baseline figée)
wmlab new project NAME --modality M [--bits 48] [--title T] [--description D]
wmlab new experiment PROJECT NAME [--from EXPERIMENT]
wmlab new plugin KIND NAME --modality M        # squelette dans lab/<kind>s/ ; M : image|audio|video|image,video ; all (preprocess/loss/metric)
wmlab new hpo PROJECT NAME --experiment EXP
wmlab runs list [--project P] [--status S] [--include-trials] [--limit 100] [--json]   # runs d'essais HPO exclus sans --include-trials
wmlab runs show RUN [--json]      # manifest + summary + checkpoints + evals (pour l'analyse) ; RUN = id, fragment unique ou dossier
wmlab index                       # reconstruit l'index SQLite
wmlab users add EMAIL --name NAME [--role admin|member] [--password-stdin]   # mode hébergé (§12.2) : compte actif ; mot de passe temporaire affiché UNE fois (sauf --password-stdin)
wmlab users list [--json]              # comptes (e-mail, nom, rôle, état, dernière connexion ; jamais les hachés)
wmlab users approve EMAIL [--role R]   # approuve une inscription en attente (rôle member par défaut)
wmlab users role EMAIL ROLE            # change le rôle (refus : soi-même, dernier admin)
wmlab users disable EMAIL              # désactive le compte et révoque ses sessions (l'entrée reste pour l'historique)
wmlab users enable EMAIL               # réactive un compte désactivé
wmlab users reset-password EMAIL       # mot de passe temporaire affiché UNE fois, sessions révoquées, changement forcé
wmlab weights list [--json]            # poids officiels déclarés : source, licence, présence locale (§4.6)
wmlab weights pull NAME... [--force]   # télécharge des poids officiels dans weights_dir()
```

---

## 9. API HTTP (backend FastAPI, `wmlab.server`)

- Écoute `127.0.0.1` uniquement. Chaque requête `/api/*` exige un jeton dans l'en-tête
  `X-WMLab-Token` : le **jeton serveur** (`--token` / `WMLAB_TOKEN` ; admin implicite, utilisé par
  l'app en mode local) ou un **jeton de session** `wmls_…` obtenu par `POST /api/auth/login`
  (compte e-mail + mot de passe, §12.2). `?token=` n'est accepté que sur `GET /api/runs/{id}/file`
  et `GET /api/costs.csv` (fichiers média et export ; jamais ailleurs : un jeton dans une URL finit
  dans les journaux et l'historique). Routes publiques : `GET /api/health` est public (sonde de
  démarrage de l'app ; ne renvoie `workspace` qu'avec un jeton valide), ainsi que
  `POST /api/auth/register` et `POST /api/auth/login`.
  `OPTIONS` (préflight CORS) est libre. CORS : toutes origines (le jeton protège, jamais de cookie) —
  uniquement quand un jeton serveur est défini. Le serveur a toujours un jeton serveur : `--token`,
  sinon `WMLAB_TOKEN`, sinon un jeton aléatoire généré et affiché une fois sur stderr ; hors loopback
  (`--host`), un jeton explicite est obligatoire. Comparaison en bytes (jeton non ASCII ⇒ 401).
- Rôles (§12.2, appliqués par le middleware, refus par défaut) : `admin` = tout ; `member` =
  **lecture seule** : `GET` uniquement, moins les routes marquées **admin** ci-dessous (code
  d'entraînement, registres, journaux, réglages, git, utilisateurs, inventaire des machines) ;
  seules écritures autorisées à un member : `POST /api/auth/logout` et `POST /api/auth/password`.
  Toute autre route non-`GET` est **admin**. Les réponses marquées « expurgé pour member »
  omettent ce qui décrit le code ou l'infrastructure. Refus : 403 `{detail}` en français.
- `GET /` sert le build du frontend (`desktop/dist`) s'il existe (usage navigateur) ;
  `index.html` est servi en `Cache-Control: no-cache` (un rebuild n'est jamais masqué par le
  cache), les `assets/` hachés en `immutable`.
- Réponses compressées (gzip) au-delà de 1 ko quand le client l'accepte (`Accept-Encoding`) :
  le détail d'un run (`git_diff`, `spec_yaml`, `events`) atteint plusieurs centaines de ko.
- Erreurs : statut 4xx/5xx + `{"detail": str}` ; corps JSON invalide ou champ inconnu ⇒ 422 ; corps
  `/api/*` au-delà de 4 Mo ⇒ 413, corps sans `Content-Length` ⇒ 411. Bornes de lecture :
  `max_points` ≤ 5 000, `tail` ≤ 20 000, `limit` ≤ 10 000, 20 runs au plus par `GET /api/compare`.
  Aucune route WebSocket : un scope WebSocket sous `/api/` est fermé (code 1008). Hors loopback,
  le jeton serveur explicite doit faire au moins 16 caractères.
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
  parent: { run_id: string; checkpoint: string; start_phase?: string | number | null; resume?: true } | null;
  tags: string[]; progress: Progress | null; summary: Record<string, number>;
  params: Record<string, string | number | boolean | null>; error: string | null;
  user: string | null;                        // membre ayant lancé le run via l'app (§12.2)
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
  user: string | null;
}
interface StudyDetail extends StudySummary {
  spec: Record<string, unknown>; trials: Trial[]; importances: Record<string, number> | null;
  pareto: number[];                       // numéros d'essais sur le front
  notes: string;                          // <study>/notes.md
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
  scaleway: { zone: string; server_id: string } | null;   // §12.4 (hosts.yaml)
  decommissioned_at: string | null;                       // §12.4 (hosts.yaml)
  unretrieved_checkpoints: number | null;                 // dernier inventaire (null : jamais calculé / injoignable)
  unretrieved_bytes: number | null; inventory_at: string | null;
  active_jobs: number;                                    // jobs queued|launching|running sur la machine (calculé)
}
interface Retrieval { status: "complete" | "partial" | "failed" | "n/a"; checkpoints: number; bytes: number; missing: string[] }
interface RetrieveResult { runs: number; studies: number; checkpoints: number; bytes: number; missing: string[] }
interface Job {
  job_id: string; kind: "train" | "hpo" | "eval" | "setup" | "retrieve"; host: string; target: string;
  overrides: Record<string, string>; status: JobStatus; run_id: string | null; study_id: string | null;
  created_at: string; started_at: string | null; ended_at: string | null; max_hours: number | null;
  smoke: boolean; error: string | null; pid: number | null; last_sync_at: string | null;
  user: string | null;                        // §12.2
  git_ref: string | null; git_sha: string | null;   // §12.3 (git_ref null : copie de travail synchronisée, git_sha = HEAD informatif)
  retrieval: Retrieval | null;                // §12.4 : sync complète de fin de job (train/hpo/eval sur ssh)
  result: RetrieveResult | null;              // jobs `retrieve` (mis à jour pendant le rapatriement)
}
type UserRole = "admin" | "member";
type UserStatus = "pending" | "active" | "disabled";
interface Me {
  id: string; name: string; email: string; role: UserRole; hosted: boolean; public_url: string | null;
  must_change_password: boolean;              // mot de passe temporaire : à changer avant d'utiliser l'app
  auth: "session" | "server_token";           // server_token : jeton serveur (app en mode local, secours admin)
}
interface UserInfo {
  id: string; name: string; email: string; role: UserRole; status: UserStatus; must_change_password: boolean;
  created_at: string; approved_at: string | null; approved_by: string | null; disabled_at: string | null;
  last_login_at: string | null;
}
interface LoginResult { token: string; user: Me }           // token = jeton de session wmls_… (§12.2)
interface CreatedUser { user: UserInfo; temporary_password: string }   // affiché une seule fois
interface GitState {
  available: boolean; hosted: boolean; branch: string | null; sha: string | null; short_sha: string | null;
  dirty: boolean; remote: string | null; ahead: number | null; behind: number | null;
  last_pull_at: string | null; last_pull_error: string | null;
}
interface FileGit { committed: string | null; pushed: boolean; error: string | null }
interface HostInventory {
  host: string; reachable: boolean; error: string | null; checked_at: string; active_jobs: number;
  remote_data_dir: string | null;
  runs: { run_id: string; project: string | null; status: string | null; checkpoints_total: number;
          checkpoints_local: number; bytes_remote: number; bytes_missing: number; indexed: boolean }[];
  studies: { study_id: string; trials: number; synced: boolean }[];
  unretrieved_checkpoints: number; unretrieved_bytes: number; unindexed_runs: number;
}
interface InstanceInfo { configured: boolean; state: string | null; commercial_type: string | null; zone: string | null; error: string | null }
interface DecommissionResult { ok: true; deleted_instance: boolean; instance_state: string | null; message: string }
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
interface HostedSettings { enabled: boolean; git_branch: string; auto_pull_s: number; auto_commit: boolean; public_url: string | null; allow_registration: boolean }
interface WorkspaceSettings {
  name: string; budget: { total_eur: number; start: string | null; end: string | null;
    manual_entries: { label: string; eur: number; date: string; project: string | null }[] };
  data_dir: string; sync_interval_s: number; default_host: string;
  hosted: HostedSettings;                     // §12.1
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
| GET | `/api/health` | `{ok: true, version, hosted: boolean, registration: boolean, workspace?}` — **public** (sans jeton ; `registration` = `hosted.allow_registration` ; `workspace` seulement avec jeton) |
| POST | `/api/auth/register` body `{email, name, password}` | `{ok: true, status: "pending", message}` (202) — **public** ; 403 si `hosted.allow_registration = false` ; 400 e-mail, nom (`[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`, `admin` réservé) ou mot de passe (12 à 256 caractères) invalides ; réponse identique quand l'e-mail ou le nom existe déjà (pas d'énumération) ; 429 limitation (§12.2) |
| POST | `/api/auth/login` body `{email, password}` | `LoginResult` — **public** ; 401 « E-mail ou mot de passe incorrect. » ; 403 compte `pending` ou `disabled` (après vérification du mot de passe) ; 429 limitation + `Retry-After` |
| POST | `/api/auth/logout` | `{ok: true}` (révoque la session courante ; `{ok: true}` aussi pour le jeton serveur) — **member** |
| POST | `/api/auth/password` body `{current_password, new_password}` | `{ok: true}` (change son propre mot de passe, révoque les autres sessions, efface `must_change_password` ; 400 jeton serveur ou politique ; 401 mot de passe courant faux) — **member** |
| GET | `/api/me` | `Me` (utilisateur de la session ; `admin` implicite pour le jeton serveur, §12.2) |
| GET | `/api/users` | `UserInfo[]` (**admin** ; `pending`, puis `active`, puis `disabled`, chaque groupe par `name`) |
| POST | `/api/users` body `{email, name, role}` | `CreatedUser` (**admin** ; compte `active` avec mot de passe temporaire, `must_change_password: true` ; 409 e-mail ou nom déjà utilisés) |
| POST | `/api/users/{id}/approve` body `{role}` | `UserInfo` (**admin** ; 404 compte inconnu — idem pour toutes les routes `/api/users/{id}/*` ; 409 si le compte n'est pas `pending`) |
| POST | `/api/users/{id}/role` body `{role}` | `UserInfo` (**admin** ; 400 soi-même ; 409 dernier admin actif) |
| POST | `/api/users/{id}/disable` | `UserInfo` (**admin** ; sessions révoquées ; 400 soi-même ; 409 dernier admin actif) |
| POST | `/api/users/{id}/enable` | `UserInfo` (**admin** ; `disabled` → `active` ; 409 pour un compte `pending` : passer par `approve`) |
| POST | `/api/users/{id}/reset-password` | `CreatedUser` (**admin** ; mot de passe temporaire, sessions révoquées, `must_change_password: true` ; sur son propre compte, la session courante est révoquée aussi) |
| GET | `/api/git` | `GitState` (§12.3 ; **admin**) |
| POST | `/api/git/pull` | `GitState` (pull `--ff-only` immédiat ; 400 sans dépôt, 409 arbre sale ou pull impossible) |
| GET | `/api/overview` | `Overview` |
| GET | `/api/projects` | `ProjectSummary[]` |
| POST | `/api/projects` body `{name, title, modality, message_bits, description}` | `ProjectSummary` |
| GET | `/api/projects/{name}` | `{summary: ProjectSummary, spec, experiments: ExperimentInfo[], hpo_plans: HpoPlanInfo[], notes: string}` |
| POST | `/api/projects/{name}/experiments` body `{name, from?: string}` | `{path}` |
| POST | `/api/projects/{name}/hpo` body `{name, experiment}` | `{path}` |
| GET | `/api/files?path=` | `{path, content, kind, validation: {valid, errors}}` (**admin** : le code et les specs ne sont jamais servis à un member) |
| PUT | `/api/files` body `{path, content}` | `{saved: true, validation, git: FileGit \| null}` (uniquement sous `projects/`, `benchmarks/`, `datasets/`, `lab/` ; anti path traversal, insensible à la casse ; 409 pour un `benchmarks/<nom>.yaml` existant utilisé par une évaluation enregistrée : nouvelle version seulement ; `git` renseigné en mode hébergé avec `auto_commit` dans un dépôt git, sinon `null`, §12.3) |
| POST | `/api/validate` body `{content, kind?, path?}` | `{valid, errors, plan: PhasePlanPreview \| null}` |
| GET | `/api/registry` | `{entries: Record<kind, RegistryEntry[]>, errors: {module, error}[]}` (**admin**) |
| GET | `/api/runs?project=&experiment=&status=&study=&host=&include_trials=false&limit=500` | `RunSummary[]` (plus récents d'abord ; expurgé pour member : `params = {}`, `error` réduit à sa dernière ligne — idem dans `Overview.running|recent` et `GET /api/compare`) |
| GET | `/api/runs/{id}` | `RunDetail` (expurgé pour member : `spec = {}`, `spec_yaml = ""`, `spec_path = null`, `overrides = {}`, `params = {}`, `plan = null`, `env = {git: null, git_diff: null, hardware, pip_freeze_lines: 0, command: null, launch: null}`, `error`, `events[].error` et `events[].message` réduits à leur dernière ligne, clé `events[].traceback` supprimée) |
| GET | `/api/runs/{id}/metrics?keys=a,b&max_points=1000` | `{series: Record<string, Series>, phases: {name, start, end}[]}` (keys vide ⇒ toutes) |
| GET | `/api/runs/{id}/system?max_points=500` | `{t: number[], series: Record<string, number[]>}` |
| GET | `/api/runs/{id}/logs?tail=500` | `{lines: string[]}` (**admin** : les journaux contiennent commandes et tracebacks) |
| GET | `/api/runs/{id}/file?path=&token=` | binaire (confiné au dossier du run ; member : `samples/` seulement, 403 ailleurs ; types média sûrs en ligne, le reste en pièce jointe, §12.7) |
| PUT | `/api/runs/{id}/notes` body `{notes}` | `{ok: true}` |
| PUT | `/api/runs/{id}/tags` body `{tags}` | `{ok: true}` |
| POST | `/api/runs/{id}/stop` | `Job \| {ok: true}` |
| POST | `/api/runs/{id}/checkpoints/{name}/fetch` | `{ok: true}` (rapatrie un .pt distant) |
| GET | `/api/compare?runs=a,b&keys=k1,k2&max_points=500` | `{runs: RunSummary[], series: Record<run_id, Record<key, Series>>}` |
| GET | `/api/studies?project=` | `StudySummary[]` |
| GET | `/api/studies/{id}` | `StudyDetail` (expurgé pour member : `spec = {}`) |
| PUT | `/api/studies/{id}/notes` body `{notes}` | `{ok: true}` |
| GET | `/api/leaderboard?project=&benchmark=&source=benchmark\|val` | `Leaderboard` |
| GET | `/api/benchmarks` | `BenchmarkInfo[]` |
| GET | `/api/datasets` | `DatasetInfo[]` (expurgé pour member : `paths = {}`) |
| GET/PUT | `/api/hosts` | `Host[]` (GET expurgé pour member : `ssh_target`, `workdir`, `data_dir`, `python`, `scaleway`, `notes` à `null` ; PUT reçoit `Host[]`, **admin** ; les champs calculés `unretrieved_*`, `inventory_at`, `active_jobs` sont ignorés à l'écriture) |
| POST | `/api/hosts/{name}/test` | `{ok, latency_ms, output, error}` |
| POST | `/api/hosts/{name}/setup` | `Job` (400 pour une machine retirée) |
| GET | `/api/hosts/{name}/inventory` | `HostInventory` (**admin** ; recalculé à chaque appel, mis en cache dans `.wmlab/hosts_inventory.json`, §12.4) |
| POST | `/api/hosts/{name}/retrieve` | `Job` de `kind: "retrieve"` (400 machine locale ; 409 si un rapatriement de la machine est déjà actif) |
| POST | `/api/hosts/{name}/decommission` body `{confirm: true, delete_instance?: boolean, force?: boolean}` | `DecommissionResult` (**admin** ; 400 sans `confirm` ou machine locale ; 409 job actif, checkpoints/runs non rapatriés, machine injoignable sans `force`, déjà retirée ; 502 erreur API Scaleway, machine non retirée) |
| POST | `/api/hosts/{name}/poweroff` | `{ok: true, instance_state}` (**admin** ; 409 job actif ; 400 sans `scaleway` ou sans `SCW_SECRET_KEY` ; 502 erreur API) |
| GET | `/api/hosts/{name}/instance` | `InstanceInfo` (**admin** ; jamais d'exception) |
| GET | `/api/jobs?status=` | `Job[]` (expurgé pour member : `overrides = {}`, `error` réduit à sa dernière ligne — idem dans `Overview.jobs`) |
| POST | `/api/jobs` body `{kind, host, spec_path?, run_id?, checkpoint?, benchmark?, overrides?, max_hours?, smoke?, init_from?: {run, checkpoint, start_phase, resume?}, git_ref?}` | `Job` (`git_ref` : §12.3 ; 400 révision inconnue, machine locale ≠ HEAD, ou machine retirée) |
| POST | `/api/jobs/estimate` body `{spec_path, host, overrides?}` | `{total_steps, est_hours: number \| null, est_eur: number \| null, basis: string}` |
| POST | `/api/jobs/{id}/cancel` | `Job` |
| POST | `/api/jobs/{id}/sync` | `Job` |
| GET | `/api/costs` | `Costs` |
| GET | `/api/costs.csv` | CSV (séparateur `;`, en-têtes FR) |
| GET/PUT | `/api/settings` | `WorkspaceSettings` (GET et PUT : **admin** ; en mode hébergé avec `auto_commit`, `workspace.yaml` est commité et poussé, §12.3) |

### 9.1 Serveur : organisation interne
- `wmlab/server/app.py` : `create_app(workspace: Path, token: str | None, sync: bool = True) -> FastAPI`
  et `serve(port, token, host, workspace, sync)`.
- `wmlab/server/catalog.py` : fichiers déclaratifs du workspace ; un lien symbolique (fichier ou
  dossier de projet, commité dans le dépôt) n'est jamais suivi, un YAML de plus de 1 Mo ou
  pathologique (imbrication profonde) est ignoré comme invalide.
- `wmlab/server/db.py` : SQLite (stdlib `sqlite3`, WAL). Tables `runs`, `studies`, `evals`,
  `checkpoints` (index reconstructible depuis les fichiers).
- `wmlab/server/indexer.py` : scan incrémental (mtime) de `runs/` et `studies/`, thread
  périodique (5 s) + `reindex()` à la demande.
- `wmlab/server/jobs.py` : `JobManager` (fichiers `.wmlab/jobs/<job_id>.json`), démarrage,
  annulation, rattachement au redémarrage (les jobs survivent à la fermeture de l'app).
- `wmlab/server/executors/{base,local,ssh}.py` :
  - `local` : `subprocess.Popen([...python, "-m", "wmlab.cli", ...], start_new_session=True)`,
    env `WMLAB_WORKSPACE/WMLAB_DATA/WMLAB_HOST/WMLAB_PRICE_EUR_PER_HOUR` (jamais `WMLAB_TOKEN`
    ni `SCW_SECRET_KEY` : le process d'entraînement importe les plugins), sortie vers
    `<data>/runs/<run_id>/stdout.log` (ou `studies/…`).
  - `ssh` : `ssh -o BatchMode=yes -o ConnectTimeout=10 <target>` (utilise ~/.ssh/config, clés
    de l'utilisateur ; aucun secret stocké), `rsync -az --delete` du code (exclusions : `.git`,
    `.venv`, `.wmlab`, `desktop`, `node_modules`, `__pycache__`), écriture du launch file,
    lancement détaché `nohup setsid … & echo $!`, annulation `kill -TERM -<pgid>`,
    sync descendante `rsync -az --safe-links --exclude 'checkpoints/*.pt'` des dossiers de
    runs/études vers `.wmlab/runs|studies/` (`--safe-links` : un lien symbolique créé sur la
    machine GPU vers l'extérieur du dossier — `/etc/wmlab/env`, clé SSH — n'est jamais recréé
    sur le serveur) ; tout identifiant venant de la machine (inventaire, `trials.jsonl`,
    noms de checkpoints) doit respecter `SAFE_ID` (`[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`, sans
    `..`) avant de servir de composant de chemin, sinon il est ignoré (journal) ou refusé. Tous les arguments passent par `shlex.quote` (y compris la partie
    chemin des spécifications rsync `cible:'chemin'`, interprétée par le shell distant) ;
    `ssh … -- <cible> <commande>` ; `ssh_target` est validé (`[utilisateur@]hôte`, sans
    option ni espace) ainsi que `workdir`/`data_dir`/`python` (ni `-` initial ni caractère de
    contrôle). Le lancement renvoie aussi `ps -o lstart= -p $!` (`Job.remote_start`) : poll
    et annulation ignorent un pid réutilisé par un autre process (job `lost`).
- `wmlab/server/costs.py` : agrégation (runs hors essais + études + évals + entrées manuelles).
- `wmlab/server/gitops.py` (`GitRepo`, `GitSync`), `wmlab/server/inventory.py` (inventaire,
  vérification des tailles, cache), `wmlab/server/hostops.py` (retrait / extinction / état
  d'instance), `wmlab/server/scaleway.py` (client HTTP), `wmlab/users.py` (comptes, mots de
  passe, sessions) et `wmlab/server/auth.py` (routes `/api/auth/*`, `/api/users*`, limitation de
  débit, journal d'audit) : mode hébergé (§12).
- Le launch file (`env/launch.json`) contient `git` (sha, branch, dirty, remote) + `git_diff`
  capturés **sur le Mac** au lancement (le serveur distant n'a pas `.git`), plus `user`,
  `git_ref`, `git_sha` (§12.2–12.3 ; pour un lancement par révision, `git` décrit le commit
  exporté : `{sha, branch: git_ref, dirty: false, remote}`, sans diff).

---

## 10. Desktop (`desktop/`)

- Electron (main/preload en CommonJS non compilé : `electron/main.cjs`, `electron/preload.cjs`),
  renderer React 19 + TypeScript + Vite + Tailwind CSS v4 (`@tailwindcss/vite`),
  `@tanstack/react-query`, `react-router` (HashRouter), ECharts (wrapper maison),
  CodeMirror 6 (`@uiw/react-codemirror` + `@codemirror/lang-yaml`), `lucide-react`.
- Main process : résout le workspace (`WMLAB_WORKSPACE` → config userData `config.json`
  (choix de l'utilisateur via le menu, mémorisé) → `resources/workspace.json` (défaut embarqué
  au build) → dialogue de choix ; une source n'est retenue que si son dossier existe), lance `<workspace>/.venv/bin/python -m wmlab.cli server
  --port <libre> --workspace <ws>` avec le jeton aléatoire dans la variable d'environnement
  `WMLAB_TOKEN` (jamais sur la ligne de commande, visible par `ps`), attend `/api/health`, expose
  `window.wmlab = {apiBase, token, platform}` via preload (contextBridge ; le preload obtient
  `apiBase`/`token` par un IPC synchrone `wmlab:session` réservé à la fenêtre principale), tue
  le backend à la fermeture (les jobs d'entraînement, eux, continuent). Fenêtre
  `titleBarStyle: "hiddenInset"`.
- Hors Electron (navigateur servi par FastAPI) : `apiBase = window.location.origin`, jeton de
  session lu dans `localStorage` (`wmlab.token`) après connexion par e-mail + mot de passe
  (`LoginGate`, §12.6) ; `?token=` n'est plus lu depuis l'URL.
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
`/runs/:id` · `/studies/:id` (onglets Analyse, Notes = `notes.md` de l'étude) · `/compare?runs=` · `/leaderboard` · `/library` (registres) ·
`/datasets` · `/benchmarks` · `/hosts` · `/budget` · `/settings`.
Bouton global « Nouveau lancement » (dialogue : projet → expérience ou plan HPO → machine →
overrides → branchement `init_from` (run, checkpoint, phase de départ ou `resume`) → durée max →
estimation € → lancer).

---

## 11. Skills Claude (`.claude/skills/`)
- `wmlab-experiment` : créer une architecture / attaque / dataset / perte / métrique / plan de
  curriculum / plan HPO / benchmark compatibles, les valider (`wmlab validate`,
  `wmlab plugin-test`, `wmlab train --smoke`).
- `wmlab-analyse` : analyser runs et études (`wmlab runs show --json`, fichiers de run),
  rédiger les conclusions dans `notes.md`, proposer les prochains plans en tenant compte du budget.
- `check` : toutes les vérifications du dépôt (`wmlab validate`, `pytest`, build + tests du
  desktop, `deploy/tests_dry_run.sh`) via l'agent `wmlab-check` ; rapport des échecs.
- Agents typés (`.claude/agents/` : `wmlab-check`, `wmlab-explore`, `desktop-build`,
  `wmlab-review`), hooks de garde et de validation (`.claude/hooks/`), règles par dossier
  (`.claude/rules/`) et `.claude/settings.json` : voir `CLAUDE.md` § Outillage Claude.

---

## 12. Mode hébergé : serveur central, équipe, git et rapatriement des checkpoints

Objectif : héberger le backend et toutes les données sur un serveur central (petite instance
Scaleway), laisser le code (scripts d'entraînement, plugins `lab/`, YAML) dans un dépôt git édité
localement par chaque membre de l'équipe, et garantir qu'aucun checkpoint n'est perdu quand une
machine GPU est supprimée.

```
[Postes des membres] ── git push ──► [Dépôt git distant (GitHub/GitLab…)]
        │ app de bureau (mode distant) ou navigateur          │ git pull --ff-only (auto)
        ▼                                                     ▼
[Serveur central : backend wmlab + .wmlab (runs, checkpoints, index, jobs, users)]
        │ git archive <sha> → rsync → workdir/code/<sha>      ▲ rsync complet (dont *.pt) en fin de job
        ▼                                                     │
[Machines GPU Scaleway, créées et retirées à la demande] ─────┘
```

### 12.1 Réglages (`workspace.yaml`)
```yaml
hosted:
  enabled: false        # true sur le serveur central
  git_branch: main      # branche suivie par le serveur
  auto_pull_s: 60       # 0 = désactivé
  auto_commit: true     # PUT /api/files → commit + push (sinon écriture locale seule)
  public_url: null      # ex. "https://lab.example.com", affiché dans l'UI
  allow_registration: true   # POST /api/auth/register ouvert (comptes créés en attente d'approbation)
```
`WorkspaceSettings.hosted: HostedSettings` (défauts ci-dessus). Les postes des membres gardent
`enabled: false` (mode local inchangé). Un `workspace.yaml` présent mais invalide ne ramène pas
le serveur aux valeurs par défaut : les derniers réglages lus avec succès sont conservés (journal
`error`), pour ne jamais désactiver le mode hébergé ni rouvrir l'inscription par accident.

### 12.2 Comptes, mots de passe et sessions (`.wmlab/users.yaml`, `.wmlab/sessions.json`)
```yaml
users:
  - id: u_1f3c9a0b7d2e              # identifiant stable (u_ + 12 hex)
    name: justin                    # identifiant d'équipe unique (attribution des runs, auteur git)
    email: justin@example.com       # identifiant de connexion, unique, stocké en minuscules
    role: admin                     # admin | member
    status: active                  # pending | active | disabled
    password_hash: "scrypt$15$8$1$<sel b64>$<hash b64>"   # null : mot de passe non défini
    must_change_password: false     # true après un mot de passe temporaire (CLI, réinitialisation)
    created_at: "…Z"
    approved_at: "…Z"               # null tant que pending
    approved_by: cli                # name de l'admin, ou "cli"
    disabled_at: null
    last_login_at: "…Z"
```
- **Deux rôles.** `admin` : tout (éditer, lancer, suivre, gérer machines, budget et comptes).
  `member` : **lecture seule** (suivi des runs, études, classement, budget, machines) **sans
  accès au code d'entraînement ni aux architectures** : ni `lab/`, ni `wmlab/`, ni specs YAML,
  ni registres, ni journaux, ni réglages (politique §9 : `GET` seulement, routes **admin**
  exclues, réponses expurgées). Lancer une expérience depuis l'app demande donc un compte
  `admin` ; le flux d'équipe (éditer localement → pousser → lancer par révision) est celui des
  admins.
- **Inscription** (`POST /api/auth/register`, publique si `hosted.allow_registration`) : crée un
  compte `pending`, rôle `member`, sans aucun accès (login ⇒ 403 « compte en attente »). Un
  admin l'approuve en choisissant le rôle (app : Réglages → Utilisateurs ; `wmlab users approve`).
  Un admin peut aussi créer directement un compte actif (`POST /api/users`, `wmlab users add`) :
  un **mot de passe temporaire** est affiché **une seule fois** ; `must_change_password: true`
  oblige à le changer à la première connexion (l'app n'affiche rien d'autre tant que ce n'est
  pas fait). Le premier admin est créé à l'installation (`deploy/install_server.sh --admin NAME
  --admin-email EMAIL`, §12.7).
- **Mot de passe** : 12 à 256 caractères, aucune autre règle (longueur plutôt que complexité) ;
  refusé s'il est égal à l'e-mail ou au nom. Haché **scrypt** (`hashlib.scrypt`, n = 2¹⁵, r = 8,
  p = 1, sel 16 octets, 32 octets de sortie, `maxmem = 2²⁶`), stocké `scrypt$15$8$1$<sel>$<hash>`
  (base64 sans padding ; le préfixe permet de changer d'algorithme plus tard). Vérification en
  temps constant (`hmac.compare_digest`) ; un e-mail inconnu subit quand même un hachage factice
  (pas d'énumération par le temps de réponse).
- **Session** : `POST /api/auth/login {email, password}` → jeton `wmls_<48 hex>` (24 octets
  `secrets.token_bytes`) ; l'ouverture relit le compte sous verrou et refuse (401) si son état ou
  son mot de passe a changé depuis la vérification (une réinitialisation ou une désactivation
  concurrente ne laisse jamais une session survivante) ; seul son SHA-256 est stocké dans `<data>/sessions.json`
  (`{sessions: [{id, user_id, token_sha256, created_at, expires_at, last_seen_at, ip}]}`, mode
  0600). Durée 30 jours (`SESSION_TTL_S`), `last_seen_at` rafraîchi au plus toutes les 5 min ;
  les sessions expirées sont purgées à chaque écriture. Révocation : `POST /api/auth/logout`
  (session courante), changement de mot de passe (toutes les autres sessions du compte),
  désactivation ou réinitialisation par un admin (toutes). Le rôle et l'état sont relus dans
  `users.yaml` à chaque requête (une rétrogradation ou une désactivation est immédiate).
- **Authentification** (`TokenMiddleware`) : en-tête `X-WMLab-Token` = jeton serveur (`--token` /
  `WMLAB_TOKEN` ; utilisateur implicite `{id: "", name: "admin", email: "", role: "admin"}`,
  `auth: "server_token"` ; usages : app en mode local, secours admin en ligne de commande) ou jeton
  de session d'un compte `active`. `scope["wmlab_user"] = {"id", "name", "email", "role",
  "auth", "session_id"}`. Sans jeton serveur configuré (`token=None` : tests, intégration), admin
  implicite. `?token=` : seulement `GET /api/runs/{id}/file` et `GET /api/costs.csv`.
- **Limitation de débit** (mémoire du process, fenêtre glissante, réservation atomique de la
  tentative avant la vérification du mot de passe, `wmlab/server/auth.py`) : `login` 10 tentatives
  par couple (e-mail, IP) et 30 par IP sur 15 min, 100 par e-mail par heure toutes IP confondues
  (force brute distribuée sans permettre de verrouiller un compte depuis une seule IP) ; `register`
  5 par IP par heure ; `POST /api/auth/password` 5 échecs par compte sur 15 min → 429 `{detail}` +
  `Retry-After` (exposé en CORS). IP = `request.client.host` (uvicorn applique `X-Forwarded-For`
  venant du proxy local, `forwarded_allow_ips` par défaut = 127.0.0.1) ; clé IPv6 = /64. Un e-mail
  de connexion mal formé ⇒ 401 sans journalisation de la valeur. Au plus `SCRYPT_SLOTS = 4` scrypt
  en parallèle ; au-delà de 5 s d'attente ⇒ 503 « serveur occupé ».
- **Journal d'audit** (`.wmlab/logs/server.log`, logger `wmlab.server`, lignes préfixées
  `audit:`) : connexions réussies et échouées (e-mail, IP), inscriptions, créations, approbations,
  changements de rôle, désactivations / réactivations, réinitialisations, changements de mot de
  passe, déconnexions, refus 403. Jamais de mot de passe ni de jeton.
- **Garde-fous** : un admin ne peut ni se rétrograder ni se désactiver lui-même (400) ; le
  dernier admin actif ne peut être ni rétrogradé ni désactivé (409) ; le nom `admin` reste
  réservé au jeton serveur.
- **Attribution** : inchangée — `Job.user`, `run.json.user`, `study.json.user` = `name` du
  compte (string | null), auteur git `<name> <<name>@wmlab.local>` (jamais l'e-mail dans le dépôt).
- **Module `wmlab.users`** : `User`, `Session`, `UserStore(data_root)` (écritures atomiques
  avec fsync, mode 0600, verrou du process + verrou inter-process `fcntl` sur `<data>/.users.lock`
  pour chaque lecture-modification-écriture — la CLI et le serveur peuvent tourner en même temps —,
  relecture quand un fichier change (mtime, taille, inode) ; les hachages scrypt se calculent hors
  verrou) avec `register(email, name, password)`, `add(email, name, role, password=None, by="cli")
  -> (User, temporary_password | None)`, `approve(id, role, by)`, `set_role(id, role, actor)`,
  `disable(id, actor)`, `enable(id, actor)`, `reset_password(id, actor) -> (User,
  temporary_password)` (`actor` = identité `{id, name, …}` de l'appelant, `CLI_ACTOR` en ligne de
  commande), `change_password(id, current, new, keep_session=None)` (409 si le hachage a changé
  entre-temps), `authenticate(email, password) -> User` (lève `UserError(401|403)`),
  `open_session(user, ip) -> (Session, token)`, `resolve_session(token) -> (User, Session) | None`
  (un échec d'écriture du `last_seen_at` est journalisé, jamais bloquant), `revoke_session(id)`,
  `revoke_user_sessions(user_id, keep=None)`, `by_email`, `by_id`, `by_name`, `users()`,
  `legacy_entries()`, `broken()` ; fonctions `hash_password`, `verify_password`,
  `check_password_policy`, `check_email`, `check_name`, `temporary_password()` (16 caractères
  `secrets`), `normalize_email`. E-mails, identifiants et mots de passe doivent être encodables en
  UTF-8 et sans caractère de contrôle (400 sinon). Erreurs : `UserError(message, status)` en
  français ; `UsersFileError` (503) quand `users.yaml` existe mais est illisible : les lectures ne
  voient aucun compte (personne ne se connecte, le jeton serveur reste utilisable), **aucune
  écriture n'a lieu** (le fichier n'est jamais remplacé), `wmlab users list` affiche l'erreur.
- **Routes** (§9) : `POST /api/auth/register`, `POST /api/auth/login`, `POST /api/auth/logout`,
  `POST /api/auth/password`, `GET /api/me`, `GET /api/users`, `POST /api/users`,
  `POST /api/users/{id}/approve|role|disable|enable|reset-password` (module `wmlab/server/auth.py`).
- **CLI** (sur le serveur, §8.4) : `wmlab users add EMAIL --name NAME [--role admin|member]
  [--password-stdin]`, `users list [--json]`, `users approve EMAIL [--role R]`, `users role EMAIL
  ROLE`, `users disable EMAIL`, `users enable EMAIL`, `users reset-password EMAIL`.
- **Ancien format** (entrées `{name, token_sha256}` sans `email`, jetons `wml_`) : plus reconnu.
  `wmlab users list` signale ces entrées (« ancien format : recréer le compte ») ; rien d'autre
  ne les lit, et elles sont conservées telles quelles quand le fichier est réécrit.

### 12.3 Git : révision lancée et édition depuis l'app
- `GET /api/git` → `GitState = {available: boolean, hosted: boolean, branch: string|null,
  sha: string|null, short_sha: string|null, dirty: boolean, remote: string|null` (URL sans
  identifiants : `https://user:token@hôte` est renvoyé `https://hôte`)`,
  ahead: number|null, behind: number|null, last_pull_at: string|null, last_pull_error: string|null}`.
  `POST /api/git/pull` → `GitState` (pull `--ff-only` immédiat de la branche courante ; 400 sans
  dépôt git, 409 si arbre sale ou pull impossible, `{detail}` en français).
- Thread `GitSync` (mode hébergé, `auto_pull_s > 0`) : `git status --porcelain` vide (hors
  `workspace.yaml`, voir ci-dessous) et serveur sur `hosted.git_branch` ⇒ `git fetch origin
  <branch>` puis `git merge --ff-only`. Erreurs mémorisées (`last_pull_error`), jamais levées.
- `PUT /api/files` en mode hébergé avec `auto_commit` : écrire → `git add <path>` →
  `git commit -m "app(<user>): <path>" --author="<user> <<user>@wmlab.local>"` → `git push origin
  <branch>` (sur échec : `git pull --rebase` puis un second push). Réponse enrichie :
  `git: {committed: string|null, pushed: boolean, error: string|null} | null` (`null` hors mode
  hébergé ou hors dépôt git). Un échec de push laisse le commit local et renvoie 200 avec
  `pushed: false` ; l'UI l'affiche.
- `workspace.yaml` (suivi par git, réécrit par `PUT /api/settings`) : en mode hébergé avec
  `auto_commit`, `PUT /api/settings` fait le même commit + push que `PUT /api/files`
  (`app(<user>): workspace.yaml` ; réponse inchangée, un échec de push laisse le commit local,
  visible dans `GitState.ahead`). En repli, ce fichier est exclu du test de saleté
  (`git status --porcelain -- . ':!workspace.yaml'`, utilisé par `GitState.dirty`, `GitSync` et
  `POST /api/git/pull`) : un `workspace.yaml` modifié localement ne suspend jamais le pull
  automatique ; si un pull amont touche ce fichier, l'échec du ff-only est mémorisé dans
  `last_pull_error`.
- Lancement : `POST /api/jobs` accepte `git_ref?: string` (défaut : `HEAD` du checkout du
  serveur ; forme `[A-Za-z0-9][A-Za-z0-9._/-]*`, sans `..`). Résolution
  `git rev-parse --verify "<ref>^{commit}"` (essayée aussi en `origin/<ref>`, après un
  `git fetch --prune origin` si la révision est inconnue localement) → `Job.git_ref`,
  `Job.git_sha` (400 si inconnu). Export : `git archive --format=tar <sha> | tar -x -C
  <data>/code/<sha>/` (cache marqué par `.wmlab-export` ; les 10 exports les plus récents sont
  conservés) ; un échec d'export ⇒ job `failed`. Pour un lancement par révision sur une machine
  `ssh`, le launch file décrit le commit exporté : `git = {sha, branch: git_ref, dirty: false,
  remote}`, `git_diff: null` (même si la révision est HEAD et que le checkout du serveur est
  sale : l'export ne contient aucune modification locale).
- Exécuteur `ssh` avec sha : rsync `<data>/code/<sha>/` → `<workdir>/code/<sha>/` (mêmes
  exclusions) ; venv **partagé** `<workdir>/.venv` ; `uv sync --frozen --extra train` exécuté dans
  `<workdir>/code/<sha>` avec `UV_PROJECT_ENVIRONMENT=<workdir>/.venv` seulement si le SHA-256 de
  `uv.lock` diffère du marqueur `<workdir>/.venv/.wmlab-lock` (écrit après un sync réussi) ;
  commande : `cd <workdir>/code/<sha> && PYTHONPATH=<workdir>/code/<sha> WMLAB_WORKSPACE=<workdir>/code/<sha> WMLAB_DATA=<data_dir> … <python> -m wmlab.cli …`
  (`PYTHONPATH` garantit que le package `wmlab` importé est celui du sha, quel que soit
  l'editable install du venv). Sans dépôt git (ou `git_ref` absent hors mode hébergé) : comportement
  actuel (rsync du workspace vers `<workdir>`), `Job.git_sha` = HEAD si disponible.
- Exécuteur `local` : exécute toujours le checkout du serveur (HEAD) ; `git_ref` ≠ HEAD ⇒ 400
  « la machine locale exécute la révision courante du serveur ». `Job.git_ref = "HEAD"`,
  `Job.git_sha` = HEAD ; le launch file garde l'état réel du checkout (`dirty`, `git_diff`).
- Job setup en mode git (mode hébergé avec dépôt) : exporte HEAD vers `<workdir>/code/<sha>`
  (`Job.git_ref = "HEAD"`) puis lance `scripts/remote_bootstrap.sh` depuis ce dossier avec
  `UV_PROJECT_ENVIRONMENT=<workdir>/.venv` ; sinon rsync du workspace vers `<workdir>` et
  bootstrap depuis `<workdir>` (`git_ref: null`). Le venv est `<workdir>/.venv` dans les deux
  cas ; le script écrit le marqueur `.wmlab-lock`.

### 12.4 Rapatriement des checkpoints et retrait d'une machine
- **Fin de job (train/hpo/eval)** : synchronisation **complète** (`checkpoints/*.pt` inclus) des
  runs et études du job, puis vérification : chaque entrée de `checkpoints/index.json` doit exister
  localement avec `size_bytes` identique (une relance rsync sur écart). Résultat dans
  `Job.retrieval: {status: "complete"|"partial"|"failed"|"n/a", checkpoints: number, bytes: number,
  missing: string[]} | null`. Le job reste `completed` même si `partial` (l'UI l'affiche en
  avertissement). `Checkpoint.available_locally` reflète l'état sur le serveur.
- **Inventaire** : `GET /api/hosts/{name}/inventory` →
  `HostInventory = {host: string, reachable: boolean, error: string|null, checked_at: string,
  active_jobs: number, remote_data_dir: string|null,
  runs: {run_id: string, project: string|null, status: string|null, checkpoints_total: number,
         checkpoints_local: number, bytes_remote: number, bytes_missing: number, indexed: boolean}[],
  studies: {study_id: string, trials: number, synced: boolean}[],
  unretrieved_checkpoints: number, unretrieved_bytes: number, unindexed_runs: number}`.
  Implémentation ssh : une commande distante listant `<data_dir>/runs/*/checkpoints/index.json`
  et les tailles des `*.pt` (`find … -printf '%p\t%s\n'`), comparée aux fichiers locaux.
  Hôte `local` : inventaire trivial (tout est local). Résultat mis en cache dans
  `.wmlab/hosts_inventory.json` ; `Host` expose `unretrieved_checkpoints: number|null`,
  `unretrieved_bytes: number|null`, `inventory_at: string|null`, `active_jobs: number`.
- **Rapatriement** : `POST /api/hosts/{name}/retrieve` → `Job` de `kind: "retrieve"`
  (`target` = nom de l'hôte) : rsync complet de **tous** les runs et études présents sur l'hôte
  (pas seulement ceux lancés par l'app), vérification comme ci-dessus, réindexation ;
  `Job.result: {runs: number, studies: number, checkpoints: number, bytes: number,
  missing: string[]} | null` (mis à jour pendant le transfert). Un job `retrieve` ne compte pas
  dans `max_concurrent_jobs` ; il est accepté sur une machine déjà retirée (400 pour la
  machine locale, 409 si un rapatriement de la même machine est actif) ; il s'exécute dans le
  process du serveur : interrompu par un redémarrage, il passe `lost` (le relancer ; rsync
  reprend où il en était) ; l'inventaire de la machine est recalculé à sa fin.
- **Retrait** : `POST /api/hosts/{name}/decommission` body
  `{confirm: true, delete_instance?: boolean, force?: boolean}` (admin ; 400 sans `confirm` ou
  pour la machine locale). Refus 409 si la machine est déjà retirée, si un job est actif sur
  l'hôte, si l'inventaire (recalculé à l'appel) a `unretrieved_checkpoints > 0` ou
  `unindexed_runs > 0` (message : « N checkpoints (X Go) non rapatriés — lancez le
  rapatriement »), ou si l'hôte est injoignable (sauf `force: true`, admin, journalisé). Une
  erreur de l'API Scaleway (502) laisse la machine non retirée. Succès : `HostSpec.decommissioned_at`
  renseigné (l'entrée reste dans `hosts.yaml` pour l'historique, plus proposée au lancement) ;
  si `delete_instance` et `host.scaleway` configuré et `SCW_SECRET_KEY` présent dans
  l'environnement du serveur : suppression de l'instance via l'API Scaleway (voir 12.5).
  Réponse : `{ok: true, deleted_instance: boolean, instance_state: string|null, message: string}`.
- **Extinction** : `POST /api/hosts/{name}/poweroff` (admin ; même garde « aucun job actif » ;
  400 sans `scaleway` configuré ou sans `SCW_SECRET_KEY`, 502 erreur API ; `poweroff` conserve
  les volumes, facturation GPU arrêtée ; attente de l'état `stopped`) → `{ok, instance_state}`.
- `HostSpec` (et `Host` API) gagne : `scaleway: {zone: string, server_id: string} | null`,
  `decommissioned_at: string | null`. Les jobs refusent une machine décommissionnée (400).

### 12.5 Client Scaleway (`wmlab/server/scaleway.py`)
- `httpx` (dépendance de l'extra `server`), base `https://api.scaleway.com`, en-tête
  `X-Auth-Token: $SCW_SECRET_KEY` ; jamais de clé dans `hosts.yaml`, les logs ou l'API.
- `get_server(zone, server_id) -> {id, name, state, commercial_type, zone, volumes: [...]}` ;
  `server_action(zone, server_id, action)` avec `action ∈ {poweroff, terminate}` (POST
  `/instance/v1/zones/{zone}/servers/{id}/action`) ; `delete_server(zone, server_id,
  with_volumes=True)` = `terminate` puis suppression des volumes restants (`DELETE
  /instance/v1/zones/{zone}/volumes/{id}`) ; attente de l'état final avec timeout (5 min).
- `GET /api/hosts/{name}/instance` → `{configured: boolean, state: string|null, commercial_type,
  zone, error: string|null}` (jamais d'exception).
- Erreurs API → `JobError(502, message en français)`.

### 12.6 Desktop et navigateur en mode équipe
- **Écran de connexion** (`LoginGate`) : affiché quand aucune session n'est connue ou que
  `GET /api/me` renvoie 401 : e-mail + mot de passe (et URL du serveur en navigateur si l'origine
  n'est pas le backend) → `POST /api/auth/login` ; lien « Créer un compte » → formulaire
  (identifiant, e-mail, mot de passe, confirmation) → `POST /api/auth/register` → message
  « Demande enregistrée : un administrateur doit approuver votre compte » (lien caché si
  `GET /api/health` indique `registration: false`). Jeton de session mémorisé dans `localStorage`
  (`wmlab.token`, try/catch) en navigateur ; « Se déconnecter » (pied de la sidebar, Réglages →
  Connexion) appelle `POST /api/auth/logout` puis oublie le jeton. Si `Me.must_change_password`,
  l'app n'affiche qu'un écran « Définir un nouveau mot de passe » (`POST /api/auth/password`)
  jusqu'au changement.
- **Electron, mode distant** : `config.json` (userData) `{mode: "remote", apiBase, token}` (jeton
  de session, chiffré via `safeStorage` quand disponible) ; menu « Serveur » : « Se connecter à
  un serveur distant… » (dialogue URL + e-mail + mot de passe ; le main process appelle
  `/api/health` puis `POST /api/auth/login` et ne stocke que le jeton de session, jamais le mot de
  passe), « Utiliser le backend local ». « Se déconnecter » appelle `POST /api/auth/logout`
  (meilleur effort) puis oublie le jeton. En mode distant, aucun backend n'est lancé ;
  `window.wmlab = {apiBase, token, platform, mode}`.
- **Permissions dans l'interface** (`usePermissions()` : `{role, isAdmin, canWrite, canViewCode}`,
  dérivé de `GET /api/me` ; mode local Electron = admin) : pour un `member`, l'interface est en
  lecture seule — pas de bouton « Nouveau lancement », de création de projet / expérience / plan,
  d'éditeur YAML (les expériences et plans sont listés avec leurs métadonnées seulement), de
  modification des notes et des tags, d'arrêt / annulation / rapatriement, d'actions sur les
  machines ; pages Bibliothèque et onglet Journaux absents ; ni paramètres (`params` vides) ni
  timeline des phases (`plan` null) ; Réglages réduit à « Mon compte » (nom, e-mail, rôle,
  changement de mot de passe), « Connexion » et « Apparence » (préférence locale). Un 403
  inattendu est affiché en toast « Action réservée aux administrateurs ».
- **Auteur** : colonne/label « par <user> » dans les listes de runs, d'études et de jobs ;
  filtre « mes runs ».
- **Dialogue de lancement** : champ « Révision git » pré-rempli depuis `GET /api/git`
  (`branch @ short_sha`), avertissement si `dirty` hors mode hébergé, aide « poussez votre branche
  puis indiquez son nom ou un sha ».
- **Éditeur de spec** : après enregistrement, toast « Enregistré · commité abc1234 · poussé » ou
  avertissement « commité mais non poussé : … ».
- **Machines** : par machine, inventaire (runs, checkpoints non rapatriés en nombre et Go, jobs
  actifs, état de l'instance Scaleway si configurée), boutons « Rapatrier les checkpoints »
  (job `retrieve` avec progression), « Éteindre l'instance », « Retirer la machine » (désactivé tant
  qu'il reste des checkpoints non rapatriés ou des jobs actifs ; dialogue de confirmation qui
  récapitule runs/checkpoints vérifiés et propose « supprimer aussi l'instance Scaleway » quand
  c'est configuré). Badge d'avertissement sur la carte tant que `unretrieved_checkpoints > 0`.
- **Réglages** : section « Mon compte » (tous : nom, e-mail, rôle, formulaire de changement de
  mot de passe), section « Mode hébergé » (admin : branche, dernier pull, bouton « Tirer
  maintenant », URL publique, inscription ouverte ou fermée), section « Utilisateurs » (admin :
  tableau e-mail / nom / rôle / état / dernière connexion ; actions « Approuver » (choix du rôle),
  « Changer le rôle », « Désactiver » / « Réactiver », « Réinitialiser le mot de passe » et bouton
  « Créer un compte » — le mot de passe temporaire est montré une seule fois dans un dialogue ;
  badge « en attente » dans la sidebar tant qu'une inscription attend un admin).

### 12.7 Déploiement (`deploy/`)
- `deploy/install_server.sh` (Ubuntu 24.04, idempotent) : paquets (git, rsync, curl), uv,
  Node 22 (NodeSource), clone du dépôt (`--repo URL --branch main --dir /opt/wmlab`),
  `uv sync --extra server --extra train` (+ `--extra cpu` si l'extra CPU-only de torch est défini
  dans `pyproject.toml` via les sources uv conditionnelles), `npm ci && npm run build` dans
  `desktop/`, premier compte admin `wmlab users add <email> --name <nom> --role admin`
  (`--admin NAME --admin-email EMAIL` ; mot de passe temporaire affiché une seule fois, à changer
  à la première connexion ; le nom `admin` est réservé à l'utilisateur implicite du jeton
  serveur), service systemd `deploy/wmlab-server.service` (`EnvironmentFile=/etc/wmlab/env` :
  `WMLAB_WORKSPACE`, `WMLAB_DATA=<dir>/.wmlab` (racine des données épinglée par
  l'environnement : `workspace.yaml:data_dir`, fichier versionné tiré de git, ne peut pas
  déplacer `users.yaml`), `WMLAB_TOKEN`, `SCW_SECRET_KEY` optionnel ; durcissement :
  `ProtectSystem=strict` + `ReadWritePaths=<dir> <home>/.ssh`, `ProtectHome=read-only` non utilisé
  car la clé SSH est sous `<home>`, `NoNewPrivileges`, `PrivateTmp`, `PrivateDevices`,
  `ProtectKernel*`, `ProtectControlGroups`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`,
  `RestrictNamespaces`, `RestrictRealtime`, `RestrictSUIDSGID`, `LockPersonality`,
  `SystemCallArchitectures=native`, `SystemCallFilter=@system-service` avec
  `SystemCallErrorNumber=EPERM`, `CapabilityBoundingSet=`, `UMask=0027`, `CacheDirectory=wmlab`
  + `XDG_CACHE_HOME=/var/cache/wmlab` pour les caches torch / poids des jobs locaux ; marqueurs
  `__WMLAB_DIR__`, `__WMLAB_USER__`, `__WMLAB_HOME__` (home réel du compte de service),
  `__WMLAB_PORT__` remplacés à l'installation ; `.wmlab/` en 0750), reverse proxy
  `deploy/Caddyfile` (HTTPS automatique → `127.0.0.1:8765` ;
  en-têtes `Strict-Transport-Security` avec `includeSubDomains`, `Content-Security-Policy`
  (`default-src 'self'`, `img-src 'self' data: blob:`, `media-src 'self' blob:`, `style-src 'self'
  'unsafe-inline'`, `frame-ancestors 'none'`, `base-uri 'self'`, `form-action 'self'`, `object-src
  'none'`), `X-Frame-Options DENY`, `X-Content-Type-Options nosniff`, `Referrer-Policy
  no-referrer`, `Permissions-Policy` minimal, en-têtes en `defer` pour remplacer ceux du
  backend ; `request_body max_size 8MB` ; journal d'accès JSON filtré : chaîne de requête
  retirée de l'URI et en-tête `X-WMLab-Token` supprimé — Caddy ne masque par défaut
  qu'`Authorization` et `Cookie`), `deploy/patch_workspace.py` (bloc `hosted:` dont
  `allow_registration`, option `--allow-registration true|false`), `deploy/backup.sh` (archive quotidienne de `.wmlab` vers un bucket Object
  Storage via `rclone` si présent, sinon `tar` local), `deploy/README.md` (DNS, clé SSH du serveur
  à déposer sur les machines GPU, comptes et approbation, connexion de l'app).
- Les mêmes en-têtes de sécurité (hors HSTS) sont ajoutés par le backend aux pages de
  `desktop/dist` (`_DesktopFiles`, `SPA_SECURITY_HEADERS`) : l'interface reste protégée en
  accès par tunnel SSH. Autres protections du backend (`wmlab/server/auth.py`) :
  `BodyLimitMiddleware` (corps `/api/*` > 4 Mo ⇒ 413, corps sans `Content-Length` ⇒ 411) ;
  `GET /api/runs/{id}/file` ne sert en ligne que les types média sûrs (`INLINE_MEDIA_TYPES` :
  images, audio, vidéo, JSON, texte) avec `X-Content-Type-Options: nosniff` et
  `Content-Security-Policy: default-src 'none'; sandbox`, tout autre fichier (HTML, SVG, `.pt`…)
  part en pièce jointe `application/octet-stream` ; un member n'y lit que `samples/` (jamais les
  checkpoints, `spec.yaml` ni les journaux) ; en mode hébergé, les réponses 404 (fichier) et
  500 ne contiennent ni chemin du serveur ni texte d'exception (référence vers `server.log`) ;
  uvicorn sans en-tête `Server`.
