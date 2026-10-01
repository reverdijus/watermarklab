"""Plugins utilisateur Watermark Lab (découverts automatiquement).

Chaque module Python placé dans un sous-dossier (models/, attacks/, datasets/, losses/,
metrics/, preprocess/) est importé au démarrage de l'app et de la CLI ; ses décorateurs
``@MODELS.register(...)``, ``@ATTACKS.register(...)``… rendent le composant utilisable dans les
YAML d'expériences. Exemples commentés : ``models/example_residual_unet.py`` et
``attacks/example_pixelate.py``. Créer un squelette : ``wmlab new plugin KIND NAME --modality M``.
Vérifier : ``wmlab plugin-test KIND NAME``.
"""
