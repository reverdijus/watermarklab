/**
 * Unsaved drafts (run / project notes, YAML specs) kept in memory so that they survive
 * in-app navigation (tab changes, page changes) until saved or discarded, plus a
 * `beforeunload` guard: while at least one draft is dirty, reloading (⌘R) or closing the
 * window asks for confirmation (the main process shows the dialog on `will-prevent-unload`).
 *
 * Keys are namespaced by the caller ("file:<path>", "run-notes:<run_id>", …). A draft is
 * only restored when the server content (`base`) has not changed in the meantime.
 */

export interface Draft {
  draft: string;
  base: string;
}

const drafts = new Map<string, Draft>();
let guardInstalled = false;

function onBeforeUnload(e: BeforeUnloadEvent) {
  if (drafts.size === 0) return;
  e.preventDefault();
  // Legacy browsers (and Chromium's will-prevent-unload) need a returnValue.
  e.returnValue = "";
}

/** Install the `beforeunload` guard once (no-op outside a browser). */
export function installDraftGuard(): void {
  if (guardInstalled || typeof window === "undefined") return;
  guardInstalled = true;
  window.addEventListener("beforeunload", onBeforeUnload);
}

/** Remembered draft for `key`, or null. */
export function getDraft(key: string): Draft | null {
  return drafts.get(key) ?? null;
}

/**
 * Draft to restore for `key` when the current server content is `base`; null when there is
 * none or when the server content changed since (the stale draft is then forgotten).
 */
export function restoreDraft(key: string, base: string): string | null {
  const pending = drafts.get(key);
  if (!pending) return null;
  if (pending.base !== base) {
    drafts.delete(key);
    return null;
  }
  return pending.draft;
}

/** Remember (dirty) or forget (clean) a draft. */
export function rememberDraft(key: string, draft: string, base: string): void {
  if (draft === base) drafts.delete(key);
  else drafts.set(key, { draft, base });
}

export function clearDraft(key: string): void {
  drafts.delete(key);
}

/** True while at least one draft has unsaved changes. */
export function hasDirtyDrafts(): boolean {
  return drafts.size > 0;
}

/** Keys of the dirty drafts (for diagnostics / tests). */
export function dirtyDraftKeys(): string[] {
  return [...drafts.keys()];
}

/** Forget every draft (tests). */
export function resetDrafts(): void {
  drafts.clear();
}
