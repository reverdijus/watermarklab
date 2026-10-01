import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { CircleAlert, Microscope, Rocket, Target, Trophy } from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  KeyValue,
  Segmented,
  Select,
  Skeleton,
  Switch,
  Table,
  type Column,
} from "../ui";
import { Heatmap, ScatterPareto, type ParetoPoint } from "../charts";
import { useLaunch } from "../launch/LaunchProvider";
import {
  AUTO_QUALITY,
  bareMetric,
  bestUnderConstraint,
  busiestBenchmark,
  defaultConstraintOp,
  defaultQualityMetric,
  leaderValue,
  objectiveDirection,
  sliderRange,
  type Scored,
} from "../analysis";
import { errorMessage } from "../../lib/api";
import { formatDate, formatNumber, formatRelative, plural, shortId, toDate } from "../../lib/format";
import { formatMetric, formatMetricAxis, metricInfo, metricLabel, parseConstraint, satisfies, type Constraint } from "../../lib/metrics";
import { useLeaderboard, usePermissions } from "../../lib/queries";
import { runPath } from "../../lib/runs";
import type { LeaderRow, Objective, ProjectSummary } from "../../lib/types";

type Source = "benchmark" | "val";

export const rowId = (r: LeaderRow) => `${r.run_id}::${r.checkpoint ?? "val"}::${r.benchmark ?? ""}`;

/** Short unique label of a leaderboard row. */
export const rowLabel = (r: LeaderRow) => (r.checkpoint ? `${shortId(r.run_id)} · ${r.checkpoint}` : shortId(r.run_id));

const opSymbol = (op: string) => (op === ">=" ? "≥" : op === "<=" ? "≤" : op);

/** Rows scored on the two objective metrics. */
export function scoreRows(rows: LeaderRow[], quality: string, robustness: string): Scored<LeaderRow>[] {
  return rows.map((item) => ({ item, quality: leaderValue(item, quality), robustness: leaderValue(item, robustness) }));
}

/** Axis label of an objective metric (project label when it matches). */
function axisLabel(metric: string, objective?: Objective | null): string {
  if (metric === AUTO_QUALITY) return "PSNR / SNR (dB)";
  if (objective?.label && bareMetric(objective.metric) === bareMetric(metric)) return objective.label;
  return metricLabel(bareMetric(metric));
}

/** Key used to format values of an axis metric (the virtual quality axis is in dB). */
const formatKey = (metric: string) => (metric === AUTO_QUALITY ? "psnr" : metric);

function ConstraintSlider({
  metric,
  label,
  constraint,
  range,
  enabled,
  onEnabled,
  onValue,
}: {
  /** Metric key used to format values. */
  metric: string;
  /** Displayed name of the metric (defaults to its label). */
  label?: string;
  constraint: Constraint;
  range: { min: number; max: number; step: number };
  enabled: boolean;
  onEnabled: (v: boolean) => void;
  onValue: (v: number) => void;
}) {
  return (
    <div>
      <Switch
        size="sm"
        checked={enabled}
        onChange={onEnabled}
        label="Contrainte de qualité"
        description={enabled ? `${label ?? metricLabel(metric)} ${opSymbol(constraint.op)} ${formatMetric(metric, constraint.value)}` : "Désactivée : tous les points comptent"}
      />
      <input
        type="range"
        min={range.min}
        max={range.max}
        step={range.step}
        value={constraint.value}
        disabled={!enabled}
        onChange={(e) => onValue(Number(e.target.value))}
        className="no-drag mt-3 w-full accent-[var(--accent)] disabled:opacity-40"
        aria-label="Seuil de qualité"
      />
      <div className="mt-0.5 flex justify-between text-[11px] text-faint">
        <span className="tabular">{formatMetric(metric, range.min)}</span>
        <span className="tabular">{formatMetric(metric, range.max)}</span>
      </div>
    </div>
  );
}

