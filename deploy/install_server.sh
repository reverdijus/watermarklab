#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────
# Watermark Lab — installation du SERVEUR CENTRAL (mode hébergé) — ARCHITECTURE.md §12.7
#
# Cible : Ubuntu 24.04 (petite instance Scaleway, sans GPU). Idempotent : peut être relancé
# sans risque (met à jour le dépôt, garde le jeton serveur, la clé SSH et les comptes).
#
#   sudo bash deploy/install_server.sh --repo git@github.com:equipe/wmlab.git \
#        --branch main --dir /opt/wmlab --domain lab.example.com \
#        --admin justin --admin-email justin@example.com
#
# Options :
#   --repo URL        dépôt git à cloner (obligatoire au premier lancement)
#   --branch NAME     branche suivie par le serveur (défaut : main)
#   --dir PATH        dossier d'installation = workspace (défaut : /opt/wmlab)
#   --domain FQDN     nom DNS public → Caddy + HTTPS automatique (sans domaine : pas de Caddy,
#                     accès par tunnel SSH seulement)
#   --admin NAME      identifiant d'équipe du premier compte admin (obligatoire ; attribution
#                     des runs, auteur git). Le nom « admin » est réservé au jeton serveur.
#   --admin-email EMAIL
#                     e-mail de connexion de ce compte (obligatoire). Le compte est créé par
#                     « wmlab users add EMAIL --name NAME --role admin » : son mot de passe
#                     temporaire est affiché UNE fois et doit être changé à la 1re connexion.
#   --dry-run         affiche les commandes sans rien exécuter (ne demande pas root)
#   -h, --help        cette aide
#
# Variables : UV_VERSION (défaut 0.12.19, comme scripts/remote_bootstrap.sh),
#             UV_INSTALLER_SHA256 (sha256 attendu de install.sh, facultatif).
# ─────────────────────────────────────────────────────────────────────────────────────────────
set -euo pipefail

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m !\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[1;31m ✗ Erreur :\033[0m %s\n' "$*" >&2; exit 1; }

usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; }

# ── Options ─────────────────────────────────────────────────────────────────────────────────
REPO=""
BRANCH="main"
DIR="/opt/wmlab"
DOMAIN=""
ADMIN=""
ADMIN_EMAIL=""
DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    REPO="${2:-}"; shift 2 ;;
    --branch)  BRANCH="${2:-}"; shift 2 ;;
    --dir)     DIR="${2:-}"; shift 2 ;;
    --domain)  DOMAIN="${2:-}"; shift 2 ;;
    --admin)   ADMIN="${2:-}"; shift 2 ;;
    --admin-email) ADMIN_EMAIL="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "option inconnue : $1 (voir --help)" ;;
  esac
