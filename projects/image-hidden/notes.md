# Image — HiDDeN baseline

Journal du projet : objectifs, décisions, pistes. Les conclusions détaillées de chaque run
vont dans le `notes.md` du run (onglet « Notes » du run dans l'app) ; ici, on garde la vue
d'ensemble.

## Objectif

Valider le pipeline complet (curriculum → HPO → benchmark figé → classement) sur un schéma
image simple, puis servir de **référence** pour toute nouvelle architecture image.

- Message : 48 bits.
- Qualité : PSNR ≥ 38 dB (contrainte du projet).
- Robustesse : `bit_acc_mean` sur `image_v1` (moyenne des attaques hors identité).

## Contenu

| Fichier | Rôle |
|---|---|
| `experiments/baseline.yaml` | HiDDeN + curriculum 3 phases, 64 px, 1 500 steps (quelques minutes sur le Mac) |
| `experiments/resolution_curriculum.yaml` | 64 px puis 128 px (surcharge `data` par phase) |
| `experiments/wsd_branch.yaml` | LR warmup → plateau → décroissance, décroissance branchable (`init_from`) |
| `hpo/lr_weights.yaml` | TPE + ASHA, mono-objectif `val/bit_acc_mean` sous contrainte `val/psnr` |
| `hpo/pareto.yaml` | NSGA-II, front de Pareto `val/psnr` × `val/bit_acc_mean` |

## Plan de travail proposé

1. `wmlab train …/baseline.yaml --smoke` puis la baseline complète **en local** (~3 min sur
   Mac M4) : vérifier que le décodeur décolle de 0,5 pendant `warmup_decode` et que le PSNR
   remonte pendant la rampe. Repère mesuré : fin de warmup `val/bit_acc` ~0,94 (PSNR ~10 dB,
   normal : aucune contrainte de qualité) ; fin de run PSNR ~34 dB, `val/bit_acc_mean` ~0,71.
2. `hpo/lr_weights.yaml` en local (≈ 24 essais à 25 % des steps).
3. Passer aux données réelles : `@coco2017` (voir le commentaire dans `baseline.yaml`),
   128–256 px, ×10 à ×50 steps, sur un GPU Scaleway ; commencer par un run court
   (`--max-hours 1`) pour mesurer le débit réel et estimer le coût.
4. Créer `benchmarks/image_v2.yaml` sur DIV2K (`@div2k_valid`) une fois les données en place ;
   ne jamais modifier `image_v1`.
5. Explorer le front de Pareto (`hpo/pareto.yaml`) sur le meilleur réglage.

## Passer à l'échelle (GPU)

- `batch_size` 32–64, `num_workers` 8, images 128–256 px ;
- `precision: auto` active bf16 sur CUDA ;
- comptez ~10–20 % des steps pour la décroissance finale (voir `wsd_branch.yaml`) ;
- toujours : smoke → run court → run complet.

## Calibrage des poids (mesuré sur le Mac, 1 500 steps)

| Affinage `img` (rampe → 100) | PSNR final | `val/bit_acc_mean` |
|---|---|---|
| 2 (rampe → 1) | ~14 dB | 0,92 |
| 20 (rampe → 10) | ~22 dB | 0,84 |
| 200 | ~32 dB | 0,74 |
| **300 (baseline)** | **~34 dB** | **0,71** |
| 500 | ~36 dB | 0,69 |
| 1 000 (rampe → 300) | ~42 dB | 0,62 |

La MSE sur [0, 1] est minuscule face à la BCE : les poids de qualité utiles sont ~100–1 000.
Réduire `strength` (0,1) au lieu d'augmenter les poids bloque le décodeur (~0,55 en fin de
warmup) : mieux vaut laisser le résidu libre au début et contraindre ensuite.

## Journal

