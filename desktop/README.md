# Watermark Lab — application de bureau

Electron + React 19 + Vite + Tailwind v4. En mode **local** (défaut), le main process lance le
backend Python (`.venv/bin/python -m wmlab.cli server`) du workspace, puis affiche l'interface.
En mode **distant** (ARCHITECTURE.md §12.6), l'application se connecte au serveur central de
l'équipe avec un compte (e-mail + mot de passe) : aucun backend n'est lancé sur le Mac.

## Prérequis

- Environnement Python du dépôt : `uv sync --extra train --extra server` (à la racine).
- Node ≥ 22.12 (exigé par Electron 44 ; testé avec Node 26) : `npm install` dans `desktop/` (le binaire Electron est téléchargé au premier lancement).

## Commandes (dans `desktop/`)

| commande | rôle |
|---|---|
| `npm run dev` | Vite (HMR) + Electron sur le serveur de dev |
| `npm run build` | vérification TypeScript (`tsc -b`) + build Vite dans `dist/` |
| `npm start` | Electron sur le build (`dist/`) |
| `npm run selftest` | démarre backend + interface sans fenêtre, affiche `SELFTEST OK` / `SELFTEST FAIL: …` |
| `npm run dist` | produit `release/mac-arm64/Watermark Lab.app` (workspace mémorisé dans l'app) |
| `npm test` | tests unitaires des utilitaires (`node --test`) |
| `npm run render-check` | rendu réel de chaque page contre un backend démarré (voir ci-dessous) |
| `npm run mock` | backend factice Node (§9 + §12, comptes et rôles compris) servant `dist/` sur le port 8781 (voir ci-dessous) |
| `npm run remote-check` | vérifie sans fenêtre le mode distant d'Electron contre le mock : `REMOTE-CHECK OK` / `FAIL` (voir ci-dessous) |
| `npm run icon` | régénère `build/icon.png` (PIL) |

### Vérification de rendu (`scripts/render-check.cjs`)

Démarrer un backend sur le workspace, puis :

```bash
cd .. && .venv/bin/python -m wmlab.cli server --port 8799 --token test   # dans un autre terminal
cd desktop && npm run build && npm run render-check -- --dark --launch
```

Une fenêtre Electron invisible charge chaque route du build servi par FastAPI, relève les
erreurs/avertissements console et les exceptions, et enregistre une capture PNG par route dans
`desktop/.selftest/` (+ `report.json`). Le jeton (`--token`, jeton serveur = admin implicite) est
déposé dans le `localStorage` de la page comme le ferait l'écran de connexion : l'application ne
lit jamais `?token=`. `--dark` refait 5 routes clés en thème sombre ; `--launch`
ouvre le dialogue « Nouveau lancement », lance un entraînement *smoke* local et vérifie la page du
run jusqu'à son achèvement (crée un run dans `.wmlab/runs/`). `--dialog` ouvre et capture seulement
le dialogue (champ « Révision git » compris, sans lancer de job) ; `--login` exerce l'écran de
connexion (formulaire sans session, mot de passe faux, compte en attente, formulaire
d'inscription, connexion e-mail + mot de passe `--email` / `--password`, changement forcé d'un
mot de passe temporaire, session refusée, inscription fermée — les étapes qui créent ou modifient
un compte ne tournent que contre le mock) ; `--member` se connecte en membre (`--member-email`,
défaut `marie@example.com`) et vérifie l'interface en lecture seule sur les routes clés (aucun
bouton d'écriture, ni YAML, ni journaux, ni bibliothèque, Réglages réduits, aucun 403) ;
`--users` exerce Réglages → Utilisateurs (création d'un compte et mot de passe temporaire,
approbation ; mock seulement) ; `--hosts` exerce la page Machines (inventaire, job de
rapatriement, dialogue de retrait — prévu pour le mock). `--routes /a,/b` restreint la
liste ; `--base` / `--token` changent le backend visé. Sortie : `RENDER-CHECK OK` (code 0) ou `FAIL`.

### Backend factice (`scripts/mock-api.cjs`)

Pour vérifier l'interface du mode hébergé sans Python ni serveur central :

```bash
npm run build && npm run mock                       # http://127.0.0.1:8781/ (se connecter : voir ci-dessous)
npm run render-check -- --base http://127.0.0.1:8781 --token test \
  --routes "/,/runs,/hosts,/settings,/runs/{run}" --dialog --hosts --login --member --users
```

Le mock sert `dist/` et répond en mémoire à toutes les routes du §9 et du §12 avec des données
plausibles : trois projets (les YAML sont lus dans le workspace, jamais écrits), six runs, une étude
HPO, des jobs (dont un entraînement au rapatriement partiel et un job `retrieve` terminé), quatre
machines — `local`, `scw-l40s` (instance Scaleway, 3 checkpoints non rapatriés, un job actif),
`scw-h100` (propre) et `scw-old` (retirée) —, l'état git (`main @ 3f2a9c1`, hébergé) et cinq
comptes, tous avec le mot de passe `motdepasse-demo-123` : `justin@example.com` (admin),
`marie@example.com` (membre), `paul@example.com` (inscription en attente), `marc@example.com`
(désactivé) et `nina@example.com` (membre au mot de passe temporaire, à changer à la connexion).
Authentification comme le serveur réel (§9, §12.2) : jeton serveur `test` (`--token`, admin
implicite « admin », `auth: "server_token"`) ou jeton de session `wmls_…` émis par
`POST /api/auth/login` ; la session `member` (marie) est pré-émise pour les essais rapides ; tout
autre jeton ⇒ 401. Un membre est en lecture seule : 403 sur toute requête non-GET (hors
déconnexion / mot de passe) et sur les routes **admin**, réponses expurgées (spec, environnement,
overrides, paramètres et plan de phases d'un run, erreurs réduites à leur dernière ligne,
overrides des jobs, chemins des machines et des datasets). `?token=`
n'est accepté que pour les fichiers d'un run et `costs.csv`. Inscription, approbation, rôles,
désactivation, réinitialisation (mot de passe temporaire) et limitation de débit (10 échecs de
connexion par e-mail ⇒ 429 + `Retry-After`) sont simulés.
Les actions sont simulées : le rapatriement se termine après ~5 s et efface l'avertissement, le
retrait est refusé (409) tant que des checkpoints manquent, `PUT /api/files` renvoie un commit
poussé (contenu contenant `unpushed` ⇒ commit non poussé), `POST /api/git/pull` avance la révision.
`POST /api/mock/reset` remet l'état initial (comptes et sessions compris ; `render-check --hosts`,
`--login` et `--users` l'appellent, leurs scénarios sont donc rejouables sans redémarrer le mock).
Options : `--port`, `--token` (jeton serveur).

### Vérification du mode distant (`scripts/remote-check.cjs`)

```bash
npm run mock                                          # dans un autre terminal
npm run remote-check -- --base http://127.0.0.1:8781 --token test   # [--keep] conserve le profil
npm run remote-check -- --base http://127.0.0.1:8781 --login        # dialogue e-mail + mot de passe
```

Le script crée un profil `userData` isolé et temporaire contenant `config.json` =
`{mode: "remote", apiBase, token}` (avec `--login` : sans jeton), puis charge le **vrai** main process (`electron/main.cjs`)
avec `BrowserWindow.show` neutralisé (aucune fenêtre) et `child_process.spawn` instrumenté (tout
lancement de process — le backend Python en particulier — est consigné et refusé). Il vérifie
ensuite dans le renderer `window.wmlab.mode === "remote"`, `apiBase`, la présence du jeton,
`[data-app-ready]` (le jeton a atteint le renderer et le serveur a répondu), le nom de l'utilisateur
dans la barre latérale, le menu « Serveur » (« Se connecter à un serveur distant… », « Utiliser le
backend local » activée, « Connecté à <serveur> ») et l'absence de process lancé ; capture dans
`desktop/.selftest/remote.png`. Avec `--login` (`--email` / `--password`, défaut : l'admin du
mock) : sans session, l'écran « Connexion requise » s'affiche et le dialogue de connexion s'ouvre
seul ; le script y saisit d'abord un mauvais mot de passe (le message du serveur doit être
relayé), puis le bon ; il vérifie ensuite que `config.json` contient la session `wmls_…` et jamais
le mot de passe, puis que « Se déconnecter » révoque la session sur le serveur (`/api/me` ⇒ 401)
et l'oublie (`safeStorage` est désactivé pendant ce contrôle : aucun accès au trousseau).
Sortie : `REMOTE-CHECK OK` (code 0) ou `FAIL: …` (code 1).

