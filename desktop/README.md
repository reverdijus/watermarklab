# Watermark Lab — application de bureau

Electron + React 19 + Vite + Tailwind v4. En mode **local** (défaut), le main process lance le
backend Python (`.venv/bin/python -m wmlab.cli server`) du workspace, puis affiche l'interface.
En mode **distant** (ARCHITECTURE.md §12.6), l'application se connecte au serveur central de
l'équipe avec un jeton personnel : aucun backend n'est lancé sur le Mac.

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
| `npm run mock` | backend factice Node (§9 + §12) servant `dist/` sur le port 8781 (voir ci-dessous) |
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
`desktop/.selftest/` (+ `report.json`). `--dark` refait 5 routes clés en thème sombre ; `--launch`
ouvre le dialogue « Nouveau lancement », lance un entraînement *smoke* local et vérifie la page du
run jusqu'à son achèvement (crée un run dans `.wmlab/runs/`). `--dialog` ouvre et capture seulement
le dialogue (champ « Révision git » compris, sans lancer de job) ; `--login` capture l'écran de
connexion sans jeton puis avec un jeton refusé ; `--hosts` exerce la page Machines (inventaire,
job de rapatriement, dialogue de retrait — prévu pour le mock). `--routes /a,/b` restreint la
liste ; `--base` / `--token` changent le backend visé. Sortie : `RENDER-CHECK OK` (code 0) ou `FAIL`.

### Backend factice (`scripts/mock-api.cjs`)

Pour vérifier l'interface du mode hébergé sans Python ni serveur central :

```bash
npm run build && npm run mock                       # http://127.0.0.1:8781/?token=test
npm run render-check -- --base http://127.0.0.1:8781 --token test \
  --routes "/,/runs,/hosts,/settings,/runs/{run}" --dialog --hosts --login
```

Le mock sert `dist/` et répond en mémoire à toutes les routes du §9 et du §12 avec des données
plausibles : trois projets (les YAML sont lus dans le workspace, jamais écrits), six runs, une étude
HPO, des jobs (dont un entraînement au rapatriement partiel et un job `retrieve` terminé), quatre
machines — `local`, `scw-l40s` (instance Scaleway, 3 checkpoints non rapatriés, un job actif),
`scw-h100` (propre) et `scw-old` (retirée) —, l'état git (`main @ 3f2a9c1`, hébergé) et trois
utilisateurs. Jetons : `test` (admin « justin »), `member` (membre « lea »), tout autre ⇒ 401.
Les actions sont simulées : le rapatriement se termine après ~5 s et efface l'avertissement, le
retrait est refusé (409) tant que des checkpoints manquent, `PUT /api/files` renvoie un commit
poussé (contenu contenant `unpushed` ⇒ commit non poussé), `POST /api/git/pull` avance la révision.
`POST /api/mock/reset` remet l'état initial (`render-check --hosts` l'appelle avant son scénario, qui
est donc rejouable sans redémarrer le mock). Options : `--port`, `--token` (jeton admin).

### Vérification du mode distant (`scripts/remote-check.cjs`)

```bash
npm run mock                                          # dans un autre terminal
npm run remote-check -- --base http://127.0.0.1:8781 --token test   # [--keep] conserve le profil
```

Le script crée un profil `userData` isolé et temporaire contenant `config.json` =
`{mode: "remote", apiBase, token}`, puis charge le **vrai** main process (`electron/main.cjs`)
avec `BrowserWindow.show` neutralisé (aucune fenêtre) et `child_process.spawn` instrumenté (tout
lancement de process — le backend Python en particulier — est consigné et refusé). Il vérifie
ensuite dans le renderer `window.wmlab.mode === "remote"`, `apiBase`, la présence du jeton,
`[data-app-ready]` (le jeton a atteint le renderer et le serveur a répondu), le nom de l'utilisateur
dans la barre latérale, le menu « Serveur » (« Se connecter à un serveur distant… », « Utiliser le
backend local » activée, « Connecté à <serveur> ») et l'absence de process lancé ; capture dans
`desktop/.selftest/remote.png`. Sortie : `REMOTE-CHECK OK` (code 0) ou `FAIL: …` (code 1).

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

## Mode distant et connexion (§12.6)

- **Application** : menu **Serveur → Se connecter à un serveur distant…** ouvre un petit dialogue
  (adresse du serveur, jeton personnel) ; l'adresse est vérifiée sur `/api/health` puis le jeton
  sur `/api/me`. La session est mémorisée dans `config.json` (`{mode: "remote", apiBase,
  tokenEncrypted}` — jeton chiffré par `safeStorage`, en clair dans `token` seulement si le
  chiffrement système est indisponible). Au lancement suivant, l'application se connecte
  directement au serveur sans démarrer de backend. **Serveur → Utiliser le backend local** revient
  au mode local ; « Choisir le workspace… » aussi. `WMLAB_WORKSPACE` et `npm run selftest` forcent
  toujours le mode local ; `WMLAB_USER_DATA=<dossier>` isole le profil (`config.json`, journaux).
  `window.wmlab = {apiBase, token, platform, mode, openConnectDialog, useLocalBackend, disconnect}`.
  En mode distant, le renderer (chargé depuis `dist/`) parle au serveur en CORS avec l'en-tête
  `X-WMLab-Token` ; les liens externes ne s'ouvrent dans le navigateur que s'ils sont en `http(s):`
  ou `mailto:`. Si le jeton mémorisé est refusé au démarrage, l'interface affiche « jeton refusé »
  avec les boutons « Se connecter à un serveur distant… » et « Utiliser le backend local ».
- **Navigateur** : le serveur central sert `dist/` ; sans jeton (ou si le jeton est refusé —
  401 sur `/api/me`), l'écran de connexion (`LoginGate`) demande le jeton (et l'adresse du serveur
  si la page n'est pas servie par le backend) ; il est mémorisé dans `localStorage`
  (`wmlab.token`, `wmlab.apiBase` si différent de l'origine). `?token=<jeton>` dans l'URL reste
  accepté (et retiré de l'URL une fois mémorisé).
- « Se déconnecter » (pied de la barre latérale, Réglages → Connexion) oublie le jeton : retour à
  l'écran de connexion (navigateur) ou au dialogue de connexion (application). En mode local
  (backend lancé par l'application), il n'y a ni écran de connexion ni déconnexion.
- Rôles : un membre voit les boutons réservés aux administrateurs (retrait, extinction, liste des
  utilisateurs) désactivés ou masqués ; le serveur reste l'autorité (403).

## Navigateur

Le backend sert aussi `dist/` : ouvrir `http://127.0.0.1:<port>/?token=<jeton>` (ou se connecter
sur l'écran de connexion). En dev pur navigateur : `VITE_API_BASE=http://127.0.0.1:8765
VITE_API_TOKEN=… npx vite`.

Guide du design system : [DESIGN.md](DESIGN.md).
