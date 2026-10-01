import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import type { EChartsOption } from "echarts";
import { ArrowUpRight, CircleAlert, Clock, Cpu, FileCode, FlaskConical, ListChecks, RotateCw, Square, Trophy, Wallet } from "lucide-react";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  EmptyState,
  Kpi,
  Page,
  PageHeader,
  PageSkeleton,
  Segmented,
  StatusBadge,
  Table,
  Tabs,
  Tooltip,
  copyText,
  useConfirm,
  useToast,
  type Column,
  type TabItem,
  type Tone,
} from "../components/ui";
import {
  BarChart,
  EChart,
  LineChart,
  ParallelCoords,
  ScatterPareto,
  axisBase,
  escapeHtml,
  legendBase,
  seriesColor,
  tooltipBase,
  useChartTokens,
  withAlpha,
  type LineSeries,
  type ParallelDimension,
} from "../components/charts";
import { useLaunch } from "../components/launch/LaunchProvider";
import { NotesEditor } from "../components/NotesEditor";
import { cumulativeBest } from "../components/analysis";
import { studyProgress, trialCounts } from "../components/StudiesTable";
import { asDict, asList } from "../components/SpecSummaries";
import { ApiError, errorMessage } from "../lib/api";
import { formatDate, formatDuration, formatEur, formatHours, formatInt, formatPercent, formatRelative, formatSignificant, plural, secondsBetween, shortId } from "../lib/format";
import { useNow } from "../lib/hooks";
import { formatMetric, formatMetricAxis, metricLabel, parseConstraint, type Constraint } from "../lib/metrics";
import { useCancelJob, useJobs, usePermissions, useSetStudyNotes, useStudy } from "../lib/queries";
import { experimentPath, hpoPlanPath, isActiveStatus, projectPath, runPath } from "../lib/runs";
import type { Direction, Objective, StudyDetail as StudyData, Trial } from "../lib/types";

type TabId = "analysis" | "notes";
const TAB_IDS: TabId[] = ["analysis", "notes"];

const TRIAL_STATES: Record<Trial["state"], { label: string; tone: Tone }> = {
  COMPLETE: { label: "Terminé", tone: "success" },
  PRUNED: { label: "Élagué", tone: "warning" },
  FAIL: { label: "Échec", tone: "danger" },
  RUNNING: { label: "En cours", tone: "accent" },
  WAITING: { label: "En attente", tone: "neutral" },
};

const fmtParam = (v: unknown): string =>
  typeof v === "number" ? formatSignificant(v, 4) : v === null || v === undefined ? "—" : typeof v === "object" ? JSON.stringify(v) : String(v);

const objectiveName = (o: Objective) => metricLabel(o.metric, { withGroup: true });

/** Optimisation history: value of each trial and running best for one objective. */
function HistoryChart({ trials, objective, index }: { trials: Trial[]; objective: Objective; index: number }) {
  const t = useChartTokens();
  const option = useMemo<EChartsOption>(() => {
    const ax = axisBase(t);
    const sorted = [...trials].sort((a, b) => a.number - b.number);
    const value = (tr: Trial) => (tr.state === "COMPLETE" ? (tr.values?.[index] ?? null) : null);
    const best = cumulativeBest(sorted.map(value), objective.direction);
    const fmt = (v: number) => formatMetric(objective.metric, v);
    const pruned = sorted
      .filter((tr) => tr.state === "PRUNED" && tr.intermediate.length)
      .map((tr) => ({ value: [tr.number, tr.intermediate[tr.intermediate.length - 1][1]], trial: tr }));
    return {
      grid: { left: 8, right: 16, top: 34, bottom: 24, containLabel: true },
      legend: { ...legendBase(t) },
      tooltip: {
        ...tooltipBase(t),
        trigger: "item",
        formatter: (raw: unknown) => {
          const p = raw as { seriesName: string; value: [number, number] };
          return `<div style="font-weight:600">Essai ${p.value[0]}</div><div style="display:flex;justify-content:space-between;gap:16px"><span style="color:${t.muted}">${escapeHtml(p.seriesName)}</span><span>${escapeHtml(fmt(p.value[1]))}</span></div>`;
        },
      },
      xAxis: { ...ax, type: "value", name: "Essai", nameLocation: "middle", nameGap: 22, minInterval: 1, splitLine: { show: false } },
      yAxis: { ...ax, type: "value", scale: true, axisLine: { show: false }, axisLabel: { ...ax.axisLabel, formatter: (v: number) => formatMetricAxis(objective.metric, v) } },
      series: [
        {
          id: "values",
          name: "Essais terminés",
          type: "scatter",
          symbolSize: 8,
          itemStyle: { color: seriesColor(t, 1), borderColor: t.surface, borderWidth: 1 },
          data: sorted.filter((tr) => value(tr) !== null).map((tr) => [tr.number, value(tr)]),
          z: 3,
        },
        {
          id: "pruned",
          name: "Élagués (dernière valeur)",
          type: "scatter",
          symbol: "emptyCircle",
          symbolSize: 7,
          itemStyle: { color: t.faint },
          data: pruned.map((p) => p.value),
          z: 2,
        },
        {
          id: "best",
          name: "Meilleur cumulé",
          type: "line",
          step: "end",
          showSymbol: false,
          lineStyle: { color: t.accent, width: 2 },
          itemStyle: { color: t.accent },
          data: sorted.map((tr, i) => [tr.number, best[i]]).filter((d) => d[1] !== null),
          z: 4,
          tooltip: { show: false },
        },
      ],
    } as EChartsOption;
  }, [t, trials, objective, index]);
  const hasData = trials.some((tr) => tr.state === "COMPLETE" && tr.values?.[index] != null);
  return <EChart option={option} height={300} empty={hasData ? undefined : "Aucun essai terminé pour l'instant."} />;
}

