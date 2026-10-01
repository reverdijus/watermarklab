import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";
import {
  Activity,
  ArrowDown,
  ArrowUp,
  CircleAlert,
  Copy,
  FileCode,
  FlaskConical,
  FolderKanban,
  Microscope,
  NotebookPen,
  Play,
  Plus,
  Rocket,
  Sparkles,
  Trophy,
  Wallet,
} from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  Kpi,
  Markdown,
  ModalityDot,
  Page,
  PageHeader,
  PageSkeleton,
  Section,
  Select,
  Switch,
  Tabs,
  Textarea,
  Tooltip,
  useToast,
  type TabItem,
} from "../components/ui";
import { useLaunch } from "../components/launch/LaunchProvider";
import { LeaderboardView } from "../components/leaderboard/LeaderboardView";
import { MiniPareto } from "../components/leaderboard/MiniPareto";
import { RunsTable } from "../components/RunsTable";
import { StudiesTable } from "../components/StudiesTable";
import { NewExperimentDialog, NewHpoPlanDialog } from "../components/SpecDialogs";
import { YamlFileDrawer } from "../components/YamlFileDrawer";
import { ApiError, errorMessage } from "../lib/api";
import { clearDraft, rememberDraft, restoreDraft } from "../lib/drafts";
import { formatEur, formatRelative, formatSteps, plural, shortId } from "../lib/format";
import { formatMetric, metricLabel } from "../lib/metrics";
import { toastFileSaved } from "../components/saveToast";
import { useProject, useRuns, useSaveFile, useStudies } from "../lib/queries";
import { experimentPath, hpoPlanPath, MODALITY_LABELS, runPath, RUN_STATUSES, STATUS_LABELS } from "../lib/runs";
import type { ExperimentInfo, HpoPlanInfo, Objective, ProjectDetail, ProjectSummary } from "../lib/types";

type TabId = "overview" | "experiments" | "runs" | "hpo" | "leaderboard";
const TAB_IDS: TabId[] = ["overview", "experiments", "runs", "hpo", "leaderboard"];

const opSymbol = (c: string) => c.replace(">=", "≥").replace("<=", "≤");

