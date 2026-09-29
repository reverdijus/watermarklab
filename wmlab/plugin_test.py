"""Quick conformance checks of a registered component (shapes, ranges, gradients).

Usage: ``wmlab plugin-test KIND NAME [--modality M]`` or :func:`run_plugin_test`.
"""

from __future__ import annotations

import inspect
import traceback
from pathlib import Path
from typing import Any, Callable

from wmlab.registry import Entry, get_registry, load_plugins

NBITS = 16
BATCH = 2
INPUT_SHAPES: dict[str, tuple[int, ...]] = {
    "image": (3, 64, 64),
    "audio": (1, 8000),
    "video": (4, 3, 32, 32),
}
RANGES: dict[str, tuple[float, float]] = {"image": (0.0, 1.0), "audio": (-1.0, 1.0),
                                          "video": (0.0, 1.0)}
# values used for required parameters without defaults (datasets/preprocess)
_REQUIRED_GUESSES: dict[str, Any] = {
    "size": 32, "frames": 2, "seconds": 0.25, "length": 4000, "num_items": 4,
    "sample_rate": 16000, "fps": 24,
}
# dataset params shrunk for a fast test when the dataset accepts them
_DATASET_SMALL: dict[str, dict[str, Any]] = {
    "image": {"size": 32, "num_items": 4},
    "audio": {"length": 4000, "num_items": 4},
    "video": {"size": 32, "frames": 4, "num_items": 4},
}

CheckResult = dict[str, Any]


class _Stop(Exception):
    """Abort the remaining checks (a prerequisite failed)."""


