# Vidéo — frame par frame

Journal du projet vidéo. Conclusions détaillées : `notes.md` de chaque run.

## Objectif

Une baseline vidéo simple et robuste : un modèle **image** (`hidden`) appliqué à chaque
frame avec le même message de 32 bits, logits agrégés sur les frames (`video_framewise`),
perte `temporal` contre le scintillement.

- Qualité : PSNR ≥ 36 dB ; surveiller `flicker` dans le benchmark `video_v1`.
- Robustesse : `bit_acc_mean` sur `video_v1` (H.264 CRF 23/28, pertes de frames, JPEG par
  frame, flou temporel).

## Stratégie : pré-entraînement image, puis fine-tune vidéo

C'est la recette de **Video Seal** (Meta, 2024) : les images sont bien moins chères que la
vidéo (un step vidéo de 8 frames coûte ~8 steps image) et beaucoup plus variées. On apprend
donc d'abord l'essentiel du tatouage sur des images, puis on spécialise sur la vidéo :
codec H.264, pertes de frames, cohérence temporelle. Le fine-tune vidéo est court.

Comme `video_framewise` enveloppe un modèle image, un clip d'**une seule frame** est
exactement une image : le pré-entraînement se fait avec le même modèle, donc des poids
directement réutilisables.

### Option A — deux runs reliés par `init_from` (recommandée)

1. Créer l'expérience de pré-entraînement :
   `wmlab new experiment video-framewise image_pretrain --from baseline`, puis y mettre
   des clips d'une frame, un plus gros batch, des attaques image par frame, et plus de steps :

   ```yaml
   data:
     train:
       dataset: synthetic
       params: {modality: video, size: 64, frames: 1, num_items: 4096, seed: 0}
     val:
       dataset: synthetic
       params: {modality: video, size: 64, frames: 1, num_items: 128, seed: 1}
     batch_size: 32
   loss:
     terms:
       msg: {name: message_bce, weight: 1.0}
       vid: {name: mse, weight: 100.0}        # pas de terme temporal : une seule frame
   phases:
     - name: warmup_decode
       steps: 1000
       lr: {schedule: linear_warmup, warmup_steps: 200}
       loss_weights: {vid: 0.1}
       attacks: []
     - name: image_robustness
       steps: 4000
       lr: {schedule: cosine, min_factor: 0.05}
       loss_weights: {vid: {ramp: [0.1, 100.0]}}
       attacks:
         - {name: framewise, params: {attack: jpeg_diff, params: {quality: 50}}}
         - {name: framewise, params: {attack: gaussian_blur, params: {sigma: 2.0}}}
         - {name: framewise, params: {attack: crop, params: {area: 0.5}}}
       attack_strength: {ramp: [0.0, 1.0]}
   eval:
     val_attacks: [{label: jpeg50, name: framewise, params: {attack: jpeg, params: {quality: 50}}}]
     final_benchmark: null
   ```

   Le **modèle doit rester identique** (`video_framewise` + mêmes `image_params`), sinon les
   poids ne se rechargent pas.

2. Fine-tune vidéo : dans `baseline.yaml` (ou une copie), brancher sur le pré-entraînement
   et sauter le warmup (le décodeur sait déjà décoder) :

   ```yaml
   init_from:
     run: "<run_id de image_pretrain>"         # onglet Runs de l'app
     checkpoint: "best:val/bit_acc_mean"       # ou "last", "phase_end:image_robustness"
     start_phase: robustness_ramp              # on démarre directement à la phase vidéo
     load_optimizer: false                     # nouvelle distribution : optimiseur neuf
   ```

   Réduire aussi `optimizer.lr` (÷ 3 à ÷ 10) : on affine un modèle déjà entraîné.

### Option B — un seul run, curriculum image → vidéo

Même idée en une seule expérience : une première phase sur des clips d'une frame grâce à la
surcharge `data` par phase, puis les phases vidéo sur 8 frames.

```yaml
phases:
  - name: image_pretrain
    steps: 2000
    batch_size: 32
    data:
      train:
        dataset: synthetic
        params: {modality: video, size: 64, frames: 1, num_items: 4096, seed: 0}
    lr: {schedule: linear_warmup, warmup_steps: 200}
    loss_weights: {vid: {ramp: [0.1, 100.0]}, temp: 0.0}
    attacks: [{name: framewise, params: {attack: jpeg_diff, params: {quality: 50}}}]
    attack_strength: {ramp: [0.0, 1.0]}
  # … puis robustness_ramp / finetune_imperceptibility de baseline.yaml (8 frames)
```

Moins flexible que l'option A (on ne peut pas réutiliser un même pré-entraînement pour
plusieurs fine-tunes), mais tout est dans un seul run.

### Avec de vraies images (COCO) plutôt que du synthétique

Un projet vidéo n'accepte que des datasets de modalité `video` (`image_folder` sera refusé
par `wmlab validate`). Deux solutions :

- `video_folder` sur `@sav` avec `frames: 1` (une frame par vidéo : moins varié que COCO) ;
- un plugin `lab/datasets/image_as_video.py` (modalité `video`) qui lit un dossier d'images
  (`root: "@coco2017"`) et renvoie des clips `[1, 3, H, W]` — à demander à Claude avec la
  skill `wmlab-experiment` (« crée un dataset vidéo qui lit des images COCO comme des clips
  d'une frame »).

### Réutiliser un run du projet image-hidden ?

Pas directement : `image-hidden` utilise 48 bits (32 ici) et un modèle `hidden` nu, dont les
noms de poids diffèrent de ceux de `video_framewise` (le modèle image y est imbriqué sous
`image.`). Le trainer vérifie `modality` et `nbits` du checkpoint puis charge les poids en
mode strict : un checkpoint `hidden` est refusé avec un message explicite. L'option A évite
ce problème (même modèle `video_framewise` des deux côtés).

## Points d'attention

- `h264` et le vrai `jpeg` sont non différentiables : straight-through automatique.
- Le coût d'un step croît avec `batch_size × frames × H × W` : sur le Mac, garder ≤ 64 images
  64 px par step ; sur GPU, passer à 16 frames 128–256 px.
- Si `val/bit_acc/frame_drop` est bon mais `h264_crf28` mauvais : le résidu est trop haute
  fréquence (le codec l'efface) → augmenter la part de H.264 ou la force du résidu.

## Journal

- 2026-09-30 — `20260930-073453-baseline-02ca` (tag `demo`, raccourci : phases 200/450/150) —
  val/psnr 38,7 dB, val/bit_acc 0,72, val/bit_acc_mean 0,70 ; benchmark `video_v1` : PSNR
  38,1 dB, bit_acc_mean 0,71. Compromis proche du run complet (39 dB / 0,73).
- 2026-09-30 — run raccourci 100/300/100 (supprimé) : le warmup de décodage à 100 steps ne
  suffit pas (bit_acc 0,58 à sa fin) et la rampe `vid` 0,1 → 100 efface ensuite le résidu :
  PSNR 50 dB mais bit_acc 0,49 (décodeur bloqué au hasard). **Pour raccourcir un run, ne
  jamais raccourcir `warmup_decode`** (200 steps) : réduire seulement les deux phases suivantes.
