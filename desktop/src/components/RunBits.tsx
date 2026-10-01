/** Small run-related building blocks shared by pages (progress, metrics, tags, jobs). */
import clsx from "clsx";
import { useState, type KeyboardEvent } from "react";
import { Link } from "react-router";
import { ArrowUpRight, Ban, Plus, RefreshCw, TriangleAlert, X } from "lucide-react";
import { Badge, IconButton, ProgressBar, StatusBadge, Tooltip, useConfirm, useToast } from "./ui";
import { errorMessage } from "../lib/api";
import { formatDuration, formatInt, formatPercent, formatRelative, shortId } from "../lib/format";
import { describeRetrieveResult, hasRetrievalIssue, retrievalNotice } from "../lib/hosted";
import { formatMetric, metricInfo, metricLabel } from "../lib/metrics";
import { useCancelJob, useSyncJob } from "../lib/queries";
import { isActiveStatus, JOB_KIND_LABELS, progressFraction, runEta, runPath, studyPath } from "../lib/runs";
import type { Job, Progress, RunSummary } from "../lib/types";

// ---------------------------------------------------------------------------
// RunProgress
// ---------------------------------------------------------------------------

export interface RunProgressProps {
  progress: Progress | null | undefined;
  status?: RunSummary["status"];
  /** Show "Phase i/n · name" and the ETA line. */
  detailed?: boolean;
  run?: Pick<RunSummary, "progress" | "started_at" | "status">;
  className?: string;
}

