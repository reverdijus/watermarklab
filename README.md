# Watermark Lab

Application de bureau macOS pour **centraliser, lancer et suivre** les entraînements de
schémas de watermarking **image, audio et vidéo** : entraînement local sur le Mac ou sur des
serveurs GPU Scaleway via SSH, curriculum par phases, plans d'optimisation
d'hyperparamètres (HPO), benchmarks d'attaques figés, tableaux de bord
(front de Pareto imperceptibilité × robustesse) et suivi du budget GPU (40 000 €).

- **Tout est déclaratif** : projets, expériences, plans HPO, benchmarks et datasets sont des
  fichiers YAML validés.
- **Tout est modulaire** : architectures, attaques, datasets, prétraitements, pertes et
  métriques sont des composants enregistrés ; vos propres composants (plugins) vivent dans
  `lab/` et sont découverts automatiquement.
- **Le dossier de run est la source de vérité** : chaque entraînement écrit un dossier
  autonome (métriques, checkpoints, échantillons, évaluations), synchronisé vers le Mac. Un
  run sur un serveur continue même si le Mac est éteint.
- **Le coût est une métrique** : chaque run, étude et évaluation porte son prix.
- **En équipe** : un serveur central héberge le backend et les données, le code vit dans un
  dépôt git, chaque lancement exécute une révision git précise et aucune machine GPU n'est
  retirée avant que ses checkpoints soient rapatriés et vérifiés (section 5).

Le contrat technique complet (formats, API, interfaces) est dans
[`ARCHITECTURE.md`](ARCHITECTURE.md).

---

## Sommaire

