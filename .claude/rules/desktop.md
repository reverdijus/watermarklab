---
paths:
  - "desktop/**"
---
# Desktop (Electron + React 19 + TypeScript + Vite + Tailwind v4)

- Lire `desktop/DESIGN.md` avant toute nouvelle page ou composant : tokens CSS (`bg-canvas`,
  `text-muted`, accent argile réservé aux actions primaires), composants `src/components/ui`,
  patrons de page (§6), conventions de texte (§7). Pas de couleur en dur ; nombres via `lib/format.ts`.
- Contrat des routes et de l'API : `ARCHITECTURE.md` §9 (API HTTP) et §10 (desktop). Les types
  partagés vivent dans `src/lib` ; ne pas redéclarer un type du §9.
- Main et preload : `electron/main.cjs`, `electron/preload.cjs` (CommonJS non compilé). Le jeton
  du backend passe par la variable `WMLAB_TOKEN`, jamais en argument de ligne de commande.
- Avant de rendre la main : `cd desktop && npm run build && npm test`. Vérification visuelle
  sans Python : `npm run mock` puis `npm run render-check -- --base http://127.0.0.1:8781 --token test`.
  Déléguer la mise au vert à l'agent `desktop-build` (sonnet).
- Pas de nouvelle dépendance npm sans accord (licence à vérifier).
- UI en français (libellés, messages, erreurs) ; identifiants et code en anglais.
