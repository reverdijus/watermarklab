"""Gabarit de dataset IMAGE pour Watermark Lab — à copier dans lab/datasets/<nom>.py.

Contrat (ARCHITECTURE.md §4.3) :
- ``DATASETS.build(name, **params)`` renvoie un ``torch.utils.data.Dataset`` dont chaque item
  est ``{"x": tenseur SANS dim batch, "id": str}`` — image ``[3,H,W]`` float32 dans [0,1]
  (audio ``[1,T]`` dans [-1,1] ; vidéo ``[F,3,H,W]`` dans [0,1]).
- Un chemin commençant par « @ » (ex. ``root: "@coco2017"``) est résolu via le catalogue
  ``datasets/coco2017.yaml`` selon l'hôte courant : ``wmlab.data.resolve_path``. Jamais de
  chemin absolu dans les specs.
- Les prétraitements (``preprocess`` dans le YAML) sont appliqués APRÈS, par
  ``wmlab.data.build_dataset`` ; ``limit`` aussi. Le dataset renvoie donc l'image brute :
  ajoutez ``random_resized_crop`` / ``center_crop`` dans le YAML pour une taille fixe.

Exemple : un dossier d'images avec un découpage train / val DÉTERMINISTE par hachage du
chemin (stable quand on ajoute des fichiers), utile quand la source n'a pas de split officiel.

Utilisation :
    train: {dataset: image_split_folder, params: {root: "@coco2017", split: train},
            preprocess: [{name: random_resized_crop, params: {size: 128}}]}
    val:   {dataset: image_split_folder, params: {root: "@coco2017", split: val},
            preprocess: [{name: center_crop, params: {size: 128}}], limit: 256}
Vérifier (root requis ⇒ tester sur un mini-dossier, voir references/checklist.md) :
    .venv/bin/python -c "from wmlab.plugin_test import run_plugin_test as t; \
print(t('dataset', 'image_split_folder', params={'root': '/tmp/wm_imgs'}))"
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

import numpy as np
import torch
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from torch.utils.data import Dataset

from wmlab.registry import DATASETS

HASH_BUCKETS = 10_000


class ImageSplitFolderParams(BaseModel):
    """Paramètres exposés dans le YAML (data.train.params / data.val.params)."""

    model_config = ConfigDict(extra="forbid")

    root: str = Field(..., description="Dossier d'images ou « @nom » du catalogue datasets/")
    split: Literal["train", "val", "all"] = Field("all", description="Sous-ensemble renvoyé")
    val_fraction: float = Field(0.02, ge=0.0, lt=1.0, description="Part des fichiers en validation")
    extensions: list[str] = Field(default_factory=lambda: [".jpg", ".jpeg", ".png", ".webp"])


def _bucket(relpath: str) -> int:
    """Stable pseudo-random bucket of a relative path (independent of the file list)."""
    return int(hashlib.sha1(relpath.encode("utf-8")).hexdigest()[:8], 16) % HASH_BUCKETS


@DATASETS.register("image_split_folder", modality="image", params=ImageSplitFolderParams)
class ImageSplitFolder(Dataset):
    """Dossier d'images (récursif) avec découpage train/val déterministe par hachage du chemin."""

    def __init__(self, root: str, split: str = "all", val_fraction: float = 0.02,
                 extensions: list[str] | None = None) -> None:
        from wmlab.data import resolve_path

        self.root = Path(resolve_path(root)).expanduser()
        if not self.root.is_dir():
            raise FileNotFoundError(
                f"dossier d'images introuvable : {self.root} (paramètre root={root!r}) — "
                "vérifiez datasets/<nom>.yaml (paths.<hôte> ou paths.default)")
        exts = {e.lower() for e in (extensions or [".jpg", ".jpeg", ".png", ".webp"])}
        threshold = int(val_fraction * HASH_BUCKETS)
        files: list[Path] = []
        for path in sorted(p for p in self.root.rglob("*") if p.suffix.lower() in exts):
            is_val = _bucket(path.relative_to(self.root).as_posix()) < threshold
            if split == "all" or (split == "val") == is_val:
                files.append(path)
        if not files:
            raise FileNotFoundError(
                f"aucune image ({', '.join(sorted(exts))}) pour split={split!r} dans {self.root}")
        self.files = files

    def __len__(self) -> int:
        return len(self.files)

    def __getitem__(self, index: int) -> dict:
        path = self.files[index]
        with Image.open(path) as img:
            array = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
        x = torch.from_numpy(array).permute(2, 0, 1).contiguous()     # [3, H, W] in [0, 1]
        return {"x": x, "id": path.relative_to(self.root).as_posix()}
