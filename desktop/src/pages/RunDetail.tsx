import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import {
  CircleAlert,
  Clock,
  Cpu,
  Euro,
  GitBranch,
  Microscope,
  Scissors,
  Square,
  TriangleAlert,
  User,
} from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  CodeBlock,
  EmptyState,
  Page,
  PageHeader,
  PageSkeleton,
  Spinner,
  StatusBadge,
  Tabs,
  Tooltip,
  copyText,
  useConfirm,
  useToast,
  type TabItem,
} from "../components/ui";
import { AdminOnlyNotice } from "../components/AdminOnly";
import { RunProgress, TagEditor } from "../components/RunBits";
import { useLaunch } from "../components/launch/LaunchProvider";
import { ApiError, errorMessage } from "../lib/api";
import { formatDate, formatDuration, formatEur, formatInt, formatRelative, shortId } from "../lib/format";
import { useNow } from "../lib/hooks";
import { hasRetrievalIssue } from "../lib/hosted";
import { useJobs, usePermissions, useRun, useSetRunTags, useStopRun } from "../lib/queries";
import { experimentPath, isActiveStatus, isPrunedTrial, projectPath, runDuration, runPath, studyPath } from "../lib/runs";
import type { RunDetail as RunDetailData } from "../lib/types";
import { OverviewTab } from "./run/OverviewTab";
import { MetricsTab } from "./run/MetricsTab";
import { SystemTab } from "./run/SystemTab";
import { CheckpointsTab } from "./run/CheckpointsTab";
import { EvalsTab } from "./run/EvalsTab";
import { SamplesTab } from "./run/SamplesTab";
import { LogsTab } from "./run/LogsTab";
import { ConfigTab } from "./run/ConfigTab";
import { EnvTab } from "./run/EnvTab";
import { NotesTab } from "./run/NotesTab";

type TabId = "overview" | "metrics" | "system" | "checkpoints" | "evals" | "samples" | "logs" | "config" | "env" | "notes";

const TAB_IDS: TabId[] = ["overview", "metrics", "system", "checkpoints", "evals", "samples", "logs", "config", "env", "notes"];

