"""Curriculum trainer (ARCHITECTURE.md §8.1).

Step convention: ``step`` in logs, checkpoints and events is the number of completed
optimisation steps. The step logged as ``k`` was computed with the curriculum values of
the global step index ``k - 1`` (e.g. ``train/lr`` at ``k`` = ``optimizer.lr × lr_factor(k-1)``).
"""

from __future__ import annotations

import copy
import json
import math
import random
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import torch

from wmlab import paths
from wmlab.attacks.base import configure_attack
from wmlab.curriculum import PhaseCursor, PhasePlan
from wmlab.models import external_weights_of, load_checkpoint_weights
from wmlab.registry import ATTACKS, LOSSES, METRICS, MODELS, load_plugins
from wmlab.spec import (
    ExperimentSpec,
    PhaseSpec,
    ProjectSpec,
    load_benchmark,
    load_experiment,
    validate_data,
)
from wmlab.spec.loader import resolve_in_workspace
from wmlab.tracking import (
    CHECKPOINT_FORMAT,
    CheckpointNotFoundError,
    Run,
    RunTerminated,
    flatten_params,
    read_json,
    resolve_checkpoint,
)
from wmlab.train import runtime
from wmlab.train.smoke import SMOKE_BENCH_ITEMS, apply_smoke

LOG_EVERY = 20
MAX_NONFINITE_STEPS = 10
EVAL_PHASE = "eval"  # phase label of an evaluation-only experiment (``phases: []``)
TrialCallback = Callable[[int, dict[str, float]], None]


# -- helpers --------------------------------------------------------------------------------
def _relative_spec_path(path: Path) -> str:
    """Spec path relative to the workspace (absolute when outside)."""
    try:
        return path.resolve().relative_to(paths.workspace_root().resolve()).as_posix()
    except Exception:
        return str(path)


def _read_launch_file(launch_file: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(launch info, warning)``."""
    if not launch_file:
        return None, None
    data = read_json(Path(launch_file).expanduser())
    if not isinstance(data, dict):
        return None, f"fichier de lancement absent ou illisible : {launch_file}"
    return data, None


def _parent_run_dir(ref: str) -> Path:
    candidate = Path(ref).expanduser()
    if (candidate / "checkpoints").is_dir():
        return candidate.resolve()
    run_dir = paths.runs_dir() / ref
    if not (run_dir / "checkpoints").is_dir() and not (run_dir / "run.json").is_file():
        raise FileNotFoundError(f"init_from : run parent introuvable '{ref}' (dossier {run_dir})")
    return run_dir


def _start_phase_index(plan: PhasePlan, start_phase: str | int | None) -> int:
    if start_phase is None:
        return 0
    if isinstance(start_phase, str) and start_phase.strip().isdigit() \
            and all(p.name != start_phase for p in plan.phases):
        start_phase = int(start_phase)
    return plan.phase_index(start_phase)


def _quiet_dataloader_workers() -> None:
    """Stop torch's SIGCHLD handler from raising while an interrupted run shuts down.

    Loader workers ignore SIGTERM (see ``wmlab.data``), but one killed anyway (e.g. before its
    init) would otherwise turn the emergency checkpoint into a ``RuntimeError``.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    except (ValueError, OSError, AttributeError):
        pass


def _is_pruned(exc: BaseException) -> bool:
    optuna = sys.modules.get("optuna")
    return optuna is not None and isinstance(exc, optuna.TrialPruned)


def _mean(values: list[torch.Tensor]) -> float | None:
    if not values:
        return None
    return float(torch.stack([v.detach().float().reshape(()) for v in values]).mean().cpu())


def _fmt(value: float | None, spec: str = ".4g") -> str:
    return "—" if value is None or not math.isfinite(value) else format(value, spec)


@dataclass
class _Window:
    """Accumulator of training statistics between two ``train/*`` logs."""

    steps: int = 0
    items: int = 0
    time_s: float = 0.0
    data_s: float = 0.0
    loss: list[torch.Tensor] = field(default_factory=list)
    terms: dict[str, list[torch.Tensor]] = field(default_factory=dict)
    bit_acc: list[torch.Tensor] = field(default_factory=list)
    grad_norm: list[torch.Tensor] = field(default_factory=list)

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]


@dataclass
class _InitFrom:
    entry: dict[str, Any]
    load_optimizer: bool
    resume: bool = False


