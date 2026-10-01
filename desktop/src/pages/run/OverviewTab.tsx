import { useMemo } from "react";
import {
  CircleAlert,
  CircleCheck,
  Database,
  Flag,
  Info,
  Microscope,
  Image as ImageIcon,
  Play,
  StickyNote,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { Card, CardHeader, EmptyState, Kpi, Markdown, Skeleton } from "../../components/ui";
import { LineChart, type LineSeries } from "../../components/charts";
import { PhaseTimeline } from "../../components/PhaseTimeline";
import { formatDate, formatInt, formatRelative } from "../../lib/format";
import { formatMetric, formatMetricAxis, metricLabel, qualityMetricFor } from "../../lib/metrics";
import { useRunMetrics } from "../../lib/queries";
import { STATUS_LABELS } from "../../lib/runs";
import type { RunDetail, RunEvent, RunStatus } from "../../lib/types";

const EVENT_ICONS: Record<string, LucideIcon> = {
  status: Play,
  phase_start: Flag,
  phase_end: CircleCheck,
  checkpoint: Database,
  checkpoint_deleted: Database,
  eval: Microscope,
  sample: ImageIcon,
  note: StickyNote,
  error: CircleAlert,
  warning: TriangleAlert,
};

const str = (v: unknown) => (v == null ? "" : String(v));

/** French one-line description of a run event. */
export function describeEvent(e: RunEvent): string {
  switch (e.type) {
    case "status":
      return `Statut : ${STATUS_LABELS[str(e.status) as RunStatus] ?? str(e.status)}`;
    case "phase_start":
      return `Début de la phase ${str(e.phase ?? e.name)}`;
    case "phase_end":
      return `Fin de la phase ${str(e.phase ?? e.name)}`;
    case "checkpoint":
      return `Checkpoint ${str(e.name)}${Array.isArray(e.tags) && e.tags.length ? ` (${(e.tags as string[]).join(", ")})` : ""}`;
    case "checkpoint_deleted":
      return `Checkpoint supprimé : ${str(e.name)}`;
    case "eval":
      return `Évaluation ${str(e.benchmark)} sur ${str(e.checkpoint)}`;
    case "sample":
      return `Échantillons enregistrés (step ${str(e.step)})`;
    case "note":
      return str(e.text ?? e.message ?? "Note");
    case "error":
      return `Erreur : ${str(e.message ?? e.error)}`;
    case "warning":
      return `Avertissement : ${str(e.message)}`;
    default:
      return e.type;
  }
}

const EVENT_TONES: Record<string, string> = {
  error: "text-danger",
  warning: "text-warning",
  phase_start: "text-accent",
  phase_end: "text-success",
};

export function OverviewTab({ run, live }: { run: RunDetail; live: boolean }) {
  const q = qualityMetricFor(run.modality);
  const keys = useMemo(
    () =>
      ["train/loss", "val/bit_acc_mean", "val/bit_acc", "train/bit_acc", `val/${q}`].filter((k) => run.metric_keys.includes(k)),
    [run.metric_keys, q],
  );
  const metrics = useRunMetrics(run.run_id, keys, { live, maxPoints: 800, enabled: keys.length > 0 });
  const phases = metrics.data?.phases ?? run.plan?.phases.map((p) => ({ name: p.name, start: p.start, end: p.end })) ?? [];
  const markX = live && run.progress ? { value: run.progress.step } : null;

  /** Series for [key, display name?] pairs, skipping keys without data. */
  const seriesFor = (defs: [string, string?][]): LineSeries[] =>
    defs
      .filter(([k]) => metrics.data?.series[k])
      .map(([k, name]) => ({
        name: name ?? metricLabel(k, { withGroup: true }),
        x: metrics.data!.series[k].steps,
        y: metrics.data!.series[k].values,
      }));

  const kpiKeys = useMemo(() => {
    const preferred = ["val/bit_acc_mean", `val/${q}`, "val/bit_acc", "val/loss"];
    const best = Object.keys(run.summary).filter((k) => k.startsWith("best/"));
    return [...preferred.filter((k) => run.summary[k] !== undefined), ...best].slice(0, 8);
  }, [run.summary, q]);

  const spec = run.spec as { hypothesis?: string; description?: string };
  const events = [...run.events].sort((a, b) => b.t - a.t).slice(0, 14);

  return (
    <div className="space-y-6">
      {run.plan ? (
        <Card>
          <CardHeader
            title="Curriculum"
            description={`${run.plan.phases.length} phase${run.plan.phases.length > 1 ? "s" : ""} · ${formatInt(run.plan.total_steps)} steps`}
          />
          <PhaseTimeline plan={run.plan} currentStep={run.progress?.step ?? null} live={live} />
        </Card>
      ) : (
        <Card>
          <EmptyState compact icon={Info} title="Plan de phases indisponible" description="La spec résolue de ce run ne fournit pas d'aperçu du curriculum." />
        </Card>
      )}

      {kpiKeys.length > 0 && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {kpiKeys.map((k) => (
            <Kpi key={k} label={metricLabel(k, { withGroup: true })} value={formatMetric(k, run.summary[k])} hint={k} />
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Card padding="sm" className="px-4 pt-3.5">
          <CardHeader title="Perte d'entraînement" className="mb-1" />
          {metrics.isLoading ? (
            <Skeleton className="h-[220px]" />
          ) : (
            <LineChart
              height={220}
              series={seriesFor([["train/loss"]])}
              phases={phases}
              markX={markX}
              yFormatter={(v) => formatMetricAxis("train/loss", v)}
              empty="Pas encore de mesure"
            />
          )}
        </Card>
        <Card padding="sm" className="px-4 pt-3.5">
          <CardHeader title="Robustesse (bit acc.)" className="mb-1" />
          {metrics.isLoading ? (
            <Skeleton className="h-[220px]" />
          ) : (
            <LineChart
              height={220}
              series={seriesFor([
                ["val/bit_acc_mean", "Val · moyenne attaques"],
                ["val/bit_acc", "Val · sans attaque"],
                ["train/bit_acc", "Entraînement"],
              ])}
              phases={phases}
              markX={markX}
              yFormatter={(v) => formatMetricAxis("val/bit_acc", v)}
              valueFormatter={(v) => formatMetric("val/bit_acc", v)}
              empty="Pas encore de validation"
            />
          )}
        </Card>
        <Card padding="sm" className="px-4 pt-3.5">
          <CardHeader title={`Qualité (${metricLabel(q)})`} className="mb-1" />
          {metrics.isLoading ? (
            <Skeleton className="h-[220px]" />
          ) : (
            <LineChart
              height={220}
              series={seriesFor([[`val/${q}`]])}
              phases={phases}
              markX={markX}
              yFormatter={(v) => formatMetricAxis(`val/${q}`, v)}
              valueFormatter={(v) => formatMetric(`val/${q}`, v)}
              empty="Pas encore de validation"
            />
          )}
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader title="Hypothèse" description={spec.description || undefined} />
          <Markdown source={spec.hypothesis ?? ""} empty="Aucune hypothèse renseignée dans la spec." />
        </Card>
        <Card>
          <CardHeader title="Événements récents" description={run.created_at ? `Créé le ${formatDate(run.created_at)}` : undefined} />
          {events.length === 0 ? (
            <div className="text-[13px] text-muted">Aucun événement.</div>
          ) : (
            <ul className="space-y-2">
              {events.map((e, i) => {
                const Icon = EVENT_ICONS[e.type] ?? Info;
                return (
                  <li key={`${e.t}-${i}`} className="flex items-start gap-2.5 text-[13px]">
                    <Icon size={14} strokeWidth={1.75} className={`mt-0.5 shrink-0 ${EVENT_TONES[e.type] ?? "text-muted"}`} aria-hidden />
                    <span className="min-w-0 flex-1 truncate text-fg" title={describeEvent(e)}>
                      {describeEvent(e)}
                    </span>
                    <span className="shrink-0 text-[12px] text-faint">{formatRelative(e.t)}</span>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}
