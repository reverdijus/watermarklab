/** Generic React hooks (debounce, hotkeys, persisted state, ticking clock). */
import { useCallback, useEffect, useRef, useState } from "react";

/** Structural identity of a value (objects compared by content, not by reference). */
function stableKey(value: unknown): unknown {
  if (value === null || typeof value !== "object") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return value;
  }
}

/**
 * Value that only updates after `delay` ms without change. Objects are compared
 * structurally, so callers may pass a literal recreated on every render without
 * restarting the timer (and without re-render loops).
 */
export function useDebouncedValue<T>(value: T, delay = 400): T {
  const [debounced, setDebounced] = useState(value);
  const latest = useRef(value);
  latest.current = value;
  const key = stableKey(value);
  useEffect(() => {
    const id = window.setTimeout(() => setDebounced(() => latest.current), delay);
    return () => window.clearTimeout(id);
  }, [key, delay]);
  return debounced;
}

export interface HotkeyOptions {
  /** Trigger even when focus is in an input/textarea/editor (default true for mod-combos). */
  allowInInputs?: boolean;
  enabled?: boolean;
}

/**
 * Global keyboard shortcut. `combo` uses "mod" for Cmd (macOS) / Ctrl (others),
 * e.g. "mod+n", "mod+\\", "mod+s", "escape".
 */
export function useHotkey(combo: string, handler: (e: KeyboardEvent) => void, opts: HotkeyOptions = {}): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  const { enabled = true } = opts;
  const parts = combo.toLowerCase().split("+");
  const key = parts[parts.length - 1];
  const needMod = parts.includes("mod");
  const needShift = parts.includes("shift");
  const needAlt = parts.includes("alt");
  const allowInInputs = opts.allowInInputs ?? needMod;

  useEffect(() => {
    if (!enabled) return;
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      if (needMod !== mod || needShift !== e.shiftKey || needAlt !== e.altKey) return;
      if (e.key.toLowerCase() !== key && e.code.toLowerCase() !== `key${key}`) {
        // Backslash is reported as "\\" (key) or "Backslash" (code).
        if (!(key === "\\" && e.code === "Backslash")) return;
      }
      if (!allowInInputs) {
        const t = e.target as HTMLElement | null;
        if (t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName))) return;
      }
      e.preventDefault();
      handlerRef.current(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enabled, key, needMod, needShift, needAlt, allowInInputs]);
}

/** useState persisted in localStorage (JSON). */
export function useLocalStorage<T>(key: string, initial: T): [T, (v: T | ((prev: T) => T)) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });
  const set = useCallback(
    (v: T | ((prev: T) => T)) => {
      setValue((prev) => {
        const next = typeof v === "function" ? (v as (p: T) => T)(prev) : v;
        try {
          window.localStorage.setItem(key, JSON.stringify(next));
        } catch {
          /* storage full or unavailable */
        }
        return next;
      });
    },
    [key],
  );
  return [value, set];
}

/** Current time (ms), refreshed every `intervalMs` — keeps relative dates alive. */
export function useNow(intervalMs = 30000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}

/** Latest value in a ref (for stable callbacks). */
export function useLatest<T>(value: T): { readonly current: T } {
  const ref = useRef(value);
  ref.current = value;
  return ref;
}
