# CLAUDE.md — Watermark Lab

App de bureau (Electron + React) + backend / moteur Python (`wmlab`) pour entraîner, évaluer
et suivre des schémas de watermarking image / audio / vidéo, en local (Mac, MPS) ou sur GPU
Scaleway (SSH). L'utilisateur est francophone.

## À lire d'abord

- **`ARCHITECTURE.md` est la source de vérité** (noms, signatures, formats de fichiers,
  routes, types). Tout changement de contrat s'y fait d'abord, puis dans le code.
- `README.md` : installation, concepts, CLI, machines Scaleway, travail en équipe (§5).
- `deploy/README.md` : serveur central (mode hébergé, `ARCHITECTURE.md` §12) : installation,
  jetons, machines GPU, sauvegardes, mise à jour, dépannage.

## Conventions

- Langue : UI, docs utilisateur, messages d'erreur et commentaires YAML en **français** ;
  code, identifiants, clés de métriques, docstrings et commentaires de code en **anglais**
  (exception : le 1er paragraphe du docstring d'un composant enregistré — c'est lui, en
  entier, qui est affiché dans l'app et `wmlab registry` — en français et en une phrase ;
  détails techniques dans un 2e paragraphe).
- Python : annotations de type, docstrings courtes, pydantic `extra="forbid"`, pas de
  sur-ingénierie, erreurs utilisateur explicites en français.
- Composants utilisateur = plugins dans `lab/<kind>s/` (registres `wmlab.registry`) ;
  expériences / plans / benchmarks / datasets = YAML (`projects/`, `benchmarks/`,
  `datasets/`).
- Toujours via le venv : `.venv/bin/python …` (le venv peut ne pas être activé).

## Commandes

```bash
.venv/bin/python -m pytest -q                                   # tests (rapides, CPU)
.venv/bin/python -m wmlab.cli validate                          # tous les YAML + plugins
.venv/bin/python -m wmlab.cli train <spec> --smoke              # essai éclair d'une expérience
.venv/bin/python -m wmlab.cli hpo <plan> --smoke                # essai éclair d'un plan HPO
.venv/bin/python -m wmlab.cli plugin-test <kind> <name> [--param k=v]   # conformité d'un plugin
.venv/bin/python -m wmlab.cli registry [--kind K]               # composants disponibles
.venv/bin/python -m wmlab.cli weights list                      # poids officiels des baselines (licence, présence)
.venv/bin/python -m wmlab.cli weights pull NAME...              # télécharge des poids (avec l'accord de l'utilisateur)
cd desktop && npm run build                                     # typecheck + build du frontend
cd desktop && npm run dev                                       # app en mode développement
bash deploy/tests_dry_run.sh                                    # scripts de déploiement (dry-run, sans root)
# Sur le serveur central uniquement (mode équipe) :
.venv/bin/python -m wmlab.cli users add NAME [--role admin|member]   # jeton affiché UNE fois
.venv/bin/python -m wmlab.cli users list [--json]                    # « admin » : nom réservé au jeton serveur
.venv/bin/python -m wmlab.cli users revoke NAME
```

## Travail en équipe (serveur central, `ARCHITECTURE.md` §12)

- Flux : **éditer localement** (poste du membre, mode local) → `wmlab validate` et
  `wmlab train <spec> --smoke` (ou `hpo --smoke`) → **commit + push** → lancer **depuis
  l'app connectée au serveur** en indiquant la **révision git** (branche poussée ou sha).
  Le serveur exporte exactement ce commit vers la machine GPU : une modification non
  commitée ou non poussée n'est **jamais** exécutée — le dire explicitement à l'utilisateur
  quand on lui remet une expérience à lancer.
- **Ne jamais lancer (ni faire lancer) une machine GPU sans plan de rapatriement** : qui
  rapatrie, quand, et vérification de l'inventaire de la machine (page Machines :
  checkpoints non rapatriés = 0, aucun job actif) avant de proposer « Retirer la machine »
  ou la suppression de l'instance. Un checkpoint perdu = budget GPU perdu.
- Les YAML enregistrés depuis l'app en mode hébergé sont commités/poussés par le serveur
  (`app(<user>): <chemin>`) : après un tel enregistrement, faire `git pull` localement
  avant d'éditer le même fichier.
- Le serveur ne relit le package `wmlab/` et les plugins `lab/` qu'au redémarrage
  (`deploy/README.md` §9) ; les machines GPU exécutent toujours le commit choisi.
- `deploy/` : `install_server.sh` (idempotent, `--dry-run`), `wmlab-server.service`,
  `Caddyfile`, `backup.sh` + timer, `patch_workspace.py`, `tests_dry_run.sh`. Aucun secret
  dans le dépôt : jetons dans `.wmlab/users.yaml` (hachés) et `/etc/wmlab/env` sur le
  serveur ; `SCW_SECRET_KEY` jamais dans `hosts.yaml`.

## Règles

- **Ne jamais modifier un benchmark publié** (`benchmarks/*.yaml`) : créer `<modalité>_v<N+1>.yaml`.
- **Ne jamais supprimer de runs ni d'études**, ni modifier à la main un dossier sous
  `.wmlab/` (source de vérité des résultats et des coûts). Seul `notes.md` d'un run peut
  être complété (conclusions).
- `nbits` jamais dans `model.params` (vient de `project.yaml: message_bits`) ; attaques :
  `strength = 0` ⇒ identité, forme conservée ; pas de chemin absolu dans les specs
  (`@<dataset>` du catalogue).
- Coût GPU : `--smoke` → local → run court sur GPU (`--max-hours`) → run complet ; toujours
  une `hypothesis` dans une nouvelle expérience ou un nouveau plan.
- Disque limité : ne pas télécharger de datasets ni de poids sans accord ; dépendances via
  `pyproject.toml` (extras `train`, `server`, `quality`, `baselines`, `dev`) et `uv`.
- Baselines pré-entraînées (`lab/models/baseline_*.py`, poids dans `.wmlab/weights/`) : une
  baseline figée s'évalue avec `phases: []` ; seul `pixelseal` est entraînable ; vérifier la
  licence des poids (`wmlab weights list`, SSL = non commercial) avant tout usage produit.
- Valider avant de rendre la main : `validate`, `pytest` pour le code Python, `npm run build`
  pour le desktop, `bash deploy/tests_dry_run.sh` pour `deploy/`.
- En équipe : ce qui n'est pas poussé n'existe pas pour le serveur ; pas de machine GPU
  sans plan de rapatriement (section « Travail en équipe »).

## Skills

- **`wmlab-experiment`** (`.claude/skills/wmlab-experiment/`) : pour créer ou modifier une
  architecture, attaque, dataset, prétraitement, perte, métrique, expérience / curriculum,
  plan HPO ou benchmark. Contient les interfaces, la référence des specs, des motifs de
  curriculum, un guide HPO et des gabarits.
- **`wmlab-analyse`** (`.claude/skills/wmlab-analyse/`) : pour analyser runs et études,
  diagnostiquer (dont l'auteur `user`, l'inventaire des machines et `Job.retrieval`), écrire
  les conclusions dans `notes.md` et proposer la suite avec son coût.
