/**
 * Metric knowledge: French labels, optimisation direction, value formatting,
 * grouping of normalised keys (ARCHITECTURE.md §6.5) and small numeric helpers
 * (Pareto front, EMA smoothing, constraint parsing).
 */
import { formatNumber, formatPercent, formatSignificant, DASH } from "./format.ts";
import type { Direction, Modality } from "./types";

export type MetricKind = "ratio" | "percent100" | "db" | "sci" | "seconds" | "gb" | "number" | "int";

export interface MetricInfo {
  label: string;
  direction: Direction | null;
  kind: MetricKind;
  /** Short description displayed in tooltips. */
  hint?: string;
}

/** Known base metric names (last meaningful segment of a key). */
export const METRIC_INFO: Record<string, MetricInfo> = {
  psnr: { label: "PSNR", direction: "maximize", kind: "db", hint: "Rapport signal/bruit de crête (qualité image/vidéo)" },
  ssim: { label: "SSIM", direction: "maximize", kind: "number", hint: "Similarité structurelle (1 = identique)" },
  snr: { label: "SNR", direction: "maximize", kind: "db", hint: "Rapport signal/bruit (qualité audio)" },
  si_snr: { label: "SI-SNR", direction: "maximize", kind: "db", hint: "SNR invariant à l'échelle" },
  stoi: { label: "STOI", direction: "maximize", kind: "number", hint: "Intelligibilité de la parole" },
  lpips: { label: "LPIPS", direction: "minimize", kind: "number", hint: "Distance perceptuelle apprise (0 = identique)" },
  flicker: { label: "Scintillement", direction: "minimize", kind: "number", hint: "Instabilité temporelle du résidu" },
  bit_acc: { label: "Bit acc.", direction: "maximize", kind: "ratio", hint: "Proportion de bits correctement extraits" },
  bit_acc_mean: {
    label: "Bit acc. moyenne",
    direction: "maximize",
    kind: "ratio",
    hint: "Moyenne de la bit acc. sur les attaques (hors identité)",
  },
  loss: { label: "Perte", direction: "minimize", kind: "number" },
  grad_norm: { label: "Norme du gradient", direction: null, kind: "number", hint: "Norme L2 du gradient (avant clipping)" },
  lr: { label: "Taux d'apprentissage", direction: null, kind: "sci" },
  attack_strength: { label: "Force d'attaque", direction: null, kind: "number" },
  step_time_s: { label: "Temps par step", direction: "minimize", kind: "seconds" },
  data_wait_frac: { label: "Attente des données", direction: "minimize", kind: "ratio" },
  items_per_s: { label: "Débit (items/s)", direction: "maximize", kind: "number" },
  gpu_util: { label: "Utilisation GPU", direction: null, kind: "percent100" },
  gpu_mem_gb: { label: "Mémoire GPU", direction: null, kind: "gb" },
  cpu_percent: { label: "CPU", direction: null, kind: "percent100" },
  ram_gb: { label: "RAM", direction: null, kind: "gb" },
  proc_ram_gb: { label: "RAM du process", direction: null, kind: "gb", hint: "Mémoire résidente du process d'entraînement" },
  step: { label: "Step", direction: null, kind: "int" },
};

export interface MetricGroup {
  id: string;
  label: string;
  keys: string[];
}

export const GROUP_LABELS: Record<string, string> = {
  train: "Entraînement",
  val: "Validation",
  best: "Meilleures valeurs",
  eval: "Évaluation",
  system: "Système",
  other: "Autres",
};

const GROUP_ORDER = ["train", "val", "best", "eval", "system", "other"];
const SYSTEM_KEYS = new Set(["gpu_util", "gpu_mem_gb", "cpu_percent", "ram_gb", "proc_ram_gb"]);
const GROUP_SHORT: Record<string, string> = { train: "train", val: "val", eval: "éval" };

const TPR_RE = /^tpr@([^_]+)(_mean)?$/;

/** Group id of a key ("train/loss" → "train"). */
export function metricGroup(key: string): string {
  const first = key.split("/")[0];
  if (first === "train" || first === "val" || first === "best" || first === "eval") return first;
  if (SYSTEM_KEYS.has(key) || first === "system") return "system";
  return "other";
}