export interface LeaderboardViewProps {
  /** Project whose objectives drive the axes (and filter the rows). */
  project?: ProjectSummary | null;
  /** Color points by project (cross-project leaderboard). */
  groupByProject?: boolean;
}

/**
 * Quality × robustness leaderboard: Pareto scatter with a quality constraint slider
 * highlighting the best point under constraint, checkpoints × attacks heatmap and a
 * sortable table. Source and benchmark selections live in the URL (`source`, `bench`).
 */
export function LeaderboardView({ project, groupByProject }: LeaderboardViewProps) {
  const navigate = useNavigate();
  const { openLaunch } = useLaunch();
  const { canWrite } = usePermissions();
  const [params, setParams] = useSearchParams();
  const defaultSource: Source = project?.best?.source === "val" ? "val" : "benchmark";
  const source: Source = params.get("source") === "val" ? "val" : params.get("source") === "benchmark" ? "benchmark" : defaultSource;
  const benchParam = params.get("bench") ?? "";
  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const lb = useLeaderboard({ project: project?.name, source });
  const data = lb.data;

  const benchmarks = data?.benchmarks ?? [];
  const bench =
    source === "benchmark"
      ? benchmarks.includes(benchParam)
        ? benchParam
        : project?.default_benchmark && benchmarks.includes(project.default_benchmark)
          ? project.default_benchmark
          : busiestBenchmark(data?.rows ?? [], benchmarks)
      : "";
  const rows = useMemo(
    () => (data?.rows ?? []).filter((r) => r.source === source && (source !== "benchmark" || !bench || r.benchmark === bench)),
    [data, source, bench],
  );

  // Axis metrics (defaults: project objectives).
  const qObjective = project?.objectives?.quality ?? null;
  const rObjective = project?.objectives?.robustness ?? null;
  const qualityOptions = useMemo(() => {
    const keys = [...new Set(rows.flatMap((r) => Object.keys(r.quality)))].sort();
    // Mixed modalities (cross-project validation view): offer the per-modality axis first.
    return keys.includes("psnr") && keys.includes("snr") ? [AUTO_QUALITY, ...keys] : keys;
  }, [rows]);
  const robustnessOptions = useMemo(() => {
    const q = new Set(qualityOptions);
    return [...new Set(rows.flatMap((r) => Object.keys(r.summary)))].filter((k) => !q.has(k) && k !== "loss").sort();
  }, [rows, qualityOptions]);
  const defaultQ = qObjective ? bareMetric(qObjective.metric) : defaultQualityMetric(rows, qualityOptions);
  const defaultR = rObjective ? bareMetric(rObjective.metric) : robustnessOptions.includes("bit_acc_mean") || !robustnessOptions.length ? "bit_acc_mean" : robustnessOptions[0];
  const [qPick, setQPick] = useState<string | null>(null);
  const [rPick, setRPick] = useState<string | null>(null);
  const qMetric = qPick ?? defaultQ;
  const rMetric = rPick ?? defaultR;
  const qDir = objectiveDirection(qMetric, qObjective);
  const rDir = objectiveDirection(rMetric, rObjective);
  const qLabel = axisLabel(qMetric, qObjective);
  const rLabel = axisLabel(rMetric, rObjective);
  const qKey = formatKey(qMetric);
  // Ratios (bit accuracy, TPR) never exceed 1: pin the axis so a lone point is not stretched.
  const rMax = metricInfo(rMetric).kind === "ratio" ? 1 : undefined;

  const scored = useMemo(() => scoreRows(rows, qMetric, rMetric), [rows, qMetric, rMetric]);
  const qValues = scored.map((s) => s.quality).filter((v): v is number => v !== null);

  // Constraint on quality (defaults to the project's constraint when the metric matches).
  const projectConstraint =
    qObjective && bareMetric(qObjective.metric) === qMetric ? parseConstraint(qObjective.constraint) : null;
  const range = sliderRange(qValues, projectConstraint?.value);
  const [constraintValues, setConstraintValues] = useState<Record<string, number>>({});
  const [constraintToggles, setConstraintToggles] = useState<Record<string, boolean>>({});
  const constraintOn = constraintToggles[qMetric] ?? !!projectConstraint;
  const constraint: Constraint = {
    op: projectConstraint?.op ?? defaultConstraintOp(qDir),
    value: constraintValues[qMetric] ?? projectConstraint?.value ?? (qDir === "maximize" ? range.min : range.max),
  };
  const activeConstraint = constraintOn ? constraint : null;
  // Mixed modalities (virtual "PSNR / SNR" axis): an audio SNR and an image PSNR are not
  // comparable, so neither a Pareto front nor a "best under constraint" makes sense.
  const mixed = qMetric === AUTO_QUALITY;
  const best = mixed ? null : bestUnderConstraint(scored, activeConstraint, rDir, qDir);
  const bestId = best ? rowId(best.item) : null;
  const nFeasible = scored.filter((s) => s.quality !== null && (!activeConstraint || satisfies(s.quality, activeConstraint))).length;

  const points: ParetoPoint[] = scored
    .filter((s) => s.quality !== null && s.robustness !== null)
    .map((s) => ({
      id: rowId(s.item),
      x: s.quality!,
      y: s.robustness!,
      label: `${s.item.experiment} · ${s.item.checkpoint ?? shortId(s.item.run_id)}`,
      group: groupByProject ? s.item.project : s.item.experiment,
      meta: {
        Run: shortId(s.item.run_id),
        ...(s.item.checkpoint ? { Checkpoint: s.item.checkpoint } : {}),
        ...(s.item.benchmark ? { Benchmark: s.item.benchmark } : {}),
        ...(groupByProject ? { Projet: s.item.project } : {}),
      },
    }));

  // Heatmap: best rows × attacks.
  const heatKeys = useMemo(() => {
    const keys = new Set<string>();
    for (const r of rows) for (const att of Object.values(r.robustness)) Object.keys(att).forEach((k) => keys.add(k));
    return [...keys].sort((a, b) => (a === "bit_acc" ? -1 : b === "bit_acc" ? 1 : a.localeCompare(b, "en", { numeric: true })));
  }, [rows]);
  const [heatPick, setHeatPick] = useState("bit_acc");
  const heatMetric = heatKeys.includes(heatPick) ? heatPick : (heatKeys[0] ?? "bit_acc");
  const heatRows = useMemo(() => {
    const ranked = [...scored]
      .filter((s) => Object.keys(s.item.robustness).length > 0)
      .sort((a, b) => {
        const av = a.robustness ?? -Infinity;
        const bv = b.robustness ?? -Infinity;
        return rDir === "maximize" ? bv - av : av - bv;
      });
    return ranked.slice(0, 14);
  }, [scored, rDir]);
  const attacks = useMemo(() => {
    const set = new Set(heatRows.flatMap((s) => Object.keys(s.item.robustness)));
    return [...set].sort((a, b) => (a === "none" || a === "identity" ? -1 : b === "none" || b === "identity" ? 1 : a.localeCompare(b)));
  }, [heatRows]);

  const tprKeys = robustnessOptions.filter((k) => k.startsWith("tpr@") && k.endsWith("_mean") && k !== rMetric).slice(0, 2);

  const openRow = (r: LeaderRow) => navigate(runPath(r.run_id, r.source === "benchmark" ? "evals" : undefined));

  const columns: Column<Scored<LeaderRow>>[] = [
    {
      id: "run",
      header: "Run",
      cell: (s) => (
        <div className="min-w-0 max-w-[260px]">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium text-fg">{s.item.experiment}</span>
            {rowId(s.item) === bestId && (
              <Badge tone="accent" icon={Trophy}>
                meilleur
              </Badge>
            )}
          </div>
          <div className="truncate font-mono text-[11.5px] text-muted">{shortId(s.item.run_id)}</div>
        </div>
      ),
      sortValue: (s) => s.item.experiment,
    },
    ...(groupByProject
      ? [{ id: "project", header: "Projet", cell: (s: Scored<LeaderRow>) => <span className="text-muted">{s.item.project}</span>, sortValue: (s: Scored<LeaderRow>) => s.item.project }]
      : []),
    ...(source === "benchmark"
      ? [
          {
            id: "ckpt",
            header: "Checkpoint",
            cell: (s: Scored<LeaderRow>) => <span className="font-mono text-[12px] text-fg">{s.item.checkpoint ?? "—"}</span>,
            sortValue: (s: Scored<LeaderRow>) => s.item.checkpoint,
          },
        ]
      : []),
    {
      id: "quality",
      header: qLabel,
      align: "right",
      cell: (s) => <span className="tabular">{formatMetric(qKey, s.quality)}</span>,
      sortValue: (s) => s.quality,
    },
    {
      id: "robustness",
      header: rLabel,
      align: "right",
      cell: (s) => <span className="tabular font-medium">{formatMetric(rMetric, s.robustness)}</span>,
      sortValue: (s) => (s.robustness === null ? null : rDir === "maximize" ? s.robustness : -s.robustness),
    },
    ...tprKeys.map<Column<Scored<LeaderRow>>>((k) => ({
      id: k,
      header: metricLabel(k),
      align: "right",
      cell: (s) => <span className="tabular text-muted">{formatMetric(k, s.item.summary[k])}</span>,
      sortValue: (s) => s.item.summary[k],
    })),
    ...(activeConstraint
      ? [
          {
            id: "feasible",
            header: "Contrainte",
            align: "center" as const,
            cell: (s: Scored<LeaderRow>) =>
              s.quality === null ? (
                <span className="text-faint">—</span>
              ) : satisfies(s.quality, activeConstraint) ? (
                <Badge tone="success">respectée</Badge>
              ) : (
                <Badge tone="neutral">hors seuil</Badge>
              ),
            sortValue: (s: Scored<LeaderRow>) => (s.quality === null ? null : satisfies(s.quality, activeConstraint)),
          },
        ]
      : []),
    {
      id: "date",
      header: "Date",
      align: "right",
      cell: (s) => <span className="whitespace-nowrap text-muted">{formatRelative(s.item.created_at)}</span>,
      sortValue: (s) => toDate(s.item.created_at)?.getTime(),
    },
  ];

  const controls = (
    <div className="mb-4 flex flex-wrap items-end gap-3">
      <div>
        <div className="mb-1.5 text-[13px] font-medium text-fg">Source</div>
        <Segmented<Source>
          size="md"
          value={source}
          onChange={(v) => setParam("source", v)}
          options={[
            { value: "benchmark", label: "Benchmark", title: "Évaluations sur un benchmark figé (comparables entre elles)" },
            { value: "val", label: "Validation", title: "Dernières métriques de validation des runs (hors essais HPO)" },
          ]}
        />
      </div>
      {source === "benchmark" && (
        <Select
          label="Benchmark"
          size="sm"
          value={bench}
          onChange={(v) => setParam("bench", v)}
          options={benchmarks.map((b) => ({ value: b, label: b }))}
          placeholder={benchmarks.length ? undefined : "Aucun benchmark évalué"}
          className="w-48"
        />
      )}
      <Select
        label="Axe qualité"
        size="sm"
        value={qMetric}
        onChange={setQPick}
        options={[...new Set([qMetric, ...qualityOptions])].map((k) => ({ value: k, label: axisLabel(k, qObjective) }))}
        className="w-48"
      />
      <Select
        label="Axe robustesse"
        size="sm"
        value={rMetric}
        onChange={setRPick}
        options={[...new Set([rMetric, ...robustnessOptions])].map((k) => ({ value: k, label: axisLabel(k, rObjective) }))}
        className="w-52"
      />
      {data && (
        <span className="mb-1.5 ml-auto text-[12.5px] text-muted">
          {plural(rows.length, source === "benchmark" ? "évaluation" : "run")}
        </span>
      )}
    </div>
  );

  if (lb.isError) {
    return (
      <>
        {controls}
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Impossible de charger le classement"
          action={
            <Button size="sm" onClick={() => void lb.refetch()}>
              Réessayer
            </Button>
          }
        >
          {errorMessage(lb.error)}
        </Callout>
      </>
    );
  }

  if (lb.isLoading) {
    return (
      <>
        {controls}
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Skeleton className="h-[420px] rounded-xl xl:col-span-2" />
          <Skeleton className="h-[420px] rounded-xl" />
        </div>
      </>
    );
  }

  if (!rows.length) {
    return (
      <>
        {controls}
        <Card padding="none">
          {source === "benchmark" ? (
            <EmptyState
              icon={Microscope}
              title="Aucune évaluation sur benchmark"
              description="Évaluez un checkpoint sur un benchmark figé (bouton « Évaluer » d'un run) pour comparer les schémas sur des données et attaques identiques."
              action={
                <>
                  <Button onClick={() => setParam("source", "val")}>Voir la validation</Button>
                  {canWrite && (
                    <Button variant="primary" icon={Microscope} onClick={() => openLaunch({ kind: "eval", project: project?.name })}>
                      Évaluer un checkpoint
                    </Button>
                  )}
                </>
              }
            />
          ) : (
            <EmptyState
              icon={Rocket}
              title="Aucun run avec des métriques de validation"
              description="Les runs apparaissent ici dès leur première validation (val/*)."
              action={
                canWrite ? (
                  <Button variant="primary" icon={Rocket} onClick={() => openLaunch({ project: project?.name })}>
                    Nouveau lancement
                  </Button>
                ) : undefined
              }
            />
          )}
        </Card>
      </>
    );
  }

  return (
    <div>
      {controls}
      {mixed && (
        <Callout tone="info" icon={CircleAlert} title="Comparaison inter-modalités" className="mb-4">
          Les projets affichés mêlent des modalités : un SNR audio et un PSNR image/vidéo ne se comparent pas. Le front de Pareto et le
          « meilleur sous contrainte » ne sont donc pas calculés dans cette vue — sélectionnez un projet (ou un axe de qualité précis) pour
          les obtenir.
        </Callout>
      )}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader
            title={mixed ? `Nuage — ${qLabel} × ${rLabel}` : `Front de Pareto — ${qLabel} × ${rLabel}`}
            description={
              mixed
                ? "Chaque point est un checkpoint évalué, coloré par projet ; sans front de Pareto (modalités non comparables). Cliquez un point pour ouvrir le run."
                : "Chaque point est un checkpoint évalué ; la ligne pointillée relie les compromis non dominés (avec ou sans contrainte). Cliquez un point pour ouvrir le run."
            }
          />
          <ScatterPareto
            points={points}
            xLabel={qLabel}
            yLabel={rLabel}
            xDirection={qDir}
            yDirection={rDir}
            xFormatter={(v) => formatMetricAxis(qKey, v)}
            yFormatter={(v) => formatMetricAxis(rMetric, v)}
            xConstraint={mixed ? null : activeConstraint}
            highlightId={bestId}
            showFront={!mixed}
            yMax={rMax}
            onPointClick={(p) => {
              const s = scored.find((x) => rowId(x.item) === p.id);
              if (s) openRow(s.item);
            }}
            height={400}
            empty="Aucun point n'a les deux métriques sélectionnées."
          />
        </Card>
        <Card className="flex flex-col">
          <CardHeader title="Meilleur sous contrainte" icon={Target} description="Robustesse maximale parmi les points qui respectent le seuil de qualité." />
          {mixed ? (
            <div className="flex-1 text-[13px] text-muted">
              Indisponible pour des modalités mélangées : choisissez un projet ou un axe de qualité (PSNR ou SNR) pour comparer des points
              homogènes.
            </div>
          ) : (
            <>
              <ConstraintSlider
                metric={qKey}
                label={qLabel}
                constraint={constraint}
                range={range}
                enabled={constraintOn}
                onEnabled={(v) => setConstraintToggles((m) => ({ ...m, [qMetric]: v }))}
                onValue={(v) => setConstraintValues((m) => ({ ...m, [qMetric]: v }))}
              />
              <div className="mt-5 flex-1 border-t border-border pt-4">
                {best ? (
                  <>
                    <div className="flex items-center gap-2">
                      <Trophy size={15} strokeWidth={1.75} className="text-accent" aria-hidden />
                      <span className="truncate text-[14.5px] font-medium text-fg">{best.item.experiment}</span>
                    </div>
                    <div className="mt-0.5 truncate font-mono text-[11.5px] text-muted">{rowLabel(best.item)}</div>
                    <div className="mt-3 grid grid-cols-2 gap-3">
                      <div>
                        <div className="truncate text-[11.5px] text-muted">{qLabel}</div>
                        <div className="tabular font-serif text-[20px] text-fg">{formatMetric(qKey, best.quality)}</div>
                      </div>
                      <div>
                        <div className="truncate text-[11.5px] text-muted">{rLabel}</div>
                        <div className="tabular font-serif text-[20px] text-fg">{formatMetric(rMetric, best.robustness)}</div>
                      </div>
                    </div>
                    <KeyValue
                      dense
                      className="mt-3"
                      items={[
                        ...(groupByProject ? [{ label: "Projet", value: best.item.project }] : []),
                        ...(best.item.benchmark ? [{ label: "Benchmark", value: best.item.benchmark }] : []),
                        { label: "Date", value: formatDate(best.item.created_at) },
                      ]}
                    />
                    <Button size="sm" className="mt-4" onClick={() => openRow(best.item)}>
                      Ouvrir le run
                    </Button>
                  </>
                ) : (
                  <div className="text-[13px] text-muted">Aucun point ne respecte ce seuil. Assouplissez la contrainte avec le curseur.</div>
                )}
              </div>
              <div className="mt-4 text-[12px] text-muted">
                {formatNumber(nFeasible, { maxDigits: 0 })} / {formatNumber(scored.length, { maxDigits: 0 })} points respectent la contrainte
              </div>
            </>
          )}
        </Card>
      </div>

      {heatRows.length > 0 && attacks.length > 0 && (
        <Card className="mt-4">
          <CardHeader
            title={`Robustesse par attaque — ${metricLabel(heatMetric)}`}
            description={`Les ${heatRows.length} meilleurs ${source === "benchmark" ? "checkpoints" : "runs"} selon ${rLabel.toLowerCase()} ; cliquez une ligne pour ouvrir le run.`}
            actions={
              heatKeys.length > 1 ? (
                <Select
                  size="sm"
                  value={heatMetric}
                  onChange={setHeatPick}
                  options={heatKeys.map((k) => ({ value: k, label: metricLabel(k) }))}
                  className="w-48"
                />
              ) : undefined
            }
          />
          <Heatmap
            rows={heatRows.map((s) => rowLabel(s.item))}
            columns={attacks}
            values={heatRows.map((s) => attacks.map((a) => s.item.robustness[a]?.[heatMetric] ?? null))}
            min={heatMetric === "bit_acc" ? 0.5 : 0}
            max={1}
            formatter={(v) => formatMetric(heatMetric, v)}
            onCellClick={(row) => {
              const s = heatRows.find((x) => rowLabel(x.item) === row);
              if (s) openRow(s.item);
            }}
          />
        </Card>
      )}

      <Table
        className="mt-4"
        columns={columns}
        rows={scored}
        rowKey={(s) => rowId(s.item)}
        onRowClick={(s) => openRow(s.item)}
        rowClassName={(s) => (rowId(s.item) === bestId ? "bg-accent-soft/40" : undefined)}
        initialSort={{ id: "robustness", desc: true }}
        dense
        maxHeight={560}
      />
    </div>
  );
}
