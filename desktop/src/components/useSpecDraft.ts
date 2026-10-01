/**
 * Editing state of a workspace spec file: loads it (GET /api/files), keeps a local draft,
 * tracks unsaved changes, validates the draft live (debounced POST /api/validate) and saves
 * it (PUT /api/files). A remote change is followed as long as there is no local edit.
 * Unsaved drafts survive in-app navigation (kept in memory until saved or discarded).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { clearDraft, getDraft, rememberDraft, restoreDraft } from "../lib/drafts";
import { useFile, useSaveFile, useValidation } from "../lib/queries";
import type { SaveFileResult, ValidationError } from "../lib/types";

/** Draft-store key of a workspace file (drafts are restored on the same file version). */
const draftKey = (path: string) => `file:${path}`;

export interface SpecDraft {
  /** File query (loading / error / server-side validation). */
  file: ReturnType<typeof useFile>;
  /** Current editor content. */
  content: string;
  setContent: (value: string) => void;
  /** True once the file content is available. */
  ready: boolean;
  /** Unsaved local changes. */
  dirty: boolean;
  /** An unsaved draft from a previous visit was restored. */
  restored: boolean;
  /** Save the draft; resolves with the server validation. */
  save: () => Promise<SaveFileResult>;
  saving: boolean;
  /** Discard local changes (and the remembered draft). */
  reset: () => void;
  /** Live validation of the draft (errors + phase plan). */
  validation: ReturnType<typeof useValidation>;
  errors: ValidationError[];
  valid: boolean | undefined;
}

export function useSpecDraft(path: string | null | undefined, kind?: string): SpecDraft {
  const file = useFile(path);
  const saveFile = useSaveFile();
  const [draft, setDraft] = useState<string | null>(null);
  const [base, setBase] = useState<string | null>(null);
  const [restored, setRestored] = useState(false);
  const loadedFor = useRef<string | null>(null);
  const draftRef = useRef(draft);
  const baseRef = useRef(base);
  draftRef.current = draft;
  baseRef.current = base;

  const remote = file.data?.content;
  useEffect(() => {
    if (remote === undefined || !path) return;
    if (loadedFor.current !== path) {
      loadedFor.current = path;
      const pending = restoreDraft(draftKey(path), remote);
      setDraft(pending ?? remote);
      setBase(remote);
      setRestored(pending !== null);
      return;
    }
    // Follow a remote change only when the user has not edited the file.
    if (remote !== baseRef.current && draftRef.current === baseRef.current) {
      setDraft(remote);
      setBase(remote);
    }
  }, [remote, path]);

  // Remember unsaved drafts across page changes.
  useEffect(() => {
    if (!path || draft === null || base === null || loadedFor.current !== path) return;
    rememberDraft(draftKey(path), draft, base);
  }, [path, draft, base]);

  const content = draft ?? remote ?? "";
  const ready = draft !== null;
  const dirty = draft !== null && base !== null && draft !== base;

  // Stable body reference: the debounce hook restarts its timer whenever the object changes.
  const body = useMemo(() => (path && ready ? { content, kind, path } : null), [path, ready, content, kind]);
  const validation = useValidation(body);
  const errors = validation.data?.errors ?? file.data?.validation.errors ?? [];
  const valid = validation.data ? validation.data.valid : file.data?.validation.valid;

  const save = useCallback(async () => {
    if (!path) throw new Error("Aucun fichier à enregistrer.");
    const value = draftRef.current ?? "";
    const res = await saveFile.mutateAsync({ path, content: value });
    if (getDraft(draftKey(path))?.draft === value) clearDraft(draftKey(path));
    setBase(value);
    setRestored(false);
    return res;
  }, [path, saveFile]);

  const reset = useCallback(() => {
    if (path) clearDraft(draftKey(path));
    setDraft(baseRef.current);
    setRestored(false);
  }, [path]);

  return {
    file,
    content,
    setContent: setDraft,
    ready,
    dirty,
    restored,
    save,
    saving: saveFile.isPending,
    reset,
    validation,
    errors,
    valid,
  };
}
