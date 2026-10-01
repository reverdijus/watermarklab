# Guide HPO pour le watermarking (Watermark Lab + Optuna)

Méthode inspirée du *Deep Learning Tuning Playbook* (Google Research, Godbole et al.,
2023), adaptée au compromis imperceptibilité × robustesse et au budget GPU (40 000 €).

## Sommaire

1. Principes : progresser par rounds, explorer avant d'exploiter
2. Hyperparamètres scientifiques, de nuisance et fixes
3. Quoi optimiser en watermarking (objectifs, contraintes, espace)
4. Samplers et pruners
5. Multi-objectif (front de Pareto)
6. Fidélité (proxy basse fidélité)
7. Budget € et estimation du coût
8. Lire une étude et conclure
9. Exemples de plans

---

## 1. Principes

- **Stratégie incrémentale** : partir d'une configuration simple qui fonctionne (la
  baseline), puis améliorer par **rounds**, chacun avec UN objectif clair (« quel LR pour
  cette architecture ? », « la rampe d'attaques doit-elle être plus longue ? »). On n'adopte
  un changement que s'il est justifié par des preuves.
- **Explorer avant d'exploiter** : la plupart des rounds servent à *comprendre* le problème
  (quels hyperparamètres comptent, interactions, instabilités), pas à maximiser un score.
  L'exploitation (optimisation bayésienne serrée) vient à la fin, sur un espace réduit.
- Chaque round = une étude (`hpo/<plan>.yaml`) avec `description` et **`hypothesis`**.

## 2. Scientifiques, nuisances, fixes

Pour chaque round, classer les hyperparamètres :

| Type | Définition | Traitement |
|---|---|---|
| **scientifique** | celui dont on veut mesurer l'effet (ex. `model.params.channels`, durée du warmup, présence d'une attaque) | valeurs comparées (souvent `categorical`) |
| **nuisance** | doit être ré-optimisé pour que la comparaison soit juste (ex. LR, poids de qualité) | inclus dans `space`, co-optimisé |
| **fixe** | figé pendant ce round (ex. `batch_size`, `message_bits`, données) | hors `space` ; les conclusions ne valent que pour ces valeurs |

Le **LR est presque toujours une nuisance** : comparer deux architectures à LR fixe compare
surtout leur sensibilité au LR. En watermarking, le **poids de qualité** et la **force du
résidu** sont aussi des nuisances dès qu'on compare la robustesse (sinon on compare des
points différents du compromis).

## 3. Quoi optimiser en watermarking

**Objectif.** Le compromis est intrinsèque : maximiser la robustesse seule pousse le modèle à
un résidu visible. Deux formulations :
- **mono-objectif sous contrainte** (recommandé pour régler) :
  `objectives: [{metric: val/bit_acc_mean, direction: maximize}]` +
  `constraints: [{metric: val/psnr, op: ">=", value: 36}]` (audio : `val/snr`) ;
- **multi-objectif** (pour cartographier le compromis) : `val/psnr` × `val/bit_acc_mean`
  (§5).

`val/bit_acc_mean` = moyenne sur `eval.val_attacks` : choisir des attaques de validation
représentatives du benchmark (sinon on optimise la mauvaise chose). La TPR à FPR faible
(`tpr@1e-06`, calculée par le benchmark) est la métrique finale de détection ; la bit
accuracy est un bon proxy pendant l'HPO.

**Espace de recherche typique** (du plus au moins influent) :

| Chemin | Loi | Remarque |
|---|---|---|
| `optimizer.lr` | float log, ~[1e-4, 3e-3] | toujours |
| `phases.<i>.loss_weights.<qualité>` | float log, ex. [30, 1000] pour une MSE | viser les **phases** (elles écrasent `loss.terms.<t>.weight`) |
| `model.params.strength` | float log | amplitude du résidu |
| `phases.<i>.attack_strength.ramp.1` | float [0.5, 1] | force max de la rampe |
| `phases.<i>.steps` | int avec `step` | durée du warmup / de la rampe |
| `phases.<i>.attacks.<j>.p` | float | mélange des attaques |
| `model.params.channels` | categorical | capacité (scientifique en général) |
| `optimizer.weight_decay` | float log, [1e-6, 1e-2] | secondaire |

Garder **≤ 4–5 dimensions** par étude (budget ~8–10 essais par dimension en TPE).
Utiliser `log: true` pour tout ce qui varie en ordre de grandeur. Vérifier après coup que le
meilleur essai n'est **pas au bord** de l'espace ; sinon, élargir et relancer.

## 4. Samplers et pruners

| Sampler | Quand |
|---|---|
| `random` | exploration, référence naïve, espaces avec beaucoup de catégoriels ; parallélisable sans perte |
| `tpe` (défaut) | mono-objectif, espace mixte ; `params: {n_startup_trials: 8, multivariate: true}` |
| `cmaes` | 2–10 dimensions continues, budget ≥ 50 essais |
| `nsga2` | multi-objectif ; `params: {population_size: 12}` ; budget ≥ 3 générations |
| `grid` | balayage exhaustif d'une ou deux variables scientifiques (floats avec `step`) |

