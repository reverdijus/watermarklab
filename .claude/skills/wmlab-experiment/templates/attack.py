"""Gabarits d'attaques IMAGE pour Watermark Lab — à copier dans lab/attacks/<nom>.py.

Contrat (ARCHITECTURE.md §4.2) :
- ``__init__(**params)`` : les params décrivent la sévérité à strength = 1.
- ``forward(x, strength=1.0)`` : strength ∈ [0, 1] interpole la sévérité ;
  **strength = 0 ⇒ identité exacte** (renvoyer x) ; **même forme** en sortie ; rester dans la
  plage de la modalité (image [0, 1], audio [-1, 1]).
- ``differentiable = False`` pour une opération non différentiable (codec réel, PIL, PyAV,
  numpy) : le trainer applique alors automatiquement un straight-through estimator.

Deux exemples ci-dessous (garder celui qui sert, un nom de registre par attaque) :
- ``gamma`` : différentiable (correction gamma), utilisable dans les rampes ;
- ``webp``  : codec réel via PIL, non différentiable (fin de curriculum, validation,
  benchmark).

NOMS DÉJÀ PRIS : chaque nom de registre est unique dans tout le workspace (builtins +
lab/). Les exemples livrés dans lab/ occupent déjà ``pixelate`` (lab/attacks/example_pixelate.py)
et ``example_unet`` (lab/models/example_residual_unet.py) ; un builtin (``jpeg``, ``crop``…)
ne se surcharge qu'avec ``override=True`` (à éviter). Vérifier avant de copier :
``wmlab registry --kind attack``. Un doublon fait échouer l'import de TOUT le module (les
autres attaques du fichier disparaissent aussi) : l'erreur apparaît dans ``wmlab registry``
et ``wmlab validate`` (« import du plugin impossible »).

Utilisation :  attacks: [{name: gamma, params: {gamma: 2.0}}]
               val_attacks: [{label: webp50, name: webp, params: {quality: 50}}]
Vérifier : .venv/bin/python -m wmlab.cli plugin-test attack gamma
           .venv/bin/python -m wmlab.cli plugin-test attack webp
"""

from __future__ import annotations

import io

import numpy as np
import torch
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from torch import Tensor

from wmlab.attacks.base import Attack
from wmlab.registry import ATTACKS


# -- differentiable ------------------------------------------------------------------------
class GammaParams(BaseModel):
    """Sévérité à strength = 1."""

    model_config = ConfigDict(extra="forbid")

    gamma: float = Field(2.0, gt=0.0, le=8.0,
                         description="Exposant de la correction gamma à strength = 1 (1 = identité)")


@ATTACKS.register("gamma", modality="image", params=GammaParams)
class Gamma(Attack):
    """Correction gamma : x ** g, g interpolé de 1 (identité) à `gamma` (différentiable)."""

    modality = "image"
    differentiable = True

    def __init__(self, gamma: float = 2.0) -> None:
        super().__init__(gamma=gamma)
        self.gamma = float(gamma)

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        if strength <= 0:
            return x                                   # exact identity required by the contract
        # Interpolated severity: exponent 1 at strength 0 → `gamma` at strength 1.
        g = 1.0 + float(strength) * (self.gamma - 1.0)
        # Small floor keeps the gradient of x ** g finite at x = 0 when g < 1.
        return x.clamp(min=1e-6).pow(g).clamp(0.0, 1.0)


# -- non differentiable (real codec) -------------------------------------------------------
class WebpParams(BaseModel):
    """Sévérité à strength = 1."""

    model_config = ConfigDict(extra="forbid")

    quality: int = Field(50, ge=1, le=100, description="Qualité WebP à strength = 1")


@ATTACKS.register("webp", modality="image", params=WebpParams)
class Webp(Attack):
    """Compression WebP réelle via Pillow (non différentiable : straight-through automatique)."""

    modality = "image"
    differentiable = False

    def __init__(self, quality: int = 50) -> None:
        super().__init__(quality=quality)
        self.quality = quality

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        if strength <= 0:
            return x                                   # exact identity (no q=100 re-encoding)
        q = int(round(100 - float(strength) * (100 - self.quality)))
        images = (x.detach().float().clamp(0, 1) * 255).round().to(torch.uint8).cpu()
        out = torch.empty(images.shape, dtype=torch.float32)
        for i, img in enumerate(images):               # img [3, H, W] uint8
            buffer = io.BytesIO()
            Image.fromarray(img.permute(1, 2, 0).numpy()).save(buffer, format="WEBP", quality=q)
            buffer.seek(0)
            decoded = np.asarray(Image.open(buffer).convert("RGB"), dtype=np.float32) / 255.0
            out[i] = torch.from_numpy(decoded).permute(2, 0, 1)
        return out.to(device=x.device, dtype=x.dtype)