# -- trainer --------------------------------------------------------------------------------
class Trainer:
    """Runs the phase plan of one experiment inside an open :class:`Run`."""

    def __init__(self, spec: ExperimentSpec, project: ProjectSpec, resolved: dict[str, Any],
                 run: Run, *, device: torch.device, smoke: bool = False,
                 max_hours: float | None = None,
                 trial_callback: TrialCallback | None = None) -> None:
        self.spec = spec
        self.project = project
        self.resolved = resolved
        self.run = run
        self.device = device
        self.smoke = smoke
        self.modality = project.modality
        self.nbits = int(project.message_bits)
        self.plan = PhasePlan(spec)
        self.deadline = time.time() + float(max_hours) * 3600.0 if max_hours else None
        self.trial_callback = trial_callback
        self.base_lr = float(spec.optimizer.lr)
        self.dtype: torch.dtype | None = None
        self.step = 0
        self.last_val: dict[str, float] = {}
        self.last_val_step: int | None = None
        self.best_val: dict[str, float] = {}
        self.scaler: Any = None
        self.saved_steps: set[int] = set()
        self.sampled_steps: set[int] = set()
        self.kill_reason: str | None = None
        self.msg_gen = torch.Generator().manual_seed(int(spec.seed))
        self.attack_rng = random.Random(int(spec.seed))
        self._attacks: dict[str, Any] = {}
        self._loader_key: tuple[str, int] | None = None
        self._train_loader: Any = None
        self._train_iter: Any = None
        self._window = _Window()
        self._warned: set[str] = set()
        self._nonfinite = 0
        self._phase_index: int | None = None
        self.model: Any = None
        self.optimizer: torch.optim.Optimizer | None = None
        self.losses: dict[str, Any] = {}

    # -- setup ---------------------------------------------------------------------------
    def reseed(self, step: int) -> None:
        """Move the message / attack RNGs to a stream specific to ``step`` (resume: the
        exact draws of the parent cannot be replayed, but the run stays deterministic)."""
        seed = int(self.spec.seed) + 1_000_003 * int(step)
        self.msg_gen = torch.Generator().manual_seed(seed)
        self.attack_rng = random.Random(seed)
        runtime.seed_everything(seed)

    def warn(self, message: str, key: str | None = None) -> None:
        """Record a warning event (once per key) and print it."""
        key = key or message
        if key in self._warned:
            return
        self._warned.add(key)
        self.run.event("warning", message=message)
        print(f"Avertissement : {message}", flush=True)

    def setup(self, init: _InitFrom | None = None) -> None:
        """Build model, losses, optimizer, data and validation attacks."""
        from wmlab.data import dataset_version
        from wmlab.metrics import default_quality_metric

        spec = self.spec
        runtime.seed_everything(int(spec.seed))
        self.dtype, warning = runtime.resolve_precision(spec.resources.precision, self.device)
        if warning:
            self.warn(warning)
        self.scaler = runtime.make_grad_scaler(self.device, self.dtype)
        if self.scaler is not None:
            print("Précision fp16 : gradients mis à l'échelle (GradScaler)", flush=True)
        entry = MODELS.get(spec.model.name)
        if not entry.supports(self.modality):
            raise ValueError(f"le modèle '{spec.model.name}' ne supporte pas la modalité "
                             f"{self.modality} (modalités : {entry.modality_label()})")
        self.model = MODELS.build(spec.model.name, nbits=self.nbits, **spec.model.params)
        self.model = self.model.to(self.device)
        if spec.resources.compile:
            self._compile()
        self.losses = {}
        for term, term_spec in spec.loss.terms.items():
            loss = LOSSES.build(term_spec.name, **term_spec.params)
            self.losses[term] = loss.to(self.device) if hasattr(loss, "to") else loss
        # evaluation-only experiment (phases: []): nothing is optimised
        self.optimizer = self._build_optimizer() if self.plan.total_steps else None
        if init is not None:
            self._load_init(init)

        self.val_loader = runtime.make_loader(
            spec.data.val, self.modality, batch_size=spec.data.batch_size, shuffle=False,
            num_workers=spec.data.num_workers, drop_last=False, seed=int(spec.seed))
        self.val_dataset = self.val_loader.dataset
        versions = {"train": dataset_version(spec.data.train), "val": dataset_version(spec.data.val)}
        for phase in spec.phases:
            if phase.data is not None:
                versions[f"train:{phase.name}"] = dataset_version(phase.data.train)
        self.run.set_datasets(versions)

        self.val_attacks = [(va.label, va.name, self._attack(va.name, va.params))
                            for va in spec.eval.val_attacks]
        if not any(name != "identity" for _, name, _ in self.val_attacks):
            self.warn("eval.val_attacks ne contient aucune attaque : val/bit_acc_mean vaudra "
                      "val/bit_acc (précision sans attaque), ce n'est pas une mesure de "
                      "robustesse")
        self.quality_metrics = [default_quality_metric(self.modality)]
        if self.modality in ("image", "video") and "ssim" not in self.quality_metrics:
            self.quality_metrics.append("ssim")
        self.metric_ctx = self._metric_ctx()
        ckpt = spec.checkpoint
        self.run.set_checkpoint_policy(ckpt.keep_last, dict(ckpt.keep_best),
                                       keep_phase_end=ckpt.at_phase_end)
        n_params = sum(p.numel() for p in self.model.parameters())
        print(f"Modèle {spec.model.name} : {n_params / 1e6:.3f} M paramètres — val : "
              f"{len(self.val_dataset)} items", flush=True)

    def _metric_ctx(self) -> dict[str, Any]:
        from wmlab.eval.benchmark import metric_context

        return metric_context(self.modality, self.spec.data.sample_rate, self.spec.data.fps)

    def _compile(self) -> None:
        try:
            self.model.embed = torch.compile(self.model.embed)
            self.model.extract = torch.compile(self.model.extract)
        except Exception as exc:
            self.warn(f"torch.compile indisponible ({exc}) : exécution sans compilation")

    def _param_groups(self) -> list[dict[str, Any]]:
        params = list(self.model.parameters())
        groups_fn = getattr(self.model, "param_groups", None)
        raw = None
        if callable(groups_fn):
            try:
                raw = groups_fn()
            except Exception as exc:
                self.warn(f"model.param_groups() a échoué ({exc}) : un seul groupe utilisé")
        if isinstance(raw, dict) and raw:
            seen: set[int] = set()
            groups: list[dict[str, Any]] = []
            for name, group in raw.items():
                unique = [p for p in group if id(p) not in seen]
                seen.update(id(p) for p in unique)
                if unique:
                    groups.append({"params": unique, "name": str(name)})
            rest = [p for p in params if id(p) not in seen]
            if rest:
                groups.append({"params": rest, "name": "rest"})
            return groups
        return [{"params": params, "name": "all"}]

    def _build_optimizer(self) -> torch.optim.Optimizer:
        opt = self.spec.optimizer
        groups = self._param_groups()
        if not any(g["params"] for g in groups):
            raise ValueError(f"le modèle '{self.spec.model.name}' n'a aucun paramètre entraînable")
        name = opt.name.lower()
        betas = (float(opt.betas[0]), float(opt.betas[1]))
        if name == "adamw":
            return torch.optim.AdamW(groups, lr=opt.lr, betas=betas, weight_decay=opt.weight_decay)
        if name == "adam":
            return torch.optim.Adam(groups, lr=opt.lr, betas=betas, weight_decay=opt.weight_decay)
        if name == "sgd":
            return torch.optim.SGD(groups, lr=opt.lr, momentum=betas[0],
                                   weight_decay=opt.weight_decay)
        raise ValueError(f"optimiseur inconnu '{opt.name}' — valeurs possibles : adamw, adam, sgd")

    def _load_init(self, init: _InitFrom) -> None:
        payload = runtime.load_checkpoint_payload(init.entry["path"])
        for key, expected in (("modality", self.modality), ("nbits", self.nbits)):
            found = payload.get(key)
            if found is not None and found != expected:
                raise ValueError(f"init_from : checkpoint incompatible ({key} = {found}, "
                                 f"attendu {expected})")
        try:
            load_checkpoint_weights(self.model, payload)
        except RuntimeError as exc:
            raise ValueError(f"init_from : les poids du checkpoint {init.entry.get('name')} ne "
                             f"correspondent pas au modèle '{self.spec.model.name}' : {exc}") from None
        if init.load_optimizer:
            if payload.get("optimizer"):
                try:
                    self.optimizer.load_state_dict(payload["optimizer"])
                except (ValueError, KeyError) as exc:
                    self.warn(f"init_from : état de l'optimiseur non chargé ({exc})")
            else:
                self.warn("init_from : le checkpoint ne contient pas d'état d'optimiseur")
        print(f"Poids chargés depuis {init.entry.get('name')} ({init.entry.get('path')})",
              flush=True)

    def _attack(self, name: str, params: dict[str, Any]) -> Any:
        key = f"{name}|{json.dumps(params, sort_keys=True, default=str)}"
        attack = self._attacks.get(key)
        if attack is None:
            entry = ATTACKS.get(name)
            if not entry.supports(self.modality):
                raise ValueError(f"l'attaque '{name}' ne supporte pas la modalité {self.modality} "
                                 f"(modalités : {entry.modality_label()})")
            attack = ATTACKS.build(name, **params)
            configure_attack(attack, sample_rate=self.spec.data.sample_rate,
                             fps=self.spec.data.fps, params=params)
            if hasattr(attack, "to"):
                attack = attack.to(self.device)
            self._attacks[key] = attack
        return attack

    # -- data ----------------------------------------------------------------------------
    def _ensure_loader(self, phase: PhaseSpec) -> None:
        """(Re)build the training loader when the phase changes its data or batch size."""
        split = phase.data.train if phase.data is not None else self.spec.data.train
        batch_size = int(phase.batch_size or self.spec.data.batch_size)
        key = (runtime.split_key(split), batch_size)
        if key == self._loader_key:
            return
        loader = runtime.make_loader(split, self.modality, batch_size=batch_size, shuffle=True,
                                     num_workers=self.spec.data.num_workers, drop_last=True,
                                     seed=int(self.spec.seed))
        self.close_loaders(train_only=True)
        self._train_loader = loader
        self._train_iter = runtime.infinite_batches(loader)
        self._loader_key = key
        try:
            size = f"{len(loader.dataset)} items"
        except TypeError:
            size = "taille inconnue"
        print(f"Loader d'entraînement : {split.dataset} ({size}), batch_size={batch_size}",
              flush=True)

    def close_loaders(self, train_only: bool = False) -> None:
        """Stop the loader worker processes now (they would otherwise linger until exit)."""
        from wmlab.data import shutdown_loader

        self._train_iter = None
        if self._train_loader is not None:
            shutdown_loader(self._train_loader)
            self._train_loader = None
        if not train_only and getattr(self, "val_loader", None) is not None:
            shutdown_loader(self.val_loader)

    # -- phases --------------------------------------------------------------------------
    def _apply_freeze(self, phase: PhaseSpec) -> None:
        prefixes = list(phase.freeze)
        matched: set[str] = set()
        trainable = 0
        for name, param in self.model.named_parameters():
            hits = [p for p in prefixes if name.startswith(p)]
            matched.update(hits)
            param.requires_grad_(not hits)
            trainable += 0 if hits else param.numel()
        unmatched = [p for p in prefixes if p not in matched]
        if unmatched:
            self.warn(f"phase {phase.name} : aucun paramètre ne correspond aux préfixes de gel "
                      f"{', '.join(unmatched)}")
        if trainable == 0:
            raise ValueError(f"phase {phase.name} : tous les paramètres sont gelés (freeze)")

    def _enter_phase(self, cur: PhaseCursor) -> None:
        phase = cur.phase
        self._apply_freeze(phase)
        if phase.reset_optimizer:
            self.optimizer = self._build_optimizer()
        self._ensure_loader(phase)
        self._phase_index = cur.index
        self.run.event("phase_start", phase=phase.name, index=cur.index, step=self.step)
        self.run.set_progress(step=self.step, total_steps=self.plan.total_steps, phase=phase.name,
                              phase_index=cur.index, n_phases=len(self.plan))
        attacks = ", ".join(a.label or a.name for a in phase.attacks) or "aucune"
        print(f"== Phase {cur.index + 1}/{len(self.plan)} « {phase.name} » : steps "
              f"{cur.start}→{cur.end} ({phase.steps}) — attaques : {attacks}", flush=True)

    # -- training step -------------------------------------------------------------------
    def _draw_attack(self, phase: PhaseSpec) -> tuple[str, Any] | None:
        entries = [a for a in phase.attacks if a.p > 0]
        if not entries:
            return None
        entry = self.attack_rng.choices(entries, weights=[a.p for a in entries])[0]
        return entry.label or entry.name, self._attack(entry.name, entry.params)

    def _extras(self, **more: Any) -> dict[str, Any]:
        extras: dict[str, Any] = dict(self.metric_ctx)
        extras.update(more)
        return extras

    def _train_step(self, cur: PhaseCursor, index: int) -> dict[str, Any] | None:
        """One optimisation step at global step index ``index``; None if skipped."""
        from wmlab.attacks import apply_attack, sample_strength
        from wmlab.losses.base import StepBatch
        from wmlab.metrics.robustness import bit_accuracy

        phase = cur.phase
        t0 = time.perf_counter()
        batch = next(self._train_iter)
        x = batch["x"].to(self.device, non_blocking=True).float()
        data_s = time.perf_counter() - t0
        msg = torch.randint(0, 2, (x.shape[0], self.nbits), generator=self.msg_gen).float()
        msg = msg.to(self.device)
        weights = self.plan.loss_weights(index)
        lr = self.base_lr * self.plan.lr_factor(index)
        for group in self.optimizer.param_groups:
            group["lr"] = lr
        drawn = self._draw_attack(phase)
        strength = 0.0
        if drawn is not None:
            strength = float(sample_strength(self.plan.attack_strength(index),
                                             phase.random_strength, self.attack_rng))
        with runtime.autocast(self.device, self.dtype):
            x_w = self.model.embed(x, msg)
            x_att = apply_attack(drawn[1], x_w, strength) if drawn is not None else x_w
            logits = self.model.extract(x_att)
            step_batch = StepBatch(
                x=x, x_w=x_w, x_att=x_att, msg=msg, logits=logits, modality=self.modality,
                model=self.model, step=index, phase=phase.name,
                extras=self._extras(attack=drawn[0] if drawn else "identity", strength=strength))
            terms = {t: fn(step_batch).float() for t, fn in self.losses.items()}
            aux_fn = getattr(self.model, "aux_losses", None)
            aux = dict(aux_fn(step_batch) or {}) if callable(aux_fn) else {}
        total = sum(weights[t] * v for t, v in terms.items())
        for value in aux.values():
            total = total + value.float()
        if not bool(torch.isfinite(total.detach())):
            self._nonfinite += 1
            self.warn(f"perte non finie (NaN/inf) au step {index + 1} : step ignoré",
                      key=f"nonfinite-{index}")
            if self._nonfinite >= MAX_NONFINITE_STEPS:
                raise FloatingPointError(f"perte non finie pendant {self._nonfinite} steps "
                                         "consécutifs : entraînement divergent (baisser le LR ?)")
            self.optimizer.zero_grad(set_to_none=True)
            return None
        self._nonfinite = 0
        self.optimizer.zero_grad(set_to_none=True)
        if self.scaler is not None:  # fp16: scaled loss, gradients unscaled before clipping
            self.scaler.scale(total).backward()
            self.scaler.unscale_(self.optimizer)
        else:
            total.backward()
        grad_norm = None
        clip = self.spec.optimizer.grad_clip
        trainable = [p for p in self.model.parameters() if p.requires_grad and p.grad is not None]
        if clip and trainable:
            grad_norm = torch.nn.utils.clip_grad_norm_(trainable, float(clip)).detach()
        if self.scaler is not None:
            self.scaler.step(self.optimizer)
            self.scaler.update()
        else:
            self.optimizer.step()
        aux_names = {f"aux_{k}" if k in terms else k: v for k, v in aux.items()}
        return {
            "loss": total.detach(),
            "terms": {**{t: v.detach() for t, v in terms.items()},
                      **{k: v.detach().float() for k, v in aux_names.items()}},
            "bit_acc": bit_accuracy(logits.detach().float(), msg).float().mean(),
            "grad_norm": grad_norm,
            "items": int(x.shape[0]),
            "data_s": data_s,
        }

    def _accumulate(self, stats: dict[str, Any] | None, step_s: float) -> None:
        w = self._window
        w.steps += 1
        w.time_s += step_s
        if stats is None:
            return
        w.items += stats["items"]
        w.data_s += stats["data_s"]
        w.loss.append(stats["loss"])
        for term, value in stats["terms"].items():
            w.terms.setdefault(term, []).append(value)
        w.bit_acc.append(stats["bit_acc"])
        if stats["grad_norm"] is not None:
            w.grad_norm.append(stats["grad_norm"])

    def _log_train(self, cur: PhaseCursor, index: int) -> None:
        w = self._window
        if w.steps == 0:
            return
        values: dict[str, float | None] = {"train/loss": _mean(w.loss)}
        for term, items in w.terms.items():
            values[f"train/loss/{term}"] = _mean(items)
        values["train/bit_acc"] = _mean(w.bit_acc)
        if w.grad_norm:
            values["train/grad_norm"] = _mean(w.grad_norm)
        values["train/lr"] = self.base_lr * self.plan.lr_factor(index)
        for term, weight in self.plan.loss_weights(index).items():
            values[f"train/weight/{term}"] = weight
        values["train/attack_strength"] = self.plan.attack_strength(index)
        values["train/step_time_s"] = w.time_s / w.steps
        values["train/data_wait_frac"] = w.data_s / w.time_s if w.time_s > 0 else 0.0
        values["train/items_per_s"] = w.items / w.time_s if w.time_s > 0 else 0.0
        clean = {k: v for k, v in values.items() if v is not None}
        self.run.log_metrics(self.step, clean, phase=cur.name)
        total = self.plan.total_steps
        print(f"[{cur.name} {cur.index + 1}/{len(self.plan)}] step {self.step}/{total} "
              f"({100.0 * self.step / total:.1f} %) loss={_fmt(values['train/loss'])} "
              f"bit_acc={_fmt(values['train/bit_acc'], '.3f')} "
              f"lr={_fmt(values['train/lr'], '.2e')} "
              f"strength={_fmt(values['train/attack_strength'], '.2f')} "
              f"{_fmt(values['train/items_per_s'], '.1f')} items/s", flush=True)
        w.reset()

    # -- validation, checkpoints, samples ------------------------------------------------
    @torch.no_grad()
    def validate(self, cur: PhaseCursor, index: int) -> dict[str, float]:
        """Validation without attack, then with each ``eval.val_attacks`` at strength 1.

        Messages and attack draws are seeded (``seed + 1``) inside a forked RNG state: every
        validation of a run (and of every trial of a study) sees exactly the same draws, so
        ``val/*`` values are comparable across steps and runs.
        """
        with runtime.fork_rng(self.device):
            torch.manual_seed(int(self.spec.seed) + 1)
            return self._validate(cur, index)

    def _validate(self, cur: PhaseCursor | None, index: int) -> dict[str, float]:
        from wmlab.attacks import apply_attack
        from wmlab.losses.base import StepBatch
        from wmlab.metrics.robustness import bit_accuracy

        self.model.eval()
        generator = torch.Generator().manual_seed(int(self.spec.seed) + 1)
        if cur is None:  # evaluation only: base weights of loss.terms
            weights = {t: float(s.weight) for t, s in self.spec.loss.terms.items()}
        else:
            weights = self.plan.loss_weights(index)
        phase_name = cur.name if cur is not None else EVAL_PHASE
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}

        def add(key: str, values: torch.Tensor) -> None:
            values = values.detach().float().reshape(-1).cpu()
            values = values[torch.isfinite(values)]
            sums[key] = sums.get(key, 0.0) + float(values.sum())
            counts[key] = counts.get(key, 0) + int(values.numel())

        try:
            for b, batch in enumerate(self.val_loader):
                if b >= self.spec.eval.val_batches:
                    break
                x = batch["x"].to(self.device).float()
                size = x.shape[0]
                msg = torch.randint(0, 2, (size, self.nbits), generator=generator).float()
                msg = msg.to(self.device)
                with runtime.autocast(self.device, self.dtype):
                    x_w = self.model.embed(x, msg)
                    logits = self.model.extract(x_w)
                    step_batch = StepBatch(x=x, x_w=x_w, x_att=x_w, msg=msg, logits=logits,
                                           modality=self.modality, model=self.model, step=index,
                                           phase=phase_name,
                                           extras=self._extras(attack="identity", strength=0.0))
                    loss = sum(weights[t] * fn(step_batch).float() for t, fn in self.losses.items())
                add("val/loss", torch.as_tensor(loss).float().expand(size))
                x_w = x_w.float()
                for name in list(self.quality_metrics):
                    try:
                        fn = METRICS.get(name).obj
                        add(f"val/{name}", runtime.call_quality_metric(fn, x, x_w, self.metric_ctx))
                    except Exception as exc:
                        self.quality_metrics.remove(name)
                        self.warn(f"métrique de validation '{name}' indisponible ({exc})")
                add("val/bit_acc", bit_accuracy(logits.float(), msg))
                for label, _, attack in self.val_attacks:
                    x_att = apply_attack(attack, x_w, 1.0)
                    add(f"val/bit_acc/{label}",
                        bit_accuracy(self.model.extract(x_att).float(), msg))
        finally:
            self.model.train()
        values = {k: sums[k] / counts[k] for k in sums if counts.get(k)}
        if "val/bit_acc" not in values:
            self.warn("validation impossible : le dataset de validation est vide")
            return {}
        # mean over the attacks, ``identity`` excluded (same rule as the benchmarks, §6.3)
        attack_keys = [f"val/bit_acc/{label}" for label, name, _ in self.val_attacks
                       if name != "identity"]
        attack_values = [values[k] for k in attack_keys if k in values]
        values["val/bit_acc_mean"] = (sum(attack_values) / len(attack_values) if attack_values
                                      else values["val/bit_acc"])
        self.run.log_metrics(self.step, values, phase=phase_name)
        self.last_val = dict(values)
        self.last_val_step = self.step
        self.run.update_summary({**values, "step": self.step})
        shown = " ".join(f"{k[4:]}={v:.4g}" for k, v in values.items()
                         if k.count("/") == 1)
        print(f"[{phase_name}] validation step {self.step} : {shown}", flush=True)
        return values

    def _checkpoint_state(self, phase: str) -> dict[str, Any]:
        state = {
            "format": CHECKPOINT_FORMAT,
            "model": runtime.to_cpu(self.model.state_dict()),
            "optimizer": runtime.to_cpu(self.optimizer.state_dict()) if self.optimizer else None,
            "step": int(self.step),
            "phase": phase,
            "spec": self.resolved,
            "model_name": self.spec.model.name,
            "model_params": copy.deepcopy(dict(self.spec.model.params)),
            "nbits": self.nbits,
            "modality": self.modality,
            "metrics": self.current_val_metrics(),
        }
        external = external_weights_of(self.model)
        if external:
            state["external_weights"] = external
            if not self.plan.total_steps:
                # reference checkpoint: the untouched official weights are not copied into
                # the run, the model constructor reloads them (sha256 checked at load time)
                state["model"] = {}
        return state

    def current_val_metrics(self) -> dict[str, float]:
        """Validation metrics measured at the current step (empty if none: a checkpoint never
        carries the metrics of another step, ``best:<metric>`` would point to wrong weights)."""
        return dict(self.last_val) if self.last_val_step == self.step else {}

    def improves_keep_best(self, values: dict[str, float]) -> list[str]:
        """Metrics of ``checkpoint.keep_best`` improved by this validation (tracked in place)."""
        improved: list[str] = []
        for metric, direction in self.spec.checkpoint.keep_best.items():
            value = values.get(metric)
            if value is None or not math.isfinite(value):
                continue
            best = self.best_val.get(metric)
            better = best is None or (value < best if direction == "min" else value > best)
            if better:
                self.best_val[metric] = value
                improved.append(metric)
        return improved

    def save_checkpoint(self, phase: str, tags: list[str] | None = None) -> str | None:
        """Save a checkpoint at the current step (skipped if already saved without new tags)."""
        if self.step in self.saved_steps and not tags:
            return None
        name = self.run.save_checkpoint(self._checkpoint_state(phase), step=self.step,
                                        phase=phase, metrics=self.current_val_metrics(),
                                        tags=tags or [])
        self.saved_steps.add(self.step)
        print(f"Checkpoint {name}" + (f" [{', '.join(tags)}]" if tags else ""), flush=True)
        return name

    def _current_phase_name(self) -> str:
        return self.plan.locate(max(self.step - 1, 0)).name

    def emergency_checkpoint(self) -> None:
        """Best-effort checkpoint when the process is interrupted (SIGTERM)."""
        if self.model is None or self.optimizer is None or self.step in self.saved_steps:
            return
        try:
            self.save_checkpoint(self._current_phase_name())
        except BaseException as exc:  # never mask the interruption
            print(f"Checkpoint d'urgence impossible : {exc}", flush=True)

    @torch.no_grad()
    def export_samples(self) -> None:
        """Write ``samples.count`` validation items: original, watermarked and amplified diff."""
        count = min(int(self.spec.samples.count), len(self.val_dataset))
        if count <= 0 or self.step in self.sampled_steps:
            return
        self.sampled_steps.add(self.step)
        self.model.eval()
        try:
            x = torch.stack([self.val_dataset[i]["x"] for i in range(count)]).to(self.device)
            x = x.float()
            generator = torch.Generator().manual_seed(int(self.spec.seed) + 2)
            msg = torch.randint(0, 2, (count, self.nbits), generator=generator).float()
            x_w = self.model.embed(x, msg.to(self.device)).float()
            x, x_w = x.cpu(), x_w.cpu()
            sr, fps = self.spec.data.sample_rate, self.spec.data.fps
            for i in range(count):
                name = str(i)
                if self.modality == "audio":
                    residual = ((x_w[i] - x[i]) * 10.0).clamp(-1.0, 1.0)
                    for group, data in (("original", x[i]), ("watermarked", x_w[i]),
                                        ("residual", residual)):
                        self.run.log_sample(self.step, "audio", name, data, group=group,
                                            sample_rate=sr)
                    continue
                diff = ((x_w[i] - x[i]).abs() * 10.0).clamp(0.0, 1.0)
                for group, data in (("original", x[i]), ("watermarked", x_w[i]), ("diff", diff)):
                    self.run.log_sample(self.step, self.modality, name, data, group=group,
                                        fps=fps if self.modality == "video" else None)
                if self.modality == "video":
                    self.run.log_sample(self.step, "image", name, x_w[i][x_w.shape[1] // 2],
                                        group="frame")
        except Exception as exc:
            self.warn(f"export des échantillons impossible au step {self.step} : {exc}",
                      key=f"samples-{self.step}")
        finally:
            self.model.train()

    # -- main loop -----------------------------------------------------------------------
    def fit(self, start_step: int = 0) -> None:
        """Train from global step ``start_step`` to the end of the plan (or the deadline)."""
        total = self.plan.total_steps
        self.step = int(start_step)
        self.model.train()
        eval_every = int(self.spec.eval.every_steps)
        ckpt = self.spec.checkpoint
        samples_every = self.spec.samples.every_steps
        while self.step < total:
            cur = self.plan.locate(self.step)
            if cur.index != self._phase_index:
                self._enter_phase(cur)
            index = self.step
            t0 = time.perf_counter()
            stats = self._train_step(cur, index)
            self._accumulate(stats, time.perf_counter() - t0)
            self.step += 1
            phase_end = self.step == cur.end
            if self.step % LOG_EVERY == 0 or phase_end:
                self._log_train(cur, index)
            improved: list[str] = []
            if self.step % eval_every == 0 or phase_end:
                values = self.validate(cur, index)
                improved = self.improves_keep_best(values)
                if self.trial_callback is not None and values:
                    self.trial_callback(self.step, dict(values))
            tags = [f"phase_end:{cur.name}"] if phase_end and ckpt.at_phase_end else []
            # a validation that improves a keep_best metric gets its own checkpoint (tagged
            # best:<metric> by the index): best:… always designates weights measured at
            # that step, whatever checkpoint.every_steps is
            if tags or improved or (ckpt.every_steps and self.step % ckpt.every_steps == 0):
                self.save_checkpoint(cur.name, tags)
            if samples_every and self.step % samples_every == 0:
                self.export_samples()
            if phase_end:
                self.run.event("phase_end", phase=cur.name, index=cur.index, step=self.step)
            self.run.set_progress(step=self.step, total_steps=total, phase=cur.name,
                                  phase_index=cur.index, n_phases=len(self.plan))
            if self.deadline is not None and time.time() >= self.deadline and self.step < total:
                self.kill_reason = "max_hours reached"
                print(f"Durée maximale atteinte au step {self.step} : arrêt du run", flush=True)
                return

    def evaluate_only(self) -> None:
        """Evaluation-only experiment (``phases: []``, pretrained baseline): validation at
        step 0, checkpoint ``step_000000``, samples and final benchmark — no training."""
        self.step = 0
        self.run.set_progress(step=0, total_steps=0, phase=EVAL_PHASE, phase_index=0,
                              n_phases=0)
        print("Évaluation seule (phases: []) : aucun entraînement", flush=True)
        self.validate(None, 0)
        self.save_checkpoint(EVAL_PHASE)
        self.export_samples()
        self.final_benchmark()

    def finalize(self) -> None:
        """Final checkpoint, samples and benchmark."""
        phase = self._current_phase_name()
        self.save_checkpoint(phase)
        self.export_samples()
        self.final_benchmark()

    def final_benchmark(self) -> None:
        """Evaluate the selected checkpoints on ``eval.final_benchmark`` (errors are logged)."""
        from wmlab.eval.benchmark import run_benchmark, with_ids

        final = self.spec.eval.final_benchmark
        if final is None:
            return
        try:
            bench = load_benchmark(final.benchmark)
        except Exception as exc:
            self._benchmark_error(final.benchmark, exc)
            return
        if bench.modality != self.modality:
            self._benchmark_error(bench.name, ValueError(
                f"modalité du benchmark ({bench.modality}) ≠ modalité du projet ({self.modality})"))
            return
        done: set[str] = set()
        last_name = None
        try:
            last_name = resolve_checkpoint(self.run.dir, "last").get("name")
        except CheckpointNotFoundError:
            pass
        for selector in final.checkpoints:
            try:
                entry = resolve_checkpoint(self.run.dir, selector)
                if entry["name"] in done:
                    continue
                done.add(entry["name"])
                model = self.model if entry["name"] == last_name else self._model_from(entry)
                print(f"Benchmark {bench.name} sur {entry['name']} ({selector})…", flush=True)
                result = run_benchmark(model, bench, device=self.device, nbits=self.nbits,
                                       limit=SMOKE_BENCH_ITEMS if self.smoke else None,
                                       sample_rate=self.spec.data.sample_rate,
                                       fps=self.spec.data.fps)
                result = with_ids(result, run_id=self.run.run_id, checkpoint=entry["name"],
                                  in_run=True)
                if self.smoke:
                    result["smoke"] = True
                self.run.log_eval(result)
                shown = " ".join(f"{k}={v:.4g}" for k, v in result["summary"].items())
                print(f"Benchmark {bench.name} [{entry['name']}] : {shown}", flush=True)
            except Exception as exc:
                self._benchmark_error(f"{bench.name} ({selector})", exc)
            finally:
                self.model.train()

    def _model_from(self, entry: dict[str, Any]) -> Any:
        model = copy.deepcopy(self.model)
        load_checkpoint_weights(model, runtime.load_checkpoint_payload(entry["path"]))
        return model.to(self.device)

    def _benchmark_error(self, what: str, exc: Exception) -> None:
        message = f"benchmark final {what} impossible : {exc}"
        self.run.event("error", message=message,
                       traceback=traceback.format_exc(limit=5)[-4000:])
        print(f"Erreur : {message}", flush=True)


# -- entry point ----------------------------------------------------------------------------
def _resolve_init(spec: ExperimentSpec, plan: PhasePlan
                  ) -> tuple[_InitFrom | None, dict[str, Any] | None, int]:
    """``(init, parent manifest entry, start step)`` of ``spec.init_from``.

    ``resume: true`` starts at the checkpoint's step (the phase is located in the plan,
    the optimizer state is loaded); otherwise the plan starts at ``start_phase``.
    """
    init = spec.init_from
    if init is None:
        return None, None, 0
    parent_dir = _parent_run_dir(init.run)
    entry = resolve_checkpoint(parent_dir, init.checkpoint)
    if not entry.get("available_locally"):
        raise FileNotFoundError(f"init_from : le checkpoint {entry.get('name')} du run "
                                f"{parent_dir.name} n'est pas disponible localement")
    parent = {"run_id": parent_dir.name, "checkpoint": entry["name"],
              "start_phase": init.start_phase, "selector": init.checkpoint,
              "resume": bool(init.resume)}
    if init.resume:
        step = int(entry.get("step") or 0)
        if step >= plan.total_steps:
            raise ValueError(f"init_from.resume : le checkpoint {entry['name']} est au step "
                             f"{step}, le plan n'en compte que {plan.total_steps} : rien à "
                             "reprendre (allongez une phase ou branchez sans resume)")
        parent["start_phase"] = plan.locate(step).name
        return _InitFrom(entry, True, resume=True), parent, step
    index = _start_phase_index(plan, init.start_phase)
    return _InitFrom(entry, init.load_optimizer), parent, plan.bounds[index][1]


def train_from_spec(spec_path: str | Path, *, overrides: dict[str, str] | None = None,
                    run_id: str | None = None, smoke: bool = False, device: str | None = None,
                    max_hours: float | None = None, launch_file: str | None = None,
                    study: str | None = None, trial: int | None = None, gpu_share: float = 1.0,
                    trial_callback: TrialCallback | None = None) -> dict[str, Any]:
    """Train an experiment and write its run folder; return the run summary.

    Spec errors are raised before the run folder is created; any later error marks the run
    ``failed`` (traceback in ``run.json.error``) and is re-raised. SIGTERM saves a checkpoint,
    marks the run ``killed`` and re-raises; ``max_hours`` does the same but returns normally.
    """
    report = load_plugins()
    path = resolve_in_workspace(spec_path)
    spec, project, resolved = load_experiment(path, overrides)
    if smoke:
        resolved = apply_smoke(resolved, project.modality)
        spec = validate_data(resolved, "experiment", path)
        resolved = spec.model_dump(mode="json")
    plan = PhasePlan(spec)
    torch_device = runtime.resolve_device(device or spec.resources.device)
    launch_info, launch_warning = _read_launch_file(launch_file)
    init, parent, start_step = _resolve_init(spec, plan)
    tags = list(spec.tags) + (["smoke"] if smoke and "smoke" not in spec.tags else [])
    run = Run.create(project=project.name, experiment=spec.name, modality=project.modality,
                     spec=resolved, spec_path=_relative_spec_path(path), run_id=run_id,
                     study=study, trial=trial, parent=parent,
                     params=flatten_params(resolved, overrides), overrides=overrides, tags=tags,
                     gpu_share=gpu_share, launch_info=launch_info)
    trainer: Trainer | None = None
    try:
        trainer = Trainer(spec, project, resolved, run, device=torch_device, smoke=smoke,
                          max_hours=max_hours, trial_callback=trial_callback)
        for error in report.errors:
            last = error["error"].strip().splitlines()[-1] if error["error"].strip() else ""
            trainer.warn(f"module {error['module']} non chargé : {last}")
        if launch_warning:
            trainer.warn(launch_warning)
        print(f"Run {run.run_id} — {project.name}/{spec.name} ({project.modality}, "
              f"{project.message_bits} bits) — device {torch_device}"
              + (" — mode smoke" if smoke else ""), flush=True)
        print(f"Dossier : {run.dir}", flush=True)
        trainer.setup(init)
        if init is not None and init.resume:
            trainer.reseed(start_step)
            print(f"Reprise de {parent['run_id']}:{parent['checkpoint']} au step {start_step} "
                  f"(phase « {parent['start_phase']} »)", flush=True)
        elif start_step:
            print(f"Branche depuis {parent['run_id']}:{parent['checkpoint']} — départ au step "
                  f"{start_step}", flush=True)
        if init is not None and not init.resume:
            ckpt_step = int(init.entry.get("step") or 0)
            if ckpt_step != start_step:
                trainer.warn(
                    f"init_from : le checkpoint {init.entry.get('name')} est au step "
                    f"{ckpt_step} du run parent mais le curriculum repart au step {start_step} "
                    f"(warmup du LR, rampes de poids et d'attaques rejoués depuis "
                    f"« {trainer.plan.locate(start_step).name} ») — pour reprendre le run là où "
                    "il s'est arrêté, utilisez init_from.resume: true")
        if not plan.total_steps:
            trainer.evaluate_only()
            run.finish("completed")
            print(f"Run {run.run_id} terminé ({run.manifest.get('status')})", flush=True)
            return dict(run.manifest.get("summary") or {})
        trainer.fit(start_step)
        if trainer.kill_reason:
            trainer.save_checkpoint(trainer._current_phase_name())
            run.finish("killed", trainer.kill_reason)
        else:
            trainer.finalize()
            run.finish("completed")
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            _quiet_dataloader_workers()
        if run.finished:
            raise
        if _is_pruned(exc):
            print(f"Essai élagué (pruning) au step {trainer.step if trainer else 0}", flush=True)
            run.finish("killed", "pruned")
        elif isinstance(exc, KeyboardInterrupt):
            if trainer is not None:
                trainer.emergency_checkpoint()
            run.finish("killed", "SIGTERM reçu" if isinstance(exc, RunTerminated)
                       else "interrompu (KeyboardInterrupt)")
        else:
            run.finish("failed", traceback.format_exc())
        raise
    finally:
        if trainer is not None:
            trainer.close_loaders()
    print(f"Run {run.run_id} terminé ({run.manifest.get('status')})", flush=True)
    return dict(run.manifest.get("summary") or {})
