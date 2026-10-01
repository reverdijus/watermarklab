"""Hosted mode §12.5: Scaleway Instance API client (``httpx.MockTransport``) and the
instance / poweroff / decommission routes that use it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from helpers_server import job_status_of, populate, wait_for
from wmlab.server.app import create_app
from wmlab.server.jobs import JobError
from wmlab.server.scaleway import ScalewayClient, ScalewayError, instance_view
from wmlab.spec import ScalewayRef

ZONE = "fr-par-2"
SERVER = "11111111-2222-3333-4444-555555555555"
SPEC = "projects/demo/experiments/baseline.yaml"
TOKEN = "t"


class FakeScaleway:
    """In-memory Instance API: one server whose state follows the actions."""

    def __init__(self, state: str = "running", key: str = "scw-secret") -> None:
        self.key = key
        self.state: str | None = state
        self.requests: list[tuple[str, str, Any]] = []
        self.volumes = {"0": {"id": "vol-local", "name": "root", "volume_type": "l_ssd",
                              "size": 100, "boot": True},
                        "1": {"id": "vol-block", "name": "data", "volume_type": "b_ssd",
                              "size": 500, "boot": False},
                        "2": {"id": "vol-sbs", "name": "sbs", "volume_type": "sbs_volume",
                              "size": 200, "boot": False}}
        self.deleted_volumes: list[str] = []
        self.fail_actions = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.headers.get("X-Auth-Token") != self.key:
            return httpx.Response(401, json={"message": "invalid auth token", "type": "denied_authentication"})
        prefix = f"/instance/v1/zones/{ZONE}/servers/{SERVER}"
        if request.method == "GET" and request.url.path == prefix:
            if self.state is None:
                return httpx.Response(404, json={"message": "server not found", "type": "not_found"})
            return httpx.Response(200, json={"server": {
                "id": SERVER, "name": "gpu-1", "state": self.state, "commercial_type": "L40S-1-48G",
                "zone": ZONE, "volumes": self.volumes}})
        if request.method == "POST" and request.url.path == f"{prefix}/action":
            if self.fail_actions:
                return httpx.Response(400, json={"message": "server is locked", "type": "invalid_request_error"})
            action = body["action"]
            if action == "poweroff":
                self.state = "stopped"
                self.volumes.pop("0", None)  # local volumes survive a stop too, but keep it simple
                self.volumes["0"] = {"id": "vol-local", "name": "root", "volume_type": "l_ssd",
                                     "size": 100, "boot": True}
            elif action == "terminate":
                self.state = None
                self.volumes.pop("0", None)  # local volumes go with the server
            elif action == "poweron":
                self.state = "running"
            return httpx.Response(202, json={"task": {"id": "task-1", "description": action,
                                                      "status": "pending", "progress": 0}})
        if request.method == "DELETE" and request.url.path.startswith(f"/instance/v1/zones/{ZONE}/volumes/"):
            self.deleted_volumes.append(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(204)
        if request.method == "DELETE" and request.url.path.startswith(f"/block/v1alpha1/zones/{ZONE}/volumes/"):
            self.deleted_volumes.append("sbs:" + request.url.path.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(404, json={"message": "no route", "type": "not_found"})


def client_for(fake: FakeScaleway, key: str = "scw-secret") -> ScalewayClient:
    return ScalewayClient(key, transport=fake.transport(), sleep=lambda _: None, wait_interval=0)


# -- client ---------------------------------------------------------------------------------------
def test_get_server_and_headers():
    fake = FakeScaleway()
    client = client_for(fake)
    server = client.get_server(ZONE, SERVER)
    assert server["id"] == SERVER and server["state"] == "running"
    assert server["commercial_type"] == "L40S-1-48G" and server["zone"] == ZONE
    assert [v["id"] for v in server["volumes"]] == ["vol-local", "vol-block", "vol-sbs"]
    assert fake.requests == [("GET", f"/instance/v1/zones/{ZONE}/servers/{SERVER}", None)]
    assert client.server_state(ZONE, SERVER) == "running"
    fake.state = None
    assert client.server_state(ZONE, SERVER) == "deleted"
    with pytest.raises(ScalewayError, match="introuvable"):
        client.get_server(ZONE, SERVER)
    client.close()


def test_errors_are_502_in_french():
    fake = FakeScaleway()
    wrong = client_for(fake, key="wrong")
    with pytest.raises(ScalewayError) as exc:
        wrong.get_server(ZONE, SERVER)
    assert exc.value.status == 502 and "clé refusée" in exc.value.message
    assert isinstance(exc.value, JobError)
    with pytest.raises(ScalewayError, match="Clé Scaleway absente"):
        ScalewayClient("", transport=fake.transport())
    client = client_for(fake)
    with pytest.raises(ScalewayError, match="Action Scaleway inconnue"):
        client.server_action(ZONE, SERVER, "explode")
    fake.fail_actions = True
    with pytest.raises(ScalewayError, match="erreur 400"):
        client.server_action(ZONE, SERVER, "poweroff")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("réseau coupé")

    down = ScalewayClient("k", transport=httpx.MockTransport(boom), sleep=lambda _: None)
    with pytest.raises(ScalewayError, match="injoignable"):
        down.get_server(ZONE, SERVER)
    assert instance_view(down, ScalewayRef(zone=ZONE, server_id=SERVER))["error"]
    assert instance_view(None, None) == {"configured": False, "state": None,
                                         "commercial_type": None, "zone": None, "error": None}
    assert "SCW_SECRET_KEY" in instance_view(None, ScalewayRef(zone=ZONE, server_id=SERVER))["error"]


def test_poweroff_waits_for_stopped():
    fake = FakeScaleway()
    client = client_for(fake)
    assert client.poweroff(ZONE, SERVER) == "stopped"
    actions = [r for r in fake.requests if r[0] == "POST"]
    assert actions == [("POST", f"/instance/v1/zones/{ZONE}/servers/{SERVER}/action",
                        {"action": "poweroff"})]
    assert fake.volumes  # volumes are kept
    assert client.poweroff(ZONE, SERVER) == "stopped"  # idempotent: no second action
    assert len([r for r in fake.requests if r[0] == "POST"]) == 1
    fake.state = "locked"
    with pytest.raises(ScalewayError, match="impossible d'éteindre"):
        client.poweroff(ZONE, SERVER)
    fake.state = None
    with pytest.raises(ScalewayError, match="n'existe plus"):
        client.poweroff(ZONE, SERVER)


def test_wait_timeout():
    fake = FakeScaleway(state="stopping")
    client = client_for(fake)
    with pytest.raises(ScalewayError, match="toujours « stopping »"):
        client.wait_for(ZONE, SERVER, ("stopped",), timeout=0.0)


def test_delete_server_terminates_then_deletes_volumes():
    fake = FakeScaleway()
    client = client_for(fake)
    result = client.delete_server(ZONE, SERVER)
    assert result["state"] == "deleted"
    assert result["deleted_volumes"] == ["vol-local", "vol-block", "vol-sbs"]
    assert ("POST", f"/instance/v1/zones/{ZONE}/servers/{SERVER}/action",
            {"action": "terminate"}) in fake.requests
    # instance volumes through the Instance API, SBS volumes through the Block Storage API
    assert fake.deleted_volumes == ["vol-local", "vol-block", "sbs:vol-sbs"]
    assert client.delete_server(ZONE, SERVER) == {"state": "deleted", "deleted_volumes": []}
    fake2 = FakeScaleway()
    kept = client_for(fake2).delete_server(ZONE, SERVER, with_volumes=False)
    assert kept["deleted_volumes"] == [] and fake2.deleted_volumes == []


def test_from_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SCW_SECRET_KEY", raising=False)
    assert ScalewayClient.from_env() is None
    monkeypatch.setenv("SCW_SECRET_KEY", "scw-secret")
    fake = FakeScaleway()
    client = ScalewayClient.from_env(transport=fake.transport())
    assert client is not None and client.get_server(ZONE, SERVER)["state"] == "running"


# -- routes ---------------------------------------------------------------------------------------
@pytest.fixture
def hosted(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, FakeScaleway, Any]:
    from helpers_server import FakeRunner

    populate(workspace)
    monkeypatch.setenv("SCW_SECRET_KEY", "scw-secret")
    fake = FakeScaleway()
    runner = FakeRunner()
    app = create_app(workspace, TOKEN, background=False, runner=runner)
    app.state.wmlab.scaleway_transport = fake.transport()
    client = TestClient(app, headers={"X-WMLab-Token": TOKEN})
    hosts = client.get("/api/hosts").json()
    hosts[1]["scaleway"] = {"zone": ZONE, "server_id": SERVER}
    assert client.put("/api/hosts", json=hosts).status_code == 200
    return client, fake, runner


def test_instance_route(hosted: tuple[TestClient, FakeScaleway, Any]):
    client, fake, _ = hosted
    body = client.get("/api/hosts/scw/instance").json()
    assert body == {"configured": True, "state": "running", "commercial_type": "L40S-1-48G",
                    "zone": ZONE, "error": None}
    fake.state = None
    gone = client.get("/api/hosts/scw/instance").json()
    assert gone["configured"] and gone["state"] is None and "introuvable" in gone["error"]
    assert client.get("/api/hosts/local/instance").json()["configured"] is False
    fake.key = "rotated"
    denied = client.get("/api/hosts/scw/instance").json()
    assert "clé refusée" in denied["error"]  # never an exception, never the key itself
    assert "scw-secret" not in json.dumps(denied)


def test_poweroff_route(hosted: tuple[TestClient, FakeScaleway, Any]):
    client, fake, _ = hosted
    body = client.post("/api/hosts/scw/poweroff")
    assert body.status_code == 200, body.text
    assert body.json() == {"ok": True, "instance_state": "stopped"}
    assert fake.state == "stopped"
    fake.state = "locked"
    failed = client.post("/api/hosts/scw/poweroff")
    assert failed.status_code == 502 and "impossible d'éteindre" in failed.json()["detail"]


def test_decommission_deletes_the_instance(hosted: tuple[TestClient, FakeScaleway, Any]):
    client, fake, runner = hosted
    runner.fail_ssh = True  # empty machine, but unreachable: force the retirement
    body = client.post("/api/hosts/scw/decommission",
                       json={"confirm": True, "force": True, "delete_instance": True})
    assert body.status_code == 200, body.text
    body = body.json()
    assert body["ok"] and body["deleted_instance"] is True and body["instance_state"] == "deleted"
    assert "supprimée" in body["message"] and "3 volume(s)" in body["message"]
    assert fake.state is None and fake.deleted_volumes == ["vol-local", "vol-block", "sbs:vol-sbs"]
    scw = next(h for h in client.get("/api/hosts").json() if h["name"] == "scw")
    assert scw["decommissioned_at"] and scw["scaleway"] == {"zone": ZONE, "server_id": SERVER}


def test_decommission_reports_instance_state_without_deleting(hosted):
    client, fake, runner = hosted
    runner.fail_ssh = True
    fake.state = "stopped"
    body = client.post("/api/hosts/scw/decommission", json={"confirm": True, "force": True}).json()
    assert body["deleted_instance"] is False and body["instance_state"] == "stopped"
    assert fake.state == "stopped" and fake.deleted_volumes == []


def test_decommission_api_failure_keeps_the_host(hosted):
    client, fake, runner = hosted
    runner.fail_ssh = True
    fake.fail_actions = True
    failed = client.post("/api/hosts/scw/decommission",
                         json={"confirm": True, "force": True, "delete_instance": True})
    assert failed.status_code == 502 and "API Scaleway" in failed.json()["detail"]
    scw = next(h for h in client.get("/api/hosts").json() if h["name"] == "scw")
    assert scw["decommissioned_at"] is None
    job = client.post("/api/jobs", json={"kind": "train", "host": "scw", "spec_path": SPEC})
    assert job.status_code == 200  # still usable
    runner.fail_ssh = False
    assert wait_for(lambda: job_status_of(client, job.json()["job_id"])["status"] in
                    ("running", "failed"))
