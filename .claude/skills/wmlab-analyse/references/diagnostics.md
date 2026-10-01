# Diagnostics typiques du watermarking

Pour chaque cas : **symptômes** (clés de métriques), **vérifications** (chiffres à extraire),
**causes probables**, **remèdes** (à proposer comme prochain run). Lire les courbes par
phase : la baisse de `train/bit_acc` quand `train/attack_strength` monte est normale.

## Sommaire

1. Le run n'a pas fini (failed, killed, lost)
2. Décodeur bloqué à 0,5
3. Imperceptibilité qui s'effondre
4. Surapprentissage aux attaques d'entraînement
5. GPU sous-utilisé / goulot de données
6. LR trop fort, instabilités, NaN
7. Robustesse qui plafonne
8. Spécificités audio et vidéo
9. Lire une étude HPO
10. Checkpoints non rapatriés, inventaire des machines (mode équipe)
11. Extraits de code utiles

---

## 1. Le run n'a pas fini

- `status: failed` : `run.json.error` + fin de `stdout.log` (traceback). Causes fréquentes :
  OOM GPU (`CUDA out of memory` → batch ↓, résolution ↓), dataset introuvable (chemin `@…`
  absent sur l'hôte → `datasets/<nom>.yaml`, `paths.<hôte>`), codec absent, NaN.
- `status: killed` : arrêt demandé, SIGTERM, ou `max_hours reached` (checkpoint `last`
  sauvegardé → reprendre avec `init_from: {run: <id>, checkpoint: last, resume: true}` :
  le run repart au step du checkpoint, phase localisée, optimiseur restauré ; sans
  `resume`, `init_from` ne recharge que les poids et **rejoue** le curriculum depuis
  `start_phase`).
- `status: lost` : heartbeat > 600 s. Machine éteinte / process tué (OOM système) /
  sync interrompue. Vérifier la page « Machines », `events.jsonl` (dernier événement),
  `system.jsonl` (RAM qui monte avant la perte).

## 2. Décodeur bloqué à 0,5

**Symptômes** : `train/bit_acc` et `val/bit_acc` ≈ 0,5 (±0,02) après la phase de warmup ;
`train/loss/msg` ≈ 0,693 (= ln 2) constante.

**Vérifications** : valeur de `val/bit_acc` (SANS attaque) à la fin de `warmup_decode` ;
`train/lr` ; `train/weight/<qualité>` pendant le warmup ; PSNR très élevé (> 50 dB) = résidu
quasi nul.

**Causes** : attaques dès le début (pas de warmup) ; poids de qualité trop fort d'emblée
(le modèle annule le résidu) ; `strength` du modèle trop faible ; LR trop faible (ou trop
fort : BCE qui oscille autour de ln 2) ; bug de modèle (message non injecté, logits passés
dans une sigmoïde).

**Remèdes** : phase `warmup_decode` sans attaque avec `loss_weights: {img: 0.1}` (audio :
`{wav: 0.0, spec: 0.0}` — une perte normalisée comme `stft` suffit à bloquer le détecteur) ;
`strength` ↑ ; balayage du LR (×0,3 / ×3) ; `wmlab plugin-test model <nom>` pour un modèle
maison.

## 3. Imperceptibilité qui s'effondre

**Symptômes** : `val/psnr` (image/vidéo) ou `val/snr` (audio) qui chute pendant la rampe
d'attaques et ne remonte pas ; échantillons avec motifs visibles / bruit audible.

**Vérifications** : `val/psnr` en fin de chaque phase vs contrainte du projet (ex. 38 dB) ;
`train/weight/img` au même moment ; `train/attack_strength`.

**Causes** : poids de qualité trop bas quand les attaques deviennent fortes (rappel
d'échelle : une MSE sur [0, 1] demande des poids ~100–1 000 ; avec `img: 2` la baseline
image plafonne vers 14 dB) ; force d'attaque qui monte trop vite ; `strength` trop grand ;
pas de phase d'affinage.

**Remèdes** : rampe de qualité plus tôt (`end: 0.5`) / plus haute ; rampe d'attaque plus
lente ; phase d'affinage (cosinus, poids qualité ×2–4) ; HPO mono-objectif sous contrainte
de qualité ; ajouter `ssim` / `stft` / `lpips` (perceptuel) au lieu de la seule MSE.

## 4. Surapprentissage aux attaques d'entraînement

**Symptômes** : bonnes `val/bit_acc/<label>` sur les attaques vues à l'entraînement mais
`bit_acc` faible au benchmark sur les autres (`eval/…json`, `robustness[label]`), ou
approximation vs réel : `jpeg_diff` à l'entraînement, `jpeg_q50` réel faible.

**Vérifications** : tableau par attaque du benchmark (`robustness`) comparé à la liste
d'attaques de `spec.yaml` ; écart `val/bit_acc/jpeg50` (réel) vs attaques d'entraînement.

**Remèdes** : diversifier les attaques (familles valeur / géométrie / codec) ; phase finale
avec les attaques réelles (STE) ; paramètres d'attaque plus sévères que le benchmark ;
attaques de validation distinctes de l'entraînement ; `combo`.

## 5. GPU sous-utilisé / goulot de données

**Symptômes** : `gpu_util` moyen < 60 % dans `system.jsonl` ; `train/data_wait_frac` > 0,2
(part du temps passée à attendre les données) ; `train/items_per_s` bas vs runs similaires.
**Sur Mac/MPS, `gpu_util` n'est pas mesuré** (`system.jsonl` : `gpu_mem_gb`, `cpu_percent`,
`ram_gb`, `proc_ram_gb` seulement) : ce diagnostic ne se fait alors que sur
`train/data_wait_frac`, `train/step_time_s` et `train/items_per_s` — le dire explicitement.

