# Code tiers adapté dans `lab/models/`

Les baselines pré-entraînées (`baseline_*.py`, `_meta_modules.py`) réécrivent des
architectures publiées sous licence MIT pour pouvoir charger les **poids officiels** sans
installer les paquets d'origine (dépendances lourdes ou non installables sur Mac). Les
noms de modules suivent ceux du code d'origine (clés des `state_dict`).

Les poids eux-mêmes ne sont pas dans le dépôt : `wmlab weights list` indique leur source et
leur licence (`.wmlab/weights/`, un dossier par fichier).

| Fichier | Code d'origine | Licence du code | Licence des poids utilisés |
|---|---|---|---|
| `_meta_modules.py`, `baseline_videoseal.py` | [facebookresearch/videoseal](https://github.com/facebookresearch/videoseal) (Video Seal, Pixel Seal) | MIT — Copyright (c) Meta Platforms, Inc. and affiliates | MIT |
| `_meta_modules.py`, `baseline_wam.py` | [facebookresearch/watermark-anything](https://github.com/facebookresearch/watermark-anything) | MIT — Copyright (c) Facebook, Inc. and its affiliates | MIT (`wam_mit.pth` ; `wam_coco.pth`, CC-BY-NC, n'est pas utilisé) |
| `baseline_trustmark.py` | [adobe/trustmark](https://github.com/adobe/trustmark) | MIT — Copyright 2023 Adobe | MIT |
| `baseline_invismark.py` | [microsoft/InvisMark](https://github.com/microsoft/InvisMark) | MIT — Copyright (c) Microsoft Corporation | non précisée (miroir Hugging Face non officiel `shelock/watermark-invismark-model`) |
| `baseline_ssl.py` | méthode de [facebookresearch/ssl_watermarking](https://github.com/facebookresearch/ssl_watermarking) (code réécrit, rien copié) | CC-BY-NC 4.0 | **CC-BY-NC 4.0 : usage non commercial** |

## Texte de la licence MIT

S'applique aux parties adaptées de videoseal, watermark-anything, trustmark et InvisMark,
avec les mentions de copyright du tableau ci-dessus.

```
MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