function tprLabel(name: string): string | null {
  const m = TPR_RE.exec(name);
  if (!m) return null;
  return `${m[2] ? "TPR moyen" : "TPR"} @ FPR ${m[1].replace(".", ",")}`;
}

function baseInfo(name: string): MetricInfo {
  const tpr = tprLabel(name);
  if (tpr) return { label: tpr, direction: "maximize", kind: "ratio", hint: "Taux de détection à taux de faux positifs fixé" };
  return METRIC_INFO[name] ?? { label: name, direction: null, kind: "number" };
}

/** Full metadata for any key (label without group context). */
export function metricInfo(key: string): MetricInfo {
  const parts = key.split("/");
  if (parts[0] === "best" && parts.length > 1) {
    const inner = metricInfo(parts.slice(1).join("/"));
    return { ...inner, label: `Meilleur · ${inner.label}` };
  }
  const rest = parts[0] === "train" || parts[0] === "val" || parts[0] === "eval" || parts[0] === "system" ? parts.slice(1) : parts;
  if (rest.length === 0) return { label: key, direction: null, kind: "number" };
  const [head, ...tail] = rest;
  const base = baseInfo(head);
  if (tail.length === 0) return base;
  const suffix = tail.join("/");
  if (head === "loss") return { ...base, label: `Perte · ${suffix}` };
  if (head === "weight") return { label: `Poids · ${suffix}`, direction: null, kind: "number" };
  if (head === "bit_acc") return { ...base, label: `Bit acc. · ${suffix}` };
  return { ...base, label: `${base.label} · ${suffix}` };
}

/** French label; `withGroup` appends the split ("PSNR (val)"). */
export function metricLabel(key: string, opts: { withGroup?: boolean } = {}): string {
  const info = metricInfo(key);
  if (!opts.withGroup) return info.label;
  const group = key.split("/")[0];
  const short = GROUP_SHORT[group];
  return short ? `${info.label} (${short})` : info.label;
}

/** Optimisation direction of a key, or null when it is not an objective. */
export function metricDirection(key: string): Direction | null {
  return metricInfo(key).direction;
}

/** Format a metric value according to its kind ("93,4 %", "38,20 dB", "3,0e-4"…). */
export function formatMetric(key: string, value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  switch (metricInfo(key).kind) {
    case "ratio":
      return formatPercent(value, 1);
    case "percent100":
      return `${formatNumber(value, { maxDigits: 0 })} %`;
    case "db":
      return `${formatNumber(value, { digits: 2 })} dB`;
    case "sci":
      return formatSignificant(value, 3);
    case "seconds":
      return `${formatSignificant(value, 3)} s`;
    case "gb":
      return `${formatNumber(value, { maxDigits: 1 })} Go`;
    case "int":
      return formatNumber(value, { maxDigits: 0 });
    default:
      return formatSignificant(value, 4);
  }
}

/** Compact axis formatter for charts (no unit repetition). */
export function formatMetricAxis(key: string, value: number): string {
  const kind = metricInfo(key).kind;
  if (kind === "ratio") return formatPercent(value, 0);
  if (kind === "sci") return formatSignificant(value, 2);
  return formatSignificant(value, 3);
}

const IMPORTANT_ORDER = ["loss", "bit_acc_mean", "bit_acc", "psnr", "ssim", "snr", "si_snr", "stoi", "lpips", "flicker", "lr"];

function importance(key: string): number {
  const parts = key.split("/");
  const head = parts[0] === "best" ? parts[2] ?? "" : parts[1] ?? parts[0];
  const idx = IMPORTANT_ORDER.indexOf(head);
  const nested = parts.length > (parts[0] === "best" ? 3 : 2) ? 0.5 : 0;
  return (idx === -1 ? IMPORTANT_ORDER.length : idx) + nested;
}