/** Overall progress bar (+ phase dots, phase name and ETA when detailed). */
export function RunProgress({ progress, status, detailed, run, className }: RunProgressProps) {
  const frac = progressFraction(progress);
  const eta = run ? runEta(run) : null;
  const tone = status === "failed" ? "danger" : status === "completed" ? "success" : status === "killed" || status === "lost" ? "warning" : "accent";
  if (!progress) return <span className={clsx("text-[12.5px] text-faint", className)}>—</span>;
  return (
    <div className={clsx("min-w-0", className)}>
      {detailed && (
        <div className="mb-1.5 flex items-baseline justify-between gap-3 text-[12.5px]">
          <span className="flex min-w-0 items-center gap-2 text-muted">
            <span className="flex shrink-0 items-center gap-1" aria-hidden>
              {Array.from({ length: progress.n_phases }).map((_, i) => (
                <span
                  key={i}
                  className={clsx(
                    "h-1.5 w-1.5 rounded-full",
                    i < progress.phase_index ? "bg-fg/40" : i === progress.phase_index ? "bg-accent" : "bg-border-strong",
                  )}
                />
              ))}
            </span>
            <span className="truncate">
              Phase {progress.phase_index + 1}/{progress.n_phases} · <span className="text-fg">{progress.phase}</span>
            </span>
          </span>
          <span className="tabular shrink-0 text-muted">
            {formatInt(progress.step)} / {formatInt(progress.total_steps)}
          </span>
        </div>
      )}
      <div className="flex items-center gap-2">
        <ProgressBar value={frac} tone={tone} size={detailed ? "sm" : "xs"} className="min-w-0 flex-1" />
        {!detailed && <span className="tabular w-10 shrink-0 text-right text-[12px] text-muted">{frac == null ? "—" : formatPercent(frac, 0)}</span>}
      </div>
      {detailed && (
        <div className="mt-1 flex justify-between text-[12px] text-muted">
          <span>{frac == null ? "—" : formatPercent(frac, 1)}</span>
          {eta != null && <span>reste ≈ {formatDuration(eta)}</span>}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// MetricValue / MetricList
// ---------------------------------------------------------------------------

/** A metric value with its French label as tooltip. */
export function MetricValue({ metric, value, className }: { metric: string; value: number | null | undefined; className?: string }) {
  const info = metricInfo(metric);
  return (
    <Tooltip content={`${metricLabel(metric, { withGroup: true })}${info.hint ? ` — ${info.hint}` : ""}`}>
      <span className={clsx("tabular", className)}>{formatMetric(metric, value)}</span>
    </Tooltip>
  );
}

/** Inline list "Label valeur · Label valeur". */
export function MetricList({ items, className }: { items: { key: string; value: number }[]; className?: string }) {
  if (!items.length) return <span className={clsx("text-[12.5px] text-faint", className)}>Pas encore de validation</span>;
  return (
    <div className={clsx("flex flex-wrap gap-x-4 gap-y-1 text-[12.5px]", className)}>
      {items.map((m) => (
        <span key={m.key} className="whitespace-nowrap text-muted">
          {metricLabel(m.key)} <span className="tabular font-medium text-fg">{formatMetric(m.key, m.value)}</span>
        </span>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TagEditor
// ---------------------------------------------------------------------------

export interface TagEditorProps {
  tags: string[];
  onChange: (tags: string[]) => void | Promise<unknown>;
  disabled?: boolean;
  className?: string;
}

/** Editable tag chips (Enter to add, × to remove). */
export function TagEditor({ tags, onChange, disabled, className }: TagEditorProps) {
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const commit = () => {
    const t = draft.trim();
    setDraft("");
    setAdding(false);
    if (t && !tags.includes(t)) void onChange([...tags, t]);
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.stopPropagation();
      setDraft("");
      setAdding(false);
    }
  };
  return (
    <div className={clsx("flex flex-wrap items-center gap-1.5", className)}>
      {tags.map((t) => (
        <span
          key={t}
          className="group inline-flex h-6 items-center gap-1 rounded-full border border-border bg-surface pl-2.5 pr-1.5 text-[12px] text-fg"
        >
          {t}
          {!disabled && (
            <button
              type="button"
              aria-label={`Retirer ${t}`}
              className="rounded-full p-0.5 text-faint hover:bg-hover hover:text-fg"
              onClick={() => void onChange(tags.filter((x) => x !== t))}
            >
              <X size={11} strokeWidth={2} />
            </button>
          )}
        </span>
      ))}
      {!disabled &&
        (adding ? (
          <input
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKey}
            onBlur={commit}
            placeholder="nouveau tag"
            className="h-6 w-28 rounded-full border border-accent/50 bg-surface px-2.5 text-[12px] text-fg outline-none"
          />
        ) : (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="inline-flex h-6 items-center gap-1 rounded-full border border-dashed border-border px-2.5 text-[12px] text-muted hover:border-border-strong hover:text-fg"
          >
            <Plus size={12} strokeWidth={2} /> Tag
          </button>
        ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// JobList
// ---------------------------------------------------------------------------

function jobTarget(job: Job): string {
  if (job.kind === "retrieve") return `Machine ${job.target}`;
  const base = job.target.split("/").pop() ?? job.target;
  return base.replace(/\.ya?ml$/, "");
}

/** Compact list of jobs with cancel / sync actions and links to their run or study. */
export function JobList({ jobs, empty, className }: { jobs: Job[]; empty?: string; className?: string }) {
  const cancel = useCancelJob();
  const sync = useSyncJob();
  const confirm = useConfirm();
  const toast = useToast();

  if (!jobs.length) return <div className={clsx("py-6 text-center text-[13px] text-muted", className)}>{empty ?? "Aucun job."}</div>;

  const onCancel = async (job: Job) => {
    const ok = await confirm({
      title: "Annuler ce job ?",
      message: "Le process d'entraînement recevra un signal d'arrêt ; le run sera marqué « Arrêté ».",
      confirmLabel: "Annuler le job",
      cancelLabel: "Garder",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await cancel.mutateAsync(job.job_id);
      toast.success("Job annulé");
    } catch (err) {
      toast.error("Annulation impossible", errorMessage(err));
    }
  };
  const onSync = async (job: Job) => {
    try {
      await sync.mutateAsync(job.job_id);
      toast.success("Synchronisation lancée");
    } catch (err) {
      toast.error("Synchronisation impossible", errorMessage(err));
    }
  };

  return (
    <ul className={clsx("divide-y divide-border", className)}>
      {jobs.map((job) => {
        const link = job.run_id ? runPath(job.run_id) : job.study_id ? studyPath(job.study_id) : job.kind === "retrieve" ? "/hosts" : null;
        const active = isActiveStatus(job.status);
        const retrievalIssue = hasRetrievalIssue(job) ? retrievalNotice(job.retrieval) : null;
        const result = job.kind === "retrieve" && job.status === "completed" ? describeRetrieveResult(job.result) : "";
        return (
          <li key={job.job_id} className="flex items-center gap-3 py-2.5">
            <StatusBadge status={job.status} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 truncate text-[13.5px] text-fg">
                <span className="truncate">{jobTarget(job)}</span>
                {job.smoke && <Badge tone="info">smoke</Badge>}
              </div>
              <div className="truncate text-[12px] text-muted" title={job.git_ref && job.git_sha ? `Révision ${job.git_ref} (${job.git_sha})` : undefined}>
                {JOB_KIND_LABELS[job.kind] ?? job.kind} · {job.host} · {formatRelative(job.created_at)}
                {job.user && ` · par ${job.user}`}
                {job.run_id && <> · <span className="font-mono">{shortId(job.run_id)}</span></>}
                {job.git_sha && <> · <span className="font-mono">{job.git_sha.slice(0, 7)}</span></>}
                {result && ` · ${result}`}
                {job.error && <span className="text-danger"> · {job.error}</span>}
              </div>
              {retrievalIssue && (
                <div className="mt-0.5 flex items-start gap-1 text-[12px] leading-snug text-warning" title={job.retrieval?.missing.join("\n")}>
                  <TriangleAlert size={12} strokeWidth={2} className="mt-0.5 shrink-0" aria-hidden />
                  <span className="line-clamp-2">{retrievalIssue}</span>
                </div>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-0.5">
              {job.host !== "local" && (
                <IconButton icon={RefreshCw} label="Synchroniser maintenant" size="sm" onClick={() => void onSync(job)} />
              )}
              {active && <IconButton icon={Ban} label="Annuler le job" size="sm" onClick={() => void onCancel(job)} />}
              {link && (
                <Link to={link} className="no-drag inline-flex h-7 w-7 items-center justify-center rounded-lg text-muted hover:bg-hover hover:text-fg" aria-label="Ouvrir">
                  <ArrowUpRight size={15} strokeWidth={1.75} />
                </Link>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}
