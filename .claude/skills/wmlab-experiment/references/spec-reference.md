# Référence des specs YAML (ARCHITECTURE.md §5, schémas `wmlab/spec/models.py`)

Règles générales : **champs inconnus interdits** partout ; `name` = nom du fichier (sans
`.yaml`) pour expériences, plans HPO, benchmarks et datasets ; `name` d'un projet = nom de
son dossier. Noms : `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`. Validation en deux niveaux :
schéma (types, bornes) puis sémantique (composants connus et compatibles avec la modalité,
params acceptés, références `@dataset`, benchmark, phases, termes de perte, sélecteurs).

## Sommaire

1. Valeurs planifiées (`ScheduledValue`)
2. Projet (`project.yaml`)
3. Expérience (`experiments/<nom>.yaml`) — tous les blocs
4. Phase (`PhaseSpec`) et planning de LR (`LRScheduleSpec`)
5. Overrides (`--set`, espaces HPO)
6. Plan HPO (`hpo/<nom>.yaml`)
7. Benchmark (`benchmarks/<nom>.yaml`)
8. Catalogue de datasets (`datasets/<nom>.yaml`)
9. Hôtes (`.wmlab/hosts.yaml`) et workspace (`workspace.yaml`)

---

## 1. Valeurs planifiées

Utilisables pour `phases[].loss_weights.<terme>` et `phases[].attack_strength`. `progress`
va de 0 (1er step de la phase) à 1 (dernier step).

| Forme | Sens |
|---|---|
| `0.5` | constante |
| `{ramp: [a, b], shape: linear\|cosine\|exp, start: 0.0, end: 1.0}` | de a à b entre les fractions `start` et `end` de la phase, constante avant / après ; défauts `shape: linear`, `start: 0`, `end: 1` |
| `{piecewise: [[0.0, 0.1], [0.5, 1.0], [1.0, 1.0]]}` | interpolation linéaire par morceaux (fractions croissantes dans [0, 1]) |

`exp` : interpolation géométrique si a et b sont de même signe et non nuls.

## 2. Projet — `projects/<p>/project.yaml`

| Champ | Type / défaut | Notes |
|---|---|---|
| `name` | requis | = dossier |
| `title` | `""` → `name` | affiché dans l'app |
| `modality` | requis : `image` \| `audio` \| `video` | fixe pour le projet |
| `description` | `""` | |
| `message_bits` | `48` (1–4096) | = `nbits` des modèles |
| `default_benchmark` | `null` | nom d'un `benchmarks/*.yaml` de même modalité |
| `objectives.quality` | `{metric, direction: maximize, label, constraint}` | défaut : `psnr` (image/vidéo), `snr` (audio) ; `constraint` : `">= 38"` (opérateurs `>=`, `<=`, `>`, `<`, `==`) |
| `objectives.robustness` | idem | défaut `bit_acc_mean` |
| `tags` | `[]` | |

## 3. Expérience — `projects/<p>/experiments/<nom>.yaml`

