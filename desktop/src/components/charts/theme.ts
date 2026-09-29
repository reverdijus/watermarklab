/**
 * Chart theming derived from the CSS tokens (light/dark aware) + shared
 * option builders so every chart looks the same.
 */
import { useMemo } from "react";
import { cssVar, useResolvedTheme, type ResolvedTheme } from "../../lib/theme";

export interface ChartTokens {
  theme: ResolvedTheme;
  text: string;
  muted: string;
  faint: string;
  border: string;
  surface: string;
  surface2: string;
  accent: string;
  accentSoft: string;
  success: string;
  warning: string;
  danger: string;
  info: string;
  /** Categorical palette (validated: fixed order, never cycled without dashes). */
  palette: string[];
  fontSans: string;
  fontMono: string;
}

const FALLBACK_LIGHT = ["#c6613f", "#3f6fb0", "#3e8a5e", "#7b5cb8", "#b8862b", "#c0567f"];

/** Read the chart tokens from the current CSS variables. */
export function readChartTokens(theme: ResolvedTheme): ChartTokens {
  const palette = [1, 2, 3, 4, 5, 6].map((i, idx) => cssVar(`--chart-${i}`) || FALLBACK_LIGHT[idx]);
  return {
    theme,
    text: cssVar("--text") || "#1f1e1d",
    muted: cssVar("--muted") || "#6e6c64",
    faint: cssVar("--faint") || "#9a978d",
    border: cssVar("--border") || "#e6e3d9",
    surface: cssVar("--surface") || "#ffffff",
    surface2: cssVar("--surface-2") || "#f5f4ef",
    accent: cssVar("--accent") || "#c6613f",
    accentSoft: cssVar("--accent-soft") || "#f5e6de",
    success: cssVar("--success") || "#3e7b57",
    warning: cssVar("--warning") || "#a86b12",
    danger: cssVar("--danger") || "#b3413a",
    info: cssVar("--info") || "#44689e",
    palette,
    fontSans: 'system-ui, -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif',
    fontMono: 'ui-monospace, "SF Mono", Menlo, monospace',
  };
}

/** Chart tokens, recomputed when the effective theme changes. */
export function useChartTokens(): ChartTokens {
  const theme = useResolvedTheme();
  return useMemo(() => readChartTokens(theme), [theme]);
}

/**
 * Series color by index: the six validated hues in fixed order; beyond six the
 * hues repeat and callers must add a secondary encoding (dashed lines).
 */
export function seriesColor(t: ChartTokens, index: number): string {
  return t.palette[index % t.palette.length];
}

/** Whether series `index` needs a secondary encoding (dash) because hues repeat. */
export function needsDash(index: number, t: ChartTokens): boolean {
  return index >= t.palette.length;
}

/** Hex (#rrggbb) + alpha → rgba(). Falls back to the input for non-hex colors. */
export function withAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color.trim());
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

/** Linear interpolation between two hex colors. */
export function mixHex(a: string, b: string, t: number): string {
  const pa = /^#([0-9a-f]{6})$/i.exec(a.trim());
  const pb = /^#([0-9a-f]{6})$/i.exec(b.trim());
  if (!pa || !pb) return t < 0.5 ? a : b;
  const na = parseInt(pa[1], 16);
  const nb = parseInt(pb[1], 16);
  const ch = (n: number, s: number) => (n >> s) & 255;
  const mix = (s: number) => Math.round(ch(na, s) + (ch(nb, s) - ch(na, s)) * t);
  return `#${[16, 8, 0].map((s) => mix(s).toString(16).padStart(2, "0")).join("")}`;
}

/** Relative luminance of a hex color (0 = black, 1 = white). */
export function luminance(hex: string): number {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return 0.5;
  const n = parseInt(m[1], 16);
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255);
}

/** Escape text inserted in tooltip HTML. */
export function escapeHtml(s: unknown): string {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/** Tooltip container style shared by all charts. */
export function tooltipBase(t: ChartTokens) {
  return {
    backgroundColor: t.surface,
    borderColor: t.border,
    borderWidth: 1,
    padding: [8, 10],
    textStyle: { color: t.text, fontSize: 12, fontFamily: t.fontSans },
    extraCssText: `border-radius: 10px; box-shadow: 0 4px 16px rgba(0,0,0,${t.theme === "dark" ? 0.35 : 0.08});`,
    confine: true,
  };
}

/** Value/category axis style shared by all charts (recessive grid, muted labels). */
export function axisBase(t: ChartTokens) {
  return {
    axisLine: { show: true, lineStyle: { color: t.border } },
    axisTick: { show: false },
    axisLabel: { color: t.muted, fontSize: 11, fontFamily: t.fontSans, hideOverlap: true },
    splitLine: { show: true, lineStyle: { color: t.border, opacity: 0.6, type: [3, 3] as number[] } },
    nameTextStyle: { color: t.muted, fontSize: 11, fontFamily: t.fontSans },
  };
}

/** Legend style. */
export function legendBase(t: ChartTokens) {
  return {
    type: "scroll" as const,
    top: 0,
    left: 0,
    icon: "roundRect",
    itemWidth: 10,
    itemHeight: 4,
    itemGap: 14,
    textStyle: { color: t.muted, fontSize: 11.5, fontFamily: t.fontSans },
    pageIconColor: t.muted,
    pageTextStyle: { color: t.muted },
  };
}

/** A colored dot for tooltip rows. */
export function tooltipDot(color: string): string {
  return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:6px"></span>`;
}
