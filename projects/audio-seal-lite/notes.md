# Audio — AudioSeal-lite

Journal du projet audio. Conclusions détaillées : `notes.md` de chaque run.

## Objectif

Un schéma audio léger, robuste aux transformations courantes (codecs MP3/AAC, filtrage,
bruit, écho, décalage temporel, rééchantillonnage), inspiré d'AudioSeal (Meta, 2024) :
générateur de résidu + détecteur, message de 16 bits.

- Qualité : SNR ≥ 25 dB (contrainte du projet) ; suivre aussi `si_snr` dans le benchmark.
- Robustesse : `bit_acc_mean` sur `audio_v1`.

## Contenu

| Fichier | Rôle |
|---|---|
| `experiments/baseline.yaml` | `audio_conv`, 1 s à 16 kHz, pertes `message_bce` + `mse` + `stft`, rampe d'attaques puis codecs réels |
| `hpo/lr.yaml` | TPE + ASHA sur le LR (poids STFT et force du résidu en nuisance) |

## Points d'attention

- Les codecs (`mp3`, `aac`) sont réels (PyAV) donc **non différentiables** : le trainer
  applique un straight-through estimator ; ils sont plus lents (CPU) — on les réserve à la
  dernière phase et à la validation.
- `time_shift` exige un détecteur invariant au décalage (pooling global) : si
  `val/bit_acc` est bon mais que le benchmark `time_shift_50ms` est ~0,5, suspecter
  l'architecture.
- Warmup : **aucune** contrainte de qualité (`{wav: 0.0, spec: 0.0}`). Mesuré sur le Mac :
  avec `{wav: 0.1, spec: 0.1}` le détecteur reste bloqué à 0,5 pendant tout le run (la perte
  `stft`, normalisée, pèse lourd même pour un résidu minuscule) ; à 0, `bit_acc` ≈ 0,99 dès
  300 steps et la rampe remonte ensuite le SNR (~32 dB en fin de run, `bit_acc_mean` ~0,97).
- Poids des pertes : la MSE sur des signaux dans [-1, 1] est numériquement très petite ; si le
  SNR reste bas, augmenter `wav`/`spec` en fin de curriculum (ou lancer `hpo/lr.yaml`).
- Vitesse : `audio_conv` à `channels: 32` coûte ~0,8 s/step sur le Mac (batch 16 × 1 s) ;
  `channels: 16` ~0,2 s/step. Garder 16 en local, monter sur GPU.

## Données réelles

- Parole propre : `@librispeech_clean100` (CC BY 4.0) ; parole variée : `@voxpopuli_en` (CC0).
- Musique : `@fma_small` (licences par artiste).
- Créer `benchmarks/audio_v2.yaml` sur un sous-ensemble figé (jamais modifier `audio_v1`).

## Journal

- 2026-09-30 — `20260930-073039-baseline-1c3e` (tag `demo`, raccourci : phases 100/300/100,
  warmup LR 30) — val/snr 25,8 dB, val/bit_acc 0,96 ; benchmark `audio_v1` : SNR 27,5 dB,
  bit_acc_mean 0,90 en 2 min 20 sur le Mac. Le décodeur audio accroche vite (0,75 à 100
  steps) : un tiers des steps suffit pour une démonstration.
