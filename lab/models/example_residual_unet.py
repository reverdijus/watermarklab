"""Exemple commenté : une architecture image « U-Net résiduel » enregistrée sous `example_unet`.

Ce fichier sert de MODÈLE pour écrire vos propres architectures. Il est découvert
automatiquement (tout module de lab/ est importé au démarrage) : rien d'autre à configurer.

Le contrat à respecter (ARCHITECTURE.md §4.1) tient en quatre points :

1. hériter de `WatermarkModel` et déclarer `modality = "image"` (ou "audio" / "video") ;
2. `__init__(self, nbits, ...)` : `nbits` (longueur du message) est TOUJOURS fourni par le
   moteur depuis project.yaml → il ne doit jamais apparaître dans les params du YAML ;
3. `embed(x, msg)` renvoie un tenseur de MÊME FORME et de MÊME PLAGE que `x`
   (image : [B, 3, H, W] dans [0, 1]) — pensez au `clamp` final ;
4. `extract(x)` renvoie des logits [B, nbits] (logit > 0 ⇒ bit 1), quelle que soit la
   résolution de `x` : terminez par un pooling global (moyenne) plutôt qu'un `Flatten`.

Utilisation dans une expérience (projects/<projet>/experiments/<exp>.yaml) :

    model:
      name: example_unet
      params: {channels: 32, depth: 2, strength: 0.5}

Vérification (formes, plages, gradients) :  wmlab plugin-test model example_unet
Puis un entraînement éclair :              wmlab train <exp.yaml> --smoke
"""

from __future__ import annotations

import torch
import torch.nn.functional as F
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor, nn

from wmlab.models.base import WatermarkModel
from wmlab.registry import MODELS


# -----------------------------------------------------------------------------------------
# 1) Les hyperparamètres exposés dans le YAML (model.params).
#    Un modèle pydantic sert à trois choses : valider le YAML (fautes de frappe, bornes),
#    générer le formulaire de l'app (description, valeurs par défaut) et documenter le modèle.
#    `extra="forbid"` refuse les paramètres inconnus (ex. « chanels » au lieu de « channels »).
# -----------------------------------------------------------------------------------------
class ExampleUNetParams(BaseModel):
    """Hyperparamètres de `example_unet` (ne jamais y mettre nbits)."""

    model_config = ConfigDict(extra="forbid")

    channels: int = Field(32, ge=8, le=256, description="Canaux du premier niveau du U-Net")
    depth: int = Field(2, ge=1, le=4, description="Nombre de sous-échantillonnages (×2)")
    strength: float = Field(1.0, gt=0.0, le=10.0,
                            description="Amplitude maximale du résidu (tanh × strength)")


def conv_block(c_in: int, c_out: int) -> nn.Sequential:
    """Deux convolutions 3×3 + GroupNorm + GELU (bloc de base du U-Net)."""
    groups = min(8, c_out)  # GroupNorm : indépendant de la taille du batch (≠ BatchNorm)
    return nn.Sequential(
        nn.Conv2d(c_in, c_out, 3, padding=1), nn.GroupNorm(groups, c_out), nn.GELU(),
        nn.Conv2d(c_out, c_out, 3, padding=1), nn.GroupNorm(groups, c_out), nn.GELU(),
    )


