import { useMemo } from "react";
import { useNavigate } from "react-router";
import { ScatterPareto, type ParetoPoint } from "../charts";
import { Skeleton } from "../ui";
import { bareMetric, bestUnderConstraint, objectiveDirection } from "../analysis";
import { rowId, scoreRows } from "./LeaderboardView";
import { shortId } from "../../lib/format";
import { formatMetricAxis, metricLabel, parseConstraint } from "../../lib/metrics";
import { useLeaderboard } from "../../lib/queries";
import { runPath } from "../../lib/runs";
import type { ProjectSummary } from "../../lib/types";

/** Small quality × robustness scatter of a project (default benchmark, else validation). */
export function MiniPareto({ project, height = 260 }: { project: ProjectSummary; height?: number }) {
  const navigate = useNavigate();
  const source = project.best?.source === "val" ? "val" : "benchmark";
  const lb = useLeaderboard({ project: project.name, source });
  const q = project.objectives.quality;
  const r = project.objectives.robustness;
  const qMetric = bareMetric(q.metric);
  const rMetric = bareMetric(r.metric);
  const constraint = parseConstraint(q.constraint);

  const scored = useMemo(() => {
    const rows = (lb.data?.rows ?? []).filter(
      (row) => row.source === source && (source !== "benchmark" || !project.default_benchmark || row.benchmark === project.default_benchmark),
    );
    return scoreRows(rows, qMetric, rMetric);
  }, [lb.data, source, project.default_benchmark, qMetric, rMetric]);

  if (lb.isLoading) return <Skeleton className="h-64 rounded-xl" />;

  const best = bestUnderConstraint(scored, constraint, objectiveDirection(rMetric, r), objectiveDirection(qMetric, q));
  const points: ParetoPoint[] = scored
    .filter((s) => s.quality !== null && s.robustness !== null)
    .map((s) => ({
      id: rowId(s.item),
      x: s.quality!,
      y: s.robustness!,
      label: `${s.item.experiment} · ${s.item.checkpoint ?? shortId(s.item.run_id)}`,
      group: s.item.experiment,
    }));

  return (
    <ScatterPareto
      points={points}
      xLabel={q.label || metricLabel(qMetric)}
      yLabel={r.label || metricLabel(rMetric)}
      xDirection={q.direction}
      yDirection={r.direction}
      xFormatter={(v) => formatMetricAxis(qMetric, v)}
      yFormatter={(v) => formatMetricAxis(rMetric, v)}
      xConstraint={constraint}
      highlightId={best ? rowId(best.item) : null}
      onPointClick={(p) => {
        const s = scored.find((x) => rowId(x.item) === p.id);
        if (s) navigate(runPath(s.item.run_id, s.item.source === "benchmark" ? "evals" : undefined));
      }}
      height={height}
      empty={source === "benchmark" ? "Aucune évaluation sur le benchmark par défaut." : "Aucune validation pour l'instant."}
    />
  );
}
