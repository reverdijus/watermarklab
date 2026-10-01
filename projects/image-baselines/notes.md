# Image — baselines pré-entraînées

Journal du projet. Les modèles de l'état de l'art sont évalués avec leurs **poids officiels**
(aucun entraînement : expériences `phases: []`), à charge utile égale (32 bits, la capacité
de Watermark Anything). Détails des modèles, licences et poids : `README.md` §8,
`wmlab weights list`, `lab/models/THIRD_PARTY_NOTICES.md`.

## Contenu

| Expérience | Modèle | Poids | Licence des poids |
|---|---|---|---|
| `videoseal` | Video Seal 1.0 (Meta, 2024), 256 bits natifs | `videoseal_1.0` | MIT |
| `pixelseal` | Pixel Seal (Meta, 2025), 256 bits natifs | `pixelseal` | MIT |
| `pixelseal_finetune` | Pixel Seal ré-entraîné (détecteur puis conjoint, COCO) | `pixelseal` | MIT |
| `wam` | Watermark Anything (Meta, 2025), 32 bits | `wam_mit` | MIT |
| `trustmark` | TrustMark Q (Adobe, 2023), 100 bits bruts | `trustmark_q_*` | MIT |
| `invismark` | InvisMark (Microsoft, 2025), 100 bits dont 6 figés | `invismark_paper` (miroir HF) | non précisée |
| `ssl_watermark` | SSL watermarking (Meta, 2022), bits libres | `ssl_*` | **CC-BY-NC** |

## Vérifications faites (2026-09-30)

- Chargement **strict** des poids officiels dans les réimplémentations (aucune clé manquante
  ou en trop), puis comparaison au code officiel exécuté avec les mêmes poids et les mêmes
  entrées : écart ≤ 2e-6 sur l'image tatouée et ≤ 7e-5 sur les logits (Video Seal, Pixel
  Seal, WAM) ; identique au bit près pour les réseaux TrustMark et InvisMark.
- InvisMark : sur les vrais poids, le décodeur renvoie **toujours** `0100` aux positions
  48-51 et `10` aux positions 64-65 (bits de version et de variante des UUIDv4 du jeu
  d'entraînement) : capacité réelle 94 bits. `uuid_payload: true` force ces bits et les
  retire du message (bit_acc 1,0 sur 94 bits sans attaque). À 32 bits, ils sont hors charge
  utile.

## Premiers résultats — `image_v1` (synthétique 128 px, 64 images, 32 bits)

Runs locaux (Mac M4, MPS), coût 0 €. Bit accuracy par attaque (benchmark final) :

| Modèle | PSNR | none | jpeg q50 | jpeg q80 | flou σ2 | bruit 0,06 | crop 50 % | resize 50 % | rot. 10° | lumin. | contraste | crop+jpeg | **moyenne** | TPR@1e-6 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| videoseal | 50,7 | 0,96 | 0,53 | 0,61 | 0,72 | 0,50 | 0,81 | 0,81 | 0,91 | 0,91 | 0,96 | 0,51 | **0,726** | 0,26 |
| pixelseal | 51,0 | 0,98 | 0,49 | 0,52 | 0,58 | 0,50 | 0,84 | 0,75 | 0,95 | 0,93 | 0,97 | 0,52 | **0,705** | 0,30 |
| wam | 42,0 | 0,98 | 0,51 | 0,52 | 0,84 | 0,81 | 0,82 | 0,93 | 0,61 | 0,99 | 0,99 | 0,51 | **0,753** | 0,33 |
| trustmark | 46,0 | 1,00 | 0,80 | 0,96 | 0,91 | 0,70 | 0,59 | 0,99 | 0,50 | 1,00 | 1,00 | 0,50 | **0,794** | 0,44 |
| invismark | 47,6 | 0,95 | 0,49 | 0,49 | 0,49 | 0,51 | 0,81 | 0,47 | 0,50 | 0,93 | 0,90 | 0,49 | **0,608** | 0,19 |
| ssl_watermark | 41,9 | 0,89 | 0,62 | 0,75 | 0,74 | 0,57 | 0,64 | 0,80 | 0,77 | 0,78 | 0,86 | 0,58 | **0,712** | 0,02 |

**À ne pas sur-interpréter.** Ces modèles ont été entraînés sur des photos à 256 px et plus ;
sur 128 px synthétiques (aplats, dégradés), leur tatouage est faible (JND) et le
redimensionnement 128 → 256 → 128 en efface une partie. Contrôle sur 4 photos naturelles à
512 px (même message de 32 bits) : flou σ2 et resize 50 % à 1,00 pour tous ; JPEG q50
0,79 (videoseal), 0,77 (pixelseal), 0,89 (wam), 0,98 (trustmark), 0,70 (invismark) ; bruit
0,06 de 0,55 (pixelseal) à 1,00 (wam) ; crop 50 % 0,92–1,00 sauf trustmark 0,57. Conclusions
provisoires, cohérentes sur les deux jeux : TrustMark est le plus robuste au JPEG et le plus
fragile au crop et à la rotation (décodeur ResNet global) ; WAM (décodage par pixel) résiste
au crop et au bruit ; les modèles Meta 256 bits gardent le PSNR le plus élevé.

## Suite proposée

1. **Benchmark réel `image_v2`** (ne pas modifier `image_v1`) : DIV2K (`@div2k_valid`,
   100 photos 2K, ~450 Mo à télécharger avec accord), `center_crop` 512, mêmes attaques ;
   relancer les 6 évaluations (quelques minutes en local, 0 €). C'est la condition pour des
   comparaisons publiables.
2. **Fine-tuning Pixel Seal** (`pixelseal_finetune`, hypothèse dans le YAML) : exige COCO sur
   la machine d'entraînement. `--smoke` validé en local ; ensuite un run court sur GPU
   Scaleway (`--max-hours 1`, ~3 000 steps, batch 16 à 256 px) pour mesurer le débit, puis le
   run complet si le gain en val/bit_acc_mean dépasse +0,03 à PSNR −1 dB au plus. Chaque
   checkpoint pèse ~0,9 Go (poids + état AdamW) : rapatrier puis retirer la machine.
3. Comparer à **capacité native** (94 / 100 / 256 bits) : un autre projet avec un autre
   `message_bits` (les modèles s'adaptent tant que `message_bits` ≤ capacité).