function ObjectiveText({ label, objective }: { label: string; objective: Objective }) {
  const Arrow = objective.direction === "maximize" ? ArrowUp : ArrowDown;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="text-faint">{label}</span>
      <span className="text-fg">{objective.label || metricLabel(objective.metric)}</span>
      <Tooltip content={objective.direction === "maximize" ? "À maximiser" : "À minimiser"}>
        <Arrow size={12} strokeWidth={2} aria-hidden />
      </Tooltip>
      {objective.constraint && <Badge tone="warning">{opSymbol(objective.constraint)}</Badge>}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Overview
// ---------------------------------------------------------------------------

function BestCard({ summary }: { summary: ProjectSummary }) {
  const navigate = useNavigate();
  const { openLaunch } = useLaunch();
  const best = summary.best;
  const q = summary.objectives.quality;
  const r = summary.objectives.robustness;
  return (
    <Card className="flex h-full flex-col">
      <CardHeader
        title="Meilleur checkpoint"
        icon={Trophy}
        description={
          best
            ? best.source === "benchmark"
              ? `Sur le benchmark ${summary.default_benchmark ?? "par défaut"}, contrainte de qualité respectée en priorité.`
              : "D'après les métriques de validation (aucune évaluation sur benchmark)."
            : undefined
        }
      />
      {best ? (
        <>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <div className="truncate text-[11.5px] text-muted">{q.label || metricLabel(q.metric)}</div>
              <div className="tabular font-serif text-[24px] text-fg">{formatMetric(q.metric, best.quality)}</div>
            </div>
            <div>
              <div className="truncate text-[11.5px] text-muted">{r.label || metricLabel(r.metric)}</div>
              <div className="tabular font-serif text-[24px] text-fg">{formatMetric(r.metric, best.robustness)}</div>
            </div>
          </div>
          <div className="mt-3 space-y-1 text-[12.5px] text-muted">
            <div>
              Run <span className="font-mono text-fg">{shortId(best.run_id)}</span>
            </div>
            {best.checkpoint && (
              <div>
                Checkpoint <span className="font-mono text-fg">{best.checkpoint}</span>
              </div>
            )}
          </div>
          <div className="mt-auto flex flex-wrap gap-2 pt-4">
            <Button size="sm" onClick={() => navigate(runPath(best.run_id, best.source === "benchmark" ? "evals" : undefined))}>
              Ouvrir le run
            </Button>
            <Button
              size="sm"
              variant="ghost"
              icon={Microscope}
              onClick={() =>
                openLaunch({ kind: "eval", project: summary.name, evalRun: best.run_id, evalCheckpoint: best.checkpoint ?? "last" })
              }
            >
              Évaluer
            </Button>
          </div>
        </>
      ) : (
        <EmptyState
          compact
          icon={Trophy}
          title="Pas encore de résultat"
          description="Le meilleur compromis apparaîtra après la première validation ou évaluation."
        />
      )}
    </Card>
  );
}

/** Draft-store key: the card unmounts on every tab change, the draft must outlive it. */
const notesDraftKey = (project: string) => `project-notes:${project}`;

function NotesCard({ project, notes }: { project: string; notes: string }) {
  const save = useSaveFile();
  const toast = useToast();
  // An unsaved draft from a previous visit reopens the editor (same server content only).
  const [draft, setDraft] = useState(() => restoreDraft(notesDraftKey(project), notes) ?? notes);
  const [editing, setEditing] = useState(() => draft !== notes);
  const dirty = editing && draft !== notes;

  // Remember the unsaved draft across tab / page changes (and guard ⌘R / window close).
  useEffect(() => {
    rememberDraft(notesDraftKey(project), editing ? draft : notes, notes);
  }, [project, editing, draft, notes]);

  const submit = async () => {
    try {
      const res = await save.mutateAsync({ path: `projects/${project}/notes.md`, content: draft });
      clearDraft(notesDraftKey(project));
      toastFileSaved(toast, res, "Notes enregistrées");
      setEditing(false);
    } catch (err) {
      toast.error("Notes non enregistrées", errorMessage(err));
    }
  };

  return (
    <Card>
      <CardHeader
        title="Notes du projet"
        icon={NotebookPen}
        description={<span className="font-mono text-[11.5px]">projects/{project}/notes.md</span>}
        actions={
          editing ? (
            <>
              {dirty && <span className="mr-1 text-[12.5px] text-warning">Modifications non enregistrées</span>}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  clearDraft(notesDraftKey(project));
                  setDraft(notes);
                  setEditing(false);
                }}
              >
                Annuler
              </Button>
              <Button size="sm" variant="primary" onClick={() => void submit()} loading={save.isPending}>
                Enregistrer
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setDraft(notes);
                setEditing(true);
              }}
            >
              Modifier
            </Button>
          )
        }
      />
      {editing ? (
        <Textarea
          mono
          rows={14}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
              e.preventDefault();
              void submit();
            }
          }}
          placeholder="Journal du projet : décisions, conclusions, prochaines pistes…"
          autoFocus
        />
      ) : (
        <div className="max-h-[420px] overflow-y-auto">
          <Markdown source={notes} empty="Aucune note. Consignez ici les décisions, conclusions et prochaines pistes du projet." />
        </div>
      )}
    </Card>
  );
}

