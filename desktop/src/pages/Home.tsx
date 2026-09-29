import { useNavigate } from "react-router";
import { Activity, CircleAlert, Cpu, FlaskConical, Plus, Rocket, Wallet } from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  EmptyState,
  Kpi,
  ModalityDot,
  Page,
  PageHeader,
  Section,
  Skeleton,
  StatusBadge,
  Table,
  type Column,
} from "../components/ui";
import { JobList, MetricList, MetricValue, RunProgress } from "../components/RunBits";
import { useLaunch } from "../components/launch/LaunchProvider";
import { errorMessage } from "../lib/api";
import { formatEur, formatHours, formatRelative, plural, shortId } from "../lib/format";
import { useNow } from "../lib/hooks";
import { formatMetric, metricLabel, pickKeyMetrics, qualityMetricFor } from "../lib/metrics";
import { useOverview } from "../lib/queries";
import { MODALITY_LABELS, projectPath, runPath } from "../lib/runs";
import type { ProjectSummary, RunSummary } from "../lib/types";

function greeting(date: Date): string {
  const h = date.getHours();
  if (h < 5) return "Bonne nuit";
  if (h < 12) return "Bonjour";
  if (h < 18) return "Bon après-midi";
  return "Bonsoir";
}

function RunningCard({ run }: { run: RunSummary }) {
  const navigate = useNavigate();
  const metrics = pickKeyMetrics(run.summary, run.modality, 3);
  return (
    <Card interactive padding="md" onClick={() => navigate(runPath(run.run_id))}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ModalityDot modality={run.modality} />
            <span className="truncate text-[14.5px] font-medium text-fg">{run.experiment}</span>
            {run.trial != null && <Badge tone="info">essai {run.trial}</Badge>}
          </div>
          <div className="mt-0.5 truncate text-[12.5px] text-muted">
            {run.project} · <span className="font-mono">{shortId(run.run_id)}</span> · {run.host}
          </div>
        </div>
        <StatusBadge status={run.status} />
      </div>
      <RunProgress progress={run.progress} status={run.status} run={run} detailed className="mt-4" />
      <div className="mt-3 flex items-end justify-between gap-3">
        <MetricList items={metrics} />
        <span className="tabular shrink-0 text-[12.5px] text-muted">{formatEur(run.cost_eur)}</span>
      </div>
    </Card>
  );
}

function ProjectCard({ project }: { project: ProjectSummary }) {
  const navigate = useNavigate();
  const q = project.objectives?.quality;
  const r = project.objectives?.robustness;
  return (
    <Card interactive padding="md" onClick={() => navigate(projectPath(project.name))} className="flex flex-col">
      <div className="flex items-center gap-2">
        <ModalityDot modality={project.modality} />
        <span className="truncate text-[14.5px] font-medium text-fg">{project.title || project.name}</span>
        {project.n_running > 0 && (
          <Badge tone="accent" dot pulse className="ml-auto">
            {project.n_running} en cours
          </Badge>
        )}
      </div>
      <p className="mt-1.5 line-clamp-2 min-h-[2.6em] text-[13px] leading-relaxed text-muted">
        {project.description || `${MODALITY_LABELS[project.modality]} · ${project.message_bits} bits`}
      </p>
      <div className="mt-4 grid grid-cols-2 gap-3 border-t border-border pt-3">
        <div>
          <div className="truncate text-[11.5px] text-muted">{q?.label || metricLabel(q?.metric ?? qualityMetricFor(project.modality))}</div>
          <div className="tabular font-serif text-[18px] text-fg">
            {project.best?.quality != null ? formatMetric(q?.metric ?? "psnr", project.best.quality) : "—"}
          </div>
        </div>
        <div>
          <div className="truncate text-[11.5px] text-muted">{r?.label || metricLabel(r?.metric ?? "bit_acc_mean")}</div>
          <div className="tabular font-serif text-[18px] text-fg">
            {project.best?.robustness != null ? formatMetric(r?.metric ?? "bit_acc_mean", project.best.robustness) : "—"}
          </div>
        </div>
      </div>
      <div className="mt-3 flex items-center justify-between text-[12px] text-muted">
        <span>
          {plural(project.n_runs, "run")} · {plural(project.n_experiments, "expérience")}
        </span>
        <span className="tabular">{formatEur(project.cost_eur)}</span>
      </div>
      {project.best && (
        <div className="mt-1 text-[11.5px] text-faint">
          Meilleur : {project.best.source === "benchmark" ? "benchmark" : "validation"} · {shortId(project.best.run_id)}
        </div>
      )}
    </Card>
  );
}

