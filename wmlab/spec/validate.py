"""Two-level validation of YAML specs: pydantic schema, then semantics (registries, refs)."""

from __future__ import annotations

import difflib
import inspect
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from wmlab.registry import (
    REGISTRIES,
    Entry,
    PluginReport,
    kind_label,
    load_plugins,
    none_word,
    unknown_word,
)
from wmlab.spec.errors import SpecError, format_validation_error
from wmlab.spec.loader import (
    YAML_SUFFIXES,
    OverrideError,
    apply_experiment_overrides,
    benchmark_path,
    dataset_catalog_path,
    detect_kind_from_content,
    experiment_path_for_hpo,
    find_project_dir,
    get_path,
    kind_from_location,
    list_benchmarks,
    parse_yaml,
    read_yaml,
    validate_data,
)
from wmlab.spec.models import (
    DEFAULT_QUALITY_METRIC,
    BenchmarkSpec,
    DataSplitSpec,
    DatasetCatalogEntry,
    ExperimentSpec,
    HpoSpec,
    ProjectSpec,
)

ValidationErrors = list[dict[str, str]]

# kwargs supplied by the engine rather than the YAML (never required in params)
_CONTEXT_KEYS: dict[str, set[str]] = {"model": {"nbits"}}


def _workspace_or_none() -> Path | None:
    try:
        from wmlab.paths import workspace_root

        return workspace_root()
    except Exception:
        return None


def _abs_path(path: str | Path | None, root: Path | None) -> Path | None:
    if path is None:
        return None
    p = Path(path).expanduser()
    if not p.is_absolute() and not p.exists() and root is not None:
        p = root / p
    return p.resolve()


