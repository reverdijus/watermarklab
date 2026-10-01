"""Junction points between modules fixed during integration (small, CPU-only checks)."""

from __future__ import annotations

import os
from pathlib import Path

from wmlab.attacks import configure_attack
from wmlab.registry import ATTACKS, load_plugins, reload_plugins
from wmlab.server.leaderboard import leaderboard, project_best
from wmlab.tracking.run import write_eval_file


def test_configure_attack_propagates_rate_and_fps() -> None:
    load_plugins()
    lowpass = configure_attack(ATTACKS.build("lowpass", cutoff_hz=2000.0), sample_rate=44100)
    assert lowpass.sample_rate == 44100 and lowpass.params["sample_rate"] == 44100
    # an explicit YAML value wins over the data setting
    explicit = configure_attack(ATTACKS.build("mp3", sample_rate=8000), sample_rate=44100,
                                params={"sample_rate": 8000})
    assert explicit.sample_rate == 8000
    # nested attacks (combo / framewise) are configured too
    combo = ATTACKS.build("combo", attacks=[{"name": "echo"}, {"name": "gaussian_noise"}])
    configure_attack(combo, sample_rate=22050)
    assert combo.steps[0].sample_rate == 22050
    h264 = configure_attack(ATTACKS.build("h264"), fps=30)
    assert h264.fps == 30
    # attacks without such attributes and None values are left alone
    assert configure_attack(ATTACKS.build("identity"), sample_rate=8000) is not None
    assert configure_attack(None, sample_rate=8000) is None
    jpeg = configure_attack(ATTACKS.build("jpeg"), sample_rate=None, fps=None)
    assert not hasattr(jpeg, "sample_rate")


def test_reload_plugins_reimports_lab(workspace: Path) -> None:
    plugin = workspace / "lab" / "glue_plugin.py"
    plugin.write_text(
        "from wmlab.registry import ATTACKS\n"
        "from wmlab.attacks.base import Attack\n"
        "@ATTACKS.register('glue_attack', modality='image')\n"
        "class GlueAttack(Attack):\n"
        "    VERSION = 1\n"
        "    def forward(self, x, strength=1.0):\n"
        "        return x\n", encoding="utf-8")
    report = load_plugins(workspace)
    assert not report.errors and ATTACKS.get("glue_attack").obj.VERSION == 1
    plugin.write_text(plugin.read_text(encoding="utf-8").replace("VERSION = 1", "VERSION = 2"),
                      encoding="utf-8")
    stat = plugin.stat()  # same size, same second: move the mtime so the .pyc cache is stale
    os.utime(plugin, (stat.st_atime, stat.st_mtime + 5))
    assert ATTACKS.get("glue_attack").obj.VERSION == 1  # load_plugins alone keeps the old one
    report = reload_plugins(workspace)
    assert not report.errors and ATTACKS.get("glue_attack").obj.VERSION == 2


def _eval(run_id: str, checkpoint: str, psnr: float, *, smoke: bool = False) -> dict:
    result = {"run_id": run_id, "checkpoint": checkpoint, "benchmark": "image_v1",
              "benchmark_version": 1, "created_at": "2026-10-01T00:00:00Z", "duration_s": 1.0,
              "nbits": 8, "n_items": 4, "quality": {"psnr": psnr},
              "robustness": {"jpeg": {"bit_acc": 0.9}}, "summary": {"psnr": psnr,
                                                                    "bit_acc_mean": 0.9},
              "project": "p", "experiment": "e"}
    if smoke:
        result["smoke"] = True
    return result


def test_smoke_eval_file_and_leaderboard_exclusion(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-x"
    full = write_eval_file(run_dir, _eval("run-x", "step_000010", 30.0))
    smoke = write_eval_file(run_dir, _eval("run-x", "step_000010", 5.0, smoke=True))
    assert full.name == "step_000010__image_v1.json"
    assert smoke.name == "step_000010__image_v1__smoke.json" and full.is_file()
    evals = [_eval("run-x", "step_000010", 30.0), _eval("run-x", "step_000010", 5.0, smoke=True)]
    board = leaderboard([], evals)
    assert [r["quality"]["psnr"] for r in board["rows"]] == [30.0]
    objectives = {"quality": {"metric": "psnr", "direction": "maximize"},
                  "robustness": {"metric": "bit_acc_mean", "direction": "maximize"}}
    best = project_best(objectives, "image_v1", [], evals)
    assert best is not None and best["quality"] == 30.0


def _manifest(run_id: str, psnr: float, tags: list[str]) -> dict:
    return {"run_id": run_id, "project": "p", "experiment": "e", "modality": "image",
            "status": "completed", "created_at": "2026-10-01T00:00:00Z", "tags": tags,
            "summary": {"val/psnr": psnr, "val/bit_acc_mean": 0.8, "val/bit_acc": 0.9}}


def test_smoke_runs_excluded_from_validation_leaderboard() -> None:
    runs = [_manifest("run-full", 30.0, ["baseline"]), _manifest("run-smoke", 7.0, ["smoke"])]
    board = leaderboard(runs, [], source="val")
    assert [r["run_id"] for r in board["rows"]] == ["run-full"]
    objectives = {"quality": {"metric": "psnr", "direction": "maximize"},
                  "robustness": {"metric": "bit_acc_mean", "direction": "maximize"}}
    best = project_best(objectives, None, runs, [])
    assert best is not None and best["run_id"] == "run-full"
