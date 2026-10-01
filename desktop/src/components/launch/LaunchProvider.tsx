import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { useHotkey } from "../../lib/hooks";
import { usePermissions } from "../../lib/queries";
import type { InitFrom } from "../../lib/types";
import { LaunchDialog } from "./LaunchDialog";

export type LaunchKind = "train" | "hpo" | "eval";

/** Values pre-filled in the launch dialog (all optional). */
export interface LaunchPreset {
  kind?: LaunchKind;
  project?: string;
  /** Experiment name (train). */
  experiment?: string;
  /** HPO plan name (hpo). */
  plan?: string;
  host?: string;
  overrides?: Record<string, string>;
  smoke?: boolean;
  maxHours?: number | null;
  /** Branch from a checkpoint (train). */
  initFrom?: InitFrom | null;
  /** Evaluation target. */
  evalRun?: string;
  evalCheckpoint?: string;
  benchmark?: string;
}

export interface LaunchContextValue {
  /** Open the global launch dialog, optionally pre-filled. */
  openLaunch: (preset?: LaunchPreset) => void;
  closeLaunch: () => void;
  isOpen: boolean;
}

const LaunchContext = createContext<LaunchContextValue | null>(null);

/**
 * Global "Nouveau lancement" dialog (Cmd+N) available through `useLaunch()`. A member
 * (read-only, §12.6) never gets it: `openLaunch` is a no-op and ⌘N is disabled.
 */
export function LaunchProvider({ children }: { children: ReactNode }) {
  const { canWrite } = usePermissions();
  const [state, setState] = useState<{ open: boolean; preset: LaunchPreset; seq: number }>({
    open: false,
    preset: {},
    seq: 0,
  });

  const openLaunch = useCallback(
    (preset: LaunchPreset = {}) => {
      if (!canWrite) return;
      setState((s) => ({ open: true, preset, seq: s.seq + 1 }));
    },
    [canWrite],
  );
  const closeLaunch = useCallback(() => setState((s) => ({ ...s, open: false })), []);

  // ⌘N while the dialog is already open must not remount it (the form would be reset).
  useHotkey("mod+n", () => openLaunch(), { enabled: !state.open && canWrite });

  const value = useMemo(() => ({ openLaunch, closeLaunch, isOpen: state.open }), [openLaunch, closeLaunch, state.open]);

  return (
    <LaunchContext.Provider value={value}>
      {children}
      {state.open && canWrite && <LaunchDialog key={state.seq} preset={state.preset} onClose={closeLaunch} />}
    </LaunchContext.Provider>
  );
}

/** `const { openLaunch } = useLaunch(); openLaunch({ kind: "eval", evalRun: id })`. */
export function useLaunch(): LaunchContextValue {
  const ctx = useContext(LaunchContext);
  if (!ctx) throw new Error("useLaunch doit être utilisé dans <LaunchProvider>");
  return ctx;
}