export default function Home() {
  const overview = useOverview();
  const { openLaunch } = useLaunch();
  const navigate = useNavigate();
  const now = useNow(60000);
  const data = overview.data;

  const recentColumns: Column<RunSummary>[] = [
    { id: "status", header: "", width: 28, cell: (r) => <StatusBadge status={r.status} dotOnly /> },
    {
      id: "run",
      header: "Run",
      cell: (r) => (
        <div className="min-w-0">
          <div className="truncate text-fg">{r.experiment}</div>
          <div className="truncate text-[11.5px] text-muted">{r.project}</div>
        </div>
      ),
    },
    {
      id: "metric",
      header: "Bit acc.",
      align: "right",
      cell: (r) => <MetricValue metric="val/bit_acc_mean" value={r.summary["val/bit_acc_mean"]} />,
    },
    {
      id: "quality",
      header: "Qualité",
      align: "right",
      cell: (r) => {
        const k = `val/${qualityMetricFor(r.modality)}`;
        return <MetricValue metric={k} value={r.summary[k]} />;
      },
    },
    {
      id: "date",
      header: "Créé",
      align: "right",
      cell: (r) => <span className="whitespace-nowrap text-muted">{formatRelative(r.created_at, now)}</span>,
    },
  ];

  return (
    <Page>
      <PageHeader
        eyebrow={new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long" }).format(new Date(now))}
        title={greeting(new Date(now))}
        description={data ? data.workspace.name : undefined}
        actions={
          <>
            <Button icon={Activity} to="/runs">
              Tous les runs
            </Button>
            <Button variant="primary" icon={Rocket} onClick={() => openLaunch()}>
              Nouveau lancement
            </Button>
          </>
        }
      />

      {overview.isError && (
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Impossible de charger l'accueil"
          action={
            <Button size="sm" onClick={() => void overview.refetch()}>
              Réessayer
            </Button>
          }
          className="mb-6"
        >
          {errorMessage(overview.error)}
        </Callout>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Kpi
          icon={Activity}
          label="Runs actifs"
          value={data?.counts.running ?? "—"}
          sub={data ? `${plural(data.counts.runs, "run")} au total` : undefined}
          loading={overview.isLoading}
          onClick={() => navigate("/runs?status=running")}
        />
        <Kpi
          icon={Cpu}
          label="GPU-heures"
          value={data ? formatHours(data.counts.gpu_hours) : "—"}
          sub={data ? plural(data.counts.projects, "projet") : undefined}
          loading={overview.isLoading}
        />
        <Kpi
          icon={Wallet}
          label="Budget consommé"
          value={data ? formatEur(data.budget.spent_eur, { digits: 0 }) : "—"}
          sub={
            data
              ? `sur ${formatEur(data.budget.total_eur, { digits: 0 })}${
                  data.budget.running_eur_per_hour > 0 ? ` · ${formatEur(data.budget.running_eur_per_hour)}/h en cours` : ""
                }`
              : undefined
          }
          progress={data && data.budget.total_eur > 0 ? data.budget.spent_eur / data.budget.total_eur : null}
          loading={overview.isLoading}
          onClick={() => navigate("/budget")}
        />
        <Kpi
          icon={FlaskConical}
          label="Études HPO"
          value={data?.counts.studies ?? "—"}
          sub="plans d'optimisation lancés"
          loading={overview.isLoading}
        />
      </div>

      <Section
        title="En cours"
        description={data?.running.length ? "Progression par phase, métriques de validation et temps restant estimé." : undefined}
        className="mt-10"
      >
        {overview.isLoading ? (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <Skeleton className="h-40 rounded-xl" />
            <Skeleton className="h-40 rounded-xl" />
          </div>
        ) : data?.running.length ? (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {data.running.map((r) => (
              <RunningCard key={r.run_id} run={r} />
            ))}
          </div>
        ) : (
          <Card padding="none">
            <EmptyState
              compact
              icon={Rocket}
              title="Aucun entraînement en cours"
              description="Lancez une expérience en local ou sur un serveur GPU : sa progression apparaîtra ici."
              action={
                <Button variant="primary" icon={Plus} onClick={() => openLaunch()}>
                  Nouveau lancement
                </Button>
              }
            />
          </Card>
        )}
      </Section>

      <div className="mt-10 grid grid-cols-1 gap-8 xl:grid-cols-5">
        <Section title="Runs récents" className="mt-0 xl:col-span-3" actions={<Button size="sm" variant="ghost" to="/runs">Voir tout</Button>}>
          <Table
            columns={recentColumns}
            rows={(data?.recent ?? []).slice(0, 8)}
            rowKey={(r) => r.run_id}
            onRowClick={(r) => navigate(runPath(r.run_id))}
            loading={overview.isLoading}
            dense
            empty="Aucun run pour l'instant."
          />
        </Section>
        <Section title="Jobs" className="mt-0 xl:col-span-2">
          <Card padding="none" className="px-4">
            {overview.isLoading ? (
              <Skeleton lines={4} className="py-4" />
            ) : (
              <JobList jobs={(data?.jobs ?? []).slice(0, 8)} empty="Aucun job lancé depuis l'app." />
            )}
          </Card>
        </Section>
      </div>

      <Section title="Projets" className="mt-10">
        {overview.isLoading ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {Array.from({ length: 3 }).map((_, i) => (
              <Skeleton key={i} className="h-44 rounded-xl" />
            ))}
          </div>
        ) : data?.projects.length ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {data.projects.map((p) => (
              <ProjectCard key={p.name} project={p} />
            ))}
          </div>
        ) : (
          <Card padding="none">
            <EmptyState
              compact
              title="Aucun projet"
              description="Créez un projet depuis la barre latérale pour définir un schéma de watermarking et ses objectifs."
            />
          </Card>
        )}
      </Section>
    </Page>
  );
}