class _Checker:
    """Accumulates semantic errors for one file."""

    def __init__(self, root: Path | None, report: PluginReport | None) -> None:
        self.root = root
        self.report = report
        self.errors: ValidationErrors = []

    def err(self, loc: str, msg: str) -> None:
        self.errors.append({"loc": loc, "msg": msg})

    # -- components ----------------------------------------------------------------------
    def _plugins_hint(self) -> str:
        if self.report is not None and self.report.errors:
            return (" (certains modules n'ont pas pu être importés : lancez « wmlab registry » "
                    "pour voir les erreurs)")
        return ""

    def unknown(self, kind: str, name: str, modality: str | None) -> str:
        """Message for an unknown component, listing the compatible names."""
        compatible = REGISTRIES[kind].names(modality)
        msg = f"{kind_label(kind)} {unknown_word(kind)} '{name}'"
        close = difflib.get_close_matches(name, compatible, n=3, cutoff=0.5)
        if close:
            msg += f" — vouliez-vous dire : {', '.join(close)} ?"
        msg += f" — disponibles : {', '.join(compatible) if compatible else none_word(kind)}"
        return msg + self._plugins_hint()

    def component(self, kind: str, name: str, params: dict[str, Any] | None, loc: str,
                  *, modality: str | None, name_key: str = "name",
                  params_key: str = "params") -> Entry | None:
        registry = REGISTRIES[kind]
        name_loc = f"{loc}.{name_key}" if loc else name_key
        params_loc = f"{loc}.{params_key}" if loc else params_key
        if not registry.has(name):
            self.err(name_loc, self.unknown(kind, name, modality))
            return None
        compatible = registry.names(modality)
        entry = registry.get(name)
        if modality is not None and not entry.supports(modality):
            self.err(name_loc,
                     f"{kind_label(kind)} '{name}' incompatible avec la modalité {modality} "
                     f"(supporte : {entry.modality_label()}) — compatibles : "
                     f"{', '.join(compatible) if compatible else none_word(kind)}")
        self.params(entry, params or {}, params_loc)
        return entry

    def params(self, entry: Entry, params: dict[str, Any], loc: str) -> None:
        context = _CONTEXT_KEYS.get(entry.kind, set())
        label = f"{kind_label(entry.kind)} '{entry.name}'"
        if entry.params_model is not None:
            fields = entry.params_model.model_fields
            if entry.params_model.model_config.get("extra") != "allow":
                for key in params:
                    if key not in fields and key not in context:
                        self.err(f"{loc}.{key}", _unknown_param_msg(key, label, list(fields)))
            known = {k: v for k, v in params.items() if k in fields}
            try:
                entry.params_model.model_validate(known)
            except ValidationError as exc:
                self.errors.extend(format_validation_error(exc, prefix=loc))
            return
        accepted, required, var_kw = accepted_params(entry)
        if accepted is None:
            return
        if not var_kw:
            for key in params:
                if key not in accepted and key not in context:
                    self.err(f"{loc}.{key}", _unknown_param_msg(key, label, accepted))
        for key in required:
            if key not in params and key not in context:
                self.err(f"{loc}.{key}", f"paramètre requis manquant pour {label}")

    def dataset_refs(self, value: Any, loc: str) -> None:
        """Check ``@name`` references against ``datasets/<name>.yaml``."""
        if isinstance(value, str) and value.startswith("@"):
            name = value[1:].split("/", 1)[0]
            if self.root is not None and dataset_catalog_path(name, self.root) is None:
                available = _yaml_stems(self.root / "datasets")
                self.err(loc, f"dataset '@{name}' absent du catalogue — créez datasets/{name}.yaml"
                              f" (disponibles : {', '.join(available) if available else '(aucun)'})")
        elif isinstance(value, dict):
            for key, item in value.items():
                self.dataset_refs(item, f"{loc}.{key}")
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self.dataset_refs(item, f"{loc}.{i}")

    def split(self, split: DataSplitSpec, loc: str, modality: str | None) -> None:
        self.component("dataset", split.dataset, split.params, loc, modality=modality,
                       name_key="dataset")
        declared = split.params.get("modality")
        if modality is not None and isinstance(declared, str) and declared != modality:
            self.err(f"{loc}.params.modality",
                     f"modalité '{declared}' différente de celle du projet ({modality})")
        self.dataset_refs(split.params, f"{loc}.params")
        for i, op in enumerate(split.preprocess):
            self.component("preprocess", op.name, op.params, f"{loc}.preprocess.{i}",
                           modality=modality)

    def file_name(self, name: str, path: Path | None, what: str) -> None:
        if path is not None and path.stem != name:
            self.err("name", f"le nom '{name}' doit correspondre au nom du {what} '{path.stem}'")

    def benchmark_ref(self, name: str, loc: str, modality: str | None,
                      nbits: int | None = None) -> None:
        if self.root is None:
            return
        path = benchmark_path(name, self.root)
        if path is None:
            available = list_benchmarks(self.root)
            self.err(loc, f"benchmark inconnu '{name}' — disponibles : "
                          f"{', '.join(available) if available else '(aucun)'}")
            return
        try:
            bench = validate_data(read_yaml(path), "benchmark", path)
        except SpecError as exc:
            self.err(loc, f"le benchmark '{name}' est invalide (benchmarks/{path.name}) : "
                          f"{_first_error(exc)}")
            return
        if modality is not None and bench.modality != modality:
            self.err(loc, f"le benchmark '{name}' est de modalité {bench.modality}, "
                          f"le projet est {modality}")
        if nbits:
            unreachable = [f for f in bench.fpr if not fpr_reachable(nbits, f)]
            if unreachable:
                shown = ", ".join(f"{f:g}" for f in unreachable)
                self.err(loc, (
                    f"le benchmark '{name}' demande un FPR inatteignable avec {nbits} bits : "
                    f"{shown} < 2^-{nbits} = {2.0 ** -nbits:.2g} (même {nbits}/{nbits} bits "
                    f"corrects dépassent ce FPR) — tpr@{unreachable[0]:g} vaudrait toujours 0. "
                    f"Créez une nouvelle version du benchmark avec fpr ≥ {2.0 ** -nbits:.2g} "
                    "(ou augmentez message_bits)."))


