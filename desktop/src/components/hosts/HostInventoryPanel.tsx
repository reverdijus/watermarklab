/**
 * Inventory of a machine (§12.4): runs present on the host, checkpoints not yet retrieved
 * (count and size), unindexed runs, active jobs and the Scaleway instance state when
 * configured. Loaded on demand and refreshed after each retrieve job.
 */
import { CircleAlert, RefreshCw } from "lucide-react";
import { Badge, Button, Callout, IconButton, Skeleton, Table, type Column } from "../ui";
import { errorMessage } from "../../lib/api";
import { formatBytes, formatInt, formatRelative, plural, shortId } from "../../lib/format";
import { useHostInstance, useHostInventory } from "../../lib/queries";
import type { Host, HostInventory, HostInventoryRun, HostInstance } from "../../lib/types";

function Stat({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone?: "warning" | "danger" }) {
  return (
    <div className="min-w-0">
      <div className="text-[11.5px] text-muted">{label}</div>
      <div className={`tabular truncate font-serif text-[18px] ${tone === "warning" ? "text-warning" : tone === "danger" ? "text-danger" : "text-fg"}`}>
        {value}
      </div>
      {sub && <div className="truncate text-[11.5px] text-muted">{sub}</div>}
    </div>
  );
}

/** French label of a Scaleway instance state. */
export function instanceStateLabel(state: string | null | undefined): string {
  switch (state) {
    case "running":
      return "en marche";
    case "stopped":
      return "éteinte";
    case "stopped in place":
      return "arrêtée (volumes conservés)";
    case "stopping":
      return "extinction…";
    case "starting":
      return "démarrage…";
    case "locked":
      return "verrouillée";
    case null:
    case undefined:
    case "":
      return "inconnue";
    default:
      return state;
  }
}

function instanceTone(state: string | null | undefined): "success" | "neutral" | "warning" | "info" {
  if (state === "running") return "success";
  if (state === "stopped" || state === "stopped in place") return "neutral";
  if (state === "stopping" || state === "starting") return "info";
  return "warning";
}

function InstanceLine({ host, instance }: { host: Host; instance: ReturnType<typeof useHostInstance> }) {
  if (!host.scaleway) return null;
  const data: HostInstance | undefined = instance.data;
  return (
    <div className="flex flex-wrap items-center gap-2 text-[12.5px] text-muted">
      <span>Instance Scaleway</span>
      {instance.isLoading ? (
        <Skeleton className="h-4 w-24" />
      ) : instance.isError ? (
        <span className="text-danger">{errorMessage(instance.error)}</span>
      ) : data?.configured ? (
        <>
          <Badge tone={instanceTone(data.state)} dot pulse={data.state === "stopping" || data.state === "starting"}>
            {instanceStateLabel(data.state)}
          </Badge>
          <span className="font-mono text-[11.5px]">
            {data.commercial_type ?? "—"} · {data.zone ?? host.scaleway.zone} · {host.scaleway.server_id}
          </span>
          {data.error && <span className="text-danger">{data.error}</span>}
        </>
      ) : (
        <span className="text-faint">non configurée côté serveur{data?.error ? ` — ${data.error}` : ""}</span>
      )}
    </div>
  );
}

export interface HostInventoryPanelProps {
  host: Host;
  /** Load the inventory (false until the user asks or a retrieve job ends). */
  enabled: boolean;
}