function OverviewTab({ detail, onTab }: { detail: ProjectDetail; onTab: (t: TabId) => void }) {
  const s = detail.summary;
  const runs = useRuns({ project: s.name, limit: 8 });
  const navigate = useNavigate();
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Kpi icon={Activity} label="Runs" value={s.n_runs} sub={s.n_running ? `${s.n_running} en cours` : "aucun en cours"} onClick={() => onTab("runs")} />
        <Kpi icon={FlaskConical} label="Expériences" value={s.n_experiments} sub={plural(s.n_hpo_plans, "plan HPO", "plans HPO")} onClick={() => onTab("experiments")} />
        <Kpi icon={Wallet} label="Coût" value={formatEur(s.cost_eur)} sub="runs, études et évaluations" onClick={() => navigate("/budget")} />
        <Kpi
          icon={Sparkles}
          label="Dernière activité"
          value={s.last_activity ? formatRelative(s.last_activity) : "—"}
          sub={s.default_benchmark ? `benchmark : ${s.default_benchmark}` : "aucun benchmark par défaut"}
        />
      </div>
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
        <BestCard summary={s} />
        <Card className="xl:col-span-2">
          <CardHeader
            title="Compromis qualité × robustesse"
            description="Front de Pareto des checkpoints du projet ; le seuil de qualité du projet est en pointillés."
            actions={
              <Button size="sm" variant="ghost" onClick={() => onTab("leaderboard")}>
                Classement complet
              </Button>
            }
          />
          <MiniPareto project={s} />
        </Card>
      </div>
      <div className="grid grid-cols-1 gap-6 xl:grid-cols-5">
        <Section
          title="Runs récents"
          className="xl:col-span-3"
          actions={
            <Button size="sm" variant="ghost" onClick={() => onTab("runs")}>
              Voir tout
            </Button>
          }
        >
          <RunsTable runs={runs.data ?? []} loading={runs.isLoading} empty="Aucun run dans ce projet pour l'instant." />
        </Section>
        <div className="xl:col-span-2">
          <NotesCard key={detail.notes} project={s.name} notes={detail.notes} />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Experiments
// ---------------------------------------------------------------------------

function ExperimentCard({ project, exp, onDuplicate }: { project: string; exp: ExperimentInfo; onDuplicate: () => void }) {
  const navigate = useNavigate();
  const { openLaunch } = useLaunch();
  return (
    <Card interactive className="flex flex-col" onClick={() => navigate(experimentPath(project, exp.name))}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate font-mono text-[14px] font-medium text-fg">{exp.name}</div>
          <div className="mt-0.5 truncate font-mono text-[11.5px] text-faint">{exp.model || "modèle ?"}</div>
        </div>
        {exp.valid ? (
          <Badge tone="success">Valide</Badge>
        ) : (
          <Tooltip content={exp.errors.slice(0, 3).map((e) => `${e.loc ? `${e.loc} : ` : ""}${e.msg}`).join(" · ") || "Spécification invalide"}>
            <Badge tone="danger">{plural(exp.errors.length || 1, "erreur")}</Badge>
          </Tooltip>
        )}
      </div>
      <p className="mt-2 line-clamp-2 text-[13px] leading-relaxed text-fg">{exp.description || <span className="text-faint">Sans description</span>}</p>
      {exp.hypothesis && (
        <p className="mt-1.5 line-clamp-2 text-[12.5px] italic leading-relaxed text-muted">Hypothèse : {exp.hypothesis}</p>
      )}
      <div className="mt-auto pt-4">
        <div className="flex flex-wrap gap-x-4 gap-y-1 border-t border-border pt-3 text-[12px] text-muted">
          <span>{plural(exp.n_phases, "phase")}</span>
          <span className="tabular">{formatSteps(exp.total_steps)} steps</span>
          <span>{plural(exp.n_runs, "run")}</span>
        </div>
        <div className="mt-3 flex flex-wrap gap-2" onClick={(e) => e.stopPropagation()}>
          <Button size="sm" to={experimentPath(project, exp.name)} icon={FileCode}>
            Ouvrir
          </Button>
          <Button
            size="sm"
            variant="primary"
            icon={Play}
            disabled={!exp.valid}
            title={exp.valid ? undefined : "Corrigez la spécification avant de lancer"}
            onClick={() => openLaunch({ kind: "train", project, experiment: exp.name })}
          >
            Lancer
          </Button>
          <Button size="sm" variant="ghost" icon={Copy} onClick={onDuplicate}>
            Dupliquer
          </Button>
        </div>
      </div>
    </Card>
  );
}

function ExperimentsTab({ detail }: { detail: ProjectDetail }) {
  const [dialog, setDialog] = useState<{ from: string | null } | null>(null);
  const name = detail.summary.name;
  return (
    <Section
      title="Expériences"
      description="Chaque expérience est un fichier YAML (modèle, données, pertes, curriculum de phases)."
      actions={
        <Button variant="primary" icon={Plus} onClick={() => setDialog({ from: null })}>
          Nouvelle expérience
        </Button>
      }
    >
      {detail.experiments.length ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {detail.experiments.map((exp) => (
            <ExperimentCard key={exp.name} project={name} exp={exp} onDuplicate={() => setDialog({ from: exp.name })} />
          ))}
        </div>
      ) : (
        <Card padding="none">
          <EmptyState
            icon={FlaskConical}
            title="Aucune expérience"
            description="Créez une première expérience : un squelette commenté est généré, prêt à éditer et valider en direct."
            action={
              <Button variant="primary" icon={Plus} onClick={() => setDialog({ from: null })}>
                Nouvelle expérience
              </Button>
            }
          />
        </Card>
      )}
      <NewExperimentDialog
        open={!!dialog}
        onClose={() => setDialog(null)}
        project={name}
        experiments={detail.experiments}
        from={dialog?.from}
      />
    </Section>
  );
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

function RunsTab({ detail }: { detail: ProjectDetail }) {
  const { openLaunch } = useLaunch();
  const [experiment, setExperiment] = useState("");
  const [status, setStatus] = useState("");
  const [trials, setTrials] = useState(false);
  const runs = useRuns({
    project: detail.summary.name,
    experiment: experiment || undefined,
    status: status || undefined,
    include_trials: trials,
    limit: 500,
  });
  const filtered = !!(experiment || status || trials);
  return (
    <div>
      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Select
          size="sm"
          value={experiment}
          onChange={setExperiment}
          placeholder="Toutes les expériences"
          options={detail.experiments.map((e) => ({ value: e.name, label: e.name }))}
          className="w-52"
        />
        <Select
          size="sm"
          value={status}
          onChange={setStatus}
          placeholder="Tous les statuts"
          options={RUN_STATUSES.map((s) => ({ value: s, label: STATUS_LABELS[s] }))}
          className="w-40"
        />
        <Switch size="sm" checked={trials} onChange={setTrials} label="Essais HPO" className="mb-1.5" />
        <span className="mb-1.5 ml-auto text-[12.5px] text-muted">{runs.data ? plural(runs.data.length, "run") : ""}</span>
      </div>
      {runs.isError && (
        <Callout tone="danger" icon={CircleAlert} title="Impossible de charger les runs" className="mb-4">
          {errorMessage(runs.error)}
        </Callout>
      )}
      <RunsTable
        runs={runs.data ?? []}
        loading={runs.isLoading}
        selectable
        empty={
          filtered ? (
            "Aucun run ne correspond à ces filtres."
          ) : (
            <span>
              Aucun run dans ce projet.{" "}
              <button type="button" className="text-accent hover:underline" onClick={() => openLaunch({ project: detail.summary.name })}>
                Lancer un entraînement
              </button>
            </span>
          )
        }
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// HPO
// ---------------------------------------------------------------------------

function PlanCard({ project, plan }: { project: string; plan: HpoPlanInfo }) {
  const navigate = useNavigate();
  const { openLaunch } = useLaunch();
  return (
    <Card interactive className="flex flex-col" onClick={() => navigate(hpoPlanPath(project, plan.name))}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate font-mono text-[14px] font-medium text-fg">{plan.name}</div>
          <div className="mt-0.5 truncate text-[12px] text-muted">
            sur <span className="font-mono">{plan.experiment || "?"}</span>
          </div>
        </div>
        {plan.valid ? <Badge tone="success">Valide</Badge> : <Badge tone="danger">{plural(plan.errors.length || 1, "erreur")}</Badge>}
      </div>
      <p className="mt-2 line-clamp-2 text-[13px] leading-relaxed text-fg">{plan.description || <span className="text-faint">Sans description</span>}</p>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {plan.objectives.map((o) => (
          <Badge key={o.metric} tone="neutral" icon={o.direction === "maximize" ? ArrowUp : ArrowDown}>
            {metricLabel(o.metric, { withGroup: true })}
          </Badge>
        ))}
        {plan.objectives.length > 1 && <Badge tone="info">multi-objectif</Badge>}
      </div>
      <div className="mt-auto pt-4">
        <div className="flex gap-4 border-t border-border pt-3 text-[12px] text-muted">
          <span>{plural(plan.n_params, "paramètre")}</span>
          <span>{plural(plan.n_studies, "étude")}</span>
        </div>
        <div className="mt-3 flex gap-2" onClick={(e) => e.stopPropagation()}>
          <Button size="sm" icon={FileCode} to={hpoPlanPath(project, plan.name)}>
            Ouvrir
          </Button>
          <Button size="sm" variant="primary" icon={Play} disabled={!plan.valid} onClick={() => openLaunch({ kind: "hpo", project, plan: plan.name })}>
            Lancer
          </Button>
        </div>
      </div>
    </Card>
  );
}

function HpoTab({ detail }: { detail: ProjectDetail }) {
  const name = detail.summary.name;
  const studies = useStudies(name);
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-10">
      <Section
        title="Plans d'optimisation"
        description="Espace de recherche, objectifs, sampler, pruner et budget sur une expérience de base."
        actions={
          <Button variant="primary" icon={Plus} onClick={() => setOpen(true)} disabled={!detail.experiments.length}>
            Nouveau plan
          </Button>
        }
      >
        {detail.hpo_plans.length ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
            {detail.hpo_plans.map((p) => (
              <PlanCard key={p.name} project={name} plan={p} />
            ))}
          </div>
        ) : (
          <Card padding="none">
            <EmptyState
              icon={Sparkles}
              title="Aucun plan HPO"
              description={
                detail.experiments.length
                  ? "Un plan fait varier des hyperparamètres d'une expérience (TPE, NSGA-II…) avec élagage des essais peu prometteurs."
                  : "Créez d'abord une expérience : un plan HPO s'appuie toujours sur une expérience de base."
              }
              action={
                <Button variant="primary" icon={Plus} onClick={() => setOpen(true)} disabled={!detail.experiments.length}>
                  Nouveau plan
                </Button>
              }
            />
          </Card>
        )}
      </Section>
      <Section title="Études" description="Exécutions des plans : chaque essai est un run avec les valeurs tirées.">
        {studies.isError ? (
          <Callout tone="danger" icon={CircleAlert} title="Impossible de charger les études">
            {errorMessage(studies.error)}
          </Callout>
        ) : (
          <StudiesTable studies={studies.data ?? []} loading={studies.isLoading} empty="Aucune étude lancée pour ce projet." />
        )}
      </Section>
      <NewHpoPlanDialog open={open} onClose={() => setOpen(false)} project={name} experiments={detail.experiments} plans={detail.hpo_plans} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function ProjectPage() {
  const params = useParams();
  const name = params.name ? decodeURIComponent(params.name) : "";
  const project = useProject(name);
  const { openLaunch } = useLaunch();
  const [search, setSearch] = useSearchParams();
  const [yamlOpen, setYamlOpen] = useState(false);
  const tabParam = search.get("tab") as TabId | null;
  const tab: TabId = tabParam && TAB_IDS.includes(tabParam) ? tabParam : "overview";
  const setTab = (t: TabId) => {
    const next = new URLSearchParams();
    if (t !== "overview") next.set("tab", t);
    setSearch(next, { replace: true });
  };

  if (project.isLoading) return <PageSkeleton />;
  if (project.isError || !project.data) {
    const notFound = project.error instanceof ApiError && project.error.status === 404;
    return (
      <Page>
        <EmptyState
          icon={notFound ? FolderKanban : CircleAlert}
          title={notFound ? "Projet introuvable" : "Impossible de charger le projet"}
          description={notFound ? `Aucun projet « ${name} » dans le workspace.` : errorMessage(project.error)}
          action={
            <>
              <Button to="/">Accueil</Button>
              {!notFound && (
                <Button variant="primary" onClick={() => void project.refetch()}>
                  Réessayer
                </Button>
              )}
            </>
          }
        />
      </Page>
    );
  }

  const detail = project.data;
  const s = detail.summary;
  const items: TabItem<TabId>[] = [
    { id: "overview", label: "Vue d'ensemble" },
    { id: "experiments", label: "Expériences", count: detail.experiments.length || null },
    { id: "runs", label: "Runs", count: s.n_runs || null },
    { id: "hpo", label: "HPO", count: detail.hpo_plans.length || null },
    { id: "leaderboard", label: "Classement" },
  ];

  return (
    <Page width="wide">
      <PageHeader
        back={{ to: "/", label: "Accueil" }}
        eyebrow={
          <span className="inline-flex items-center gap-2">
            <ModalityDot modality={s.modality} />
            Projet {MODALITY_LABELS[s.modality]?.toLowerCase() ?? s.modality} · <span className="font-mono">{s.name}</span>
          </span>
        }
        title={s.title || s.name}
        description={s.description || undefined}
        actions={
          <>
            <Button icon={FileCode} onClick={() => setYamlOpen(true)}>
              project.yaml
            </Button>
            <Button variant="primary" icon={Rocket} onClick={() => openLaunch({ project: s.name })}>
              Nouveau lancement
            </Button>
          </>
        }
        meta={
          <>
            <Badge tone="neutral" size="md">
              {s.message_bits} bits
            </Badge>
            <ObjectiveText label="Qualité" objective={s.objectives.quality} />
            <ObjectiveText label="Robustesse" objective={s.objectives.robustness} />
            <span className="inline-flex items-center gap-1.5">
              <Wallet size={13} strokeWidth={1.75} aria-hidden /> {formatEur(s.cost_eur)}
            </span>
            {s.tags.map((t) => (
              <span key={t} className="text-faint">
                #{t}
              </span>
            ))}
          </>
        }
      />
      <Tabs items={items} value={tab} onChange={setTab} className="mb-6" />
      {tab === "overview" && <OverviewTab detail={detail} onTab={setTab} />}
      {tab === "experiments" && <ExperimentsTab detail={detail} />}
      {tab === "runs" && <RunsTab detail={detail} />}
      {tab === "hpo" && <HpoTab detail={detail} />}
      {tab === "leaderboard" && <LeaderboardView project={s} />}
      <YamlFileDrawer
        open={yamlOpen}
        onClose={() => setYamlOpen(false)}
        path={`projects/${s.name}/project.yaml`}
        kind="project"
        title="Réglages du projet"
        description="Titre, modalité, taille du message, benchmark par défaut et objectifs (axes du front de Pareto)."
      />
    </Page>
  );
}