/** Group keys by prefix (train/, val/, best/, eval/, system, other) with important keys first. */
export function groupMetricKeys(keys: string[]): MetricGroup[] {
  const buckets = new Map<string, string[]>();
  for (const key of keys) {
    const g = metricGroup(key);
    if (!buckets.has(g)) buckets.set(g, []);
    buckets.get(g)!.push(key);
  }
  return GROUP_ORDER.filter((g) => buckets.has(g)).map((g) => ({
    id: g,
    label: GROUP_LABELS[g],
    keys: buckets.get(g)!.sort((a, b) => importance(a) - importance(b) || a.localeCompare(b)),
  }));
}

/** Default quality metric per modality (§4.5). */
export function qualityMetricFor(modality: Modality | string | null | undefined): string {
  return modality === "audio" ? "snr" : "psnr";
}

/** Keys worth showing first for a run of the given modality. */
export function keyMetricKeys(modality: Modality | string | null | undefined): string[] {
  const q = qualityMetricFor(modality);
  return ["val/bit_acc_mean", `val/${q}`, "val/bit_acc", "val/loss"];
}

/** Present key metrics of a summary, in display order (falls back to the first val/* keys). */
export function pickKeyMetrics(
  summary: Record<string, number> | null | undefined,
  modality: Modality | string | null | undefined,
  max = 3,
): { key: string; value: number }[] {
  if (!summary) return [];
  const out: { key: string; value: number }[] = [];
  for (const key of keyMetricKeys(modality)) {
    if (typeof summary[key] === "number") out.push({ key, value: summary[key] });
  }
  if (out.length < max) {
    for (const key of Object.keys(summary).sort()) {
      if (key.startsWith("val/") && !out.some((o) => o.key === key) && typeof summary[key] === "number") {
        out.push({ key, value: summary[key] });
      }
      if (out.length >= max) break;
    }
  }
  return out.slice(0, max);
}

/** True when `a` is strictly better than `b` in the given direction. */
export function isBetter(a: number, b: number, direction: Direction): boolean {
  return direction === "maximize" ? a > b : a < b;
}

/**
 * Indices of the non-dominated points (Pareto front), ordered by increasing x.
 * Points with non-finite coordinates are ignored.
 */
export function paretoFront(points: { x: number; y: number }[], xDir: Direction, yDir: Direction): number[] {
  const sx = xDir === "maximize" ? 1 : -1;
  const sy = yDir === "maximize" ? 1 : -1;
  const candidates = points
    .map((p, i) => ({ i, X: p.x * sx, Y: p.y * sy }))
    .filter((p) => Number.isFinite(p.X) && Number.isFinite(p.Y))
    .sort((a, b) => b.X - a.X || b.Y - a.Y);
  const front: number[] = [];
  let bestY = -Infinity;
  for (const c of candidates) {
    if (c.Y > bestY) {
      front.push(c.i);
      bestY = c.Y;
    }
  }
  return front.sort((a, b) => points[a].x - points[b].x);
}

/** TensorBoard-like debiased exponential moving average (weight in [0, 1)). */
export function smoothSeries(values: number[], weight: number): number[] {
  if (weight <= 0) return values;
  const w = Math.min(weight, 0.999);
  const out: number[] = [];
  let last = 0;
  let n = 0;
  for (const v of values) {
    if (!Number.isFinite(v)) {
      out.push(v);
      continue;
    }
    last = last * w + (1 - w) * v;
    n++;
    out.push(last / (1 - Math.pow(w, n)));
  }
  return out;
}

export interface Constraint {
  op: ">=" | "<=" | ">" | "<" | "==";
  value: number;
}

/** Parse ">= 38" style constraints (project objectives). */
export function parseConstraint(text: string | null | undefined): Constraint | null {
  if (!text) return null;
  const m = /^\s*(>=|<=|==|>|<)\s*(-?[\d.]+(?:e-?\d+)?)\s*$/i.exec(text);
  if (!m) return null;
  const value = Number(m[2]);
  return Number.isFinite(value) ? { op: m[1] as Constraint["op"], value } : null;
}

/** Whether a value satisfies a constraint. */
export function satisfies(value: number, c: Constraint): boolean {
  switch (c.op) {
    case ">=":
      return value >= c.value;
    case "<=":
      return value <= c.value;
    case ">":
      return value > c.value;
    case "<":
      return value < c.value;
    default:
      return value === c.value;
  }
}