def fpr_reachable(nbits: int, fpr: float) -> bool:
    """False when even a perfect decoder cannot reach ``fpr`` (``2**-nbits > fpr``)."""
    return 2.0 ** -int(nbits) <= float(fpr)


def _first_error(exc: SpecError) -> str:
    errors = getattr(exc, "errors", None) or []
    if errors:
        first = errors[0]
        loc = first.get("loc") or ""
        return f"{loc} : {first.get('msg', '')}" if loc else str(first.get("msg", ""))
    return str(exc)


def _unknown_param_msg(key: str, label: str, accepted: list[str]) -> str:
    close = difflib.get_close_matches(key, accepted, n=1, cutoff=0.6)
    hint = f" — vouliez-vous dire '{close[0]}' ?" if close else ""
    return (f"paramètre inconnu '{key}' pour {label}{hint} — attendus : "
            f"{', '.join(accepted) if accepted else '(aucun)'}")


def _yaml_stems(folder: Path) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.iterdir() if p.suffix in YAML_SUFFIXES)


def accepted_params(entry: Entry) -> tuple[list[str] | None, list[str], bool]:
    """Return ``(accepted names, required names, accepts **kwargs)`` of a component."""
    if entry.params_model is not None:
        return list(entry.params_model.model_fields), [], False
    try:
        sig = inspect.signature(entry.obj)
    except (TypeError, ValueError):
        return None, [], True
    accepted: list[str] = []
    required: list[str] = []
    var_kw = False
    skip = 2 if entry.kind == "metric" else 0
    for pname, param in sig.parameters.items():
        if param.kind == param.VAR_KEYWORD:
            var_kw = True
            continue
        if param.kind == param.VAR_POSITIONAL:
            continue
        if skip and param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
            skip -= 1
            continue
        if pname == "self" or param.kind == param.POSITIONAL_ONLY:
            continue
        accepted.append(pname)
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    return accepted, required, var_kw


# -- per-kind semantic checks ---------------------------------------------------------------
def _project_context(c: _Checker, path: Path | None) -> ProjectSpec | None:
    if path is None:
        return None
    project_dir = find_project_dir(path)
    if project_dir is None:
        c.err("", "aucun project.yaml trouvé au-dessus de ce fichier : la modalité et les "
                  "références ne peuvent pas être vérifiées")
        return None
    try:
        return validate_data(read_yaml(project_dir / "project.yaml"), "project",
                             project_dir / "project.yaml")
    except SpecError:
        c.err("", f"project.yaml du projet '{project_dir.name}' invalide : corrigez-le d'abord")
        return None


def _check_selector(c: _Checker, selector: str, loc: str, spec: ExperimentSpec) -> None:
    phases = [p.name for p in spec.phases]
    if selector == "last":
        return
    if selector.startswith("best:"):
        metric = selector[5:]
        if metric not in spec.checkpoint.keep_best:
            c.err(loc, f"'{selector}' : la métrique '{metric}' n'est pas suivie — ajoutez-la à "
                       f"checkpoint.keep_best (actuellement : "
                       f"{', '.join(spec.checkpoint.keep_best) or '(vide)'})")
    elif selector.startswith("phase_end:"):
        phase = selector[len("phase_end:"):]
        if phase not in phases:
            c.err(loc, f"'{selector}' : phase inconnue '{phase}' — phases : {', '.join(phases)}")
        elif not spec.checkpoint.at_phase_end:
            c.err(loc, f"'{selector}' exige checkpoint.at_phase_end: true")