| Champ | Défaut | Notes |
|---|---|---|
| `name` | requis | = fichier |
| `description`, `hypothesis` | `""` | **toujours remplir** |
| `seed` | `0` | |
| `model.name` | requis | registre `MODELS` compatible avec la modalité |
| `model.params` | `{}` | **jamais `nbits`** ; validés par le params model du composant |
| `data.train`, `data.val` | requis | `DataSplitSpec` ci-dessous |
| `data.batch_size` | `32` | |
| `data.num_workers` | `4` | 0 = chargement dans le process principal |
| `data.sample_rate` | `16000` | audio |
| `data.fps` | `24` | vidéo |
| `optimizer` | `{name: adamw, lr: 1.0e-3, weight_decay: 1.0e-4, betas: [0.9, 0.999], grad_clip: 1.0}` | `lr` > 0 = LR de base ; `grad_clip: null` désactive le clipping |
| `loss.terms` | requis (≥ 1) | `{<terme>: {name, weight: 1.0, params: {}}}` |
| `phases` | requis | voir §4 ; noms uniques ; `[]` = **évaluation seule** (baseline pré-entraînée : validation au step 0, checkpoint de référence, benchmark final ; pas d'`init_from`, pas de plan HPO dessus ; `data.train` reste requis mais inutilisé) |
| `eval.every_steps` | `1000` | + validation en fin de phase |
| `eval.val_batches` | `10` | |
| `eval.val_attacks` | `[]` | `[{label, name, params}]` (label défaut = name, uniques) ⇒ `val/bit_acc/<label>` et `val/bit_acc_mean` |
| `eval.final_benchmark` | `null` | `{benchmark: <nom>, checkpoints: ["last", "best:val/…"]}` |
| `checkpoint.every_steps` | `2000` | `null` = seulement fin de phase / final |
| `checkpoint.keep_last` | `2` | |
| `checkpoint.keep_best` | `{}` | `{"val/<métrique>": max\|min}` (clés `val/...` uniquement) |
| `checkpoint.at_phase_end` | `true` | crée `phase_end:<phase>` (branchements) |
| `samples` | `{every_steps: 2000, count: 4}` | `every_steps: null` = jamais |
| `resources` | `{device: auto, precision: auto, compile: false}` | device `auto\|cpu\|mps\|cuda\|cuda:N` ; precision `auto\|fp32\|bf16\|fp16` (auto = bf16 sur CUDA) |
| `init_from` | `null` | `{run, checkpoint: "last", start_phase: null, load_optimizer: false, resume: false}` |
| `tags` | `[]` | |

`DataSplitSpec` : `{dataset: <nom>, params: {}, preprocess: [{name, params: {}}], limit: null}`.
`limit` > 0 tronque le dataset. `params.modality` (dataset `synthetic`) doit valoir celle du
projet. Chemins : `root: "@<catalogue>"`, jamais absolus.

`init_from` : `run` = run_id (dossier sous `.wmlab/runs/`) ; `checkpoint` = sélecteur
(`last`, `best:<métrique>`, `phase_end:<phase>`, nom exact) ; `start_phase` = nom ou index de
phase de CETTE expérience (le step global commence à la somme des phases précédentes) ;
`load_optimizer` recharge l'état d'Adam. Même modèle et même `message_bits` requis.
`resume: true` (incompatible avec `start_phase`) reprend au **step du checkpoint** : phase
localisée dans le plan de cette expérience, optimiseur chargé, RNG re-seedés — pour
continuer un run arrêté (`max_hours`, kill). Sans `resume`, le curriculum est rejoué depuis
`start_phase` avec les poids chargés (branche).

## 4. Phase et planning de LR

| Champ | Défaut | Notes |
|---|---|---|
| `name` | requis | unique |
| `steps` | requis (> 0) | |
| `lr` | `{schedule: constant}` | voir tableau suivant |
| `loss_weights` | `{}` | `{<terme>: ScheduledValue}` ; termes absents ⇒ `loss.terms.<t>.weight` ; terme inconnu = erreur |
| `attacks` | `[]` | `[{name, params: {}, p: 1.0, label: null}]` ; tirage ∝ `p` à chaque step ; vide ⇒ identité |
| `attack_strength` | `1.0` | ScheduledValue |
| `random_strength` | `true` | force effective `U(0, s)` ; `false` ⇒ exactement `s` |
| `freeze` | `[]` | préfixes de noms de paramètres gelés (ex. `["embedder."]`) |
| `batch_size` | `null` | override de `data.batch_size` |
| `data` | `null` | `{train: DataSplitSpec}` ⇒ loader reconstruit en début de phase |
| `reset_optimizer` | `false` | réinitialise l'optimiseur au début de la phase |

`LRScheduleSpec` (facteur multiplié par `optimizer.lr`) :

| Champ | Défaut | Sens |
|---|---|---|
| `schedule` | `constant` | `constant`, `linear_warmup` (= constant après warmup), `cosine`, `linear`, `wsd`, `exp` |
| `warmup_steps` | `0` | warmup linéaire de `factor/W` à `factor`, appliqué à TOUS les schedules |
| `factor` | `1.0` | multiplicateur crête |
| `min_factor` | `0.0` | plancher de `cosine`, `linear`, `wsd`, `exp` |
| `decay_fraction` | `0.2` | `wsd` : fraction finale (après warmup) en décroissance linéaire |
| `gamma` | `0.999` | `exp` : facteur par step |

La timeline de l'éditeur de l'app (et `POST /api/validate`) affiche LR, poids et force
d'attaque par phase : utile pour vérifier un curriculum.

## 5. Overrides

`wmlab train SPEC --set optimizer.lr=3e-4 --set phases.1.steps=5000` ; même syntaxe pour les
clés de `space` et `fidelity.overrides` d'un plan HPO et pour le dialogue « Nouveau
lancement ». Chemins pointés, **index de liste numériques** (`phases.1`, `attacks.0`),
valeurs parsées en YAML (`3e-4` → float, `null` → None). Une clé finale absente d'un dict est
créée (ex. `model.params.depth` si le modèle l'accepte).

## 6. Plan HPO — `projects/<p>/hpo/<nom>.yaml`

| Champ | Défaut | Notes |
|---|---|---|
| `name` | requis | = fichier |
| `experiment` | requis | expérience de base du même projet (nom ou chemin) |
| `description`, `hypothesis` | `""` | |
| `sampler` | `{name: tpe, params: {}}` | `tpe`, `random`, `nsga2`, `cmaes`, `grid` ; `params` passés au sampler Optuna |
| `pruner` | `{name: asha, params: {}}` | `asha`, `hyperband`, `median`, `none` ; ignoré si > 1 objectif |
| `objectives` | requis (≥ 1) | `[{metric: val/..., direction: maximize\|minimize}]` |
| `constraints` | `[]` | `[{metric: val/..., op: ">=", value: 36}]` (`>=`, `<=`, `>`, `<`) |
| `space` | requis (≥ 1) | `{<chemin>: {type: float\|int\|categorical, low, high, step, log, choices}}` |
| `fidelity` | `{steps_scale: 1.0, overrides: {}}` | `steps_scale` ∈ ]0, 1] |
| `budget` | `{max_trials: 20, max_hours: null, max_eur: null, parallel: 1}` | au moins une limite |
| `objective_agg` | `last` | `last` \| `best` |

