#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────
# Watermark Lab — tests des scripts de déploiement SANS root ni Ubuntu (mode --dry-run).
#
#   bash deploy/tests_dry_run.sh
#
# Vérifie : syntaxe bash (bash -n) de tous les scripts, sortie de install_server.sh en dry-run
# (clonage d'un dossier vide, puis mise à jour du dépôt courant), refus des mauvaises options,
# patch_workspace.py sur une copie de workspace.yaml, backup.sh en dry-run (mode local).
# Code de retour 0 si tout passe. Aucun fichier du dépôt n'est modifié.
# ─────────────────────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/wmlab-deploy-tests.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

PASS=0; FAILS=0
check() {   # check "libellé" motif fichier  → grep -E
  local label="$1" pattern="$2" file="$3"
  if grep -Eq -- "$pattern" "$file"; then PASS=$((PASS + 1)); printf ' ✓ %s\n' "$label"
  else FAILS=$((FAILS + 1)); printf ' ✗ %s (motif absent : %s)\n' "$label" "$pattern"; fi
}
check_absent() {
  local label="$1" pattern="$2" file="$3"
  if grep -Eq -- "$pattern" "$file"; then FAILS=$((FAILS + 1)); printf ' ✗ %s (motif présent : %s)\n' "$label" "$pattern"
  else PASS=$((PASS + 1)); printf ' ✓ %s\n' "$label"; fi
}

echo "==> Syntaxe (bash -n)"
for f in "$HERE"/install_server.sh "$HERE"/backup.sh "$HERE"/tests_dry_run.sh "$ROOT"/scripts/remote_bootstrap.sh; do
  bash -n "$f" && printf ' ✓ %s\n' "${f#"$ROOT"/}"
done
python3 -m py_compile "$HERE/patch_workspace.py" && printf ' ✓ deploy/patch_workspace.py (py_compile)\n'

echo "==> --help"
bash "$HERE/install_server.sh" --help > "$TMP/help.txt"
check "aide : options listées" "--repo URL" "$TMP/help.txt"
check "aide : --dry-run documenté" "--dry-run" "$TMP/help.txt"

echo "==> Options invalides"
if bash "$HERE/install_server.sh" --dry-run --dir /opt/wmlab --bidule 2>"$TMP/err1.txt"; then
  FAILS=$((FAILS + 1)); echo ' ✗ une option inconnue devrait échouer'
else PASS=$((PASS + 1)); echo ' ✓ option inconnue refusée'; fi
if bash "$HERE/install_server.sh" --dry-run --dir "$TMP/vide" --admin justin 2>"$TMP/err2.txt" >/dev/null; then
  FAILS=$((FAILS + 1)); echo ' ✗ sans --repo ni dépôt existant, le script devrait échouer'
else check "sans --repo : message explicite" "--repo URL est obligatoire" "$TMP/err2.txt"; fi
if bash "$HERE/install_server.sh" --dry-run --repo x --admin "Justin Reverdi" 2>"$TMP/err3.txt" >/dev/null; then
  FAILS=$((FAILS + 1)); echo ' ✗ --admin avec espace/majuscule devrait échouer'
else check "--admin invalide refusé" "identifiant en minuscules" "$TMP/err3.txt"; fi
if bash "$HERE/install_server.sh" --dry-run --repo x --admin admin 2>"$TMP/err4.txt" >/dev/null; then
  FAILS=$((FAILS + 1)); echo ' ✗ --admin admin (nom réservé) devrait échouer'
else check "--admin admin refusé (réservé au jeton serveur)" "réservé" "$TMP/err4.txt"; fi
if bash "$HERE/install_server.sh" --dry-run --repo x 2>"$TMP/err5.txt" >/dev/null; then
  FAILS=$((FAILS + 1)); echo ' ✗ sans --admin, le script devrait échouer'
else check "--admin obligatoire" "--admin NAME est obligatoire" "$TMP/err5.txt"; fi

echo "==> Dry-run 1 : installation neuve (clonage dans un dossier inexistant)"
OUT1="$TMP/dry1.txt"
bash "$HERE/install_server.sh" --dry-run --repo git@github.com:equipe/wmlab.git --branch main \
  --dir "$TMP/opt-wmlab" --domain lab.example.com --admin justin > "$OUT1" 2>&1
[[ ! -e "$TMP/opt-wmlab" ]] && { PASS=$((PASS + 1)); echo ' ✓ dossier cible non créé'; } \
  || { FAILS=$((FAILS + 1)); echo ' ✗ le dry-run a créé le dossier cible'; }