function RunHeader({ run }: { run: RunDetailData }) {
  const { canWrite, canViewCode } = usePermissions();
  const { openLaunch } = useLaunch();
  const stop = useStopRun();
  const setTags = useSetRunTags(run.run_id);
  const confirm = useConfirm();
  const toast = useToast();
  const now = useNow(15000);
  const [showTrace, setShowTrace] = useState(false);
  const live = isActiveStatus(run.status);
  const pruned = isPrunedTrial(run);
  const duration = runDuration(run, now);
  const project = run.project;
  // Job that produced this run (or its study): its end-of-job checkpoint retrieval (§12.4).
  const jobs = useJobs();
  const job = (jobs.data ?? []).find((j) => j.run_id === run.run_id) ?? (run.study ? (jobs.data ?? []).find((j) => j.study_id === run.study) : undefined);
  const retrievalIssue = job && hasRetrievalIssue(job) ? job.retrieval! : null;

  const onStop = async () => {
    const ok = await confirm({
      title: "Arrêter ce run ?",
      message: "Le process recevra un signal d'arrêt : un dernier checkpoint est écrit puis le run passe à « Arrêté ».",
      confirmLabel: "Arrêter le run",
      cancelLabel: "Continuer",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await stop.mutateAsync(run.run_id);
      toast.success("Arrêt demandé", shortId(run.run_id));
    } catch (err) {
      toast.error("Impossible d'arrêter le run", errorMessage(err));
    }
  };

  const branch = () =>
    openLaunch({
      kind: "train",
      project,
      experiment: run.experiment,
      host: run.host,
      initFrom: { run: run.run_id, checkpoint: "last" },
    });
  const evaluate = () => openLaunch({ kind: "eval", project, evalRun: run.run_id, evalCheckpoint: "last" });

  const errorFirstLine = run.error?.trim().split("\n").filter(Boolean).slice(-1)[0];

  return (
    <>
      <PageHeader
        back={{ to: run.study ? studyPath(run.study) : "/runs", label: run.study ? "Étude HPO" : "Runs" }}
        eyebrow={
          <span className="inline-flex items-center gap-1.5">
            <Link to={projectPath(project)} className="no-drag hover:text-fg">
              {project}
            </Link>
            <span className="text-faint">/</span>
            {canViewCode ? (
              <Link to={experimentPath(project, run.experiment)} className="no-drag hover:text-fg">
                {run.experiment}
              </Link>
            ) : (
              <span>{run.experiment}</span>
            )}
          </span>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-3">
            {run.experiment}
            <StatusBadge status={run.status} size="md" pruned={pruned} />
            {run.trial != null && <Badge tone="info" size="md">essai {run.trial}</Badge>}
          </span>
        }
        actions={
          canWrite ? (
            <>
              {live && (
                <Button variant="danger" icon={Square} onClick={() => void onStop()} loading={stop.isPending}>
                  Arrêter
                </Button>
              )}
              <Button icon={GitBranch} onClick={branch} disabled={!run.checkpoints.length && !live}>
                Brancher
              </Button>
              <Button icon={Microscope} onClick={evaluate} disabled={!run.checkpoints.length}>
                Évaluer
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
                onClick={() => void copyText(run.run_id).then((ok) => ok && toast.info("Identifiant copié"))}
              >
                {run.run_id}
              </button>
            </Tooltip>
            <span className="inline-flex items-center gap-1.5">
              <Cpu size={13} strokeWidth={1.75} aria-hidden /> {run.host}
            </span>
            <span className="inline-flex items-center gap-1.5" title={`Créé le ${formatDate(run.created_at)}`}>
              <Clock size={13} strokeWidth={1.75} aria-hidden /> {formatDuration(duration)}
              {run.ended_at ? ` · terminé ${formatRelative(run.ended_at, now)}` : run.started_at ? ` · démarré ${formatRelative(run.started_at, now)}` : ""}
            </span>
            <span className="inline-flex items-center gap-1.5">
              <Euro size={13} strokeWidth={1.75} aria-hidden /> {formatEur(run.cost_eur)}
              {run.price_eur_per_hour ? <span className="text-faint">({formatEur(run.price_eur_per_hour)}/h)</span> : null}
            </span>
            {run.parent && (
              <Link to={runPath(run.parent.run_id)} className="no-drag inline-flex items-center gap-1.5 hover:text-fg">
                <GitBranch size={13} strokeWidth={1.75} aria-hidden />
                {run.parent.resume ? "repris depuis" : "branché depuis"} <span className="font-mono">{shortId(run.parent.run_id)}</span> · {run.parent.checkpoint}
                {!run.parent.resume && run.parent.start_phase != null && ` → ${run.parent.start_phase}`}
              </Link>
            )}
            {run.study && (
              <Link to={studyPath(run.study)} className="no-drag hover:text-fg">
                étude <span className="font-mono">{shortId(run.study)}</span>
              </Link>
            )}
            {run.user && (
              <span className="inline-flex items-center gap-1.5">
                <User size={13} strokeWidth={1.75} aria-hidden /> par {run.user}
              </span>
            )}
          </>
        }
      />
      {(canWrite || run.tags.length > 0) && (
        <TagEditor
          tags={run.tags}
          disabled={!canWrite}
          className="-mt-3 mb-6"
          onChange={async (tags) => {
            try {
              await setTags.mutateAsync(tags);
            } catch (err) {
              toast.error("Tags non enregistrés", errorMessage(err));
            }
          }}
        />
      )}
      {live && (
        <div className="mb-6 rounded-xl border border-border bg-surface px-4 py-3 shadow-sm">
          {run.progress ? (
            <RunProgress detailed run={run} progress={run.progress} status={run.status} />
          ) : (
            <div className="flex items-center gap-2 text-[12.5px] text-muted">
              <Spinner size={14} />
              <span>
                {run.status === "queued" ? "En file d'attente" : "Démarrage"}…{" "}
                {run.plan ? (
                  <>
                    <span className="tabular text-fg">0</span> / {formatInt(run.plan.total_steps)} steps
                  </>
                ) : (
                  "la progression apparaîtra au premier step."
                )}
              </span>
            </div>
          )}
        </div>
      )}
      {pruned && (
        <Callout tone="info" icon={Scissors} title="Essai élagué par l'étude" className="mb-6">
          Le pruner de l'étude a arrêté cet essai
          {run.progress ? ` au step ${formatInt(run.progress.step)} / ${formatInt(run.progress.total_steps)}` : " avant la fin"} : ses valeurs
          intermédiaires étaient moins prometteuses que celles des autres essais. C'est le fonctionnement normal d'une étude HPO, pas une erreur.
          {run.study && (
            <>
              {" "}
              <Link to={studyPath(run.study)} className="no-drag font-medium text-fg hover:underline">
                Voir l'étude
              </Link>
            </>
          )}
        </Callout>
      )}
      {run.error && !pruned && (
        <Callout
          tone="danger"
          icon={CircleAlert}
          title={run.status === "killed" ? "Run interrompu" : "Le run a échoué"}
          className="mb-6"
          action={
            run.error.includes("\n") ? (
              <Button size="sm" variant="ghost" onClick={() => setShowTrace((s) => !s)}>
                {showTrace ? "Masquer" : "Détails"}
              </Button>
            ) : undefined
          }
        >
          {showTrace ? <CodeBlock code={run.error} maxHeight={320} className="mt-2" /> : errorFirstLine}
        </Callout>
      )}
      {retrievalIssue && (
        <Callout
          tone="warning"
          icon={TriangleAlert}
          title={retrievalIssue.status === "failed" ? "Checkpoints non rapatriés" : "Rapatriement partiel des checkpoints"}
          className="mb-6"
          action={
            <Button size="sm" to="/hosts">
              Machines
            </Button>
          }
        >
          {retrievalIssue.missing.length
            ? `${retrievalIssue.missing.length} checkpoint${retrievalIssue.missing.length > 1 ? "s" : ""} encore sur ${run.host} : ${retrievalIssue.missing.slice(0, 4).join(", ")}${retrievalIssue.missing.length > 4 ? "…" : ""}.`
            : `La synchronisation de fin de job depuis ${run.host} n'a pas pu vérifier tous les checkpoints.`}{" "}
          {canWrite
            ? "Lancez « Rapatrier les checkpoints » depuis la page Machines avant de retirer la machine."
            : "Un administrateur doit les rapatrier depuis la page Machines avant de retirer la machine."}
        </Callout>
      )}
      {run.status === "lost" && (
        <Callout tone="warning" icon={CircleAlert} title="Run perdu" className="mb-6">
          Aucun signe de vie (heartbeat) depuis plus de 10 minutes. Le process a peut-être été tué ou la machine est injoignable ;
          synchronisez le job ou vérifiez la machine.
        </Callout>
      )}
    </>
  );
}

export default function RunDetail() {
  const { id = "" } = useParams();
  const runId = decodeURIComponent(id);
  const [params, setParams] = useSearchParams();
  const run = useRun(runId);
  const { canViewCode } = usePermissions();
  // Logs hold commands and tracebacks: admin only (§9); a member's ?tab=logs falls back to the overview.
  const tabIds = canViewCode ? TAB_IDS : TAB_IDS.filter((t) => t !== "logs");
  const tabParam = params.get("tab") as TabId | null;
  const tab: TabId = tabParam && tabIds.includes(tabParam) ? tabParam : "overview";
  const setTab = (t: TabId) => {
    const next = new URLSearchParams(params);
    if (t === "overview") next.delete("tab");
    else next.set("tab", t);
    setParams(next, { replace: true });
  };

  if (run.isLoading) return <PageSkeleton />;
  if (run.isError || !run.data) {
    const notFound = run.error instanceof ApiError && run.error.status === 404;
    return (
      <Page>
        <EmptyState
          icon={CircleAlert}
          title={notFound ? "Run introuvable" : "Impossible de charger le run"}
          description={notFound ? `Aucun run « ${runId} » dans le workspace.` : errorMessage(run.error)}
          action={
            <>
              <Button to="/runs">Tous les runs</Button>
              {!notFound && (
                <Button variant="primary" onClick={() => void run.refetch()}>
                  Réessayer
                </Button>
              )}
            </>
          }
        />
      </Page>
    );
  }

  const data = run.data;
  const live = isActiveStatus(data.status);
  const items: TabItem<TabId>[] = [
    { id: "overview", label: "Vue d'ensemble" },
    { id: "metrics", label: "Métriques" },
    { id: "system", label: "Système" },
    { id: "checkpoints", label: "Checkpoints", count: data.checkpoints.length || null },
    { id: "evals", label: "Évaluations", count: data.evals.length || null },
    { id: "samples", label: "Échantillons", count: data.samples.length || null },
    ...(canViewCode ? [{ id: "logs" as const, label: "Logs" }] : []),
    { id: "config", label: "Configuration" },
    { id: "env", label: "Environnement" },
    { id: "notes", label: "Notes" },
  ];

  return (
    <Page width="wide">
      <RunHeader run={data} />
      <Tabs items={items} value={tab} onChange={setTab} className="mb-6" />
      {tab === "overview" && <OverviewTab run={data} live={live} />}
      {tab === "metrics" && <MetricsTab run={data} live={live} />}
      {tab === "system" && <SystemTab run={data} live={live} />}
      {tab === "checkpoints" && <CheckpointsTab run={data} />}
      {tab === "evals" && <EvalsTab run={data} />}
      {tab === "samples" && <SamplesTab run={data} />}
      {tab === "logs" && canViewCode && <LogsTab run={data} live={live} />}
      {tab === "config" && <ConfigTab run={data} restricted={!canViewCode} restrictedNotice={<AdminOnlyNotice compact what="Configuration réservée aux administrateurs : la spécification, les overrides et le code du run ne sont pas visibles avec un compte membre." />} />}
      {tab === "env" && <EnvTab run={data} restricted={!canViewCode} />}
      {tab === "notes" && <NotesTab run={data} />}
    </Page>
  );
}
