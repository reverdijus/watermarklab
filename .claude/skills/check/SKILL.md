---
name: check
description: Lance toutes les vérifications du dépôt Watermark Lab (wmlab validate, pytest, build + tests desktop, dry-run deploy) via l'agent wmlab-check (haiku) et rapporte les échecs avec leur sortie brute. À utiliser avant un commit ou avant de rendre la main. Argument optionnel - python | desktop | deploy | all.
context: fork
agent: wmlab-check
---
Lance les vérifications demandées — périmètre : `$ARGUMENTS` (vide = `all`) — depuis la racine du dépôt, exactement comme décrit dans ta définition d'agent, et rends le rapport au format prévu : une ligne OK / ÉCHEC par vérification, la sortie brute de chaque échec dans un bloc de code, puis « Tout est vert » ou « N vérification(s) en échec ».