def _describe_error(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    where = f" ({Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {message}{where}"


class _Checks:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def add(self, check: str, ok: bool, detail: str = "") -> bool:
        self.results.append({"check": check, "ok": bool(ok), "detail": detail})
        return bool(ok)

    def run(self, check: str, fn: Callable[[], tuple[bool, str]], *, required: bool = False
            ) -> bool:
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001 — reported as a failed check
            ok, detail = False, _describe_error(exc)
        self.add(check, ok, detail)
        if required and not ok:
            raise _Stop
        return ok


def _accepted(entry: Entry) -> tuple[set[str], set[str], bool]:
    from wmlab.spec.validate import accepted_params

    if entry.params_model is not None:
        fields = entry.params_model.model_fields
        return set(fields), {k for k, f in fields.items() if f.is_required()}, False
    accepted, required, var_kw = accepted_params(entry)
    return set(accepted or []), set(required), var_kw


def make_test_params(entry: Entry, modality: str, params: dict[str, Any] | None = None
                ) -> dict[str, Any]:
    """Build parameters for a test instance (user params win over guesses)."""
    accepted, required, _ = _accepted(entry)
    out: dict[str, Any] = {}
    if entry.kind == "dataset":
        if "modality" in accepted:
            out["modality"] = modality
        out.update({k: v for k, v in _DATASET_SMALL[modality].items() if k in accepted})
    for key in required:
        if key not in out and key in _REQUIRED_GUESSES and key != "nbits":
            out[key] = _REQUIRED_GUESSES[key]
    out.update(params or {})
    return out


def _input(torch: Any, modality: str, batch: bool = True) -> Any:
    shape = ((BATCH,) if batch else ()) + INPUT_SHAPES[modality]
    x = torch.rand(*shape)
    if modality == "audio":
        x = (x * 2.0 - 1.0) * 0.5
    return x


def _in_range(tensor: Any, modality: str, tol: float = 1e-4) -> tuple[bool, str]:
    lo, hi = RANGES[modality]
    mn, mx = float(tensor.min()), float(tensor.max())
    return mn >= lo - tol and mx <= hi + tol, f"[{mn:.4f}, {mx:.4f}] (attendu [{lo:g}, {hi:g}])"


def _grad_norm(params: Any) -> float:
    total = 0.0
    for p in params:
        if p.grad is not None:
            total += float(p.grad.detach().abs().sum())
    return total


# -- per kind -------------------------------------------------------------------------------
def _check_model(c: _Checks, entry: Entry, modality: str, params: dict[str, Any]) -> None:
    import torch

    registry = get_registry("model")
    state: dict[str, Any] = {}

    def build() -> tuple[bool, str]:
        model = registry.build(entry.name, nbits=NBITS, **params)
        state["model"] = model
        n = sum(p.numel() for p in model.parameters())
        return n > 0, f"{type(model).__name__}, {n:,} paramètres".replace(",", " ")

    c.run("construction (nbits=16)", build, required=True)
    model = state["model"]
    model.train()
    declared = getattr(model, "modality", None)
    c.add("modalité déclarée", declared == modality,
          f"modality = {declared!r} (attendu {modality!r})")
    x = _input(torch, modality)
    msg = torch.randint(0, 2, (BATCH, NBITS)).float()

    def embed() -> tuple[bool, str]:
        x_w = model.embed(x, msg)
        state["x_w"] = x_w
        return tuple(x_w.shape) == tuple(x.shape), f"{tuple(x_w.shape)} (entrée {tuple(x.shape)})"

    c.run("embed : forme", embed, required=True)
    x_w = state["x_w"]
    c.run("embed : plage", lambda: _in_range(x_w.detach(), modality))
    c.add("embed : valeurs finies", bool(torch.isfinite(x_w).all()), "")

    def extract() -> tuple[bool, str]:
        logits = model.extract(x_w)
        state["logits"] = logits
        return tuple(logits.shape) == (BATCH, NBITS), f"{tuple(logits.shape)} (attendu {(BATCH, NBITS)})"

    c.run("extract : forme", extract, required=True)
    logits = state["logits"]
    c.add("extract : valeurs finies", bool(torch.isfinite(logits).all()), "")

    def grad_logits() -> tuple[bool, str]:
        model.zero_grad(set_to_none=True)
        loss = torch.nn.functional.binary_cross_entropy_with_logits(
            model.extract(model.embed(x, msg)), msg)
        loss.backward()
        norm = _grad_norm(model.parameters())
        return norm > 0 and norm == norm and norm != float("inf"), f"|grad| = {norm:.3e}"

    def grad_xw() -> tuple[bool, str]:
        model.zero_grad(set_to_none=True)
        out = model.embed(x, msg)
        (out * torch.randn_like(out)).mean().backward()
        norm = _grad_norm(model.parameters())
        return norm > 0 and norm == norm and norm != float("inf"), f"|grad| = {norm:.3e}"

    c.run("gradient depuis les logits (perte message)", grad_logits)
    c.run("gradient depuis x_w (perte d'imperceptibilité)", grad_xw)

    def forward_is_embed() -> tuple[bool, str]:
        model.eval()
        with torch.no_grad():
            a, b = model(x, msg), model.embed(x, msg)
        model.train()
        diff = float((a - b).abs().max())
        return diff <= 1e-5, f"écart max {diff:.2e}"

    c.run("forward(x, msg) = embed(x, msg)", forward_is_embed)
    if hasattr(model, "param_groups"):
        def groups() -> tuple[bool, str]:
            result = model.param_groups()
            ok = isinstance(result, dict) and all(isinstance(v, (list, tuple)) for v in result.values())
            return ok, f"groupes : {', '.join(result) if isinstance(result, dict) else type(result).__name__}"

        c.run("param_groups()", groups)


def _check_attack(c: _Checks, entry: Entry, modality: str, params: dict[str, Any]) -> None:
    import torch

    state: dict[str, Any] = {}

    def build() -> tuple[bool, str]:
        state["attack"] = get_registry("attack").build(entry.name, **params)
        return True, type(state["attack"]).__name__

    c.run("construction", build, required=True)
    attack = state["attack"]
    x = _input(torch, modality)
    outputs: dict[float, Any] = {}
    for strength in (0.0, 0.5, 1.0):
        def run(s: float = strength) -> tuple[bool, str]:
            with torch.no_grad():
                out = attack(x.clone(), strength=s)
            outputs[s] = out
            finite = bool(torch.isfinite(out).all())
            same = tuple(out.shape) == tuple(x.shape)
            return same and finite, f"{tuple(out.shape)}{'' if finite else ', valeurs non finies'}"

        c.run(f"strength={strength:g} : forme conservée", run)
    if 0.0 in outputs:
        diff = float((outputs[0.0] - x).abs().max())
        c.add("strength=0 ≈ identité", diff <= 1e-4, f"écart max {diff:.2e}")
    if 1.0 in outputs:
        c.run("strength=1 : plage", lambda: _in_range(outputs[1.0], modality, tol=1e-3))
    if getattr(attack, "differentiable", True):
        def grad() -> tuple[bool, str]:
            x_req = x.clone().requires_grad_(True)
            out = attack(x_req, strength=1.0)
            (out * torch.randn_like(out)).sum().backward()
            if x_req.grad is None:
                return False, "aucun gradient ne remonte vers l'entrée"
            norm = float(x_req.grad.abs().sum())
            return norm > 0 and norm == norm, f"|grad| = {norm:.3e}"

        c.run("gradient (differentiable = True)", grad)
    else:
        c.add("gradient", True, "non différentiable : le trainer applique un straight-through "
                                "estimator")


def _check_preprocess(c: _Checks, entry: Entry, modality: str, params: dict[str, Any]) -> None:
    import torch

    state: dict[str, Any] = {}

    def build() -> tuple[bool, str]:
        op = get_registry("preprocess").build(entry.name, **params)
        state["op"] = op
        return callable(op), type(op).__name__ + ("" if callable(op) else " (non appelable)")

    c.run("construction (callable)", build, required=True)
    op = state["op"]
    x = _input(torch, modality, batch=False)

    def apply() -> tuple[bool, str]:
        out = op(x.clone())
        state["out"] = out
        if not isinstance(out, torch.Tensor):
            return False, f"sortie de type {type(out).__name__} (tenseur attendu)"
        return True, f"{tuple(x.shape)} → {tuple(out.shape)}"

    c.run("sortie tenseur", apply, required=True)
    out = state["out"]
    channel_axis = 1 if modality == "video" else 0
    same_layout = out.ndim == x.ndim and out.shape[channel_axis] == x.shape[channel_axis]
    c.add("disposition conservée (sans dim batch)", same_layout,
          f"{out.ndim} dims, canaux = {out.shape[channel_axis] if out.ndim > channel_axis else '?'}")
    c.add("valeurs finies", bool(torch.isfinite(out).all()), "")
    deterministic = getattr(op, "deterministic", None)
    c.add("attribut deterministic", isinstance(deterministic, bool),
          f"deterministic = {deterministic!r}")
    if deterministic is True:
        def repeat() -> tuple[bool, str]:
            again = op(x.clone())
            same = tuple(again.shape) == tuple(out.shape) and bool(torch.equal(again, out))
            return same, "sorties identiques" if same else "sorties différentes"

        c.run("déterminisme", repeat)


def _check_dataset(c: _Checks, entry: Entry, modality: str, params: dict[str, Any]) -> None:
    import torch

    state: dict[str, Any] = {}

    def build() -> tuple[bool, str]:
        state["ds"] = get_registry("dataset").build(entry.name, **params)
        return True, f"{type(state['ds']).__name__} ({', '.join(f'{k}={v!r}' for k, v in params.items())})"

    c.run("construction", build, required=True)
    ds = state["ds"]
    c.run("longueur > 0", lambda: (len(ds) > 0, f"{len(ds)} items"), required=True)

    def item() -> tuple[bool, str]:
        value = ds[0]
        state["item"] = value
        if not isinstance(value, dict) or "x" not in value or "id" not in value:
            return False, f"item de type {type(value).__name__} (attendu dict avec 'x' et 'id')"
        return isinstance(value["id"], str), f"id = {value['id']!r}"

    c.run("item {'x', 'id'}", item, required=True)
    x = state["item"]["x"]
    expected_ndim = len(INPUT_SHAPES[modality])
    ok = isinstance(x, torch.Tensor) and x.ndim == expected_ndim
    if ok:
        channels = x.shape[1] if modality == "video" else x.shape[0]
        ok = channels == INPUT_SHAPES[modality][1 if modality == "video" else 0]
    layout = {"image": "[3,H,W]", "audio": "[1,T]", "video": "[F,3,H,W]"}[modality]
    c.add(f"forme {layout}", ok, f"{tuple(x.shape) if isinstance(x, torch.Tensor) else type(x).__name__}")
    if isinstance(x, torch.Tensor):
        c.add("dtype float32", x.dtype == torch.float32, str(x.dtype))
        c.run("plage", lambda: _in_range(x.float(), modality, tol=1e-4))


def _dummy_model(modality: str) -> Any:
    import torch

    try:
        from wmlab.models.base import WatermarkModel
    except Exception:
        WatermarkModel = torch.nn.Module  # type: ignore[assignment,misc]

    class _Dummy(WatermarkModel):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            try:
                super().__init__(NBITS)
            except TypeError:
                super().__init__()
            self.nbits = NBITS
            self.modality = modality
            self.scale = torch.nn.Parameter(torch.ones(1))

        def embed(self, x: Any, msg: Any) -> Any:
            return x * self.scale

        def forward(self, x: Any, msg: Any) -> Any:
            return self.embed(x, msg)

        def extract(self, x: Any) -> Any:
            return torch.zeros(x.shape[0], NBITS)

    return _Dummy()


def _check_loss(c: _Checks, entry: Entry, modality: str, params: dict[str, Any]) -> None:
    import torch

    state: dict[str, Any] = {}

    def build() -> tuple[bool, str]:
        state["loss"] = get_registry("loss").build(entry.name, **params)
        return callable(state["loss"]), type(state["loss"]).__name__

    c.run("construction", build, required=True)

    def batch() -> tuple[bool, str]:
        from wmlab.losses.base import StepBatch

        lo, hi = RANGES[modality]
        x = _input(torch, modality)
        x_w = (x + 0.01 * torch.randn_like(x)).clamp(lo, hi).detach().requires_grad_(True)
        logits = torch.randn(BATCH, NBITS, requires_grad=True)
        state.update(x_w=x_w, logits=logits)
        state["batch"] = StepBatch(
            x=x, x_w=x_w, x_att=x_w * 1.0, msg=torch.randint(0, 2, (BATCH, NBITS)).float(),
            logits=logits, modality=modality, model=_dummy_model(modality), step=0,
            phase="plugin_test", extras={"sample_rate": 16000, "fps": 24},
        )
        return True, "StepBatch factice"

    c.run("StepBatch (wmlab.losses.base)", batch, required=True)

    def value() -> tuple[bool, str]:
        out = state["loss"](state["batch"])
        state["value"] = out
        if not isinstance(out, torch.Tensor) or out.numel() != 1:
            shape = tuple(out.shape) if isinstance(out, torch.Tensor) else type(out).__name__
            return False, f"sortie {shape} (scalaire attendu)"
        return bool(torch.isfinite(out).all()), f"valeur = {float(out):.4g}"

    c.run("scalaire fini", value, required=True)

    def grad() -> tuple[bool, str]:
        state["value"].reshape(()).backward()
        norm = sum(float(t.grad.abs().sum()) for t in (state["x_w"], state["logits"])
                   if t.grad is not None)
        return norm > 0 and norm == norm, f"|grad(x_w, logits)| = {norm:.3e}"

    c.run("gradient vers x_w ou logits", grad)


def _check_metric(c: _Checks, entry: Entry, modality: str, params: dict[str, Any]) -> None:
    import torch

    state: dict[str, Any] = {}

    def build() -> tuple[bool, str]:
        obj = entry.obj
        fn = get_registry("metric").build(entry.name, **params) if inspect.isclass(obj) else obj
        state["fn"] = fn
        return callable(fn), getattr(fn, "__name__", type(fn).__name__)

    c.run("fonction appelable", build, required=True)
    lo, hi = RANGES[modality]
    x = _input(torch, modality)
    x_w = (x + 0.01 * torch.randn_like(x)).clamp(lo, hi)

    def call() -> tuple[bool, str]:
        with torch.no_grad():
            out = state["fn"](x, x_w, sample_rate=16000, fps=24)
        state["out"] = out
        if not isinstance(out, torch.Tensor):
            return False, f"sortie de type {type(out).__name__} (Tensor[B] attendu)"
        return tuple(out.shape) == (BATCH,), f"{tuple(out.shape)} (attendu {(BATCH,)})"

    c.run("sortie [B]", call, required=True)
    out = state["out"]
    c.add("valeurs finies", bool(torch.isfinite(out).all()),
          ", ".join(f"{float(v):.4g}" for v in out.flatten()[:4]))


_CHECKERS = {
    "model": _check_model, "attack": _check_attack, "preprocess": _check_preprocess,
    "dataset": _check_dataset, "loss": _check_loss, "metric": _check_metric,
}


def run_plugin_test(kind: str, name: str, modality: str | None = None, *,
                    params: dict[str, Any] | None = None, workspace: Path | None = None
                    ) -> list[CheckResult]:
    """Run conformance checks; return ``[{"check", "ok", "detail"}]``."""
    c = _Checks()
    try:
        registry = get_registry(kind)
    except ValueError as exc:
        c.add("type de composant", False, str(exc))
        return c.results
    report = load_plugins(workspace)
    if not registry.has(name):
        try:
            registry.get(name)
        except KeyError as exc:
            detail = str(exc)
        related = [e for e in report.errors if e["module"].startswith("lab")]
        if related:
            detail += " — erreurs d'import : " + "; ".join(
                f"{e['module']}: {e['error'].strip().splitlines()[-1]}" for e in related)
        c.add("enregistrement", False, detail)
        return c.results
    entry = registry.get(name)
    c.add("enregistrement", True, f"{entry.source} (modalité : {entry.modality_label()})")
    if modality is None:
        modality = entry.modality[0] if entry.modality else "image"
    if modality not in INPUT_SHAPES:
        c.add("modalité", False, f"modalité invalide '{modality}' — valeurs possibles : "
                                 f"{', '.join(INPUT_SHAPES)}")
        return c.results
    if not entry.supports(modality):
        c.add("modalité", False, f"'{name}' ne supporte pas {modality} "
                                 f"(supporte : {entry.modality_label()})")
        return c.results
    try:
        import torch
    except ImportError:
        c.add("torch", False, "PyTorch n'est pas installé")
        return c.results
    torch.manual_seed(0)
    try:
        _CHECKERS[entry.kind](c, entry, modality, make_test_params(entry, modality, params))
    except _Stop:
        pass
    except Exception as exc:  # noqa: BLE001
        c.add("exécution", False, _describe_error(exc))
    return c.results