/** Intermediate values reported at each validation (pruned trials dashed and faded). */
function IntermediateChart({ trials, objective, bestTrials }: { trials: Trial[]; objective: Objective; bestTrials: Set<number> }) {
  const t = useChartTokens();
  const series: LineSeries[] = trials
    .filter((tr) => tr.intermediate.length > 0)
    .sort((a, b) => Number(bestTrials.has(a.number)) - Number(bestTrials.has(b.number)))
    .map((tr) => {
      const isBest = bestTrials.has(tr.number);
      const color =
        tr.state === "PRUNED"
          ? withAlpha(t.faint, 0.8)
          : tr.state === "RUNNING"
            ? t.accent
            : isBest
              ? t.text
              : withAlpha(seriesColor(t, 1), 0.65);
      return {
        name: `Essai ${tr.number}${tr.state === "PRUNED" ? " (élagué)" : isBest ? " (meilleur)" : ""}`,
        x: tr.intermediate.map((p) => p[0]),
        y: tr.intermediate.map((p) => p[1]),
        color,
        dashed: tr.state === "PRUNED",
        width: isBest ? 2.5 : 1.25,
      };
    });
  return (
    <LineChart
      series={series}
      height={300}
      legend={false}
      xLabel="Step"
      yFormatter={(v) => formatMetricAxis(objective.metric, v)}
      valueFormatter={(v) => formatMetric(objective.metric, v)}
      empty="Aucune valeur intermédiaire rapportée."
    />
  );
}