# -----------------------------------------------------------------------------------------
# 2) L'enregistrement : le nom ("example_unet") est celui utilisé dans le YAML.
#    `modality` filtre les listes de l'app ; `params` branche la validation ci-dessus.
#    La description affichée dans la bibliothèque = 1er paragraphe de la docstring.
# -----------------------------------------------------------------------------------------
@MODELS.register("example_unet", modality="image", params=ExampleUNetParams)
class ExampleResidualUNet(WatermarkModel):
    """U-Net résiduel (exemple de plugin) : message injecté au goulot, extracteur CNN global."""

    modality = "image"

    def __init__(self, nbits: int, channels: int = 32, depth: int = 2,
                 strength: float = 1.0) -> None:
        super().__init__(nbits)  # stocke self.nbits
        self.strength = strength
        self.depth = depth

        # --- embedder : encodeur (descente) ---------------------------------------------
        widths = [channels * 2 ** i for i in range(depth + 1)]  # ex. [32, 64, 128]
        self.stem = conv_block(3, widths[0])
        self.down = nn.ModuleList(
            nn.Sequential(nn.Conv2d(widths[i], widths[i + 1], 4, stride=2, padding=1),
                          conv_block(widths[i + 1], widths[i + 1]))
            for i in range(depth)
        )
        # --- message : un vecteur appris par bit, ajouté aux features du goulot ----------
        # msg ∈ {0,1}^nbits → {-1,+1}^nbits → projection linéaire vers les canaux du goulot.
        self.msg_proj = nn.Linear(nbits, widths[-1])
        # --- embedder : décodeur (remontée) avec connexions « skip » ---------------------
        self.up = nn.ModuleList(
            nn.ConvTranspose2d(widths[i + 1], widths[i], 2, stride=2)
            for i in reversed(range(depth))
        )
        self.merge = nn.ModuleList(conv_block(2 * widths[i], widths[i])
                                   for i in reversed(range(depth)))
        self.to_residual = nn.Conv2d(widths[0], 3, 1)

        # --- extracteur : CNN strié + pooling global (indépendant de la résolution) ------
        self.extractor = nn.Sequential(
            conv_block(3, channels),
            nn.Conv2d(channels, 2 * channels, 3, stride=2, padding=1), nn.GELU(),
            nn.Conv2d(2 * channels, 2 * channels, 3, stride=2, padding=1), nn.GELU(),
            nn.Conv2d(2 * channels, nbits, 1),
            nn.AdaptiveAvgPool2d(1),  # [B, nbits, 1, 1] quelle que soit la taille d'entrée
            nn.Flatten(),             # [B, nbits]
        )

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        """x [B,3,H,W] dans [0,1], msg [B,nbits] → x_w de même forme, dans [0,1]."""
        h, w = x.shape[-2:]
        # Le U-Net divise la résolution par 2**depth : on complète (padding) l'image pour que
        # ses dimensions soient des multiples, puis on recadre à la fin → n'importe quelle
        # taille d'entrée est acceptée (y compris impaire).
        multiple = 2 ** self.depth
        pad_h, pad_w = (-h) % multiple, (-w) % multiple
        feats = F.pad(x * 2.0 - 1.0, (0, pad_w, 0, pad_h), mode="replicate")

        skips = []
        feats = self.stem(feats)
        for down in self.down:
            skips.append(feats)
            feats = down(feats)
        signed = msg.to(x.dtype) * 2.0 - 1.0                     # bits → ±1
        feats = feats + self.msg_proj(signed)[:, :, None, None]  # diffusé sur toute l'image
        for up, merge in zip(self.up, self.merge):
            feats = up(feats)
            feats = merge(torch.cat([feats, skips.pop()], dim=1))

        # Résidu BORNÉ : tanh ∈ [-1, 1] × strength → contrôle direct de l'imperceptibilité.
        residual = torch.tanh(self.to_residual(feats))[..., :h, :w]
        # Toujours revenir dans la plage de la modalité (image : [0, 1]).
        return (x + self.strength * residual).clamp(0.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        """x [B,3,H,W] (éventuellement attaquée) → logits [B,nbits] (> 0 ⇒ bit 1)."""
        return self.extractor(x * 2.0 - 1.0)

    # Optionnel : pertes propres au modèle, ajoutées par le moteur (clé → tenseur scalaire).
    # Exemple : pénaliser l'énergie du résidu. Par défaut WatermarkModel renvoie {}.
    # def aux_losses(self, batch) -> dict[str, Tensor]:
    #     return {"residual_energy": (batch.x_w - batch.x).pow(2).mean()}
