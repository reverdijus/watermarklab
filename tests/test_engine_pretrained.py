"""Pretrained baselines: weights catalogue, payload mapping, evaluation-only runs and
reference checkpoints (ARCHITECTURE.md §4.1, §4.6, §5.3, §6.2)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Iterator

import pytest
import torch

from helpers_engine import experiment_dict, make_workspace, shimmed, write_yaml
from wmlab import weights
from wmlab.models import load_model_from_checkpoint
from wmlab.models.pretrained import PretrainedWatermarkModel, embed_at_resolution
from wmlab.spec.validate import validate_file
from wmlab.tracking import RunReader, list_runs
from wmlab.train.trainer import train_from_spec

PLUGIN = '''\
"""Fake pretrained wrapper (official weights = a small tensor file)."""

import torch

from wmlab.models.pretrained import PretrainedWatermarkModel
from wmlab.registry import MODELS
from wmlab.weights import WeightsFile, register_weights

register_weights(WeightsFile(
    name="_fake_v1", url="https://example.invalid/fake.pt", filename="fake.pt", size_mb=0.01,
    license="MIT", source="https://example.invalid"))


@MODELS.register("_fake_pretrained", modality="image")
class FakePretrained(PretrainedWatermarkModel):
    """Baseline factice."""

    native_nbits = 20

    def __init__(self, nbits: int, download: bool = False) -> None:
        super().__init__(nbits, fixed_bits={3: 1})
        state = torch.load(self.weights_file("_fake_v1", download=download), weights_only=True)
        self.gain = torch.nn.Parameter(state["gain"])

    def embed(self, x, msg):
        flat = x.clone().flatten(2)  # bits written in the first pixels of channel 0
        flat[:, 0, : self.native] = self.to_native(msg) * self.gain
        return flat.reshape(x.shape).clamp(0, 1)

    def extract(self, x):
        return self.from_native((x.flatten(2)[:, 0, : self.native] - 0.5) * 20 * self.gain)
'''


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(weights, "WEIGHTS", {})
    with shimmed():
        yield


@pytest.fixture
def ws(workspace: Path) -> Path:
    evaluation = experiment_dict(name="baseline_eval", phases=[])
    evaluation["model"] = {"name": "_fake_pretrained", "params": {}}
    evaluation["eval"]["final_benchmark"] = {"benchmark": "eng_v1", "checkpoints": ["last"]}
    make_workspace(workspace, experiments=[evaluation])
    (workspace / "lab" / "fake_pretrained.py").write_text(PLUGIN, encoding="utf-8")
    return workspace


def _write_official(workspace: Path, gain: float = 1.0) -> Path:
    path = workspace / ".wmlab" / "weights" / "_fake_v1" / "fake.pt"
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"gain": torch.tensor(gain)}, path)
    return path


class _Payload(PretrainedWatermarkModel):
    native_nbits = 8


def test_payload_mapping_with_filler_and_fixed_bits():
    model = _Payload(5, fixed_bits={1: 1, 6: 0})
    msg = torch.tensor([[1.0, 0.0, 1.0, 1.0, 0.0]])
    native = model.to_native(msg)
    assert native.shape == (1, 8)
    assert model.payload_index.tolist() == [0, 2, 3, 4, 5]
    assert native[0, 1] == 1 and native[0, 6] == 0
    assert torch.equal(model.from_native(native), msg)
    # the filler is deterministic and identical for every image
    assert torch.equal(_Payload(5, fixed_bits={1: 1, 6: 0}).filler, model.filler)
    assert "payload_index" not in model.state_dict()
    with pytest.raises(ValueError, match="dont 2 figé"):
        _Payload(7, fixed_bits={1: 1, 6: 0})


def test_embed_at_resolution_keeps_full_resolution_content():
    x = torch.rand(2, 3, 40, 30)
    out = embed_at_resolution(x, 16, lambda small: small + 0.1)
    assert out.shape == x.shape
    assert torch.allclose(out - x, torch.full_like(x, 0.1), atol=1e-5)


def test_missing_weights_explain_how_to_pull(ws: Path):
    from wmlab.registry import MODELS, load_plugins

    load_plugins()
    with pytest.raises(FileNotFoundError, match="wmlab weights pull _fake_v1"):
        MODELS.build("_fake_pretrained", nbits=8)


def test_pull_weights_is_atomic_and_checks_sha256(ws: Path, monkeypatch: pytest.MonkeyPatch):
    from wmlab.registry import load_plugins

    load_plugins()
    payload = b"official-weights"

    class _Response(io.BytesIO):
        headers = {"Content-Length": str(len(payload))}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(weights.urllib.request, "urlopen", lambda *a, **k: _Response(payload))
    path = weights.pull_weights("_fake_v1")
    assert path.read_bytes() == payload and not path.with_name("fake.pt.part").exists()
    entry = weights.WEIGHTS["_fake_v1"]
    monkeypatch.setitem(weights.WEIGHTS, "_fake_v1",
                        weights.WeightsFile(**{**entry.__dict__, "sha256": "0" * 64}))
    with pytest.raises(ValueError, match="sha256"):
        weights.pull_weights("_fake_v1", force=True)
    assert not path.with_name("fake.pt.part").exists()


def test_evaluation_only_run_writes_reference_checkpoint(ws: Path):
    official = _write_official(ws)
    train_from_spec("projects/eng/experiments/baseline_eval.yaml", device="cpu")
    reader = RunReader(list_runs(ws / ".wmlab" / "runs")[0])
    manifest = reader.manifest()
    assert manifest["status"] == "completed"
    assert manifest["summary"]["val/bit_acc"] == 1.0
    (ckpt,) = reader.checkpoints()
    assert ckpt["name"] == "step_000000" and ckpt["phase"] == "eval"
    state = torch.load(ws / ".wmlab" / "runs" / manifest["run_id"] / "checkpoints" / ckpt["file"],
                       weights_only=True)
    assert state["model"] == {} and state["optimizer"] is None
    assert state["external_weights"][0]["sha256"] == weights.sha256_file(official)
    (result,) = reader.evals()
    assert result["benchmark"] == "eng_v1" and result["robustness"]["none"]["bit_acc"] == 1.0

    # the reference checkpoint reloads the official file… as long as it is unchanged
    ckpt_path = reader.dir / "checkpoints" / ckpt["file"]
    assert load_model_from_checkpoint(ckpt_path).nbits == 16
    _write_official(ws, gain=0.5)
    with pytest.raises(RuntimeError, match="non reproductibles"):
        load_model_from_checkpoint(ckpt_path)


def test_frozen_baseline_refuses_training_phases(ws: Path):
    path = ws / "projects" / "eng" / "experiments" / "train_frozen.yaml"
    spec = experiment_dict(name="train_frozen")
    spec["model"] = {"name": "_fake_pretrained", "params": {}}
    write_yaml(path, spec)
    errors = validate_file(path)
    assert any(e["loc"] == "phases" and "évaluation seule" in e["msg"] for e in errors)


def test_evaluation_only_refuses_init_from(ws: Path):
    path = ws / "projects" / "eng" / "experiments" / "bad_eval.yaml"
    spec = experiment_dict(name="bad_eval", phases=[],
                           init_from={"run": "x", "checkpoint": "last"})
    write_yaml(path, spec)
    assert any("init_from" in e["msg"] for e in validate_file(path))