def _check_experiment(c: _Checker, spec: ExperimentSpec, path: Path | None) -> None:
    project = _project_context(c, path)
    modality = project.modality if project is not None else None
    c.file_name(spec.name, path, "fichier")

    model = c.component("model", spec.model.name, spec.model.params, "model", modality=modality)
    if model is not None and spec.phases and getattr(model.obj, "trainable", True) is False:
        c.err("phases", f"le modèle '{spec.model.name}' est une baseline figée (poids officiels, "
                        "évaluation seule) : utilisez « phases: [] »")
    c.split(spec.data.train, "data.train", modality)
    c.split(spec.data.val, "data.val", modality)
    for term_name, term in spec.loss.terms.items():
        c.component("loss", term.name, term.params, f"loss.terms.{term_name}", modality=modality)

    terms = list(spec.loss.terms)
    for i, phase in enumerate(spec.phases):
        for key in phase.loss_weights:
            if key not in spec.loss.terms:
                c.err(f"phases.{i}.loss_weights.{key}",
                      f"terme de perte inconnu '{key}' — termes définis dans loss.terms : "
                      f"{', '.join(terms)}")
        for j, attack in enumerate(phase.attacks):
            c.component("attack", attack.name, attack.params, f"phases.{i}.attacks.{j}",
                        modality=modality)
        if phase.data is not None:
            c.split(phase.data.train, f"phases.{i}.data.train", modality)

    labels: list[str] = []
    for j, attack in enumerate(spec.eval.val_attacks):
        c.component("attack", attack.name, attack.params, f"eval.val_attacks.{j}",
                    modality=modality)
        if attack.label in labels:
            c.err(f"eval.val_attacks.{j}.label", f"label en double '{attack.label}'")
        labels.append(attack.label)

    _check_warmups(c, spec, "")

    final = spec.eval.final_benchmark
    if final is not None:
        c.benchmark_ref(final.benchmark, "eval.final_benchmark.benchmark", modality,
                        nbits=project.message_bits if project is not None else None)
        for k, selector in enumerate(final.checkpoints):
            _check_selector(c, selector, f"eval.final_benchmark.checkpoints.{k}", spec)

    for metric in spec.checkpoint.keep_best:
        if not metric.startswith("val/"):
            c.err(f"checkpoint.keep_best.{metric}",
                  "keep_best doit porter sur une métrique de validation (val/...), "
                  "ex. val/bit_acc_mean")

    if spec.init_from is not None and spec.init_from.start_phase is not None:
        start = spec.init_from.start_phase
        names = [p.name for p in spec.phases]
        if isinstance(start, int):
            if not 0 <= start < len(names):
                c.err("init_from.start_phase",
                      f"index de phase {start} hors bornes (0 à {len(names) - 1})")
        elif start not in names:
            c.err("init_from.start_phase",
                  f"phase inconnue '{start}' — phases : {', '.join(names)}")


def _check_warmups(c: _Checker, spec: ExperimentSpec, prefix: str,
                   hint: str = "") -> None:
    """A warmup longer than its phase never reaches the peak LR (silently accepted by
    ``lr_factor``): report it."""
    for i, phase in enumerate(spec.phases):
        warmup, steps = int(phase.lr.warmup_steps), int(phase.steps)
        if warmup > steps:
            c.err(f"{prefix}phases.{i}.lr.warmup_steps",
                  f"phase '{phase.name}' : warmup_steps ({warmup}) > steps ({steps}) — le LR "
                  f"plafonne à {steps / warmup:.0%} de sa valeur et le schedule n'est jamais "
                  f"appliqué{hint}")


def _check_project(c: _Checker, spec: ProjectSpec, path: Path | None) -> None:
    if path is not None and path.parent.name != spec.name:
        c.err("name", f"le nom '{spec.name}' doit correspondre au dossier du projet "
                      f"'{path.parent.name}'")
    if spec.default_benchmark:
        c.benchmark_ref(spec.default_benchmark, "default_benchmark", spec.modality,
                        nbits=spec.message_bits)
    metric = spec.objectives.quality.metric.removeprefix("val/")
    metrics = REGISTRIES["metric"]
    if not metrics.has(metric):
        c.err("objectives.quality.metric", c.unknown("metric", metric, spec.modality))
    elif not metrics.get(metric).supports(spec.modality):
        c.err("objectives.quality.metric",
              f"métrique '{metric}' incompatible avec la modalité {spec.modality}")


