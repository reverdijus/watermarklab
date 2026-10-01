---
paths:
  - "deploy/**"
---
# Déploiement (serveur central, mode équipe)

- Référence : `deploy/README.md` (§3 installation, §4 comptes, §7 machines GPU, §8 sauvegardes,
  §9 mise à jour, §11 sécurité) et `ARCHITECTURE.md` §12 (§12.2 comptes, §12.7 déploiement).
- `install_server.sh` reste idempotent et supporte `--dry-run` (`--admin NAME --admin-email
  EMAIL` obligatoires). Aucun secret dans le dépôt : comptes avec mots de passe hachés (scrypt)
  dans `.wmlab/users.yaml` et sessions (empreintes des jetons) dans `.wmlab/sessions.json`,
  tous deux en 0600 ; jeton serveur et `SCW_SECRET_KEY` uniquement dans `/etc/wmlab/env`,
  jamais dans `hosts.yaml`.
- Garder le durcissement de `wmlab-server.service` (`ProtectSystem=strict` + `ReadWritePaths`,
  etc.) et les en-têtes / le journal filtré du `Caddyfile` ; l'unité et le Caddyfile de repli de
  `install_server.sh` doivent rester alignés sur ces fichiers.
- Le backend n'écoute que `127.0.0.1:8765` ; seul Caddy (80 / 443) est exposé.
- Avant de rendre la main : `bash deploy/tests_dry_run.sh` (sans root).
- Le serveur ne relit `wmlab/` et `lab/` qu'au redémarrage (`deploy/README.md` §9) ; les machines
  GPU exécutent toujours le commit choisi dans l'app.