| Pruner | Quand |
|---|---|
| `asha` (défaut) | mono-objectif, essais **longs** ; `params: {min_resource: <steps>, reduction_factor: 3}` ; `min_resource` ≥ fin du warmup (sinon on coupe des essais qui n'ont pas encore commencé à apprendre). ASHA ne décide qu'aux **paliers** `min_resource × reduction_factor^k` (validations à ces steps exactement, ou la première après) et jamais à la validation finale : avec des essais courts (ex. 900 steps, `min_resource: 300`) il n'y a qu'une décision, au step 300 — avant que les paramètres des phases suivantes aient agi |
| `hyperband` | idem, plus robuste au choix de `min_resource` (plusieurs brackets) |
| `median` | essais courts ou peu nombreux ; `params: {n_startup_trials: <k>, n_warmup_steps: <step>}` : décide à **chaque** validation après `n_warmup_steps` (à placer après la phase où les paramètres cherchés agissent) — c'est le choix des plans de démo |
| `none` | multi-objectif (automatique), ou courbes non monotones (curriculum agressif) |

Attention : avec un curriculum, les métriques **baissent** normalement quand la force
d'attaque monte. Le pruning compare les essais à steps égaux, donc reste juste tant que
tous les essais ont le même curriculum ; mais si `space` modifie la durée des phases, la
comparaison à steps égaux devient biaisée → `pruner: {name: none}` ou `hyperband`.

## 5. Multi-objectif

Deux objectifs ou plus ⇒ l'étude calcule le **front de Pareto** (essais non dominés),
affiché dans l'app (détail de l'étude). Pas de pruning. On choisit ensuite le point du front
qui respecte la contrainte du projet, puis on le relance en pleine fidélité. Utile pour
comparer deux architectures : superposer leurs fronts (la meilleure domine l'autre).

## 6. Fidélité

`fidelity.steps_scale` multiplie les steps de chaque phase (curriculum conservé en
proportion) ; `fidelity.overrides` s'applique à chaque essai. Recommandations :
- `steps_scale: 0.2–0.3` en exploration ; `0.5` si le compromis dépend de l'affinage final ;
- `steps_scale` réduit les `steps` de chaque phase **et** ses `lr.warmup_steps` dans la même
  proportion (arrondi, ≥ 1 step) ; un override explicite (`fidelity.overrides` ou clé de
  `space`) sur `phases.<i>.steps` / `phases.<i>.lr.warmup_steps` l'emporte — utile pour garder
  un warmup de décodage entier (`phases.0.steps: 300`) ; `wmlab validate` refuse un warmup
  plus long que sa phase, avant et après fidélité ;
- `eval.final_benchmark: null` (pas de benchmark par essai) ; `eval.every_steps` assez
  petit pour le pruner (≥ 3 points par essai) ;
- données plus petites si pertinent (`data.train.params.size`, `data.val` réduit) ;
- **vérifier la corrélation de rang** : relancer les 3 meilleurs essais + 1 médian en pleine
  fidélité ; si l'ordre change, le proxy est trop agressif.

## 7. Budget € et estimation

Coût d'une étude ≈ durée murale × prix de l'hôte ;
durée ≈ `max_trials × durée d'un essai / parallel` (si le GPU n'est pas saturé par un essai).
Durée d'un essai ≈ `steps_scale × total_steps × s/step` (+ validations).

Mesurer `s/step` sur un run court réel (`train/step_time_s` dans `metrics.jsonl`, ou durée
÷ steps d'un run passé : `wmlab runs show <run> --json`). L'app estime aussi le coût dans
« Nouveau lancement » (`POST /api/jobs/estimate`).

Exemple : baseline 20 000 steps à 0,12 s/step sur L40S (1,4 €/h) ; étude de 24 essais à
25 % ⇒ 24 × 5 000 × 0,12 s = 4 h de calcul ; `parallel: 2` ⇒ ~2 h murales ≈ 3 €.

Garde-fous : toujours `max_hours` **et** `max_eur` sur un hôte payant ; `--smoke` avant ;
premier essai réel court pour mesurer le débit. Ordre de grandeur de répartition du budget
global : exploration HPO basse fidélité ≲ 15 %, confirmations multi-seeds ≈ 15 %,
entraînements finaux et ablations le reste. Suivre la page « Budget » de l'app.

## 8. Lire une étude et conclure

- Importances des paramètres (app) : ignorer ceux qui ne comptent pas au round suivant
  (les fixer).
- Graphes valeur × paramètre : un optimum au bord ⇒ élargir l'espace.
- Beaucoup d'essais infaisables (contrainte) ⇒ contrainte trop dure pour la fidélité, ou
  poids de qualité trop bas dans l'espace.
- Bruit : relancer les 2–3 meilleures configs avec 2–3 `seed` différentes avant d'adopter
  un changement ; un écart < 2 écarts-types n'est pas une preuve.
- Écrire la conclusion (skill `wmlab-analyse`) et mettre à jour l'expérience de base avec la
  configuration retenue (nouvelle expérience, pas d'écrasement silencieux).

## 9. Exemples de plans

- `projects/image-hidden/hpo/lr_weights.yaml` — TPE + ASHA, contrainte PSNR, 4 dimensions.
- `projects/image-hidden/hpo/pareto.yaml` — NSGA-II, front PSNR × bit_acc_mean.
- `projects/audio-seal-lite/hpo/lr.yaml` — LR scientifique, nuisances co-optimisées.

Comparer deux architectures (scientifique = modèle) : une étude par architecture avec le
même espace de nuisances et le même budget, puis comparer les meilleurs points (ou les
fronts) relancés en pleine fidélité avec plusieurs seeds.
