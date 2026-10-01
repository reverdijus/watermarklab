"""End-to-end integration: real engine data (smoke training, evaluations, smoke HPO study)
in a temporary workspace, then every GET response of the HTTP API (§9) is checked against
the TypeScript interfaces of the contract (keys AND value types), plus the junction points
between the server and the engine (file creation, HPO estimate, HPO job overrides).

Components are the real builtin ones (``hidden``, ``synthetic``, ``jpeg``…), kept tiny so
that the whole module runs in well under a minute on CPU.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml
from fastapi.testclient import TestClient

from helpers_core import write_yaml
from wmlab.eval.benchmark import evaluate_checkpoint
from wmlab.hpo.driver import run_study
from wmlab.server.app import create_app
from wmlab.server.indexer import reindex_workspace
from wmlab.tracking import RunReader, read_json
from wmlab.train.trainer import train_from_spec

TOKEN = "integration-token"
PROJECT = "tiny-image"

# -- §9 TypeScript interfaces as type shapes ----------------------------------------------------
# A shape is: a python type / tuple of types, ``None`` (null), ``"str"``…, a dict (object with
# exactly these keys unless a key is suffixed by "?"), a list ``[shape]`` (array), a tuple
# ``(shape_a, shape_b)`` (union), ``{"*": shape}`` (record) or ``Enum(...)``.
Num = (int, float)


class Enum:
    def __init__(self, *values: Any) -> None:
        self.values = values


MODALITY = Enum("image", "audio", "video")
RUN_STATUS = Enum("queued", "running", "completed", "failed", "killed", "lost")
JOB_STATUS = Enum("queued", "launching", "running", "completed", "failed", "killed", "lost")
DIRECTION = Enum("maximize", "minimize")
SERIES = {"steps": [Num], "values": [Num]}
OBJECTIVE = {"metric": str, "direction": DIRECTION, "label?": (str, None), "constraint?": (str, None)}
VALIDATION_ERROR = {"loc": str, "msg": str}
PROJECT_SUMMARY = {
    "name": str, "title": str, "modality": MODALITY, "description": str, "message_bits": int,
    "default_benchmark": (str, None), "tags": [str],
    "objectives": {"quality": OBJECTIVE, "robustness": OBJECTIVE},
    "n_experiments": int, "n_hpo_plans": int, "n_runs": int, "n_running": int,
    "last_activity": (str, None), "cost_eur": Num,
    "best": ({"run_id": str, "checkpoint": (str, None), "quality": (Num, None),
              "robustness": (Num, None), "source": Enum("benchmark", "val")}, None),
}
EXPERIMENT_INFO = {"name": str, "path": str, "description": str, "hypothesis": str, "model": str,
                   "n_phases": int, "total_steps": int, "valid": bool,
                   "errors": [VALIDATION_ERROR], "n_runs": int}
HPO_PLAN_INFO = {"name": str, "path": str, "experiment": str, "description": str,
                 "n_params": int, "objectives": [OBJECTIVE], "valid": bool,
                 "errors": [VALIDATION_ERROR], "n_studies": int}
POINTS = [[Num]]  # [step, value][] — checked for pairs by check_pairs
PHASE_PREVIEW = {"name": str, "start": int, "end": int, "steps": int, "lr": POINTS,
                 "attack_strength": POINTS, "loss_weights": {"*": POINTS}, "attacks": [str]}
PHASE_PLAN_PREVIEW = {"total_steps": int, "phases": [PHASE_PREVIEW]}
PROGRESS = {"step": int, "total_steps": int, "phase": str, "phase_index": int, "n_phases": int}
RUN_SUMMARY = {
    "run_id": str, "project": str, "experiment": str, "modality": MODALITY, "status": RUN_STATUS,
    "host": str, "created_at": str, "started_at": (str, None), "ended_at": (str, None),
    "duration_s": (Num, None), "cost_eur": Num, "study": (str, None), "trial": (int, None),
    "parent": ({"run_id": str, "checkpoint": str, "start_phase?": (str, int, None), "resume?": bool}, None),
    "tags": [str], "progress": (PROGRESS, None), "summary": {"*": Num},
    "params": {"*": (str, int, float, bool, None)}, "error": (str, None), "user": (str, None),
}
CHECKPOINT = {"name": str, "step": int, "phase": str, "metrics": {"*": Num}, "tags": [str],
              "created_at": str, "size_bytes": int, "available_locally": bool}
EVAL_RESULT = {"run_id": str, "checkpoint": str, "benchmark": str, "benchmark_version": int,
               "created_at": str, "duration_s": (Num, None), "nbits": int, "n_items": int,
               "quality": {"*": Num}, "robustness": {"*": {"*": Num}}, "summary": {"*": Num}}
SAMPLE = {"step": int, "kind": MODALITY, "name": str, "path": str, "group": str}
RUN_EVENT = {"t": Num, "type": str, "*": object}
RUN_DETAIL = {
    **RUN_SUMMARY, "spec": {"*": object}, "spec_yaml": str, "spec_path": (str, None),
    "env": {"git": ({"*": object}, None), "git_diff": (str, None),
            "hardware": ({"*": object}, None), "pip_freeze_lines": int, "command": (str, None),
            "launch": ({"*": object}, None)},
    "plan": (PHASE_PLAN_PREVIEW, None), "events": [RUN_EVENT], "checkpoints": [CHECKPOINT],
    "evals": [EVAL_RESULT], "notes": str, "samples": [SAMPLE], "metric_keys": [str],
    "price_eur_per_hour": (Num, None), "datasets": {"*": str}, "overrides": {"*": str},
}
TRIAL = {"number": int, "state": Enum("COMPLETE", "PRUNED", "FAIL", "RUNNING", "WAITING"),
         "params": {"*": object}, "values": ([Num], None), "intermediate": POINTS,
         "run_id": (str, None), "duration_s": (Num, None)}
STUDY_SUMMARY = {
    "study_id": str, "project": str, "plan": str, "experiment": str, "status": RUN_STATUS,
    "host": str, "created_at": str, "ended_at": (str, None), "n_trials": int, "n_complete": int,
    "n_pruned": int, "n_failed": int, "n_running": int, "objectives": [OBJECTIVE],
    "best": [{"trial": int, "values": [Num], "params": {"*": object}, "run_id": (str, None)}],
    "cost_eur": Num,
    "budget": {"max_trials": (int, None), "max_hours": (Num, None), "max_eur": (Num, None),
               "parallel": int},
    "user": (str, None),
}
STUDY_DETAIL = {**STUDY_SUMMARY, "spec": {"*": object}, "trials": [TRIAL],
                "importances": ({"*": Num}, None), "pareto": [int], "notes": str}
LEADER_ROW = {"run_id": str, "project": str, "experiment": str, "checkpoint": (str, None),
              "benchmark": (str, None), "source": Enum("benchmark", "val"), "created_at": str,
              "quality": {"*": Num}, "summary": {"*": Num}, "robustness": {"*": {"*": Num}}}
LEADERBOARD = {"benchmarks": [str], "quality_metrics": [str], "robustness_metrics": [str],
               "attacks": [str], "rows": [LEADER_ROW]}
REGISTRY_ENTRY = {"name": str, "kind": str, "modality": ([str], None), "description": str,
                  "params_schema": ({"*": object}, None), "source": str, "builtin": bool}
BENCHMARK_INFO = {"name": str, "path": str, "modality": MODALITY, "version": int,
                  "description": str,
                  "attacks": [{"label": str, "name": str, "params": {"*": object}}],
                  "quality_metrics": [str], "fpr": [Num], "n_items": (int, None), "valid": bool,
                  "errors": [VALIDATION_ERROR]}
DATASET_INFO = {"name": str, "path": str, "modality": MODALITY, "description": str,
                "license": str, "source": str, "paths": {"*": str}, "used_by": [str],
                "valid": bool, "errors": [VALIDATION_ERROR]}
HOST = {"name": str, "kind": Enum("local", "ssh"), "ssh_target?": (str, None),
        "workdir?": (str, None), "data_dir?": (str, None), "python?": (str, None),
        "price_eur_per_hour": Num, "gpu?": (str, None), "max_concurrent_jobs": int,
        "notes?": (str, None), "scaleway": ({"zone": str, "server_id": str}, None),
        "decommissioned_at": (str, None), "unretrieved_checkpoints": (int, None),
        "unretrieved_bytes": (int, None), "inventory_at": (str, None), "active_jobs": int}
RETRIEVAL = {"status": Enum("complete", "partial", "failed", "n/a"), "checkpoints": int,
             "bytes": int, "missing": [str]}
JOB = {"job_id": str, "kind": Enum("train", "hpo", "eval", "setup", "retrieve"), "host": str,
       "target": str, "overrides": {"*": str}, "status": JOB_STATUS, "run_id": (str, None),
       "study_id": (str, None), "created_at": str, "started_at": (str, None),
       "ended_at": (str, None), "max_hours": (Num, None), "smoke": bool, "error": (str, None),
       "pid": (int, None), "last_sync_at": (str, None), "user": (str, None),
       "git_ref": (str, None), "git_sha": (str, None), "retrieval": (RETRIEVAL, None),
       "result": ({"runs": int, "studies": int, "checkpoints": int, "bytes": int,
                   "missing": [str]}, None)}
ME = {"id": str, "name": str, "email": str, "role": Enum("admin", "member"), "hosted": bool,
      "public_url": (str, None), "must_change_password": bool,
      "auth": Enum("session", "server_token")}
GIT_STATE = {"available": bool, "hosted": bool, "branch": (str, None), "sha": (str, None),
             "short_sha": (str, None), "dirty": bool, "remote": (str, None),
             "ahead": (int, None), "behind": (int, None), "last_pull_at": (str, None),
             "last_pull_error": (str, None)}
HOST_INVENTORY = {"host": str, "reachable": bool, "error": (str, None), "checked_at": str,
                  "active_jobs": int, "remote_data_dir": (str, None),
                  "runs": [{"run_id": str, "project": (str, None), "status": (str, None),
                            "checkpoints_total": int, "checkpoints_local": int,
                            "bytes_remote": int, "bytes_missing": int, "indexed": bool}],
                  "studies": [{"study_id": str, "trials": int, "synced": bool}],
                  "unretrieved_checkpoints": int, "unretrieved_bytes": int, "unindexed_runs": int}
INSTANCE = {"configured": bool, "state": (str, None), "commercial_type": (str, None),
            "zone": (str, None), "error": (str, None)}
COST_ITEM = {"kind": Enum("run", "study", "eval", "manual"), "id": str, "project": (str, None),
             "host": (str, None), "started_at": (str, None), "hours": Num, "eur": Num,
             "label?": str}
BUCKET = {"eur": Num, "gpu_hours": Num}
COSTS = {"total_eur": Num, "spent_eur": Num, "remaining_eur": Num, "running_eur_per_hour": Num,
         "by_project": [{"project": str, **BUCKET}], "by_host": [{"host": str, **BUCKET}],
         "by_month": [{"month": str, **BUCKET}], "items": [COST_ITEM],
         "budget": {"total_eur": Num, "start": (str, None), "end": (str, None)}}
SETTINGS = {"name": str,
            "budget": {"total_eur": Num, "start": (str, None), "end": (str, None),
                       "manual_entries": [{"label": str, "eur": Num, "date": str,
                                           "project": (str, None)}]},
            "data_dir": str, "sync_interval_s": Num, "default_host": str,
            "hosted": {"enabled": bool, "git_branch": str, "auto_pull_s": int,
                       "auto_commit": bool, "public_url": (str, None),
                       "allow_registration": bool}}
OVERVIEW = {"workspace": {"name": str, "path": str},
            "budget": {"total_eur": Num, "spent_eur": Num, "remaining_eur": Num,
                       "running_eur_per_hour": Num},
            "counts": {"projects": int, "runs": int, "running": int, "studies": int,
                       "gpu_hours": Num},
            "running": [RUN_SUMMARY], "recent": [RUN_SUMMARY], "projects": [PROJECT_SUMMARY],
            "jobs": [JOB]}
PROJECT_DETAIL = {"summary": PROJECT_SUMMARY, "spec": {"*": object},
                  "experiments": [EXPERIMENT_INFO], "hpo_plans": [HPO_PLAN_INFO], "notes": str}
METRICS_RESPONSE = {"series": {"*": SERIES}, "phases": [{"name": str, "start": int, "end": int}]}
SYSTEM_RESPONSE = {"t": [Num], "series": {"*": [Num]}}
FILE_RESPONSE = {"path": str, "content": str, "kind": str,
                 "validation": {"valid": bool, "errors": [VALIDATION_ERROR]}}
REGISTRY_RESPONSE = {"entries": {"*": [REGISTRY_ENTRY]}, "errors": [{"module": str, "error": str}]}
COMPARE_RESPONSE = {"runs": [RUN_SUMMARY], "series": {"*": {"*": SERIES}}}
ESTIMATE = {"total_steps": int, "est_hours": (Num, None), "est_eur": (Num, None), "basis": str}


def check(value: Any, shape: Any, where: str = "$") -> None:
    """Assert that ``value`` matches ``shape`` (see the shape mini-language above)."""
    if isinstance(shape, Enum):
        assert value in shape.values, f"{where}: {value!r} ∉ {shape.values}"
    elif shape is None:
        assert value is None, f"{where}: attendu null, reçu {value!r}"
    elif shape is object:
        return
    elif shape is bool:
        assert isinstance(value, bool), f"{where}: booléen attendu, reçu {value!r}"
    elif shape is int:
        assert isinstance(value, int) and not isinstance(value, bool), \
            f"{where}: entier attendu, reçu {value!r}"
    elif isinstance(shape, type):
        assert isinstance(value, shape) and not isinstance(value, bool), \
            f"{where}: {shape.__name__} attendu, reçu {value!r}"
    elif isinstance(shape, tuple):  # union
        errors = []
        for option in shape:
            try:
                check(value, option, where)
                return
            except AssertionError as exc:
                errors.append(str(exc))
        raise AssertionError(f"{where}: aucune alternative ne correspond — " + " | ".join(errors))
    elif isinstance(shape, list):
        assert isinstance(value, list), f"{where}: liste attendue, reçu {type(value).__name__}"
        for i, item in enumerate(value):
            check(item, shape[0], f"{where}[{i}]")
    elif isinstance(shape, dict):
        assert isinstance(value, dict), f"{where}: objet attendu, reçu {type(value).__name__}"
        if "*" in shape and len(shape) == 1:  # record
            for key, item in value.items():
                assert isinstance(key, str)
                check(item, shape["*"], f"{where}.{key}")
            return
        required = {k for k in shape if not k.endswith("?") and k != "*"}
        optional = {k[:-1] for k in shape if k.endswith("?")}
        missing = required - set(value)
        assert not missing, f"{where}: clés manquantes {sorted(missing)}"
        if "*" not in shape:
            extra = set(value) - required - optional
            assert not extra, f"{where}: clés inattendues {sorted(extra)}"
        for key, item in value.items():
            sub = shape.get(key, shape.get(f"{key}?", shape.get("*")))
            if sub is not None or key in shape:
                check(item, sub, f"{where}.{key}")
    else:  # pragma: no cover
        raise TypeError(f"forme inconnue {shape!r}")


def check_pairs(points: list[Any], where: str) -> None:
    for i, point in enumerate(points):
        assert isinstance(point, list) and len(point) == 2, f"{where}[{i}]: paire attendue"


# -- fixtures: a real tiny workspace ------------------------------------------------------------
def tiny_experiment() -> dict[str, Any]:
    return {
        "name": "baseline",
        "description": "Expérience minuscule d'intégration",
        "hypothesis": "Le pipeline complet fonctionne de bout en bout",
        "seed": 0,
        "model": {"name": "hidden", "params": {"channels": 8, "enc_blocks": 2, "dec_blocks": 2}},
        "data": {"train": {"dataset": "synthetic",
                           "params": {"modality": "image", "size": 32, "num_items": 16}},
                 "val": {"dataset": "synthetic",
                         "params": {"modality": "image", "size": 32, "num_items": 8, "seed": 1}},
                 "batch_size": 4, "num_workers": 0},
        "optimizer": {"name": "adamw", "lr": 1.0e-3},
        "loss": {"terms": {"msg": {"name": "message_bce", "weight": 1.0},
                           "img": {"name": "mse", "weight": 10.0}}},
        "phases": [
            {"name": "warmup", "steps": 4, "lr": {"schedule": "linear_warmup", "warmup_steps": 2},
             "loss_weights": {"img": 0.1}, "attacks": []},
            {"name": "robust", "steps": 4, "loss_weights": {"img": {"ramp": [0.1, 10.0]}},
             "attacks": [{"name": "identity", "p": 0.5}, {"name": "jpeg_diff"},
                         {"name": "gaussian_noise", "params": {"std": 0.05}}],
             "attack_strength": {"ramp": [0.0, 1.0]}},
        ],
        "eval": {"every_steps": 4, "val_batches": 2,
                 "val_attacks": [{"label": "jpeg50", "name": "jpeg", "params": {"quality": 50}}],
                 "final_benchmark": {"benchmark": "tiny_v1", "checkpoints": ["last"]}},
        "checkpoint": {"every_steps": 4, "keep_last": 2, "keep_best": {"val/bit_acc_mean": "max"}},
        "samples": {"every_steps": 4, "count": 1},
        "resources": {"device": "cpu"},
    }


def tiny_benchmark() -> dict[str, Any]:
    return {"name": "tiny_v1", "version": 1, "modality": "image", "description": "Suite minuscule",
            "data": {"dataset": "synthetic",
                     "params": {"modality": "image", "size": 32, "num_items": 8, "seed": 5}},
            "batch_size": 4, "quality_metrics": ["psnr", "ssim"], "fpr": [1.0e-2, 5.0e-3],
            "attacks": [{"label": "none", "name": "identity"},
                        {"label": "jpeg50", "name": "jpeg", "params": {"quality": 50}},
                        {"label": "noise", "name": "gaussian_noise", "params": {"std": 0.05}}]}


def tiny_plan() -> dict[str, Any]:
    return {"name": "sweep", "experiment": "baseline", "description": "Balayage minuscule",
            "sampler": {"name": "random", "params": {"seed": 0}}, "pruner": {"name": "none"},
            "objectives": [{"metric": "val/bit_acc_mean", "direction": "maximize"}],
            "space": {"optimizer.lr": {"type": "float", "low": 1.0e-4, "high": 1.0e-2,
                                       "log": True},
                      "model.params.channels": {"type": "categorical", "choices": [4, 8]}},
            "fidelity": {"steps_scale": 0.5, "overrides": {"eval.final_benchmark": None}},
            "budget": {"max_trials": 3, "parallel": 1}}


WORKSPACE_YAML = """\
name: "Workspace d'intégration"
budget:
  total_eur: 1000
  start: "2026-10-01"
  end: "2027-09-30"
  manual_entries: []
