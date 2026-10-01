"""Registry, plugin discovery and builtins loading."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, ValidationError

from helpers_core import install_fake_plugins
from wmlab import builtins
from wmlab.registry import (
    ATTACKS,
    METRICS,
    MODELS,
    REGISTRIES,
    PluginReport,
    Registry,
    UnknownComponentError,
    get_registry,
    load_plugins,
)


class ToyParams(BaseModel):
    width: int = Field(4, gt=0)
    gain: float = 1.0


class Toy:
    """A toy component.

    Details that are not part of the description.
    """

    def __init__(self, nbits: int = 0, width: int = 4, gain: float = 1.0) -> None:
        self.nbits, self.width, self.gain = nbits, width, gain


def test_register_get_has_list():
    reg = Registry("model")
    reg.register("toy", modality="image", params=ToyParams)(Toy)
    entry = reg.get("toy")
    assert reg.has("toy") and "toy" in reg and not reg.has("other")
    assert entry.kind == "model" and entry.obj is Toy and entry.modality == ("image",)
    assert entry.description == "A toy component."
    assert entry.builtin is False and entry.source == Toy.__module__
    assert [e.name for e in reg.list("image")] == ["toy"]
    assert reg.list("audio") == []
    assert reg.names() == ["toy"]


def test_modality_forms():
    reg = Registry("attack")
    reg.register("a", modality=["image", "video"])(Toy)
    reg.register("b")(type("B", (), {}))
    assert reg.get("a").modality == ("image", "video")
    assert reg.get("b").modality is None and reg.get("b").supports("audio")
    assert [e.name for e in reg.list("audio")] == ["b"]
    with pytest.raises(ValueError, match="modalité invalide"):
        reg.register("c", modality="text")(Toy)


def test_duplicate_registration_rules():
    reg = Registry("model")
    reg.register("toy")(Toy)
    reg.register("toy")(Toy)  # same object: re-import is fine

    class Other:
        pass

    with pytest.raises(ValueError, match="déjà enregistré"):
        reg.register("toy")(Other)
    reg.register("toy", override=True)(Other)
    assert reg.get("toy").obj is Other


def test_unknown_name_lists_close_names():
    reg = Registry("model")
    reg.register("hidden")(Toy)
    reg.register("audio_conv")(Toy)
    with pytest.raises(KeyError) as info:
        reg.get("hiden")
    message = str(info.value)
    assert isinstance(info.value, UnknownComponentError)
    assert message.startswith("modèle inconnu 'hiden'")
    assert "vouliez-vous dire : hidden" in message
    assert "disponibles : audio_conv, hidden" in message
    with pytest.raises(KeyError, match="attaque inconnue"):
        Registry("attack").get("x")


def test_build_validates_params_and_passes_defaults():
    reg = Registry("model")
    reg.register("toy", params=ToyParams)(Toy)
    obj = reg.build("toy", nbits=16, width=8)
    assert (obj.nbits, obj.width, obj.gain) == (16, 8, 1.0)
    with pytest.raises(ValidationError):
        reg.build("toy", nbits=16, width=0)
    no_model = Registry("attack")
    no_model.register("toy")(Toy)
    assert no_model.build("toy", width=3).width == 3


def test_describe_uses_params_model_or_signature():
    reg = Registry("model")
    reg.register("with_model", params=ToyParams)(Toy)

    class Sig:
        """Signature-derived schema."""

        def __init__(self, nbits: int, size: int, ratio: float = 0.5, *args, flag: bool = False,
                     name: str | None = None, **kwargs) -> None:
            pass

    reg.register("with_sig", modality="audio")(Sig)
    described = {d["name"]: d for d in reg.describe()}
    assert set(described["with_model"]) == {"name", "kind", "modality", "description",
                                             "params_schema", "source", "builtin"}
    assert described["with_model"]["params_schema"]["properties"]["width"]["default"] == 4
    schema = described["with_sig"]["params_schema"]
    assert set(schema["properties"]) == {"size", "ratio", "flag", "name"}
    assert schema["properties"]["size"]["type"] == "integer"
    assert schema["properties"]["ratio"] == {"title": "Ratio", "type": "number", "default": 0.5}
    assert schema["properties"]["flag"]["type"] == "boolean"
    assert schema["required"] == ["size"]
    assert described["with_sig"]["modality"] == ["audio"]
    assert described["with_sig"]["description"] == "Signature-derived schema."


def test_metric_schema_skips_inputs():
    reg = Registry("metric")

    def metric(x, x_w, eps: float = 1e-8, **ctx):
        """Metric."""

    reg.register("m")(metric)
    schema = reg.describe()[0]["params_schema"]
    assert set(schema["properties"]) == {"eps"}


def test_global_registries_and_aliases():
    assert set(REGISTRIES) == {"model", "attack", "dataset", "preprocess", "loss", "metric"}
    assert get_registry("models") is MODELS and get_registry("attack") is ATTACKS
    with pytest.raises(ValueError, match="type de composant inconnu"):
        get_registry("optimizer")


def test_builtins_load_never_raises():
    errors = builtins.load()
    assert isinstance(errors, list)
    for error in errors:
        assert set(error) == {"module", "error"} and error["module"] in builtins.BUILTIN_MODULES


def test_load_plugins_imports_lab_recursively(workspace: Path):
    install_fake_plugins(workspace)
    (workspace / "lab" / "models").mkdir()
    (workspace / "lab" / "models" / "__init__.py").write_text("")
    (workspace / "lab" / "models" / "extra.py").write_text(
        "from wmlab.registry import MODELS\n\n"
        "@MODELS.register('_test_extra', modality='video')\n"
        "class Extra:\n    '''Extra model.'''\n    def __init__(self, nbits):\n        pass\n")
    report = load_plugins(workspace)
    assert isinstance(report, PluginReport)
    assert {"lab", "lab.core_fakes", "lab.models", "lab.models.extra"} <= set(report.loaded)
    assert not [e for e in report.errors if e["module"].startswith("lab")]
    assert MODELS.has("_test_extra") and MODELS.has("_test_model") and METRICS.has("_test_psnr")
    assert MODELS.get("_test_extra").source == "lab.models.extra"
    assert MODELS.get("_test_extra").builtin is False
    # idempotent
    again = load_plugins(workspace)
    assert set(report.loaded) == set(again.loaded)


def test_load_plugins_collects_errors(workspace: Path):
    (workspace / "lab" / "broken.py").write_text("raise RuntimeError('plugin cassé')\n")
    (workspace / "lab" / "ok.py").write_text(
        "from wmlab.registry import LOSSES\n\n@LOSSES.register('_test_ok')\n"
        "class Ok:\n    pass\n")
    report = load_plugins(workspace)
    errors = {e["module"]: e["error"] for e in report.errors}
    assert "lab.broken" in errors and "plugin cassé" in errors["lab.broken"]
    assert "lab.ok" in report.loaded
    assert not report.ok


def test_load_plugins_switching_workspace(tmp_path: Path, workspace: Path, monkeypatch):
    monkeypatch.setattr(sys, "path", list(sys.path))
    install_fake_plugins(workspace)
    load_plugins(workspace)
    assert MODELS.has("_test_model")
    other = tmp_path / "other"
    (other / "lab").mkdir(parents=True)
    (other / "lab" / "__init__.py").write_text("")
    (other / "workspace.yaml").write_text("name: other\n")
    report = load_plugins(other)
    assert not MODELS.has("_test_model")  # entries of the previous lab were forgotten
    assert "lab" in report.loaded
    load_plugins(workspace)
    assert MODELS.has("_test_model")
