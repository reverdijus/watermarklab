# Audit de sécurité de Watermark Lab — état après remédiation (2 octobre 2026)

Audit réalisé sur l'instantané du dépôt au commit `8b91a0d` (état avant ce travail), par
16 agents d'analyse (8 surfaces × 2 tours), déduplication sémantique, puis vérification
adversariale indépendante de chaque constat (3 verdicts pour les constats critiques à moyens,
1 pour les mineurs) et critique de complétude : 146 constats bruts → 70 uniques → **64 confirmés**,
10 écartés. Le rapport d'audit lui-même (section « Rapport d'audit », inchangé) décrit l'état
initial ; la section ci-dessous indique ce qui a été corrigé dans la foulée et ce qui reste.

## État des corrections

Bilan : 27 corrigés, 8 atténués, 4 acceptés (modèle de confiance),
3 relevant d'une procédure, 1 non applicable, **21 à faire** (aucun critique ni grave parmi eux, sauf
SEC-09 qui concerne une machine GPU compromise).

Ce qui a été livré avec cet audit (contrat : `ARCHITECTURE.md` §9 et §12.2) :

- **Comptes e-mail + mot de passe** (scrypt), inscription en attente d'approbation, mot de passe
  temporaire à changer, sessions de 30 jours révocables, limitation de débit, journal d'audit.
- **Rôle `member` en lecture seule, sans accès au code** : refus par défaut de toute écriture et
  des routes qui décrivent le code, l'infrastructure ou les journaux ; réponses expurgées ;
  interface desktop et navigateur adaptées.
- **Durcissement** : jeton en URL limité aux téléchargements, fichiers de run jamais servis comme
  pages, en-têtes de sécurité (CSP…), corps de requêtes bornés, erreurs génériques en mode hébergé,
  liens symboliques jamais suivis, `rsync --safe-links`, identifiants venant des machines GPU
  validés, secrets non transmis aux jobs, unité systemd confinée, Caddy avec journal sans jeton,
  racine des données épinglée.

