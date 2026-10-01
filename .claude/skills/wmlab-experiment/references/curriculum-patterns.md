# Motifs de curriculum pour le watermarking (prêts à l'emploi)

Un curriculum = la liste `phases` d'une expérience. Chaque phase fixe : durée, planning de
LR, poids des pertes (constants ou planifiés), attaques et leur force, données, gel de
modules. Les extraits ci-dessous sont des blocs `phases` (ou des morceaux de phase) valides à
adapter ; les termes de perte supposés sont `msg` (`message_bce`) et `img` (`mse`) — `wav` /
`spec` en audio, `vid` / `temp` en vidéo.

**Échelle des poids.** La MSE sur des signaux dans [0, 1] est minuscule (PSNR 38 dB ⇔ MSE
1,6e-4) face à la BCE du message (0,05–0,7) : les poids de qualité MSE utiles sont de l'ordre
de **10 à 1 000**. Repères mesurés sur la baseline `image-hidden` (1 500 steps, 64 px) :
affinage `img: 2` ⇒ ~14 dB (marque très visible) ; `img: 200` ⇒ ~32 dB ; `img: 300` ⇒ ~34 dB ;
`img: 1000` ⇒ ~42 dB mais `bit_acc_mean` ~0,62. Les pertes normalisées (`stft`, `ssim`) se
pondèrent autour de 1.

Vérifier le résultat dans l'éditeur de l'app (timeline LR / poids / force par phase) ou avec
`wmlab validate`, puis `wmlab train <spec> --smoke`.

## Sommaire

1. Warmup du décodeur
2. Rampe d'attaques
3. Rampe des poids de qualité
4. Affinage final avec attaques réelles
5. WSD et branchement de la décroissance
6. Curriculum de résolution (ou de durée)
7. Image → vidéo
8. Gel de modules
9. Mélange et difficulté des attaques
10. Répartition des steps (règles empiriques)

---

## 1. Warmup du décodeur

Problème : au départ, le résidu est quasi nul et les attaques détruisent le peu de signal ;
le décodeur reste bloqué à 50 % de bits justes (`train/bit_acc` ≈ 0,5). Solution : quelques
centaines / milliers de steps **sans attaque**, contrainte de qualité faible, warmup du LR.

```yaml
  - name: warmup_decode
    steps: 2000                                  # ~10–15 % du total
    lr: {schedule: linear_warmup, warmup_steps: 500}
    loss_weights: {img: 0.1}                     # le message d'abord
    attacks: []                                  # identité
```

Critère de sortie : `train/bit_acc` > 0,95 à la fin de la phase. Sinon : allonger la phase,
monter `model.params.strength`, vérifier le LR, baisser encore les poids de qualité.
Audio : `loss_weights: {wav: 0.0, spec: 0.0}` — mesuré sur `audio-seal-lite`, même
`spec: 0.1` (perte STFT normalisée) laisse le détecteur bloqué à 0,5.
Le PSNR / SNR est très bas en fin de warmup : c'est attendu, la rampe de qualité le remonte.

## 2. Rampe d'attaques

Augmenter la sévérité progressivement ; garder une part d'exemples propres.

```yaml
  - name: robustness_ramp
    steps: 10000
    lr: {schedule: constant}
    attacks:
      - {name: identity, p: 0.5}                 # ~10 % d'exemples non attaqués
      - {name: jpeg_diff, params: {quality: 50}}
      - {name: gaussian_blur, params: {sigma: 2.0}}
      - {name: crop, params: {area: 0.5}}
      - {name: gaussian_noise, params: {std: 0.06}}
    attack_strength: {ramp: [0.0, 1.0], end: 0.7}   # force max atteinte à 70 % puis plateau
    random_strength: true                         # force tirée dans U(0, s) : couvre les faibles forces
```

Variantes :
- rampe « lente puis rapide » : `{ramp: [0.0, 1.0], shape: exp}` ;
- paliers : `{piecewise: [[0.0, 0.2], [0.3, 0.2], [0.6, 0.7], [1.0, 1.0]]}` ;
- dépasser la sévérité du benchmark pour la marge : `params: {quality: 40}` (le benchmark
  mesure `quality: 50` à strength 1) ;
- introduire les attaques par familles : une phase « valeurs » (bruit, luminosité,
  contraste), puis une phase « géométrie » (crop, resize, rotation), puis tout.