1. [Installation](#1-installation)
2. [Premiers pas](#2-premiers-pas)
3. [Concepts](#3-concepts)
4. [Configurer une machine Scaleway](#4-configurer-une-machine-scaleway)
5. [Travailler en équipe (serveur central)](#5-travailler-en-équipe-serveur-central)
6. [Coûts d'hébergement](#6-coûts-dhébergement)
7. [Ligne de commande](#7-ligne-de-commande)
8. [Ajouter un composant (plugin)](#8-ajouter-un-composant-plugin)
9. [Utiliser les skills Claude](#9-utiliser-les-skills-claude)
10. [Arborescence](#10-arborescence)
11. [Où sont les données et comment les sauvegarder](#11-où-sont-les-données-et-comment-les-sauvegarder)
12. [Dépannage](#12-dépannage)

---

## 1. Installation

Prérequis : Mac Apple Silicon, [uv](https://docs.astral.sh/uv/) (Python 3.12 est installé
automatiquement), Node.js ≥ 22.12 et npm (exigé par Electron 44 ; testé avec Node 26),
ffmpeg facultatif (PyAV embarque ses codecs).

```bash
cd ~/Documents/"studio AI"                        # racine du dépôt (= le workspace)

# 1) Environnement Python : moteur d'entraînement + serveur de l'app + outils de dev
#    (+ baselines : torchvision, pour TrustMark, InvisMark et SSL — voir §8)
uv sync --extra train --extra server --extra dev --extra baselines

# 2) Application de bureau
cd desktop
npm install
npm run dist                                       # construit desktop/release/mac-arm64/Watermark Lab.app
open "release/mac-arm64/Watermark Lab.app"         # (ou glisser l'app dans /Applications)
```

- Mode développement (rechargement à chaud) : `cd desktop && npm run dev`.
- L'app lance elle-même le backend (`.venv/bin/python -m wmlab.cli server`) et le coupe à
  la fermeture ; **les entraînements, eux, continuent**.
- Le chemin du workspace est mémorisé dans l'app au moment du `npm run dist`
  (`resources/workspace.json`, embarqué : valeur par défaut). Si vous déplacez le dossier :
  menu **Watermark Lab → Choisir le workspace…** — le choix est mémorisé
  (`~/Library/Application Support/Watermark Lab/config.json`) et prime sur le chemin
  embarqué aux lancements suivants ; la variable `WMLAB_WORKSPACE` prime sur tout (ordre
  complet : `desktop/README.md` § Workspace).
- Extras optionnels : `uv sync --extra quality` (LPIPS, STOI, torchvision) pour les
  métriques perceptuelles.

Vérifier l'installation :

```bash
.venv/bin/python -m wmlab.cli validate            # tous les YAML du workspace
.venv/bin/python -m pytest -q                     # tests (rapides, CPU)
```

## 2. Premiers pas

1. **Essai éclair** d'un projet fourni (quelques secondes, données synthétiques) :
   ```bash
   .venv/bin/python -m wmlab.cli train projects/image-hidden/experiments/baseline.yaml --smoke
   ```
2. Ouvrir l'app : **Accueil** montre les projets, les runs actifs et le budget.
3. Bouton **« Nouveau lancement » (⌘N)** → projet `image-hidden` → expérience `baseline`
   → machine `local` → durée max → estimation du coût → **Lancer**. Le run apparaît dans
   **Runs** : courbes par phase, échantillons (original / tatoué / différence amplifiée ×10 ;
   audio : résidu ×10), checkpoints, évaluation finale sur le benchmark `image_v1`.
   Tant qu'aucun run d'au moins 50 steps et 60 s n'a tourné sur la machine choisie,
   l'estimation affiche « Aucun run … comparable : estimation impossible » (un essai
   `--smoke` ne compte pas) : c'est normal, lancer d'abord le run, l'estimation sera
   disponible dès le suivant.
4. Comparer, classer : **Classement** (front de Pareto qualité × robustesse, heatmap des
   attaques) et « Comparer des runs ».

Trois projets de référence sont fournis, abondamment commentés :

| Projet | Modalité | Contenu |
|---|---|---|
| `image-hidden` | image, 48 bits | baseline HiDDeN 3 phases, curriculum de résolution, WSD + branchement, HPO TPE + ASHA, HPO multi-objectif NSGA-II |
| `audio-seal-lite` | audio, 16 bits | générateur résiduel 1D + détecteur, codecs MP3/AAC réels, HPO du LR |
| `video-framewise` | vidéo, 32 bits | HiDDeN frame par frame, H.264, pertes de frames, perte temporelle ; stratégie image → vidéo dans `notes.md` |

Ils tournent sur données **synthétiques** (aucun téléchargement) et en quelques minutes sur
le Mac ; chaque YAML montre en commentaire comment passer aux données réelles.

## 3. Concepts

| Concept | Où | Rôle |
|---|---|---|
| **Projet** | `projects/<p>/project.yaml` | un schéma de watermarking : modalité, taille du message (`message_bits`), objectifs qualité / robustesse (axes du Pareto, contrainte, ex. PSNR ≥ 38 dB), benchmark de référence ; journal dans `notes.md` |
| **Expérience** | `projects/<p>/experiments/<e>.yaml` | une recette d'entraînement complète : modèle, données, optimiseur, pertes, **phases**, validation, checkpoints ; avec une **hypothèse** |
| **Phase / curriculum** | `phases:` d'une expérience | étapes successives, chacune avec son planning de LR (constant, cosinus, WSD…), ses poids de pertes (constants ou en rampe), ses attaques et leur force, éventuellement ses données, son batch, des modules gelés |
| **Plan HPO** | `projects/<p>/hpo/<plan>.yaml` | étude Optuna sur une expérience : espace de recherche (chemins pointés), objectifs (mono ou multi), contraintes, sampler, pruner, proxy basse fidélité, budget (essais, heures, €) |
| **Benchmark** | `benchmarks/<nom>.yaml` | suite d'évaluation **figée et versionnée** (données, attaques à force 1, métriques, FPR). Ne jamais modifier : créer `image_v2`… |
| **Dataset** | `datasets/<nom>.yaml` | catalogue : description, source, licence, chemins **par machine** ; référencé par `@nom` dans les specs |
| **Run** | `.wmlab/runs/<run_id>/` | un entraînement (ou un essai HPO) : manifest, spec résolue, métriques JSONL, checkpoints, échantillons, évaluations, notes |
| **Étude** | `.wmlab/studies/<study_id>/` | une exécution d'un plan HPO ; chaque essai est un run |
| **Machine** | `.wmlab/hosts.yaml` (page **Machines**) | `local` (le Mac) ou un serveur `ssh` avec son prix €/h ; inventaire des checkpoints non rapatriés, rapatriement, retrait (section 5) |
| **Révision git** | champ du dialogue « Nouveau lancement » | commit exécuté par le job (branche ou sha) ; en mode équipe, le serveur exporte exactement ce commit vers la machine GPU |
| **Budget** | `workspace.yaml` (page **Budget**) | total, période, dépenses calculées depuis les runs / études / évaluations + entrées manuelles |

Branchement : une expérience peut repartir d'un checkpoint d'un autre run
(`init_from: {run, checkpoint: "phase_end:stable", start_phase: decay}`) — par exemple
pour tester plusieurs fins d'entraînement sans refaire le début
(`projects/image-hidden/experiments/wsd_branch.yaml`).

## 4. Configurer une machine Scaleway

1. **Créer une instance GPU** (ex. L40S, H100) avec une image Ubuntu « GPU OS » (pilotes
   NVIDIA et CUDA préinstallés) et votre **clé SSH publique** (console Scaleway →
   Identifiants → Clés SSH). Noter l'IP publique.
2. **Clé SSH et alias** sur le Mac (l'app n'enregistre aucun secret : elle utilise
   `~/.ssh/config` et vos clés, en mode non interactif) :
   ```bash
   ssh-keygen -t ed25519 -C "wmlab"                # si vous n'avez pas encore de clé
   ```
   `~/.ssh/config` :
   ```
   Host scw-l40s
       HostName 51.15.0.1
       User root
       IdentityFile ~/.ssh/id_ed25519
       ServerAliveInterval 30
   ```
   Tester une première fois à la main (accepte l'empreinte du serveur) : `ssh scw-l40s`.
3. **Déclarer la machine** dans l'app, page **Machines** (ou dans `.wmlab/hosts.yaml`) :
   ```yaml
   hosts:
     - {name: local, kind: local, price_eur_per_hour: 0, max_concurrent_jobs: 1}
     - name: scw-l40s
       kind: ssh
       ssh_target: scw-l40s              # alias de ~/.ssh/config (ou root@51.15.0.1)
       workdir: /root/wmlab              # le code est synchronisé ici (rsync)
       data_dir: /root/wmlab-data        # runs et études du serveur
       python: .venv/bin/python
       price_eur_per_hour: 1.4           # prix réel de l'instance : sert au suivi du budget
       gpu: "L40S 48GB"
       max_concurrent_jobs: 2
   ```
4. **Tester puis préparer** la machine (page Machines) : le test vérifie SSH, le GPU et le
   disque ; la préparation synchronise le code puis exécute `scripts/remote_bootstrap.sh`
   (idempotent : vérifie `nvidia-smi`, installe `uv`, `uv sync --extra train`, crée le
   dossier de données, affiche GPU / torch / CUDA). La première préparation **télécharge
   torch + CUDA (plusieurs Go) : comptez plusieurs minutes** (délai maximal de l'app : 1 h) ;
   les suivantes sont quasi instantanées. À la main :
   ```bash
   ssh scw-l40s 'cd /root/wmlab && bash scripts/remote_bootstrap.sh /root/wmlab-data'
   ```
   `rsync` doit exister sur le serveur (présent sur les images Ubuntu ; sinon
   `apt install rsync`).
5. **Données** : rien n'est téléchargé automatiquement. L'URL de téléchargement de chaque
   dataset du catalogue est dans sa fiche `datasets/<nom>.yaml` (`description` / `source` ;
   ex. COCO 2017 train : `http://images.cocodataset.org/zips/train2017.zip`, ~18 Go ; DIV2K
   valid : `DIV2K_valid_HR.zip`). Les télécharger **directement sur le serveur** (`wget` +
   `unzip`), ou les copier depuis le Mac (`rsync -avP ~/datasets/coco/ scw-l40s:/root/datasets/coco/`),
   puis ajouter le chemin de la machine dans le catalogue :
   `datasets/coco2017.yaml → paths: {scw-l40s: "/root/datasets/coco/train2017"}`.
6. **Lancer** : « Nouveau lancement » → machine `scw-l40s` → **Révision git** (pré-remplie
   avec la branche et le sha courants ; avertissement si l'arbre est sale) → **durée max**
   (toujours) → l'estimation € s'affiche. Pendant le run, les dossiers de runs sont
   rapatriés toutes les `sync_interval_s` secondes (sans les `.pt`) ; **en fin de job, la
   synchronisation est complète** (checkpoints compris) et vérifiée fichier par fichier
   (`Job.retrieval` : `complete`, sinon `partial` avec la liste des manquants, affiché en
   avertissement sur le job). Un `.pt` reste aussi rapatriable à la demande depuis le
   détail du run.
7. **Premier entraînement réel** (exemple `image-hidden`, ~10× la baseline synthétique) :
   copier la baseline puis changer les données, la taille et le nombre de steps —
   `wmlab new experiment image-hidden coco_128 --from baseline`, puis dans
   `projects/image-hidden/experiments/coco_128.yaml` :
   ```yaml
   data:
     train:
       dataset: image_folder
       params: {root: "@coco2017"}          # chemin résolu selon la machine (paths du catalogue)
       preprocess:
         - {name: random_resized_crop, params: {size: 128, scale: [0.5, 1.0]}}
         - {name: hflip, params: {p: 0.5}}
     val:
       dataset: image_folder
       params: {root: "@div2k_valid"}
       preprocess: [{name: center_crop, params: {size: 128}}]
     batch_size: 32
     num_workers: 8
   # phases : steps ×10 (300 → 3000, 450 → 4500… voir baseline.yaml) ; le reste inchangé
   ```
   Équivalent sans toucher au YAML (les valeurs `--set` sont du YAML, y compris un bloc) :
   ```bash
   wmlab train projects/image-hidden/experiments/baseline.yaml \
     --set 'data.train={dataset: image_folder, params: {root: "@coco2017"}, preprocess: [{name: random_resized_crop, params: {size: 128, scale: [0.5, 1.0]}}, {name: hflip, params: {p: 0.5}}]}' \
     --set 'data.val={dataset: image_folder, params: {root: "@div2k_valid"}, preprocess: [{name: center_crop, params: {size: 128}}]}' \
     --set data.batch_size=32 --set phases.0.steps=3000 --set phases.1.steps=9000 --set phases.2.steps=3000
   ```
   Ordre prudent, toujours le même : `--smoke` (vérifie la recette **sans** ouvrir les
   données : elles sont remplacées par du synthétique) → run court sur la machine GPU avec
   **durée max 0,5 h** (`--max-hours 0.5` ; étalonne le débit réel et débloque l'estimation €
   des lancements suivants) → run complet. Repères de départ : 128 px, batch 32, ×10 steps ;
   voir `projects/image-hidden/notes.md` (« Passer à l'échelle ») pour la suite (256 px, ×50).
8. **Rapatrier puis retirer** quand la machine ne sert plus : Scaleway facture tant que
   l'instance existe. Sur la carte de la machine (page **Machines**), l'**inventaire**
   indique les runs présents sur la machine et les checkpoints non rapatriés (nombre, Go) ;
   **« Rapatrier les checkpoints »** lance un job `retrieve` (tout ce qui est sur la machine,
   vérifié, réindexé) ; **« Retirer la machine »** (admin) **refuse tant qu'il reste des
   checkpoints non rapatriés, des runs non indexés ou un job actif**, puis marque la
   machine `decommissioned_at` (elle reste dans l'historique des coûts) et, si l'instance
   est déclarée (`scaleway: {zone, server_id}`) et `SCW_SECRET_KEY` configurée sur le
   serveur, propose de **supprimer l'instance Scaleway** (sinon : console Scaleway,
   supprimer avec ses volumes). **« Éteindre l'instance »** (`poweroff`) suspend la
   facturation GPU entre deux sessions en gardant le disque.

## 5. Travailler en équipe (serveur central)

Pour une équipe, le backend et toutes les données quittent le Mac pour un **serveur
central** (petite instance Scaleway, sans GPU) ; le code (YAML, plugins `lab/`, package
`wmlab`) vit dans un **dépôt git** ; les machines GPU sont créées et retirées à la demande.
Contrat : `ARCHITECTURE.md` §12 ; guide d'installation et d'exploitation :
[`deploy/README.md`](deploy/README.md).

```
[Postes des membres] ── git push ──► [Dépôt git distant]
        │ app (menu Serveur → distant) ou navigateur     │ git pull --ff-only (auto)
        ▼                                                ▼
[Serveur central : backend wmlab + .wmlab (runs, checkpoints, index, jobs, users)]
        │ git archive <sha> → rsync → workdir/code/<sha> ▲ rsync complet (dont *.pt) en fin de job
        ▼                                                │
[Machines GPU Scaleway, créées et retirées à la demande] ┘
```

- **Installation** : `sudo bash deploy/install_server.sh --repo <url> --branch main --dir
  /opt/wmlab --domain lab.example.com --admin <nom> --admin-email <e-mail>` sur Ubuntu 24.04
  (idempotent) : uv, Node 22, clone, `uv sync`, build du frontend, service systemd durci,
  Caddy (HTTPS automatique, en-têtes de sécurité), sauvegardes quotidiennes, premier compte
  admin (mot de passe temporaire affiché une seule fois, à changer à la première connexion),
  `workspace.yaml` en mode hébergé.
- **Comptes** : chacun se connecte avec **son e-mail et son mot de passe** (plus de jeton
  personnel). Inscription depuis l'écran de connexion (« Créer un compte ») → compte **en
  attente** → approuvé par un admin (Réglages → Utilisateurs, ou `wmlab users approve EMAIL`) ;
  ou création directe par un admin (`wmlab users add EMAIL --name NAME [--role admin|member]`,
  mot de passe temporaire affiché une seule fois). Mot de passe oublié : un admin lance
  `wmlab users reset-password EMAIL`. Les runs, études et jobs portent leur auteur (`user` =
  nom du compte) ; filtre « mes runs ».
- **Rôles** : `admin` = tout (lancements, édition des YAML, machines, réglages, comptes) ;
  `member` = **lecture seule** (runs, études, classement, budget, machines) **sans accès au code
  d'entraînement ni aux architectures** (`lab/`, `wmlab/`, specs YAML, registres, journaux).
- **Connexion** : app de bureau, menu **Serveur → Se connecter à un serveur distant…** (URL +
  e-mail + mot de passe ; l'app ne garde qu'un jeton de session de 30 jours ; retour au backend
  local par le même menu) ; ou navigateur sur `https://lab.example.com/` (même écran de
  connexion). Le jeton serveur (`WMLAB_TOKEN`) n'est qu'un secours admin en ligne de commande.
- **Flux de travail** : éditer **localement** (poste du membre, mode local inchangé) →
  `wmlab validate` / `wmlab train --smoke` → **commit + push** → dans l'app connectée au
  serveur avec un compte **admin**, « Nouveau lancement » avec la **révision git** (branche ou
  sha) : le serveur exporte ce commit exact vers la machine GPU. Les modifications non poussées
  ne sont pas prises en compte. Un YAML enregistré depuis l'app est commité et poussé par le
  serveur.
- **Machines GPU** : la clé SSH du serveur (générée à l'installation) est déposée dans les
  clés du projet Scaleway ; cycle : créer → **Préparer** → lancer → **Rapatrier les
  checkpoints** → **Retirer la machine** (refuse tant que tout n'est pas rapatrié) →
  supprimer l'instance (depuis l'app si `SCW_SECRET_KEY` est configurée, sinon console).
- **Sur les postes**, `workspace.yaml` garde `hosted.enabled: false` : le mode local
  (section 4) continue de fonctionner tel quel.

## 6. Coûts d'hébergement

Ordres de grandeur, prix Scaleway **HT, juin 2026** (à vérifier sur la console ; le suivi du
budget de l'app ne compte que les runs, études et évaluations, plus vos entrées manuelles) :

| Poste | Prix | Remarque |
|---|---|---|
| Serveur central (DEV1-S ou BASIC2-A2C-4G) | ≈ 7 à 17 €/mois | + volume Block Storage selon le volume de checkpoints conservés |
| Object Storage (sauvegardes, checkpoints archivés) | ≈ 16 €/To/mois | facultatif mais recommandé (hors site) |
| GPU L40S (48 Go) | 1,47 €/h | ≈ 35 €/jour si laissée allumée |
| GPU H100 (80 Go) | 2,87 €/h | ≈ 69 €/jour |

Une machine GPU est facturée **tant que l'instance existe** (éteinte : volume seul) : la
retirer dès qu'elle ne sert plus, après rapatriement. Ajouter le serveur central et le
stockage au budget via les **entrées manuelles** (page Budget) pour un suivi complet.

## 7. Ligne de commande

Toutes les commandes : `.venv/bin/python -m wmlab.cli <commande>` (ou `wmlab <commande>` si
le venv est activé : `source .venv/bin/activate`).

```
wmlab server   [--port 8765] [--token TOKEN] [--host 127.0.0.1] [--workspace PATH] [--no-sync]
wmlab train    SPEC [--set k=v]... [--run-id ID] [--smoke] [--device D] [--max-hours H] [--launch-file F]
wmlab hpo      SPEC [--set k=v]... [--study-id ID] [--smoke] [--device D] [--max-hours H] [--max-trials N] [--launch-file F]
wmlab hpo-worker STUDY_DIR --worker-index N [--device D]
wmlab eval     --run RUN --checkpoint SEL --benchmark NAME [--device D] [--smoke]
wmlab validate [PATH]... [--json] # sans argument : tout le workspace (YAML + import des plugins) ; code retour ≠ 0 si erreur
wmlab registry [--kind K] [--json]
wmlab plugin-test KIND NAME [--modality M] [--param k=v]... [--json]   # vérifie un plugin (formes, plages, gradients)
wmlab new project NAME --modality M [--bits 48] [--title T] [--description D]
wmlab new experiment PROJECT NAME [--from EXPERIMENT]
wmlab new plugin KIND NAME --modality M        # squelette dans lab/<kind>s/ ; M = image|audio|video|image,video (all : preprocess/loss/metric)
wmlab new hpo PROJECT NAME --experiment EXP
wmlab runs list [--project P] [--status S] [--include-trials] [--limit 100] [--json]   # sans --include-trials : runs d'essais HPO exclus
wmlab runs show RUN [--json]      # manifest + summary + checkpoints + evals (RUN : id, fragment unique ou dossier)
wmlab index                       # reconstruit l'index SQLite de l'app
wmlab users add EMAIL --name NAME [--role admin|member] [--password-stdin]   # mode équipe (serveur) : compte actif,
                                  # mot de passe temporaire affiché UNE fois (à changer à la 1re connexion)
wmlab users list [--json]         # comptes : e-mail, nom, rôle, état, dernière connexion (« admin » : nom réservé au jeton serveur)
wmlab users approve EMAIL [--role admin|member]   # approuve une inscription en attente (member par défaut)
wmlab users role EMAIL ROLE       # change le rôle (refusé pour soi-même et pour le dernier admin)
wmlab users disable EMAIL         # désactive le compte et révoque ses sessions (401 immédiat)
wmlab users enable EMAIL          # réactive un compte désactivé
wmlab users reset-password EMAIL  # mot de passe oublié : temporaire affiché UNE fois, sessions révoquées
wmlab weights list [--json]       # poids officiels des baselines : source, licence, présence sur cette machine
wmlab weights pull NAME... [--force]   # télécharge des poids officiels (.wmlab/weights/)
```

Exemples :

```bash
# Surcharger des valeurs sans modifier le YAML (chemins pointés, index de liste numériques)
wmlab train projects/image-hidden/experiments/baseline.yaml --set optimizer.lr=3e-4 --set phases.1.steps=2000

# Étude HPO en essai éclair, puis réelle
wmlab hpo projects/image-hidden/hpo/lr_weights.yaml --smoke
wmlab hpo projects/image-hidden/hpo/lr_weights.yaml
# Étude courte : --set s'applique à l'expérience de base de chaque essai, --max-trials /
# --max-hours plafonnent le budget du plan
wmlab hpo projects/image-hidden/hpo/lr_weights.yaml --set seed=1 --max-trials 6 --max-hours 0.5

# Évaluer le meilleur checkpoint d'un run sur un benchmark
wmlab eval --run 20261001-120000-baseline-3fa2 --checkpoint best:val/bit_acc_mean --benchmark image_v1
```

Sélecteurs de checkpoint : `last`, `best:<métrique>`, `phase_end:<phase>`, ou un nom exact
(`step_001500`).

## 8. Ajouter un composant (plugin)

1. Générer un squelette commenté :
   ```bash
   wmlab new plugin model my_unet --modality image     # → lab/models/my_unet.py
   ```
   Types : `model`, `attack`, `dataset`, `preprocess`, `loss`, `metric` ; `--modality` est
   obligatoire. Deux plugins d'exemple, commentés et **déjà enregistrés**, sont livrés dans
   `lab/` : `lab/models/example_residual_unet.py` (`example_unet`, U-Net résiduel) et
   `lab/attacks/example_pixelate.py` (`pixelate`) — utilisables tels quels dans une
   expérience ; pour les modifier, copier sous un autre nom (un nom de registre en double
   fait échouer l'import de tout le module).
2. Compléter les `TODO` en respectant les contrats (`ARCHITECTURE.md` §2–§4) : tenseurs
   image `[B,3,H,W]` dans [0,1], audio `[B,1,T]` dans [-1,1], vidéo `[B,F,3,H,W]` ; `nbits`
   jamais dans les params ; une attaque à `strength = 0` est l'identité et conserve la forme ;
   `differentiable = False` pour un codec réel (straight-through automatique).
3. Vérifier : `wmlab plugin-test model my_unet`, puis `wmlab registry --kind model`.
4. L'utiliser dans une expérience (`model: {name: my_unet, params: {...}}`) :
   `wmlab new experiment image-hidden unet_v1 --from baseline`, modifier, puis
   `wmlab validate` et `wmlab train … --smoke`.

Le composant apparaît dans la **Bibliothèque** de l'app avec son schéma de paramètres et,
comme description, le **1er paragraphe** de sa docstring (une phrase, en français).

### Baselines pré-entraînées (poids officiels)

Six modèles publiés sont livrés comme plugins (`lab/models/baseline_*.py`) : ils chargent
leurs **poids officiels** et s'utilisent comme n'importe quelle architecture. Les poids ne
sont pas dans le dépôt : `wmlab weights list` puis `wmlab weights pull <nom>…` (une fois par
machine, dans `.wmlab/weights/`), ou `download: true` dans `model.params`.

| Modèle | Papier | Bits natifs | Poids (`wmlab weights pull …`) | Licence des poids |
|---|---|---|---|---|
| `videoseal` | Video Seal 1.0 (Meta, 2024) | 256 | `videoseal_1.0` (228 Mo) | MIT |
| `pixelseal` | Pixel Seal (Meta, 2025) — **ré-entraînable** | 256 | `pixelseal` (1,2 Go) | MIT |
| `wam` | Watermark Anything (Meta, ICLR 2025) | 32 | `wam_mit` (378 Mo) | MIT |
| `trustmark` | TrustMark (Adobe, 2023), variantes Q/B/C/P | 100 | `trustmark_q_encoder` `trustmark_q_decoder` (65 Mo) | MIT |
| `invismark` | InvisMark (Microsoft, WACV 2025) | 100 (94 utiles) | `invismark_paper` (1,2 Go, miroir HF non officiel) | non précisée |
| `ssl_watermark` | SSL watermarking (Meta, ICASSP 2022) | libre | `ssl_dino_r50_plus` `ssl_normlayer_yfcc` (806 Mo) | **CC-BY-NC (non commercial)** |

- Une baseline figée s'évalue avec une expérience **`phases: []`** (évaluation seule : aucun
  entraînement, benchmark final, checkpoint de référence sans copie des poids) : voir
  `projects/image-baselines/` (32 bits pour tous, la capacité de WAM). Seul `pixelseal` peut
  être entraîné : `projects/image-baselines/experiments/pixelseal_finetune.yaml`.
- Le projet fixe `message_bits` ; un modèle plus capacitaire complète son message avec un
  bourrage fixe ignoré au décodage (`message_bits` ≤ capacité native).
- InvisMark a été entraîné sur des UUIDv4 tronqués : les bits 48-51 et 64-65 sont figés
  (`uuid_payload: true` les force et les retire du message, 94 bits utiles).
- Code tiers adapté (licences, attributions) : `lab/models/THIRD_PARTY_NOTICES.md`.

## 9. Utiliser les skills Claude

Deux skills sont fournies dans `.claude/skills/` ; Claude Code les utilise automatiquement
quand la demande s'y prête (dans ce dossier).

- **`wmlab-experiment`** : créer une architecture, une attaque, un dataset, une perte, une
  métrique, une expérience, un curriculum, un plan HPO ou un benchmark compatibles, puis
  les valider jusqu'au vert (`validate`, `plugin-test`, `train --smoke`).
- **`wmlab-analyse`** : analyser runs et études (courbes, benchmark, utilisation GPU),
  diagnostiquer, écrire la conclusion dans le `notes.md` du run et proposer la suite avec
  son coût face au budget.
- **`/check`** (à taper dans Claude Code) : lance `validate`, `pytest`, le build et les tests
  du desktop et le dry-run de `deploy/`, puis rapporte les échecs.

Le dépôt fournit aussi des agents spécialisés (`.claude/agents/`, chacun avec le modèle adapté
à sa tâche), des hooks qui empêchent d'écrire sous `.wmlab/` ou de modifier un benchmark publié
et relancent `wmlab validate` après l'édition d'un YAML, et des règles chargées par dossier
(`.claude/rules/`) ; voir `CLAUDE.md` § Outillage Claude.

Exemples de demandes :

- « Ajoute une architecture image type StegaStamp (U-Net + décodeur à STN) au projet
  image-hidden et une expérience pour la comparer à la baseline. »
- « Crée une attaque audio de compression Opus et ajoute-la à la dernière phase d'audio-seal-lite. »
- « Fais un curriculum qui démarre à 64 px et finit à 256 px sur COCO, avec une décroissance WSD. »
- « Prépare un plan HPO multi-objectif PSNR × robustesse pour ma nouvelle architecture,
  budget 30 €. »
- « Crée benchmarks/image_v2 sur DIV2K avec les mêmes attaques que v1 plus la saturation. »
- « Analyse le dernier run de video-framewise : pourquoi H.264 reste à 0,6 ? Que lancer ensuite ? »
- « Combien m'a coûté l'étude lr_weights et combien reste-t-il de budget ? »

## 10. Arborescence

```
studio AI/                         # workspace (racine du dépôt)
  workspace.yaml                   # nom, budget (40 000 €), dossier de données, machine par défaut
  ARCHITECTURE.md  CLAUDE.md  README.md  pyproject.toml  uv.lock
  wmlab/                           # package Python : SDK, moteur (train/eval/hpo), serveur, CLI
  lab/                             # VOS plugins (models/, attacks/, datasets/, losses/, metrics/, preprocess/)
                                   #   + 2 exemples livrés : models/example_residual_unet.py, attacks/example_pixelate.py
                                   #   + 6 baselines pré-entraînées : models/baseline_*.py (§8)
  projects/<projet>/
    project.yaml                   # modalité, message_bits, objectifs, benchmark
    experiments/<exp>.yaml         # recettes d'entraînement (phases)
    hpo/<plan>.yaml                # plans d'optimisation
    notes.md                       # journal du projet
  benchmarks/<nom>.yaml            # suites d'évaluation figées (image_v1, audio_v1, video_v1)
  datasets/<nom>.yaml              # catalogue (coco2017, div2k_valid, librispeech_clean100,
                                   #   voxpopuli_en, fma_small, sav)
  scripts/remote_bootstrap.sh      # préparation d'un serveur GPU
  deploy/                          # serveur central : install_server.sh, service systemd, Caddyfile,
                                   #   backup.sh, README.md (mode équipe)
  tests/                           # pytest
  desktop/                         # app Electron + React
  .claude/skills/                  # skills Claude (wmlab-experiment, wmlab-analyse)
  .wmlab/                          # DONNÉES LOCALES (non versionnées) ; en mode équipe : sur le serveur
```

## 11. Où sont les données et comment les sauvegarder

Tout ce qui est produit vit dans **`.wmlab/`** à la racine du workspace (réglable :
`data_dir` dans `workspace.yaml`, ou variable `WMLAB_DATA`) :

| Chemin | Contenu | Sauvegarder ? |
|---|---|---|
| `.wmlab/runs/<run_id>/` | runs (locaux et rapatriés) : `run.json`, `metrics.jsonl`, `checkpoints/`, `samples/`, `eval/`, `notes.md` | **oui** (source de vérité) |
| `.wmlab/studies/<study_id>/` | études HPO (`optuna.journal`, `trials.jsonl`) | **oui** |
| `.wmlab/hosts.yaml` | machines (avec `decommissioned_at` pour l'historique) | oui (petit) |
| `.wmlab/users.yaml` | mode équipe (serveur) : comptes (e-mail, nom, rôle, état, mot de passe haché scrypt), mode 0600 | oui (petit) |
| `.wmlab/sessions.json` | mode équipe (serveur) : sessions de connexion (empreintes SHA-256 des jetons, expiration), mode 0600 | facultatif (sans lui, chacun se reconnecte) |
| `.wmlab/jobs/` | suivi des lancements de l'app | facultatif |
| `.wmlab/index.sqlite` | index de l'app | non (reconstruit : `wmlab index`) |
| `.wmlab/weights/` | poids officiels des baselines (plusieurs Go) | non (`wmlab weights pull`) |
| `.wmlab/cache/`, `.wmlab/logs/` | caches, journaux | non |

Les YAML (`projects/`, `benchmarks/`, `datasets/`, `lab/`) sont du code : les versionner
avec git. Sauvegarde des données (checkpoints `.pt` volumineux inclus ou non) :

```bash
# Copie incrémentale vers un disque externe (sans les checkpoints : ajouter --exclude '*.pt')
rsync -a ".wmlab/runs" ".wmlab/studies" ".wmlab/hosts.yaml" /Volumes/Sauvegarde/wmlab/
```

Les checkpoints des runs distants restent sur la machine GPU tant qu'ils ne sont pas
rapatriés (fin de job, ou bouton « Rapatrier les checkpoints ») : **vérifier l'inventaire de
la machine et rapatrier avant de supprimer une instance Scaleway** — « Retirer la machine »
le refuse tant qu'il reste quelque chose. Ne jamais supprimer ni modifier un dossier de run à
la main (les coûts et le classement en dépendent). En mode équipe, les sauvegardes du serveur
sont décrites dans `deploy/README.md` §8 (Object Storage via rclone, ou archive locale).

## 12. Dépannage

- **L'app ne démarre pas** : journal du backend dans
  `~/Library/Application Support/Watermark Lab/logs/backend.log` ; vérifier que
  `.venv/bin/python -m wmlab.cli server` fonctionne dans le workspace.
- **`wmlab validate` signale un composant inconnu** : `wmlab registry` affiche les erreurs
  d'import des plugins.
- **`401` ou « jeton requis » sur l'API** : le serveur a toujours un jeton — `--token`,
  sinon la variable `WMLAB_TOKEN`, sinon un jeton aléatoire affiché **une fois** sur stderr
  au démarrage (à passer dans l'en-tête `X-WMLab-Token`). Hors loopback (`--host`), un
  jeton explicite est obligatoire. L'app packagée génère le sien et le transmet au backend
  par `WMLAB_TOKEN` (jamais sur la ligne de commande).
- **`401` sur le serveur central** : e-mail ou mot de passe incorrect à la connexion (mot de
  passe oublié : un admin lance `wmlab users reset-password EMAIL`), ou session expirée
  (30 jours) / révoquée (déconnexion, changement de mot de passe, compte désactivé) : se
  reconnecter. **`403`** : compte **en attente** d'approbation (`wmlab users approve EMAIL`),
  compte **désactivé** (`wmlab users enable EMAIL`), ou action réservée aux admins (un
  `member` est en lecture seule). **`429`** : trop de tentatives de connexion, attendre.
  Détails : `deploy/README.md` §10.
- **Run « perdu » (lost)** : plus de heartbeat depuis 10 min (machine éteinte, process tué,
  synchronisation coupée) ; voir la page Machines et le `stdout.log` du run.
- **Connexion SSH impossible** depuis l'app alors que `ssh <alias>` marche : l'app utilise
  `BatchMode=yes` (aucune saisie possible) — la clé doit être chargée sans mot de passe
  interactif (`ssh-add --apple-use-keychain ~/.ssh/id_ed25519`).