def _sample_value(space: Any) -> Any:
    if space.type == "categorical":
        return space.choices[0]
    return space.low


_PARAM_OWNER = [
    (re.compile(r"^model$"), "model", "name"),
    (re.compile(r"^loss\.terms\.[^.]+$"), "loss", "name"),
    (re.compile(r"(^|\.)attacks\.[^.]+$"), "attack", "name"),
    (re.compile(r"(^|\.)val_attacks\.[^.]+$"), "attack", "name"),
    (re.compile(r"(^|\.)preprocess\.[^.]+$"), "preprocess", "name"),
    (re.compile(r"(^|\.)(train|val)$"), "dataset", "dataset"),
]


def _param_accepted(resolved: dict[str, Any], key: str) -> bool:
    """True if ``key`` targets a param (``....params.<p>``) accepted by its component."""
    parts = key.split(".")
    if len(parts) < 3 or parts[-2] != "params":
        return False
    owner_path = ".".join(parts[:-2])
    try:
        owner = get_path(resolved, owner_path)
    except KeyError:
        return False
    if not isinstance(owner, dict):
        return False
    for pattern, kind, name_key in _PARAM_OWNER:
        if pattern.search(owner_path):
            registry = REGISTRIES[kind]
            name = owner.get(name_key)
            if not isinstance(name, str) or not registry.has(name):
                return False
            accepted, _, var_kw = accepted_params(registry.get(name))
            return accepted is None or var_kw or parts[-1] in accepted
    return False


def _check_override_paths(c: _Checker, raw: dict[str, Any], base: ExperimentSpec | None,
                          values: dict[str, Any], loc_prefix: str) -> None:
    resolved = base.model_dump(mode="json") if base is not None else None
    for key, value in values.items():
        loc = f"{loc_prefix}.{key}"
        try:
            candidate = apply_experiment_overrides(raw, {key: value})
        except OverrideError as exc:
            c.err(loc, str(exc).replace(f"override '{key}' invalide : ", "chemin non résolu : "))
            continue
        if resolved is None:
            continue
        try:
            get_path(resolved, key)
        except KeyError:
            if not _param_accepted(resolved, key):
                c.err(loc, f"chemin '{key}' introuvable dans l'expérience de base (ni dans ses "
                           "valeurs par défaut, ni parmi les paramètres du composant)")
                continue
        try:
            ExperimentSpec.model_validate(candidate)
        except ValidationError as exc:
            first = format_validation_error(exc)[0]
            c.err(loc, f"la valeur {value!r} rend l'expérience invalide ({first['loc']} : "
                       f"{first['msg']})")


def _check_hpo(c: _Checker, spec: HpoSpec, path: Path | None) -> None:
    c.file_name(spec.name, path, "fichier")
    for i, objective in enumerate(spec.objectives):
        if not objective.metric.startswith("val/"):
            c.err(f"objectives.{i}.metric",
                  f"'{objective.metric}' : l'objectif doit être une métrique de validation "
                  "de la forme 'val/...' (ex. val/psnr, val/bit_acc_mean)")
    for i, constraint in enumerate(spec.constraints):
        if not constraint.metric.startswith("val/"):
            c.err(f"constraints.{i}.metric",
                  f"'{constraint.metric}' : la contrainte doit porter sur une métrique 'val/...'")
    if spec.sampler.name == "grid":
        for key, space in spec.space.items():
            if space.type == "float" and space.step is None:
                c.err(f"space.{key}", "sampler grid : un paramètre float doit avoir un 'step' "
                                      "(ou utilisez type: categorical)")
    if path is None:
        return
    exp_path = experiment_path_for_hpo(path, spec)
    if not exp_path.is_file():
        available = _yaml_stems(exp_path.parent)
        c.err("experiment", f"expérience de base introuvable '{spec.experiment}' — disponibles : "
                            f"{', '.join(available) if available else '(aucune)'}")
        return
    try:
        raw = read_yaml(exp_path)
    except SpecError:
        c.err("experiment", f"l'expérience de base '{spec.experiment}' n'est pas un YAML valide")
        return
    if not isinstance(raw, dict):
        c.err("experiment", f"l'expérience de base '{spec.experiment}' est invalide")
        return
    try:
        base = ExperimentSpec.model_validate(raw)
    except ValidationError as exc:
        first = format_validation_error(exc)[0]
        c.err("experiment", f"l'expérience de base '{spec.experiment}' est invalide "
                            f"({first['loc'] or '(racine)'} : {first['msg']}) : corrigez-la "
                            "d'abord")
        base = None
    if base is not None and not base.phases:
        c.err("experiment", f"l'expérience de base '{spec.experiment}' est une évaluation seule "
                            "(phases: []) : il n'y a rien à optimiser")
        return
    _check_override_paths(c, raw, base,
                          {k: _sample_value(s) for k, s in spec.space.items()}, "space")
    _check_override_paths(c, raw, base, dict(spec.fidelity.overrides), "fidelity.overrides")
    if base is None:
        return
    _check_hpo_metrics(c, spec, raw, base, _project_context(_Checker(None, None), path))
    _check_ineffective_paths(c, spec, base)
    _check_fidelity(c, spec, raw, base)


