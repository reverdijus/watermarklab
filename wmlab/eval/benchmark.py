"""Benchmark evaluation: quality without attack, robustness per attack (§6.3, §8.2)."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import torch

from wmlab import paths
from wmlab.ids import utc_iso
from wmlab.spec import BenchmarkSpec, load_benchmark
from wmlab.train.runtime import call_quality_metric, limited_split, make_loader, resolve_device

SMOKE_ITEMS = 8


def tpr_key(fpr: float) -> str:
    """Key of a TPR value, e.g. ``tpr@0.01`` or ``tpr@1e-06``."""
    return f"tpr@{fpr:g}"


def metric_context(modality: str, sample_rate: int | None = None, fps: int | None = None
                   ) -> dict[str, Any]:
    """Keyword context passed to quality metrics (``sample_rate`` for audio, ``fps`` for video)."""
    if modality == "audio":
        return {"sample_rate": int(sample_rate or 16000)}
    if modality == "video":
        return {"fps": int(fps or 24)}
    return {}


def _mean(values: list[float]) -> float | None:
    finite = [v for v in values if v is not None and math.isfinite(v)]
    return sum(finite) / len(finite) if finite else None


def run_benchmark(model: Any, bench: BenchmarkSpec, *, device: Any, nbits: int,
                  limit: int | None = None, sample_rate: int | None = None,
                  fps: int | None = None) -> dict[str, Any]:
    """Evaluate ``model`` on ``bench``; return an EvalResult without run/checkpoint ids.

    Messages are drawn with a generator seeded by ``bench.seed``; attacks run at
    ``strength=1`` with the global RNG temporarily seeded by ``bench.seed``.
    """
    from wmlab.attacks import apply_attack, configure_attack
    from wmlab.metrics import default_quality_metric
    from wmlab.metrics.robustness import bit_accuracy, detection_threshold, tpr_at_fpr
    from wmlab.registry import ATTACKS, METRICS

    start = time.time()
    # a FPR below 2**-nbits cannot be reached even with every bit correct: its TPR would be a
    # structural 0, misleading in the tables — the key is left out (see spec.validate)
    fprs = [f for f in bench.fpr if detection_threshold(int(nbits), f) <= int(nbits)]
    for fpr in bench.fpr:
        if fpr not in fprs:
            print(f"Avertissement : {tpr_key(fpr)} omis — FPR {fpr:g} inatteignable avec "
                  f"{nbits} bits (2^-{nbits} = {2.0 ** -int(nbits):.2g})", flush=True)
    device = device if isinstance(device, torch.device) else resolve_device(device)
    modality = bench.modality
    loader = make_loader(limited_split(bench.data, limit), modality,
                         batch_size=bench.batch_size, shuffle=False, num_workers=0,
                         drop_last=False, seed=bench.seed)
    n_items = len(loader.dataset)
    metric_names = list(dict.fromkeys(bench.quality_metrics)) or [default_quality_metric(modality)]
    metric_fns = {name: METRICS.get(name).obj for name in metric_names}
    attacks: list[tuple[str, str, Any]] = []
    for entry in bench.attacks:
        if not ATTACKS.get(entry.name).supports(modality):
            raise ValueError(f"l'attaque '{entry.name}' du benchmark {bench.name} ne supporte "
                             f"pas la modalité {modality}")
        attack = ATTACKS.build(entry.name, **entry.params)
        configure_attack(attack, sample_rate=sample_rate, fps=fps, params=entry.params)
        if hasattr(attack, "to"):
            attack = attack.to(device)
        attacks.append((entry.label, entry.name, attack))
    ctx = metric_context(modality, sample_rate, fps)

    sums = {name: 0.0 for name in metric_names}
    counts = {name: 0 for name in metric_names}
    logits_by_label: dict[str, list[torch.Tensor]] = {label: [] for label, _, _ in attacks}
    messages: list[torch.Tensor] = []
    generator = torch.Generator().manual_seed(int(bench.seed))
    was_training = bool(getattr(model, "training", False))
    model.eval()
    try:
        with torch.no_grad(), torch.random.fork_rng(devices=[]):
            torch.manual_seed(int(bench.seed))
            for batch in loader:
                x = batch["x"].to(device).float()
                msg = torch.randint(0, 2, (x.shape[0], nbits), generator=generator).float()
                x_w = model.embed(x, msg.to(device)).float()
                for name, fn in metric_fns.items():
                    values = call_quality_metric(fn, x, x_w, ctx)
                    finite = values[torch.isfinite(values)]
                    sums[name] += float(finite.sum())
                    counts[name] += int(finite.numel())
                for label, _, attack in attacks:
                    x_att = apply_attack(attack, x_w, 1.0)
                    logits_by_label[label].append(model.extract(x_att).float().cpu())
                messages.append(msg)
    finally:
        model.train(was_training)

    msg_all = torch.cat(messages)
    robustness: dict[str, dict[str, float]] = {}
    for label, _, _ in attacks:
        logits = torch.cat(logits_by_label[label])
        row = {"bit_acc": float(bit_accuracy(logits, msg_all).float().mean())}
        for fpr in fprs:
            row[tpr_key(fpr)] = float(tpr_at_fpr(logits, msg_all, fpr))
        robustness[label] = row
    quality = {name: sums[name] / counts[name] for name in metric_names if counts[name]}

    summary: dict[str, float] = dict(quality)
    scored = [label for label, name, _ in attacks if name != "identity"] \
        or [label for label, _, _ in attacks]
    bit_acc_mean = _mean([robustness[label]["bit_acc"] for label in scored])
    if bit_acc_mean is not None:
        summary["bit_acc_mean"] = bit_acc_mean
    for fpr in fprs:
        value = _mean([robustness[label][tpr_key(fpr)] for label in scored])
        if value is not None:
            summary[f"{tpr_key(fpr)}_mean"] = value
    return {
        "benchmark": bench.name,
        "benchmark_version": bench.version,
        "duration_s": round(time.time() - start, 3),
        "host": paths.current_host(),
        "price_eur_per_hour": paths.current_price(),
        "nbits": int(nbits),
        "n_items": n_items,
        "quality": quality,
        "robustness": robustness,
        "summary": summary,
    }


def with_ids(result: dict[str, Any], *, run_id: str, checkpoint: str,
             in_run: bool = False) -> dict[str, Any]:
    """Return a full EvalResult (ids and ``created_at`` first, contract key order).

    ``in_run`` marks the trainer's final benchmark: its GPU time is inside the run duration
    and is not billed again by the cost accounting.
    """
    head = {"run_id": run_id, "checkpoint": checkpoint, "benchmark": result.get("benchmark"),
            "benchmark_version": result.get("benchmark_version"), "created_at": utc_iso()}
    return {**head, **{k: v for k, v in result.items() if k not in head}, "in_run": bool(in_run)}


def resolve_run_dir(run_id_or_dir: str | Path) -> Path:
    """Resolve a run id, a unique id fragment or a run folder path."""
    from wmlab.tracking import list_runs

    text = str(run_id_or_dir).strip()
    candidate = Path(text).expanduser()
    if (candidate / "run.json").is_file():
        return candidate.resolve()
    base = paths.runs_dir()
    if text and (base / text / "run.json").is_file():
        return base / text
    matches = [p for p in list_runs(base) if text and text in p.name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise FileNotFoundError(f"run introuvable : '{text}' (dossier {base})")
    raise FileNotFoundError(f"identifiant de run ambigu '{text}' : "
                            f"{', '.join(p.name for p in matches[:10])}")


def evaluate_checkpoint(run_id_or_dir: str, checkpoint: str, benchmark: str, *,
                        device: str | None = None, smoke: bool = False) -> dict[str, Any]:
    """Evaluate a checkpoint of a run on a benchmark; write ``eval/<ckpt>__<bench>.json``.

    The run folder is not reopened for writing: the result file is written atomically and
    an ``eval`` event is appended to ``events.jsonl``.
    """
    from wmlab.models import load_model_from_checkpoint
    from wmlab.registry import load_plugins
    from wmlab.tracking import RunReader, log_eval_to, resolve_checkpoint

    load_plugins()
    run_dir = resolve_run_dir(run_id_or_dir)
    reader = RunReader(run_dir)
    manifest = reader.raw_manifest()
    entry = resolve_checkpoint(run_dir, checkpoint)
    if not entry.get("available_locally"):
        raise FileNotFoundError(f"le checkpoint {entry.get('name')} du run {run_dir.name} n'est "
                                "pas disponible localement : rapatriez-le d'abord (bouton "
                                "« Récupérer » ou synchronisation)")
    bench = load_benchmark(benchmark)
    modality = manifest.get("modality")
    if modality and modality != bench.modality:
        raise ValueError(f"modalité incompatible : le run {run_dir.name} est « {modality} » et le "
                         f"benchmark {bench.name} est « {bench.modality} »")
    torch_device = resolve_device(device)
    model = load_model_from_checkpoint(entry["path"], device=str(torch_device))
    model = model.to(torch_device)
    nbits = int(getattr(model, "nbits", 0) or 0)
    if nbits <= 0:
        raise ValueError(f"impossible de déterminer nbits pour le checkpoint {entry.get('name')} "
                         "(attribut model.nbits absent)")
    data = reader.spec().get("data") or {}
    result = run_benchmark(model, bench, device=torch_device, nbits=nbits,
                           limit=SMOKE_ITEMS if smoke else None,
                           sample_rate=data.get("sample_rate"), fps=data.get("fps"))
    result = with_ids(result, run_id=run_dir.name, checkpoint=entry["name"])
    if smoke:
        result["smoke"] = True
    log_eval_to(run_dir, result)
    return result