Contraintes de `space` : `low ≤ high` ; `log: true` ⇒ `low > 0` et pas de `step` ; `int` ⇒
bornes entières ; `categorical` ⇒ `choices` non vide ; sampler `grid` ⇒ floats avec `step`.
Chaque chemin doit exister dans l'expérience de base (ou être un param accepté par le
composant) et la valeur doit garder l'expérience valide.

## 7. Benchmark — `benchmarks/<nom>.yaml`

| Champ | Défaut | Notes |
|---|---|---|
| `name` | requis | = fichier, ex. `image_v2` |
| `version` | `1` | entier ≥ 1 |
| `modality` | requis | |
| `description` | `""` | rappeler « ne jamais modifier » |
| `data` | requis | `DataSplitSpec` déterministe (seed, `limit`) |
| `batch_size` | `16` | |
| `seed` | `0` | messages et aléa des attaques |
| `quality_metrics` | `[]` | métriques compatibles avec la modalité |
| `fpr` | `[1e-2, 1e-4, 1e-6]` | dans ]0, 1[ |
| `attacks` | requis (≥ 1) | `[{label, name, params}]`, labels uniques, toutes à strength 1 |

Résultat : `eval/<checkpoint>__<benchmark>.json` (`…__smoke.json` pour une évaluation
`--smoke`, ignorée par le classement) ; `summary.bit_acc_mean` et `tpr@<fpr>_mean` = moyenne
hors `identity`.

## 8. Catalogue — `datasets/<nom>.yaml`

`{name, modality, description: "", source: "", license: "", paths: {default: "~/…", <hôte>: "/…"}}`
(`paths` non vide). Référencé par `@<nom>` dans les params de dataset.

## 9. Hôtes et workspace

`.wmlab/hosts.yaml` : `hosts: [{name, kind: local|ssh, ssh_target, workdir, data_dir,
python, price_eur_per_hour: 0, gpu, max_concurrent_jobs: 1, notes}]` (`ssh_target` requis
pour `ssh`). Géré depuis la page « Machines » de l'app.

`workspace.yaml` : `name`, `budget: {total_eur: 40000, start, end, manual_entries: [{label,
eur, date, project}]}`, `data_dir: .wmlab`, `sync_interval_s: 30`, `default_host: local`.