def experiment_val_keys(spec: ExperimentSpec, modality: str | None) -> set[str]:
    """Keys of ``val/*`` metrics the trainer logs for ``spec`` (§6.5)."""
    keys = {"val/loss", "val/bit_acc", "val/bit_acc_mean"}
    if modality is None:
        keys |= {f"val/{m}" for m in DEFAULT_QUALITY_METRIC.values()} | {"val/ssim"}
    else:
        keys.add(f"val/{DEFAULT_QUALITY_METRIC.get(modality, 'psnr')}")
        if modality in ("image", "video"):
            keys.add("val/ssim")
    for attack in spec.eval.val_attacks:
        keys.add(f"val/bit_acc/{attack.label}")
    return keys


def _check_hpo_metrics(c: _Checker, spec: HpoSpec, raw: dict[str, Any], base: ExperimentSpec,
                       project: ProjectSpec | None) -> None:
    """Objectives / constraints must be metrics the trials will actually log: a typo would
    otherwise fail every trial after its full training."""
    effective = base
    try:  # fidelity overrides may change eval.val_attacks
        effective = ExperimentSpec.model_validate(
            apply_experiment_overrides(raw, dict(spec.fidelity.overrides)))
    except (OverrideError, ValidationError):
        pass
    keys = experiment_val_keys(effective, project.modality if project is not None else None)
    shown = ", ".join(sorted(keys))
    for i, objective in enumerate(spec.objectives):
        if objective.metric.startswith("val/") and objective.metric not in keys:
            close = difflib.get_close_matches(objective.metric, sorted(keys), n=1, cutoff=0.6)
            hint = f" — vouliez-vous dire '{close[0]}' ?" if close else ""
            c.err(f"objectives.{i}.metric",
                  f"'{objective.metric}' n'est pas une métrique produite par l'expérience "
                  f"'{spec.experiment}'{hint} — disponibles : {shown}")
    for i, constraint in enumerate(spec.constraints):
        if constraint.metric.startswith("val/") and constraint.metric not in keys:
            close = difflib.get_close_matches(constraint.metric, sorted(keys), n=1, cutoff=0.6)
            hint = f" — vouliez-vous dire '{close[0]}' ?" if close else ""
            c.err(f"constraints.{i}.metric",
                  f"'{constraint.metric}' n'est pas une métrique produite par l'expérience "
                  f"'{spec.experiment}'{hint} — disponibles : {shown}")


_TERM_WEIGHT_RE = re.compile(r"^loss\.terms\.([^.]+)\.weight$")


