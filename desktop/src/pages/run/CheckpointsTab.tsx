import { Database, Download, GitBranch, Microscope } from "lucide-react";
import { Badge, Card, EmptyState, IconButton, Spinner, Table, Tooltip, useToast, type Column } from "../../components/ui";
import { useLaunch } from "../../components/launch/LaunchProvider";
import { errorMessage } from "../../lib/api";
import { formatBytes, formatInt, formatRelative, toDate } from "../../lib/format";
import { formatMetric, metricLabel, pickKeyMetrics } from "../../lib/metrics";
import { useFetchCheckpoint } from "../../lib/queries";
import type { Checkpoint, RunDetail } from "../../lib/types";

function tagTone(tag: string) {
  if (tag === "last") return "accent" as const;
  if (tag.startsWith("best:")) return "success" as const;
  if (tag.startsWith("phase_end:")) return "info" as const;
  return "neutral" as const;
}

export function CheckpointsTab({ run }: { run: RunDetail }) {
  const { openLaunch } = useLaunch();
  const fetchCkpt = useFetchCheckpoint(run.run_id);
  const toast = useToast();

  const onFetch = async (c: Checkpoint) => {
    try {
      await fetchCkpt.mutateAsync(c.name);
      toast.success("Rapatriement lancé", `${c.name} sera copié depuis ${run.host}.`);
    } catch (err) {
      toast.error("Rapatriement impossible", errorMessage(err));
    }
  };

  if (!run.checkpoints.length) {
    return (
      <Card padding="none">
        <EmptyState
          icon={Database}
          title="Aucun checkpoint"
          description="Les checkpoints apparaissent selon checkpoint.every_steps, en fin de phase et à la fin du run."
        />
      </Card>
    );
  }

  const columns: Column<Checkpoint>[] = [
    {
      id: "step",
      header: "Checkpoint",
      cell: (c) => (
        <div>
          <div className="font-mono text-[12.5px] text-fg">{c.name}</div>
          <div className="text-[11.5px] text-muted">step {formatInt(c.step)}</div>
        </div>
      ),
      sortValue: (c) => c.step,
    },
    {
      id: "phase",
      header: "Phase",
      cell: (c) => (
        <span className="block max-w-[160px] truncate text-muted" title={c.phase}>
          {c.phase}
        </span>
      ),
      sortValue: (c) => c.phase,
    },
    {
      id: "tags",
      header: "Tags",
      wrap: true,
      // Flexible column: takes the remaining width so the table never scrolls sideways.
      className: "w-full min-w-[150px] max-w-0",
      headerClassName: "w-full",
      cell: (c) => (
        <div className="flex flex-wrap gap-1">
          {c.tags.map((t) => (
            <Badge key={t} tone={tagTone(t)}>
              {t}
            </Badge>
          ))}
        </div>
      ),
    },
    {
      id: "metrics",
      header: "Métriques",
      wrap: true,
      cell: (c) => {
        const items = pickKeyMetrics(c.metrics, run.modality, 2);
        return items.length ? (
          <div className="flex flex-wrap gap-x-3 text-[12.5px]">
            {items.map((m) => (
              <span key={m.key} className="whitespace-nowrap text-muted">
                {metricLabel(m.key)} <span className="tabular text-fg">{formatMetric(m.key, m.value)}</span>
              </span>
            ))}
          </div>
        ) : (
          <span className="text-faint">—</span>
        );
      },
    },
    {
      id: "file",
      header: "Fichier",
      cell: (c) => (
        <span className="inline-flex items-center gap-2">
          <span className="tabular text-muted">{formatBytes(c.size_bytes)}</span>
          {c.available_locally ? (
            <Badge tone="success">local</Badge>
          ) : (
            <Tooltip content={`Fichier .pt resté sur ${run.host} (non synchronisé)`}>
              <Badge tone="warning">distant</Badge>
            </Tooltip>
          )}
        </span>
      ),
      sortValue: (c) => (c.available_locally ? 1 : 0) * 1e15 + c.size_bytes,
    },
    {
      id: "date",
      header: "Créé",
      align: "right",
      cell: (c) => <span className="whitespace-nowrap text-muted">{formatRelative(c.created_at)}</span>,
      sortValue: (c) => toDate(c.created_at)?.getTime(),
    },
    {
      id: "actions",
      header: "",
      align: "right",
      cell: (c) => (
        <div className="flex justify-end gap-0.5">
          {!c.available_locally &&
            (fetchCkpt.isPending && fetchCkpt.variables === c.name ? (
              <span className="inline-flex h-7 w-7 items-center justify-center">
                <Spinner size={14} />
              </span>
            ) : (
              <IconButton icon={Download} label={`Rapatrier depuis ${run.host}`} size="sm" onClick={() => void onFetch(c)} />
            ))}
          <IconButton
            icon={Microscope}
            label="Évaluer ce checkpoint"
            size="sm"
            onClick={() => openLaunch({ kind: "eval", project: run.project, evalRun: run.run_id, evalCheckpoint: c.name })}
          />
          <IconButton
            icon={GitBranch}
            label="Brancher un nouveau run depuis ce checkpoint"
            size="sm"
            onClick={() =>
              openLaunch({
                kind: "train",
                project: run.project,
                experiment: run.experiment,
                host: run.host,
                initFrom: { run: run.run_id, checkpoint: c.name },
              })
            }
          />
        </div>
      ),
    },
  ];

  return (
    <div className="space-y-3">
      <p className="text-[13px] text-muted">
        Rétention : les derniers checkpoints, le meilleur pour chaque critère <span className="font-mono">keep_best</span> et toutes les fins de
        phase sont conservés.
      </p>
      <Table columns={columns} rows={run.checkpoints} rowKey={(c) => c.name} initialSort={{ id: "step", desc: true }} dense />
    </div>
  );
}
