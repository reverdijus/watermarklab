# Watermark Lab — serveur central (mode hébergé)

Ce guide installe et exploite le **serveur central** décrit dans `ARCHITECTURE.md` §12 : une
petite instance Scaleway qui héberge le backend `wmlab`, toutes les données (`.wmlab/` : runs,
checkpoints, index, jobs, utilisateurs) et pilote les machines GPU. Le code (YAML, plugins
`lab/`, package `wmlab`) reste dans un **dépôt git** que chaque membre édite sur son poste.

Sommaire : [1. Architecture](#1-architecture-de-léquipe) · [2. Prérequis](#2-prérequis) ·
[3. Installation](#3-installation-pas-à-pas) · [4. Utilisateurs et comptes](#4-utilisateurs-et-comptes) ·
[5. Se connecter](#5-se-connecter-au-serveur) · [6. Dépôt git](#6-dépôt-git-distant) ·
[7. Machines GPU](#7-machines-gpu) · [8. Sauvegardes](#8-sauvegardes) ·
[9. Mise à jour](#9-mettre-à-jour-le-serveur) · [10. Dépannage](#10-dépannage) ·
[11. Sécurité](#11-sécurité)

---

## 1. Architecture de l'équipe

```
[Postes des membres] ── git push ──► [Dépôt git distant (GitHub/GitLab…)]
        │ app de bureau (mode distant) ou navigateur          │ git pull --ff-only (auto)
        ▼                                                     ▼
[Serveur central : backend wmlab + .wmlab (runs, checkpoints, index, jobs, users)]
        │ git archive <sha> → rsync → workdir/code/<sha>      ▲ rsync complet (dont *.pt) en fin de job
        ▼                                                     │
[Machines GPU Scaleway, créées et retirées à la demande] ─────┘
```

- **Chaque membre** garde son installation locale (README §1) pour éditer et valider
  (`wmlab validate`, `wmlab train --smoke`), puis **commit + push**. Son app de bureau est
  connectée au serveur central (menu **Serveur → Se connecter à un serveur distant…**) avec
  **son compte** (e-mail + mot de passe, §4). Les comptes `admin` lancent les jobs et gèrent
  les machines ; les comptes `member` suivent les résultats en **lecture seule**, sans accès
  au code d'entraînement.
- **Le serveur** suit une branche (`hosted.git_branch`, `git pull --ff-only` automatique
  toutes les `auto_pull_s` secondes), exécute les jobs, synchronise les runs, indexe et sert
  l'interface (`https://<domaine>/`). Une modification de YAML enregistrée depuis l'app est
  commitée et poussée par le serveur (`auto_commit`).
- **Chaque lancement** (compte admin) cible une **révision git précise** (champ « Révision
  git » du dialogue de lancement : branche ou sha) : le serveur exporte ce commit
  (`git archive`) vers la machine GPU, qui exécute exactement ce code. Les modifications non
  poussées ne sont jamais prises en compte.
- **En fin de job**, le serveur rapatrie le dossier de run **complet** (checkpoints `.pt`
  compris) et vérifie chaque fichier (`Job.retrieval`). Une machine GPU ne peut être
  « retirée » que lorsque son inventaire ne contient plus rien de non rapatrié.

Sur le serveur central, `workspace.yaml` porte `hosted.enabled: true` ; sur les postes des
membres, il reste `false` (mode local inchangé).

## 2. Prérequis

| Élément | Recommandation |
|---|---|
| Instance Scaleway (sans GPU) | **BASIC2-A2C-4G** (2 vCPU, 4 Go, ≈ 16,79 € HT/mois) ou **DEV1-S** (2 vCPU, 2 Go, ≈ 6,55 € HT/mois — juste pour le build du frontend, prévoir du swap). Prix juin 2026, à vérifier sur la console. |
| Disque | Le serveur **conserve tous les checkpoints rapatriés** : dimensionner le volume Block Storage selon le volume attendu (≈ 100–500 Mo par checkpoint HiDDeN, `keep_last` + `keep_best` + `phase_end` par run). 40 Go minimum (torch CUDA ≈ 5 Go dans `.venv` tant que l'extra `cpu` n'existe pas), 100–200 Go confortable ; extensible plus tard. |
| Image | Ubuntu 24.04 LTS (Noble). |
| Réseau | IP publique (IPv4). Groupe de sécurité : entrée **22** (SSH), **80** et **443** (Caddy / Let's Encrypt) seulement. |
| DNS | Un enregistrement **A** `lab.example.com → <IP publique>` créé **avant** l'installation (Caddy obtient le certificat au premier accès). |
| Dépôt git | Dépôt **privé** sur GitHub / GitLab / Gitea contenant le workspace (§6). |
| Object Storage (facultatif) | Un bucket Scaleway pour les sauvegardes hors site (§8). |
| Clé API Scaleway (facultatif) | Clé IAM avec droits Instances pour éteindre / supprimer les machines GPU depuis l'app (`SCW_SECRET_KEY`, §7 et §11). |

## 3. Installation pas à pas

1. **Créer l'instance** (console Scaleway → Instances → Créer), Ubuntu 24.04, votre clé SSH
   d'administrateur, IP publique ; créer l'enregistrement DNS.
2. **Se connecter** : `ssh root@<ip>`.
3. **Récupérer le script d'installation** (le dépôt est privé : copier le script depuis votre
   poste, il clonera lui-même le dépôt avec la clé du serveur) :
   ```bash
   # depuis votre Mac, à la racine du workspace
   scp deploy/install_server.sh root@<ip>:/root/
   ```
4. **Premier passage** — le script crée l'utilisateur de service `wmlab`, sa clé SSH ed25519,
   installe uv, Node 22, puis **s'arrête** si le dépôt n'est pas accessible en affichant la clé
   publique :
   ```bash
   sudo bash /root/install_server.sh \
     --repo git@github.com:equipe/wmlab.git --branch main \
     --dir /opt/wmlab --domain lab.example.com \
     --admin justin --admin-email justin@example.com
   ```
   `--admin` est l'identifiant d'équipe du premier compte admin (attribution des runs, auteur des
   commits de l'app ; le nom `admin` est réservé au jeton serveur), `--admin-email` son e-mail de
   connexion (stocké en minuscules).
5. **Déposer la clé publique du serveur** comme **clé de déploiement en écriture** sur le dépôt
   (GitHub : *Settings → Deploy keys → Add → Allow write access* ; GitLab : *Settings →
   Repository → Deploy keys → Grant write permissions*). L'écriture est nécessaire pour
   `auto_commit` (enregistrement des YAML depuis l'app).
6. **Relancer la même commande** : elle est idempotente. Elle enchaîne :
   - paquets (`git rsync curl openssl zstd…`), `uv` (installateur officiel épinglé, même
     version que `scripts/remote_bootstrap.sh`), Node 22 (NodeSource) ;
   - `git clone --branch main` dans `/opt/wmlab` (propriétaire `wmlab`), identité git du
     serveur pour les commits de l'app ;
   - `uv sync --extra server --extra train` (+ `--extra cpu` s'il existe dans `pyproject.toml` :
     torch CPU-only, beaucoup plus léger) ;
   - `npm ci && npm run build` dans `desktop/` (frontend servi par FastAPI sur `/`) ;
   - `/etc/wmlab/env` (`0640 root:wmlab` : `WMLAB_WORKSPACE`, `WMLAB_TOKEN` généré,
     `SCW_SECRET_KEY` commentée) ;
   - `wmlab users add justin@example.com --name justin --role admin` → **le mot de passe
     temporaire du compte admin est affiché une seule fois** : le noter tout de suite. À la
     première connexion (app ou navigateur), l'app exige d'en choisir un nouveau (12 caractères
     minimum). Si le compte existe déjà (relance du script), rien n'est réaffiché : mot de passe
     oublié → `wmlab users reset-password justin@example.com` (§4) ;
   - service systemd `wmlab-server` (`deploy/wmlab-server.service`, port 8765 sur
     127.0.0.1, `Restart=always`, durcissement §11) ;
   - Caddy (dépôt officiel) + `/etc/caddy/Caddyfile` : `https://lab.example.com` →
     `127.0.0.1:8765`, certificat automatique, en-têtes de sécurité, journal d'accès sans
     secrets (§11) ;
   - `deploy/backup.sh` en `/usr/local/bin/wmlab-backup` + minuterie quotidienne
     `wmlab-backup.timer` (03:30) ;
   - `workspace.yaml` passé en mode hébergé (`hosted.enabled: true`, `git_branch`,
     `public_url`) par `deploy/patch_workspace.py`, puis marqué `git update-index
     --skip-worktree` pour que l'arbre reste propre (condition de l'auto-pull) ;
   - résumé final avec la **clé publique** à déposer sur les machines GPU (§7).
7. **Vérifier** : `curl -s https://lab.example.com/api/health` → `{"ok": true, …}` ;
   `systemctl status wmlab-server caddy wmlab-backup.timer`.
8. **Première connexion** : ouvrir `https://lab.example.com/` (ou l'app de bureau, §5) avec
   `justin@example.com` et le mot de passe temporaire, puis définir le mot de passe définitif.
   Créer ensuite les comptes de l'équipe (§4).

Sans nom de domaine (`--domain` omis), Caddy n'est pas installé : accès par tunnel SSH
`ssh -N -L 8765:127.0.0.1:8765 root@<ip>` puis `http://127.0.0.1:8765/`.

Le script se relance sans risque à tout moment (mise à jour, §9) : il conserve
`/etc/wmlab/env`, la clé SSH et les comptes existants. Pour voir ce qu'il ferait sans
rien exécuter (ni root) : ajouter `--dry-run` ; les scripts de `deploy/` sont testés ainsi par
`bash deploy/tests_dry_run.sh` (syntaxe, sortie du dry-run, refus des options invalides,
directives de durcissement de l'unité, en-têtes du Caddyfile, patch de `workspace.yaml`,
`backup.sh`).

Fichiers de `deploy/` : `install_server.sh` (installation), `wmlab-server.service`
(unité systemd du backend), `Caddyfile` (reverse proxy), `backup.sh` +
`wmlab-backup.service` / `wmlab-backup.timer` (sauvegardes), `patch_workspace.py` (patch
minimal du bloc `hosted:` de `workspace.yaml`), `tests_dry_run.sh` (tests).

## 4. Utilisateurs et comptes

Chaque membre a **son compte** : un **e-mail** (identifiant de connexion), un **mot de passe**
et un **nom** d'équipe (attribution des runs, auteur des commits de l'app ; lettres, chiffres,
`_`, `-`, `.`). Comptes dans `/opt/wmlab/.wmlab/users.yaml` (mots de passe hachés **scrypt**,
jamais en clair), sessions de connexion dans `/opt/wmlab/.wmlab/sessions.json` (empreintes
SHA-256 des jetons de session) ; les deux fichiers sont en `0600`, propriétaire `wmlab`.

Raccourci pratique dans `/root/.bashrc` (utilisé par les commandes ci-dessous) :

```bash
alias wmlab='sudo -u wmlab -H env WMLAB_WORKSPACE=/opt/wmlab /opt/wmlab/.venv/bin/python -m wmlab.cli'
```

### Rôles

| Rôle | Droits |
|---|---|
| `member` | **Lecture seule** : suivi des runs, études, classement, budget et machines. **Aucun accès au code d'entraînement ni aux architectures** (`lab/`, `wmlab/`, specs YAML, registres), ni aux journaux, réglages, git, utilisateurs ou inventaire des machines ; ne lance rien et ne modifie rien (sauf son propre mot de passe). |
| `admin` | Tout : lancements, édition des YAML, notes et tags, machines (préparation, rapatriement, retrait), réglages, comptes. |

Un refus est un `403` avec un message en français ; l'app masque d'elle-même ce qu'un `member`
ne peut pas faire. Réserver `admin` à ceux qui lancent des jobs et gèrent machines et budget.

### Créer un compte

- **Inscription** (ouverte par défaut, `hosted.allow_registration: true`) : écran de connexion
  (app ou navigateur) → **« Créer un compte »** → identifiant, e-mail, mot de passe (12
  caractères minimum). Le compte est créé **en attente** : aucun accès (la connexion répond
  « compte en attente ») tant qu'un admin ne l'a pas approuvé en choisissant son rôle — app :
  **Réglages → Utilisateurs → Approuver** (badge « en attente » dans la sidebar), ou
  `wmlab users approve marie@example.com --role member`. L'e-mail n'est pas vérifié :
  n'approuver que des personnes identifiées.
- **Création directe** par un admin : Réglages → Utilisateurs → **« Créer un compte »**, ou
  `wmlab users add marie@example.com --name marie --role member` → **mot de passe temporaire
  affiché une seule fois**, à transmettre par un canal sûr (gestionnaire de mots de passe,
  message éphémère ; jamais par le dépôt git, un ticket ou un e-mail en clair). Le membre doit
  le changer à sa première connexion (l'app n'affiche rien d'autre tant que ce n'est pas
  fait). `--password-stdin` : le mot de passe est lu sur l'entrée standard au lieu d'être
  généré.

### Gérer les comptes

```bash
wmlab users list [--json]                          # e-mail, nom, rôle, état, dernière connexion
wmlab users approve marie@example.com [--role member|admin]   # inscription en attente → active
wmlab users role marie@example.com admin           # changer le rôle
wmlab users disable marie@example.com              # départ : compte désactivé, sessions révoquées
wmlab users enable marie@example.com               # réactiver
wmlab users reset-password marie@example.com       # mot de passe oublié (voir ci-dessous)
```

- **Mot de passe oublié** : un admin lance `wmlab users reset-password EMAIL` (ou Réglages →
  Utilisateurs → « Réinitialiser le mot de passe ») : nouveau mot de passe temporaire affiché
  **une seule fois**, toutes les sessions du compte révoquées, changement forcé à la connexion
  suivante. Aucun e-mail n'est envoyé par le serveur.
- **Changer son mot de passe** : Réglages → Mon compte (les autres sessions du compte sont
  révoquées).
- **Garde-fous** : un admin ne peut ni se rétrograder ni se désactiver lui-même ; le dernier
  admin actif ne peut être ni rétrogradé ni désactivé. Un compte désactivé reste dans
  `users.yaml` (historique, attribution des runs). Le nom `admin` est réservé au jeton serveur.
- **Fermer l'inscription** une fois l'équipe constituée : §11.
- **Jeton serveur** (`WMLAB_TOKEN` dans `/etc/wmlab/env`) : admin implicite de **secours**, pour
  la ligne de commande uniquement (scripts, panne de comptes) — ne pas le distribuer, il ne
  sert pas à se connecter dans l'app. Exemple, sur le serveur :
  ```bash
  TOKEN=$(sudo sed -n 's/^WMLAB_TOKEN=//p' /etc/wmlab/env)
  curl -s -H "X-WMLab-Token: $TOKEN" http://127.0.0.1:8765/api/users
  ```
- L'app affiche, pour chaque run, étude et job, **qui** l'a lancé (`user` = nom du compte) et
  propose le filtre « mes runs ».
- Anciens jetons personnels `wml_` (version précédente) : **plus reconnus**. `wmlab users list`
  signale ces entrées (« ancien format ») : recréer le compte avec
  `wmlab users add EMAIL --name NAME`.

## 5. Se connecter au serveur

**App de bureau (macOS)** : menu **Serveur → Se connecter à un serveur distant…**, saisir
l'URL (`https://lab.example.com`), votre **e-mail** et votre **mot de passe**. L'app vérifie
`/api/health`, ouvre une session (`POST /api/auth/login`) et ne mémorise que le **jeton de
session** (chiffré via `safeStorage`) dans son `config.json` — jamais le mot de passe ; elle
ne lance plus de backend local. Avec un mot de passe temporaire, elle demande d'abord d'en
définir un nouveau. Retour au mode local : **Serveur → Utiliser le backend local**. Le nom de
l'utilisateur connecté et « Se déconnecter » (révoque la session) sont en bas de la sidebar.

**Navigateur** : ouvrir `https://lab.example.com/` → écran de connexion (e-mail + mot de
passe ; l'URL du serveur est demandée si la page n'est pas servie par le backend), lien
**« Créer un compte »** pour s'inscrire (en attente d'approbation, §4 ; lien masqué si
l'inscription est fermée). Le jeton de session est mémorisé dans le `localStorage` du
navigateur. Il n'y a plus de connexion par `?token=` dans l'URL.

Une session dure **30 jours** ; à son expiration, après « Se déconnecter », une désactivation
ou une réinitialisation du mot de passe, l'écran de connexion réapparaît.

**Ligne de commande / scripts** : en-tête `X-WMLab-Token` avec un jeton de session `wmls_…`
(réponse de `POST /api/auth/login`), ou le jeton serveur pour un admin sur le serveur (§4) :

```bash
# jq : apt-get install jq (ou brew install jq) ; le mot de passe n'apparaît pas dans l'historique
read -r -s -p "Mot de passe : " PW; echo
SESSION=$(curl -s https://lab.example.com/api/auth/login -H 'Content-Type: application/json' \
  -d "$(jq -n --arg e marie@example.com --arg p "$PW" '{email: $e, password: $p}')" | jq -r .token)
curl -s -H "X-WMLab-Token: $SESSION" https://lab.example.com/api/me
curl -s -X POST -H "X-WMLab-Token: $SESSION" https://lab.example.com/api/auth/logout   # fin de session
```

## 6. Dépôt git distant

Le workspace **est** le dépôt git (racine : `workspace.yaml`, `pyproject.toml`, `wmlab/`,
`projects/`, `lab/`…). `.wmlab/` (données) et `.venv/` sont ignorés (`.gitignore`).

1. **Créer un dépôt privé vide** (GitHub / GitLab / Gitea) : par exemple `equipe/wmlab`.
2. **Sur votre poste** (celui qui possède déjà l'historique) :
   ```bash
   cd ~/Documents/"studio AI"
   git remote add origin git@github.com:equipe/wmlab.git
   git push -u origin main
   ```
3. **Accès du serveur** : la clé publique du serveur (`/home/wmlab/.ssh/id_ed25519.pub`,
   affichée par le script) déposée comme **clé de déploiement en écriture** (§3, étape 5).
   Alternative moins bonne : URL HTTPS avec un jeton d'accès dans l'URL
   (`https://<token>@github.com/equipe/wmlab.git`) — le jeton finit dans `.git/config` en
   clair ; réserver aux tests.
4. **Les autres membres** qui développent clonent le dépôt (`git clone
   git@github.com:equipe/wmlab.git`), installent leur environnement local (README §1) et
   poussent sur `main` (ou sur des branches, lancées par un admin en indiquant leur nom dans
   « Révision git »). Un compte `member` du serveur ne donne aucun accès au code : l'accès au
   dépôt git se gère à part, chez l'hébergeur git.
5. **Le serveur** tire la branche `hosted.git_branch` en `--ff-only` : ne jamais réécrire
   l'historique de cette branche (`push --force`), sinon l'auto-pull s'arrête (état visible
   dans Réglages → Mode hébergé, `last_pull_error`) et il faut réaligner à la main :
   `sudo -u wmlab git -C /opt/wmlab reset --hard origin/main`.

Modifications de YAML faites **depuis l'app** en mode hébergé : commit
`app(<user>): <chemin>` + push par le serveur ; en cas d'échec de push, le commit reste local
(`pushed: false`, avertissement dans l'éditeur), le serveur retente `pull --rebase` + push à
l'enregistrement suivant.

## 7. Machines GPU

### Clé SSH du serveur

Le serveur se connecte aux machines GPU en `root@<ip>` avec **sa** clé
(`/home/wmlab/.ssh/id_ed25519`, jamais de mot de passe ni de clé dans `hosts.yaml`). Déposer la
clé publique :

- **Une fois pour toutes** : console Scaleway → *Projet → Identifiants → Clés SSH → Ajouter*
  (contenu de `id_ed25519.pub`). Toute instance créée ensuite dans le projet l'accepte.
- **Machine déjà créée** : `ssh root@<ip-gpu> 'echo "<clé publique>" >> ~/.ssh/authorized_keys'`
  depuis votre poste.
- Test depuis le serveur : `sudo -u wmlab -H ssh root@<ip-gpu> nvidia-smi` (la première
  connexion mémorise l'empreinte : `StrictHostKeyChecking accept-new` dans
  `/home/wmlab/.ssh/config`).

### Cycle de vie d'une machine (page **Machines** de l'app, connectée au serveur)

Toutes les actions ci-dessous (déclarer, préparer, lancer, rapatrier, éteindre, retirer) sont
réservées aux comptes **admin** ; un `member` voit les machines, les runs et les coûts.

1. **Créer l'instance GPU** (console Scaleway : L40S, H100… image « GPU OS », clé SSH du
   projet). Noter l'IP, la zone et l'identifiant de l'instance.
2. **Déclarer la machine** (admin) : nom, `ssh_target: root@<ip>`, `workdir: /root/wmlab`,
   `data_dir: /root/wmlab-data`, `python: .venv/bin/python`, prix €/h réel, GPU,
   `max_concurrent_jobs` ; facultatif `scaleway: {zone: fr-par-2, server_id: <uuid>}` pour
   piloter l'instance depuis l'app (extinction / suppression, nécessite `SCW_SECRET_KEY`).
3. **Tester** (SSH, GPU, disque) puis **« Préparer »** : le serveur exporte le commit courant
   dans `<workdir>/code/<sha>/` et y exécute `scripts/remote_bootstrap.sh` (uv, `uv sync
   --extra train` dans le venv partagé `<workdir>/.venv`, dossier de données). Première
   préparation : plusieurs minutes (torch + CUDA).
4. **Données** : télécharger les datasets directement sur la machine et renseigner
   `datasets/<nom>.yaml → paths.<machine>` (README §4.5), commit + push.
5. **Lancer** : « Nouveau lancement » → expérience / plan → machine → **Révision git**
   (pré-remplie `main @ abc1234` ; branche ou sha poussé) → durée max → estimation € →
   Lancer. Le serveur résout le sha, l'exporte, le synchronise sur la machine et lance
   `wmlab train|hpo|eval` **dans ce code exact** (`run.json.env/git.json` : `sha`,
   `dirty: false`).
6. **Pendant le run** : synchronisation périodique des dossiers de runs (sans `.pt`) ; l'app
   est accessible à toute l'équipe, le Mac de l'auteur peut être éteint.
7. **Fin de job** : synchronisation **complète** (checkpoints inclus) et vérification
   (`Job.retrieval: complete | partial | failed`). `partial` = avertissement sur le job : relancer
   la synchronisation (bouton du job) ou le rapatriement de la machine.
8. **« Rapatrier les checkpoints »** (carte de la machine) : job `retrieve` qui rapatrie **tout**
   ce qui est sur la machine (y compris les runs lancés à la main), vérifie tailles et index,
   puis réindexe. L'inventaire de la carte (runs, checkpoints non rapatriés en nombre et Go,
   jobs actifs) doit tomber à zéro.
9. **« Retirer la machine »** (admin) : **refusé (409)** tant qu'un job est actif, que
   `unretrieved_checkpoints > 0` ou `unindexed_runs > 0`, ou que la machine est injoignable
   (sauf `force`, journalisé). Le dialogue récapitule ce qui a été vérifié et propose « supprimer
   aussi l'instance Scaleway » si `scaleway` est configuré et `SCW_SECRET_KEY` présente.
   L'entrée reste dans `hosts.yaml` avec `decommissioned_at` (historique des coûts), et n'est
   plus proposée au lancement.
10. **Supprimer l'instance** (si ce n'est pas fait par l'app) : console Scaleway → Instance →
    Supprimer, **avec ses volumes**. Entre deux sessions, « Éteindre l'instance » (`poweroff`)
    arrête la facturation GPU en conservant le disque (facturation du volume seulement).

Règle d'équipe : **on ne crée pas de machine GPU sans plan de rapatriement** (qui rapatrie,
quand, et vérification de l'inventaire avant suppression). Une instance supprimée avec des
checkpoints non rapatriés, c'est de l'argent GPU perdu.

## 8. Sauvegardes

`deploy/backup.sh` (installé en `/usr/local/bin/wmlab-backup`, lancé chaque nuit à 03:30 par
`wmlab-backup.timer` sous l'utilisateur `wmlab`) sauvegarde `.wmlab/` (runs, études,
`hosts.yaml`, `users.yaml`, `sessions.json`, jobs), sans `index.sqlite` (reconstruit par
`wmlab index`), `cache/` ni `logs/`. `users.yaml` ne contient que des hachés **scrypt** des
mots de passe et `sessions.json` que des **empreintes** SHA-256 des jetons de session (aucun
secret utilisable tel quel), mais l'archive reste confidentielle (e-mails, poids des
modèles) :

| Mode | Condition | Ce qui est fait |
|---|---|---|
| **Object Storage** (recommandé) | `rclone` installé et remote `wmlab-backup:` configuré | `rclone copy` incrémental de `.wmlab/` (checkpoints `.pt` **compris**) vers `wmlab-backup:<bucket>/wmlab/` ; rien n'est jamais effacé côté bucket |
| **Local** (repli) | sinon | `tar.zst` de `.wmlab/` **sans** les `.pt` (`WMLAB_BACKUP_CHECKPOINTS=1` pour les inclure) dans `/var/backups/wmlab`, rotation 14 jours — même disque : filet de sécurité, pas une sauvegarde hors site |

Mettre en place le mode Object Storage :

```bash
apt-get install -y rclone                     # ou la version officielle : https://rclone.org/install/
# Console Scaleway : créer le bucket « wmlab-backup » (fr-par) et une clé API (Object Storage)
sudo -u wmlab -H rclone config                # nouveau remote : nom wmlab-backup, type s3,
                                              # provider Scaleway, access/secret key, region fr-par,
                                              # endpoint s3.fr-par.scw.cloud
# bucket d'un autre nom : WMLAB_BACKUP_BUCKET=<nom> dans /etc/wmlab/env
systemctl start wmlab-backup.service && journalctl -u wmlab-backup -n 30   # test immédiat
systemctl list-timers wmlab-backup.timer
```

Restauration : `rclone copy wmlab-backup:<bucket>/wmlab/ /opt/wmlab/.wmlab/` (ou
`tar --zstd -xf /var/backups/wmlab/wmlab-<date>.tar.zst -C /opt/wmlab`), `chown -R
wmlab:wmlab /opt/wmlab/.wmlab`, `chmod 0600 /opt/wmlab/.wmlab/users.yaml
/opt/wmlab/.wmlab/sessions.json`, puis `wmlab index` et `systemctl restart wmlab-server`.
Comptes et sessions reviennent à l'état de la sauvegarde : vérifier `wmlab users list` (un
compte désactivé depuis serait de nouveau actif : `wmlab users disable EMAIL`).
Sauvegarder aussi `/etc/wmlab/env` (jeton serveur, clé Scaleway) dans le gestionnaire de
secrets de l'équipe — il n'est pas dans l'archive. Coût indicatif Object Storage Scaleway :
≈ 16 € HT/To/mois (juin 2026).

## 9. Mettre à jour le serveur

Les YAML (`projects/`, `benchmarks/`, `datasets/`) poussés sur la branche suivie arrivent
seuls (auto-pull ; bouton **Tirer maintenant** dans Réglages → Mode hébergé). Pour une
modification du **package `wmlab/`**, des **plugins `lab/`**, des **dépendances** (`uv.lock`)
ou du **frontend** (`desktop/`), le serveur doit être reconstruit et redémarré — le plus
simple est de relancer le script d'installation (idempotent) :

```bash
sudo bash /opt/wmlab/deploy/install_server.sh --branch main --dir /opt/wmlab --domain lab.example.com \
  --admin justin --admin-email justin@example.com
```

À la main, l'équivalent :

```bash
sudo -u wmlab -H git -C /opt/wmlab pull --ff-only
sudo -u wmlab -H env UV_PROJECT_ENVIRONMENT=/opt/wmlab/.venv uv --directory /opt/wmlab sync --extra server --extra train
sudo -u wmlab -H env ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm --prefix /opt/wmlab/desktop ci && sudo -u wmlab -H npm --prefix /opt/wmlab/desktop run build
sudo systemctl restart wmlab-server
```

Le redémarrage est sans danger pour les jobs en cours (`KillMode=process` : les processus
lancés par le backend survivent et sont rattachés au redémarrage). Les machines GPU, elles,
exécutent toujours le commit choisi au lancement : pas de mise à jour à faire dessus, hormis
« Préparer » à nouveau si `uv.lock` a changé (fait automatiquement au lancement suivant,
marqueur `<workdir>/.venv/.wmlab-lock`).

## 10. Dépannage

| Symptôme | Vérification / remède |
|---|---|
| L'app ne répond pas | `systemctl status wmlab-server` ; `journalctl -u wmlab-server -n 100 -f` ; `curl -s http://127.0.0.1:8765/api/health` sur le serveur |
| Le service redémarre en boucle | erreur Python au démarrage dans le journal (dépendances : relancer `uv sync` ; `workspace.yaml` invalide : `wmlab validate`) ; port 8765 occupé : `ss -ltnp \| grep 8765` |
| `https://…` en erreur de certificat / timeout | DNS non propagé (`dig +short lab.example.com`), ports 80/443 fermés dans le groupe de sécurité, `journalctl -u caddy -n 50` ; `caddy validate --config /etc/caddy/Caddyfile` |
| **401** à la connexion « E-mail ou mot de passe incorrect » | e-mail mal saisi (comparé en minuscules) ou mot de passe faux ; mot de passe oublié : un admin lance `wmlab users reset-password EMAIL` (§4) |
| **401** en cours d'utilisation (retour à l'écran de connexion) | session expirée (30 jours), révoquée (« Se déconnecter », changement ou réinitialisation du mot de passe, compte désactivé) : se reconnecter. En CLI : en-tête `X-WMLab-Token` absent ou jeton invalide |
| **403** « compte en attente » | inscription non encore approuvée : un admin l'approuve (Réglages → Utilisateurs, ou `wmlab users approve EMAIL --role member`) |
| **403** « compte désactivé » | `wmlab users enable EMAIL` si c'est une erreur |
| **403** « Action réservée aux administrateurs » | le compte est `member` (lecture seule, sans code) : demander l'action à un admin, ou `wmlab users role EMAIL admin` |
| **429** « trop de tentatives » | limitation de débit (10 échecs de connexion par e-mail ou 30 par IP en 15 min, 5 inscriptions par IP et par heure) : attendre la durée de `Retry-After` ; vérifier le journal d'audit (§11) en cas de doute |
| Plus aucun admin utilisable (mot de passe perdu, compte désactivé) | sur le serveur : `wmlab users reset-password EMAIL` ou `wmlab users enable EMAIL` (la CLI agit directement sur `users.yaml`), ou jeton serveur (§4) ; le dernier admin actif ne peut être ni rétrogradé ni désactivé |
| Auto-pull arrêté (`last_pull_error`) | arbre sale (`sudo -u wmlab git -C /opt/wmlab status`) : un fichier modifié sur le serveur — `git stash` ou commit ; historique réécrit : `git reset --hard origin/main` ; `workspace.yaml` modifié en amont alors qu'il est en `skip-worktree` local : `git update-index --no-skip-worktree workspace.yaml`, résoudre, repasser `deploy/patch_workspace.py` puis `--skip-worktree` |
| Push de l'app refusé (`pushed: false`) | clé de déploiement sans droit d'écriture, ou branche protégée : autoriser l'écriture / le push direct pour la clé |
| Machine GPU « injoignable » | `sudo -u wmlab -H ssh -o BatchMode=yes root@<ip> true` ; clé du serveur absente de `authorized_keys` (§7) ; instance éteinte |
| Rapatriement `partial` | disque du serveur plein (`df -h /opt/wmlab`) ; connexion coupée : relancer « Rapatrier les checkpoints » (rsync reprend) ; `missing` dans le détail du job |
| « Retirer la machine » refusé | c'est voulu : lancer le rapatriement, attendre la fin des jobs ; machine détruite avant rapatriement : `force` (admin, journalisé) — les checkpoints sont perdus |
| Disque plein | `du -sh /opt/wmlab/.wmlab/runs/* \| sort -h` ; agrandir le volume ; ne jamais supprimer un run à la main (déplacer vers le bucket puis archiver) |
| Sauvegarde en échec | `journalctl -u wmlab-backup` ; `sudo -u wmlab -H rclone lsd wmlab-backup:` (clé API, bucket) |

Reconstruire l'index après une restauration ou un doute : `wmlab index` (alias §4).

## 11. Sécurité

- **Comptes et mots de passe** : un compte par personne ; mots de passe de 12 à 256 caractères
  (longueur plutôt que complexité, refusés s'ils égalent l'e-mail ou le nom), hachés **scrypt**
  (`users.yaml`, `0600`) ; jamais transmis ailleurs qu'à `POST /api/auth/login` en HTTPS, ni
  mémorisés par l'app. Mots de passe temporaires (création, réinitialisation) affichés une seule
  fois et à changer à la première connexion.
- **Sessions** : jeton `wmls_…` aléatoire, seul son SHA-256 est stocké (`sessions.json`,
  `0600`) ; durée **30 jours** ; révoquées par « Se déconnecter », un changement ou une
  réinitialisation de mot de passe, une désactivation. Rôle et état relus à chaque requête :
  une rétrogradation ou une désactivation est immédiate.
- **Rôles** : `member` = lecture seule, sans accès au code d'entraînement ni aux architectures ;
  `admin` = tout (§4). Le jeton serveur (`/etc/wmlab/env`, `0640 root:wmlab`) est un secours
  admin en ligne de commande : ne pas le distribuer.
- **Limitation de débit** : connexion limitée à 10 échecs par e-mail et 30 par IP sur 15 min,
  inscription à 5 par IP et par heure → `429` + `Retry-After` (IP réelle transmise par Caddy).
- **Journal d'audit** : `/opt/wmlab/.wmlab/logs/server.log`, lignes `audit:` — connexions
  réussies et échouées (e-mail, IP), inscriptions, créations, approbations, changements de rôle,
  désactivations, réinitialisations, changements de mot de passe, déconnexions, refus `403`.
  Jamais de mot de passe ni de jeton. Lecture : `grep 'audit:' /opt/wmlab/.wmlab/logs/server.log
  | tail -50`.
- **Fermer l'inscription** une fois l'équipe constituée : Réglages → Mode hébergé →
  inscription fermée (admin), ou sur le serveur :
  ```bash
  sudo -u wmlab -H /opt/wmlab/.venv/bin/python /opt/wmlab/deploy/patch_workspace.py \
    --file /opt/wmlab/workspace.yaml --allow-registration false
  sudo systemctl restart wmlab-server
  ```
  (`hosted.allow_registration: false` : `POST /api/auth/register` répond `403`, le lien
  « Créer un compte » disparaît ; les comptes restent créés par un admin, §4).
- **HTTPS obligatoire** dès que le serveur est accessible d'Internet : le backend n'écoute que
  `127.0.0.1:8765` ; seul Caddy est exposé (80/443) avec un certificat Let's Encrypt renouvelé
  automatiquement. Groupe de sécurité Scaleway : 22, 80, 443 entrants uniquement.
- **En-têtes Caddy** (`deploy/Caddyfile`) : HSTS avec `includeSubDomains` (1 an),
  `Content-Security-Policy` (scripts, styles, images, médias et connexions de la même origine
  seulement ; `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options:
  nosniff`, `Referrer-Policy: no-referrer`, `Permissions-Policy` (caméra, micro, géolocalisation
  refusés) ; corps de requête limité à 8 Mo. Journal d'accès `/var/log/caddy/wmlab-access.log`
  (JSON, rotation, 30 jours) **sans** la chaîne de requête des URL (`?token=` des
  téléchargements) ni l'en-tête `X-WMLab-Token`. Le backend ajoute les mêmes en-têtes (hors
  HSTS) à l'interface, y compris en accès par tunnel SSH.
- **Durcissement systemd** (`deploy/wmlab-server.service`) : compte `wmlab` sans privilège ni
  capacité (`CapabilityBoundingSet=`, `NoNewPrivileges`), système de fichiers en lecture seule
  (`ProtectSystem=strict`) sauf `/opt/wmlab` et `/home/wmlab/.ssh` (`ReadWritePaths`) et le cache
  `/var/cache/wmlab`, `/tmp` privé, `/dev` minimal, noyau / cgroups / horloge / nom d'hôte
  protégés, familles réseau `AF_UNIX AF_INET AF_INET6`, filtre d'appels système
  `@system-service`, `UMask=0027`. Vérification : `systemd-analyze security wmlab-server`
  (le réseau reste nécessaire : git, ssh, rsync, API Scaleway). Un `data_dir` hors de
  `/opt/wmlab` doit être ajouté à `ReadWritePaths` (puis `systemctl daemon-reload &&
  systemctl restart wmlab-server`).
- **`SCW_SECRET_KEY`** (clé IAM) uniquement dans `/etc/wmlab/env`, avec une politique IAM
  limitée aux Instances du projet ; jamais dans `hosts.yaml`, les logs, l'API ni le dépôt.
  Sans elle, l'app ne peut ni éteindre ni supprimer une instance (retrait « logique » seul).
- **`hosts.yaml` ne contient aucun secret** : cibles SSH, chemins, prix. L'accès aux machines
  GPU passe par la clé du serveur (`/home/wmlab/.ssh/id_ed25519`, `0600`), clé de
  déploiement du dépôt limitée à ce dépôt. `StrictHostKeyChecking accept-new` : l'empreinte
  d'une machine est mémorisée à la première connexion puis vérifiée.
- **Dépôt privé** : les YAML décrivent vos schémas ; les commits de l'app portent le nom du
  compte, jamais son e-mail (`--author "<user> <<user>@wmlab.local>"`). Un `member` n'a pas
  accès au code via le serveur : ne lui donner accès au dépôt git que s'il doit en avoir.
- **Sauvegardes** : le bucket contient les checkpoints (poids des modèles), `users.yaml`
  (e-mails et hachés scrypt) et `sessions.json` (empreintes) : bucket privé, clé API dédiée en
  lecture/écriture sur ce seul bucket.
- **Mises à jour système** : `unattended-upgrades` est actif par défaut sur Ubuntu ;
  redémarrer le serveur après une mise à jour du noyau (`/var/run/reboot-required`).

- **Frontière machines GPU → serveur** : une machine GPU exécute le commit choisi par un admin ;
  tout ce qui en revient est traité comme non fiable : rsync avec `--safe-links` (liens
  symboliques vers l'extérieur ignorés), identifiants de runs / études / checkpoints validés
  avant tout chemin, fichiers de run servis en pièce jointe (jamais comme page), seuls les
  `samples/` sont lisibles par un membre. Un fichier `notes.md` ou YAML poussé sous forme de
  lien symbolique dans le dépôt n'est jamais lu par le serveur.
- **Racine des données** : `WMLAB_DATA=/opt/wmlab/.wmlab` dans `/etc/wmlab/env` (posé par le
  script) ; `workspace.yaml:data_dir`, versionné, ne peut pas la déplacer.
- **Dépôt git = chaîne d'approvisionnement** : tout ce qui est poussé sur la branche suivie est
  exécuté par le serveur (après redémarrage pour `wmlab/` et `lab/`) et par les machines GPU.
  Protéger la branche (relecture obligatoire, pas de push direct pour les non-admins) et réserver
  la clé de déploiement à ce dépôt.
