/**
 * Theme handling: preference ("system" | "light" | "dark") stored in
 * localStorage and applied as [data-theme] on <html>; `useResolvedTheme`
 * returns the effective mode and re-renders on OS or preference changes.
 */
import { useEffect, useSyncExternalStore } from "react";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

const STORAGE_KEY = "wmlab.theme";
const listeners = new Set<() => void>();

function readPreference(): ThemePreference {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch {
    /* ignore */
  }
  return "system";
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Apply the stored preference to <html> (call once at startup). */
export function applyThemePreference(pref: ThemePreference = readPreference()): void {
  const root = document.documentElement;
  if (pref === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", pref);
}

/** Change and persist the theme preference. */
export function setThemePreference(pref: ThemePreference): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, pref);
  } catch {
    /* ignore */
  }
  applyThemePreference(pref);
  listeners.forEach((l) => l());
}

export function getThemePreference(): ThemePreference {
  return readPreference();
}

/** Effective theme currently displayed. */
export function getResolvedTheme(): ResolvedTheme {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced === "light" || forced === "dark") return forced;
  return systemTheme();
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
  mq?.addEventListener("change", cb);
  const observer = new MutationObserver(cb);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => {
    listeners.delete(cb);
    mq?.removeEventListener("change", cb);
    observer.disconnect();
  };
}

/** Effective theme ("light" | "dark"), reactive. */
export function useResolvedTheme(): ResolvedTheme {
  return useSyncExternalStore(subscribe, getResolvedTheme, () => "light");
}

/** Stored preference, reactive. */
export function useThemePreference(): [ThemePreference, (p: ThemePreference) => void] {
  const pref = useSyncExternalStore(subscribe, readPreference, () => "system" as ThemePreference);
  return [pref, setThemePreference];
}

/** Read a CSS custom property from <html> (e.g. cssVar("--accent")). */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Keep <html data-theme> in sync with the stored preference across windows. */
export function useThemeSync(): void {
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) {
        applyThemePreference();
        listeners.forEach((l) => l());
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
}