def _check_ineffective_paths(c: _Checker, spec: HpoSpec, base: ExperimentSpec) -> None:
    """``loss.terms.<t>.weight`` has no effect when every phase sets ``loss_weights.<t>``
    (the phase value wins, §5.3): a study would spend its budget on a dead dimension."""
    for source, values in (("space", spec.space), ("fidelity.overrides", spec.fidelity.overrides)):
        for key in values:
            match = _TERM_WEIGHT_RE.match(str(key))
            if match is None or not base.phases:
                continue
            term = match.group(1)
            if all(term in phase.loss_weights for phase in base.phases):
                phases = ", ".join(f"phases.{i}.loss_weights.{term}"
                                   for i in range(len(base.phases)))
                c.err(f"{source}.{key}",
                      f"chemin sans effet : loss.terms.{term}.weight est écrasé dans toutes les "
                      f"phases par {phases} — cherchez directement sur ces chemins (ex. "
                      f"phases.{len(base.phases) - 1}.loss_weights.{term}"
                      + (".ramp.1" if hasattr(base.phases[-1].loss_weights.get(term), "ramp")
                         else "") + ")")


def _check_fidelity(c: _Checker, spec: HpoSpec, raw: dict[str, Any], base: ExperimentSpec
                    ) -> None:
    """Warmups must still fit their phases once ``fidelity`` (steps_scale + overrides) is
    applied to the base experiment."""
    from wmlab.hpo.space import trial_overrides

    try:
        overrides = trial_overrides(base.model_dump(mode="json"), {}, spec.fidelity)
        effective = ExperimentSpec.model_validate(apply_experiment_overrides(raw, overrides))
    except (OverrideError, ValidationError, KeyError, TypeError):
        return
    _check_warmups(c, effective, "fidelity.", hint=" (valeurs après steps_scale et "
                   "fidelity.overrides : ajoutez un override phases.<i>.lr.warmup_steps)")


def _check_benchmark(c: _Checker, spec: BenchmarkSpec, path: Path | None) -> None:
    c.file_name(spec.name, path, "fichier")
    c.split(spec.data, "data", spec.modality)
    labels: list[str] = []
    for i, attack in enumerate(spec.attacks):
        c.component("attack", attack.name, attack.params, f"attacks.{i}", modality=spec.modality)
        if attack.label in labels:
            c.err(f"attacks.{i}.label", f"label en double '{attack.label}'")
        labels.append(attack.label)
    for i, metric in enumerate(spec.quality_metrics):
        registry = REGISTRIES["metric"]
        if not registry.has(metric):
            c.err(f"quality_metrics.{i}", c.unknown("metric", metric, spec.modality))
        elif not registry.get(metric).supports(spec.modality):
            c.err(f"quality_metrics.{i}",
                  f"métrique '{metric}' incompatible avec la modalité {spec.modality}")


def _check_dataset(c: _Checker, spec: DatasetCatalogEntry, path: Path | None) -> None:
    c.file_name(spec.name, path, "fichier")


_SEMANTIC = {
    "experiment": _check_experiment,
    "project": _check_project,
    "hpo": _check_hpo,
    "benchmark": _check_benchmark,
    "dataset": _check_dataset,
}


# -- public API -----------------------------------------------------------------------------
def _validate(content: str | dict[str, Any], kind: str | None, path: Path | None,
              root: Path | None, report: PluginReport | None, semantic: bool
              ) -> ValidationErrors:
    if isinstance(content, (str, bytes)):
        text = content.decode("utf-8") if isinstance(content, bytes) else content
        try:
            data = parse_yaml(text, path)
        except SpecError as exc:
            return exc.errors
    else:
        data = content
    if data is None:
        data = {}
    kind = kind or (kind_from_location(path) if path is not None else None) \
        or detect_kind_from_content(data)
    if kind is None:
        return [{"loc": "", "msg": "type de fichier inconnu : impossible de déterminer s'il "
                                   "s'agit d'un projet, d'une expérience, d'un plan HPO, d'un "
                                   "benchmark ou d'un dataset"}]
    try:
        spec = validate_data(data, kind, path)
    except SpecError as exc:
        return exc.errors
    check = _SEMANTIC.get(kind)
    if not semantic or check is None:
        return []
    if report is None:
        report = load_plugins(root)
    checker = _Checker(root, report)
    check(checker, spec, path)
    return checker.errors