export function HostInventoryPanel({ host, enabled }: HostInventoryPanelProps) {
  const inventory = useHostInventory(host.name, { enabled });
  const instance = useHostInstance(host.name, { enabled: enabled && !!host.scaleway });
  if (!enabled) return null;

  const inv: HostInventory | undefined = inventory.data;
  const columns: Column<HostInventoryRun>[] = [
    {
      id: "run",
      header: "Run",
      className: "w-full min-w-[140px] max-w-0",
      headerClassName: "w-full",
      cell: (r) => (
        <div className="min-w-0">
          <div className="truncate font-mono text-[12px] text-fg">{shortId(r.run_id)}</div>
          <div className="truncate text-[11.5px] text-muted">
            {r.project ?? "—"}
            {r.status ? ` · ${r.status}` : ""}
            {!r.indexed && <span className="text-warning"> · non indexé</span>}
          </div>
        </div>
      ),
    },
    {
      id: "ckpt",
      header: "Checkpoints",
      align: "right",
      width: 110,
      cell: (r) => (
        <span className={`tabular ${r.checkpoints_local < r.checkpoints_total ? "text-warning" : "text-muted"}`}>
          {formatInt(r.checkpoints_local)} / {formatInt(r.checkpoints_total)}
        </span>
      ),
      sortValue: (r) => r.checkpoints_total - r.checkpoints_local,
    },
    {
      id: "missing",
      header: "À rapatrier",
      align: "right",
      width: 100,
      cell: (r) => <span className={`tabular ${r.bytes_missing > 0 ? "text-warning" : "text-faint"}`}>{r.bytes_missing > 0 ? formatBytes(r.bytes_missing) : "—"}</span>,
      sortValue: (r) => r.bytes_missing,
    },
  ];

  return (
    <div className="mt-3 rounded-xl border border-border bg-surface-2/50 px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-[13px] font-medium text-fg">
          Inventaire
          {inv && <span className="ml-2 font-normal text-muted">vérifié {formatRelative(inv.checked_at)}</span>}
        </div>
        <IconButton
          icon={RefreshCw}
          label="Actualiser l'inventaire"
          size="sm"
          onClick={() => {
            void inventory.refetch();
            if (host.scaleway) void instance.refetch();
          }}
          disabled={inventory.isFetching}
        />
      </div>

      {inventory.isLoading ? (
        <div className="mt-3 grid grid-cols-4 gap-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-11" />
          ))}
        </div>
      ) : inventory.isError ? (
        <Callout
          tone="danger"
          icon={CircleAlert}
          className="mt-3"
          action={
            <Button size="sm" onClick={() => void inventory.refetch()}>
              Réessayer
            </Button>
          }
        >
          {errorMessage(inventory.error)}
        </Callout>
      ) : inv ? (
        <>
          {!inv.reachable && (
            <Callout tone="danger" icon={CircleAlert} title="Machine injoignable" className="mt-3">
              {inv.error ?? "La commande distante a échoué : l'inventaire affiché est celui du dernier passage."}
            </Callout>
          )}
          <div className="mt-3 grid grid-cols-3 gap-3">
            <Stat
              label="Runs présents"
              value={formatInt(inv.runs.length)}
              sub={inv.unindexed_runs > 0 ? `dont ${plural(inv.unindexed_runs, "non indexé")}` : inv.studies.length ? plural(inv.studies.length, "étude") : undefined}
              tone={inv.unindexed_runs > 0 ? "warning" : undefined}
            />
            <Stat
              label="Non rapatriés"
              value={formatInt(inv.unretrieved_checkpoints)}
              sub={inv.unretrieved_checkpoints > 0 ? `checkpoints · ${formatBytes(inv.unretrieved_bytes)}` : "tout est sur le serveur"}
              tone={inv.unretrieved_checkpoints > 0 ? "warning" : undefined}
            />
            <Stat label="Jobs actifs" value={formatInt(inv.active_jobs)} tone={inv.active_jobs > 0 ? "warning" : undefined} />
          </div>
          {host.scaleway && (
            <div className="mt-3 border-t border-border pt-3">
              <InstanceLine host={host} instance={instance} />
            </div>
          )}
          {inv.runs.length > 0 && (
            <div className="mt-3">
              <Table
                columns={columns}
                rows={[...inv.runs].sort((a, b) => b.bytes_missing - a.bytes_missing).slice(0, 8)}
                rowKey={(r) => r.run_id}
                dense
                empty="Aucun run sur cette machine."
              />
              {inv.runs.length > 8 && (
                <div className="mt-1.5 text-[11.5px] text-muted">
                  {formatInt(inv.runs.length - 8)} autre{inv.runs.length - 8 > 1 ? "s" : ""} run{inv.runs.length - 8 > 1 ? "s" : ""} non affiché
                  {inv.runs.length - 8 > 1 ? "s" : ""} (triés par volume à rapatrier).
                </div>
              )}
            </div>
          )}
          {inv.runs.length === 0 && inv.reachable && <div className="mt-3 text-[12.5px] text-muted">Aucun run sur cette machine.</div>}
        </>
      ) : null}
    </div>
  );
}