function StudyHeader({ study }: { study: StudyData }) {
  const { openLaunch } = useLaunch();
  const { canWrite, canViewCode } = usePermissions();
  const jobs = useJobs();
  const cancel = useCancelJob();
  const confirm = useConfirm();
  const toast = useToast();
  const now = useNow(15000);
  const job = (jobs.data ?? []).find((j) => j.study_id === study.study_id && isActiveStatus(j.status));
  const duration = secondsBetween(study.created_at, study.ended_at, now);

  const stop = async () => {
    if (!job) return;
    const ok = await confirm({
      title: "Arrêter l'étude ?",
      message: "Les essais en cours reçoivent un signal d'arrêt ; les essais terminés et le journal Optuna sont conservés.",
      confirmLabel: "Arrêter l'étude",
      cancelLabel: "Continuer",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await cancel.mutateAsync(job.job_id);
      toast.success("Arrêt demandé", shortId(study.study_id));
    } catch (err) {
      toast.error("Impossible d'arrêter l'étude", errorMessage(err));
    }
  };

  return (
    <PageHeader
      back={{ to: projectPath(study.project, "hpo"), label: "HPO" }}
      eyebrow={
        <span className="inline-flex items-center gap-1.5">
          <Link to={projectPath(study.project)} className="no-drag hover:text-fg">
            {study.project}
          </Link>
          <span className="text-faint">/</span>
          {canViewCode ? (
            <Link to={hpoPlanPath(study.project, study.plan)} className="no-drag hover:text-fg">
              plan {study.plan}
            </Link>
          ) : (
            <span>plan {study.plan}</span>
          )}
        </span>
      }
      title={
        <span className="inline-flex flex-wrap items-center gap-3">
          Étude {study.plan}
          <StatusBadge status={study.status} size="md" />
          {study.objectives.length > 1 && (
            <Badge tone="info" size="md">
              multi-objectif
            </Badge>
          )}
        </span>
      }
      actions={
        canWrite ? (
          <>
            {job && (
              <Button variant="danger" icon={Square} onClick={() => void stop()} loading={cancel.isPending}>
                Arrêter
              </Button>
            )}
            <Button icon={FileCode} to={hpoPlanPath(study.project, study.plan)}>
              Ouvrir le plan
            </Button>
            <Button icon={RotateCw} onClick={() => openLaunch({ kind: "hpo", project: study.project, plan: study.plan, host: study.host })}>
              Relancer
            </Button>
          </>
        ) : undefined
      }
      meta={
        <>
          <Tooltip content="Copier l'identifiant">
            <button
              type="button"
              className="no-drag font-mono text-[12px] text-muted hover:text-fg"
              onClick={() => void copyText(study.study_id).then((ok) => ok && toast.info("Identifiant copié"))}
            >
              {study.study_id}
            </button>
          </Tooltip>
          {canViewCode ? (
            <Link to={experimentPath(study.project, study.experiment)} className="no-drag inline-flex items-center gap-1.5 hover:text-fg">
              <FlaskConical size={13} strokeWidth={1.75} aria-hidden /> {study.experiment}
            </Link>
          ) : (
            <span className="inline-flex items-center gap-1.5">
              <FlaskConical size={13} strokeWidth={1.75} aria-hidden /> {study.experiment}
            </span>
          )}
          <span className="inline-flex items-center gap-1.5">
            <Cpu size={13} strokeWidth={1.75} aria-hidden /> {study.host}
          </span>
          <span className="inline-flex items-center gap-1.5" title={`Créée le ${formatDate(study.created_at)}`}>
            <Clock size={13} strokeWidth={1.75} aria-hidden /> {formatDuration(duration)}
            {study.ended_at ? ` · terminée ${formatRelative(study.ended_at, now)}` : ` · démarrée ${formatRelative(study.created_at, now)}`}
          </span>
        </>
      }
    />
  );
}

function StudyNotesTab({ study }: { study: StudyData }) {
  const save = useSetStudyNotes(study.study_id);
  const { canWrite } = usePermissions();
  return (
    <NotesEditor
      readOnly={!canWrite}
      draftKey={`study-notes:${study.study_id}`}
      notes={study.notes ?? ""}
      save={(notes) => save.mutateAsync(notes)}
      saving={save.isPending}
      description={
        <>
          Hypothèse, observations et conclusion de l'étude — enregistrées dans <span className="font-mono">notes.md</span> du dossier d'étude.
        </>
      }
      placeholder={"## Observations\n\n- …\n\n## Conclusion\n\n- …"}
    />
  );
}

export default function StudyDetail() {
  const { id = "" } = useParams();
  const studyId = decodeURIComponent(id);
  const study = useStudy(studyId);
  const navigate = useNavigate();
  const now = useNow(15000);
  const [objIndex, setObjIndex] = useState(0);
  const [params, setParams] = useSearchParams();
  const tabParam = params.get("tab") as TabId | null;
  const tab: TabId = tabParam && TAB_IDS.includes(tabParam) ? tabParam : "analysis";
  const setTab = (t: TabId) => {
    const next = new URLSearchParams(params);
    if (t === "analysis") next.delete("tab");
    else next.set("tab", t);
    setParams(next, { replace: true });
  };

  const data = study.data;
  const trials = data?.trials ?? [];
  const spec = useMemo(() => asDict(data?.spec), [data]);
  const space = useMemo(() => asDict(spec.space), [spec]);
  const objectives = data?.objectives ?? [];
  const k = Math.min(objIndex, Math.max(0, objectives.length - 1));
  const objective: Objective = objectives[k] ?? { metric: "value", direction: "maximize" };
  const bestTrials = useMemo(() => new Set((data?.best ?? []).map((b) => b.trial)), [data]);
  const paramKeys = useMemo(() => {
    const keys = Object.keys(space);
    const fromTrials = [...new Set(trials.flatMap((tr) => Object.keys(tr.params ?? {})))];
    return [...keys, ...fromTrials.filter((x) => !keys.includes(x))];
  }, [space, trials]);

  const constraintFor = (metric: string): Constraint | null => {
    const c = asList(spec.constraints)
      .map(asDict)
      .find((x) => x.metric === metric);
    return c ? parseConstraint(`${String(c.op ?? ">=")} ${String(c.value)}`) : null;
  };

  if (study.isLoading) return <PageSkeleton />;
  if (study.isError || !data) {
    const notFound = study.error instanceof ApiError && study.error.status === 404;
    return (
      <Page>
        <EmptyState
          icon={CircleAlert}
          title={notFound ? "Étude introuvable" : "Impossible de charger l'étude"}
          description={notFound ? `Aucune étude « ${studyId} » dans le workspace.` : errorMessage(study.error)}
          action={
            <>
              <Button to="/">Accueil</Button>
              {!notFound && (
                <Button variant="primary" onClick={() => void study.refetch()}>
                  Réessayer
                </Button>
              )}
            </>
          }
        />
      </Page>
    );
  }

  const hours = (secondsBetween(data.created_at, data.ended_at, now) ?? 0) / 3600;
  const complete = trials.filter((tr) => tr.state === "COMPLETE" && tr.values);
  const multi = objectives.length > 1;
  const best = data.best[0];

  // Parallel coordinates: params → objective.
  const dims: ParallelDimension[] = paramKeys.map((key) => {
    const def = asDict(space[key]);
    const values = trials.map((tr) => tr.params?.[key]);
    const categorical = def.type === "categorical" || values.some((v) => v !== undefined && v !== null && typeof v !== "number");
    return {
      key,
      label: key.split(".").slice(-2).join("."),
      type: categorical ? "category" : "value",
      categories: categorical
        ? [...new Set([...asList(def.choices), ...values].filter((v) => v !== undefined && v !== null).map(fmtParam))]
        : undefined,
      log: !categorical && def.log === true,
      formatter: (v: number) => formatSignificant(v, 3),
    };
  });
  dims.push({
    key: "__objective",
    label: metricLabel(objective.metric),
    formatter: (v: number) => formatMetric(objective.metric, v),
    inverse: objective.direction === "minimize",
  });
  const parallelRows = trials
    .filter((tr) => tr.state === "COMPLETE" || tr.state === "PRUNED")
    .map((tr) => ({
      id: String(tr.number),
      label: `Essai ${tr.number}${tr.state === "PRUNED" ? " (élagué)" : ""}`,
      highlight: bestTrials.has(tr.number),
      values: [
        ...paramKeys.map((key, i) => {
          const v = tr.params?.[key];
          if (v === undefined || v === null) return null;
          return dims[i].type === "category" ? fmtParam(v) : typeof v === "number" ? v : null;
        }),
        tr.state === "COMPLETE" ? (tr.values?.[k] ?? null) : null,
      ],
    }));

  const importances = Object.entries(data.importances ?? {}).sort((a, b) => b[1] - a[1]);

  const trialColumns: Column<Trial>[] = [
    { id: "number", header: "#", width: 48, cell: (tr) => <span className="tabular text-muted">{tr.number}</span>, sortValue: (tr) => tr.number },
    {
      id: "state",
      header: "État",
      cell: (tr) => (
        <span className="inline-flex items-center gap-1.5">
          <Badge tone={TRIAL_STATES[tr.state]?.tone ?? "neutral"} dot pulse={tr.state === "RUNNING"}>
            {TRIAL_STATES[tr.state]?.label ?? tr.state}
          </Badge>
          {bestTrials.has(tr.number) && (
            <Tooltip content={multi ? "Sur le front de Pareto" : "Meilleur essai"}>
              <Trophy size={13} strokeWidth={1.75} className="text-accent" aria-hidden />
            </Tooltip>
          )}
        </span>
      ),
      sortValue: (tr) => tr.state,
    },
    ...paramKeys.map<Column<Trial>>((key) => ({
      id: `p:${key}`,
      header: <span className="font-mono text-[11px]">{key}</span>,
      title: key,
      align: "right",
      cell: (tr) => <span className="tabular">{fmtParam(tr.params?.[key])}</span>,
      sortValue: (tr) => {
        const v = tr.params?.[key];
        return typeof v === "number" || typeof v === "string" || typeof v === "boolean" ? v : null;
      },
    })),
    ...objectives.map<Column<Trial>>((o, i) => ({
      id: `v:${i}`,
      header: objectiveName(o),
      align: "right",
      cell: (tr) => <span className="tabular font-medium">{tr.values ? formatMetric(o.metric, tr.values[i]) : "—"}</span>,
      sortValue: (tr) => (tr.values ? tr.values[i] : null),
    })),
    {
      id: "duration",
      header: "Durée",
      align: "right",
      cell: (tr) => <span className="text-muted">{formatDuration(tr.duration_s)}</span>,
      sortValue: (tr) => tr.duration_s,
    },
    {
      id: "run",
      header: "Run",
      align: "right",
      cell: (tr) =>
        tr.run_id ? (
          <Link
            to={runPath(tr.run_id)}
            onClick={(e) => e.stopPropagation()}
            className="no-drag inline-flex items-center gap-1 font-mono text-[11.5px] text-muted hover:text-fg"
          >
            {shortId(tr.run_id)} <ArrowUpRight size={12} strokeWidth={1.75} aria-hidden />
          </Link>
        ) : (
          <span className="text-faint">—</span>
        ),
    },
  ];

  const dirOf = (i: number): Direction => objectives[i]?.direction ?? "maximize";
  const tabItems: TabItem<TabId>[] = [
    { id: "analysis", label: "Analyse" },
    { id: "notes", label: "Notes" },
  ];

  if (tab === "notes") {
    return (
      <Page width="wide">
        <StudyHeader study={data} />
        <Tabs items={tabItems} value={tab} onChange={setTab} className="mb-6" />
        <StudyNotesTab study={data} />
      </Page>
    );
  }

  return (
    <Page width="wide">
      <StudyHeader study={data} />
      <Tabs items={tabItems} value={tab} onChange={setTab} className="mb-6" />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Kpi
          icon={ListChecks}
          label="Essais"
          value={`${formatInt(data.n_trials)}${data.budget.max_trials ? ` / ${formatInt(data.budget.max_trials)}` : ""}`}
          sub={trialCounts(data)}
          progress={studyProgress(data)}
        />
        <Kpi
          icon={Clock}
          label="Durée"
          value={formatHours(hours)}
          sub={data.budget.max_hours ? `sur ${formatHours(data.budget.max_hours)} max.` : "sans limite de durée"}
          progress={data.budget.max_hours ? hours / data.budget.max_hours : undefined}
        />
        <Kpi
          icon={Wallet}
          label="Coût"
          value={formatEur(data.cost_eur)}
          sub={data.budget.max_eur ? `sur ${formatEur(data.budget.max_eur, { digits: 0 })} max.` : "sans plafond"}
          progress={data.budget.max_eur ? data.cost_eur / data.budget.max_eur : undefined}
          progressTone={data.budget.max_eur && data.cost_eur > data.budget.max_eur * 0.9 ? "warning" : "accent"}
        />
        <Kpi
          icon={Trophy}
          label={multi ? "Front de Pareto" : `Meilleur · ${objectiveName(objectives[0] ?? objective)}`}
          value={multi ? `${data.best.length} essai${data.best.length > 1 ? "s" : ""}` : best ? formatMetric(objectives[0]?.metric ?? "", best.values[0]) : "—"}
          sub={best && !multi ? `essai ${best.trial} · ${data.budget.parallel} en parallèle` : `${plural(data.budget.parallel, "essai")} en parallèle`}
          onClick={best?.run_id && !multi ? () => navigate(runPath(best.run_id!)) : undefined}
        />
      </div>

      {multi && (
        <div className="mt-8 flex items-center gap-3">
          <span className="text-[13px] text-muted">Objectif affiché</span>
          <Segmented
            value={String(k)}
            onChange={(v) => setObjIndex(Number(v))}
            options={objectives.map((o, i) => ({ value: String(i), label: objectiveName(o) }))}
          />
        </div>
      )}

      <div className="mt-6 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader
            title="Historique d'optimisation"
            description={`${objectiveName(objective)} par essai et meilleure valeur cumulée (${objective.direction === "maximize" ? "à maximiser" : "à minimiser"}).`}
          />
          <HistoryChart trials={trials} objective={objective} index={k} />
        </Card>
        {multi ? (
          <Card>
            <CardHeader title="Front de Pareto" description="Essais terminés ; les points non dominés sont reliés. Cliquez un point pour ouvrir le run." />
            <ScatterPareto
              points={complete.map((tr) => ({
                id: String(tr.number),
                x: tr.values![0],
                y: tr.values![1],
                label: `Essai ${tr.number}`,
                group: bestTrials.has(tr.number) ? "Front" : "Essais",
                meta: Object.fromEntries(paramKeys.map((key) => [key, fmtParam(tr.params?.[key])])),
              }))}
              xLabel={objectiveName(objectives[0])}
              yLabel={objectiveName(objectives[1])}
              xDirection={dirOf(0)}
              yDirection={dirOf(1)}
              xFormatter={(v) => formatMetricAxis(objectives[0].metric, v)}
              yFormatter={(v) => formatMetricAxis(objectives[1].metric, v)}
              xConstraint={constraintFor(objectives[0].metric)}
              yConstraint={constraintFor(objectives[1].metric)}
              onPointClick={(p) => {
                const tr = trials.find((x) => String(x.number) === p.id);
                if (tr?.run_id) navigate(runPath(tr.run_id));
              }}
              height={300}
              empty="Aucun essai terminé pour l'instant."
            />
          </Card>
        ) : (
          <Card>
            <CardHeader title="Courbes intermédiaires" description="Valeur rapportée à chaque validation ; les essais élagués (pointillés) s'arrêtent tôt." />
            <IntermediateChart trials={trials} objective={objective} bestTrials={bestTrials} />
          </Card>
        )}
      </div>

      <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader
            title="Coordonnées parallèles"
            description="Chaque ligne est un essai : hyperparamètres → objectif (plus la teinte est soutenue, meilleur est l'objectif ; le meilleur essai est en trait épais)."
          />
          <ParallelCoords
            dimensions={dims}
            rows={parallelRows}
            height={340}
            onLineClick={(row) => {
              const tr = trials.find((x) => String(x.number) === row.id);
              if (tr?.run_id) navigate(runPath(tr.run_id));
            }}
            empty="Aucun essai terminé pour l'instant."
          />
        </Card>
        <Card>
          <CardHeader title="Importance des hyperparamètres" description="Part de la variance de l'objectif expliquée (fANOVA)." />
          {importances.length ? (
            <BarChart
              horizontal
              categories={importances.map(([key]) => key)}
              series={[{ name: "Importance", values: importances.map(([, v]) => v) }]}
              valueFormatter={(v) => formatPercent(v, 0)}
            />
          ) : (
            <EmptyState compact title="Pas encore calculée" description="L'importance apparaît après quelques essais terminés (mono-objectif de préférence)." />
          )}
        </Card>
      </div>

      {multi && (
        <Card className="mt-4">
          <CardHeader title="Courbes intermédiaires" description={`${objectiveName(objectives[0])} rapporté à chaque validation.`} />
          <IntermediateChart trials={trials} objective={objectives[0]} bestTrials={bestTrials} />
        </Card>
      )}

      <Card padding="none" className="mt-4 overflow-hidden">
        <div className="px-5 pt-4">
          <CardHeader title="Essais" description="Cliquez une ligne pour ouvrir le run de l'essai." className="mb-3" />
        </div>
        <Table
          bare
          dense
          columns={trialColumns}
          rows={trials}
          rowKey={(tr) => String(tr.number)}
          onRowClick={(tr) => tr.run_id && navigate(runPath(tr.run_id))}
          rowClassName={(tr) => (bestTrials.has(tr.number) ? "bg-accent-soft/40" : undefined)}
          initialSort={{ id: "number", desc: true }}
          maxHeight={560}
          empty="Aucun essai pour l'instant : les workers démarrent."
        />
      </Card>
    </Page>
  );
}
