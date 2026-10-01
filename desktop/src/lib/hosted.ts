/**
 * Pure helpers of the hosted mode (§12): git revision of a launch, toast after a git-backed
 * save, checkpoint retrieval notices and decommission guards. No React, unit-tested.
 */
import { formatBytes, formatInt, plural } from "./format.ts";
import type { FileGitResult, GitState, Host, Job, JobRetrieval, RetrieveResult } from "./types";

// ---------------------------------------------------------------------------
// Git revision of a launch
// ---------------------------------------------------------------------------

/** "main @ 3f2a9c1" — the label shown for the server checkout. */
export function gitRevisionLabel(git: Pick<GitState, "branch" | "short_sha" | "sha"> | null | undefined): string {
  if (!git) return "";
  const sha = git.short_sha || (git.sha ? git.sha.slice(0, 7) : "");
  if (git.branch && sha) return `${git.branch} @ ${sha}`;
  return git.branch || sha || "";
}

/**
 * `git_ref` to send for a launch. `value` is the "Révision git" field, `defaultLabel` its
 * pre-filled text (`gitRevisionLabel`). An unchanged field pins the sha shown in hosted mode
 * (HEAD may move under the auto pull), and sends nothing outside hosted mode (current
 * behaviour: the workspace is synchronised as is). A `branch @ sha` form keeps the sha.
 */
export function gitRefForLaunch(value: string, defaultLabel: string, git: Pick<GitState, "sha" | "hosted"> | null | undefined): string | undefined {
  const v = value.trim();
  if (!v || v === defaultLabel.trim()) return git?.hosted && git.sha ? git.sha : undefined;
  const m = /^(\S+)\s*@\s*([0-9a-fA-F]{7,40})$/.exec(v);
  return m ? m[2] : v;
}

// ---------------------------------------------------------------------------
// Toast after PUT /api/files
// ---------------------------------------------------------------------------

export interface SaveNotice {
  tone: "success" | "warning";
  title: string;
  description: string | null;
}

/**
 * Toast content after a save, from the `git` field of the response: "Enregistré · commité
 * abc1234 · poussé", a warning when the commit could not be pushed (or nothing was
 * committed), or a plain "Enregistré" when `git` is null (local mode / no repository).
 */
export function describeFileSave(git: FileGitResult | null | undefined, title = "Enregistré", detail: string | null = null): SaveNotice {
  if (!git) return { tone: "success", title, description: detail };
  const short = git.committed ? git.committed.slice(0, 7) : null;
  if (short && git.pushed) return { tone: "success", title: `${title} · commité ${short} · poussé`, description: detail };
  if (short) {
    return {
      tone: "warning",
      title: `${title} · commité ${short} mais non poussé`,
      description: git.error ? `Push refusé : ${git.error}` : "Le push a échoué ; le commit reste local sur le serveur.",
    };
  }
  return {
    tone: "warning",
    title: `${title}, mais non commité`,
    description: git.error ?? "Le commit git a échoué sur le serveur.",
  };
}

// ---------------------------------------------------------------------------
// Checkpoint retrieval (§12.4)
// ---------------------------------------------------------------------------

/** Short French sentence for a partial / failed retrieval, null when nothing to report. */
export function retrievalNotice(retrieval: JobRetrieval | null | undefined): string | null {
  if (!retrieval || retrieval.status === "complete" || retrieval.status === "n/a") return null;
  const missing = retrieval.missing.length;
  const head = retrieval.status === "failed" ? "Rapatriement des checkpoints échoué" : "Rapatriement partiel";
  if (!missing) return head;
  return `${head} : ${plural(missing, "checkpoint manquant", "checkpoints manquants")} — relancez le rapatriement depuis la page Machines`;
}

/** Job worth a warning in lists: a finished train / hpo / eval job whose retrieval is incomplete. */
export function hasRetrievalIssue(job: Pick<Job, "kind" | "retrieval">): boolean {
  return job.kind !== "retrieve" && job.kind !== "setup" && !!job.retrieval && (job.retrieval.status === "partial" || job.retrieval.status === "failed");
}

/** "12 runs · 1 étude · 37 checkpoints · 18,2 Go" (+ missing count). */
export function describeRetrieveResult(result: RetrieveResult | null | undefined): string {
  if (!result) return "";
  const parts = [
    plural(result.runs, "run"),
    plural(result.studies, "étude"),
    plural(result.checkpoints, "checkpoint"),
    formatBytes(result.bytes),
  ];
  if (result.missing.length) parts.push(`${formatInt(result.missing.length)} manquant${result.missing.length > 1 ? "s" : ""}`);
  return parts.join(" · ");
}

// ---------------------------------------------------------------------------
// Decommission guards
// ---------------------------------------------------------------------------

/**
 * Reasons preventing "Retirer la machine" (empty = allowed). Mirrors the server refusals of
 * §12.4 so the button is disabled with an explanation instead of failing.
 */
export function decommissionBlockers(
  host: Pick<Host, "unretrieved_checkpoints" | "unretrieved_bytes" | "decommissioned_at" | "kind">,
  activeJobs: number,
): string[] {
  const out: string[] = [];
  if (host.kind === "local") out.push("La machine locale ne peut pas être retirée.");
  if (host.decommissioned_at) out.push("Machine déjà retirée.");
  if (activeJobs > 0) out.push(`${plural(activeJobs, "job actif", "jobs actifs")} sur cette machine.`);
  const n = host.unretrieved_checkpoints ?? 0;
  if (n > 0) {
    const size = host.unretrieved_bytes ? ` (${formatBytes(host.unretrieved_bytes)})` : "";
    out.push(`${plural(n, "checkpoint non rapatrié", "checkpoints non rapatriés")}${size} — lancez le rapatriement.`);
  }
  return out;
}
