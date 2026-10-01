"""Exemple commenté : une attaque image « pixellisation » enregistrée sous `pixelate`.

Ce fichier sert de MODÈLE pour écrire vos propres attaques. Il est découvert automatiquement
(tout module de lab/ est importé au démarrage).

Le contrat (ARCHITECTURE.md §4.2) :

1. hériter de `Attack`, déclarer `modality` et `differentiable` ;
2. `__init__(**params)` : les params décrivent la sévérité MAXIMALE (atteinte à strength=1) ;
3. `forward(x, strength)` renvoie un tenseur de MÊME FORME et de même plage que `x` ;
4. `strength ∈ [0, 1]` INTERPOLE la sévérité : 0 ⇒ identité exacte (renvoyer `x` tel quel),
   1 ⇒ les params. C'est ce qui permet au curriculum de monter progressivement la difficulté
   (`attack_strength: {ramp: [0.0, 1.0]}`), et aux benchmarks d'utiliser strength=1.

Si votre attaque n'est pas différentiable (codec réel, opération numpy…), mettez
`differentiable = False` : le moteur applique alors un straight-through estimator
(`wmlab.attacks.apply_attack`), vous n'avez rien d'autre à faire.

Utilisation dans une phase de curriculum :

    attacks:
      - {name: pixelate, params: {block: 8}, p: 1.0}

ou dans un benchmark :  - {label: pixelate8, name: pixelate, params: {block: 8}}
Vérification :           wmlab plugin-test attack pixelate
"""

from __future__ import annotations

import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.attacks.base import Attack, clamp_strength, lerp
from wmlab.registry import ATTACKS


class PixelateParams(BaseModel):
    """Sévérité à strength=1 (paramètres du YAML)."""

    model_config = ConfigDict(extra="forbid")

    block: int = Field(8, ge=1, le=128, description="Taille des blocs (pixels) à strength=1")


@ATTACKS.register("pixelate", modality="image", params=PixelateParams)
class Pixelate(Attack):
    """Pixellisation : moyenne par blocs de `block`×`block` pixels (exemple de plugin)."""

    modality = "image"
    differentiable = True  # moyenne + agrandissement « nearest » : le gradient passe

    def __init__(self, block: int = 8) -> None:
        super().__init__(block=block)  # garde une trace des params (affichage, repr)
        self.block = block

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        s = clamp_strength(strength)  # borne dans [0, 1] (et protège contre NaN)
        if s == 0.0:
            return x  # identité EXACTE à strength=0 (exigé par le contrat)

        # Interpolation de la sévérité : bloc de 1 pixel (identité) à `block` pixels.
        block = round(lerp(1.0, float(self.block), s))
        if block <= 1:
            return x

        h, w = x.shape[-2:]
        small = (max(1, h // block), max(1, w // block))
        # 1) réduction anti-aliasée ≈ moyenne de chaque bloc (fonctionne aussi sur MPS,
        #    contrairement à F.interpolate(mode="area") pour des tailles non multiples) ;
        low = F.interpolate(x, size=small, mode="bilinear", align_corners=False, antialias=True)
        # 2) retour à la taille d'origine en « nearest » → blocs visibles, FORME CONSERVÉE.
        out = F.interpolate(low, size=(h, w), mode="nearest")
        return out.clamp(0.0, 1.0)  # rester dans la plage de la modalité