data_dir: .wmlab
sync_interval_s: 30
default_host: local
"""


# The ``workspace`` fixture of conftest is function-scoped; the module-scoped equivalent below
# builds the same layout once for the whole module (training is the slow part).
def _make_module_workspace(root: Path) -> None:
    for sub in ("projects", "datasets", "benchmarks", "lab"):
        (root / sub).mkdir(parents=True)
    (root / "workspace.yaml").write_text(WORKSPACE_YAML, encoding="utf-8")
    (root / "lab" / "__init__.py").write_text("", encoding="utf-8")
    project = root / "projects" / PROJECT
    write_yaml(project / "project.yaml", {
        "name": PROJECT, "title": "Image minuscule", "modality": "image", "message_bits": 8,
        "default_benchmark": "tiny_v1", "description": "Projet d'intégration",
        "objectives": {"quality": {"metric": "psnr", "constraint": ">= 10"},
                       "robustness": {"metric": "bit_acc_mean"}}, "tags": ["test"]})
    write_yaml(project / "experiments" / "baseline.yaml", tiny_experiment())
    write_yaml(project / "hpo" / "sweep.yaml", tiny_plan())
    (project / "notes.md").write_text("# Notes\n", encoding="utf-8")
    write_yaml(root / "benchmarks" / "tiny_v1.yaml", tiny_benchmark())
    write_yaml(root / "datasets" / "toy.yaml", {
        "name": "toy", "modality": "image", "description": "Catalogue de test",
        "source": "https://example.org", "license": "CC0",
        "paths": {"default": "~/datasets/toy"}})


class Data:
    """Ids of the real artefacts created for the module."""

    run_id: str
    branch_id: str
    study_id: str
    root: Path


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Data]:
    import os

    root = (tmp_path_factory.mktemp("integration") / "workspace").resolve()
    _make_module_workspace(root)
    saved = {k: os.environ.get(k) for k in ("WMLAB_WORKSPACE", "WMLAB_DATA", "WMLAB_HOST",
                                            "WMLAB_PRICE_EUR_PER_HOUR")}
    os.environ["WMLAB_WORKSPACE"] = str(root)
    os.environ["WMLAB_DATA"] = str(root / ".wmlab")
    os.environ.pop("WMLAB_HOST", None)
    os.environ["WMLAB_PRICE_EUR_PER_HOUR"] = "7200"  # 2 €/s: costs stay visible after rounding
    cwd = os.getcwd()
    os.chdir(root)
    try:
        out = Data()
        out.root = root
        spec = f"projects/{PROJECT}/experiments/baseline.yaml"
        train_from_spec(spec, smoke=True, run_id="int-run-a", overrides={"seed": "1"},
                        device="cpu")
        out.run_id = "int-run-a"
        # a run branched from the phase_end checkpoint of the first one
        train_from_spec(spec, smoke=True, run_id="int-run-b", device="cpu", overrides={
            "init_from": json.dumps({"run": "int-run-a", "checkpoint": "phase_end:warmup",
                                     "start_phase": "robust"}),
            "tags": '["branch"]'})
        out.branch_id = "int-run-b"
        evaluate_checkpoint("int-run-a", "last", "tiny_v1", device="cpu")          # full
        evaluate_checkpoint("int-run-a", "last", "tiny_v1", device="cpu", smoke=True)  # smoke
        result = run_study(f"projects/{PROJECT}/hpo/sweep.yaml", study_id="int-study",
                           smoke=True, device="cpu")
        assert result["status"] == "completed", result
        out.study_id = "int-study"
        yield out
    finally:
        os.chdir(cwd)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def client(data: Data) -> Iterator[TestClient]:
    app = create_app(data.root, TOKEN, background=False)
    with TestClient(app, headers={"X-WMLab-Token": TOKEN}) as client:
        yield client


def get(client: TestClient, url: str, **params: Any) -> Any:
    response = client.get(url, params=params or None)
    assert response.status_code == 200, f"{url}: {response.status_code} {response.text[:300]}"
    return response.json()


# -- engine artefacts -----------------------------------------------------------------------------
def test_engine_artifacts(data: Data) -> None:
    run = RunReader(data.root / ".wmlab" / "runs" / data.run_id)
    manifest = run.manifest()
    assert manifest["status"] == "completed" and manifest["price_eur_per_hour"] == 7200
    assert manifest["cost_eur"] >= 0 and "smoke" in manifest["tags"]  # < 1 s ⇒ may be 0
    names = {c["name"] for c in run.checkpoints()}
    assert names and any("phase_end:warmup" in c["tags"] for c in run.checkpoints())
    files = sorted(p.name for p in (run.dir / "eval").glob("*.json"))
    # the final benchmark of a smoke run and the smoke eval share the __smoke suffix; the full
    # evaluation of `last` keeps the plain name
    assert any(f.endswith("__tiny_v1.json") for f in files)
    assert any(f.endswith("__tiny_v1__smoke.json") for f in files)
    branch = RunReader(data.root / ".wmlab" / "runs" / data.branch_id).manifest()
    assert branch["parent"]["run_id"] == data.run_id
    assert branch["parent"]["start_phase"] == "robust" and branch["status"] == "completed"
    assert "branch" in branch["tags"]
    study = read_json(data.root / ".wmlab" / "studies" / data.study_id / "study.json")
    assert study["n_complete"] == 3 and study["best"]


# -- every GET route of §9 ----------------------------------------------------------------------
def test_health_and_auth(client: TestClient, data: Data) -> None:
    health = get(client, "/api/health")
    check(health, {"ok": bool, "version": str, "hosted": bool, "registration": bool, "workspace": str})
    assert health["ok"] is True and health["workspace"] == str(data.root)
    assert client.get("/api/overview", headers={"X-WMLab-Token": ""}).status_code == 401


def test_overview(client: TestClient, data: Data) -> None:
    overview = get(client, "/api/overview")
    check(overview, OVERVIEW)
    assert overview["counts"]["projects"] == 1 and overview["counts"]["studies"] == 1
    assert overview["counts"]["runs"] == 2  # trial runs excluded
    assert {r["run_id"] for r in overview["recent"]} >= {data.run_id, data.branch_id}
    assert overview["budget"]["spent_eur"] >= 0
    assert overview["budget"]["remaining_eur"] == pytest.approx(
        overview["budget"]["total_eur"] - overview["budget"]["spent_eur"])
    project = overview["projects"][0]
    assert project["name"] == PROJECT and project["best"] is not None
    assert project["best"]["source"] == "benchmark" and project["best"]["run_id"] == data.run_id


def test_projects(client: TestClient, data: Data) -> None:
    projects = get(client, "/api/projects")
    check(projects, [PROJECT_SUMMARY])
    assert projects[0]["n_experiments"] == 1 and projects[0]["n_hpo_plans"] == 1
    assert projects[0]["n_runs"] == 2 and projects[0]["cost_eur"] >= 0
    detail = get(client, f"/api/projects/{PROJECT}")
    check(detail, PROJECT_DETAIL)
    exp = detail["experiments"][0]
    assert exp["valid"] and exp["n_phases"] == 2 and exp["total_steps"] == 8
    assert exp["model"] == "hidden" and exp["n_runs"] == 2
    plan = detail["hpo_plans"][0]
    assert plan["valid"] and plan["n_params"] == 2 and plan["n_studies"] == 1
    assert detail["notes"].startswith("# Notes")
    assert client.get("/api/projects/nope").status_code == 404


def test_files_registry_validate(client: TestClient) -> None:
    path = f"projects/{PROJECT}/experiments/baseline.yaml"
    file = get(client, "/api/files", path=path)
    check(file, FILE_RESPONSE)
    assert file["kind"] == "experiment" and file["validation"]["valid"]
    registry = get(client, "/api/registry")
    check(registry, REGISTRY_RESPONSE)
    assert set(registry["entries"]) == {"model", "attack", "dataset", "preprocess", "loss",
                                        "metric"}
    assert {e["name"] for e in registry["entries"]["model"]} >= {"hidden", "audio_conv",
                                                                  "video_framewise"}
    validation = client.post("/api/validate", json={"content": file["content"],
                                                    "path": path}).json()
    check(validation, {"valid": bool, "errors": [VALIDATION_ERROR],
                       "plan": (PHASE_PLAN_PREVIEW, None)})
    assert validation["valid"] and validation["plan"]["total_steps"] == 8
    for phase in validation["plan"]["phases"]:
        check_pairs(phase["lr"], "lr")
        check_pairs(phase["attack_strength"], "attack_strength")


def test_file_creation(client: TestClient, data: Data) -> None:
    """PUT /api/files creates new files under the writable roots (benchmark copy, dataset,
    project notes) and refuses the others."""
    bench = yaml.safe_load((data.root / "benchmarks" / "tiny_v1.yaml").read_text())
    bench.update(name="tiny_v2", version=2)
    saved = client.put("/api/files", json={"path": "benchmarks/tiny_v2.yaml",
                                           "content": yaml.safe_dump(bench)}).json()
    check(saved, {"saved": bool, "validation": {"valid": bool, "errors": [VALIDATION_ERROR]},
                  "git": ({"committed": (str, None), "pushed": bool, "error": (str, None)},
                          None)})
    assert saved["saved"] and saved["validation"]["valid"]
    assert saved["git"] is None  # not a git repository / hosted mode off
    assert (data.root / "benchmarks" / "tiny_v2.yaml").is_file()
    assert {b["name"] for b in get(client, "/api/benchmarks")} == {"tiny_v1", "tiny_v2"}
    dataset = {"name": "newset", "modality": "audio", "description": "x", "source": "y",
               "license": "z", "paths": {"default": "~/d"}}
    assert client.put("/api/files", json={"path": "datasets/newset.yaml",
                                          "content": yaml.safe_dump(dataset)}).json()["saved"]
    assert (data.root / "datasets" / "newset.yaml").is_file()
    notes = client.put("/api/files", json={"path": f"projects/{PROJECT}/notes.md",
                                           "content": "# Notes\n\nModifiées\n"}).json()
    assert notes["saved"] and notes["validation"]["valid"]
    assert get(client, f"/api/projects/{PROJECT}")["notes"].endswith("Modifiées\n")
    assert client.put("/api/files", json={"path": "workspace.yaml", "content": "x"}).status_code == 403
    assert client.put("/api/files", json={"path": "../x.yaml", "content": "x"}).status_code == 403


def test_runs(client: TestClient, data: Data) -> None:
    runs = get(client, "/api/runs")
    check(runs, [RUN_SUMMARY])
    ids = [r["run_id"] for r in runs]
    assert data.run_id in ids and data.branch_id in ids and len(runs) == 2
    assert all(r["study"] is None for r in runs)
    trials = get(client, "/api/runs", include_trials="true")
    assert len(trials) == 5 and sum(1 for r in trials if r["study"] == data.study_id) == 3
    assert get(client, "/api/runs", study=data.study_id) and \
        len(get(client, "/api/runs", status="completed,killed", include_trials="true")) == 5
    assert get(client, "/api/runs", project="nope") == []
    branch = next(r for r in runs if r["run_id"] == data.branch_id)
    assert branch["parent"] == {"run_id": data.run_id, "checkpoint": branch["parent"]["checkpoint"],
                                "start_phase": "robust"}
    assert branch["progress"]["step"] == branch["progress"]["total_steps"]
    assert branch["summary"]["val/bit_acc_mean"] >= 0 and branch["duration_s"] is not None
    assert branch["params"]["optimizer.lr"] == 0.001


def test_run_detail(client: TestClient, data: Data) -> None:
    detail = get(client, f"/api/runs/{data.run_id}")
    check(detail, RUN_DETAIL)
    assert detail["spec"]["model"]["name"] == "hidden" and "hidden" in detail["spec_yaml"]
    assert detail["spec_path"].endswith("baseline.yaml") and detail["overrides"] == {"seed": "1"}
    assert detail["plan"]["total_steps"] == detail["progress"]["total_steps"]
    for phase in detail["plan"]["phases"]:
        check_pairs(phase["lr"], "lr")
        for series in phase["loss_weights"].values():
            check_pairs(series, "loss_weights")
    types = {e["type"] for e in detail["events"]}
    assert {"status", "phase_start", "phase_end", "checkpoint", "eval", "sample"} <= types
    assert detail["checkpoints"] and all(c["available_locally"] for c in detail["checkpoints"])
    assert detail["evals"] and all(e["benchmark"] == "tiny_v1" for e in detail["evals"])
    assert any("psnr" in e["quality"] and "bit_acc_mean" in e["summary"] for e in detail["evals"])
    assert detail["samples"] and {s["group"] for s in detail["samples"]} >= {"original",
                                                                             "watermarked"}
    assert "train/loss" in detail["metric_keys"] and "val/bit_acc_mean" in detail["metric_keys"]
    assert set(detail["datasets"]) >= {"train", "val"} and detail["price_eur_per_hour"] == 7200
    assert detail["env"]["hardware"] is not None and detail["env"]["command"]
    assert client.get("/api/runs/nope").status_code == 404


def test_run_series_logs_files(client: TestClient, data: Data) -> None:
    metrics = get(client, f"/api/runs/{data.run_id}/metrics")
    check(metrics, METRICS_RESPONSE)
    assert "train/loss" in metrics["series"] and len(metrics["phases"]) == 2
    subset = get(client, f"/api/runs/{data.run_id}/metrics", keys="val/psnr,train/lr",
                 max_points=3)
    assert set(subset["series"]) == {"val/psnr", "train/lr"}
    assert all(len(s["steps"]) <= 3 and len(s["steps"]) == len(s["values"])
               for s in subset["series"].values())
    system = get(client, f"/api/runs/{data.run_id}/system")
    check(system, SYSTEM_RESPONSE)
    logs = get(client, f"/api/runs/{data.run_id}/logs", tail=20)
    check(logs, {"lines": [str]})
    assert logs["lines"] and len(logs["lines"]) <= 20
    sample = get(client, f"/api/runs/{data.run_id}")["samples"][0]
    response = client.get(f"/api/runs/{data.run_id}/file", params={"path": sample["path"]},
                          headers={"X-WMLab-Token": ""}, follow_redirects=True)
    assert response.status_code == 401
    response = client.get(f"/api/runs/{data.run_id}/file",
                          params={"path": sample["path"], "token": TOKEN},
                          headers={"X-WMLab-Token": ""})
    assert response.status_code == 200 and response.content[:4] == b"\x89PNG"
    assert client.get(f"/api/runs/{data.run_id}/file", params={"path": "../run.json"}
                      ).status_code in (400, 403, 404)


def test_compare(client: TestClient, data: Data) -> None:
    compare = get(client, "/api/compare", runs=f"{data.run_id},{data.branch_id}",
                  keys="val/psnr,val/bit_acc_mean", max_points=10)
    check(compare, COMPARE_RESPONSE)
    assert {r["run_id"] for r in compare["runs"]} == {data.run_id, data.branch_id}
    assert set(compare["series"]) == {data.run_id, data.branch_id}
    assert set(compare["series"][data.run_id]) == {"val/psnr", "val/bit_acc_mean"}


def test_studies(client: TestClient, data: Data) -> None:
    studies = get(client, "/api/studies")
    check(studies, [STUDY_SUMMARY])
    assert [s["study_id"] for s in studies] == [data.study_id]
    summary = studies[0]
    assert summary["n_trials"] == 3 and summary["n_complete"] == 3 and summary["status"] == "completed"
    assert summary["budget"]["max_trials"] == 3 and summary["budget"]["parallel"] == 1
    assert summary["best"] and summary["cost_eur"] >= 0
    assert get(client, "/api/studies", project="nope") == []
    detail = get(client, f"/api/studies/{data.study_id}")
    check(detail, STUDY_DETAIL)
    assert len(detail["trials"]) == 3 and all(t["state"] == "COMPLETE" for t in detail["trials"])
    for trial in detail["trials"]:
        check_pairs(trial["intermediate"], "intermediate")
        assert set(trial["params"]) == {"optimizer.lr", "model.params.channels"}
        assert trial["run_id"] and trial["values"] and trial["duration_s"] > 0
    assert detail["pareto"] == [detail["best"][0]["trial"]]
    assert detail["spec"]["name"] == "sweep"
    assert client.get("/api/studies/nope").status_code == 404


def test_leaderboard(client: TestClient, data: Data) -> None:
    board = get(client, "/api/leaderboard")
    check(board, LEADERBOARD)
    assert board["benchmarks"] == ["tiny_v1"]
    bench_rows = [r for r in board["rows"] if r["source"] == "benchmark"]
    val_rows = [r for r in board["rows"] if r["source"] == "val"]
    # only the full `wmlab eval` of run a: the final benchmarks of smoke runs and the smoke
    # evaluation are excluded from the leaderboard
    assert [r["run_id"] for r in bench_rows] == [data.run_id]
    assert all(r["checkpoint"] and r["benchmark"] == "tiny_v1" for r in bench_rows)
    # validation rows leave out smoke runs (both fixture runs are smoke) and trial runs
    assert val_rows == []
    assert "psnr" in board["quality_metrics"] and "bit_acc" in board["robustness_metrics"]
    assert "jpeg50" in board["attacks"]
    only_bench = get(client, "/api/leaderboard", project=PROJECT, benchmark="tiny_v1",
                     source="benchmark")
    assert only_bench["rows"] and all(r["source"] == "benchmark" for r in only_bench["rows"])
    assert get(client, "/api/leaderboard", project="nope")["rows"] == []


def test_catalog_routes(client: TestClient) -> None:
    benchmarks = get(client, "/api/benchmarks")
    check(benchmarks, [BENCHMARK_INFO])
    tiny = next(b for b in benchmarks if b["name"] == "tiny_v1")
    assert tiny["valid"] and tiny["n_items"] == 8 and [a["label"] for a in tiny["attacks"]] == [
        "none", "jpeg50", "noise"]
    datasets = get(client, "/api/datasets")
    check(datasets, [DATASET_INFO])
    assert {d["name"] for d in datasets} >= {"toy"}
    hosts = get(client, "/api/hosts")
    check(hosts, [HOST])
    assert hosts[0]["name"] == "local" and hosts[0]["kind"] == "local"
    assert hosts[0]["active_jobs"] == 0 and hosts[0]["decommissioned_at"] is None
    settings = get(client, "/api/settings")
    check(settings, SETTINGS)
    assert settings["data_dir"] == ".wmlab" and settings["default_host"] == "local"
    assert settings["hosted"]["enabled"] is False and settings["hosted"]["git_branch"] == "main"
    jobs = get(client, "/api/jobs")
    check(jobs, [JOB])


def test_hosted_routes(client: TestClient, data: Data) -> None:
    """§12: identity, git state, inventory and instance routes on a plain (non-git,
    non-hosted) workspace."""
    me = get(client, "/api/me")
    check(me, ME)
    assert me == {"id": "", "name": "admin", "email": "", "role": "admin", "hosted": False,
                  "public_url": None, "must_change_password": False, "auth": "server_token"}
    users = get(client, "/api/users")
    check(users, [{"id": str, "name": str, "email": str, "role": str, "status": str,
                   "must_change_password": bool, "created_at": str, "approved_at": (str, None),
                   "approved_by": (str, None), "disabled_at": (str, None),
                   "last_login_at": (str, None)}])
    git = get(client, "/api/git")
    check(git, GIT_STATE)
    assert git["available"] is False and git["hosted"] is False
    assert client.post("/api/git/pull").status_code == 400
    inventory = get(client, "/api/hosts/local/inventory")
    check(inventory, HOST_INVENTORY)
    assert inventory["reachable"] and inventory["unretrieved_checkpoints"] == 0
    assert {r["run_id"] for r in inventory["runs"]} >= {data.run_id, data.branch_id}
    assert all(r["checkpoints_total"] == r["checkpoints_local"] for r in inventory["runs"])
    hosts = get(client, "/api/hosts")
    assert hosts[0]["inventory_at"] == inventory["checked_at"]
    assert hosts[0]["unretrieved_checkpoints"] == 0
    check(get(client, "/api/hosts/local/instance"), INSTANCE)
    assert get(client, "/api/hosts/local/instance")["configured"] is False
    assert client.post("/api/hosts/local/retrieve").status_code == 400
    assert client.post("/api/hosts/local/decommission", json={"confirm": True}).status_code == 400
    assert client.post("/api/hosts/local/poweroff").status_code == 400
    assert client.get("/api/hosts/nope/inventory").status_code == 404
    runs = get(client, "/api/runs")
    assert all(r["user"] is None for r in runs)  # trained outside the app


def test_costs(client: TestClient, data: Data) -> None:
    costs = get(client, "/api/costs")
    check(costs, COSTS)
    kinds = {i["kind"] for i in costs["items"]}
    assert {"run", "study", "eval"} <= kinds
    run_items = [i for i in costs["items"] if i["kind"] == "run"]
    assert {i["id"] for i in run_items} == {data.run_id, data.branch_id}  # trials excluded
    assert costs["spent_eur"] == pytest.approx(sum(i["eur"] for i in costs["items"]), abs=0.01)
    assert costs["remaining_eur"] == pytest.approx(costs["total_eur"] - costs["spent_eur"])
    assert costs["by_project"][0]["project"] == PROJECT and costs["by_host"][0]["host"] == "local"
    assert costs["by_month"] and costs["budget"]["total_eur"] == 1000
    response = client.get("/api/costs.csv")
    assert response.status_code == 200 and "text/csv" in response.headers["content-type"]
    rows = list(csv.reader(io.StringIO(response.text.lstrip("﻿")), delimiter=";"))
    assert len(rows) == len(costs["items"]) + 1 and len(rows[0]) >= 6


def test_estimate_experiment_and_hpo(client: TestClient, data: Data) -> None:
    exp = client.post("/api/jobs/estimate", json={
        "spec_path": f"projects/{PROJECT}/experiments/baseline.yaml", "host": "local"}).json()
    check(exp, ESTIMATE)
    assert exp["total_steps"] == 8 and exp["basis"]  # no long run yet ⇒ hours may be null
    hpo = client.post("/api/jobs/estimate", json={
        "spec_path": f"projects/{PROJECT}/hpo/sweep.yaml", "host": "local"})
    assert hpo.status_code == 200, hpo.text
    hpo = hpo.json()
    check(hpo, ESTIMATE)
    assert hpo["total_steps"] == 4 * 3  # 8 steps × steps_scale 0.5 × 3 trials
    assert hpo["basis"] and (hpo["est_hours"] is None) == (hpo["est_eur"] is None)
    bad = client.post("/api/jobs/estimate", json={"spec_path": "benchmarks/tiny_v1.yaml",
                                                  "host": "local"})
    assert bad.status_code == 400


def test_hpo_job_accepts_overrides(client: TestClient, data: Data) -> None:
    """The server forwards overrides and max_hours to `wmlab hpo` (job stays queued: the
    manager runs no thread in this test app, so nothing is launched)."""
    from wmlab.server.app import ServerState

    state: ServerState = client.app.state.wmlab
    job = state.jobs.submit(kind="hpo", host="local",
                            spec_path=f"projects/{PROJECT}/hpo/sweep.yaml",
                            overrides={"seed": "4"}, max_hours=0.1, smoke=True)
    try:
        assert job.overrides == {"seed": "4"} and job.max_hours == 0.1
        args = job.args
        assert args[args.index("--set") + 1] == "seed=4"
        assert args[args.index("--max-hours") + 1] == "0.1" and "--smoke" in args
    finally:
        state.jobs.cancel(job.job_id)


def test_reindex_matches_live_index(data: Data) -> None:
    counts = reindex_workspace(data.root)
    assert counts["runs"] == 5 and counts["études"] == 1 and counts["évaluations"] >= 2


# -- process-group signals with DataLoader workers (real subprocesses) ---------------------------
def _cli_env(data: Data) -> dict[str, str]:
    import os

    return {**os.environ, "WMLAB_WORKSPACE": str(data.root), "WMLAB_DATA": str(data.root / ".wmlab"),
            "PYTHONUNBUFFERED": "1"}


def test_group_sigterm_with_loader_workers(data: Data) -> None:
    """Cancelling = SIGTERM to the whole process group (app behaviour): the DataLoader workers
    die too, and the run must still write its emergency checkpoint and end ``killed``."""
    import os
    import signal
    import subprocess
    import sys
    import time

    from wmlab.tracking import read_jsonl

    argv = [sys.executable, "-m", "wmlab.cli", "train", f"projects/{PROJECT}/experiments/baseline.yaml",
            "--device", "cpu", "--run-id", "int-sig", "--set", "phases.0.steps=100000",
            "--set", "data.num_workers=2", "--set", "eval.every_steps=100000",
            "--set", "samples.every_steps=100000", "--set", "checkpoint.every_steps=100000"]
    proc = subprocess.Popen(argv, cwd=data.root, env=_cli_env(data), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    run_dir = data.root / ".wmlab" / "runs" / "int-sig"
    deadline = time.time() + 90
    try:
        while time.time() < deadline and proc.poll() is None:
            if any("train/loss" in r["values"] for r in read_jsonl(run_dir / "metrics.jsonl")):
                break
            time.sleep(0.2)
        assert proc.poll() is None, proc.stdout.read() if proc.stdout else ""
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        output, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    manifest = RunReader(run_dir).manifest()
    assert manifest["status"] == "killed" and manifest["error"] == "SIGTERM reçu", output
    assert "DataLoader worker" not in output, output
    checkpoints = RunReader(run_dir).checkpoints()
    assert checkpoints and "last" in checkpoints[-1]["tags"], output


def test_study_worker_exits_with_loader_workers(data: Data) -> None:
    """A study whose trials use DataLoader workers must finish (the worker process used to
    hang at exit when the loader workers ignored SIGTERM)."""
    import subprocess
    import sys

    argv = [sys.executable, "-m", "wmlab.cli", "hpo", f"projects/{PROJECT}/hpo/sweep.yaml",
            "--device", "cpu", "--study-id", "int-study-workers", "--max-trials", "1",
            "--set", "data.num_workers=2"]
    result = subprocess.run(argv, cwd=data.root, env=_cli_env(data), capture_output=True,
                            text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr
    study = read_json(data.root / ".wmlab" / "studies" / "int-study-workers" / "study.json")
    assert study["status"] == "completed" and study["n_complete"] == 1, result.stdout
