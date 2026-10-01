import type { ReactNode } from "react";
import { useNavigate } from "react-router";
import { ProgressBar, StatusBadge, Table, type Column } from "./ui";
import { formatEur, formatInt, formatRelative, plural, shortId, toDate } from "../lib/format";
import { useNow } from "../lib/hooks";
import { formatMetric, metricLabel } from "../lib/metrics";
import { RUN_STATUSES, studyPath } from "../lib/runs";
import type { StudySummary } from "../lib/types";

/** "5 terminés · 1 élagué · 1 échec · 1 en cours" (zero counts of failures / running omitted). */
export function trialCounts(s: Pick<StudySummary, "n_complete" | "n_pruned" | "n_failed" | "n_running">): string {
  return [
    plural(s.n_complete, "terminé"),
    plural(s.n_pruned, "élagué"),
    s.n_failed ? plural(s.n_failed, "échec") : null,
    s.n_running ? `${formatInt(s.n_running)} en cours` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Fraction of the trial budget consumed (null when unbounded). */
export function studyProgress(s: Pick<StudySummary, "n_trials" | "budget">): number | null {
  return s.budget.max_trials ? Math.min(1, s.n_trials / s.budget.max_trials) : null;
}

/** "Bit acc. moyenne 93,1 %" for mono-objective studies, "3 essais sur le front" otherwise. */
export function studyBestLabel(s: Pick<StudySummary, "best" | "objectives">): ReactNode {
  if (!s.best.length) return <span className="text-faint">—</span>;
  if (s.objectives.length > 1) return <span className="text-muted">{`${s.best.length} essai${s.best.length > 1 ? "s" : ""} sur le front`}</span>;
  const obj = s.objectives[0];
  const v = s.best[0].values[0];
  return (
    <span className="tabular" title={obj ? metricLabel(obj.metric, { withGroup: true }) : undefined}>
      {obj ? formatMetric(obj.metric, v) : formatInt(v)}
      <span className="ml-1.5 text-[11.5px] text-faint">essai {s.best[0].trial}</span>
    </span>
  );
}

export interface StudiesTableProps {
  studies: StudySummary[];
  loading?: boolean;
  empty?: ReactNode;
  /** Show the plan column (hidden inside a plan editor). */
  showPlan?: boolean;
  className?: string;
}

/** HPO studies with status, trial budget progress, best value and cost. */
export function StudiesTable({ studies, loading, empty, showPlan = true, className }: StudiesTableProps) {
  const navigate = useNavigate();
  const now = useNow(30000);
  const columns: Column<StudySummary>[] = [
    {
      id: "status",
      header: "Statut",
      width: 104,
      cell: (s) => <StatusBadge status={s.status} />,
      sortValue: (s) => RUN_STATUSES.indexOf(s.status),
    },
    {
      id: "study",
      header: "Étude",
      cell: (s) => (
        <div className="min-w-0 max-w-[240px]">
          {showPlan && <div className="truncate font-medium text-fg">{s.plan}</div>}
          <div className="truncate text-[11.5px] text-muted">
            <span className="font-mono">{shortId(s.study_id)}</span>
            {s.user && <span className="ml-1.5">· par {s.user}</span>}
          </div>
        </div>
      ),
      sortValue: (s) => s.plan,
    },
    {
      id: "progress",
      header: "Essais",
      width: 190,
      cell: (s) => (
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ProgressBar value={studyProgress(s)} size="xs" className="min-w-0 flex-1" tone={s.status === "failed" ? "danger" : "accent"} />
            <span className="tabular shrink-0 text-[12px] text-muted">
              {formatInt(s.n_trials)}
              {s.budget.max_trials ? ` / ${formatInt(s.budget.max_trials)}` : ""}
            </span>
          </div>
          <div className="mt-0.5 text-[11.5px] text-faint">
            {trialCounts(s)}
          </div>
        </div>
      ),
      sortValue: (s) => studyProgress(s),
    },
    { id: "best", header: "Meilleur", align: "right", cell: (s) => studyBestLabel(s), sortValue: (s) => s.best[0]?.values[0] },
    { id: "host", header: "Machine", cell: (s) => <span className="text-muted">{s.host}</span>, sortValue: (s) => s.host },
    {
      id: "cost",
      header: "Coût",
      align: "right",
      cell: (s) => (
        <span className="tabular text-muted">
          {formatEur(s.cost_eur)}
          {s.budget.max_eur ? <span className="text-faint"> / {formatEur(s.budget.max_eur, { digits: 0 })}</span> : null}
        </span>
      ),
      sortValue: (s) => s.cost_eur,
    },
    {
      id: "date",
      header: "Date",
      align: "right",
      cell: (s) => <span className="whitespace-nowrap text-muted">{formatRelative(s.created_at, now)}</span>,
      sortValue: (s) => toDate(s.created_at)?.getTime(),
    },
  ];
  return (
    <Table
      className={className}
      columns={columns}
      rows={studies}
      rowKey={(s) => s.study_id}
      onRowClick={(s) => navigate(studyPath(s.study_id))}
      loading={loading}
      initialSort={{ id: "date", desc: true }}
      empty={empty ?? "Aucune étude."}
      dense
    />
  );
}