check "paquets apt (git rsync curl…)" 'apt-get install -y -qq git rsync curl' "$OUT1"
check "utilisateur de service wmlab" 'useradd --system --create-home --home-dir /home/wmlab' "$OUT1"
check "clé SSH ed25519" 'ssh-keygen -q -t ed25519' "$OUT1"
check "uv : installateur officiel épinglé" 'https://astral.sh/uv/0\.12\.19/install\.sh' "$OUT1"
check "uv : installé dans /usr/local/bin" 'UV_INSTALL_DIR=/usr/local/bin' "$OUT1"
check "Node 22 via NodeSource" 'https://deb\.nodesource\.com/setup_22\.x' "$OUT1"
check "apt install nodejs" 'apt-get install -y -qq nodejs' "$OUT1"
check "git clone de la branche" 'git clone --branch main git@github\.com:equipe/wmlab\.git' "$OUT1"
check "identité git pour les commits de l'app" 'config user\.email wmlab@lab\.example\.com' "$OUT1"
check "uv sync --extra server --extra train" 'sync --extra server --extra train' "$OUT1"
check_absent "pas d'--extra cpu sans extra cpu (dossier neuf)" 'sync --extra server --extra train --extra cpu' "$OUT1"
check "npm ci sans binaire Electron" 'ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm --prefix .*/desktop ci' "$OUT1"
check "npm run build" 'npm --prefix .*/desktop run build' "$OUT1"
check "/etc/wmlab/env : WMLAB_WORKSPACE" 'WMLAB_WORKSPACE='"$TMP"'/opt-wmlab' "$OUT1"
check "/etc/wmlab/env : WMLAB_TOKEN généré" 'WMLAB_TOKEN=<openssl rand -hex 24>' "$OUT1"
check "/etc/wmlab/env : SCW_SECRET_KEY commentée" '^ *\| # SCW_SECRET_KEY=' "$OUT1"
check "/etc/wmlab/env : droits 0640 root:wmlab" 'écriture de /etc/wmlab/env \(0640, root:wmlab\)' "$OUT1"
check "wmlab users add <admin> --role admin" 'wmlab\.cli users add justin --role admin' "$OUT1"
check "unité systemd wmlab-server" 'écriture de /etc/systemd/system/wmlab-server\.service' "$OUT1"
check "ExecStart conforme §12.7" 'ExecStart=.*/\.venv/bin/python -m wmlab\.cli server --port 8765 --host 127\.0\.0\.1 --workspace ' "$OUT1"
check "service : User=wmlab, Restart=always, EnvironmentFile" 'Restart=always' "$OUT1"
check "service : EnvironmentFile=/etc/wmlab/env" 'EnvironmentFile=/etc/wmlab/env' "$OUT1"
check "systemctl enable wmlab-server" 'systemctl enable wmlab-server' "$OUT1"
check "Caddy : dépôt officiel cloudsmith" 'dl\.cloudsmith\.io/public/caddy/stable/gpg\.key' "$OUT1"
check "Caddy : apt install caddy" 'apt-get install -y -qq caddy' "$OUT1"
check "Caddyfile : domaine" '^ *\| lab\.example\.com \{' "$OUT1"
check "Caddyfile : reverse_proxy 127.0.0.1:8765" 'reverse_proxy 127\.0\.0\.1:8765' "$OUT1"
check "Caddy : validation du Caddyfile" 'caddy validate --config /etc/caddy/Caddyfile' "$OUT1"
check "sauvegarde : minuterie quotidienne" 'systemctl enable --now wmlab-backup\.timer' "$OUT1"
check "sauvegarde : dossier local" '/var/backups/wmlab' "$OUT1"
check "workspace.yaml : patch hosted" 'patch_workspace\.py --file .*/workspace\.yaml --branch main --public-url https://lab\.example\.com' "$OUT1"
check "workspace.yaml : skip-worktree (arbre propre pour GitSync)" 'update-index --skip-worktree workspace\.yaml' "$OUT1"
check "résumé : URL publique" 'URL publique *: https://lab\.example\.com' "$OUT1"
check "résumé : clé publique à déposer sur les machines GPU" 'machine GPU' "$OUT1"

echo "==> Dry-run 2 : dépôt déjà cloné (le dépôt courant), sans domaine"
OUT2="$TMP/dry2.txt"
bash "$HERE/install_server.sh" --dry-run --dir "$ROOT" --branch main --admin owner > "$OUT2" 2>&1
check "mise à jour : fetch + pull --ff-only" 'git -C .* pull --ff-only origin main' "$OUT2"
check_absent "pas de clone quand .git existe" 'git clone' "$OUT2"
if grep -q '"cpu"' "$ROOT/pyproject.toml"; then
  check "extra cpu détecté dans pyproject.toml" 'sync --extra server --extra train --extra cpu' "$OUT2"