Pour un essai manuel avec un profil isolé : `WMLAB_USER_DATA=/tmp/wmlab-profil npm start`
(`config.json`, journaux et caches vont dans ce dossier au lieu de
`~/Library/Application Support/Watermark Lab`).

## Workspace

Ordre de résolution : variable `WMLAB_WORKSPACE` → workspace choisi par l'utilisateur
(`~/Library/Application Support/Watermark Lab/config.json`) → `resources/workspace.json` (écrit par
`scripts/write-workspace.cjs`, embarqué dans l'app : valeur par défaut du premier lancement)
→ sélecteur de dossier. Menu **Watermark Lab → Choisir le workspace…** pour en changer : le choix
est mémorisé dans `config.json` et prime sur le workspace embarqué aux lancements suivants
(supprimer `config.json` pour revenir au workspace embarqué). `npm run selftest` ignore `config.json`.

Un brouillon non enregistré (notes d'un run ou d'un projet, fichier YAML) survit aux changements
d'onglet et de page ; recharger (⌘R) ou fermer la fenêtre demande confirmation tant qu'il en reste un.

Journal du backend : `~/Library/Application Support/Watermark Lab/logs/backend.log`
(menu **Affichage → Ouvrir le journal du backend**, redémarrage : ⌥⌘R).

## Mode distant, comptes et rôles (§12.2, §12.6)

- **Comptes** : chaque membre a un compte (e-mail + mot de passe, 12 caractères minimum). Il le
  demande depuis l'écran de connexion (« Créer un compte » : identifiant, e-mail, mot de passe —
  le compte reste en attente jusqu'à l'approbation d'un administrateur ; lien masqué quand
  l'inscription est fermée, `hosted.allow_registration: false`), ou un administrateur le crée
  dans **Réglages → Utilisateurs** (« Créer un compte » : un mot de passe temporaire est affiché
  une seule fois). Un mot de passe temporaire (création, réinitialisation) doit être changé à la
  première connexion : l'application n'affiche rien d'autre que « Définir un nouveau mot de passe »
  tant que ce n'est pas fait. Les administrateurs approuvent les inscriptions (badge « N en attente »
  à côté de Réglages), changent les rôles, désactivent / réactivent et réinitialisent les mots de
  passe ; aucune action n'est proposée sur son propre compte (« Mon compte » pour le mot de passe).
