---
name: wmlab-experiment
description: >-
  Ajouter, coder ou adapter quelque chose de NOUVEAU dans Watermark Lab (watermarking
  image / audio / vidéo) en respectant ses contrats : nouvelle architecture (modèle
  embed/extract), attaque, dataset, prétraitement, perte, métrique, expérience YAML,
  curriculum par phases (warmup, rampes, WSD, branchement init_from, résolution,
  image → vidéo), plan HPO Optuna ou benchmark figé — puis le valider jusqu'au vert
  (wmlab validate, wmlab plugin-test, wmlab train --smoke). À utiliser dès que l'utilisateur
  veut créer, reproduire un papier, brancher ou modifier un composant ou un plan
  d'entraînement du lab, même sans prononcer le mot « plugin ». Keywords: new watermarking
  model, architecture, embedder, extractor, detector, attack, augmentation, codec, dataset,
  preprocessing, loss, metric, experiment spec, curriculum, phases, LR schedule, WSD,
  fine-tune, init_from, hyperparameter search, HPO, Optuna, sampler, pruner, Pareto,
  benchmark, lab/ plugin, registry,
  pretrained baseline, official weights.
---

# wmlab-experiment — créer des expériences et composants compatibles

Watermark Lab est piloté par des **YAML déclaratifs** (projets, expériences, plans HPO,
benchmarks, datasets) et des **registres** de composants Python (modèles, attaques,
datasets, prétraitements, pertes, métriques). Les composants de l'utilisateur sont des
**plugins** dans `lab/<kind>s/`, découverts automatiquement. Le contrat complet est
`ARCHITECTURE.md` à la racine : il fait foi en cas de doute.

