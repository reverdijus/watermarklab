import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { GitCompareArrows } from "lucide-react";
import { Badge, Button, StatusBadge, Table, type Column } from "./ui";
import { MetricValue, RunProgress } from "./RunBits";
import { formatEur, formatRelative, plural, shortId, toDate } from "../lib/format";
import { useNow } from "../lib/hooks";
import { qualityMetricFor } from "../lib/metrics";
import { isPrunedTrial, progressFraction, runPath, RUN_STATUSES } from "../lib/runs";
import type { RunSummary } from "../lib/types";

/** Column classes hiding a secondary column under the `xl` breakpoint (1280 px). */
const NARROW_HIDDEN = { className: "hidden xl:table-cell", headerClassName: "hidden xl:table-cell" };

export interface RunsTableProps {
  runs: RunSummary[];
  loading?: boolean;
  empty?: ReactNode;
  /** Show the project column (cross-project lists). */
  showProject?: boolean;
  /** Show the experiment name as main label (default true). */
  showExperiment?: boolean;
  /** Checkbox selection with a "Comparer" bar. */
  selectable?: boolean;
  dense?: boolean;
  maxHeight?: number | string;
  className?: string;
}

/** Compact runs table (status, progress, key metrics, cost, date) used inside pages. */
export function RunsTable({
  runs,
  loading,
  empty,
  showProject,
  showExperiment = true,
  selectable,
  dense = true,
  maxHeight,
  className,
}: RunsTableProps) {
  const navigate = useNavigate();
  const now = useNow(30000);
  const [selected, setSelected] = useState<string[]>([]);

  const columns: Column<RunSummary>[] = [
    {
      id: "status",
      header: "Statut",
      width: 104,
      cell: (r) => <StatusBadge status={r.status} pruned={isPrunedTrial(r)} />,
      sortValue: (r) => RUN_STATUSES.indexOf(r.status),
    },
    {
      id: "run",
      header: "Run",
      // Flexible column: absorbs the remaining width and truncates instead of overflowing,
      // but never below 180 px (the table then scrolls horizontally).
      className: "w-full min-w-[180px] max-w-0",
      headerClassName: "w-full min-w-[180px]",
      cell: (r) => (
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium text-fg">{showExperiment ? r.experiment : shortId(r.run_id)}</span>
            {r.trial != null && <Badge tone="info">essai {r.trial}</Badge>}
            {r.parent && (
              <Badge tone="neutral" title={`Branché depuis ${r.parent.run_id}`}>
                branche
              </Badge>
            )}
          </div>
          {showExperiment ? (
            <div className="truncate text-[11.5px] text-muted">
              <span className="font-mono">{shortId(r.run_id)}</span>
              {r.user && <span className="ml-1.5">· par {r.user}</span>}
            </div>
          ) : (
            r.user && <div className="truncate text-[11.5px] text-muted">par {r.user}</div>
          )}
        </div>
      ),
      sortValue: (r) => r.experiment,
    },
    ...(showProject
      ? [{ id: "project", header: "Projet", cell: (r: RunSummary) => <span className="text-muted">{r.project}</span>, sortValue: (r: RunSummary) => r.project }]
      : []),
    // Secondary column: hidden under 1280 px (the minimum window width is 1000 px).
    { id: "host", header: "Machine", cell: (r) => <span className="text-muted">{r.host}</span>, sortValue: (r) => r.host, ...NARROW_HIDDEN },
    {
      id: "progress",
      header: "Progression",
      width: 140,
      cell: (r) => <RunProgress progress={r.progress} status={r.status} />,
      sortValue: (r) => progressFraction(r.progress),
    },
    {
      id: "bitacc",
      header: "Bit acc.",
      align: "right",
      title: "val/bit_acc_mean — moyenne sur les attaques de validation",
      cell: (r) => <MetricValue metric="val/bit_acc_mean" value={r.summary["val/bit_acc_mean"]} />,
      sortValue: (r) => r.summary["val/bit_acc_mean"],
    },
    {
      id: "quality",
      header: "Qualité",
      align: "right",
      cell: (r) => {
        const k = `val/${qualityMetricFor(r.modality)}`;
        return <MetricValue metric={k} value={r.summary[k]} />;
      },
      sortValue: (r) => r.summary[`val/${qualityMetricFor(r.modality)}`],
    },
    {
      id: "cost",
      header: "Coût",
      align: "right",
      cell: (r) => <span className="tabular text-muted">{formatEur(r.cost_eur)}</span>,
      sortValue: (r) => r.cost_eur,
    },
    {
      id: "date",
      header: "Date",
      align: "right",
      cell: (r) => <span className="whitespace-nowrap text-muted">{formatRelative(r.created_at, now)}</span>,
      sortValue: (r) => toDate(r.created_at)?.getTime(),
    },
  ];

  return (
    <div className={className}>
      {selectable && selected.length > 0 && (
        <div className="mb-3 flex animate-fade-in items-center gap-3 rounded-xl border border-accent/25 bg-accent-soft/60 px-4 py-2 text-[13px]">
          <span className="text-fg">{plural(selected.length, "run sélectionné", "runs sélectionnés")}</span>
          <Button
            size="sm"
            variant="primary"
            icon={GitCompareArrows}
            disabled={selected.length < 2}
            onClick={() => navigate(`/compare?runs=${selected.map(encodeURIComponent).join(",")}`)}
          >
            Comparer
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setSelected([])}>
            Désélectionner
          </Button>
        </div>
      )}
      <Table
        columns={columns}
        rows={runs}
        rowKey={(r) => r.run_id}
        onRowClick={(r) => navigate(runPath(r.run_id))}
        selectable={selectable}
        selected={selected}
        onSelectionChange={setSelected}
        loading={loading}
        initialSort={{ id: "date", desc: true }}
        empty={empty ?? "Aucun run."}
        dense={dense}
        maxHeight={maxHeight}
      />
    </div>
  );
}