Audio (mêmes principes) :

```yaml
    attacks:
      - {name: identity, p: 0.5}
      - {name: gaussian_noise, params: {snr_db: 20}}
      - {name: lowpass, params: {cutoff_hz: 3000}}
      - {name: highpass, params: {cutoff_hz: 500}}
      - {name: amplitude, params: {gain_db: -6}}
      - {name: echo, params: {delay_ms: 100, decay: 0.3}}
      - {name: time_shift, params: {max_ms: 50}}
      - {name: resample, params: {rate: 8000}}
    attack_strength: {ramp: [0.0, 1.0]}
```

## 3. Rampe des poids de qualité

Monter la contrainte d'imperceptibilité quand le décodage est acquis, en même temps que les
attaques (sinon le modèle « paie » la robustesse en PSNR).

```yaml
    loss_weights:
      img: {ramp: [0.1, 100.0], shape: cosine}  # démarrage doux
      # audio : wav: {ramp: [0.1, 1.0]}, spec: {ramp: [0.1, 1.0]}
      # vidéo : vid: {ramp: [0.1, 100.0]}, temp: {ramp: [0.05, 0.2]}
```

Le poids du terme `msg` reste en général à 1 (référence). Si le PSNR plonge au milieu d'une
rampe : rampe de qualité plus rapide (`end: 0.5`) ou force d'attaque plus lente.

## 4. Affinage final avec attaques réelles

Les approximations différentiables (`jpeg_diff`) ne sont pas les vrais codecs : finir avec
les attaques réelles (non différentiables, straight-through automatique), LR décroissant,
poids de qualité fort.

```yaml
  - name: finetune_imperceptibility
    steps: 3000                                  # ~15–20 % du total
    lr: {schedule: cosine, min_factor: 0.01}
    loss_weights: {img: 300.0}
    attacks:
      - {name: jpeg, params: {quality: 50}}
      - {name: gaussian_blur, params: {sigma: 2.0}}
      - {name: crop, params: {area: 0.5}}
    attack_strength: 1.0
```

Audio : `mp3` / `aac` (`bitrate_kbps: 64`) ; vidéo : `h264` (`crf: 28`).

## 5. WSD et branchement de la décroissance

Warmup → Stable (LR constant, long) → Decay (court). Avantages : la durée totale n'a pas à
être fixée d'avance ; on peut brancher plusieurs décroissances depuis le même plateau.
Exemple complet : `projects/image-hidden/experiments/wsd_branch.yaml`.

```yaml
phases:
  - {name: warmup_decode, steps: 2000, lr: {schedule: linear_warmup, warmup_steps: 500},
     loss_weights: {img: 0.1}, attacks: []}
  - name: stable
    steps: 16000
    lr: {schedule: constant}
    loss_weights: {img: {ramp: [0.1, 100.0], end: 0.5}}
    attacks: [{name: identity, p: 0.5}, {name: jpeg_diff, params: {quality: 50}}, {name: crop, params: {area: 0.5}}]
    attack_strength: {ramp: [0.0, 1.0], end: 0.5}
  - name: decay
    steps: 4000                                  # 10–20 % du total
    lr: {schedule: wsd, decay_fraction: 1.0, min_factor: 0.0}   # toute la phase décroît
    loss_weights: {img: {ramp: [100.0, 300.0]}}
    attacks: [{name: jpeg, params: {quality: 50}}, {name: crop, params: {area: 0.5}}]
checkpoint: {every_steps: 2000, keep_last: 3, at_phase_end: true, keep_best: {"val/bit_acc_mean": max}}
```

Variante en une seule phase (pas de branchement) :
`lr: {schedule: wsd, warmup_steps: 500, decay_fraction: 0.2, min_factor: 0.0}`.