**Vérifications** : moyennes de `gpu_util`, `cpu_percent` ; `num_workers` et `batch_size`
dans `spec.yaml` ; décodage lourd (JPEG grands formats, vidéo PyAV) ; attaques CPU
(`jpeg`, `mp3`, `h264`) dans toutes les phases.

**Remèdes** : `num_workers` ↑ (≈ nombre de cœurs / GPU) ; images pré-redimensionnées
(cache) ; `batch_size` ↑ (et LR ajusté) ; codecs réels seulement en fin de curriculum ;
`precision: auto` (bf16 sur CUDA) ; plusieurs essais HPO en parallèle (`parallel: 2–4`) sur
un gros GPU. Un GPU à 30 % coûte le même prix horaire : c'est de l'argent perdu.

## 6. LR trop fort, instabilités, NaN

**Symptômes** : pics de `train/loss` ; `train/bit_acc` qui s'effondre brutalement puis
remonte ; NaN (`events.jsonl` type `error` / `warning`) ; divergence juste après le warmup.

**Vérifications** : `train/lr` au moment des pics ; `grad_clip` dans `spec.yaml` ; phase et
attaque tirée (un codec réel ou un poids qui saute brusquement entre deux phases).

**Remèdes** : LR ÷ 3 ; `warmup_steps` ↑ ; `grad_clip: 1.0` ; `reset_optimizer: true` ou un
court re-warmup quand une phase change fortement les poids ou les données ; rampes plutôt
que sauts de poids ; `precision: fp32` pour isoler un problème de bf16.

## 7. Robustesse qui plafonne

**Symptômes** : `val/bit_acc_mean` stable pendant > 30 % de la rampe malgré LR constant ;
écart entre attaques persistant.

**Remèdes** : capacité (`channels`, blocs) ; `strength` ↑ si la marge de qualité le permet ;
durée de la phase de robustesse ↑ (WSD : prolonger le plateau via une branche) ; décroissance
finale (souvent +1–3 points) ; redistribuer `p` vers les attaques faibles ; `message_bits` ↓
si le cahier des charges le permet (moins de bits = plus robuste).

## 8. Spécificités audio et vidéo

Audio :
- `time_shift_50ms` ≈ 0,5 au benchmark ⇒ détecteur non invariant au décalage (pas de
  pooling global) ;
- `resample_8k` / `lowpass_3k` faibles ⇒ le résidu vit dans les hautes fréquences :
  pondérer la perte `stft` différemment, ajouter ces attaques plus tôt ;
- SNR bon mais marque audible ⇒ la MSE ne suffit pas : perte perceptuelle / `stft`.

Vidéo :
- `h264_crf28` faible mais `frame_drop` bon ⇒ résidu haute fréquence effacé par le codec :
  plus de H.264 à l'entraînement, `strength` ↑, résidu plus basse fréquence ;
- `flicker` élevé ⇒ poids `temporal` ↑ ;
- coût : `batch × frames × H × W` ; pré-entraînement image recommandé (notes du projet
  video-framewise).

## 9. Lire une étude HPO

- `study.json` : statut, budget, `best` ; `trials.jsonl` : état de chaque essai
  (`COMPLETE`, `PRUNED`, `FAIL`), `values`, `params`, `run_id` (runs d'essais avec
  `--include-trials`).
- Beaucoup de `FAIL` : lire le `stdout.log` d'un run d'essai (souvent OOM pour les grandes
  valeurs de `channels` / `batch_size`).