- **Rôles** : `admin` = tout ; `member` = **lecture seule sans accès au code** (`usePermissions()`
  dans `src/lib/queries.ts`) : ni « Nouveau lancement », ni création de projet / expérience / plan,
  ni éditeur YAML (les expériences et plans restent listés avec leurs métadonnées ; leurs routes
  affichent « Réservé aux administrateurs »), ni bibliothèque, ni onglet Journaux, ni configuration
  du run (spec, overrides, paramètres, plan de phases, traceback), ni édition des notes et des tags, ni arrêt / annulation /
  rapatriement, ni action sur les machines (champs SSH expurgés affichés « — ») ; Réglages réduit à
  « Mon compte », « Connexion » et « Apparence ». Le serveur reste l'autorité (403).
- **Application** : menu **Serveur → Se connecter à un serveur distant…** ouvre un petit dialogue
  (adresse du serveur, e-mail, mot de passe) ; le main process vérifie l'adresse sur `/api/health`,
  appelle `POST /api/auth/login`, relaie tel quel le message du serveur en cas de refus (mot de
  passe faux, compte en attente ou désactivé, trop de tentatives) et affiche « Connecté en tant que
  <nom> ». Seul le **jeton de session** est mémorisé dans `config.json` (`{mode: "remote", apiBase,
  tokenEncrypted}` — chiffré par `safeStorage`, en clair dans `token` seulement si le chiffrement
  système est indisponible) ; le mot de passe n'est jamais conservé. Au lancement suivant, la
  session est vérifiée (`/api/me`) : valide, l'application s'ouvre sans démarrer de backend ;
  expirée ou révoquée (30 jours, déconnexion, changement de mot de passe, compte désactivé),
  l'écran « Session expirée » s'affiche et le dialogue de connexion s'ouvre. **Serveur → Utiliser le
  backend local** revient au mode local ; « Choisir le workspace… » aussi. `WMLAB_WORKSPACE` et
  `npm run selftest` forcent toujours le mode local ; `WMLAB_USER_DATA=<dossier>` isole le profil
  (`config.json`, journaux). `window.wmlab = {apiBase, token, platform, mode, openConnectDialog,
  useLocalBackend, disconnect}`. En mode distant, le renderer (chargé depuis `dist/`) parle au
  serveur en CORS avec l'en-tête `X-WMLab-Token` ; les liens externes ne s'ouvrent dans le
  navigateur que s'ils sont en `http(s):` ou `mailto:`.
- **Navigateur** : le serveur central sert `dist/` ; sans session (ou si elle est refusée — 401 sur
  `/api/me`), l'écran de connexion (`LoginGate`) demande l'e-mail et le mot de passe (et l'adresse
  du serveur si la page n'est pas servie par le backend) ; le jeton de session est mémorisé dans
  `localStorage` (`wmlab.token`, `wmlab.apiBase` si différent de l'origine). `?token=` n'est plus
  lu dans l'URL (seuls les fichiers d'un run et l'export CSV portent le jeton en paramètre).
- « Se déconnecter » (pied de la barre latérale, Réglages → Connexion, écran de changement de mot
  de passe) révoque la session (`POST /api/auth/logout`, au mieux) puis l'oublie : retour à l'écran
  de connexion (navigateur) ou au dialogue de connexion (application). En mode local (backend lancé
  par l'application avec le jeton serveur, administrateur implicite), il n'y a ni écran de
  connexion, ni déconnexion, ni « Mon compte ».

## Navigateur

Le backend sert aussi `dist/` : ouvrir `http://127.0.0.1:<port>/` et se connecter avec un compte
(contre le mock : `justin@example.com` / `motdepasse-demo-123`). En dev pur navigateur :
`VITE_API_BASE=http://127.0.0.1:8765 VITE_API_TOKEN=… npx vite` (jeton serveur ou de session).

Guide du design system : [DESIGN.md](DESIGN.md).
