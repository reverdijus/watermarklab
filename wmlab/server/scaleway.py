"""Minimal Scaleway Instance API client (ARCHITECTURE.md §12.5).

Only what the hosted mode needs: read a server, power it off, terminate it and delete the
volumes left behind. The secret key comes from ``SCW_SECRET_KEY`` in the server environment
and is never stored in ``hosts.yaml``, logged or returned by the API. Errors are raised as
:class:`ScalewayError` (a ``JobError`` with status 502 and a French message).

Endpoints (Instance API v1, base ``https://api.scaleway.com``):

- ``GET    /instance/v1/zones/{zone}/servers/{id}``            → ``{"server": {...}}``
- ``POST   /instance/v1/zones/{zone}/servers/{id}/action``     body ``{"action": ...}``
  with ``action`` ∈ ``poweron | poweroff | stop_in_place | reboot | terminate | backup``
  → ``{"task": {...}}`` (202)
- ``DELETE /instance/v1/zones/{zone}/volumes/{id}``            (instance volumes)
- ``DELETE /block/v1alpha1/zones/{zone}/volumes/{id}``         (``sbs_volume`` volumes)

``terminate`` deletes the server and its local volumes; other volumes stay and are deleted
afterwards by :meth:`ScalewayClient.delete_server`.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from wmlab.server.jobs import JobError

BASE_URL = "https://api.scaleway.com"
SECRET_ENV = "SCW_SECRET_KEY"
ACTIONS = ("poweron", "poweroff", "stop_in_place", "reboot", "terminate", "backup")
WAIT_TIMEOUT_S = 300.0
WAIT_INTERVAL_S = 5.0
REQUEST_TIMEOUT_S = 30.0
DELETED = "deleted"


class ScalewayError(JobError):
    """Scaleway API failure (HTTP 502 for the app)."""

    def __init__(self, message: str) -> None:
        super().__init__(502, message)


def secret_key_from_env() -> str | None:
    return (os.environ.get(SECRET_ENV) or "").strip() or None


def _instance_path(zone: str, *parts: str) -> str:
    return "/".join(["/instance/v1/zones", zone, *parts])


class ScalewayClient:
    """HTTP client of the Instance API (``httpx``; ``transport`` is injectable for tests)."""

    def __init__(self, secret_key: str, *, base_url: str = BASE_URL, transport: Any = None,
                 timeout: float = REQUEST_TIMEOUT_S, sleep: Callable[[float], None] = time.sleep,
                 wait_interval: float = WAIT_INTERVAL_S) -> None:
        try:
            import httpx
        except ImportError:  # pragma: no cover - depends on the environment
            raise ScalewayError("httpx n'est pas installé : installez l'extra 'server'.") from None
        if not secret_key:
            raise ScalewayError(f"Clé Scaleway absente : définissez {SECRET_ENV} dans "
                                "l'environnement du serveur.")
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport,
                                    headers={"X-Auth-Token": secret_key,
                                             "Accept": "application/json"})
        self._sleep = sleep
        self.wait_interval = wait_interval

    @classmethod
    def from_env(cls, **kwargs: Any) -> "ScalewayClient | None":
        """Client from ``SCW_SECRET_KEY`` (``None`` when the variable is not set)."""
        key = secret_key_from_env()
        return cls(key, **kwargs) if key else None

    def close(self) -> None:
        self._client.close()

    # -- HTTP ------------------------------------------------------------------------------
    def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None,
                 allow: tuple[int, ...] = ()) -> Any:
        import httpx

        try:
            response = self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise ScalewayError(f"API Scaleway injoignable ({method} {path}) : "
                                f"{type(exc).__name__}: {exc}") from None
        if response.status_code in allow:
            return None
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = str(body.get("message") or body.get("type") or body)[:300]
            except ValueError:
                detail = response.text[:300]
            if response.status_code == 401:
                raise ScalewayError(f"API Scaleway : clé refusée (401) — vérifiez {SECRET_ENV}.")
            if response.status_code == 404:
                raise ScalewayError(f"API Scaleway : ressource introuvable ({method} {path}) : "
                                    f"{detail or '404'}")
            raise ScalewayError(f"API Scaleway : erreur {response.status_code} sur {method} "
                                f"{path} : {detail or 'sans détail'}")
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ScalewayError(f"API Scaleway : réponse illisible ({method} {path}).") from None

    # -- servers ---------------------------------------------------------------------------
    @staticmethod
    def _server_view(raw: dict[str, Any]) -> dict[str, Any]:
        volumes = []
        raw_volumes = raw.get("volumes") or {}
        items = raw_volumes.values() if isinstance(raw_volumes, dict) else raw_volumes
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                volumes.append({"id": str(item["id"]), "name": item.get("name"),
                                "volume_type": item.get("volume_type"),
                                "size": item.get("size"), "boot": bool(item.get("boot"))})
        return {"id": str(raw.get("id") or ""), "name": raw.get("name"),
                "state": raw.get("state"), "commercial_type": raw.get("commercial_type"),
                "zone": raw.get("zone"), "volumes": volumes}

    def get_server(self, zone: str, server_id: str) -> dict[str, Any]:
        """``{id, name, state, commercial_type, zone, volumes: [{id, name, volume_type, size, boot}]}``."""
        body = self._request("GET", _instance_path(zone, "servers", server_id))
        server = body.get("server") if isinstance(body, dict) else None
        if not isinstance(server, dict):
            raise ScalewayError("API Scaleway : réponse inattendue pour le serveur.")
        return self._server_view(server)

    def server_state(self, zone: str, server_id: str) -> str:
        """Current state, or ``"deleted"`` when the server no longer exists."""
        try:
            return str(self.get_server(zone, server_id).get("state") or "unknown")
        except ScalewayError as exc:
            if "introuvable" in exc.message:
                return DELETED
            raise

    def server_action(self, zone: str, server_id: str, action: str) -> dict[str, Any] | None:
        """``POST …/servers/{id}/action`` ; returns the task (``{id, status, …}``)."""
        if action not in ACTIONS:
            raise ScalewayError(f"Action Scaleway inconnue : {action}")
        body = self._request("POST", _instance_path(zone, "servers", server_id, "action"),
                             json={"action": action})
        task = body.get("task") if isinstance(body, dict) else None
        return task if isinstance(task, dict) else None

    def delete_volume(self, zone: str, volume_id: str, volume_type: str | None = None) -> None:
        if volume_type == "sbs_volume":
            path = "/".join(["/block/v1alpha1/zones", zone, "volumes", volume_id])
        else:
            path = _instance_path(zone, "volumes", volume_id)
        self._request("DELETE", path, allow=(404,))

    def wait_for(self, zone: str, server_id: str, states: tuple[str, ...],
                 timeout: float = WAIT_TIMEOUT_S) -> str:
        """Poll the server until its state is in ``states`` (``"deleted"`` included)."""
        deadline = time.monotonic() + timeout
        state = self.server_state(zone, server_id)
        while state not in states:
            if time.monotonic() >= deadline:
                raise ScalewayError(f"API Scaleway : l'instance est toujours « {state} » après "
                                    f"{timeout:.0f} s (attendu : {', '.join(states)}).")
            self._sleep(self.wait_interval)
            state = self.server_state(zone, server_id)
        return state

    # -- high-level ------------------------------------------------------------------------
    def poweroff(self, zone: str, server_id: str, *, wait: bool = True) -> str:
        """Stop the instance (volumes kept, GPU billing stopped); returns the final state."""
        state = self.server_state(zone, server_id)
        if state == DELETED:
            raise ScalewayError("API Scaleway : l'instance n'existe plus.")
        if state in ("stopped", "stopped in place"):
            return state
        if state == "running":
            self.server_action(zone, server_id, "poweroff")
        elif state not in ("stopping",):
            raise ScalewayError(f"API Scaleway : impossible d'éteindre une instance « {state} ».")
        if not wait:
            return "stopping"
        return self.wait_for(zone, server_id, ("stopped", "stopped in place", DELETED))

    def delete_server(self, zone: str, server_id: str, *, with_volumes: bool = True,
                      wait: bool = True) -> dict[str, Any]:
        """``terminate`` the instance then delete its remaining volumes.

        Returns ``{state, deleted_volumes: [ids]}``.
        """
        try:
            server = self.get_server(zone, server_id)
        except ScalewayError as exc:
            if "introuvable" in exc.message:
                return {"state": DELETED, "deleted_volumes": []}
            raise
        volumes = list(server.get("volumes") or [])
        if server.get("state") != DELETED:
            self.server_action(zone, server_id, "terminate")
            if wait:
                self.wait_for(zone, server_id, (DELETED,))
        deleted: list[str] = []
        if with_volumes:
            for volume in volumes:
                try:
                    self.delete_volume(zone, volume["id"], volume.get("volume_type"))
                    deleted.append(volume["id"])
                except ScalewayError as exc:
                    if "introuvable" not in exc.message:
                        raise
        return {"state": DELETED, "deleted_volumes": deleted}


def instance_view(client: "ScalewayClient | None", scaleway: Any) -> dict[str, Any]:
    """``GET /api/hosts/{name}/instance`` payload — never raises."""
    if scaleway is None:
        return {"configured": False, "state": None, "commercial_type": None, "zone": None,
                "error": None}
    zone, server_id = str(scaleway.zone), str(scaleway.server_id)
    if client is None:
        return {"configured": True, "state": None, "commercial_type": None, "zone": zone,
                "error": f"{SECRET_ENV} absent de l'environnement du serveur."}
    try:
        server = client.get_server(zone, server_id)
    except JobError as exc:
        return {"configured": True, "state": None, "commercial_type": None, "zone": zone,
                "error": exc.message}
    except Exception as exc:  # pragma: no cover - defensive
        return {"configured": True, "state": None, "commercial_type": None, "zone": zone,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"configured": True, "state": server.get("state"),
            "commercial_type": server.get("commercial_type"), "zone": zone, "error": None}


__all__ = ["ACTIONS", "BASE_URL", "SECRET_ENV", "ScalewayClient", "ScalewayError",
           "instance_view", "secret_key_from_env"]
