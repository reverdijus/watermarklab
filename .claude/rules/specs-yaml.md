---
paths:
  - "projects/**"
  - "benchmarks/**"
  - "datasets/**"
  - "workspace.yaml"
---
# Specs YAML (projets, expériences, plans HPO, benchmarks, datasets)

- Référence des champs : `.claude/skills/wmlab-experiment/references/spec-reference.md`
  (`ARCHITECTURE.md` §5) ; champ inconnu = refus (`extra="forbid"`), ne pas inventer de clé.
- `name:` = nom du fichier (expérience, plan, benchmark) ou du dossier (projet). `nbits` jamais
  dans `model.params`. Données via `@<dataset>` du catalogue, jamais de chemin absolu. Toujours
  `description` et `hypothesis`.
- Un `benchmarks/*.yaml` commité est publié, donc immuable : créer `<modalité>_v<N+1>.yaml`
  (le hook de garde refuse l'édition).
- Après chaque édition, `wmlab validate` tourne automatiquement (hook) : corriger avant de
  continuer. Puis `wmlab train <spec> --smoke` (ou `wmlab hpo <plan> --smoke`).
- Mode équipe : un YAML enregistré depuis l'app est commité par le serveur, donc `git pull`
  avant d'éditer le même fichier ; ce qui n'est pas poussé n'est jamais exécuté sur GPU.
- Commentaires YAML en français.
