---
paths:
  - "wmlab/**"
  - "lab/**"
  - "tests/**"
---
# Moteur Python (`wmlab/`), plugins (`lab/`) et tests

- Contrat : `ARCHITECTURE.md` — §2 tenseurs, §3 registres, §4 interfaces, §5 specs pydantic,
  §6 dossier de run, §7 tracking, §8 moteur, §9 API. Un changement de contrat se fait
  d'abord dans ARCHITECTURE.md, puis dans le code.
- Style : annotations de type, docstrings courtes en anglais, pydantic `ConfigDict(extra="forbid")`,
  erreurs utilisateur en français et explicites (noms acceptés, correction proposée).
- Composant enregistré : le 1er paragraphe du docstring est une phrase en français (affichée dans
  l'app et `wmlab registry`) ; détails techniques en anglais dans un 2e paragraphe.
- Nouveau composant ou spec : skill `wmlab-experiment` (gabarits, invariants) ;
  `wmlab plugin-test <kind> <name>` doit passer.
- Tests : `tests/test_<couche>_<module>.py`, CPU, tenseurs minuscules, fixtures de
  `tests/conftest.py` (`workspace` isolé, registres restaurés après chaque test). La suite
  complète prend environ 1 min 30 : lancer d'abord le fichier concerné, la suite complète avant de
  rendre la main (agent `wmlab-check`, haiku).
- Ne jamais écrire sous `.wmlab/` ni télécharger de poids sans accord (hook de garde actif).
