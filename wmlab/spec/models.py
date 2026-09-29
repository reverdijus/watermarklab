"""Pydantic models of the declarative YAML files (ARCHITECTURE.md §5)."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    field_validator,
    model_validator,
)

Modality = Literal["image", "audio", "video"]
Direction = Literal["maximize", "minimize"]
MODALITIES: tuple[str, ...] = ("image", "audio", "video")

NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
DEFAULT_QUALITY_METRIC: dict[str, str] = {"image": "psnr", "audio": "snr", "video": "psnr"}
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
CONSTRAINT_RE = re.compile(rf"^\s*(>=|<=|==|>|<)\s*({_NUMBER})\s*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

Name = Annotated[str, Field(pattern=NAME_PATTERN)]


class StrictModel(BaseModel):
    """Base model: unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid")


def _str_list(value: Any) -> Any:
    """Coerce scalar list items (e.g. YAML numbers) to strings."""
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        value = [value]
    if isinstance(value, (list, tuple)):
        return [str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                for v in value]
    return value


def _date_str(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


# -- scheduled values (§5.1) ----------------------------------------------------------------
class RampValue(StrictModel):
    """Interpolation from ``ramp[0]`` to ``ramp[1]`` between phase fractions start/end."""

    ramp: tuple[float, float]
    shape: Literal["linear", "cosine", "exp"] = "linear"
    start: float = Field(0.0, ge=0.0, le=1.0)
    end: float = Field(1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "RampValue":
        if self.start > self.end:
            raise ValueError("'start' doit être inférieur ou égal à 'end'")
        return self


class PiecewiseValue(StrictModel):
    """Piecewise-linear interpolation over ``[[frac, value], ...]``."""

    piecewise: list[tuple[float, float]] = Field(min_length=1)

    @field_validator("piecewise")
    @classmethod
    def _check_points(cls, points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        fracs = [p[0] for p in points]
        if any(f < 0.0 or f > 1.0 for f in fracs):
            raise ValueError("les fractions de 'piecewise' doivent être dans [0, 1]")
        if fracs != sorted(fracs):
            raise ValueError("les fractions de 'piecewise' doivent être croissantes")
        return points


def _scheduled_tag(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return "~number"
    if isinstance(value, str):
        try:
            float(value)
            return "~number"
        except ValueError:
            return None
    if isinstance(value, RampValue) or (isinstance(value, dict) and "ramp" in value):
        return "~ramp"
    if isinstance(value, PiecewiseValue) or (isinstance(value, dict) and "piecewise" in value):
        return "~piecewise"
    return None


ScheduledValue = Annotated[
    Union[
        Annotated[float, Tag("~number")],
        Annotated[RampValue, Tag("~ramp")],
        Annotated[PiecewiseValue, Tag("~piecewise")],
    ],
    Discriminator(
        _scheduled_tag,
        custom_error_type="scheduled_value",
        custom_error_message=(
            "valeur planifiée invalide : attendu un nombre, {ramp: [a, b]} "
            "ou {piecewise: [[frac, valeur], ...]}"
        ),
    ),
]


# -- project (§5.2) -------------------------------------------------------------------------
class ObjectiveSpec(StrictModel):
    """One project objective (quality or robustness)."""

    metric: str = Field(min_length=1)
    direction: Direction = "maximize"
    label: str | None = None
    constraint: str | None = None

    @field_validator("constraint")
    @classmethod
    def _check_constraint(cls, value: str | None) -> str | None:
        if value is not None and not CONSTRAINT_RE.match(value):
            raise ValueError(
                f"contrainte invalide '{value}' — format attendu : '>= 38' (opérateurs >=, <=, >, <, ==)"
            )
        return value


def parse_constraint(text: str) -> tuple[str, float]:
    """Parse ``">= 38"`` into ``(">=", 38.0)``."""
    match = CONSTRAINT_RE.match(text or "")
    if not match:
        raise ValueError(f"contrainte invalide '{text}'")
    return match.group(1), float(match.group(2))


class Objectives(StrictModel):
    """Quality × robustness objectives used by dashboards (Pareto)."""

    quality: ObjectiveSpec
    robustness: ObjectiveSpec


def default_objectives(modality: str | None) -> dict[str, dict[str, Any]]:
    """Default objectives for a modality."""
    quality = DEFAULT_QUALITY_METRIC.get(modality or "image", "psnr")
    return {
        "quality": {"metric": quality, "direction": "maximize",
                    "label": "SNR (dB)" if quality == "snr" else "PSNR (dB)"},
        "robustness": {"metric": "bit_acc_mean", "direction": "maximize",
                       "label": "Bit acc. moyenne"},
    }


class ProjectSpec(StrictModel):
    """``projects/<name>/project.yaml``."""

    name: Name
    title: str = ""
    modality: Modality
    description: str = ""
    message_bits: int = Field(48, gt=0, le=4096)
    default_benchmark: str | None = None
    objectives: Objectives
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _fill_defaults(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            defaults = default_objectives(data.get("modality"))
            objectives = data.get("objectives")
            if objectives is None:
                data["objectives"] = defaults
            elif isinstance(objectives, dict):
                data["objectives"] = {**defaults, **objectives}
        return data

    @model_validator(mode="after")
    def _fill_title(self) -> "ProjectSpec":
        if not self.title:
            self.title = self.name
        return self

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: Any) -> Any:
        return _str_list(value)


# -- experiment (§5.3) ----------------------------------------------------------------------
class ModelSpec(StrictModel):
    """Architecture name (``MODELS`` registry) and its params (never ``nbits``)."""

    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class PreprocessEntry(StrictModel):
    """One preprocessing op (``PREPROCESS`` registry)."""

    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class DataSplitSpec(StrictModel):
    """Dataset + params + preprocessing for one split."""

    dataset: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    preprocess: list[PreprocessEntry] = Field(default_factory=list)
    limit: int | None = Field(None, gt=0)


class DataSpec(StrictModel):
    """Training/validation data."""

    train: DataSplitSpec
    val: DataSplitSpec
    batch_size: int = Field(32, gt=0)
    num_workers: int = Field(4, ge=0)
    sample_rate: int = Field(16000, gt=0)
    fps: int = Field(24, gt=0)


class OptimizerSpec(StrictModel):
    """Optimizer settings (``lr`` is the base learning rate)."""

    name: str = "adamw"
    lr: float = Field(1.0e-3, gt=0)
    weight_decay: float = Field(1.0e-4, ge=0)
    betas: tuple[float, float] = (0.9, 0.999)
    grad_clip: float | None = Field(1.0, gt=0)


class LossTerm(StrictModel):
    """One weighted loss term (``LOSSES`` registry)."""

    name: str = Field(min_length=1)
    weight: float = 1.0
    params: dict[str, Any] = Field(default_factory=dict)


class LossSpec(StrictModel):
    """Loss terms keyed by a short term name (e.g. ``msg``, ``img``)."""

    terms: dict[str, LossTerm] = Field(min_length=1)


class LRScheduleSpec(StrictModel):
    """Learning-rate schedule of a phase (factor applied to ``optimizer.lr``)."""

    schedule: Literal["constant", "linear_warmup", "cosine", "linear", "wsd", "exp"] = "constant"
    warmup_steps: int = Field(0, ge=0)
    factor: float = Field(1.0, gt=0)
    min_factor: float = Field(0.0, ge=0)
    decay_fraction: float = Field(0.2, ge=0.0, le=1.0)
    gamma: float = Field(0.999, gt=0.0, le=1.0)


class AttackEntry(StrictModel):
    """Attack drawn during a phase with probability ∝ ``p``."""

    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    p: float = Field(1.0, ge=0)
    label: str | None = None


class PhaseDataSpec(StrictModel):
    """Per-phase data override (the trainer rebuilds the loader)."""

    train: DataSplitSpec


class PhaseSpec(StrictModel):
    """One curriculum phase."""

    name: Name
    steps: int = Field(gt=0)
    lr: LRScheduleSpec = Field(default_factory=LRScheduleSpec)
    loss_weights: dict[str, ScheduledValue] = Field(default_factory=dict)
    attacks: list[AttackEntry] = Field(default_factory=list)
    attack_strength: ScheduledValue = 1.0
    random_strength: bool = True
    freeze: list[str] = Field(default_factory=list)
    batch_size: int | None = Field(None, gt=0)
    data: PhaseDataSpec | None = None
    reset_optimizer: bool = False


class ValAttack(StrictModel):
    """Labelled attack (validation or benchmark); ``label`` defaults to ``name``."""

    label: str = Field(min_length=1)
    name: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _default_label(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("label") and data.get("name"):
            data = {**data, "label": data["name"]}
        return data


BenchmarkAttack = ValAttack


class FinalBenchmarkSpec(StrictModel):
    """Benchmark run at the end of training on the selected checkpoints."""

    benchmark: str = Field(min_length=1)
    checkpoints: list[str] = Field(default_factory=lambda: ["last"], min_length=1)


class EvalSpec(StrictModel):
    """Periodic validation."""

    every_steps: int = Field(1000, gt=0)
    val_batches: int = Field(10, gt=0)
    val_attacks: list[ValAttack] = Field(default_factory=list)
    final_benchmark: FinalBenchmarkSpec | None = None


class CheckpointSpec(StrictModel):
    """Checkpointing and retention policy."""

    every_steps: int | None = Field(2000, gt=0)
    keep_last: int = Field(2, ge=0)
    keep_best: dict[str, Literal["max", "min"]] = Field(default_factory=dict)
    at_phase_end: bool = True


class SamplesSpec(StrictModel):
    """Sample export (original / watermarked / attacked)."""

    every_steps: int | None = Field(2000, gt=0)
    count: int = Field(4, ge=0)


class ResourcesSpec(StrictModel):
    """Device and precision."""

    device: str = Field("auto", pattern=r"^(auto|cpu|mps|cuda(:\d+)?)$")
    precision: Literal["auto", "fp32", "bf16", "fp16"] = "auto"
    compile: bool = False


class InitFromSpec(StrictModel):
    """Branch from a checkpoint of another run."""

    run: str = Field(min_length=1)
    checkpoint: str = "last"
    start_phase: str | int | None = None
    load_optimizer: bool = False


class ExperimentSpec(StrictModel):
    """``projects/<p>/experiments/<name>.yaml``."""

    name: Name
    description: str = ""
    hypothesis: str = ""
    seed: int = 0
    model: ModelSpec
    data: DataSpec
    optimizer: OptimizerSpec = Field(default_factory=OptimizerSpec)
    loss: LossSpec
    phases: list[PhaseSpec] = Field(min_length=1)
    eval: EvalSpec = Field(default_factory=EvalSpec)
    checkpoint: CheckpointSpec = Field(default_factory=CheckpointSpec)
    samples: SamplesSpec = Field(default_factory=SamplesSpec)
    resources: ResourcesSpec = Field(default_factory=ResourcesSpec)
    init_from: InitFromSpec | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("tags", mode="before")
    @classmethod
    def _coerce_tags(cls, value: Any) -> Any:
        return _str_list(value)

    @model_validator(mode="after")
    def _check_structure(self) -> "ExperimentSpec":
        names = [p.name for p in self.phases]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"noms de phases en double : {', '.join(duplicates)}")
        if "nbits" in self.model.params:
            raise ValueError(
                "'nbits' ne doit pas figurer dans model.params : il vient de "
                "project.yaml (message_bits)"
            )
        return self

    @property
    def total_steps(self) -> int:
        return sum(p.steps for p in self.phases)


# -- HPO plan (§5.4) ------------------------------------------------------------------------
class SamplerSpec(StrictModel):
    """Optuna sampler."""

    name: Literal["tpe", "random", "nsga2", "cmaes", "grid"] = "tpe"
    params: dict[str, Any] = Field(default_factory=dict)


class PrunerSpec(StrictModel):
    """Optuna pruner (ignored for multi-objective studies)."""

    name: Literal["asha", "hyperband", "median", "none"] = "asha"
    params: dict[str, Any] = Field(default_factory=dict)


class HpoObjective(StrictModel):
    """Objective of a study (a ``val/...`` metric)."""

    metric: str = Field(min_length=1)
    direction: Direction = "maximize"


class HpoConstraint(StrictModel):
    """Constraint ``metric op value`` (feasible when satisfied)."""

    metric: str = Field(min_length=1)
    op: Literal[">=", "<=", ">", "<"] = ">="
    value: float


class ParamSpace(StrictModel):
    """Search space of one dotted spec path."""

    type: Literal["float", "int", "categorical"]
    low: int | float | None = None
    high: int | float | None = None
    step: int | float | None = Field(None, gt=0)
    log: bool = False
    choices: list[Any] | None = None

    @model_validator(mode="after")
    def _check_space(self) -> "ParamSpace":
        if self.type == "categorical":
            if not self.choices:
                raise ValueError("'choices' (liste non vide) est requis pour type: categorical")
            return self
        if self.low is None or self.high is None:
            raise ValueError(f"'low' et 'high' sont requis pour type: {self.type}")
        if self.low > self.high:
            raise ValueError("'low' doit être inférieur ou égal à 'high'")
        if self.log and self.low <= 0:
            raise ValueError("'log: true' exige low > 0")
        if self.log and self.step is not None:
            raise ValueError("'log' et 'step' sont incompatibles")
        if self.type == "int":
            for key in ("low", "high", "step"):
                value = getattr(self, key)
                if value is not None:
                    if float(value) != int(value):
                        raise ValueError(f"'{key}' doit être un entier pour type: int")
                    object.__setattr__(self, key, int(value))
        return self


class FidelitySpec(StrictModel):
    """Low-fidelity proxy applied to every trial."""

    steps_scale: float = Field(1.0, gt=0.0, le=1.0)
    overrides: dict[str, Any] = Field(default_factory=dict)


class BudgetSpec(StrictModel):
    """Study budget (at least one limit)."""

    max_trials: int | None = Field(20, gt=0)
    max_hours: float | None = Field(None, gt=0)
    max_eur: float | None = Field(None, ge=0)
    parallel: int = Field(1, ge=1, le=64)

    @model_validator(mode="after")
    def _check_limit(self) -> "BudgetSpec":
        if self.max_trials is None and self.max_hours is None and self.max_eur is None:
            raise ValueError("au moins une limite est requise : max_trials, max_hours ou max_eur")
        return self


class HpoSpec(StrictModel):
    """``projects/<p>/hpo/<name>.yaml``."""

    name: Name
    experiment: str = Field(min_length=1)
    description: str = ""
    hypothesis: str = ""
    sampler: SamplerSpec = Field(default_factory=SamplerSpec)
    pruner: PrunerSpec = Field(default_factory=PrunerSpec)
    objectives: list[HpoObjective] = Field(min_length=1)
    constraints: list[HpoConstraint] = Field(default_factory=list)
    space: dict[str, ParamSpace] = Field(min_length=1)
    fidelity: FidelitySpec = Field(default_factory=FidelitySpec)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    objective_agg: Literal["last", "best"] = "last"

    @property
    def multi_objective(self) -> bool:
        return len(self.objectives) > 1


# -- benchmark (§5.5) -----------------------------------------------------------------------
class BenchmarkSpec(StrictModel):
    """``benchmarks/<name>.yaml`` — frozen evaluation suite."""

    name: Name
    version: int = Field(1, ge=1)
    modality: Modality
    description: str = ""
    data: DataSplitSpec
    batch_size: int = Field(16, gt=0)
    seed: int = 0
    quality_metrics: list[str] = Field(default_factory=list)
    fpr: list[float] = Field(default_factory=lambda: [1.0e-2, 1.0e-4, 1.0e-6], min_length=1)
    attacks: list[ValAttack] = Field(min_length=1)

    @field_validator("fpr")
    @classmethod
    def _check_fpr(cls, values: list[float]) -> list[float]:
        if any(not (0.0 < v < 1.0) for v in values):
            raise ValueError("chaque FPR doit être strictement entre 0 et 1")
        return values


# -- dataset catalog (§5.6) -----------------------------------------------------------------
class DatasetCatalogEntry(StrictModel):
    """``datasets/<name>.yaml`` — where a dataset lives on each host."""

    name: Name
    modality: Modality
    description: str = ""
    source: str = ""
    license: str = ""
    paths: dict[str, str] = Field(min_length=1)


# -- workspace (§1.1) -----------------------------------------------------------------------
class ManualEntry(StrictModel):
    """Manual cost entry (e.g. storage, data purchase)."""

    label: str = Field(min_length=1)
    eur: float
    date: str
    project: str | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _check_date(cls, value: Any) -> Any:
        value = _date_str(value)
        if isinstance(value, str) and not DATE_RE.match(value):
            raise ValueError("date attendue au format AAAA-MM-JJ")
        return value


class BudgetSettings(StrictModel):
    """GPU budget of the workspace."""

    total_eur: float = Field(40000.0, ge=0)
    start: str | None = None
    end: str | None = None
    manual_entries: list[ManualEntry] = Field(default_factory=list)

    @field_validator("start", "end", mode="before")
    @classmethod
    def _check_dates(cls, value: Any) -> Any:
        value = _date_str(value)
        if isinstance(value, str) and not DATE_RE.match(value):
            raise ValueError("date attendue au format AAAA-MM-JJ")
        return value


class WorkspaceSettings(StrictModel):
    """``workspace.yaml``."""

    name: str = "Watermark Lab"
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    data_dir: str = ".wmlab"
    sync_interval_s: int = Field(30, ge=1)
    default_host: str = "local"


# -- hosts (§5.7) ---------------------------------------------------------------------------
class HostSpec(StrictModel):
    """A machine able to run jobs (the Mac itself or an SSH GPU server)."""

    name: Name
    kind: Literal["local", "ssh"]
    ssh_target: str | None = None
    workdir: str | None = None
    data_dir: str | None = None
    python: str | None = None
    price_eur_per_hour: float = Field(0.0, ge=0)
    gpu: str | None = None
    max_concurrent_jobs: int = Field(1, ge=1)
    notes: str | None = None

    @model_validator(mode="after")
    def _check_ssh(self) -> "HostSpec":
        if self.kind == "ssh" and not self.ssh_target:
            raise ValueError("'ssh_target' est requis pour un hôte ssh (ex. root@51.15.0.1)")
        return self


class HostsFile(StrictModel):
    """``.wmlab/hosts.yaml``."""

    hosts: list[HostSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> "HostsFile":
        names = [h.name for h in self.hosts]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"noms d'hôtes en double : {', '.join(duplicates)}")
        return self


SPEC_MODELS: dict[str, type[BaseModel]] = {
    "project": ProjectSpec,
    "experiment": ExperimentSpec,
    "hpo": HpoSpec,
    "benchmark": BenchmarkSpec,
    "dataset": DatasetCatalogEntry,
    "workspace": WorkspaceSettings,
    "hosts": HostsFile,
}
