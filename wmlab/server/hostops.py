"""Host lifecycle of the hosted mode (ARCHITECTURE.md §12.4–12.5): decommission, power off,
instance state. Guards: no active job, inventory recomputed (nothing left unretrieved),
host reachable unless forced.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from wmlab.spec import HostSpec, save_hosts
from wmlab.ids import utc_iso
from wmlab.server.jobs import JobError, JobManager
from wmlab.server.scaleway import SECRET_ENV, ScalewayClient, instance_view

log = logging.getLogger("wmlab.server")

ClientFactory = Callable[[], "ScalewayClient | None"]


def _gb(n_bytes: int) -> str:
    return f"{n_bytes / 1024 ** 3:.1f}".replace(".", ",") + " Go"


def _remote_host(jobs: JobManager, name: str) -> HostSpec:
    host = jobs.host(name, allow_decommissioned=True)
    if host.kind != "ssh":
        raise JobError(400, f"« {host.name} » est la machine locale : cette opération ne "
                            "concerne que les machines distantes.")
    return host


def _no_active_jobs(jobs: JobManager, host: HostSpec) -> None:
    active = jobs.active_jobs(host.name)
    if active:
        ids = ", ".join(j.job_id for j in active[:3])
        raise JobError(409, f"{len(active)} job(s) actif(s) sur « {host.name} » ({ids}) : "
                            "attendez leur fin ou annulez-les d'abord.")


def _save_host(jobs: JobManager, updated: HostSpec) -> None:
    hosts = [updated if h.name == updated.name else h for h in jobs.ctx.hosts()]
    save_hosts(hosts, jobs.ctx.hosts_path)


def instance(jobs: JobManager, name: str, client_factory: ClientFactory) -> dict[str, Any]:
    """``GET /api/hosts/{name}/instance`` (never raises for API problems)."""
    host = jobs.host(name, allow_decommissioned=True)
    if host.scaleway is None:
        return instance_view(None, None)
    try:
        client = client_factory()
    except JobError as exc:
        return {"configured": True, "state": None, "commercial_type": None,
                "zone": host.scaleway.zone, "error": exc.message}
    try:
        return instance_view(client, host.scaleway)
    finally:
        if client is not None:
            client.close()


def poweroff(jobs: JobManager, name: str, client_factory: ClientFactory) -> dict[str, Any]:
    """``POST /api/hosts/{name}/poweroff``: stop the Scaleway instance (volumes kept)."""
    host = _remote_host(jobs, name)
    _no_active_jobs(jobs, host)
    if host.scaleway is None:
        raise JobError(400, f"Aucune instance Scaleway n'est configurée pour « {host.name} » "
                            "(champ scaleway: {zone, server_id} dans Machines).")
    client = client_factory()
    if client is None:
        raise JobError(400, f"{SECRET_ENV} absent de l'environnement du serveur : extinction "
                            "impossible depuis l'application.")
    try:
        state = client.poweroff(host.scaleway.zone, host.scaleway.server_id)
    finally:
        client.close()
    log.info("machine %s : instance Scaleway %s éteinte (%s)", host.name,
             host.scaleway.server_id, state)
    return {"ok": True, "instance_state": state}


def decommission(jobs: JobManager, name: str, *, delete_instance: bool, force: bool,
                 user: str | None, client_factory: ClientFactory) -> dict[str, Any]:
    """``POST /api/hosts/{name}/decommission`` with every guard of §12.4."""
    host = _remote_host(jobs, name)
    if host.decommissioned:
        raise JobError(409, f"La machine « {host.name} » est déjà retirée "
                            f"({host.decommissioned_at}).")
    _no_active_jobs(jobs, host)
    inventory = jobs.inventory(host.name)
    if not inventory["reachable"]:
        if not force:
            raise JobError(409, f"Machine « {host.name} » injoignable : "
                                f"{inventory['error']} — impossible de vérifier les checkpoints "
                                "(retrait forcé possible avec force: true).")
        log.warning("retrait forcé de %s (injoignable : %s) par %s", host.name,
                    inventory["error"], user or "?")
    else:
        problems = []
        if inventory["unretrieved_checkpoints"] > 0:
            problems.append(f"{inventory['unretrieved_checkpoints']} checkpoints "
                            f"({_gb(inventory['unretrieved_bytes'])}) non rapatriés")
        if inventory["unindexed_runs"] > 0:
            problems.append(f"{inventory['unindexed_runs']} run(s) non indexé(s) sur le serveur")
        if problems:
            raise JobError(409, " ; ".join(problems) + " — lancez le rapatriement.")
    deleted_instance = False
    instance_state: str | None = None
    message_parts = [f"Machine « {host.name} » retirée."]
    if delete_instance:
        if host.scaleway is None:
            message_parts.append("Aucune instance Scaleway configurée : rien à supprimer.")
        else:
            client = client_factory()
            if client is None:
                message_parts.append(f"{SECRET_ENV} absent : l'instance Scaleway n'a pas été "
                                     "supprimée (supprimez-la depuis la console).")
            else:
                try:
                    result = client.delete_server(host.scaleway.zone, host.scaleway.server_id)
                finally:
                    client.close()
                deleted_instance = True
                instance_state = str(result.get("state") or "deleted")
                volumes = result.get("deleted_volumes") or []
                message_parts.append(f"Instance Scaleway {host.scaleway.server_id} supprimée "
                                     f"({len(volumes)} volume(s) supprimé(s)).")
    elif host.scaleway is not None:
        try:
            client = client_factory()
        except JobError:
            client = None
        if client is not None:
            try:
                instance_state = client.server_state(host.scaleway.zone,
                                                     host.scaleway.server_id)
            except JobError:
                instance_state = None
            finally:
                client.close()
    updated = host.model_copy(update={"decommissioned_at": utc_iso()})
    _save_host(jobs, updated)
    log.info("machine %s retirée par %s (instance supprimée : %s)", host.name, user or "?",
             deleted_instance)
    return {"ok": True, "deleted_instance": deleted_instance, "instance_state": instance_state,
            "message": " ".join(message_parts)}


__all__ = ["decommission", "instance", "poweroff"]
