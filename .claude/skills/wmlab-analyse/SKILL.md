---
name: wmlab-analyse
description: >-
  Analyser les runs d'entraînement, évaluations et études HPO de Watermark Lab (watermarking
  image / audio / vidéo) : lire les dossiers de run (run.json, metrics.jsonl, eval/*.json,
  events.jsonl) et d'étude (study.json, trials.jsonl), diagnostiquer (décodeur bloqué à
  0,5, imperceptibilité qui s'effondre, surapprentissage aux attaques, GPU sous-utilisé, LR
  trop fort, run perdu), comparer des runs, rédiger la conclusion dans le notes.md du run et
  proposer le prochain plan (HPO, branche init_from, ablation) avec son coût face au budget.
  À utiliser dès que l'utilisateur demande « comment s'est passé ce run », « pourquoi ça ne
  marche pas », « compare », « que faire ensuite », « combien il reste de budget »,
  « peut-on retirer cette machine » (inventaire, checkpoints non rapatriés, auteur des runs
  en mode équipe). Keywords: training curves, metrics, benchmark, Pareto,
  study, trials, diagnose, GPU utilization, cost, budget, next experiment, retrieval,
  unretrieved checkpoints, host inventory, decommission, user.
---

# wmlab-analyse — analyser runs et études, conclure, proposer la suite

Les dossiers de run sont la **source de vérité** (ARCHITECTURE.md §6) : tout se lit dans
`<data>/runs/<run_id>/` (`<data>` = `.wmlab/` du workspace, ou `$WMLAB_DATA`). Les runs
distants sont synchronisés par l'app (checkpoints `.pt` exclus). Commande de base :
`.venv/bin/python -m wmlab.cli` — abrégé `wmlab` ci-dessous.

## Procédure

### 1. Trouver les runs / études concernés

```bash
wmlab runs list --json [--project P] [--status completed|running|failed|killed|lost] [--include-trials]
wmlab runs show <run_id ou fragment unique> --json   # manifest, summary, checkpoints, evals, metric_keys, last_values, notes
ls .wmlab/studies/                                    # études HPO (study.json, trials.jsonl)
```

Si l'utilisateur ne précise pas : le ou les runs les plus récents du projet dont il parle ;
en mode équipe (plusieurs auteurs, champ `user` de `run.json`), privilégier **ses** runs
(« mes runs » = `user` égal au nom de l'utilisateur connecté, `GET /api/me`) et nommer
l'auteur quand on compare des runs de personnes différentes. Statut `lost` = `running` sans
heartbeat depuis > 600 s (process mort, machine éteinte, ou synchronisation interrompue).
Sur le serveur central (`hosted.enabled: true`), `.wmlab/` est celui du serveur : lire les
runs via l'API (`X-WMLab-Token`) ou directement sur le serveur.

### 2. Lire les données

| Fichier | Contenu |
|---|---|
| `run.json` | statut, projet/expérience, hôte, prix €/h, **`user`** (auteur, mode équipe ; `null` en local), `params` (aplatis), `overrides`, `progress`, `summary` (dernières `val/*` + `best/*`), `parent`, `error` |
| `env/git.json`, `env/launch.json` | commit exécuté (`sha`, `branch`, `dirty`) ; launch file de l'app : `job_id`, `host`, `user`, `git` — en mode équipe `dirty: false` et le sha est celui choisi dans « Révision git » |
| `.wmlab/jobs/<job_id>.json` | job de l'app : `kind` (`train`/`hpo`/`eval`/`setup`/`retrieve`), `status`, `user`, `git_sha`, **`retrieval`** `{status: complete\|partial\|failed\|n/a, checkpoints, bytes, missing[]}` (rapatriement vérifié en fin de job), `result` (job `retrieve`) |
| `spec.yaml` | expérience résolue (overrides appliqués) : phases, attaques, poids |
| `metrics.jsonl` | `{"t", "step", "phase", "values": {clé: float}}` — `train/*` toutes les 20 steps, `val/*` à chaque validation |
| `eval/<ckpt>__<bench>.json` | benchmark figé : `quality`, `robustness[label] = {bit_acc, tpr@…}`, `summary` (une évaluation `--smoke` s'écrit à part en `…__smoke.json`, `"smoke": true`, hors classement) |
| `system.jsonl` | `gpu_util`, `gpu_mem_gb`, `cpu_percent`, `ram_gb`, `proc_ram_gb` toutes les 15 s ; **`gpu_util` n'existe qu'avec `nvidia-smi` (CUDA)** — sur Mac/MPS il n'y a que `gpu_mem_gb`, `cpu_percent`, `ram_gb`, `proc_ram_gb` |
| `events.jsonl` | `status`, `phase_start`/`phase_end`, `checkpoint`, `eval`, `warning`, `error` |
| `stdout.log` | sortie du process (traceback en cas d'échec) |
| `checkpoints/index.json` | checkpoints (tags `last`, `best:…`, `phase_end:…`) |
| `notes.md` | hypothèse (pré-remplie) + conclusion |
| `studies/<id>/study.json`, `trials.jsonl` | étude : objectifs, budget, `user`, `n_complete/n_pruned/n_failed`, `best` (essais COMPLETE seulement) ; essais `{number, state, params, values, intermediate, constraints, feasible, run_id}` (une ligne RUNNING puis une ligne finale par essai : `StudyReader.trials()` garde la dernière) |
| `.wmlab/hosts.yaml`, `.wmlab/hosts_inventory.json` | machines (prix, `decommissioned_at`, `scaleway`) et dernier **inventaire** par machine : runs présents, `unretrieved_checkpoints`, `unretrieved_bytes`, `unindexed_runs`, `active_jobs` (API : `GET /api/hosts/{name}/inventory`) |

Lecture programmatique (sous-échantillonnage, statut dérivé, coût) :

```python
from pathlib import Path
from wmlab.paths import runs_dir, studies_dir
from wmlab.tracking import RunReader, StudyReader

r = RunReader(runs_dir() / "<run_id>")
m = r.manifest()                                   # + status dérivé, duration_s, cost_eur
series = r.metrics(["train/bit_acc", "val/bit_acc_mean", "val/psnr"], max_points=200)
sysm = r.system(max_points=200)                     # {"t": [...], "series": {"gpu_util": [...]}}
evals = r.evals(); events = r.events(); ckpts = r.checkpoints()
trials = StudyReader(studies_dir() / "<study_id>").trials()
```

Clés normalisées : `train/loss`, `train/loss/<terme>`, `train/bit_acc`, `train/lr`,
`train/weight/<terme>`, `train/attack_strength`, `train/step_time_s`,
`train/data_wait_frac`, `train/items_per_s` ; `val/loss`, `val/<qualité>` (`psnr`, `ssim`,
`snr`…), `val/bit_acc` (sans attaque), `val/bit_acc/<label>`, `val/bit_acc_mean`.

### 3. Diagnostiquer

Toujours lire les courbes **par phase** (colonne `phase`, événements `phase_start`) : une
baisse de bit accuracy quand la force d'attaque monte est normale. Passer en revue, dans
l'ordre, les diagnostics de `references/diagnostics.md` :

1. le run a-t-il fini ? (`status`, `error`, fin de `stdout.log`) ;
2. décodeur bloqué à 0,5 ;
3. imperceptibilité qui s'effondre (PSNR / SNR) ;
4. surapprentissage aux attaques d'entraînement (écart validation / benchmark) ;
5. GPU sous-utilisé ou goulot de données (`system.jsonl`, `train/data_wait_frac`) — sur
   Mac/MPS, pas de `gpu_util` : juger sur `train/items_per_s`, `train/step_time_s` et
   `train/data_wait_frac` seulement, et le dire ;
6. LR trop fort / instabilités (pics de `train/loss`, NaN) ;
7. comparaison à la référence (baseline du projet, runs parents, meilleur du classement) ;
8. **checkpoints non rapatriés** (run distant) : `Job.retrieval.status` ≠ `complete`
   (`missing` liste les fichiers), `Checkpoint.available_locally: false` dans
   `checkpoints/index.json` vu du serveur, ou inventaire de la machine avec
   `unretrieved_checkpoints > 0` — les poids analysés/évalués ne sont alors pas sur le
   serveur : proposer « Rapatrier les checkpoints » avant toute évaluation ou branche
   `init_from`, et **avant toute proposition de retirer la machine**.

Chaque affirmation s'appuie sur des chiffres (step, valeur, phase). Distinguer le **constat**
de l'**interprétation** ; signaler le bruit (un seul seed ⇒ prudence).

### 4. Rédiger la conclusion dans `notes.md` du run

Le `notes.md` d'un run est pré-rempli (`wmlab.tracking.default_notes`) avec `## Hypothèse`
(depuis la spec) puis deux sections **vides** `## Observations` et `## Conclusion`.
Conserver l'en-tête (hypothèse), **remplir** `## Observations` (puces factuelles) et
**renommer** `## Conclusion` en section datée — ne pas ajouter un second titre du même nom :

```markdown
## Observations

- <puces factuelles : step / phase / valeur>

## Conclusion — 2026-10-12

**Hypothèse** : <rappel> → **confirmée / infirmée / non concluante**.

**Résultats** (checkpoint `last`, benchmark image_v1) : PSNR 38,4 dB · bit_acc_mean 0,912 ·
TPR@1e-6 0,85 · pire attaque : rotate_10 (0,71).

**Diagnostic** : <cause probable, niveau de confiance>.

**Suite proposée** : <prochain run / étude, coût estimé>.
```

Écrire avec `RunReader(run_dir).set_notes(texte)` (écriture atomique) ou via l'app (onglet
Notes du run, qui passe par l'API). Run **distant** : dès qu'un `notes.md` local existe, la
synchronisation l'exclut du rsync (`--exclude notes.md`) — les notes écrites en local ne sont
pas écrasées, même si le run tourne encore ; attendre tout de même la fin du run pour conclure.
Ne jamais modifier les autres fichiers d'un run ni supprimer de run. Mettre aussi une ligne dans le journal du
projet (`projects/<p>/notes.md`) si la conclusion change la direction du projet.

### 5. Proposer le prochain plan (avec coût)

Proposer 1 à 3 options concrètes, classées, chacune avec hypothèse, artefact et coût :

| Situation | Proposition |
|---|---|
| réglages incertains (LR, poids) | étude HPO basse fidélité (skill `wmlab-experiment`, `.claude/skills/wmlab-experiment/references/hpo-guide.md`) |
| plateau prometteur, fin décevante | **branche** `init_from` depuis `phase_end:<phase>` avec une autre décroissance / affinage |
| une attaque faible au benchmark | nouvelle phase ou poids `p` ↑ pour cette attaque ; gel de l'embedder pour ne pas perdre en qualité |
| gain suspect / effet de plusieurs changements | **ablation** : une expérience par changement retiré, même seed puis 2–3 seeds |
| résultats bons en synthétique | passer aux données réelles (`@coco2017`, `@librispeech_clean100`, `@sav`), puis benchmark v2 |

**Estimation du coût** : s/step mesuré sur un run comparable **sur la même machine**
(`duration_s / progress.step`, ou moyenne de `train/step_time_s`) × steps prévus × prix €/h
de la machine (`.wmlab/hosts.yaml`). Études : steps **par essai** = ceux de l'expérience de
base après `fidelity` (`steps_scale` **et** `fidelity.overrides`, qui peuvent rallonger une
phase — ex. `lr_weights` : 900 steps/essai, pas 1 500 × 0,5) × `max_trials` ÷ `parallel`,
plafonnés par `max_hours` / `max_eur` ; calcul exact (celui de `/api/jobs/estimate`) :

```python
from pathlib import Path
from wmlab.curriculum import PhasePlan
from wmlab.server.catalog import fidelity_experiment
from wmlab.spec import load_yaml
plan = Path("projects/image-hidden/hpo/lr_weights.yaml"); hpo = load_yaml(plan, "hpo")
steps_par_essai = PhasePlan(fidelity_experiment(plan, hpo)).total_steps
print(steps_par_essai, hpo.budget.max_trials, hpo.budget.parallel, hpo.budget.max_hours, hpo.budget.max_eur)
```

**Repli quand rien n'est comparable** (workspace neuf : `hosts.yaml` ne contient que `local`
à 0 €/h, aucun run GPU ; l'app répond alors « Aucun run … comparable : estimation
impossible ») : 1) faire déclarer la machine GPU avec son prix réel (page Machines ; ordre de
grandeur : L40S ≈ 1,4 €/h, prix des autres GPU à lire sur la console Scaleway) ; 2) proposer
d'abord un **run d'étalonnage** `--max-hours 0.25` de la même expérience sur cette machine
(coût ≤ 0,25 × prix), qui mesure le s/step réel et débloque l'estimation de l'app (un run
compte dès 50 steps et 60 s ; les `--smoke` et les essais HPO ne comptent pas) ; 3) donner
en attendant une fourchette explicite (ex. « 0,05–0,15 s/step à 128 px sur L40S ») en la
signalant comme non mesurée.
**Budget** : total dans `workspace.yaml` (`budget.total_eur`), dépensé = somme des
`cost_eur` des runs hors essais + études + évaluations + entrées manuelles (page
« Budget » de l'app, ou `GET /api/costs`). Script rapide :

```python
from wmlab.paths import load_workspace_settings, runs_dir, studies_dir
from wmlab.tracking import RunReader, StudyReader, list_runs, list_studies

runs = [RunReader(p).manifest() for p in list_runs(runs_dir())]
spent = sum(m["cost_eur"] for m in runs if not m.get("study"))
spent += sum(StudyReader(p).manifest()["cost_eur"] for p in list_studies(studies_dir()))
settings = load_workspace_settings()
spent += sum(e.eur for e in settings.budget.manual_entries)
print(f"dépensé ≈ {spent:.2f} € / {settings.budget.total_eur:.0f} €")
```

Toujours rappeler l'ordre prudent : `--smoke` → local → run court sur GPU (`max_hours`) →
run complet ; et donner la part du budget restant que représente la proposition.

**Machines GPU** : avant de proposer de retirer une machine (ou de supprimer l'instance
Scaleway) pour arrêter les frais, **vérifier son inventaire** (page Machines ou
`GET /api/hosts/{name}/inventory` ; cache `.wmlab/hosts_inventory.json`) :
`unretrieved_checkpoints = 0`, `unindexed_runs = 0`, `active_jobs = 0`. Sinon proposer
d'abord « Rapatrier les checkpoints » (job `retrieve`) et le dire chiffré (« 6 checkpoints,
2,3 Go non rapatriés »). Le retrait est refusé par le serveur tant que ce n'est pas le cas :
ne pas suggérer `force` sauf machine déjà détruite. Le coût d'une machine court tant que
l'instance existe : une machine sans job depuis longtemps est une dépense à signaler.

### 6. Répondre à l'utilisateur

En français, concis : verdict en une phrase, 3–5 chiffres clés, diagnostic (dont
l'état du rapatriement si le run est distant), proposition(s) avec coût, et où la conclusion
a été écrite. En équipe, nommer l'auteur (`user`) et le commit (`env/git.json.sha`) des runs
comparés. Proposer de créer l'expérience / le plan
suivant (skill `wmlab-experiment`) plutôt que de le faire sans accord si le coût n'est pas
négligeable.

## Références

- `references/diagnostics.md` — symptômes, vérifications chiffrées et remèdes typiques du
  watermarking (image / audio / vidéo), lecture d'une étude HPO.