- 2026-09-30 — runs de démonstration (tag `demo`, Mac M4 / MPS, ~2 min chacun) :
  - `20260930-072458-baseline-92be` (lr 1e-3) : val/bit_acc 0,90 sans attaque, val/psnr
    34,3 dB, val/bit_acc_mean 0,71 ; benchmark `image_v1` (last) : 35,3 dB / 0,75.
  - `20260930-072723-baseline-e9af` (lr 3e-4) : décodeur plus lent (0,79 en fin de warmup),
    PSNR final 37,8 dB mais bit_acc_mean 0,64 : à ce budget de steps, le LR de base 1e-3 est
    meilleur ; 3e-4 « achète » du PSNR en perdant de la robustesse.
  - `20260930-072930-baseline-170a` : branche de 92be depuis `phase_end:robustness_ramp`
    (`start_phase: finetune_imperceptibility`, 300 steps, 26 s d'après `run.json`) : 35,1 dB / 0,70 en validation,
    36,1 dB / 0,74 sur `image_v1` — même compromis que le parent, obtenu 4× plus vite : le
    branchement est le bon outil pour explorer la phase d'affinage.
  - Le sélecteur `best:val/bit_acc_mean` désigne toujours la fin du warmup (résidu libre :
    PSNR ~9,5 dB, bit_acc_mean 0,85) : sur le classement, ces points forment l'extrémité
    « robuste » du front de Pareto, `last` l'extrémité « imperceptible ».
- 2026-09-30 — étude `20260930-075755-lr-weights-3e51` (plan `lr_weights`, 5 essais, fidélité
  0,5 = 900 steps/essai) : 1 essai complet (0,59 à 40 dB), 2 élagués à la fin du warmup
  (essais 1 et 4 : 0,79 et 0,72 au step 300) et 2 essais marqués `PRUNED` **à tort** :
  les essais 2 et 3 (`…-9373`, `…-75ab`) ont été entraînés jusqu'au bout (courbe complète
  jusqu'au step 900, `progress.step` 899/900, « Essai élagué (pruning) au step 900 » dans
  leur `stdout.log`) et élagués à la validation finale par une version du driver antérieure
  à la garde « pas de pruning à la validation finale » (`wmlab/hpo/driver.py`, corrigée
  après l'étude). Leurs valeurs finales, 0,506 (essai 2 : `img` 22 en fin de rampe puis
  989 en affinage) et 0,500 (essai 3 : 264 en fin de rampe, 165 en affinage), sont de vrais
  résultats complets — décodeur effondré, PSNR 51 et 60 dB — simplement absents de
  `study.json.best`. L'étude reste une
  démo : la rejouer avec le driver corrigé avant de s'en servir comme référence.
  Autres écarts de cette étude de démo, datée d'une version antérieure du driver : sa durée
  (`ended_at` 08:11:40Z, « 13 min » dans l'app) est gonflée de 6 min 46 s de latence entre
  l'arrêt du worker (08:04:54Z) et l'écriture du statut `completed` — les 5 essais cumulent
  4,3 min (sur un hôte payant, le coût affiché serait surestimé d'autant) ; et avec
  `n_startup_trials: 8` > 5 essais, TPE n'a jamais été activé (tirage purement aléatoire).
  Deux leçons, reportées dans les plans :
  1. **Ne jamais raccourcir le warmup de décodage** dans le proxy basse fidélité (à 75 steps,
     tous les essais restent à ~0,5). L'élagage à la fin du warmup (ASHA `min_resource: 300`)
     était lui aussi une erreur : au step 300 seul le LR a agi et la métrique est mesurée
     avec un résidu libre (PSNR ~10 dB) — les essais 1 et 4 (0,79 / 0,72) ont été coupés au
     profit de l'essai 0 (0,81 au step 300, 0,59 à la fin). Avec des essais de 900 steps,
     ASHA ne décidait d'ailleurs qu'à ce seul palier (300 × 3 = 900 = validation finale).
     Le plan `lr_weights` utilise désormais le pruner `median` avec `n_warmup_steps: 500`
     (décisions aux validations 600 et 750, là où les quatre paramètres agissent).
  2. À 25 % des steps, la rampe (225 steps) ne remonte pas le PSNR au-dessus de ~17 dB et
     l'affinage à poids fort (≥ 300) effondre le décodeur en 75 steps : la fidélité 0,5 est le
     minimum pour cette baseline. Les poids `img` ≥ 170 en fin de rampe font aussi décrocher le
     décodeur pendant la rampe : borner l'espace à ~150 semble raisonnable pour la suite.