def validate_content(content: str | dict[str, Any], kind: str | None = None,
                     path: str | Path | None = None, *, semantic: bool = True
                     ) -> ValidationErrors:
    """Validate YAML text (or parsed data); return ``[{"loc", "msg"}]`` (empty = valid)."""
    root = _workspace_or_none()
    return _validate(content, kind, _abs_path(path, root), root, None, semantic)


def validate_file(path: str | Path, *, semantic: bool = True) -> ValidationErrors:
    """Validate a YAML file on disk."""
    root = _workspace_or_none()
    abs_path = _abs_path(path, root)
    assert abs_path is not None
    try:
        text = abs_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [{"loc": "", "msg": f"fichier illisible : {exc}"}]
    return _validate(text, None, abs_path, root, None, semantic)


def workspace_spec_files(root: Path) -> list[Path]:
    """Return every declarative YAML file of the workspace, in a stable order."""
    files: list[Path] = []
    if (root / "workspace.yaml").is_file():
        files.append(root / "workspace.yaml")
    projects = root / "projects"
    if projects.is_dir():
        for project_dir in sorted(p for p in projects.iterdir() if p.is_dir()):
            if project_dir.name.startswith("."):
                continue
            files.append(project_dir / "project.yaml")
            for sub in ("experiments", "hpo"):
                folder = project_dir / sub
                if folder.is_dir():
                    files.extend(sorted(p for p in folder.iterdir()
                                        if p.suffix in YAML_SUFFIXES and p.is_file()))
    for sub in ("benchmarks", "datasets"):
        folder = root / sub
        if folder.is_dir():
            files.extend(sorted(p for p in folder.iterdir()
                                if p.suffix in YAML_SUFFIXES and p.is_file()))
    return files


def _module_relpath(module: str, root: Path) -> str:
    base = root.joinpath(*module.split("."))
    if (base / "__init__.py").is_file():
        return (base / "__init__.py").relative_to(root).as_posix()
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py").relative_to(root).as_posix()
    return module.replace(".", "/")


def validate_workspace(root: str | Path | None = None, *, semantic: bool = True
                       ) -> dict[str, ValidationErrors]:
    """Validate every spec file of the workspace plus the ``lab`` plugins.

    Returns ``{relative path: errors}`` (an empty list means the file is valid).
    """
    root_path = Path(root).expanduser().resolve() if root is not None else _workspace_or_none()
    if root_path is None:
        raise FileNotFoundError("workspace introuvable (définissez WMLAB_WORKSPACE)")
    report = load_plugins(root_path) if semantic else None
    results: dict[str, ValidationErrors] = {}
    for file in workspace_spec_files(root_path):
        rel = file.relative_to(root_path).as_posix()
        if not file.is_file():
            results[rel] = [{"loc": "", "msg": "fichier manquant (chaque dossier de projects/ "
                                               "doit contenir un project.yaml)"}]
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except OSError as exc:
            results[rel] = [{"loc": "", "msg": f"fichier illisible : {exc}"}]
            continue
        results[rel] = _validate(text, None, file.resolve(), root_path, report, semantic)
    if report is not None:
        for rel, errors in plugin_error_results(report, root_path).items():
            results.setdefault(rel, []).extend(errors)
    return results


def plugin_error_results(report: PluginReport, root: Path) -> dict[str, ValidationErrors]:
    """``{lab/<file>.py: [error]}`` for the ``lab`` plugins that could not be imported."""
    results: dict[str, ValidationErrors] = {}
    for error in report.errors:
        module = str(error.get("module", ""))
        if module == "lab" or module.startswith("lab."):
            rel = _module_relpath(module, root)
            results.setdefault(rel, []).append(
                {"loc": "", "msg": f"import du plugin impossible : {error.get('error', '')}"})
    return results
