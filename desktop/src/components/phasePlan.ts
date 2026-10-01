/**
 * Pure helpers of the curriculum timeline (no React / DOM): the aligned curve rows built
 * from a phase plan (LR factor, attack strength, loss weights) and their interpolation.
 * Kept apart from PhaseTimeline.tsx so that `npm test` can exercise them.
 */
import { formatNumber, formatPercent } from "../lib/format.ts";
import type { PhasePreview } from "../lib/types";

export type Pt = [number, number];

/** Linear interpolation of a [step, value] series at `step`. */
export function valueAt(series: Pt[], step: number): number | null {
  if (!series.length) return null;
  if (step <= series[0][0]) return series[0][1];
  for (let i = 1; i < series.length; i++) {
    const [x1, y1] = series[i];
    if (step <= x1) {
      const [x0, y0] = series[i - 1];
      return x1 === x0 ? y1 : y0 + ((y1 - y0) * (step - x0)) / (x1 - x0);
    }
  }
  return series[series.length - 1][1];
}

export interface CurveRow {
  id: string;
  label: string;
  /** Legend note (e.g. the scale used). */
  note?: string;
  lines: { name: string; color: string; points: Pt[] }[];
  format: (v: number) => string;
  /**
   * Draw the row on a log scale. Loss weights span several orders of magnitude (img = 300,
   * msg = 1): on a linear scale the small terms are flat lines along the bottom and their
   * ramps are invisible. The read-out always shows real values.
   */
  logScale?: boolean;
}

/** Ratio between the largest and smallest positive value above which a log scale is used. */
export const LOG_SCALE_RATIO = 20;

/** Attack strength of a phase without attacks is meaningless: drawn as 0 (identity). */
export function attackStrengthPoints(phase: PhasePreview): Pt[] {
  return phase.attacks.length ? phase.attack_strength : phase.attack_strength.map(([step]) => [step, 0]);
}

export function buildRows(phases: PhasePreview[]): CurveRow[] {
  // Series of a phase stop at its last sampled step: hold the last value up to `end` so
  // that the curve reaches the phase boundary.
  const concat = (pick: (p: PhasePreview) => Pt[] | undefined) =>
    phases
      .flatMap((p) => {
        const pts = pick(p) ?? [];
        const last = pts[pts.length - 1];
        return last && last[0] < p.end ? [...pts, [p.end, last[1]] as Pt] : pts;
      })
      .sort((a, b) => a[0] - b[0]);
  const rows: CurveRow[] = [
    {
      id: "lr",
      label: "Facteur de LR",
      lines: [{ name: "LR", color: "var(--chart-2)", points: concat((p) => p.lr) }],
      format: (v) => `×${formatNumber(v, { maxDigits: 3 })}`,
    },
    {
      id: "strength",
      label: "Force d'attaque",
      lines: [{ name: "Force", color: "var(--chart-1)", points: concat(attackStrengthPoints) }],
      format: (v) => formatPercent(v, 0),
    },
  ];
  const terms = [...new Set(phases.flatMap((p) => Object.keys(p.loss_weights ?? {})))].sort();
  if (terms.length) {
    const lines = terms.map((term, i) => ({
      name: term,
      color: `var(--chart-${((i + 2) % 6) + 1})`,
      points: concat((p) => p.loss_weights?.[term]),
    }));
    const positive = lines.flatMap((l) => l.points.map((p) => p[1])).filter((v) => v > 0);
    // Linear scale when the terms are of comparable magnitude, log scale otherwise.
    const logScale = positive.length > 1 && Math.max(...positive) / Math.min(...positive) > LOG_SCALE_RATIO;
    rows.push({
      id: "weights",
      label: "Poids des pertes",
      note: logScale ? "échelle logarithmique" : undefined,
      lines,
      format: (v) => formatNumber(v, { maxDigits: 3 }),
      logScale,
    });
  }
  return rows.filter((r) => r.lines.some((l) => l.points.length > 0));
}
