/**
 * "Retirer la machine" (§12.4): recaps what the inventory verified (runs, checkpoints on the
 * server), offers to delete the Scaleway instance when configured, and calls
 * `POST /api/hosts/{name}/decommission`. The server re-checks everything and refuses with
 * 409 while checkpoints are missing or a job is active.
 */
import { useState } from "react";
import { CircleAlert, CircleCheck, Cloud, TriangleAlert } from "lucide-react";
import { Button, Callout, Dialog, KeyValue, Skeleton, Switch, useToast } from "../ui";
import { errorMessage } from "../../lib/api";
import { formatBytes, formatInt, plural } from "../../lib/format";
import { decommissionBlockers } from "../../lib/hosted";
import { useDecommissionHost, useHostInventory } from "../../lib/queries";
import type { Host } from "../../lib/types";

export interface DecommissionDialogProps {
  host: Host;
  /** Active jobs on the host (from the job list). */
  activeJobs: number;
  onClose: () => void;
}

export function DecommissionDialog({ host, activeJobs, onClose }: DecommissionDialogProps) {
  const inventory = useHostInventory(host.name);
  const decommission = useDecommissionHost();
  const toast = useToast();
  const [deleteInstance, setDeleteInstance] = useState(!!host.scaleway);
  const [force, setForce] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const inv = inventory.data;
  const checked = inv
    ? {
        unretrieved_checkpoints: inv.unretrieved_checkpoints,
        unretrieved_bytes: inv.unretrieved_bytes,
        decommissioned_at: host.decommissioned_at,
        kind: host.kind,
      }
    : host;
  const blockers = decommissionBlockers(checked, Math.max(activeJobs, inv?.active_jobs ?? 0));
  if (inv && inv.unindexed_runs > 0) blockers.push(`${plural(inv.unindexed_runs, "run non indexé", "runs non indexés")} — lancez le rapatriement.`);
  const unreachable = !!inv && !inv.reachable;
  const totalCheckpoints = inv ? inv.runs.reduce((n, r) => n + r.checkpoints_total, 0) : 0;
  const localCheckpoints = inv ? inv.runs.reduce((n, r) => n + r.checkpoints_local, 0) : 0;
  const canSubmit = !inventory.isLoading && blockers.length === 0 && (!unreachable || force);

  const submit = async () => {
    setError(null);
    try {
      const res = await decommission.mutateAsync({
        name: host.name,
        body: { confirm: true, delete_instance: !!host.scaleway && deleteInstance, ...(unreachable && force ? { force: true } : {}) },
      });
      toast.success(`${host.name} retirée`, res.message || (res.deleted_instance ? "Instance Scaleway supprimée." : undefined));
      if (!res.deleted_instance && host.scaleway && deleteInstance) {
        toast.warning("Instance non supprimée", `État : ${res.instance_state ?? "inconnu"} — supprimez-la depuis la console Scaleway.`);
      }
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="md"
      title={`Retirer ${host.name} ?`}
      description="La machine n'est plus proposée au lancement ; son entrée reste dans hosts.yaml pour l'historique des coûts."
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={decommission.isPending}>
            Annuler
          </Button>
          <Button variant="danger" onClick={() => void submit()} loading={decommission.isPending} disabled={!canSubmit}>
            Retirer la machine
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {inventory.isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-4 w-56" />
            <Skeleton className="h-4 w-72" />
            <Skeleton className="h-4 w-40" />
          </div>
        ) : inventory.isError ? (
          <Callout tone="danger" icon={CircleAlert} title="Inventaire indisponible">
            {errorMessage(inventory.error)}
          </Callout>
        ) : inv ? (
          <>
            <KeyValue
              dense
              items={[
                { label: "Runs vérifiés", value: `${formatInt(inv.runs.length)}${inv.unindexed_runs ? ` (${inv.unindexed_runs} non indexé${inv.unindexed_runs > 1 ? "s" : ""})` : ""}` },
                { label: "Études", value: formatInt(inv.studies.length) },
                { label: "Checkpoints sur le serveur", value: `${formatInt(localCheckpoints)} / ${formatInt(totalCheckpoints)}` },
                {
                  label: "Non rapatriés",
                  value: inv.unretrieved_checkpoints > 0 ? `${formatInt(inv.unretrieved_checkpoints)} · ${formatBytes(inv.unretrieved_bytes)}` : "aucun",
                },
                { label: "Jobs actifs", value: formatInt(Math.max(activeJobs, inv.active_jobs)) },
              ]}
            />
            {blockers.length === 0 && !unreachable && (
              <Callout tone="success" icon={CircleCheck} title="Tout est sur le serveur">
                Chaque checkpoint listé sur la machine existe sur le serveur avec la même taille ; rien ne sera perdu.
              </Callout>
            )}
          </>
        ) : null}

        {blockers.length > 0 && (
          <Callout tone="warning" icon={TriangleAlert} title="Retrait impossible pour l'instant">
            <ul className="list-disc space-y-0.5 pl-4">
              {blockers.map((b) => (
                <li key={b}>{b}</li>
              ))}
            </ul>
          </Callout>
        )}

        {unreachable && (
          <Callout tone="danger" icon={CircleAlert} title="Machine injoignable">
            <div className="space-y-2">
              <div>{inv?.error ?? "L'inventaire n'a pas pu être recalculé."} Retirer une machine injoignable ne vérifie rien : des checkpoints peuvent être perdus.</div>
              <Switch size="sm" checked={force} onChange={setForce} label="Retirer quand même (administrateur, journalisé)" />
            </div>
          </Callout>
        )}

        {host.scaleway ? (
          <div className="rounded-xl border border-border px-4 py-3">
            <Switch
              checked={deleteInstance}
              onChange={setDeleteInstance}
              label={
                <span className="inline-flex items-center gap-1.5">
                  <Cloud size={14} strokeWidth={1.75} aria-hidden /> Supprimer aussi l'instance Scaleway
                </span>
              }
              description={
                <>
                  <span className="font-mono text-[11.5px]">{host.scaleway.server_id}</span> · zone {host.scaleway.zone} — l'instance et ses volumes sont
                  supprimés via l'API (clé SCW_SECRET_KEY du serveur). Irréversible : la facturation s'arrête.
                </>
              }
            />
          </div>
        ) : (
          <Callout tone="info" icon={Cloud}>
            Aucune instance Scaleway n'est associée : pensez à supprimer la machine dans la console Scaleway pour arrêter la facturation.
          </Callout>
        )}

        {error && (
          <Callout tone="danger" icon={CircleAlert}>
            {error}
          </Callout>
        )}
      </div>
    </Dialog>
  );
}
