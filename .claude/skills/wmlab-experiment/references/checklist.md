# Liste de contrôle avant de rendre la main

## Plugin (`lab/<kind>s/<nom>.py`)

- [ ] Nom de registre unique (`wmlab registry --kind <kind>`), minuscules, identifiant Python.
- [ ] `modality` déclarée (et attribut de classe `modality` pour modèles / attaques).
- [ ] Params via un modèle pydantic `extra="forbid"`, défauts = config recommandée, bornes
      (`Field(ge=…, le=…)`), descriptions en français.
- [ ] 1er paragraphe du docstring de la classe = une phrase en français (c'est tout le
      paragraphe qui est affiché dans la bibliothèque) ; détails techniques après une ligne vide.
- [ ] Modèle : `nbits` en 1er argument de `__init__`, jamais dans les params ; `embed` même
      forme / plage (clamp) ; `extract` → logits `[B, nbits]` ; pooling global si possible.
- [ ] Attaque : `strength <= 0` ⇒ `return x` ; sévérité interpolée ; forme conservée ;
      plage respectée ; `differentiable = False` si PIL / PyAV / numpy / arrondi.
- [ ] Dataset : `resolve_path` pour les `@…` ; ordre trié ; `id` stable ; items
      `{"x", "id"}` sans dim batch ; float32 dans la bonne plage ; erreurs claires.
- [ ] Prétraitement : attribut `deterministic` ; pas de dim batch.
- [ ] Perte : scalaire ; aucun poids codé en dur.
- [ ] Métrique : `(x, x_w, **ctx) -> Tensor[B]`.
- [ ] `wmlab plugin-test <kind> <nom>` : tout vert (et `--modality` pour chaque modalité
      supportée).
- [ ] Logique non triviale : test pytest rapide `tests/test_lab_<nom>.py`.

Tester un dataset qui lit un dossier (en CLI : `wmlab plugin-test dataset <nom> --param root=<dossier>`, ou en Python) :

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from PIL import Image
import numpy as np
from wmlab.plugin_test import run_plugin_test

root = Path("/tmp/wm_imgs"); root.mkdir(exist_ok=True)
for i in range(4):
    Image.fromarray((np.random.rand(48, 64, 3) * 255).astype("uint8")).save(root / f"{i}.png")
for check in run_plugin_test("dataset", "<nom>", params={"root": str(root)}):
    print("OK " if check["ok"] else "ÉCHEC", check["check"], check["detail"])
PY
```

## Expérience (`projects/<p>/experiments/<nom>.yaml`)

- [ ] `name` = nom du fichier ; `description` et **`hypothesis`** remplies (`--from` les
      remet à « À compléter » et vide `tags` : les remplir, et retaguer si utile).
- [ ] Une seule variable changée par rapport à la référence (sinon, le dire).
- [ ] Données : `synthetic` ou `@dataset` du catalogue ; seeds train ≠ val ; pas d'aléa en
      validation ; aucun chemin absolu.
- [ ] Phases : noms uniques ; warmup sans attaque ; termes de `loss_weights` définis dans
      `loss.terms` ; attaques compatibles avec la modalité.
- [ ] `eval.val_attacks` non vide (sinon pas de `val/bit_acc_mean`) ; `keep_best` sur des
      clés `val/...` ; `final_benchmark` de la bonne modalité.
- [ ] `init_from` : même modèle et même `message_bits` que le run parent ; checkpoint et
      phase existants ; `resume: true` (reprise au step du checkpoint, optimiseur compris)
      exclut `start_phase`.
- [ ] `wmlab validate <fichier>` puis `wmlab train <fichier> --smoke` : OK.

## Plan HPO (`projects/<p>/hpo/<nom>.yaml`)

- [ ] Objectifs et contraintes sur des clés `val/...` produites par l'expérience.
- [ ] Nuisances incluses (LR au minimum) ; ≤ 4–5 dimensions ; `log: true` pour les échelles.
- [ ] Chemins de `space` qui ont un effet (poids des **phases**, pas `loss.terms` s'ils
      sont écrasés).
- [ ] `fidelity` (steps_scale, pas de benchmark par essai) ; `budget` avec `max_hours` et
      `max_eur` si hôte payant.
- [ ] `wmlab validate <plan>` puis `wmlab hpo <plan> --smoke` : OK.

## Benchmark (`benchmarks/<modalité>_v<N>.yaml`)

- [ ] Nouveau fichier (jamais modifier une version existante), `version` incrémentée.
- [ ] Données déterministes (`seed`, `limit`, prétraitements déterministes).
- [ ] `identity` (label `none`) + attaques à labels explicites et uniques.
- [ ] `quality_metrics` compatibles ; `fpr` standard `[1e-2, 1e-4, 1e-6]`.
- [ ] `wmlab validate benchmarks/<nom>.yaml` ; `wmlab eval --run <run> --checkpoint last
      --benchmark <nom> --smoke` sur un run existant.

## Catalogue de dataset (`datasets/<nom>.yaml`)

- [ ] `source` = page officielle ; `license` vérifiée (sinon « à vérifier ») ;
      `paths.default` sous `~/datasets/…` ; exemple d'hôte Scaleway en commentaire.

## Git (mode équipe)

- [ ] Fichiers de l'expérience commités (`git status` propre pour ces fichiers) et
      **poussés** (`git push`) : le serveur exécute exactement le commit indiqué.
- [ ] Sha ou branche à saisir dans « Révision git » communiqué à l'utilisateur.

## Réponse à l'utilisateur

- [ ] Fichiers créés / modifiés, commandes de validation lancées et résultat.
- [ ] Hypothèse et ce qu'il faudra regarder (courbes, métriques).
- [ ] Coût estimé ; comment lancer (app : « Nouveau lancement », local d'abord puis
      Scaleway avec durée max et révision git) ; rappel du rapatriement avant retrait
      de la machine.