Commande de base (le venv n'est pas forcément activé) :
`.venv/bin/python -m wmlab.cli <commande>` — abrégé `wmlab <commande>` ci-dessous.

## Procédure

### 1. Comprendre la demande

Identifier : la **modalité** (image, audio, vidéo), le **projet** concerné
(`projects/<p>/project.yaml` : `message_bits`, objectifs, benchmark), le ou les **types
d'artefacts** à produire, l'**hypothèse** testée et le **budget** acceptable (local Mac
gratuit mais lent ; GPU Scaleway payant, budget global 40 000 €).

Ne poser que les questions indispensables, regroupées en un seul message, et seulement si la
réponse change ce qu'il faut écrire. Typiquement :
- quel projet / quelle modalité (si ambigu) ;
- données : synthétiques pour commencer, ou un dataset du catalogue (`datasets/*.yaml`) —
  les vraies données sont-elles présentes sur la machine visée ? ;
- pour reproduire un papier : quels éléments sont essentiels (architecture exacte ? pertes ?
  attaques ?) ;
- contraintes de qualité (PSNR / SNR minimal) si elles diffèrent du projet.
Sinon, choisir des valeurs raisonnables, les écrire dans la description et avancer.

### 2. Lire le contrat et les exemples

- `ARCHITECTURE.md` §2 (tenseurs), §3 (registres), §4 (interfaces), §5 (specs YAML),
  §6 (dossier de run) — au minimum les sections liées à la demande.
- `references/interfaces.md` (interfaces + exemples complets) et
  `references/spec-reference.md` (tous les champs YAML et leurs défauts).
- Les exemples existants : `projects/image-hidden/` (baseline, curriculum de résolution,
  WSD + branchement, HPO mono- et multi-objectif), `projects/audio-seal-lite/`,
  `projects/video-framewise/` (+ `notes.md` : image → vidéo), `benchmarks/*.yaml`,
  les plugins déjà présents dans `lab/`, et les builtins dans `wmlab/models`,
  `wmlab/attacks`, `wmlab/data`, `wmlab/losses`, `wmlab/metrics`.
- Ce qui existe déjà : `wmlab registry --json` (ou `--kind attack`, etc.). **Réutiliser**
  un composant existant (éventuellement avec d'autres params) plutôt que d'en recoder un.
- Les `lab/*/example_*.py` livrés (`example_unet` : U-Net résiduel à skips, message au
  goulot, extracteur à pooling global ; `pixelate`) sont de **vrais composants enregistrés**
  (registre, bibliothèque de l'app, testés) qui servent aussi d'exemples commentés. Règle :
  si l'un d'eux répond à la demande, l'utiliser tel quel dans l'expérience (c'est le cas
  « réutiliser ») ; s'il faut le modifier, **copier sous un autre nom** (`lab/<kind>s/<nom>.py`,
  nouveau nom de registre) sans toucher à l'exemple ni ré-enregistrer son nom. Les noms
  `pixelate` et `example_unet` sont donc **déjà pris** : un gabarit ou squelette copié tel
  quel qui les redéclare fait échouer l'import de tout son module (voir Invariants).

### 3. Choisir quoi produire

| Demande | Artefact | Point de départ |
|---|---|---|
| modèle publié avec ses poids (baseline) | `lab/models/baseline_<nom>.py` + expérience `phases: []` | `references/interfaces.md` § Baselines pré-entraînées ; exemples `lab/models/baseline_*.py` ; poids : `wmlab weights` (avec accord) |
| nouvelle architecture | `lab/models/<nom>.py` | `wmlab new plugin model <nom> --modality M` ou `templates/model.py` (nom de registre `unet_wm` à renommer) |
| nouvelle attaque / augmentation | `lab/attacks/<nom>.py` | `wmlab new plugin attack <nom> --modality M` ou `templates/attack.py` (`gamma` + `webp`) |
| nouvelle source de données | `lab/datasets/<nom>.py` + `datasets/<nom>.yaml` (catalogue) | `wmlab new plugin dataset <nom> --modality M` ou `templates/dataset.py` |
| prétraitement | `lab/preprocess/<nom>.py` | `wmlab new plugin preprocess <nom> --modality M` |
| perte | `lab/losses/<nom>.py` | `wmlab new plugin loss <nom> --modality M` ou `templates/loss.py` |
| métrique de qualité | `lab/metrics/<nom>.py` | `wmlab new plugin metric <nom> --modality M` |
| variante d'entraînement / curriculum | `projects/<p>/experiments/<nom>.yaml` | `wmlab new experiment <p> <nom> --from baseline` ; motifs : `references/curriculum-patterns.md` |
| recherche d'hyperparamètres | `projects/<p>/hpo/<nom>.yaml` | `wmlab new hpo <p> <nom> --experiment <exp>` ; méthode : `references/hpo-guide.md` |
| nouvelle suite d'évaluation | `benchmarks/<modalité>_v<N>.yaml` | `templates/benchmark.yaml` |
| nouveau schéma complet | `wmlab new project <nom> --modality M --bits N` | puis expériences et plugins |

`--modality` est **obligatoire** pour `wmlab new plugin` (sans lui : « Missing option
'--modality' ») : `image`, `audio` ou `video`, ou une liste `image,video` ; `all` n'est accepté
que pour `preprocess`, `loss` et `metric` (un modèle, une attaque ou un dataset exige des
modalités précises).

Un nouveau composant s'accompagne presque toujours d'une **expérience** qui l'utilise
(copie de la baseline du projet avec le seul changement testé : une variable à la fois).

### 4. Implémenter

Respecter les invariants ci-dessous. Garder le code simple et lisible : annotations de type,
docstring courte, params exposés via un modèle pydantic (`model_config = ConfigDict(extra="forbid")`)
dont les défauts = la configuration recommandée. YAML abondamment commentés en français.
**Description affichée** dans la bibliothèque de l'app et `wmlab registry` = le **1er
paragraphe entier** du docstring de la classe (jusqu'à la première ligne vide, lignes
recollées ; `wmlab.registry.first_paragraph`), pas seulement sa 1re phrase : écrire ce
paragraphe en une phrase, en français, et mettre les détails techniques (en anglais) dans
un 2e paragraphe après une ligne vide.

### 5. Valider en boucle jusqu'au vert

```bash
wmlab validate                                   # tout le workspace (ou un fichier / dossier)
wmlab plugin-test <kind> <nom> [--modality M] [--param k=v]   # formes, plages, strength=0, gradients
wmlab train projects/<p>/experiments/<exp>.yaml --smoke    # ≤ 6 steps/phase, données synthétiques
wmlab hpo projects/<p>/hpo/<plan>.yaml --smoke             # 3 essais en smoke
wmlab hpo projects/<p>/hpo/<plan>.yaml --max-trials 6 --set seed=1   # étude courte (--set : expérience de base)
```

- Corriger CHAQUE erreur ; relancer jusqu'à ce que tout passe. Les messages d'erreur
  listent les noms / paramètres acceptés et proposent des corrections.
- Un plugin qui ne s'importe pas apparaît dans `wmlab validate` et `wmlab registry`
  (« import du plugin impossible ») : lire la trace.
- Dataset lisant un dossier : passer le dossier avec `--param root=<dossier>` (un
  mini-dossier factice suffit, voir `references/checklist.md`).
- Logique non triviale (codec, filtre, alignement) : ajouter un test pytest
  `tests/test_lab_<nom>.py` rapide (CPU, tenseurs minuscules) et lancer
  `.venv/bin/python -m pytest tests/test_lab_<nom>.py -q`.
- `--smoke` vérifie la mécanique, pas la qualité : ne rien conclure de ses métriques.

### 6. Documenter

Dans chaque expérience / plan : `description` (ce qui change par rapport à la référence) et
**`hypothesis`** (résultat attendu, chiffré si possible, et pourquoi). Jamais vide : un run
sans hypothèse ne s'analyse pas. Si pertinent, une ligne dans `projects/<p>/notes.md`.

### 7. Commit + push, puis lancer depuis l'app avec la révision git

En mode équipe (serveur central, `ARCHITECTURE.md` §12, `README.md` §5), **le serveur
exécute exactement le commit choisi** au lancement : il exporte ce sha (`git archive`) vers
la machine GPU. Une modification non commitée ou non poussée n'est **pas** prise en compte,
même si elle est validée localement. Étape finale, une fois tout au vert :

```bash
git status                                        # ne commiter que les fichiers de l'expérience
git add lab/<kind>s/<nom>.py projects/<p>/experiments/<exp>.yaml projects/<p>/notes.md
git commit -m "<projet>: <exp> — <ce qui est testé>"
git push origin <branche>                         # main, ou une branche de travail
git rev-parse --short HEAD                        # sha à indiquer / vérifier dans l'app
```

Puis, dans l'app connectée au serveur : **« Nouveau lancement »** → projet → expérience (ou
plan) → machine → **Révision git** = la branche poussée ou le sha ci-dessus (le champ est
pré-rempli avec `branche @ sha` du serveur ; le serveur tire `main` automatiquement, une autre
branche se lance par son nom) → durée max → estimation € → Lancer. Ne pas commiter sans
l'accord de l'utilisateur si ce n'est pas demandé : dans ce cas, lui donner les commandes.
En mode local (poste seul, `hosted.enabled: false`), le lancement exécute le workspace tel
quel ; un avertissement « arbre sale » apparaît si des fichiers ne sont pas commités.

### 8. Résumer à l'utilisateur

Réponse courte, en français : fichiers créés/modifiés, ce que teste l'expérience, résultat de
la validation (commandes lancées), coût estimé, état git (commité ? poussé ? sha), et
**comment lancer** :
1. dans l'app : bouton **« Nouveau lancement »** → projet → expérience (ou plan HPO) →
   machine **local** d'abord → overrides éventuels → durée max → estimation € → Lancer ;
   suivre le run dans « Runs » (courbes, phases, échantillons) ;
2. une fois le comportement vérifié en local : relancer sur une machine **Scaleway**
   (même dialogue, autre machine, **révision git** = commit poussé), avec une durée max
   (`max_hours`) — d'abord un run court pour mesurer le débit, puis le run complet ; rappeler
   que la machine devra être **rapatriée puis retirée** en fin de campagne ;
3. en ligne de commande : `wmlab train <spec>` / `wmlab hpo <plan>`.

## Invariants (à ne jamais violer)

- **`nbits` n'est jamais dans `model.params`** : il vient de `project.yaml: message_bits`
  et le trainer le passe au constructeur (`MODELS.build(name, nbits=…, **params)`).
- **Formes et plages (§2)** : image `[B,3,H,W]` dans [0,1] ; audio `[B,1,T]` dans [-1,1]
  (mono, `sample_rate` 16 kHz par défaut) ; vidéo `[B,F,3,H,W]` dans [0,1]. Message
  `[B,nbits]` dans {0,1} ; logits `[B,nbits]`, `> 0` ⇒ bit 1. Item de dataset :
  `{"x": tenseur SANS dim batch, "id": str}`.
- `embed(x, msg)` renvoie un tenseur de **même forme et plage** que `x` (clamper) ;
  `forward = embed` ; `extract` renvoie des **logits** (pas de sigmoïde).
- **Attaques** : `forward(x, strength)` avec `strength ∈ [0,1]` ; **`strength = 0` ⇒
  identité exacte** (retourner `x`) ; la sévérité est interpolée par `strength` (les params
  décrivent la sévérité à strength = 1) ; **ne jamais changer la forme** (rogner /
  redimensionner puis revenir à la taille d'origine) ; rester dans la plage de la modalité.
- Attaque **non différentiable** (codec réel, PIL, PyAV, numpy) ⇒ `differentiable = False` :
  le trainer applique automatiquement un straight-through estimator (`apply_attack`).
  Ne pas coder de STE soi-même ; travailler sur `x.detach().cpu()` puis revenir au
  `device`/`dtype` d'origine.
- **Noms uniques** dans chaque registre (`wmlab registry` pour vérifier) ; nom de plugin =
  identifiant Python en minuscules ; `name:` d'une expérience / d'un plan / d'un benchmark
  = nom du fichier ; `name:` d'un projet = nom du dossier. Surcharger un builtin exige
  `override=True` (à éviter). Les exemples livrés occupent déjà `pixelate`
  (`lab/attacks/example_pixelate.py`) et `example_unet` (`lab/models/example_residual_unet.py`) ;
  les gabarits déclarent `gamma`, `webp` (`templates/attack.py`) et `unet_wm`
  (`templates/model.py`) : **renommer avant de copier** un gabarit ou un squelette, sinon
  `ValueError: … déjà enregistré(e)` et **tout le module** (toutes ses classes) est ignoré —
  l'erreur n'apparaît que dans `wmlab registry` / `wmlab validate` sans argument, pas dans
  `wmlab validate <fichier>`.
- **Benchmarks immuables** : ne jamais modifier un `benchmarks/*.yaml` publié (les
  résultats deviendraient incomparables) ; créer `<modalité>_v<N+1>.yaml`.
- **Jamais de chemin absolu** dans les specs : référencer les données par `@<dataset>`
  (catalogue `datasets/<dataset>.yaml`, chemin résolu selon l'hôte). Nouveau dataset réel ⇒
  nouvelle entrée de catalogue (source, licence vérifiée, `paths.default` sous
  `~/datasets/…`).
- **Champs inconnus interdits** dans tous les YAML (`extra="forbid"`) : se référer à
  `references/spec-reference.md`, ne pas inventer de clés.
- **Seeds** : `seed` de l'expérience ; seeds différentes entre `data.train` et `data.val` ;
  pas d'aléa dans les prétraitements de validation ni dans un dataset de benchmark ;
  un composant aléatoire utilise `torch` (générateur global ensemencé) plutôt que `random`.
- **Coût GPU** : toujours `--smoke` → run local court → run court sur GPU (`max_hours`)
  → run complet. Pour l'HPO : proxy basse fidélité (`fidelity.steps_scale`, pas de
  benchmark par essai) et budget explicite (`max_trials`, `max_hours`, `max_eur`).
- **Toujours une hypothèse** (voir étape 6). Une seule variable changée par expérience
  quand on compare à une référence.
- **Ce qui n'est pas poussé n'existe pas pour le serveur** (mode équipe) : commit + push
  avant tout lancement sur une machine GPU, et indiquer la révision git dans l'app (étape 7).
- Ne jamais supprimer de runs ni modifier un dossier de run (`.wmlab/runs/…`) à la main.

## Pièges fréquents

- Modifier `loss.terms.<t>.weight` alors que chaque phase fixe `loss_weights.<t>` : sans
  effet (les phases gagnent). Viser `phases.<i>.loss_weights.<t>` dans l'HPO.
- `keep_best` et les objectifs HPO portent sur des clés `val/...` (`val/psnr`,
  `val/snr`, `val/bit_acc_mean`) ; `val/bit_acc_mean` n'existe que si `eval.val_attacks`
  est non vide.
- Sélecteurs de checkpoint : `last`, `best:<métrique de keep_best>`,
  `phase_end:<phase>` (exige `at_phase_end: true`), ou un nom exact `step_000500`.
- `init_from` exige le **même modèle** (nom + params de forme) et le **même `message_bits`**.
- Un décodeur à pooling global accepte toutes les tailles ; sinon, le curriculum de
  résolution et le benchmark à une autre taille casseront.
- Les attaques réelles (codecs) sont lentes et CPU : les réserver à la fin du curriculum et
  à la validation ; utiliser `jpeg_diff` / des approximations différentiables pendant la
  rampe.
- Vidéo : un prétraitement aléatoire image appliqué frame par frame peut casser la
  cohérence temporelle ; préférer des tirages par clip.

## Références

- `references/interfaces.md` — interfaces §4 avec exemples complets (modèle image / audio /
  vidéo, attaques, dataset, prétraitement, perte, métrique).
- `references/spec-reference.md` — tous les champs YAML avec défauts et contraintes.
- `references/curriculum-patterns.md` — motifs de curriculum prêts à l'emploi.
- `references/hpo-guide.md` — méthodologie HPO (Deep Learning Tuning Playbook) appliquée
  au watermarking, samplers, pruners, multi-objectif, fidélité, budget €.
- `references/checklist.md` — liste de contrôle avant de rendre la main.
- `templates/` — gabarits commentés et conformes : `model.py`, `attack.py`, `dataset.py`,
  `loss.py`, `experiment.yaml`, `hpo.yaml`, `benchmark.yaml`.
