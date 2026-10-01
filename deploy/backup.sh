#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────
# Watermark Lab — sauvegarde quotidienne des données du serveur central (.wmlab)
# ARCHITECTURE.md §12.7. Installé en /usr/local/bin/wmlab-backup, lancé par wmlab-backup.timer
# sous l'utilisateur wmlab (EnvironmentFile=/etc/wmlab/env).
#
# Deux modes, choisis automatiquement :
#   1. rclone présent ET remote « wmlab-backup: » configuré (sudo -u wmlab rclone config) :
#      copie incrémentale de <data>/ (runs, études, hosts.yaml, users.yaml, jobs — checkpoints
#      .pt COMPRIS) vers  wmlab-backup:$WMLAB_BACKUP_BUCKET/wmlab/ (Object Storage Scaleway).
#      « rclone copy » n'efface jamais rien côté bucket.
#   2. sinon : archive tar.zst de <data>/ dans $WMLAB_BACKUP_DIR (défaut /var/backups/wmlab),
#      SANS les checkpoints .pt (sauf WMLAB_BACKUP_CHECKPOINTS=1), rotation WMLAB_BACKUP_KEEP_DAYS
#      jours (défaut 14). Filet de sécurité sur le même disque : pas une vraie sauvegarde.
#
# Exclus dans les deux modes : index.sqlite (reconstruit par « wmlab index »), cache/, logs/.
# Options : --local (force le mode 2), --dry-run, --workspace DIR, -h/--help.
# Variables : WMLAB_WORKSPACE, WMLAB_DATA, WMLAB_BACKUP_BUCKET (défaut wmlab-backup),
#             WMLAB_BACKUP_DIR, WMLAB_BACKUP_KEEP_DAYS, WMLAB_BACKUP_CHECKPOINTS.
# ─────────────────────────────────────────────────────────────────────────────────────────────
set -euo pipefail

info()  { printf '==> %s\n' "$*"; }
ok()    { printf ' ✓ %s\n' "$*"; }
warn()  { printf ' ! %s\n' "$*" >&2; }
fail()  { printf ' ✗ Erreur : %s\n' "$*" >&2; exit 1; }
usage() { awk 'NR > 1 && !/^#/ { exit } NR > 1 { sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"; }

FORCE_LOCAL=0
DRY_RUN=0
WORKSPACE="${WMLAB_WORKSPACE:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --local)     FORCE_LOCAL=1; shift ;;
    --dry-run)   DRY_RUN=1; shift ;;
    --workspace) WORKSPACE="${2:-}"; shift 2 ;;
    -h|--help)   usage; exit 0 ;;
    *) fail "option inconnue : $1" ;;
  esac
done
[[ -n "$WORKSPACE" ]] || fail "WMLAB_WORKSPACE non défini (ou --workspace DIR)"
DATA="${WMLAB_DATA:-$WORKSPACE/.wmlab}"
[[ -d "$DATA" ]] || fail "dossier de données introuvable : $DATA"

BUCKET="${WMLAB_BACKUP_BUCKET:-wmlab-backup}"
DEST_DIR="${WMLAB_BACKUP_DIR:-/var/backups/wmlab}"
KEEP_DAYS="${WMLAB_BACKUP_KEEP_DAYS:-14}"
REMOTE="wmlab-backup"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
EXCLUDES=(index.sqlite 'index.sqlite-*' cache logs)

run() {
  if (( DRY_RUN )); then printf '[dry-run] $'; printf ' %q' "$@"; printf '\n'
  else "$@"; fi
}

info "Sauvegarde Watermark Lab — $STAMP — données : $DATA"

# ── Mode 1 : rclone → Object Storage ───────────────────────────────────────────────────────
if (( ! FORCE_LOCAL )) && command -v rclone >/dev/null 2>&1 \
   && rclone listremotes 2>/dev/null | grep -qx "$REMOTE:"; then
  info "rclone : copie incrémentale vers $REMOTE:$BUCKET/wmlab/ (checkpoints .pt inclus)"
  args=(copy "$DATA/" "$REMOTE:$BUCKET/wmlab/" --transfers 4 --checkers 8 --stats-one-line --stats 1m -v)
  for e in "${EXCLUDES[@]}"; do args+=(--exclude "/$e/**" --exclude "/$e"); done
  run rclone "${args[@]}"
  run rclone rcat "$REMOTE:$BUCKET/wmlab/.last_backup" <<<"$STAMP" || true
  ok "sauvegarde rclone terminée : $STAMP"
  exit 0
fi

# ── Mode 2 : archive locale tar.zst ────────────────────────────────────────────────────────
if (( ! FORCE_LOCAL )); then
  warn "rclone absent ou remote « $REMOTE » non configuré : archive locale (même disque : ce n'est \
pas une sauvegarde hors site — configurer rclone : sudo -u wmlab rclone config)"
fi
command -v zstd >/dev/null 2>&1 || fail "zstd manquant (apt-get install zstd)"
mkdir -p "$DEST_DIR"
ARCHIVE="$DEST_DIR/wmlab-$STAMP.tar.zst"
tar_args=(--create --zstd --file "$ARCHIVE" --directory "$(dirname "$DATA")")
for e in "${EXCLUDES[@]}"; do tar_args+=(--exclude "$(basename "$DATA")/$e"); done
if [[ "${WMLAB_BACKUP_CHECKPOINTS:-0}" != "1" ]]; then
  tar_args+=(--exclude '*.pt')
  info "archive sans les checkpoints .pt (WMLAB_BACKUP_CHECKPOINTS=1 pour les inclure)"
fi
tar_args+=("$(basename "$DATA")")
run tar "${tar_args[@]}"
if (( ! DRY_RUN )); then
  ok "archive : $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
fi
info "rotation : suppression des archives de plus de $KEEP_DAYS jours dans $DEST_DIR"
run find "$DEST_DIR" -maxdepth 1 -name 'wmlab-*.tar.zst' -type f -mtime "+$KEEP_DAYS" -delete
ok "sauvegarde locale terminée : $STAMP"
