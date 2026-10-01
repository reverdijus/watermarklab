/** Run / job / study helpers shared by pages (status labels, progress, ETA…). */
import { secondsBetween, toDate } from "./format.ts";
import type { Job, JobStatus, Modality, Progress, RunStatus, RunSummary } from "./types";

export type Tone = "neutral" | "accent" | "success" | "warning" | "danger" | "info";

export const STATUS_LABELS: Record<RunStatus | JobStatus, string> = {
  queued: "En file",
  launching: "Lancement",
  running: "En cours",
  completed: "Terminé",
  failed: "Échec",
  killed: "Arrêté",
  lost: "Perdu",
};

export const STATUS_TONES: Record<RunStatus | JobStatus, Tone> = {
  queued: "neutral",
  launching: "info",
  running: "accent",
  completed: "success",
  failed: "danger",
  killed: "warning",
  lost: "warning",
};

export const RUN_STATUSES: RunStatus[] = ["queued", "running", "completed", "failed", "killed", "lost"];

export const MODALITY_LABELS: Record<Modality, string> = {
  image: "Image",
  audio: "Audio",
  video: "Vidéo",
};

/** CSS color (token) used for the modality dot. */
export const MODALITY_COLORS: Record<Modality, string> = {
  image: "var(--chart-2)",
  audio: "var(--chart-3)",
  video: "var(--chart-4)",
};

export const JOB_KIND_LABELS: Record<Job["kind"], string> = {
  train: "Entraînement",
  hpo: "Étude HPO",
  eval: "Évaluation",
  setup: "Préparation machine",
  retrieve: "Rapatriement des checkpoints",
};

/** A run, study or job that may still change (drives polling). */
export function isActiveStatus(status: string | null | undefined): boolean {
  return status === "running" || status === "queued" || status === "launching";
}

/**
 * HPO trial stopped early by the study's pruner. The trainer records it as
 * `status = "killed"`, `error = "pruned"` (wmlab.train.trainer): this is the normal
 * behaviour of a study, not a failure, and the UI labels it "Élagué".
 */
export function isPrunedTrial(run: Pick<RunSummary, "trial" | "status" | "error">): boolean {
  return run.trial != null && run.status === "killed" && run.error === "pruned";
}

/** Status label of a run, "Élagué" for a pruned HPO trial. */
export function runStatusLabel(run: Pick<RunSummary, "trial" | "status" | "error">): string {
  return isPrunedTrial(run) ? "Élagué" : STATUS_LABELS[run.status];
}

/** Progress in [0, 1] (null when unknown). */
export function progressFraction(progress: Progress | null | undefined): number | null {
  if (!progress || !progress.total_steps) return null;
  return Math.min(1, Math.max(0, progress.step / progress.total_steps));
}

/** Estimated remaining seconds from elapsed time and step progress. */
export function runEta(run: Pick<RunSummary, "progress" | "started_at" | "status">, now: number = Date.now()): number | null {
  if (run.status !== "running") return null;
  const frac = progressFraction(run.progress);
  const elapsed = secondsBetween(run.started_at, null, now);
  if (frac === null || elapsed === null || frac <= 0.002 || elapsed <= 0) return null;
  return (elapsed * (1 - frac)) / frac;
}

/** Duration of a run (uses the server value, else started → ended/now). */
export function runDuration(run: Pick<RunSummary, "duration_s" | "started_at" | "ended_at">, now: number = Date.now()): number | null {
  if (typeof run.duration_s === "number") return run.duration_s;
  return secondsBetween(run.started_at, run.ended_at, now);
}

/** Sort helper: most recent first by created_at. */
export function byNewest<T extends { created_at: string }>(a: T, b: T): number {
  return (toDate(b.created_at)?.getTime() ?? 0) - (toDate(a.created_at)?.getTime() ?? 0);
}

/** Route of a run page. */
export function runPath(runId: string, tab?: string): string {
  return `/runs/${encodeURIComponent(runId)}${tab ? `?tab=${tab}` : ""}`;
}

/** Route of a study page. */
export function studyPath(studyId: string): string {
  return `/studies/${encodeURIComponent(studyId)}`;
}

/** Route of a project page. */
export function projectPath(name: string, tab?: string): string {
  return `/projects/${encodeURIComponent(name)}${tab ? `?tab=${tab}` : ""}`;
}

/** Route of an experiment editor. */
export function experimentPath(project: string, experiment: string): string {
  return `/projects/${encodeURIComponent(project)}/experiments/${encodeURIComponent(experiment)}`;
}

/** Route of an HPO plan editor. */
export function hpoPlanPath(project: string, plan: string): string {
  return `/projects/${encodeURIComponent(project)}/hpo/${encodeURIComponent(plan)}`;
}
