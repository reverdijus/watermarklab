# Interfaces des composants (ARCHITECTURE.md §2–§4)

Tout composant est enregistré dans un registre par décorateur et vit, côté utilisateur, dans
`lab/<kind>s/<nom>.py` (découvert automatiquement : `lab/__init__.py` et
`lab/<kind>s/__init__.py` doivent exister — `wmlab new plugin` les crée).

## Sommaire

1. Registres et conventions communes
2. Tenseurs (§2)
3. Modèle (`MODELS`) — image, audio, vidéo
4. Attaque (`ATTACKS`)
5. Dataset (`DATASETS`) et catalogue
6. Prétraitement (`PREPROCESS`)
7. Perte (`LOSSES`) et `StepBatch`
8. Métrique (`METRICS`) et robustesse
9. Composants builtin (noms exacts)

---

## 1. Registres et conventions communes

```python
from wmlab.registry import MODELS, ATTACKS, DATASETS, PREPROCESS, LOSSES, METRICS

@ATTACKS.register("my_attack", modality="image", params=MyAttackParams, description=None)
class MyAttack(Attack): ...
```

- `modality` : `"image"`, `"audio"`, `"video"`, une liste (`["image", "video"]`) ou `None`
  (toutes). Un composant multi-modal est UN seul nom qui dispatche en interne (sur `x.ndim`).
- `params` : classe pydantic (recommandé, avec `model_config = ConfigDict(extra="forbid")`) —
  valide les params du YAML, alimente le formulaire / la bibliothèque de l'app (JSON Schema).
  Sans `params`, le schéma est dérivé de la signature de `__init__` / de la fonction.