Brancher une autre décroissance (copie de l'expérience, seule la phase `decay` change) :

```yaml
init_from:
  run: "<run_id du tronc>"
  checkpoint: "phase_end:stable"                 # ou "step_012000" pour un plateau plus court
  start_phase: decay
  load_optimizer: true                           # garder les moments d'Adam
```

## 6. Curriculum de résolution (ou de durée)

Apprendre à basse résolution (4× moins cher à 64 px qu'à 128 px), finir à la résolution
cible. Exige un extracteur à pooling global. Valider à la résolution cible dès le début.
Exemple complet : `projects/image-hidden/experiments/resolution_curriculum.yaml`.

```yaml
  - name: finetune_256
    steps: 3000
    batch_size: 16                               # moins d'images par batch à haute résolution
    lr: {schedule: cosine, warmup_steps: 200, min_factor: 0.01}
    data:
      train:
        dataset: image_folder
        params: {root: "@coco2017"}
        preprocess: [{name: random_resized_crop, params: {size: 256}}, {name: hflip}]
```

Audio : même idée sur la durée des clips (`random_clip` 0,5 s puis 2 s) ; vidéo : sur le
nombre de frames (`temporal_crop` 4 puis 16) ou la taille.

## 7. Image → vidéo

Recette Video Seal : pré-entraîner sur images (moins cher, plus varié), puis fine-tuner
sur vidéo (H.264, pertes de frames, perte temporelle). Avec `video_framewise`, un clip d'une
frame est une image : même modèle, poids réutilisables. Détail et YAML complets :
`projects/video-framewise/notes.md`.

```yaml
  - name: image_pretrain                         # phase 0 : clips d'1 frame
    steps: 20000
    batch_size: 64
    data:
      train:
        dataset: synthetic                       # réel : video_folder root "@sav" frames 1, ou un plugin image→clip
        params: {modality: video, size: 128, frames: 1, num_items: 100000}
    lr: {schedule: linear_warmup, warmup_steps: 1000}
    loss_weights: {vid: {ramp: [0.1, 100.0]}, temp: 0.0}
    attacks: [{name: framewise, params: {attack: jpeg_diff, params: {quality: 50}}}]
    attack_strength: {ramp: [0.0, 1.0]}
  - name: video_finetune                         # phase 1 : 8–16 frames, attaques vidéo
    steps: 5000
    reset_optimizer: true                        # nouvelle distribution
    lr: {schedule: cosine, factor: 0.3, min_factor: 0.01}
    loss_weights: {vid: 300.0, temp: 0.2}
    attacks:
      - {name: framewise, params: {attack: jpeg_diff, params: {quality: 50}}}
      - {name: h264, params: {crf: 28}}
      - {name: frame_drop, params: {p: 0.2}}
```

## 8. Gel de modules

`freeze` = préfixes de noms de paramètres. Lister les préfixes d'un modèle :

```bash
.venv/bin/python -c "from wmlab.registry import MODELS, load_plugins; load_plugins(); \
m = MODELS.build('hidden', nbits=48); print(sorted({n.split('.')[0] for n, _ in m.named_parameters()}))"
```

Cas d'usage :
- **robustesse à une nouvelle attaque sans toucher à l'imperceptibilité** : geler
  l'embedder, n'entraîner que le décodeur (le résidu, donc le PSNR, ne bouge plus) ;
- **améliorer la qualité sans perdre le décodeur** : geler le décodeur, poids de qualité ↑.

```yaml
  - name: decoder_only_new_attack
    steps: 2000
    freeze: ["encoder."]                         # préfixe réel du modèle (ici `hidden`)
    reset_optimizer: true
    lr: {schedule: cosine, factor: 0.3, min_factor: 0.01}
    attacks: [{name: rotate, params: {degrees: 15}}, {name: jpeg_diff, params: {quality: 50}}]
    attack_strength: 1.0
```

Souvent combiné à `init_from` (repartir d'un run entraîné).

## 9. Mélange et difficulté des attaques

- `p` pondère le tirage : sur-représenter les attaques où `val/bit_acc/<label>` est faible.
- Garder `identity` (p ≈ 0,3–0,5) pour ne pas dégrader le cas propre.
- `combo` enchaîne des attaques (crop puis JPEG) : à introduire en fin de curriculum.
- Attaques de validation (`eval.val_attacks`) ≈ attaques du benchmark, mais pas
  exactement les mêmes que l'entraînement : sinon on mesure du sur-apprentissage.

## 10. Répartition des steps (règles empiriques)

| Phase | Part du total |
|---|---|
| warmup / décodage propre | 5–15 % |
| rampe de robustesse (+ plateau) | 60–75 % |
| affinage / décroissance | 10–20 % |

Toujours prévoir `at_phase_end: true` (points de reprise), une validation au moins 3–5 fois
par phase, et des échantillons (`samples`) pour inspecter visuellement / à l'oreille.