| Constat | Statut | Détail |
|---|---|---|
| SEC-01 | corrigé | rôle member en lecture seule (refus par défaut dans le middleware) ; `PUT /api/files` et `GET /api/registry` réservés aux admins. Un admin peut toujours déposer un plugin `lab/*.py` chargé à chaud : c'est le modèle de confiance (les admins ont déjà la main sur le dépôt). |
| SEC-02 | procédure | non corrigeable dans le code : tout ce qui est poussé sur la branche suivie est exécuté. Protéger la branche `main` sur GitHub (relecture obligatoire, pas de push direct pour les non-admins) ; documenté dans `deploy/README.md` §11. |
| SEC-03 | corrigé | politique « GET seulement » pour `member`, toute écriture ⇒ 403, journalisée. |
| SEC-04 | corrigé | lancement, arrêt, annulation, préparation, rapatriement, inventaire et instance Scaleway réservés aux admins. |
| SEC-05 | corrigé | `/api/files`, `/api/registry`, `/api/runs/{id}/logs`, `/api/git`, `/api/settings` réservés aux admins ; réponses expurgées (spec, overrides, params, plan, env, erreurs) ; fichiers de run limités à `samples/` pour un membre. |
| SEC-06 | atténué | `desktop/dist` n'est pas versionné et CSP + `X-Frame-Options` sont posés par le backend et Caddy ; le reste relève de la protection de la branche (SEC-02). |
| SEC-07 | corrigé | `rsync --safe-links` sur tous les rapatriements : un lien symbolique créé sur la machine GPU n'est jamais recréé sur le serveur. |
| SEC-08 | corrigé | identifiants de runs, d'études et de checkpoints venant d'une machine validés (`SAFE_ID`, sans `..`) avant tout chemin. |
| SEC-09 | à faire | le rapatriement ne vérifie pas que `run.json.host` correspond à la machine source. Correction proposée : refuser d'écraser un run local dont `host` diffère. |
| SEC-10 | à faire | sur le serveur central, retirer la machine `local` de `hosts.yaml` (ou la déclarer avec `max_concurrent_jobs: 0`) ; les lancements sont désormais réservés aux admins. |
| SEC-11 | documenté | procédure de dépannage dans `deploy/README.md` §10 ; pas de changement de code. |
| SEC-12 | corrigé | l'indexeur force `run_id` au nom du dossier du run pour chaque `eval/*.json`. |
| SEC-13 | non applicable | aucun fichier `.env*` n'est versionné (vérifié avec `git ls-files`) ; `.env` et `.env.*` sont maintenant dans `.gitignore`. |
| SEC-14 | corrigé | `GET /api/runs/{id}/file` : types média sûrs seulement en ligne, tout le reste en pièce jointe `application/octet-stream`, `nosniff`, CSP `sandbox`. |
| SEC-15 | corrigé | le serveur ne suit plus aucun lien symbolique du dépôt (projets, YAML, notes) et ignore les fichiers > 1 Mo. |
| SEC-16 | corrigé | `?token=` accepté seulement sur les deux routes de téléchargement ; sessions expirables, révocables ; plus de connexion par lien. |
| SEC-17 | corrigé | journal d'audit `audit:` (connexions, échecs, actions admin, refus) ; journal d'accès Caddy JSON sans jeton ni chaîne de requête. Reste : la rotation 4 × 5 Mo du journal du serveur (prévoir journald ou rsyslog pour une conservation longue). |
| SEC-18 | accepté | l'inventaire fait confiance à `index.json` de la machine GPU, qui exécute le code choisi par un admin ; le retrait exige un rapatriement complet et `force` est journalisé. |
| SEC-19 | accepté | venv partagé sur des machines GPU éphémères ; détruire l'instance plutôt que la réutiliser entre projets sensibles. |
| SEC-20 | accepté | `run.json` est écrit par le code lancé par un admin sur la machine GPU. |
| SEC-21 | atténué | YAML > 1 Mo ignorés, `RecursionError` absorbée ; les bombes d'alias restent possibles mais seuls les admins (ou un push git) peuvent écrire des YAML. |
| SEC-22 | corrigé | corps `/api/*` limités à 4 Mo (413), `Content-Length` obligatoire (411), 8 Mo côté Caddy. |
| SEC-23 | atténué | `POST /api/validate` réservé aux admins ; fichiers limités à 1 Mo. |
| SEC-24 | accepté | le volume rapatrié dépend des machines GPU administrées ; surveiller `df -h` (dépannage §10). |
| SEC-25 | corrigé | `WMLAB_DATA=/opt/wmlab/.wmlab` posé dans `/etc/wmlab/env` ; un `workspace.yaml` invalide conserve les derniers réglages valides au lieu de rouvrir l'inscription. |
| SEC-26 | à faire | `install_server.sh` : écrire dans `~wmlab/.ssh` sans suivre les liens symboliques et ne rien exécuter en root depuis un fichier possédé par `wmlab`. |
| SEC-27 | à faire | séparer la clé de déploiement git (écriture, pour l'auto-commit) de la clé des machines GPU ; vérifier les empreintes SSH des instances. |
| SEC-28 | à faire | bucket avec versionnage / verrou d'objets, clé API rclone limitée (pas de suppression), `rclone crypt` pour chiffrer les checkpoints. |
| SEC-29 | à faire | faible : auto-commit avec `git add -- <chemin>` littéral et refus des noms de fichiers contenant un retour à la ligne. |
| SEC-30 | corrigé | machines expurgées pour un membre, `/api/git` et `/api/settings` admin, chemin du workspace réservé aux admins. |
| SEC-31 | corrigé | interface en lecture seule pour un membre (`usePermissions`), pages de code bloquées. |
| SEC-32 | atténué | jeton serveur de secours uniquement (ligne de commande), longueur minimale hors loopback ; rotation manuelle documentée. |
| SEC-33 | corrigé | sessions de 30 jours, verrou inter-process `fcntl`, `users.yaml` illisible ⇒ 503 sans jamais réécrire le fichier. |
| SEC-34 | corrigé | limitation par (e-mail, IP), par IP et globale par e-mail, réservation atomique avant scrypt ; limite sur le changement de mot de passe. |
| SEC-35 | corrigé | CSP, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, HSTS avec `includeSubDomains` (backend + Caddy). |
| SEC-36 | corrigé | les jobs locaux n'héritent plus de `WMLAB_TOKEN` ni de `SCW_SECRET_KEY`. |
| SEC-37 | à faire | valider la spec dans l'export du commit lancé plutôt que dans le checkout du serveur. |
| SEC-38 | à faire | refuser `init_from` passé par `overrides` et tout chemin absolu ; admins seulement désormais. |
| SEC-39 | à faire | vérifier que l'IP de l'instance Scaleway correspond à `ssh_target` avant suppression. |
| SEC-40 | à faire | faible : confiner le champ `experiment` d'un plan HPO au workspace. |
| SEC-41 | à faire | cache du front de Pareto (performances). |
| SEC-42 | corrigé | `max_points` ≤ 5 000, `tail` ≤ 20 000, 20 runs par comparaison. |
| SEC-43 | à faire | exécuter SSH / rsync / git hors du pool de threads de requêtes (ou pool dédié). |
| SEC-44 | atténué | admins seulement ; pas encore de plafond de jobs `setup` en file. |
| SEC-45 | à faire | exclure les téléchargements binaires de la compression. |
| SEC-46 | à faire | pagination de `GET /api/jobs`. |
| SEC-47 | corrigé | `GET /api/registry` (et `reload`) réservé aux admins. |
| SEC-48 | à faire | documenter `tar --no-same-owner --no-same-permissions` à la restauration. |
| SEC-49 | procédure | groupes de sécurité Scaleway : SSH restreint aux IP de l'équipe et du serveur. |
| SEC-50 | corrigé | unité systemd durcie (`ProtectSystem=strict`, filtres d'appels système, capacités vides…) ; `KillMode=process` conservé volontairement pour les jobs. |
| SEC-51 | à faire | épingler et vérifier les empreintes des installateurs (uv : `UV_INSTALLER_SHA256` existe déjà, à renseigner). |
| SEC-52 | corrigé | `.wmlab` en 0750, `UMask=0027`, `users.yaml` et `sessions.json` en 0600. |
| SEC-53 | à faire | compte de service avec `/usr/sbin/nologin` et `~/.ssh` possédé par root. |
| SEC-54 | à faire | `admin off` dans le Caddyfile si le rechargement par signal suffit. |
| SEC-55 | corrigé | URL du remote renvoyée sans identifiants ; `env/git.json` d'un run n'est visible que des admins. |
| SEC-56 | à faire | renseigner les sha256 manquants des poids officiels. |
| SEC-57 | atténué | `http://` accepté seulement pour les adresses loopback ; jeton chiffré par `safeStorage` ; le repli en clair reste quand le trousseau est indisponible. |
| SEC-58 | à faire | Electron : refuser toute navigation hors `file://` du build et des origines du backend. |
| SEC-59 | corrigé | quantificateurs bornés dans les expressions régulières du rendu Markdown ; lignes > 20 000 caractères rendues en texte brut. |
| SEC-60 | corrigé | comptes e-mail + mot de passe, inscription en attente d'approbation, rôle membre en lecture seule effectif. |
| SEC-61 | atténué | scopes WebSocket fermés ; l'admin implicite sans jeton n'existe que pour les tests (`create_app(token=None)`), jamais avec `wmlab server`. |
| SEC-62 | corrigé | en mode hébergé, 404 et 500 génériques (référence vers `server.log`). |
| SEC-63 | à faire | faible : comparer l'origine (`new URL(...).origin`) plutôt qu'un préfixe de chaîne. |
| SEC-64 | atténué | un membre ne télécharge plus que `samples/` ; `hardware` reste visible dans le détail d'un run. |

**Priorités restantes avant mise en ligne** : SEC-02 et SEC-49 (procédures GitHub / Scaleway :
protection de la branche, groupes de sécurité), SEC-10 (retirer la machine `local` du serveur),
SEC-27 et SEC-28 (clés et sauvegardes), puis SEC-09, SEC-26, SEC-53 dans la semaine.

**Vérifications effectuées sur le code corrigé** : suite Python complète (`pytest`), build et
tests du desktop, `wmlab validate`, `deploy/tests_dry_run.sh`, relecture adversariale du
nouveau code d'authentification (81 agents, 31 constats corrigés avant livraison), parcours
navigateur (connexion, inscription, membre en lecture seule, approbation par un admin).
À vérifier sur le serveur Ubuntu réel : `systemd-analyze security wmlab-server`, `caddy validate`,
rapatriement réel depuis une machine GPU sous le durcissement systemd (`deploy/README.md` §11).

---

# Rapport d'audit (état initial, commit 8b91a0d)

# Audit de sécurité — Watermark Lab (wmlab)

Instantané audité : commit `8b91a0d` + modifications locales de docs, en lecture seule.
Cible : serveur public Scaleway (Ubuntu 24.04), Caddy HTTPS devant uvicorn sur `127.0.0.1:8765`, machines GPU pilotées en SSH, app desktop Electron.
Chemins relatifs à la racine du dépôt. La gravité est celle retenue après vérification (majorité/médiane des vérificateurs) ; les désaccords sont signalés.

---

## 1. Résumé exécutif

**Verdict : pas déployable en l'état sur Internet.**

Un attaquant sans compte ne peut presque rien : toutes les routes `/api/*` sauf `/api/health` exigent un jeton (160–192 bits, comparaison en temps constant). Le risque vient de tout porteur d'un jeton **member** : membre malveillant, poste compromis, ou jeton ayant fuité. Avec ce seul jeton il peut obtenir une exécution de code dans le processus serveur (SEC-01), la propager sur `main` puis aux GPU et aux Mac de l'équipe (SEC-02), lancer et piloter des jobs GPU sans plafond (SEC-04), et télécharger tout le code, les specs et les checkpoints (SEC-05).

**Cause racine commune** : le contrat `ARCHITECTURE.md:1251` (« member = tout sauf PUT /api/hosts, PUT /api/settings, décommission/extinction, utilisateurs »), appliqué fidèlement par le code. Il n'y a pas de refus par défaut : `TokenMiddleware` authentifie sans vérifier le rôle, et seules 5 routes appellent `require_admin`. C'est l'inverse de la cible produit (member en lecture seule, sans accès à `lab/`, `wmlab/` ni aux specs).

**Deuxième famille** : le serveur fait confiance à ce qui revient des GPU et de git — liens symboliques rapatriés (SEC-07, SEC-15), traversée de chemin via `trials.jsonl` (SEC-08), écrasement des runs d'autrui (SEC-09), résultats et coûts falsifiables (SEC-12, SEC-20), UI servie depuis le checkout tiré (SEC-06, SEC-13). Ces vecteurs sont atteignables sans compromettre la GPU, car un member peut y faire exécuter le code de son choix en root (`POST /api/jobs` accepte un `git_ref` libre).

**Décompte** : 2 critiques, 7 graves, 19 moyens, 31 faibles, 5 info. L'essentiel des graves et moyens disparaît une fois le rôle member passé en lecture seule et la confiance GPU/git corrigée.

**Conditions minimales avant mise en production** (détail §4, niveau P0) :
1. Refus par défaut dans `TokenMiddleware` : non-admin + méthode hors GET/HEAD → 403. Mettre à jour `ARCHITECTURE.md` §12.2 d'abord.
2. Supprimer le rechargement à chaud de `lab/` et retirer `lab` des racines inscriptibles par HTTP en mode hébergé.
3. Protéger `main` ; limiter l'auto-commit aux YAML de `projects/` sur une branche `app/*`.
4. Désactiver l'hôte `local` en mode hébergé.
5. Durcir le rapatriement : `rsync --no-links`, validation des `run_id`, quarantaine, rapatriement limité aux runs de l'hôte, ouverture des fichiers en `O_NOFOLLOW`.
6. Restreindre `/api/runs/{id}/file` à une liste blanche de types ; construire l'UI hors du checkout.
7. Retirer les secrets de l'environnement des jobs et de la portée de l'utilisateur `wmlab`.
8. Borner la taille des corps et des YAML ; mettre en place un journal d'audit.
9. Ne plus exécuter en root du code appartenant à `wmlab` lors des mises à jour ; corriger le gel du pull (`workspace.yaml` en skip-worktree).

---

## 2. Tableau des constats

| ID | Gravité | Titre | Fichier:ligne |
|---|---|---|---|
| SEC-01 | critique | RCE serveur par un member via `PUT /api/files` sur `lab/*.py` (reload à chaud) | `wmlab/server/app.py:759` |
| SEC-02 | critique | Chaîne d'appro. git : le code du member est poussé sur la branche exécutée | `wmlab/server/app.py:768` |
| SEC-03 | grave | Pas de refus par défaut : le member écrit projets, specs, HPO, fichiers, notes, tags, git pull | `wmlab/server/app.py:193` |
| SEC-04 | grave | Le member lance et pilote jobs et actions GPU | `wmlab/server/app.py:1019` |
| SEC-05 | grave | Fuite au member du code, specs, logs et checkpoints (IP) | `wmlab/server/catalog.py:371` |
| SEC-06 | grave | XSS stockée via `desktop/dist` servi depuis le checkout auto-pullé | `wmlab/server/app.py:521` |
| SEC-07 | grave | Exfiltration de secrets via symlinks rapatriés par `rsync -az` | `wmlab/server/executors/ssh.py:195` |
| SEC-08 | grave | Traversée de chemin via les `run_id` de `trials.jsonl` → écriture arbitraire | `wmlab/server/executors/ssh.py:316` |
| SEC-09 | grave | Le rapatriement écrase les runs/checkpoints d'autres machines | `wmlab/server/jobs.py:579` |
| SEC-10 | moyen | Hôte `local` implicite : jobs des members sur le serveur central | `wmlab/spec/loader.py:463` |
| SEC-11 | moyen | Push sur `workspace.yaml` gèle le pull auto ; jobs sur vieux commit | `deploy/install_server.sh:414` |
| SEC-12 | moyen | Classement falsifiable : un `eval/*.json` se déclare évaluation d'un autre run | `wmlab/server/indexer.py:141` |
| SEC-13 | moyen | `desktop/.env.production` poussé détourne les jetons vers un serveur tiers | `desktop/src/lib/api.ts:139` |
| SEC-14 | moyen | XSS stockée via fichiers de run html/svg servis en ligne | `wmlab/server/app.py:840` |
| SEC-15 | moyen | Symlinks suivis par git contournent le confinement fichiers | `wmlab/server/catalog.py:136` |
| SEC-16 | moyen | Jeton en `?token=` et en localStorage (fuite, fixation) | `wmlab/server/app.py:171` |
| SEC-17 | moyen | Aucune piste d'audit ; journal effaçable | `wmlab/server/app.py:1128` |
| SEC-18 | moyen | Inventaire basé sur l'`index.json` de la GPU → suppression d'instance avec checkpoints non rapatriés | `wmlab/server/inventory.py:156` |
| SEC-19 | moyen | Venv partagé root sur GPU : persistance d'un job malveillant | `wmlab/server/executors/ssh.py:184` |
| SEC-20 | moyen | Budget et attribution des runs falsifiables via `run.json` | `wmlab/tracking/reader.py:63` |
| SEC-21 | moyen | DoS YAML persistant (RecursionError, alias en cascade) | `wmlab/server/catalog.py:55` |
| SEC-22 | moyen | Corps de requête non bornés, pas de quota | `wmlab/server/app.py:742` |
| SEC-23 | moyen | Amplification par `plan_preview` | `wmlab/server/catalog.py:350` |
| SEC-24 | moyen | Aucune borne sur les données rapatriées (disque, mémoire) | `wmlab/server/executors/ssh.py:194` |
| SEC-25 | moyen | Passage member→admin via `workspace.yaml:data_dir` versionné | `wmlab/server/context.py:28` |
| SEC-26 | moyen | Passage wmlab→root via `install_server.sh` | `deploy/install_server.sh:94` |
| SEC-27 | moyen | Clé SSH unique sans passphrase (deploy key + root GPU), TOFU | `deploy/install_server.sh:146` |
| SEC-28 | moyen | Sauvegardes en clair, clé rclone en écriture/suppression, bucket non immuable | `deploy/backup.sh:63` |
| SEC-29 | faible | Auto-commit : pathspec glob, `.gitignore`/`.gitattributes` inscriptibles, retours à la ligne | `wmlab/server/gitops.py:200` |
| SEC-30 | faible | Fuite au member des métadonnées d'infra | `wmlab/server/app.py:948` |
| SEC-31 | faible | Desktop fail-open (rôle non pris en compte) | `desktop/src/components/layout/Sidebar.tsx:96` |
| SEC-32 | faible | `WMLAB_TOKEN` = clé maître partagée, régénérée/journalisée si vide | `wmlab/server/app.py:1098` |
| SEC-33 | faible | Cycle de vie des jetons (pas d'expiration, pas de verrou, lecture silencieuse) | `wmlab/users.py:112` |
| SEC-34 | faible | Pas de rate limiting sur l'authentification | `wmlab/server/app.py:193` |
| SEC-35 | faible | CSP et en-têtes insuffisants | `deploy/Caddyfile:7` |
| SEC-36 | faible | Secrets hérités par les jobs locaux | `wmlab/server/executors/local.py:86` |
| SEC-37 | faible | TOCTOU : spec validée ≠ spec exécutée | `wmlab/server/jobs.py:372` |
| SEC-38 | faible | Overrides contournant la validation d'`init_from` (chemins absolus) | `wmlab/server/jobs.py:230` |
| SEC-39 | faible | Suppression d'instance sans vérifier `server_id` vs `ssh_target` | `wmlab/server/scaleway.py:203` |
| SEC-40 | faible | Champ `experiment` d'un plan HPO hors workspace (oracle) | `wmlab/spec/loader.py:353` |
| SEC-41 | faible | Front de Pareto O(n²) sans cache | `wmlab/server/views.py:327` |
| SEC-42 | faible | Paramètres de lecture non plafonnés, `tail_lines` quadratique | `wmlab/server/app.py:816` |
| SEC-43 | faible | Saturation du pool de threads par routes SSH/rsync/git | `wmlab/server/app.py:979` |
| SEC-44 | faible | Création illimitée de jobs `setup` | `wmlab/server/jobs.py:307` |
| SEC-45 | faible | GZip niveau 9 sur les checkpoints | `wmlab/server/app.py:514` |
| SEC-46 | faible | `GET /api/jobs` sans pagination | `wmlab/server/app.py:1013` |
| SEC-47 | faible | `registry?reload=1` accessible au member | `wmlab/server/app.py:780` |
| SEC-48 | faible | Restauration root d'une archive écrite par wmlab (setuid) | `deploy/README.md:279` |
| SEC-49 | faible | SSH root GPU/serveur joignable depuis Internet | `deploy/README.md:203` |
| SEC-50 | faible | Unités systemd sans sandboxing, `KillMode=process` | `deploy/wmlab-server.service:26` |
| SEC-51 | faible | Chaîne d'appro. à l'installation (uv, NodeSource, GPG Caddy, npm) | `deploy/install_server.sh:192` |
| SEC-52 | faible | `.wmlab` en 0755, pas d'UMask | `deploy/install_server.sh:266` |
| SEC-53 | faible | Compte `wmlab` avec `/bin/bash` et `~/.ssh` inscriptible | `deploy/install_server.sh:138` |
| SEC-54 | faible | API admin de Caddy sur `localhost:2019` sans auth | `deploy/Caddyfile:5` |
| SEC-55 | faible | URL du remote git (identifiants) exposée | `wmlab/server/gitops.py:126` |
| SEC-56 | faible | Intégrité des poids officiels des baselines | `lab/models/baseline_trustmark.py:34` |
| SEC-57 | faible | Desktop : `http://` accepté, `config.json` sans 0600, jeton clair en repli | `desktop/electron/main.cjs:126` |
| SEC-58 | faible | Electron : navigation `file://` libre, jeton donné à toute page | `desktop/electron/main.cjs:471` |
| SEC-59 | faible | ReDoS cubique dans le rendu Markdown | `desktop/src/components/ui/Markdown.tsx:12` |
| SEC-60 | info | Écart cible produit : pas d'auth e-mail/mdp ni rôle lecture seule, CORS `*` | `wmlab/users.py:35` |
| SEC-61 | info | Fail-open d'autorisation (admin par défaut si identité absente ; scopes non HTTP) | `wmlab/server/app.py:211` |
| SEC-62 | info | Les erreurs 500/404 fuient type et texte de l'exception | `wmlab/server/app.py:580` |
| SEC-63 | info | Comparaison par préfixe de chaîne avec l'origine backend (Electron) | `desktop/electron/main.cjs:542` |
| SEC-64 | info | `pip_freeze.txt`/`hardware.json` téléchargeables par un member | `wmlab/tracking/env.py:158` |

---

## 3. Détail des constats

### Critiques

**SEC-01 — RCE serveur via `PUT /api/files` sur `lab/*.py`** (`app.py:759`, vérif. 3/3 critique)
Description : la route (app.py:738) n'appelle pas `require_admin` ; `WRITABLE_ROOTS` inclut `lab` (catalog.py:41) ; la validation se limite à `compile()` (catalog.py:445). Après écriture d'un `.py` valide, app.py:759-761 appelle `state.plugins(reload=True)` → `importlib.import_module` (registry.py:434), qui exécute le module dans le processus uvicorn pendant la requête. `GET /api/registry?reload=true` (app.py:780, sans rôle) le réexécute.
Scénario : un member écrit un plugin dont le code de niveau module lit l'environnement et les fichiers du processus serveur. Ce processus charge `EnvironmentFile=/etc/wmlab/env` (jeton serveur admin, `SCW_SECRET_KEY`) et peut lire `~/.ssh/id_ed25519` (root sur toutes les GPU, clé de déploiement), les checkpoints et `users.yaml`.
Preuve : PoC avec jeton member — `PUT 200 {'valid': True}`, fichier marqueur créé à l'import, réexécution via `registry?reload=true`. Conforme à ARCHITECTURE.md:1251 : choix de conception, pas régression.
Correction : `require_admin` sur `write_file` et sur `registry` quand `reload=True` ; en mode hébergé, retirer `lab` des racines inscriptibles par HTTP ; supprimer app.py:759-764 et le paramètre `reload`. N'importer `lab/` qu'au démarrage, idéalement dans un sous-processus jetable au `os.environ` purgé. Test de non-régression : member → 403, aucun import.

**SEC-02 — Chaîne d'approvisionnement git** (`app.py:768`, proposé grave → vérif. critique/critique/grave, retenu critique)
Description : les membres poussent sur `main` ; le serveur tire `main` toutes les 60 s (gitops.py:329 ; défauts `git_branch="main"`, `auto_pull_s=60`, `auto_commit=True`, models.py:616-618) et exécute le code tiré. L'auto-commit pousse en plus sur `main`, sous `app(<user>)` et avec la deploy key, tout fichier écrit par `PUT /api/files`, `.py` compris (app.py:765-768, gitops.py:217). Le code atterrit sur les GPU au job suivant et sur les Mac après `git pull`/`wmlab validate`/le hook `.claude/hooks/validate_specs.py`.
Scénario : un plugin écrit par un member s'exécute sur le serveur (SEC-01), est poussé sur `main`, puis exécuté sur le Mac de l'admin (clés GPU, clé Scaleway, jeton admin) à sa prochaine synchro.
Preuve : `commit_and_push(rel, user, hosted.git_branch)` → `push origin HEAD:{branch}`.
Correction : retirer le push aux membres ; protéger `main` (PR, revue, CODEOWNERS sur `lab/ wmlab/ deploy/ desktop/ workspace.yaml`, deploy key limitée à `app/*`) ; n'auto-commiter que des YAML de `projects/`, jamais de `.py`, sur une branche dédiée fusionnée par PR ; servir `wmlab` depuis un venv/wheel figé distinct du checkout tiré.

### Graves

**SEC-03 — Pas de refus par défaut** (`app.py:193`, vérif. grave/critique/grave)
`TokenMiddleware` (app.py:157-206) laisse passer toute requête authentifiée sans regarder méthode ni rôle. `require_admin` n'est appelé qu'aux lignes 608, 954, 992, 1003, 1066. Ouvertes au member : `POST /api/projects` (666), `/experiments` (706), `/hpo` (718), `PUT /api/files` (738), notes (843), tags (848, réécrit `run.json`), notes d'étude (922), `POST /api/git/pull` (616). La garde « benchmark publié » (746-754) ne couvre qu'un benchmark déjà évalué. Scénario : un member réécrit une expérience ou un nouveau `benchmarks/image_vN.yaml` (attaques → identité) et fausse classements et budget. Correction : dans `TokenMiddleware`, si `role != "admin"` et méthode hors `{GET, HEAD}` → 403, sauf liste blanche vide par défaut ; garder `require_admin` en 2e barrière ; mettre à jour ARCHITECTURE.md §12.2 ; test paramétré sur `app.routes` (member → 403 sur toute route non-GET).

**SEC-04 — Le member lance et pilote jobs et actions GPU** (`app.py:1019`, vérif. grave/grave/moyen)
Sans contrôle de rôle : `POST /api/jobs` (1019, `host`/`git_ref`/`max_hours` libres), `/jobs/estimate` (1029), `stop` (864), `cancel` (1034), `sync` (1038), `hosts/{name}/test|setup|inventory|retrieve` (966/973/979/984), `instance` (1007). `JobManager.submit` (jobs.py:216) n'impose pas `max_hours` (None accepté, jobs.py:382), et `cancel`/`stop` ne comparent pas `job.user` à l'appelant. Scénario : un member lance des entraînements sans plafond sur l'hôte le plus cher (budget 40 k€), annule les runs des collègues, relance `setup` pendant un entraînement. Correction : `require_admin` sur ces routes (ou refus par défaut SEC-03) ; si un rôle « opérateur » apparaît, `max_hours` obligatoire et plafonné, hôtes autorisés, annulation limitée à `job.user == appelant` ; masquer ces actions dans le desktop.

**SEC-05 — Fuite au member du code, specs, logs, checkpoints** (`catalog.py:371`, vérif. grave/moyen/grave)
Aucun contrôle de rôle en lecture ; `resolve_workspace_file` fonctionne par liste noire (catalog.py:40 : `.wmlab/.venv/.git/node_modules`). `GET /api/files` lit `wmlab/**`, `lab/**`, `deploy/`, les specs, `.claude/`, un éventuel `.env` racine. `GET /api/runs/{id}` expose `spec`, `spec_yaml`, `plan`, `env.git_diff`, `overrides` ; `run_summary.params` est la spec aplatie (views.py:151). `GET /api/runs/{id}/file` sert `checkpoints/*.pt`, `stdout.log`, `env/` (app.py:832-841, seul contrôle : `is_relative_to`). Scénario : un member énumère les runs et télécharge tous les poids (dizaines de k€ GPU) plus le code d'architecture. Correction : politique par rôle — pour un member, refuser `GET /api/files`, `/api/validate`, `/api/registry` détaillé, `/jobs/estimate` ; `redact_for_member` sur `run_summary`/`_run_detail`/`study_summary` (retirer `spec`, `spec_yaml`, `plan`, `env`, `overrides`, params `model.`/`optimizer.`/`loss.`/`phases.`) ; `/runs/{id}/file` en liste blanche (`samples/**`, `eval/*.json`), refus `*.pt`/`*.safetensors`/`spec.yaml`/`stdout.log`/`env/`.

**SEC-06 — XSS stockée via `desktop/dist` auto-pullé** (`app.py:521`, proposé grave → vérif. grave/moyen/réfuté, retenu grave)
`create_app` monte `ctx.workspace/desktop/dist` sur `/` (StaticFiles, app.py:520-523). `dist/` est dans `.gitignore`, mais `git add -f` permet d'y commiter un fichier ; `dirty()` ignore les fichiers ignorés (gitops.py:123), donc le `merge --ff-only` du pull accepte un commit qui ajoute ou écrase un fichier sous `desktop/dist`. Le fichier est alors servi sur l'origine de l'UI web, où le jeton vit en `localStorage` (api.ts). Scénario : un commit remplace `desktop/dist/index.html` ou ajoute une page ; tout utilisateur du navigateur exécute le JS de l'attaquant, qui lit le jeton admin. L'app Electron n'est pas touchée (`loadFile`, navigation backend convertie en téléchargement). Correction : construire l'UI hors du dépôt (p. ex. `/opt/wmlab/ui` en root, lecture seule), servie par Caddy avec liste blanche + CSP ; faire échouer `_pull` si le diff touche autre chose que `projects/`, `benchmarks/`, `datasets/` ; protéger la branche.

**SEC-07 — Exfiltration de secrets via symlinks rapatriés** (`ssh.py:195`, vérif. 3/3 grave)
`pull_argv` et `fetch_checkpoint` utilisent `rsync -az` (ssh.py:195, 380) ; `-a` implique `--links`, donc un lien symbolique créé sur la GPU est recréé dans `.wmlab/runs/<id>/`. Les lecteurs serveur (`tail_lines`, `notes`, `spec_yaml`) ouvrent les fichiers sans `O_NOFOLLOW` ni confinement ; seule `/api/runs/{id}/file` fait `is_relative_to` (app.py:834). Scénario : un member lance un job dont le code pointe `stdout.log` (ou `spec.yaml`) vers `/etc/wmlab/env` ou `~/.ssh/id_ed25519` ; après rapatriement, `GET /logs` ou le champ `spec_yaml` renvoient le secret → admin complet, clé Scaleway, root sur toutes les GPU. Correction : `rsync --no-links` (ou `--safe-links --munge-links`) + `--no-perms --chmod=D0750,F0640` ; ouvrir via un helper `O_NOFOLLOW` + `is_relative_to(run_dir)` pour logs/notes/spec/eval/events/indexeur ; isoler les secrets (LoadCredential, clé SSH hors du home du service).

**SEC-08 — Traversée de chemin via les `run_id` de `trials.jsonl`** (`ssh.py:316`, vérif. 3/3 grave)
`job_folders` (ssh.py:316) et `sync` (ssh.py:370) lisent les `run_id` dans `studies/<id>/trials.jsonl` rapatrié de la GPU, sans `SAFE_ID` (contrairement à `inventory._run_id_of`). `pull()` fait `mkdir` puis rsync vers `ctx.data/runs/<run_id>`. Un `run_id` relatif (`../../lab/...`) ou absolu sort du dossier `runs`. Scénario : une GPU (ou un job de member) écrit un `trials.jsonl` dont le `run_id` cible `.git/hooks`, `lab/models`, `.venv/.../*.pth` ou `~/.ssh` ; à la synchro, le serveur y copie des fichiers → exécution de code sur le serveur. Correction : filtrer chaque `run_id` distant par `SAFE_ID` (rejet de `/` et `..`) dans `job_folders`/`sync`/`retrieve` ; dans `pull()`, vérifier `local.resolve()` sous `(ctx.data/kind).resolve()` avant `mkdir` ; durcir systemd (`ProtectSystem=strict`, `ReadWritePaths` limités).

**SEC-09 — Le rapatriement écrase les runs d'autres machines** (`jobs.py:579`, vérif. 3/3 grave)
`_run_retrieve` (`POST /hosts/{name}/retrieve`, ouvert au member) prend tous les `runs/*` listés sur la GPU et fait `rsync -az` vers `<data>/runs/<id>/` sans vérifier `job.host`, l'existence locale ni `--ignore-existing` (seul `notes.md` est protégé). Scénario : un member crée sur la GPU un dossier homonyme d'un run de production avec un `run.json` flatteur et un checkpoint piégé, puis déclenche le rapatriement ; la référence est écrasée (poids remplacés, classement faussé). Correction : ne rapatrier que les runs/études du host (via `JobRecord` ou inconnus localement) ; refuser un run dont le `run.json` local a un autre host ; quarantaine `<data>/incoming/<host>/<id>` avec `--no-links` + `--ignore-existing` pour les `.pt` ; enregistrer le sha256 au 1er rapatriement et refuser tout remplacement.

### Moyens

**SEC-10 — Hôte `local` implicite exécute les jobs des members sur le serveur** (`loader.py:463`, vérif. 1/1 moyen)
`load_hosts` réinsère toujours un hôte `local` (price 0) si `hosts.yaml` n'en a pas (loader.py:463), et `default_host="local"` (models.py:637). En mode hébergé, `JobManager` accepte les jobs locaux à HEAD (jobs.py:268-279). Un `POST /api/jobs` sans `host` lance donc train/hpo/eval dans le serveur public, en tant que `wmlab`, avec tout l'environnement (jeton serveur, `SCW_SECRET_KEY`), sans `MemoryMax`, à coût 0 € (invisible au budget). Impossible à désactiver (réinséré). Correction : en mode hébergé, ne pas injecter `local` (ou le marquer `disabled`), refuser `host.kind=="local"` dans `submit`/`setup`/`estimate`, exiger un host explicite. Si un local reste utile, le réserver à l'admin et l'exécuter en `systemd-run` transitoire (MemoryMax, env purgé).

**SEC-11 — Push sur `workspace.yaml` gèle le pull auto** (`install_server.sh:414`, vérif. 1/1 moyen)
`install_server.sh` modifie `workspace.yaml` puis le masque par `git update-index --skip-worktree` ; `dirty()` l'exclut aussi (gitops.py:120-124). Un commit poussé qui touche `workspace.yaml` fait échouer `merge --ff-only` à chaque cycle (WARNING seulement). Le checkout ne bouge plus ; `resolve()` préfère la branche locale périmée (gitops.py:251-256), donc un `git_ref=main` exécute l'ancien code ; les auto-commits divergent ; `PUT /api/settings` ne partage plus jamais les réglages (`git add` sur une entrée skip-worktree → rc=1). Scénario : un member pousse un commit anodin touchant `workspace.yaml` → les correctifs poussés ensuite ne s'appliquent plus, les jobs « main » tournent sur du vieux code, seule trace : `last_pull_error`. Correction : ne plus modifier de fichier suivi sur le serveur (réglages serveur dans un fichier non versionné fusionné par-dessus, retirer skip-worktree) ; dans `resolve`, résoudre sur `origin/<ref>` après fetch ; alerte bloquante si `last_pull_error` persiste ; refuser au pull tout commit touchant `workspace.yaml`/`hosted.*`/`data_dir`.

**SEC-12 — Classement falsifiable : eval au nom d'un autre run** (`indexer.py:141`, vérif. 1/1 moyen)
`_index_run` force `run_id` pour `run.json` (ligne 133) mais fait seulement `result.setdefault("run_id", run_dir.name)` pour les évaluations (ligne 141) : le `run_id` écrit par le processus d'évaluation est conservé. `benchmark_row`/`project_best` l'utilisent. Scénario : un member fait écrire `eval/zz.json` avec `run_id` d'un run de référence, `created_at` en 2099 et un score effondré ; le classement attribue ce score au run de l'admin, sans rien écraser (indétectable par un contrôle d'appartenance de dossier). Nuance : l'eval forgée reste visible sous le run hôte, donc repérable à l'œil. Correction : forcer `result["run_id"] = run_dir.name` ; vérifier que `checkpoint` est une entrée de l'index du même run et que `benchmark` existe ; à terme, n'accepter comme officielle qu'une évaluation rattachée à un `JobRecord`.

**SEC-13 — `desktop/.env.production` poussé détourne les jetons** (`api.ts:139`, vérif. 1/1 moyen)
`detectConfig()` teste `import.meta.env.VITE_API_BASE` (api.ts:139-146) avant la config navigateur ; Vite lit automatiquement `desktop/.env.production` au build, et `.gitignore` n'a aucun motif `.env*`. Le serveur reconstruit l'UI depuis le checkout tiré lors de la mise à jour (README:302). Scénario : un member pousse un `.env.production` pointant `VITE_API_BASE` vers un hôte tiers ; après la mise à jour par l'admin, chaque navigateur ouvrant l'UI envoie son jeton (admin compris) à l'attaquant. Électron peu touché (le bridge l'emporte sur la variable). Correction : construire `dist` en CI depuis un commit revu ; fixer `envDir`/`envPrefix` ou échouer si un `desktop/.env*` est présent ; ne lire `VITE_API_*` qu'en `import.meta.env.DEV` ; ajouter `.env*` au `.gitignore` et CODEOWNERS sur `desktop/` ; `connect-src 'self'`.

**SEC-14 — XSS stockée via fichiers de run html/svg** (`app.py:840`, proposé grave → vérif. grave/moyen/moyen, retenu moyen)
`run_file` renvoie le fichier avec un MIME deviné, sans `Content-Disposition`, CSP ni `nosniff` au niveau route. Un `.html`/`.svg` s'exécute sur l'origine de l'app (servie par `_DesktopFiles`), où le jeton vit en localStorage ; `?token=` est accepté. Un member peut déposer le fichier via un job GPU (rsync). Limites : Caddy pose `nosniff` (sans effet sur `text/html`/`image/svg+xml`) ; l'UI affiche les samples en `<img>`/`<audio>`/`<video>` (pas d'exécution auto) ; Électron télécharge au lieu d'ouvrir. Exploitation : la victime doit ouvrir un lien forgé dans un navigateur authentifié. Correction : liste blanche de types inline, sinon `application/octet-stream` + `Content-Disposition: attachment` ; `CSP: sandbox; default-src 'none'` + `nosniff` ; refuser svg/html/xml ; à terme cookie de session HttpOnly ou URL signée.

**SEC-15 — Symlinks suivis par git contournent le confinement** (`catalog.py:136`, proposé grave → vérif. grave/moyen/faible, retenu moyen)
`project_notes` (catalog.py:136), `project_data`/`read_raw` et `scaffold.new_experiment` (scaffold.py:379) ouvrent des chemins sans résolution/confinement. Un symlink commité est recréé par `merge --ff-only` (`core.symlinks=true`) via GitSync ou `/api/git/pull`. Scénario : un member commite `projects/demo/notes.md` → `/etc/wmlab/env` et `experiments/leak.yaml` → `~/.ssh/id_ed25519` ; après pull, `GET /api/projects/demo` renvoie les secrets dans `notes`, et une copie d'expérience recopie la clé privée dans un YAML lisible. Dominé par SEC-01 (RCE directe sans git), d'où moyen. Correction : `git config core.symlinks false` et refus au pull des modes `120000` ; faire passer `project_notes`/`project_data`/`new_experiment` par `resolve_workspace_file` ou `O_NOFOLLOW`+`is_relative_to` ; sortir `/etc/wmlab/env` et `~/.ssh` de la portée du process.

**SEC-16 — Jeton en `?token=` et en localStorage** (`app.py:171`, vérif. moyen/moyen/faible)
`TokenMiddleware._supplied` lit `?token=` sur toutes les routes (app.py:171-176). Le frontend met le jeton dans `fileUrl`/`costsCsvUrl` (api.ts:406-412), le lit depuis l'URL (search et fragment) et l'écrit en `localStorage['wmlab.token']` ; `scrubTokenFromLocation` ne nettoie pas le fragment (api.ts:86). Jetons sans expiration. Vecteurs : copier-coller d'un lien de sample ou du CSV de coûts (dans Slack, un ticket), cache disque, xattr WhereFroms du CSV, fixation via `/?token=`. Surévalué : ni logs Caddy ni logs uvicorn par défaut (`access_log=False`), `Referrer-Policy no-referrer`. Correction : n'accepter `?token=` que sur `GET /runs/{id}/file`, et le remplacer par une URL signée courte (HMAC, exp ≤ 5 min) ; `Budget.tsx` via `downloadCostsCsv()` (en-tête) ; `Cache-Control: no-store` sur `/api/*` ; nettoyer le fragment ; à terme cookie HttpOnly.

**SEC-17 — Aucune piste d'audit** (`app.py:1128`, vérif. moyen/moyen/moyen)
`uvicorn` tourne `access_log=False` (app.py:1128) sans `proxy_headers` ; Caddy n'a pas de directive `log`. Non journalisés : téléchargements de checkpoints, notes/tags, fetch, test/inventory, git pull, 401. Journalisés sans utilisateur : cancel, stop, PUT files, hosts, settings. Le seul puits est un `RotatingFileHandler` 5 Mo × 3 (app.py:316), qu'un member fait tourner (ou supprime via SEC-01). Inexact : `proxy_headers`/`forwarded_allow_ips` sont les défauts uvicorn, l'IP n'est pas perdue mais jamais journalisée ; la création de job et les commits portent déjà l'auteur. Correction : middleware d'audit JSON append-only vers journald/collecteur distant (ts, user+rôle, IP réelle, méthode, route, run/job/path sans token, statut, octets ; 401/403 en WARNING) ; passer `user` à cancel/stop/poweroff/settings/hosts/write_file ; activer un log Caddy filtré.

**SEC-18 — Inventaire basé sur l'`index.json` de la GPU** (`inventory.py:156`, proposé grave → vérif. moyen/moyen/moyen, retenu moyen)
`build_inventory` ne compte que les entrées de `checkpoints/index.json` lu sur la GPU (ligne 156) ; un `.pt` présent mais hors index n'est pas compté ; `file` non confiné ; `size_bytes:0` déclare l'entrée rapatriée ; un index illisible est ignoré. `decommission` ne bloque que sur `unretrieved_checkpoints`/`unindexed_runs` (hostops.py:100-107) et ignore les études non synchronisées, avant `delete_server`. Scénario le plus réaliste : crash entre l'écriture atomique du `.pt` et celle de l'index (run.py:383-404) laisse un `.pt` orphelin non compté ; la page Machines affiche « non rapatriés = 0 », l'admin supprime l'instance et ses volumes, perte définitive. Correction : calculer depuis `listing.files` réels (chaque `.pt` distant doit exister localement, même taille) ; rejeter un `file` non conforme à `^[A-Za-z0-9._-]+\.pt$` (aussi dans `resolve_checkpoint`) ; bloquer si une étude a `synced=false` ; snapshot des volumes avant suppression.

**SEC-19 — Venv partagé root sur GPU** (`ssh.py:184`, vérif. moyen/moyen/faible)
Tous les jobs d'une GPU tournent en root dans le même venv `<workdir>/.venv` ; `uv sync` n'est relancé que si le sha256 de `uv.lock` diffère du marqueur `.wmlab-lock` (ssh.py:184-192), sans contrôle d'intégrité du venv. Un job peut déposer un `.pth` dans `site-packages`, modifier torch, etc. ; `rsync --delete` (taille+mtime) ne restaure pas un fichier de mêmes taille/mtime. Scénario : un member fait exécuter une fois un job qui empoisonne le venv ; tous les entraînements suivants (admin compris) exécutent la charge, avec un `git_sha` propre affiché. Correction : venv immuable par empreinte de lock (`venvs/<lock_sha>`, lecture seule) ; jobs sous un utilisateur non-root sans écriture sur venv/code ; `uv sync --check` ou manifeste de hachages ; re-provisionner après un job non revu ; surtout, `require_admin` sur `POST /api/jobs`.

**SEC-20 — Budget et attribution falsifiables via `run.json`** (`reader.py:63`, vérif. moyen/faible/moyen)
`cost_eur` multiplie durée × `price_eur_per_hour` × `gpu_share`, tous lus dans `run.json`, sans borne ni signe (`gpu_share` 0 ou négatif accepté, non fini → null). `run_summary`/`study_summary` exposent `user` depuis `run.json` (views.py:153). `run.json` est écrit par le processus du run (code du member sur GPU) puis rapatrié ; le serveur connaît les vraies valeurs (`JobRecord`) mais ne recoupe pas. Scénario : `gpu_share:-50` gonfle le budget restant, `gpu_share:0` masque des runs longs, `user:'justin'` attribue le coût à l'admin. PoC : `spent_eur` passe négatif. Correction : facturer côté serveur depuis `JobRecord` (user, prix de `hosts.yaml`, horodatages observés), hors dossier rapatrié ; dans `cost_eur`, rejeter non fini/négatif, borner `gpu_share` à `]0,1]` ; signaler tout écart `run.json`/job.

**SEC-21 — DoS YAML persistant** (`catalog.py:55`, vérif. moyen/moyen/moyen)
`read_raw` ne capture qu'`OSError`/`YAMLError`/`UnicodeDecodeError` : un YAML très imbriqué lève `RecursionError` → 500 persistant. `yaml.safe_load` conserve les alias : `benchmark_info` (qui sérialise `raw` quand la spec est invalide) et `_references` ont un coût exponentiel. Un member écrit le fichier via `PUT /api/files` ; en hébergé il est poussé. PoC : 24 niveaux d'alias ≈ 29 Mo de réponse, 432 Mo RSS ; un YAML imbriqué fait tomber `/api/projects/demo`, `/api/datasets`, `/api/validate` en 500. Correction : loader qui refuse les alias (erreur sur `AliasEvent`) et borne la profondeur, dans `read_raw`/`read_yaml`/`file_validation`/`validate_content` ; capturer `RecursionError`/`Exception` ; ne jamais renvoyer `raw` ; isoler chaque fichier dans les listings ; `PUT /api/files` admin.

**SEC-22 — Corps de requête non bornés** (`app.py:742`, vérif. moyen/faible/moyen)
`MAX_FILE_BYTES` (1 Mo) n'est vérifié qu'après parsing JSON (app.py:742) ; `NotesBody.notes` et `ValidateBody.content` sans `max_length` ; ni uvicorn ni Caddy ne bornent la taille ; pas de quota de fichiers ; `git archive` chargé entièrement en mémoire (gitops.py:272). Scénario : un member envoie des notes de plusieurs centaines de Mo sur chaque run, ou des milliers de PUT, jusqu'à saturer disque/RAM (échec des rsync de checkpoints et des sauvegardes). Correction : Caddy `request_body { max_size 2MB }` ; middleware ASGI 413 ; `Field(max_length=...)` sur notes/content/tags ; quota par racine + rate limit par utilisateur ; refus d'écriture sous un seuil d'espace libre ; `git archive` en flux.

**SEC-23 — Amplification par `plan_preview`** (`catalog.py:350`, vérif. moyen/faible/moyen)
`validate_payload` → `plan_preview` → `PhasePlan.preview()` génère 50 points par phase et par série ; `phases`/`terms` non bornés ; `ValidateBody.content` sans `max_length`. Le même aperçu est recalculé à chaque `GET /api/runs/{id}` (app.py:444) puis gzippé. PoC : 8 000 phases ≈ 47 Mo de JSON. Correction : `Field(max_length)` sur `content`, `phases` (64) et `terms` (32) ; plafonner le total de points ; cacher l'aperçu d'un run (spec figée) ; `MemoryMax`/`TasksMax` systemd ; rate limit.

**SEC-24 — Aucune borne sur les données rapatriées** (`ssh.py:194`, vérif. moyen/moyen/faible)
La synchro fait `rsync -az` de tout le dossier de run en n'excluant que `checkpoints/*.pt` (ssh.py:37), sans `--max-size`, quota ni contrôle d'espace ; `default_runner` capture stdout en mémoire (base.py:109), donc la sortie de `listing()` d'une GPU hostile n'est pas bornée. Scénario : un run écrit des centaines de Go dans `stdout.log`, rapatriés jusqu'à saturer le disque ; ou une GPU renvoie des Go sur stdout pendant l'inventaire. Correction : `rsync --max-size=200M` + exclusions `*.pt/*.pth/*.ckpt/*.safetensors/*.bin` pour la synchro périodique ; vérifier l'espace libre avant chaque pull ; lecture bornée (Popen + kill) ; `.wmlab` sur volume dédié.

**SEC-25 — Passage member→admin via `data_dir`** (`context.py:28`, proposé grave → vérif. moyen/faible/réfuté, retenu moyen)
`resolve_data_root()` lit `data_dir` dans `workspace.yaml` quand `WMLAB_DATA` est absent (context.py:28) ; `install_server.sh` n'écrit jamais `WMLAB_DATA` ; `data_dir` non validé (models.py:635) ; `workspace.yaml` versionné et tiré. `UserStore` lit `users_path(ctx.data)`. Scénario : un member dépose un `users.yaml` forgé sous `projects/` (via `PUT /api/files`) et pousse un `workspace.yaml` déplaçant `data_dir` ; au redémarrage, son jeton devient admin. Nuance importante (lien avec SEC-11) : avec `skip-worktree`, un `workspace.yaml` poussé n'est en général PAS appliqué (merge avorté) — l'élévation ne marche que si le fichier local est inchangé ; le member ne peut pas non plus écrire `workspace.yaml` par l'API. Souvent dominé par SEC-01. Correction : écrire `WMLAB_DATA` dans `/etc/wmlab/env` et refuser de démarrer sans lui en hébergé ; lire `users.yaml` depuis un chemin fixe hors workspace ; valider `data_dir` ; ignorer `data_dir`/`hosted.*` venant de git.

**SEC-26 — Passage wmlab→root via `install_server.sh`** (`install_server.sh:94`, proposé grave → vérif. grave/moyen/moyen, retenu moyen)
`write_file()` (root) écrit par redirection puis chmod/chown (qui suivent les liens) ; pour `~wmlab/.ssh/config` (possédé par wmlab), un lien `config.tmp` fait créer/écraser une cible arbitraire. `install_server.sh` exécute aussi en root le python du venv possédé par wmlab (ligne 276) et installe comme unités root des gabarits lus depuis `$DIR` (possédé par wmlab). Point le plus direct : la mise à jour documentée lance en root `sudo bash /opt/wmlab/deploy/install_server.sh`, script appartenant à wmlab. Scénario : une RCE en tant que wmlab (SEC-01) piège ce script, le venv ou une unité systemd, et obtient root à la prochaine mise à jour. Correction : ne jamais écrire en root dans une arborescence de wmlab (`as_wmlab tee`, `umask 077`, `mktemp` + refus si `-L`, `chown -h`) ; ne jamais exécuter en root un binaire de `$DIR` ; gabarits en heredoc ou copie root vérifiée ; mise à jour depuis une copie root du script ; séparer code (root:0755) et données (wmlab:0750).

**SEC-27 — Clé SSH unique sans passphrase** (`install_server.sh:146`, vérif. moyen/moyen/moyen)
`id_ed25519` sans passphrase sert de deploy key en écriture (README:80) et d'accès root à toutes les GPU (README:199-215) ; le README conseille de l'ajouter aux clés du PROJET Scaleway (injectée dans toute instance future) ; pas de `from=`/`command=`, jobs en root, `Host *` en `accept-new`. Scénario : toute lecture de la clé (SEC-01/SEC-07) donne root sur chaque GPU et le push sur le dépôt. Nuance : `accept-new` refuse une clé d'hôte modifiée, donc le scénario « IP recyclée → MITM » est inexact (seule la 1re connexion vers une IP neuve est exposée). Correction : deux clés (`id_git` limitée à `app/*` protégée, `id_gpu`) sélectionnées par `IdentityFile`/`IdentitiesOnly` ; clé GPU par instance avec `from="<IP serveur>"`, pas au niveau projet ; utilisateur non-root sur les GPU ; épingler les clés de github.com.

**SEC-28 — Sauvegardes en clair, clé rclone en écriture/suppression** (`backup.sh:63`, vérif. moyen/moyen/moyen)
`backup.sh` (User=wmlab) `rclone copy` tout `.wmlab` vers le bucket sans remote crypt ; les clés S3 sont en clair dans `~wmlab/.config/rclone/rclone.conf` (lisible par le backend, même uid), avec droit d'écriture et souvent de suppression ; pas de versioning/Object Lock ; `/var/backups/wmlab` appartient à wmlab. Scénario : une RCE en tant que wmlab lit `rclone.conf` et peut détruire le bucket (`rclone purge`) et le `.wmlab` local — rançongiciel, perte définitive des poids et du budget GPU. Confidentialité : gain faible (wmlab lit déjà `.wmlab`). Correction : utilisateur `wmlab-backup` (lecture seule sur `.wmlab`, `rclone.conf` 0600 à lui) ; clé IAM limitée à `PutObject` + versioning/Object Lock (rétention ≥ 30 j, pas de `Delete`) ; remote `crypt` (mot de passe hors serveur).

### Faibles

**SEC-29 — Intégrité git de l'auto-commit** (`gitops.py:200`, vérif. moyen/faible/faible, retenu faible)
`commit_and_push` fait `git add/commit -- <rel>` sans `--literal-pathspecs` (un nom `lab/*` indexe tout `lab/`) ; `resolve_workspace_file` n'interdit que `\x00`, donc on peut écrire `lab/.gitignore` (plugin invisible sur GitHub, `dirty()=False`), `.gitattributes export-ignore` (retire du code des exports GPU), et un nom avec `\n` injecte dans `server.log` et dans le message `app(<user>): <rel>` (faux trailers). Correction : `GIT_LITERAL_PATHSPECS=1` ; refuser composants en `.`, caractères de contrôle, jokers ; liste blanche d'extensions ; échec de `git add` bloquant en hébergé.

**SEC-30 — Fuite des métadonnées d'infra** (`app.py:948`, vérif. 3/3 faible, retenu faible)
`GET /api/hosts` renvoie `ssh_target` (root@IP), `workdir`, `scaleway` ; `/instance`, `/api/git` (remote), `/api/jobs` (overrides, erreurs), `/api/settings/costs` (budget), `/api/health`+`/overview` (chemin du workspace) sont ouverts au member. Impact faible (IP publiques, pas de secret ; la clé SSH reste privée). Un remote HTTPS à PAT serait l'exception (voir SEC-55). Correction : vues réduites par rôle (`host_view(role)`, `Job.to_api(role)`), `workspace.path` et `/api/settings` admin, `/api/health` public = `{ok:true}`.

**SEC-31 — Desktop fail-open** (`Sidebar.tsx:96`, vérif. faible/faible/réfuté, retenu faible)
Le rôle n'est lu qu'en 3 endroits ; `Hosts.tsx:581` traite l'utilisateur comme admin tant que `/api/me` n'a pas répondu. Lancement, éditeurs, Bibliothèque, onglets Config/Env/Logs, actions machines restent visibles au member. Rappel : l'UI n'est pas un contrôle d'accès (un member appelle l'API directement) ; les corrections serveur restent indispensables. Correction : hook `useCan()` fail-closed (`me.data?.role === "admin"`) ; masquer éditeurs/lancement/Machines pour un member.

**SEC-32 — `WMLAB_TOKEN` clé maître** (`app.py:1098`, vérif. moyen/faible/faible, retenu faible)
Tout porteur devient l'utilisateur générique `admin` (non attribuable) ; `resolve_token` accepte n'importe quelle longueur ; `WMLAB_TOKEN=` vide est régénéré et imprimé sur stderr → journald (lisible par le groupe `adm`) ; `install_server.sh:248` ne teste que la présence. Correction : en hébergé, refuser de démarrer sans un `WMLAB_TOKEN` ≥ 32 car., ne jamais en générer ni l'imprimer ; `grep -Eq '^WMLAB_TOKEN=.{32,}'` ; idéalement comptes admin nominatifs.

**SEC-33 — Cycle de vie des jetons** (`users.py:112`, vérif. 1/1 faible)
Jetons sans expiration/rotation/rate limiting ; `add_user`/`revoke_user` en lecture-modification-écriture sans `flock` ; `add_user` supprime l'entrée homonyme (efface l'historique, contredit ARCHITECTURE.md:864) ; `load_users` renvoie `[]` sans log sur fichier illisible (refus silencieux de tous les membres). Correction : `flock` + relecture ; conserver les entrées révoquées (id immuable) ; `expires_at`/`last_used` ; échouer au démarrage si `users.yaml` est illisible ; SQLite transactionnel pour la version e-mail/mdp.

**SEC-34 — Pas de rate limiting sur l'auth** (`app.py:193`, vérif. faible/faible/réfuté, retenu faible)
`TokenMiddleware` répond 401 sans compteur ni délai ; aucun module de rate limiting. Sans effet aujourd'hui (jetons 160–192 bits), mais bloquant pour la future connexion e-mail/mdp. Correction : à la livraison du login mdp, argon2id, limite par IP et par compte + délai exponentiel, réponse identique e-mail connu/inconnu ; fail2ban ou caddy-ratelimit sur les 401.

**SEC-35 — CSP et en-têtes insuffisants** (`Caddyfile:7`, vérif. 1/1 faible)
Le bloc header Caddy n'a ni CSP, ni `frame-ancestors`/`X-Frame-Options`, ni `Permissions-Policy` ; HSTS sans `includeSubDomains` ; la CSP vite en `<meta>` autorise `connect-src 'self' http: https:` et ne couvre pas les documents de l'API. Correction : CSP en EN-TÊTE (Caddy) `connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'` ; `X-Frame-Options DENY` ; HSTS `includeSubDomains` ; `/api/runs/{id}/file` en `sandbox`.

**SEC-36 — Secrets hérités par les jobs locaux** (`local.py:86`, vérif. moyen/faible/faible, retenu faible)
`LocalExecutor.environment()` copie tout `os.environ` pour chaque job local, qui exécute les plugins ; les secrets peuvent finir dans un `stdout.log` lisible via `/api/runs/{id}/logs`. Dominé par SEC-01 (le même uid lit déjà `/etc/wmlab/env`). Correction : env minimal en liste blanche (retirer `WMLAB_TOKEN`, `SCW_*`, `AWS_*`, `RCLONE_*`, `WMLAB_BACKUP_*`) ; LoadCredential systemd ; désactiver l'hôte local en hébergé (SEC-10).

**SEC-37 — TOCTOU spec validée ≠ exécutée** (`jobs.py:372`, vérif. faible/faible/faible)
`_spec_file`/`load_experiment`/`estimate`/`new_run_id` lisent la spec dans `ctx.workspace` (HEAD), mais le job SSH exécute `train <rel>` à `code/<git_sha>`. Steps, budget HPO, `init_from` peuvent différer ; validation, estimation et nom de run ne reflètent pas l'exécution ; une spec propre à une branche donne un 404. Faible car aucun plafond serveur n'existe à contourner aujourd'hui. Correction : valider/estimer la spec de `code/<sha>` (export à la soumission), enregistrer son sha256 et le vérifier avant `train` ; `max_hours` obligatoire + plafond €/job.

**SEC-38 — Overrides contournant la validation d'`init_from`** (`jobs.py:230`, vérif. moyen/faible/faible, retenu faible)
`submit` copie les overrides tels quels ; les contrôles d'`init_from` (safe_id, même machine, checkpoint présent) ne s'appliquent qu'au champ dédié. `InitFromSpec.run` sans validateur, `_parent_run_dir` accepte un chemin absolu/`~`/`..` si `checkpoints/` existe. Scénario : `overrides:{init_from:'{"run":"/chemin/absolu"}', data...root:'/home/wmlab'}` charge des poids d'un autre run et lit des dossiers arbitraires. Pas de RCE (`weights_only=True`). Correction : refuser les clés `init_from*` dans overrides ; `InitFromSpec.run` = SAFE_ID ; `_parent_run_dir` confiné à `runs_dir()` ; rejeter toute valeur d'override absolue/`~`/`..` ; aucun override pour un member.

**SEC-39 — Suppression d'instance sans vérifier `server_id`** (`scaleway.py:203`, vérif. moyen/faible/faible, retenu faible)
`decommission` vérifie l'inventaire via `ssh_target` puis supprime l'instance de `scaleway.server_id` sans comparer IP↔ssh_target ni exclure le serveur central ; `delete_server` supprime TOUS les volumes. `server_id` est libre (PUT /api/hosts, admin). Scénario : une coquille (UUID du serveur central ou d'une autre GPU) + « Retirer + supprimer l'instance » détruit l'autre instance et ses volumes. Réservé à l'admin. Correction : exiger que l'IP de l'instance corresponde à `ssh_target` ; refuser un tag `wmlab-server` ; ne supprimer que boot/local (snapshot des SBS) ; ressaisie du nom ; IAM limitée aux instances taguées GPU.

**SEC-40 — Champ `experiment` d'un plan HPO hors workspace** (`loader.py:353`, vérif. 1/1 faible)
`experiment_path_for_hpo` accepte un chemin absolu/`..` sans confinement ; `validate` et `new_hpo` lisent alors ce fichier, et les messages d'erreur révèlent existence, validité et VALEURS scalaires (via `apply_experiment_overrides`). `POST /api/validate` (member) devient un oracle de lecture de tout YAML/JSON lisible par le process, y compris `.wmlab/users.yaml`. Faible car aucun secret exploitable n'est en YAML (les token_sha256 sont des empreintes). Correction : n'accepter qu'un nom (NAME_PATTERN) résolu dans `<projet>/experiments/` ; ne pas lire un fichier hors projet ; `_check_name(experiment)` dans `new_hpo`.

**SEC-41 — Front de Pareto O(n²) sans cache** (`views.py:327`, vérif. faible/faible/faible)
`get_study` relit `trials.jsonl` et appelle `pareto_front()` à chaque requête (O(n²), pire cas objectifs anti-corrélés). Faible : il faut des milliers d'essais (chaque essai = un entraînement GPU) ; le cache d'importances existe déjà. Correction : cacher le front (clé mtime/taille) ; algo O(n log n) à 2 objectifs ; plafonner la taille de `trials.jsonl`.

**SEC-42 — Paramètres de lecture non plafonnés** (`app.py:816`, vérif. faible/faible/faible)
`max_points` n'a qu'un plancher (désactivable avec 1e9) ; `/api/compare` sans limite de runs ; `_run_detail` relit `metrics.jsonl`/`events.jsonl` en entier à chaque polling ; `tail_lines` quadratique sur une ligne très longue (io.py:114). DoS authentifié. Correction : `max_points: Query(1000, ge=2, le=5000)` ; compare ≤ 20 ; cache de `metric_keys` par (mtime,size) ; réécrire `tail_lines` (blocs + compteur incrémental + limite d'octets).

**SEC-43 — Saturation du pool de threads** (`app.py:979`, vérif. moyen/faible/faible, retenu faible)
Routes `def` synchrones longues (SSH ≤ 120 s, rsync ≤ 900 s, git ≤ 300 s) sans require_admin ni déduplication, dans le threadpool anyio (40 jetons). 40 requêtes concurrentes figent l'API 2–15 min. Le pire vecteur est `fetch_checkpoint` (rsync complet, sans verrou). Correction : `require_admin` (SEC-03/04) ; GET inventory en cache TTL ; verrou par hôte/run (409) ; sémaphore SSH sortant ; fetch/sync en tâche de fond (202).

**SEC-44 — Création illimitée de jobs `setup`** (`jobs.py:307`, vérif. faible/faible/faible)
`POST /hosts/{name}/setup` (sans require_admin) ne déduplique pas, et `setup` échappe à `max_concurrent_jobs`. Chaque job pousse le code et lance `remote_bootstrap.sh` (≤ 3600 s). N requêtes = N threads/sessions SSH. `uv sync --frozen` étant idempotent, le « venv cassé » est surtout théorique. Correction : `require_admin` ; 409 si un setup/job actif existe déjà sur l'hôte ; plafonner la file par utilisateur.

**SEC-45 — GZip niveau 9 sur les checkpoints** (`app.py:514`, vérif. 1/1 faible)
`GZipMiddleware` (niveau 9 par défaut de Starlette) compresse les `.pt` en `application/octet-stream`, par blocs de 64 Kio dans la boucle d'événements, pour un gain nul (données incompressibles), alors que Caddy compresse déjà. Correction : `exclude_content_types` + `application/octet-stream`, ou retirer le middleware et laisser Caddy.

**SEC-46 — `GET /api/jobs` sans pagination** (`app.py:1013`, vérif. 1/1 faible)
Renvoie tous les jobs depuis l'origine, chargés en mémoire, à chaque polling ; les jobs `setup` (SEC-44) gonflent la liste. Correction : `limit` (100 défaut, 1000 max) + curseur `before` ; archiver les jobs anciens ; borner `overrides`.

**SEC-47 — `registry?reload=1` accessible au member** (`app.py:780`, vérif. 1/1 faible)
Le member peut déclencher `reload_plugins` à volonté ; pendant la purge/réimport, les registres sont vides et `validate`/`estimate` d'autrui échouent par intermittence (PoC : entrée absente dans 97 % des lectures concurrentes). Correction : réserver `reload` à l'admin (POST), délai minimal, échange atomique des registres.

**SEC-48 — Restauration root d'archive wmlab (setuid)** (`README.md:279`, vérif. faible/faible/faible)
La restauration fait `tar --zstd -xf ... -C /opt/wmlab` en root (conserve owner/permissions, bits setuid), puis `chown -R wmlab .wmlab` qui ne couvre que `.wmlab/`. Les archives sont écrites par wmlab. Scénario : une archive forgée (`lab/.x` uid 0 mode 4755) donne un binaire setuid root. Dominé par SEC-26. Correction : restaurer sous wmlab avec `--no-same-owner --no-same-permissions` et seulement `.wmlab/` ; vérifier l'archive ; archives non modifiables par wmlab.

**SEC-49 — SSH root joignable depuis Internet** (`README.md:203`, vérif. 1/1 faible)
Aucun groupe de sécurité GPU (défaut Scaleway = tout entrant) ; SSH root des GPU joignable depuis Internet ; pour le serveur, port 22 ouvert à tous ; pas d'ufw/fail2ban. Auth par clé uniquement (brute force vain), donc défense en profondeur. Correction : groupe de sécurité `wmlab-gpu` (22 depuis l'IP du serveur et des admins) ; pour le serveur, 22 restreint aux admins (ou bastion/WireGuard), `PermitRootLogin no`, `PasswordAuthentication no`.

**SEC-50 — Unités systemd sans sandboxing** (`wmlab-server.service:26`, vérif. moyen/faible/faible, retenu faible)
Seuls `NoNewPrivileges` et `PrivateTmp` ; pas de `ProtectSystem`/`ProtectHome`/`ReadWritePaths`/`SystemCallFilter`/`MemoryMax` ; `wmlab-backup.service` sans durcissement ; `KillMode=process` laisse survivre jobs et reverse shells à un stop. Correction : `UMask=0027`, `ProtectSystem=strict`, `ProtectHome=read-only`, `ReadWritePaths` limités, `MemoryMax`/`TasksMax`, `SystemCallFilter=@system-service`, `CapabilityBoundingSet=` ; jobs locaux en `systemd-run` transitoire.

**SEC-51 — Chaîne d'appro. à l'installation** (`install_server.sh:192`, vérif. faible/faible/faible)
uv : sha256 facultatif puis `sh install.sh` en root ; NodeSource `setup_22.x` en root sans vérif ; clé GPG Caddy sans empreinte ; `npm ci` exécute les postinstall ; `uv sync` sans `--locked`. HTTPS partout, une seule fois à l'installation. Correction : épingler le sha256 de uv (échec sinon) ; construire `dist` en CI (supprimer Node) ou `npm ci --ignore-scripts` ; `uv sync --locked`.

**SEC-52 — `.wmlab` en 0755, pas d'UMask** (`install_server.sh:266`, vérif. 1/1 faible)
`install -d -m 0755` pour `$DIR` et `.wmlab` ; service sans `UMask` : runs, `.pt`, `hosts.yaml` créés en 0644/0755, lisibles par tout compte local (ex. caddy). (`users.yaml` est en 0600.) Correction : `install -d -m 0750` ; `UMask=0027` ; `chmod -R o-rwx .wmlab` à la migration.

**SEC-53 — Compte `wmlab` avec `/bin/bash` et `~/.ssh` inscriptible** (`install_server.sh:138`, vérif. faible/faible/faible)
Une RCE passagère devient un accès SSH persistant (le backend écrit `~/.ssh/authorized_keys`, port 22 ouvert). Nuance : `--shell nologin` ne suffit pas (cron et modification du venv restent des voies de persistance). Correction : `--shell /usr/sbin/nologin` ; `DenyUsers wmlab` (ou `AllowUsers admins`), `PasswordAuthentication no` ; `/etc/cron.deny` ; code/venv en lecture seule pour wmlab.

**SEC-54 — API admin de Caddy sans auth** (`Caddyfile:5`, vérif. 1/1 faible)
Pas de bloc global : l'API admin écoute sur `127.0.0.1:2019` sans auth. Tout code exécuté par le backend (SEC-01) peut reconfigurer le proxy, ajouter un file_server vers le stockage de Caddy (clé privée TLS) ou détourner les jetons. Correction : `{ admin unix//run/caddy/admin.sock|0600 }` (ou `admin off` + restart) ; isoler le backend par socket Unix/`IPAddressDeny`.

**SEC-55 — URL du remote git exposée** (`gitops.py:126`, vérif. 1/1 faible)
`GitRepo.remote()` renvoie `git remote get-url origin` brut ; exposé dans `GET /api/git` (member) et enregistré dans `env/git.json` de chaque run (et dans le fichier de lancement envoyé aux GPU). Un remote `https://<token>@github.com/...` (documenté README:179-181) sert le PAT en clair. Correction : retirer la userinfo (`urlsplit`) dans `remote()`/`git_info`/`_write_launch_file` ; `remote` admin seulement ; supprimer l'alternative HTTPS-jeton ; `install_server.sh` refuse un `--repo https` contenant `@`.

**SEC-56 — Intégrité des poids des baselines** (`baseline_trustmark.py:34`, vérif. 1/1 faible)
Seuls 2 des 8 fichiers TrustMark ont un sha256 épinglé ; le contrôle n'a lieu qu'au téléchargement (`require_weights` fait confiance à tout fichier présent) ; InvisMark vient d'un miroir HF tiers (épinglé). Chargement `weights_only=True` (pas de RCE). Impact : intégrité des benchmarks de baselines. Correction : épingler les 8 sha256, rendre `sha256` obligatoire, re-vérifier dans `require_weights` ; recopier le miroir InvisMark dans un bucket interne.

**SEC-57 — Desktop : `http://` accepté, `config.json` sans 0600, jeton clair en repli** (`main.cjs:126`, vérif. faible/faible/faible)
`normalizeApiBase` n'impose https qu'en l'absence de schéma : un `http://` explicite vers un hôte public envoie le jeton en clair. `config.json` écrit en 0644 (mais `~/Library` est en 0700 sur macOS, la cible). Jeton en clair si `safeStorage` indisponible (quasi inatteignable sur macOS), alors que l'aide dit « Stocké chiffré ». Correction : refuser `http:` sauf loopback ; `config.json` en `{mode:0o600}` ; refuser de mémoriser le jeton si `safeStorage` indisponible ; corriger le texte.

**SEC-58 — Electron : navigation `file://` libre, jeton donné à toute page** (`main.cjs:471`, vérif. moyen/faible/faible, retenu faible)
`isInternalUrl()` accepte toute URL `file://`/`data:` ; `will-navigate` laisse passer un fichier déposé sur la fenêtre ; le handler `wmlab:session` ne vérifie que `event.sender`, pas l'URL du frame, et renvoie `{apiBase, token}` à la page déposée. Scénario : l'utilisateur glisse un `.html` piégé sur la fenêtre, qui lit `window.wmlab.token`. Interaction forte requise. Correction : dans `will-navigate`, n'autoriser que l'URL exacte du renderer et l'origine de `DEV_URL` ; bloquer le drop ; vérifier `event.senderFrame` dans les handlers IPC.

**SEC-59 — ReDoS Markdown** (`Markdown.tsx:12`, vérif. moyen/faible/faible, retenu faible)
La 1re alternative d'`INLINE_RE` a un backtracking O(n³) sur une suite de backticks non fermée ; `renderInline` est synchrone. Le Markdown sert aux notes (mode côte à côte par défaut) et à `spec.hypothesis` (onglet par défaut, copiée dans chaque run). PoC : ~16 Ko ≈ 5–6 min de gel. Les notes sont réparables par l'API, mais `hypothesis` figée dans un run `.wmlab` donne un gel durable de l'onglet. Correction : parser les code spans par balayage linéaire ; borner la longueur de ligne ; plafonner notes/hypothesis côté serveur ; test (20 000 backticks < 50 ms).

### Info

**SEC-60 — Écart avec la cible produit** (`users.py:35`, vérif. 1/1 info)
Modèle actuel : jeton `wml_` permanent créé en CLI, sans e-mail/mdp/inscription/réinitialisation ; LoginGate ne propose qu'un champ jeton ; `CORS allow_origins=["*"]` (mais auth par en-tête, pas cookie, et pas d'`allow_credentials` : non exploitable en l'état). Une inscription ouverte donnerait aujourd'hui le rôle member, donc SEC-01 à tout internaute. Prérequis : corriger d'abord SEC-01/SEC-03 avant toute inscription. Correction : table users (email, argon2id, rôle, statut, vérification) ; inscription sur invitation ou validation admin ; session cookie HttpOnly/SameSite=Strict ; CORS = `public_url` + origine Electron.

**SEC-61 — Fail-open d'autorisation** (`app.py:211`, vérif. 1/1 info)
`current_user` renvoie `ADMIN_USER` si `scope['wmlab_user']` est absent/non-dict ; `_identify` renvoie admin si `token is None` (tests/embedding) ; le middleware laisse passer `scope!='http'`, OPTIONS et les chemins hors `/api/`. Sans effet aujourd'hui (aucune route WebSocket, `/api/health` n'appelle pas `current_user`), mais toute future route WS ou hors `/api/` serait admin/anonyme par défaut. Correction : `current_user` lève 401 si l'identité manque ; authentifier `scope=='websocket'` ; réserver `token None` à un drapeau de test explicite.

**SEC-62 — Fuite d'info via les erreurs 500/404** (`app.py:580`, vérif. 1/1 info)
`internal_error` renvoie `{type}: {exc}` et `missing_file` renvoie `str(exc)` avec chemins absolus. Impact faible (le chemin du workspace et l'env sont déjà exposés au member par ailleurs). Correction : message générique + identifiant de corrélation, détail en `log.exception` ; `missing_file` en chemin relatif.

**SEC-63 — Comparaison par préfixe de chaîne avec l'origine backend** (`main.cjs:542`, vérif. 1/1 info)
`url.startsWith(backendOrigin())` considère `https://lab.example.com.evil.net/...` comme backend et le télécharge via `wc.downloadURL`. Aucun secret n'est joint, pas d'exécution auto, boîte d'enregistrement native : impact quasi nul (cosmétique). Correction : comparer les origines parsées (`new URL(url).origin === new URL(backendOrigin()).origin && pathname.startsWith('/api/')`).

**SEC-64 — `pip_freeze.txt`/`hardware.json` téléchargeables par un member** (`env.py:158`, vérif. 1/1 info)
Chaque run enregistre `env/pip_freeze.txt` et `env/hardware.json`, téléchargeables via `/file`. Sous-cas de SEC-05, conservé pour la cible produit. (Note : `hardware.json` est de toute façon exposé par `_run_detail`, donc la correction doit porter sur les deux.) Correction : liste blanche dans `/file` (SEC-05) ; réduire `env` à `{pip_freeze_lines, hardware réduit}` pour un member dans `_run_detail`.

---

## 4. Plan de remédiation priorisé

### P0 — Bloquant avant tout déploiement public
- **SEC-01, SEC-47** : supprimer le rechargement à chaud de `lab/`, retirer `lab` des racines inscriptibles par HTTP, `require_admin` sur `write_file` et `registry?reload`.
- **SEC-03** : refus par défaut dans `TokenMiddleware` (non-admin + méthode hors GET/HEAD → 403). Mettre à jour ARCHITECTURE.md §12.2 d'abord.
- **SEC-02, SEC-06, SEC-13** : protéger `main` (PR/revue/CODEOWNERS), limiter l'auto-commit aux YAML de `projects/` sur `app/*`, construire l'UI hors du checkout, servir `wmlab` depuis un venv figé.
- **SEC-04, SEC-10** : `require_admin` sur jobs/estimate/stop/cancel/sync/hosts.* ; désactiver l'hôte `local` en hébergé.
- **SEC-05, SEC-14, SEC-64** : politique de lecture par rôle + liste blanche de types pour `/api/runs/{id}/file`.
- **SEC-07, SEC-08, SEC-09, SEC-15** : `rsync --no-links`, validation `SAFE_ID` des `run_id`, quarantaine, rapatriement limité aux runs de l'hôte, ouverture `O_NOFOLLOW`, `core.symlinks false`.
- **SEC-26** : ne plus exécuter en root du code appartenant à `wmlab` (mise à jour depuis une copie root).
- **SEC-11** : sortir les réglages serveur de `workspace.yaml` (fichier non versionné), corriger `resolve` (origin/<ref>).
- **SEC-21, SEC-22, SEC-23** : loader refusant les alias + bornes de profondeur, bornes de taille de corps/YAML.

### P1 — Dans la semaine
- **SEC-17** : journal d'audit JSON vers journald/collecteur + log Caddy.
- **SEC-16, SEC-55** : URL signées pour les fichiers, masquage de la userinfo du remote.
- **SEC-25, SEC-32, SEC-36** : `WMLAB_DATA`/`users.yaml` hors workspace, `WMLAB_TOKEN` fort et non journalisé, env minimal pour les jobs.
- **SEC-12, SEC-18, SEC-20** : forcer `run_id`, inventaire depuis les fichiers réels, facturation côté serveur.
- **SEC-19, SEC-27, SEC-28** : venv immuable + jobs non-root sur GPU, deux clés SSH restreintes, sauvegardes immuables (Object Lock) et clé rclone limitée.
- **SEC-50, SEC-52, SEC-53, SEC-54, SEC-49** : durcissement systemd, permissions `.wmlab`, shell `nologin`, API admin Caddy en socket, groupes de sécurité.
- **SEC-24, SEC-42, SEC-43, SEC-44, SEC-46** : bornes et pagination des listings, `require_admin` sur les routes coûteuses, verrous.
- **SEC-31** : masquer les actions non autorisées dans le desktop.
- **SEC-35** : CSP et en-têtes de sécurité en en-tête HTTP.

### P2 — Amélioration / défense en profondeur
- **SEC-29, SEC-33, SEC-34, SEC-37, SEC-38, SEC-39, SEC-40, SEC-41, SEC-45, SEC-48, SEC-51, SEC-56, SEC-57, SEC-58, SEC-59, SEC-30, SEC-60, SEC-61, SEC-62, SEC-63** : intégrité git, cycle de vie des jetons, rate limiting (prérequis du login mdp), TOCTOU, overrides, suppression d'instance plus sûre, oracle HPO, perfs, GZip, restauration, chaîne d'appro. d'installation, intégrité des baselines, durcissement Electron/desktop, ReDoS, fuites d'info, fail-open, conception de la future auth.

---

## 5. Annexes

### 5.1 Examiné et jugé sain, ou à confiance limitée
- **Authentification du périmètre** : comparaison des jetons en temps constant, 160–192 bits ; brute force irréaliste (base de SEC-34). Toutes les routes `/api/*` sauf `/api/health` exigent un jeton.
- **Chargement des checkpoints** : `torch.load(..., weights_only=True)` partout (models/__init__.py:53, train/runtime.py:167, models/pretrained.py:37) — un `.pt` piégé ne donne pas de RCE (limite SEC-09, SEC-38, SEC-56).
- **Au rechargement d'un run** : refus si le sha256 des poids diffère de celui du run (atténue SEC-56).
- **Non vérifiés faute d'exécution** : Caddy (taille de corps par défaut, délais de reverse_proxy, X-Forwarded-For), le build Vite (SEC-13 repose sur le comportement documenté de Vite), le comportement Electron à l'exécution (SEC-57/58, confiance moyenne), un journal Optuna rapatrié malformé, la concurrence git commit/pull, les scripts `desktop/scripts/*.cjs` et `.claude/hooks` (outils de dev).
- **Hors du dépôt, non vérifiable** : protection de la branche `main` sur GitHub, portée IAM de `SCW_SECRET_KEY`, groupes de sécurité Scaleway, état réel d'`unattended-upgrades`.

### 5.2 Constats écartés (examinés, non retenus)
- **sha256 des checkpoints absent** — réfuté : la correction ne protège contre aucun des scénarios (sur une GPU compromise le hash serait calculé sur la machine compromise ; le canal SSH garantit déjà l'intégrité du transfert). La vraie cause est l'exécution de code member en root sur GPU (SEC-19). Info.
- **Résolution d'une révision git sur la branche locale** — réfuté : comportement documenté (ARCHITECTURE.md:1282) ; n'ajoute aucun privilège (le member exécute du code qu'il a lui-même écrit). Le sujet réel est l'autorisation (SEC-03). Point fonctionnel voisin couvert par SEC-11.
- **stop_run via `pid` d'un `run.json` rapatrié** — réfuté : doublon sans valeur ajoutée (le member peut déjà annuler n'importe quel job par SEC-04) ; SIGTERM seulement, cibles limitées aux process wmlab.
- **CVE rsync client / mises à jour non garanties** — réfuté : Ubuntu 24.04 rétroporte les correctifs rsync, `unattended-upgrades` actif par défaut. Le vrai point voisin (symlinks) est SEC-07. Info.
- **`/etc/wmlab/env` lisible par le groupe wmlab et chargé par backup** — réfuté : le groupe est le groupe privé de wmlab ; tout process wmlab a déjà ces secrets autrement (SEC-36). Info.
- **`load_plugins` met le workspace en tête de `sys.path`** — réfuté : le workspace y est déjà via `python -m` ; aucune dépendance `datasets`/`projects`/`benchmarks` n'existe ; englobé par SEC-01.
- **Plancher `torch>=2.5`** — réfuté : `uv.lock` fixe torch 2.14 ; aucune installation documentée n'installe une version vulnérable ; `weights_only=True`. Durcissement (monter le plancher, `--locked`).
- **`.gitignore` incomplet** — réfuté : l'auto-commit du serveur fait `git add` d'un seul fichier confiné (pas de `git add -A`) ; poids et secrets sont hors de l'arbre git. Hygiène (info).
- **Mode historique (sans git) : rsync du workspace entier** — réfuté : ce chemin n'est jamais emprunté en mode hébergé (toujours un export `git archive`) ; il ne concerne que l'usage local sur le Mac du propriétaire. Hygiène.
- **Fuses Electron / devtools / app non signée / `workspace.json`** — réfuté : exige un accès local préalable ; le renderer est en sandbox ; `workspace.json` ignoré par git et régénéré par poste. Bonne pratique pour une future distribution binaire signée.