done
[[ -n "$BRANCH" && -n "$DIR" ]] || fail "--branch et --dir ne peuvent pas être vides"
[[ -n "$ADMIN" ]] || fail "--admin NAME est obligatoire (identifiant d'équipe du premier compte admin ; ex. --admin justin)"
[[ "$DIR" == /* ]] || fail "--dir doit être un chemin absolu ($DIR)"
[[ "$ADMIN" =~ ^[a-z0-9][a-z0-9_.-]{0,31}$ ]] \
  || fail "--admin : identifiant en minuscules ([a-z0-9_.-], 32 caractères max), reçu « $ADMIN »"
[[ "$ADMIN" != "admin" ]] \
  || fail "--admin : le nom « admin » est réservé à l'utilisateur implicite du jeton serveur (WMLAB_TOKEN) : choisissez votre prénom ou identifiant"
[[ -n "$ADMIN_EMAIL" ]] \
  || fail "--admin-email EMAIL est obligatoire (e-mail de connexion du premier compte admin ; ex. --admin-email justin@example.com)"
# E-mails stockés en minuscules (ARCHITECTURE.md §12.2) ; validation simple, pas de RFC complète.
ADMIN_EMAIL="$(printf '%s' "$ADMIN_EMAIL" | tr '[:upper:]' '[:lower:]')"
EMAIL_RE='^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$'
if ! [[ "$ADMIN_EMAIL" =~ $EMAIL_RE ]] || (( ${#ADMIN_EMAIL} > 254 )); then
  fail "--admin-email : adresse e-mail invalide « $ADMIN_EMAIL » (attendu : nom@domaine.tld)"
fi
if [[ -n "$DOMAIN" ]] && ! [[ "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  fail "--domain : nom DNS invalide « $DOMAIN »"
fi

SERVICE_USER="wmlab"
SERVICE_HOME="/home/$SERVICE_USER"
ENV_FILE="/etc/wmlab/env"
PORT=8765
UV_VERSION="${UV_VERSION:-0.12.19}"
NODE_MAJOR=22
PUBLIC_URL=""
[[ -n "$DOMAIN" ]] && PUBLIC_URL="https://$DOMAIN"
DEPLOY_DIR="$DIR/deploy"

# ── Exécution / dry-run ─────────────────────────────────────────────────────────────────────
# run      : commande simple (arguments quotés) ; run_sh : ligne shell (pipes, redirections) ;
# write_file PATH MODE OWNER : contenu sur stdin. En dry-run, tout est affiché, rien n'est fait.
run() {
  if (( DRY_RUN )); then printf '[dry-run] $'; printf ' %q' "$@"; printf '\n'
  else "$@"; fi
}
run_sh() {
  if (( DRY_RUN )); then printf '[dry-run] $ %s\n' "$1"
  else bash -o pipefail -c "$1"; fi
}
as_wmlab() { run sudo -u "$SERVICE_USER" -H "$@"; }
write_file() {
  local path="$1" mode="${2:-0644}" owner="${3:-root:root}" content
  content="$(cat)"
  if (( DRY_RUN )); then
    printf '[dry-run] écriture de %s (%s, %s) :\n' "$path" "$mode" "$owner"
    printf '%s\n' "$content" | sed 's/^/    | /'
  else
    [[ -d "$(dirname "$path")" ]] || install -d -m 0755 "$(dirname "$path")"
    printf '%s\n' "$content" > "$path.tmp"
    chmod "$mode" "$path.tmp"; chown "$owner" "$path.tmp"
    mv -f "$path.tmp" "$path"
  fi
}
# have / exists : en dry-run, tout est considéré absent pour afficher chaque étape.
have()   { (( DRY_RUN )) && return 1; command -v "$1" >/dev/null 2>&1; }
exists() { (( DRY_RUN )) && return 1; [[ -e "$1" ]]; }

if (( DRY_RUN )); then
  warn "mode dry-run : les commandes sont affichées, rien n'est exécuté ni écrit"
elif [[ "$(id -u)" -ne 0 ]]; then
  fail "ce script doit être lancé en root : sudo bash deploy/install_server.sh …"
fi

# ── 0. Système ──────────────────────────────────────────────────────────────────────────────
info "Vérification du système"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    warn "système détecté : ${PRETTY_NAME:-inconnu} — ce script est prévu pour Ubuntu 24.04"
  else
    ok "Ubuntu 24.04"
  fi
else
  warn "/etc/os-release absent : système non identifié (normal en dry-run hors Ubuntu)"
fi
echo "  Dépôt      : ${REPO:-<existant dans $DIR>}"
echo "  Branche    : $BRANCH"
echo "  Dossier    : $DIR"
echo "  Domaine    : ${DOMAIN:-<aucun : pas de Caddy>}"
echo "  Admin      : $ADMIN <$ADMIN_EMAIL>"

# ── 1. Paquets ──────────────────────────────────────────────────────────────────────────────
info "Paquets système (git, rsync, curl, openssl, zstd…)"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -qq
run apt-get install -y -qq git rsync curl ca-certificates gnupg openssl zstd sudo openssh-client
ok "paquets installés"

# ── 2. Utilisateur de service et clé SSH ───────────────────────────────────────────────────
info "Utilisateur de service « $SERVICE_USER »"
if (( DRY_RUN )) || ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  run useradd --system --create-home --home-dir "$SERVICE_HOME" --shell /bin/bash "$SERVICE_USER"
fi
# Home RÉEL (utilisateur créé à la main avec un autre home) : il sert aux chemins de la clé SSH
# et au ReadWritePaths de l'unité systemd (marqueur __WMLAB_HOME__).
if ! (( DRY_RUN )); then
  real_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
  if [[ -n "$real_home" && "$real_home" != "$SERVICE_HOME" ]]; then
    warn "home de $SERVICE_USER : $real_home (et non $SERVICE_HOME) : utilisé tel quel"
    SERVICE_HOME="$real_home"
  fi
fi
run install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$SERVICE_HOME/.ssh"
ok "utilisateur $SERVICE_USER (home : $SERVICE_HOME)"

info "Clé SSH ed25519 du serveur (déploiement git + accès aux machines GPU)"
SSH_KEY="$SERVICE_HOME/.ssh/id_ed25519"
if ! exists "$SSH_KEY"; then
  as_wmlab ssh-keygen -q -t ed25519 -N "" -C "wmlab-server@${DOMAIN:-$(hostname)}" -f "$SSH_KEY"
fi
# Première connexion sans invite (clone du dépôt, machines GPU) : l'empreinte est mémorisée
# à la première connexion (accept-new), puis vérifiée.
SSH_CONFIG="$SERVICE_HOME/.ssh/config"
if ! exists "$SSH_CONFIG"; then
  write_file "$SSH_CONFIG" 0600 "$SERVICE_USER:$SERVICE_USER" <<'EOF'
# Watermark Lab — connexions non interactives (dépôt git, machines GPU)
Host *
    StrictHostKeyChecking accept-new
    ServerAliveInterval 30
EOF
elif ! grep -q 'StrictHostKeyChecking' "$SSH_CONFIG"; then
  printf '\nHost *\n    StrictHostKeyChecking accept-new\n    ServerAliveInterval 30\n' >> "$SSH_CONFIG"
fi
ok "clé : $SSH_KEY (publique affichée dans le résumé final)"

# ── 3. uv (gestionnaire Python) ─────────────────────────────────────────────────────────────
# Même installateur officiel épinglé que scripts/remote_bootstrap.sh ; installé dans
# /usr/local/bin pour être disponible à root ET à l'utilisateur wmlab.
info "uv $UV_VERSION"
if have uv; then
  ok "uv déjà installé ($(uv --version))"
else
  UV_INSTALLER="$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX")"
  run curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "$UV_INSTALLER"
  if [[ -n "${UV_INSTALLER_SHA256:-}" ]]; then
    run_sh "echo '${UV_INSTALLER_SHA256}  ${UV_INSTALLER}' | sha256sum -c --quiet -"
    ok "installateur uv vérifié (sha256)"
  else
    warn "UV_INSTALLER_SHA256 non défini : installateur non vérifié (version épinglée seulement)"
  fi
  run env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh "$UV_INSTALLER"
  run rm -f "$UV_INSTALLER"
  ok "uv installé dans /usr/local/bin"
fi

# ── 4. Node.js 22 (NodeSource) ─────────────────────────────────────────────────────────────
info "Node.js $NODE_MAJOR (dépôt NodeSource)"
node_major=0
if have node; then node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"; fi
if [[ "$node_major" -ge "$NODE_MAJOR" ]]; then
  ok "Node.js déjà installé ($(node --version))"
else
  NODE_SETUP="$(mktemp "${TMPDIR:-/tmp}/nodesource_setup.XXXXXX")"
  run curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" -o "$NODE_SETUP"
  run bash "$NODE_SETUP"
  run rm -f "$NODE_SETUP"
  run apt-get install -y -qq nodejs
  ok "Node.js installé"
fi

# ── 5. Dépôt git ────────────────────────────────────────────────────────────────────────────
info "Dépôt git dans $DIR (branche $BRANCH)"
GIT_SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
if [[ -d "$DIR/.git" ]]; then
  ok "dépôt déjà cloné : mise à jour (fetch + checkout + pull --ff-only)"
  if [[ -n "$REPO" ]]; then as_wmlab git -C "$DIR" remote set-url origin "$REPO"; fi
  as_wmlab env GIT_SSH_COMMAND="$GIT_SSH" git -C "$DIR" fetch origin "$BRANCH"
  as_wmlab git -C "$DIR" checkout "$BRANCH"
  as_wmlab env GIT_SSH_COMMAND="$GIT_SSH" git -C "$DIR" pull --ff-only origin "$BRANCH"
else
  [[ -n "$REPO" ]] || fail "$DIR ne contient pas de dépôt : --repo URL est obligatoire au premier lancement"
  run install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DIR"
  if ! (( DRY_RUN )) && ! sudo -u "$SERVICE_USER" -H env GIT_SSH_COMMAND="$GIT_SSH" git ls-remote --exit-code "$REPO" >/dev/null 2>&1; then
    fail "dépôt $REPO inaccessible pour l'utilisateur $SERVICE_USER. Dépôt privé en SSH : déposez la clé \
publique du serveur comme clé de déploiement EN ÉCRITURE sur le dépôt, puis relancez ce script :
$(cat "$SSH_KEY.pub" 2>/dev/null || echo "(clé introuvable : $SSH_KEY.pub)")"
  fi
  as_wmlab env GIT_SSH_COMMAND="$GIT_SSH" git clone --branch "$BRANCH" "$REPO" "$DIR"
fi
# Identité des commits faits par l'app (PUT /api/files → commit + push) et accès SSH au dépôt.
as_wmlab git -C "$DIR" config user.name "Watermark Lab (serveur)"
as_wmlab git -C "$DIR" config user.email "wmlab@${DOMAIN:-$(hostname)}"
as_wmlab git -C "$DIR" config core.sshCommand "$GIT_SSH"
as_wmlab git -C "$DIR" config pull.ff only
ok "dépôt prêt"

# ── 6. Environnement Python ─────────────────────────────────────────────────────────────────
UV_EXTRAS=(--extra server --extra train)
if [[ -f "$DIR/pyproject.toml" ]] && grep -q '"cpu"' "$DIR/pyproject.toml"; then
  UV_EXTRAS+=(--extra cpu)   # torch CPU-only (sources uv conditionnelles) : évite les Go de CUDA
  info "Environnement Python : uv sync ${UV_EXTRAS[*]} (extra cpu détecté dans pyproject.toml)"
else
  info "Environnement Python : uv sync ${UV_EXTRAS[*]}"
  [[ -f "$DIR/pyproject.toml" ]] || warn "pyproject.toml pas encore présent : l'extra cpu sera ajouté au prochain lancement s'il existe"
fi
as_wmlab env PATH="/usr/local/bin:/usr/bin:/bin" UV_PROJECT_ENVIRONMENT="$DIR/.venv" \
  uv --directory "$DIR" sync "${UV_EXTRAS[@]}"
ok "environnement : $DIR/.venv"

# ── 7. Frontend (servi par FastAPI : GET / → desktop/dist) ─────────────────────────────────
info "Frontend : npm ci && npm run build dans desktop/ (binaire Electron non téléchargé)"
as_wmlab env ELECTRON_SKIP_BINARY_DOWNLOAD=1 npm --prefix "$DIR/desktop" ci --no-audit --no-fund
as_wmlab npm --prefix "$DIR/desktop" run build
ok "frontend construit : $DIR/desktop/dist"

# ── 8. Fichier d'environnement du service ──────────────────────────────────────────────────
info "Fichier d'environnement $ENV_FILE"
if exists "$ENV_FILE"; then
  ok "$ENV_FILE existe : conservé (jeton serveur inchangé)"
  grep -q '^WMLAB_WORKSPACE=' "$ENV_FILE" || echo "WMLAB_WORKSPACE=$DIR" >> "$ENV_FILE"
  # Racine des données épinglée par l'environnement : workspace.yaml:data_dir (fichier versionné,
  # tiré de git) ne peut pas déplacer users.yaml ni sessions.json.
  grep -q '^WMLAB_DATA=' "$ENV_FILE" || echo "WMLAB_DATA=$DIR/.wmlab" >> "$ENV_FILE"
  grep -q '^WMLAB_TOKEN=' "$ENV_FILE" || echo "WMLAB_TOKEN=$(openssl rand -hex 24)" >> "$ENV_FILE"
  grep -q 'SCW_SECRET_KEY' "$ENV_FILE" || printf '# SCW_SECRET_KEY=\n' >> "$ENV_FILE"
  # Ancien commentaire (jetons personnels wml_, remplacés par des comptes) : mis à jour sur place.
  if grep -q 'leur jeton personnel$' "$ENV_FILE"; then
    sed -i -e 's|^# Jeton .*leur jeton personnel$|# Jeton « serveur » : admin implicite de secours (ligne de commande, en-tête X-WMLab-Token).\n# Les membres se connectent par e-mail + mot de passe (comptes : wmlab users add EMAIL --name NAME).|' \
      -e 's|^# (wmlab users add NAME)\. Régénérer|# Régénérer|' "$ENV_FILE"
  fi
  chown "root:$SERVICE_USER" "$ENV_FILE"; chmod 0640 "$ENV_FILE"
else
  if (( DRY_RUN )); then SERVER_TOKEN='<openssl rand -hex 24>'; else SERVER_TOKEN="$(openssl rand -hex 24)"; fi
  write_file "$ENV_FILE" 0640 "root:$SERVICE_USER" <<EOF
# Watermark Lab — environnement du service wmlab-server (lu par systemd, EnvironmentFile).
# Ne pas versionner. Lecture : root et le groupe $SERVICE_USER.
WMLAB_WORKSPACE=$DIR
# Racine des données (runs, checkpoints, users.yaml, sessions.json) : fixée ici, jamais par
# workspace.yaml:data_dir (fichier versionné, tiré de git).
WMLAB_DATA=$DIR/.wmlab
# Jeton « serveur » : admin implicite de secours (ligne de commande, en-tête X-WMLab-Token).
# Les membres se connectent par e-mail + mot de passe (comptes : wmlab users add EMAIL --name NAME).
# Régénérer : openssl rand -hex 24, puis systemctl restart wmlab-server.
WMLAB_TOKEN=$SERVER_TOKEN
# Clé secrète Scaleway (IAM) pour éteindre / supprimer les instances GPU depuis l'app.
# Facultatif ; jamais dans hosts.yaml ni dans le dépôt. Décommenter puis redémarrer le service.
# SCW_SECRET_KEY=
# Sauvegardes (deploy/backup.sh) : bucket Object Storage du remote rclone « wmlab-backup ».
# WMLAB_BACKUP_BUCKET=wmlab-backup
EOF
fi
# Données (runs, checkpoints, users.yaml et sessions.json en 0600) : illisibles hors du compte
# de service et de son groupe (même règle que UMask=0027 de l'unité systemd).
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DIR/.wmlab"
ok "environnement du service prêt"

# ── 9. Premier compte admin ─────────────────────────────────────────────────────────────────
# Comptes e-mail + mot de passe (ARCHITECTURE.md §12.2) ; « wmlab » ci-dessous = alias de
# deploy/README.md §4 (sudo -u wmlab -H env WMLAB_WORKSPACE=… .venv/bin/python -m wmlab.cli).
info "Compte admin « $ADMIN » <$ADMIN_EMAIL> (wmlab users add)"
admin_account() {   # « état|e-mail|nom|rôle » du compte portant cet e-mail ou ce nom, sinon rien
  (( DRY_RUN )) && return 0
  sudo -u "$SERVICE_USER" -H env WMLAB_WORKSPACE="$DIR" "$DIR/.venv/bin/python" -m wmlab.cli users list --json 2>/dev/null \
    | "$DIR/.venv/bin/python" -c '
import json, sys
email, name = sys.argv[1], sys.argv[2]
try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
users = data.get("users", []) if isinstance(data, dict) else data
found = [u for u in users if isinstance(u, dict)
         and ((u.get("email") or "").lower() == email or u.get("name") == name)]
found.sort(key=lambda u: (u.get("email") or "").lower() != email)   # e-mail exact en premier
if found:
    u = found[0]
    state = "legacy" if not u.get("email") else (u.get("status") or "active")
    print("|".join([state, (u.get("email") or "").lower(), u.get("name") or "", u.get("role") or ""]))
' "$ADMIN_EMAIL" "$ADMIN" || true
}
ADMIN_CREATED=0
create_admin() {   # échec ⇒ code 1 (le script s'arrête, sauf appel suivi de « || »)
  echo "  Le MOT DE PASSE TEMPORAIRE ci-dessous n'est affiché qu'UNE FOIS : le noter pour la première"
  echo "  connexion de $ADMIN_EMAIL (app ou navigateur), qui exige de le changer (12 caractères minimum)."
  as_wmlab env WMLAB_WORKSPACE="$DIR" "$DIR/.venv/bin/python" -m wmlab.cli users add "$ADMIN_EMAIL" --name "$ADMIN" --role admin \
    || return 1
  ADMIN_CREATED=1
}
ACC_STATE=""; ACC_EMAIL=""; ACC_NAME=""; ACC_ROLE=""
IFS='|' read -r ACC_STATE ACC_EMAIL ACC_NAME ACC_ROLE <<<"$(admin_account)" || true
if [[ -z "$ACC_STATE" ]]; then
  create_admin
elif [[ "$ACC_STATE" == "legacy" ]]; then
  warn "entrée « $ADMIN » de l'ancien format (jeton wml_) dans users.yaml : plus reconnue, création du compte e-mail"
  create_admin || warn "compte non créé : voir le message ci-dessus, puis « wmlab users list »"
elif [[ "$ACC_EMAIL" != "$ADMIN_EMAIL" ]]; then
  warn "le nom « $ADMIN » est déjà pris par le compte $ACC_EMAIL ($ACC_STATE, rôle $ACC_ROLE) : \
$ADMIN_EMAIL non créé — relancez avec cet e-mail, ou choisissez un autre --admin"
else
  case "$ACC_STATE" in
    active)
      ok "compte $ADMIN_EMAIL déjà présent (actif, nom $ACC_NAME, rôle $ACC_ROLE) : mot de passe non \
réaffiché — oublié : « wmlab users reset-password $ADMIN_EMAIL » (nouveau mot de passe temporaire)" ;;
    pending)
      warn "inscription EN ATTENTE pour $ADMIN_EMAIL (créée depuis l'écran de connexion, sans vérification \
de l'e-mail). Si vous n'en êtes pas l'auteur, remplacez d'abord son mot de passe : « wmlab users \
reset-password $ADMIN_EMAIL », puis « wmlab users approve $ADMIN_EMAIL --role admin »" ;;
    disabled)
      warn "compte $ADMIN_EMAIL désactivé : « wmlab users enable $ADMIN_EMAIL » pour le réactiver" ;;
    *)
      warn "compte $ADMIN_EMAIL dans un état inattendu ($ACC_STATE) : « wmlab users list »" ;;
  esac
  if [[ "$ACC_STATE" != "pending" && -n "$ACC_ROLE" && "$ACC_ROLE" != "admin" ]]; then
    warn "rôle actuel de $ADMIN_EMAIL : $ACC_ROLE — pour le passer admin : « wmlab users role $ADMIN_EMAIL admin »"
  fi
fi

# ── 10. Service systemd wmlab-server ────────────────────────────────────────────────────────
info "Service systemd wmlab-server (port $PORT, 127.0.0.1)"
if [[ -f "$DEPLOY_DIR/wmlab-server.service" ]]; then
  sed -e "s|__WMLAB_DIR__|$DIR|g" -e "s|__WMLAB_USER__|$SERVICE_USER|g" -e "s|__WMLAB_HOME__|$SERVICE_HOME|g" \
    -e "s|__WMLAB_PORT__|$PORT|g" "$DEPLOY_DIR/wmlab-server.service" \
    | write_file /etc/systemd/system/wmlab-server.service 0644 root:root
else
  # Même unité que deploy/wmlab-server.service (durcissement compris), sans les commentaires.
  warn "$DEPLOY_DIR/wmlab-server.service introuvable (dépôt pas encore cloné ?) : unité générée"
  write_file /etc/systemd/system/wmlab-server.service 0644 root:root <<EOF
[Unit]
Description=Watermark Lab — backend (mode hébergé)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
Environment=XDG_CACHE_HOME=/var/cache/wmlab
ExecStart=$DIR/.venv/bin/python -m wmlab.cli server --port $PORT --host 127.0.0.1 --workspace $DIR
Restart=always
RestartSec=3
KillMode=process
TimeoutStopSec=30
UMask=0027
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DIR $SERVICE_HOME/.ssh
CacheDirectory=wmlab
CacheDirectoryMode=0750
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
EOF
fi
run systemctl daemon-reload
run systemctl enable wmlab-server
run systemctl restart wmlab-server
ok "wmlab-server activé (journal : journalctl -u wmlab-server -f ; durcissement : systemd-analyze security wmlab-server)"

# ── 11. Caddy (HTTPS automatique) ──────────────────────────────────────────────────────────
if [[ -n "$DOMAIN" ]]; then
  info "Caddy : reverse proxy https://$DOMAIN → 127.0.0.1:$PORT"
  if have caddy; then
    ok "caddy déjà installé ($(caddy version | head -1))"
  else
    # Installation officielle (https://caddyserver.com/docs/install#debian-ubuntu-raspbian)
    run apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
    run_sh "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg"
    run_sh "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list"
    run chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
    run apt-get update -qq
    run apt-get install -y -qq caddy
    ok "caddy installé"
  fi
  if [[ -f "$DEPLOY_DIR/Caddyfile" ]]; then
    sed -e "s|__DOMAIN__|$DOMAIN|g" -e "s|__WMLAB_PORT__|$PORT|g" "$DEPLOY_DIR/Caddyfile" \
      | write_file /etc/caddy/Caddyfile 0644 root:root
  else
    # Repli minimal (en-têtes de sécurité et limite de corps compris, sans journal d'accès).
    write_file /etc/caddy/Caddyfile 0644 root:root <<EOF
$DOMAIN {
    encode zstd gzip
    request_body {
        max_size 8MB
    }
    header {
        defer
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        ?Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
        X-Frame-Options "DENY"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "no-referrer"
        Permissions-Policy "camera=(), microphone=(), geolocation=()"
        -Server
    }
    reverse_proxy 127.0.0.1:$PORT
}
EOF
  fi
  run caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  run systemctl enable caddy
  run systemctl reload-or-restart caddy
  ok "Caddy configuré (le certificat est obtenu au premier accès : DNS $DOMAIN → cette machine, ports 80/443 ouverts)"
else
  warn "pas de --domain : Caddy non installé. Accès via tunnel SSH : ssh -N -L 8765:127.0.0.1:$PORT root@<ip> puis http://127.0.0.1:8765/"
fi

# ── 12. Sauvegardes quotidiennes ───────────────────────────────────────────────────────────
info "Sauvegardes : deploy/backup.sh + minuterie systemd quotidienne"
if [[ -f "$DEPLOY_DIR/backup.sh" ]]; then
  run install -m 0755 -o root -g root "$DEPLOY_DIR/backup.sh" /usr/local/bin/wmlab-backup
else
  warn "$DEPLOY_DIR/backup.sh introuvable : à installer au prochain lancement (après clonage)"
fi
run install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" /var/backups/wmlab
if [[ -f "$DEPLOY_DIR/wmlab-backup.service" && -f "$DEPLOY_DIR/wmlab-backup.timer" ]]; then
  sed -e "s|__WMLAB_USER__|$SERVICE_USER|g" -e "s|__WMLAB_ENV__|$ENV_FILE|g" "$DEPLOY_DIR/wmlab-backup.service" \
    | write_file /etc/systemd/system/wmlab-backup.service 0644 root:root
  write_file /etc/systemd/system/wmlab-backup.timer 0644 root:root < "$DEPLOY_DIR/wmlab-backup.timer"
else
  write_file /etc/systemd/system/wmlab-backup.service 0644 root:root <<EOF
[Unit]
Description=Watermark Lab — sauvegarde quotidienne de .wmlab

[Service]
Type=oneshot
User=$SERVICE_USER
Group=$SERVICE_USER
EnvironmentFile=$ENV_FILE
ExecStart=/usr/local/bin/wmlab-backup
EOF
  write_file /etc/systemd/system/wmlab-backup.timer 0644 root:root <<'EOF'
[Unit]
Description=Watermark Lab — minuterie de sauvegarde (quotidienne, 03:30)

[Timer]
OnCalendar=*-*-* 03:30:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
EOF
fi
run systemctl daemon-reload
run systemctl enable --now wmlab-backup.timer
ok "minuterie wmlab-backup.timer active (rclone → bucket si le remote « wmlab-backup » existe, sinon tar.zst dans /var/backups/wmlab, 14 jours)"

# ── 13. workspace.yaml en mode hébergé ─────────────────────────────────────────────────────
info "workspace.yaml : hosted.enabled=true, git_branch=$BRANCH, public_url=${PUBLIC_URL:-null}"
PATCH_ARGS=(--file "$DIR/workspace.yaml" --branch "$BRANCH")
[[ -n "$PUBLIC_URL" ]] && PATCH_ARGS+=(--public-url "$PUBLIC_URL")
if (( DRY_RUN )); then
  PATCH_ARGS+=(--dry-run)
  printf '[dry-run] $ %s\n' "$DIR/.venv/bin/python $DEPLOY_DIR/patch_workspace.py ${PATCH_ARGS[*]}"
  if [[ -f "$DIR/workspace.yaml" && -f "$DEPLOY_DIR/patch_workspace.py" ]]; then
    PY=python3; [[ -x "$DIR/.venv/bin/python" ]] && PY="$DIR/.venv/bin/python"
    "$PY" "$DEPLOY_DIR/patch_workspace.py" "${PATCH_ARGS[@]}" | sed 's/^/    | /'
  fi
else
  sudo -u "$SERVICE_USER" -H "$DIR/.venv/bin/python" "$DEPLOY_DIR/patch_workspace.py" "${PATCH_ARGS[@]}"
fi
# Le fichier modifié ne doit pas rendre l'arbre « sale » (GitSync ne tire que si
# `git status --porcelain` est vide) : ignoré localement via skip-worktree.
as_wmlab git -C "$DIR" update-index --skip-worktree workspace.yaml
run systemctl restart wmlab-server
ok "mode hébergé activé"

# ── 14. Résumé ─────────────────────────────────────────────────────────────────────────────
info "Résumé"
echo "  Workspace        : $DIR (branche $BRANCH)"
echo "  Service          : systemctl status wmlab-server · journalctl -u wmlab-server -f"
if [[ -n "$DOMAIN" ]]; then
  echo "  URL publique     : $PUBLIC_URL (app de bureau : menu Serveur → « Se connecter à un serveur distant… »)"
else
  echo "  URL              : tunnel SSH → http://127.0.0.1:$PORT/"
fi
echo "  Connexion        : e-mail + mot de passe (app de bureau ou navigateur) ; « Créer un compte » sur"
echo "                     l'écran de connexion → compte en attente → approbation par un admin"
if (( ADMIN_CREATED )); then
  echo "  Premier admin    : $ADMIN_EMAIL (« $ADMIN ») — mot de passe temporaire affiché à l'étape"
  echo "                     « Compte admin » (une seule fois), à changer à la première connexion"
else
  echo "  Premier admin    : $ADMIN_EMAIL (« $ADMIN ») — compte existant, voir l'étape « Compte admin »"
  echo "                     (mot de passe oublié : wmlab users reset-password $ADMIN_EMAIL)"
fi
echo "  Jeton serveur    : $ENV_FILE (WMLAB_TOKEN, secours admin en ligne de commande) — comptes : wmlab users add EMAIL --name NAME"
echo "  Sauvegardes      : systemctl list-timers wmlab-backup.timer · /var/backups/wmlab ou bucket rclone"
echo "  Clé SSH publique du serveur — à déposer sur CHAQUE machine GPU (clés du projet Scaleway)"
echo "                     et comme clé de déploiement (écriture) du dépôt git :"
if (( DRY_RUN )); then
  echo "    <contenu de $SSH_KEY.pub>"
else
  sed 's/^/    /' "$SSH_KEY.pub"
  if ! curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    sleep 3
    curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 \
      || warn "le backend ne répond pas encore sur http://127.0.0.1:$PORT/api/health : journalctl -u wmlab-server -n 50"
  fi
fi
ok "Installation terminée. Guide : $DEPLOY_DIR/README.md"