else
  check_absent "pas d'extra cpu (absent de pyproject.toml)" '--extra cpu' "$OUT2"
fi
check "sans --domain : Caddy ignoré, tunnel SSH indiqué" 'pas de --domain' "$OUT2"
check_absent "sans --domain : pas d'installation de caddy" 'apt-get install -y -qq caddy' "$OUT2"
check "unité systemd prise dans deploy/ (marqueurs remplacés)" 'WorkingDirectory='"$ROOT"'$' "$OUT2"
check_absent "aucun marqueur __WMLAB_ non remplacé" '__WMLAB_' "$OUT2"
check "patch hosted affiché (dry-run) : enabled true" '^ *\| *enabled: true' "$OUT2"
check "patch hosted : public_url null sans domaine" '^ *\| *public_url: null' "$OUT2"
if git -C "$ROOT" diff --quiet -- workspace.yaml 2>/dev/null; then
  PASS=$((PASS + 1)); echo ' ✓ workspace.yaml du dépôt non modifié'
else
  FAILS=$((FAILS + 1)); echo ' ✗ workspace.yaml du dépôt modifié par le dry-run'
fi

echo "==> patch_workspace.py sur une copie"
cp "$ROOT/workspace.yaml" "$TMP/ws.yaml"
python3 "$HERE/patch_workspace.py" --file "$TMP/ws.yaml" --branch main --public-url https://lab.example.com >/dev/null
check "hosted.enabled: true" '^  enabled: true' "$TMP/ws.yaml"
check "hosted.git_branch" '^  git_branch: "main"' "$TMP/ws.yaml"
check "hosted.public_url" '^  public_url: "https://lab\.example\.com"' "$TMP/ws.yaml"
check "budget conservé" '^  total_eur: 40000' "$TMP/ws.yaml"
python3 "$HERE/patch_workspace.py" --file "$TMP/ws.yaml" --branch main --public-url https://lab.example.com | grep -q 'déjà à jour' \
  && { PASS=$((PASS + 1)); echo ' ✓ idempotent (déjà à jour)'; } || { FAILS=$((FAILS + 1)); echo ' ✗ second passage non idempotent'; }
[[ "$(grep -c '^hosted:' "$TMP/ws.yaml")" == "1" ]] && { PASS=$((PASS + 1)); echo ' ✓ un seul bloc hosted'; } \
  || { FAILS=$((FAILS + 1)); echo ' ✗ bloc hosted dupliqué'; }
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  "$ROOT/.venv/bin/python" - "$TMP/ws.yaml" <<'PY' && { PASS=$((PASS + 1)); echo ' ✓ YAML valide (PyYAML)'; } || { FAILS=$((FAILS + 1)); echo ' ✗ YAML invalide'; }
import sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
assert d["hosted"] == {"enabled": True, "git_branch": "main", "auto_pull_s": 60, "auto_commit": True,
                       "public_url": "https://lab.example.com"}, d["hosted"]
PY
fi

echo "==> backup.sh en dry-run (mode local forcé)"
mkdir -p "$TMP/ws/.wmlab/runs" "$TMP/ws/.wmlab/cache"
OUT3="$TMP/backup.txt"
WMLAB_WORKSPACE="$TMP/ws" WMLAB_BACKUP_DIR="$TMP/backups" bash "$HERE/backup.sh" --local --dry-run > "$OUT3" 2>&1
check "archive tar.zst" 'tar --create --zstd --file .*/wmlab-[0-9]{8}-[0-9]{6}\.tar\.zst' "$OUT3"
check "exclusions index/cache/logs" '--exclude \.wmlab/index\.sqlite .*--exclude \.wmlab/cache .*--exclude \.wmlab/logs' "$OUT3"
check "checkpoints exclus par défaut" 'exclude \\\*\.pt' "$OUT3"
check "rotation 14 jours" '-mtime \+14 -delete' "$OUT3"
bash "$HERE/backup.sh" --help | grep -q 'rclone' && { PASS=$((PASS + 1)); echo ' ✓ aide backup.sh'; } || { FAILS=$((FAILS + 1)); echo ' ✗ aide backup.sh'; }

echo
if (( FAILS == 0 )); then
  echo "TESTS DRY-RUN OK ($PASS vérifications)"
else
  echo "TESTS DRY-RUN : $FAILS échec(s), $PASS réussite(s)"; exit 1
fi
