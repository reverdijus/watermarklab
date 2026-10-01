#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────────────────────
# Watermark Lab — préparation d'un serveur GPU (Ubuntu, ex. Scaleway) — ARCHITECTURE.md §1, §9.1
#
# Exécuté DEPUIS le dossier du code (synchronisé par l'app via rsync : le workdir, ou en mode
# git <workdir>/code/<sha> — §12.3), par l'app (« Machines » → « Préparer ») ou à la main :
#     cd /root/wmlab && bash scripts/remote_bootstrap.sh [DATA_DIR]
#     cd /root/wmlab/code/<sha> && UV_PROJECT_ENVIRONMENT=/root/wmlab/.venv bash scripts/remote_bootstrap.sh [DATA_DIR]
#
# DATA_DIR : racine des données Watermark Lab (runs/, studies/, cache/) ; par défaut
# $WMLAB_DATA, sinon <dossier du code>/.wmlab.
#
# Idempotent : peut être relancé sans risque (n'installe que ce qui manque).
# Variables utiles :
#   WMLAB_ALLOW_NO_GPU=1       continuer sans GPU NVIDIA (serveur CPU de test)
#   UV_PROJECT_ENVIRONMENT=…   emplacement du venv (défaut : <dossier du code>/.venv) ; en mode
#                              git l'app passe <workdir>/.venv, venv PARTAGÉ entre les révisions,
#                              et un marqueur <venv>/.wmlab-lock (sha256 de uv.lock) évite les
#                              « uv sync » inutiles au lancement des jobs.
# ─────────────────────────────────────────────────────────────────────────────────────────────
set -euo pipefail

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m ✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m !\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[1;31m ✗ Erreur :\033[0m %s\n' "$*" >&2; exit 1; }

# Racine du dépôt = dossier parent de scripts/ (fonctionne quel que soit le cwd).
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$WORKDIR"
[[ -f pyproject.toml ]] || fail "pyproject.toml introuvable dans $WORKDIR : le code a-t-il été synchronisé ?"

DATA_DIR="${1:-${WMLAB_DATA:-$WORKDIR/.wmlab}}"
# Venv : UV_PROJECT_ENVIRONMENT (mode git : <workdir>/.venv partagé) ou <code>/.venv.
VENV="${UV_PROJECT_ENVIRONMENT:-$WORKDIR/.venv}"
case "$VENV" in
  /*) ;;
  *) VENV="$WORKDIR/$VENV" ;;
esac
export UV_PROJECT_ENVIRONMENT="$VENV"

SUDO=""
if [[ "$(id -u)" -ne 0 ]] && command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
fi

# Installe des paquets apt manquants (seulement si apt-get est disponible).
apt_install() {
  if ! command -v apt-get >/dev/null 2>&1; then
    fail "commande(s) manquante(s) : $* — installez-les puis relancez ce script"
  fi
  info "Installation de : $*"
  $SUDO apt-get update -qq
  $SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
}

# ── 1. Outils système ────────────────────────────────────────────────────────────────────────
info "Vérification des outils système"
missing=()
for tool in curl rsync; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  apt_install "${missing[@]}"
fi
ok "curl et rsync présents"

# ── 2. GPU NVIDIA ────────────────────────────────────────────────────────────────────────────
info "Vérification du GPU (nvidia-smi)"
HAS_GPU=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  HAS_GPU=1
  ok "GPU détecté : $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | paste -sd ';' -)"
elif [[ "${WMLAB_ALLOW_NO_GPU:-0}" == "1" ]]; then
  warn "nvidia-smi indisponible : on continue sans GPU (WMLAB_ALLOW_NO_GPU=1)"
else
  fail "nvidia-smi indisponible ou en erreur : pilote NVIDIA absent ? Sur Scaleway, utilisez une \
image « GPU OS » (pilotes et CUDA préinstallés). Pour un serveur CPU de test : WMLAB_ALLOW_NO_GPU=1."
fi

# ── 3. uv (gestionnaire Python) ──────────────────────────────────────────────────────────────
# Version épinglée (même version que sur le Mac : `uv --version`) : l'installateur versionné
# télécharge une archive dont il vérifie lui-même la somme de contrôle. Pour verrouiller aussi
# le script d'installation, exportez UV_INSTALLER_SHA256=<sha256 de install.sh> (obtenu une
# fois avec `curl -LsSf https://astral.sh/uv/$UV_VERSION/install.sh | sha256sum`).
UV_VERSION="${UV_VERSION:-0.12.19}"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if command -v uv >/dev/null 2>&1; then
  ok "uv déjà installé ($(uv --version))"
else
  info "Installation de uv $UV_VERSION (installateur officiel astral.sh, version épinglée)"
  UV_INSTALLER="$(mktemp "${TMPDIR:-/tmp}/uv-install.XXXXXX")"
  curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "$UV_INSTALLER" \
    || fail "téléchargement de l'installateur uv ${UV_VERSION} impossible"
  if [[ -n "${UV_INSTALLER_SHA256:-}" ]]; then
    echo "${UV_INSTALLER_SHA256}  ${UV_INSTALLER}" | sha256sum -c --quiet - \
      || fail "somme de contrôle de l'installateur uv différente de UV_INSTALLER_SHA256 : installation refusée"
    ok "installateur uv vérifié (sha256)"
  else
    warn "UV_INSTALLER_SHA256 non défini : installateur non vérifié (version épinglée seulement)"
  fi
  sh "$UV_INSTALLER"
  rm -f "$UV_INSTALLER"
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv installé mais introuvable dans le PATH ($HOME/.local/bin)"
  ok "uv installé ($(uv --version))"
fi

# ── 4. Environnement Python (venv) ───────────────────────────────────────────────────────────
UV_SYNC="uv sync --extra train"
if [[ -f uv.lock ]]; then
  UV_SYNC="uv sync --frozen --extra train"   # exactement les versions du dépôt
fi
info "Synchronisation de l'environnement Python : $UV_SYNC (venv : $VENV)"
$UV_SYNC
[[ -x "$VENV/bin/python" ]] || fail "environnement $VENV absent après uv sync"
if [[ -f uv.lock ]]; then
  # Marqueur lu par l'app au lancement d'un job (§12.3) : pas de nouveau « uv sync » tant que
  # uv.lock de la révision lancée a la même empreinte.
  sha256sum uv.lock | awk '{printf "%s", $1}' > "$VENV/.wmlab-lock"
fi
ok "environnement prêt : $VENV"

# ── 5. Dossier de données ────────────────────────────────────────────────────────────────────
info "Création du dossier de données : $DATA_DIR"
mkdir -p "$DATA_DIR/runs" "$DATA_DIR/studies" "$DATA_DIR/cache"
ok "dossier de données prêt"

# ── 6. Résumé ────────────────────────────────────────────────────────────────────────────────
info "Résumé"
echo "  Hôte            : $(hostname)"
echo "  Code            : $WORKDIR"
echo "  Venv            : $VENV"
echo "  Données         : $DATA_DIR ($(df -h "$DATA_DIR" | awk 'NR==2 {print $4 " libres"}'))"
if [[ "$HAS_GPU" == "1" ]]; then
  nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader \
    | sed 's/^/  GPU             : /'
fi
CUDA_OK="$("$VENV/bin/python" - <<'PY'
import platform
import torch
cuda = torch.cuda.is_available()
print(f"  Python          : {platform.python_version()}")
print(f"  torch           : {torch.__version__} (CUDA {torch.version.cuda or 'aucun'})")
print(f"  cuda disponible : {'oui' if cuda else 'non'}"
      + (f" — {torch.cuda.device_count()} GPU, {torch.cuda.get_device_name(0)}" if cuda else ""))
print("CUDA_OK=" + ("1" if cuda else "0"))
PY
)" || fail "impossible d'importer torch dans .venv (voir l'erreur ci-dessus)"
echo "$CUDA_OK" | grep -v '^CUDA_OK='
"$VENV/bin/python" -m wmlab.cli --version | sed 's/^/  Version         : /'

if [[ "$HAS_GPU" == "1" ]] && ! grep -q '^CUDA_OK=1' <<<"$CUDA_OK"; then
  fail "un GPU est présent mais torch.cuda.is_available() est faux : version de torch/CUDA \
incompatible avec le pilote ? (vérifier « nvidia-smi » et la version CUDA de torch ci-dessus)"
fi
ok "Serveur prêt pour Watermark Lab. Exporter WMLAB_DATA=$DATA_DIR pour les commandes manuelles."