- Beaucoup de `PRUNED` précoces : `min_resource` trop petit (avant la fin du warmup).
- Un essai `PRUNED` dont `intermediate` va jusqu'au dernier step (`progress.step` = total − 1
  dans son `run.json`) a en réalité été entraîné jusqu'au bout : c'était un élagage à la
  validation finale, possible avec les études lancées avant que le driver l'interdise (cas de
  l'étude de démo `20260930-075755-lr-weights-3e51`, essais 2 et 3) ; le compter comme complet
  dans l'analyse (il reste exclu de `study.json.best`).
- Meilleur essai au bord de l'espace ⇒ élargir l'espace.
- Multi-objectif : décrire le front (points extrêmes, point respectant la contrainte).
- Avant d'adopter : relancer les 2–3 meilleurs en pleine fidélité, 2–3 seeds.

## 10. Checkpoints non rapatriés, inventaire des machines (mode équipe)

**Symptômes** : `wmlab eval` ou `init_from` échoue (« checkpoint introuvable ») alors que
`checkpoints/index.json` le liste ; `Checkpoint.available_locally: false` dans l'app ; job
avec `retrieval.status: partial|failed` (avertissement) ; carte de la machine avec un badge
« N checkpoints (X Go) non rapatriés ».

**Vérifications** : `.wmlab/jobs/<job_id>.json` → `retrieval` (`checkpoints`, `bytes`,
`missing`) ; `GET /api/hosts/{name}/inventory` (ou `.wmlab/hosts_inventory.json`) →
`unretrieved_checkpoints`, `unretrieved_bytes`, `unindexed_runs`, `active_jobs`,
`reachable` ; taille des `.pt` côté machine vs serveur (`size_bytes` de l'index).

**Causes** : job interrompu avant la synchronisation finale (`killed`, `lost`, machine
éteinte) ; disque du serveur plein ; connexion SSH coupée pendant le rsync ; run lancé à la
main sur la machine (jamais synchronisé par l'app, `indexed: false`).

**Remèdes** : « Rapatrier les checkpoints » (job `retrieve` : tout ce qui est sur la machine,
vérification des tailles, réindexation), puis relire l'inventaire ; libérer de l'espace sur
le serveur si `partial` persiste ; **ne proposer « Retirer la machine » qu'avec un inventaire
à zéro** (le serveur refuse sinon, 409) ; machine déjà détruite : les checkpoints sont perdus,
le noter dans `notes.md` du run et proposer de relancer depuis le dernier checkpoint
rapatrié (`init_from` + `resume: true` si c'est `last`).

## 11. Extraits de code utiles

Résumé d'un run par phase (dernière valeur de validation de chaque phase) :

```python
from wmlab.paths import runs_dir
from wmlab.tracking import read_jsonl

rows = read_jsonl(runs_dir() / "<run_id>" / "metrics.jsonl")
last = {}
for row in rows:
    if "val/bit_acc_mean" in row["values"]:
        last[row["phase"]] = (row["step"], row["values"])
for phase, (step, v) in last.items():
    print(phase, step, {k: round(v[k], 4) for k in v if k.startswith("val/")})
```

Utilisation GPU et attente des données :

```python
from statistics import mean
from wmlab.tracking import RunReader
r = RunReader(runs_dir() / "<run_id>")
gpu = [x for x in r.system()["series"].get("gpu_util", []) if x is not None]
wait = r.metrics(["train/data_wait_frac"]).get("train/data_wait_frac", {}).get("values", [])
print(f"GPU {mean(gpu):.0f} %" if gpu else "pas de GPU mesuré",
      f"· attente données {mean(wait):.0%}" if wait else "")
```

Pire attaque au benchmark :

```python
for ev in r.evals():
    worst = min(ev["robustness"].items(), key=lambda kv: kv[1]["bit_acc"])
    print(ev["checkpoint"], ev["benchmark"], ev["summary"].get("bit_acc_mean"), "pire :", worst)
```

Rapatriement des jobs et inventaire des machines (mode équipe) :

```python
import json
from wmlab.paths import data_root
jobs = sorted((data_root() / "jobs").glob("*.json"))
for p in jobs[-20:]:
    j = json.loads(p.read_text())
    r = j.get("retrieval") or {}
    if r and r.get("status") not in (None, "complete", "n/a"):
        print(j["job_id"], j.get("user"), j["host"], r["status"], r.get("checkpoints"), r.get("missing"))
inv = data_root() / "hosts_inventory.json"
if inv.exists():
    for name, h in json.loads(inv.read_text()).items():
        print(name, "non rapatriés :", h.get("unretrieved_checkpoints"), h.get("unretrieved_bytes"),
              "runs non indexés :", h.get("unindexed_runs"), "jobs actifs :", h.get("active_jobs"))
```
(`hosts_inventory.json` = un objet `HostInventory` par nom de machine, `wmlab.server.inventory.InventoryCache` ;
l'API `GET /api/hosts/{name}/inventory` recalcule et fait foi.)