- `description` : par défaut le 1er paragraphe du docstring (affiché dans l'app → en français).
- Nom déjà pris ⇒ `ValueError` (sauf ré-import du même objet, ou `override=True` pour
  remplacer volontairement un builtin) et **tout le module fautif est ignoré** (ses autres
  composants disparaissent aussi) ; visible dans `wmlab registry` et `wmlab validate` sans
  argument. Déjà pris par les exemples de `lab/` : `pixelate`, `example_unet`.
- Construction : `REGISTRY.build(name, **params)` valide `params` puis appelle `obj(**params)`.
- Introspection : `wmlab registry [--kind K] [--json]`.

## 2. Tenseurs

| Modalité | Batch | Plage | Contexte |
|---|---|---|---|
| image | `[B, 3, H, W]` float32 | `[0, 1]` | — |
| audio | `[B, 1, T]` float32 mono | `[-1, 1]` | `sample_rate` (16 000 par défaut) |
| vidéo | `[B, F, 3, H, W]` float32 | `[0, 1]` | `fps` (24 par défaut) |

- Message `msg` : `[B, nbits]` float32 dans `{0., 1.}` ; logits `[B, nbits]`, `> 0` ⇒ bit 1.
- `nbits` = `project.yaml: message_bits`.
- Item de dataset : `{"x": tenseur SANS dim batch, "id": str}`.

## 3. Modèle — `wmlab.models.base.WatermarkModel(nn.Module)`

```python
class WatermarkModel(nn.Module):
    modality: ClassVar[str] = "image"
    def __init__(self, nbits: int): super().__init__(); self.nbits = nbits
    def embed(self, x: Tensor, msg: Tensor) -> Tensor      # même forme/plage que x (clampé)
    def extract(self, x: Tensor) -> Tensor                 # logits [B, nbits]
    def forward(self, x, msg) -> Tensor                    # = embed (hérité)
    def aux_losses(self, batch: StepBatch) -> dict[str, Tensor]   # défaut {} ; ajoutées à la perte
    def param_groups(self) -> dict[str, list[nn.Parameter]]        # défaut {"all": params}
```

- Construit par le trainer : `MODELS.build(spec.model.name, nbits=project.message_bits,
  **spec.model.params)`. **`nbits` jamais dans `model.params`.**
- Les noms des sous-modules sont les préfixes utilisables dans `phases[].freeze`
  (ex. `freeze: ["embedder."]` gèle l'embedder pendant une phase).
- Recommandations : résidu borné (`strength * tanh(...)`), dernière couche de l'embedder
  initialisée près de zéro (PSNR élevé au départ), extracteur à **pooling global** (toutes
  résolutions / durées), BatchNorm ou GroupNorm, pas de sigmoïde sur les logits.
- Recharger un checkpoint : `wmlab.models.load_model_from_checkpoint(path, device="cpu")`.

Exemple image complet : `templates/model.py` (U-Net + extracteur conv).

### Baselines pré-entraînées — `wmlab.models.pretrained.PretrainedWatermarkModel`

Pour brancher un modèle **publié avec ses poids** (plutôt que l'entraîner) :

```python
register_weights(WeightsFile(name="my_model_v1", url="https://…/ckpt.pth", filename="ckpt.pth",
                             size_mb=120, license="MIT", source="https://github.com/…",
                             sha256=None, commercial_use=True))

@MODELS.register("my_model", modality="image", params=MyParams)   # MyParams : + download: bool
class MyModel(PretrainedWatermarkModel):
    """Mon modèle (Org, 2025), poids officiels : …, baseline figée (évaluation seule, phases: [])."""
    native_nbits = 64                  # longueur du message des poids officiels
    trainable = False                  # True seulement si on veut le fine-tuner

    def __init__(self, nbits: int, download: bool = False) -> None:
        super().__init__(nbits)        # nbits du projet ≤ native_nbits (bourrage fixe au-delà)
        self.embedder, self.detector = …   # noms = clés du state_dict officiel
        state = load_official_checkpoint(self.weights_file("my_model_v1", download=download))
        self.load_official({k: v for k, v in state.items() if k.startswith(("embedder.", "detector."))})
        self.eval()

    def embed(self, x, msg):  return … self.to_native(msg) …        # [B, native_nbits]
    def extract(self, x):     return self.from_native(logits_native)  # [B, nbits]
```

- Poids hors dépôt (`.wmlab/weights/<name>/`) : `wmlab weights list` / `wmlab weights pull`
  (**jamais sans l'accord de l'utilisateur** ; vérifier la licence, noter `commercial_use`).
- Chargement sûr uniquement (`load_official_checkpoint` = `weights_only`, liste blanche de
  classes si le fichier contient des objets picklés) ; `load_official` est strict.
- Réimplémenter l'architecture d'après le code d'origine (licence permissive, attribution dans
  `lab/models/THIRD_PARTY_NOTICES.md`) plutôt que dépendre d'un paquet lourd ; valider en
  chargeant les vrais poids : bit_acc ≈ 1 sur images tatouées, ≈ 0,5 sur images propres.
- Une baseline figée s'évalue avec `phases: []` (évaluation seule) ; `plugin-test` saute ses
  gradients. Exemples : `lab/models/baseline_*.py` (`videoseal`, `pixelseal` entraînable,
  `wam`, `trustmark`, `invismark` avec `fixed_bits`, `ssl_watermark`).

### Audio — `[B, 1, T]` dans `[-1, 1]`

```python
class AudioResParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: int = Field(32, ge=4)
    strength: float = Field(1.0, gt=0)

@MODELS.register("audio_res", modality="audio", params=AudioResParams)
class AudioRes(WatermarkModel):
    """Générateur résiduel 1D + détecteur conv 1D à pooling global."""
    modality = "audio"

    def __init__(self, nbits: int, channels: int = 32, strength: float = 1.0) -> None:
        super().__init__(nbits)
        self.strength = strength
        self.msg_proj = nn.Linear(nbits, channels)
        self.enc_in = nn.Conv1d(1, channels, 7, padding=3)
        self.enc_out = nn.Conv1d(channels, 1, 7, padding=3)
        nn.init.normal_(self.enc_out.weight, std=1e-3); nn.init.zeros_(self.enc_out.bias)
        self.dec = nn.Sequential(
            nn.Conv1d(1, channels, 7, stride=2, padding=3), nn.GELU(),
            nn.Conv1d(channels, channels, 7, stride=2, padding=3), nn.GELU(),
        )
        self.head = nn.Linear(channels, nbits)

    def embed(self, x: Tensor, msg: Tensor) -> Tensor:
        h = torch.relu(self.enc_in(x)) + self.msg_proj(msg * 2 - 1)[:, :, None]
        return (x + self.strength * 0.1 * torch.tanh(self.enc_out(h))).clamp(-1.0, 1.0)

    def extract(self, x: Tensor) -> Tensor:
        return self.head(self.dec(x).mean(dim=-1))       # pooling temporel global
```

Pour la localisation (AudioSeal), l'extracteur peut aussi produire une détection par
échantillon ; seule la sortie `[B, nbits]` est utilisée par le trainer et les benchmarks.

### Vidéo — `[B, F, 3, H, W]` dans `[0, 1]`

Réutiliser un modèle image frame par frame (c'est ce que fait le builtin
`video_framewise`) :

```python
def embed(self, x: Tensor, msg: Tensor) -> Tensor:
    b, f = x.shape[:2]
    frames = x.flatten(0, 1)                               # [B*F, 3, H, W]
    msg_f = msg.repeat_interleave(f, dim=0)                # même message sur chaque frame
    return self.image.embed(frames, msg_f).unflatten(0, (b, f))

def extract(self, x: Tensor) -> Tensor:
    b, f = x.shape[:2]
    return self.image.extract(x.flatten(0, 1)).unflatten(0, (b, f)).mean(dim=1)   # agrégation
```

Un modèle réellement spatio-temporel (Conv3d, attention temporelle) doit garder les mêmes
entrées / sorties.

## 4. Attaque — `wmlab.attacks.base.Attack(nn.Module)`

```python
class Attack(nn.Module):
    modality: ClassVar[str]
    differentiable: ClassVar[bool] = True
    def __init__(self, **params)                                   # sévérité à strength = 1
    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor  # 0 ⇒ identité, même forme
```

Règles :
- **Interpolation de la sévérité** par `strength` : ex. JPEG `quality=50` ⇒
  `q = 100 − s·(100 − 50)` ; flou `sigma = s·2.0` ; crop `area = 1 − s·(1 − 0.5)`.
- **`strength <= 0` ⇒ `return x`** (identité exacte ; ne pas ré-encoder à q=100).
- **Forme conservée** : rogner / redimensionner puis revenir à la taille d'origine ; plage de
  la modalité respectée (clamp).
- Aléa (position du crop…) : `torch.rand` / `torch.randint` (générateur global ensemencé).
- **Non différentiable** (PIL, PyAV, numpy, `round`) ⇒ `differentiable = False` ; le trainer
  applique `wmlab.attacks.apply_attack(attack, x, strength)` =
  `x + (attack(x.detach(), strength) - x).detach()` (straight-through). Travailler sur
  `x.detach().cpu()` et renvoyer sur `x.device` / `x.dtype`.
- Contexte : `sample_rate` (audio) et `fps` (vidéo) — valeurs par défaut 16 000 et 24 ; en
  faire des params si l'attaque en dépend.
- Entraînement : force effective `U(0, s_phase)` si `random_strength: true` (défaut) ;
  benchmarks : strength = 1.

Exemples complets : `templates/attack.py` (`gamma` différentiable, `webp` codec réel) et
`lab/attacks/example_pixelate.py` (`pixelate`, livré et déjà enregistré : ne pas redéclarer ce
nom ; copier sous un autre nom pour le modifier).

Exemple audio différentiable :

```python
class ClipParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    threshold: float = Field(0.5, gt=0, le=1)          # seuil d'écrêtage à strength = 1

@ATTACKS.register("hard_clip", modality="audio", params=ClipParams)
class HardClip(Attack):
    """Écrêtage : sature le signal au-delà d'un seuil."""
    modality = "audio"
    differentiable = True

    def __init__(self, threshold: float = 0.5) -> None:
        super().__init__(threshold=threshold)
        self.threshold = threshold

    def forward(self, x: Tensor, strength: float = 1.0) -> Tensor:
        if strength <= 0:
            return x
        t = 1.0 - float(strength) * (1.0 - self.threshold)   # 1.0 (identité) → threshold
        return x.clamp(-t, t)
```

Une attaque vidéo « frame par frame » n'a pas besoin d'être codée : utiliser le builtin
`framewise` (`{name: framewise, params: {attack: <attaque image>, params: {...}}}`).

## 5. Dataset — `DATASETS`

```python
DATASETS.build(name, **params) -> torch.utils.data.Dataset   # items {"x", "id"}
wmlab.data.resolve_path("@coco2017") -> str                   # chemin selon WMLAB_HOST
wmlab.data.build_dataset(split_spec, modality) -> Dataset     # applique preprocess + limit
wmlab.data.dataset_version(split_spec) -> str                 # 12 hex, écrit dans run.json
```

- Paramètre de chemin commençant par `@` ⇒ résolu via `datasets/<nom>.yaml` :
  `paths[WMLAB_HOST]`, sinon `paths["default"]` (`~` étendu). Appeler `resolve_path` dans
  `__init__` (les specs ne contiennent jamais de chemin absolu).
- Ordre déterministe (fichiers triés), `id` stable (chemin relatif), erreurs claires en
  français si le dossier est vide ou absent.
- Le dataset renvoie la donnée brute ; taille fixe via les prétraitements du YAML.
- Paramètre `modality` : seulement pour un dataset multi-modal (comme `synthetic`) ; il doit
  alors valoir la modalité du projet.
- Nouveau dataset réel ⇒ entrée de catalogue `datasets/<nom>.yaml` :

```yaml
name: my_images                     # = nom du fichier
modality: image
description: "Ce que contient le dataset, taille, téléchargement"
source: "https://… (page officielle)"
license: "Licence vérifiée sur la page officielle (sinon : « à vérifier »)"
paths:
  default: "~/datasets/my_images"
  # scw-l40s: "/root/datasets/my_images"    # clé = nom de l'hôte (.wmlab/hosts.yaml)
```

Exemple complet : `templates/dataset.py` (dossier + split train/val déterministe).

## 6. Prétraitement — `PREPROCESS`

Callable `item_x -> item_x` sur un tenseur **sans dim batch** ; attribut de classe
`deterministic: bool` (False s'il utilise de l'aléa). Les ops image s'appliquent frame par
frame sur la vidéo (pour les builtins).

```python
class GammaParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gamma: float = Field(1.0, gt=0)

@PREPROCESS.register("gamma", modality=["image", "video"], params=GammaParams)
class Gamma:
    """Correction gamma."""
    deterministic = True

    def __init__(self, gamma: float = 1.0) -> None:
        self.gamma = gamma

    def __call__(self, x: Tensor) -> Tensor:
        return x.clamp(0.0, 1.0) ** self.gamma
```

Pour un prétraitement vidéo aléatoire, tirer UN paramètre par clip (pas par frame).

## 7. Perte — `LOSSES` et `StepBatch`

```python
@dataclass
class StepBatch:                     # wmlab.losses.base
    x: Tensor; x_w: Tensor; x_att: Tensor; msg: Tensor; logits: Tensor
    modality: str; model: WatermarkModel; step: int; phase: str
    extras: dict                     # ex. {"sample_rate": 16000, "fps": 24}
```

Une perte est un `nn.Module` construit avec ses params, appelé `loss(batch) -> Tensor`
**scalaire**. Le poids est dans le YAML (`loss.terms.<t>.weight`, `phases[].loss_weights`),
jamais dans le code. Exemple complet : `templates/loss.py`.

```yaml
loss:
  terms:
    msg: {name: message_bce, weight: 1.0}
    img: {name: mse, weight: 100.0}      # MSE sur [0,1] : poids ~100 (voir curriculum-patterns)
    tv:  {name: residual_tv, weight: 10.0, params: {p: 1}}
```

Clés loggées : `train/loss/<terme>`, `train/weight/<terme>`.

## 8. Métrique — `METRICS` et robustesse

Qualité : fonction `(x, x_w, **ctx) -> Tensor[B]` (un score par élément ; `ctx` peut contenir
`sample_rate`, `fps`). Utilisée dans `benchmark.quality_metrics` et en validation
(`val/<nom>`).

```python
@METRICS.register("residual_linf", modality=None)
def residual_linf(x: Tensor, x_w: Tensor, **ctx) -> Tensor:
    """Amplitude maximale du résidu (plus bas = moins visible)."""
    return (x_w - x).abs().flatten(1).amax(dim=1)
```

Robustesse (`wmlab.metrics.robustness`) :
- `bit_accuracy(logits, msg) -> Tensor[B]`
- `detection_threshold(nbits, fpr) -> int` : plus petit k tel que P(Binom(n, ½) ≥ k) ≤ fpr
- `tpr_at_fpr(logits, msg, fpr) -> float`

## 9. Composants builtin (noms exacts)

| Registre | Noms (params à strength = 1 / défauts) |
|---|---|
| modèles | `hidden` (image : `channels=64`, `enc_blocks=4`, `dec_blocks=7`, `strength=1.0`, `grid=4`) · `audio_conv` (audio : `channels=32`, `blocks=4`, `strength=1.0`, `segments=8`) · `video_framewise` (vidéo : `image_model="hidden"`, `image_params={}`, `aggregate="mean"`) |
| attaques image | `identity` · `jpeg` (réel, non diff., `quality=50`) · `jpeg_diff` (`quality=50`) · `gaussian_noise` (`std=0.06`) · `gaussian_blur` (`sigma=2.0`) · `resize` (`scale=0.5`) · `crop` (`area=0.5`) · `rotate` (`degrees=10`) · `brightness` (`delta=0.3`) · `contrast` (`factor=0.5`) · `saturation` (`factor=0.5`) · `combo` (`attacks=[{name, params}]`) |
| attaques audio | `identity` · `gaussian_noise` (`snr_db=20`) · `lowpass` (`cutoff_hz=3000`) · `highpass` (`cutoff_hz=500`) · `resample` (`rate=8000`) · `amplitude` (`gain_db=-6`) · `echo` (`delay_ms=100`, `decay=0.3`) · `time_shift` (`max_ms=50`) · `mp3` (réel, `bitrate_kbps=64`) · `aac` (réel, `bitrate_kbps=64`) ; toutes sauf `identity`, `gaussian_noise`, `amplitude` ont aussi `sample_rate=16000` |
| attaques vidéo | `identity` · `framewise` (`attack="jpeg"`, `params={}`) · `h264` (réel, `crf=28`, `preset="medium"`, `fps=24`) · `frame_drop` (`p=0.2`) · `temporal_blur` (`weight=0.3`) |
| datasets | `synthetic` (`modality`, `size=128`, `length=16000`, `frames=8`, `num_items=512`, `seed=0`, `sample_rate=16000`) · `image_folder` (`root`, `extensions`, `recursive=True`, `limit`) · `audio_folder` (`root`, `sample_rate=16000`, `clip_seconds=1.0`, `limit`, `extensions`, `recursive=True`) · `video_folder` (`root`, `frames=16`, `size=128`, `limit`, `extensions`, `recursive=True`) |
| baselines (plugins `lab/`) | `videoseal`, `pixelseal` (entraînable), `wam`, `trustmark` (`variant`), `invismark` (`uuid_payload`), `ssl_watermark` — poids : `wmlab weights list` |
| prétraitements | image/vidéo : `resize` (`size`), `center_crop` (`size`), `random_crop` (`size`), `random_resized_crop` (`size`, `scale=[0.5,1.0]`, `ratio=[0.75,1.333]`), `hflip` (`p=0.5`) · audio : `random_clip` (`seconds`, `sample_rate`), `normalize_peak` (`peak=0.9`), `random_gain` (`db=6`) · vidéo : `temporal_crop` (`frames`, `random=True`) |
| pertes | `message_bce` · `mse` · `l1` · `ssim` (image/vidéo, 1−SSIM) · `stft` (audio multi-résolution) · `temporal` (vidéo) · `lpips` (image, optionnel) |
| métriques | `psnr`, `ssim` (image/vidéo) · `snr`, `si_snr` (audio) · `stoi` (audio, optionnel) · `lpips` (optionnel) · `flicker` (vidéo) |

Toujours confirmer avec `wmlab registry` : les params réels (et leurs bornes) font foi.
