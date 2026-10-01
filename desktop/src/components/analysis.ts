/**
 * Pure helpers used by the DESKTOP-B pages (leaderboard objectives, budget projection,
 * run comparison, HPO search space, registry parameters). No React / DOM imports so they
 * can be unit-tested with `node --test`.
 */
import { formatSignificant } from "../lib/format.ts";
import { isBetter, metricDirection, satisfies, type Constraint } from "../lib/metrics.ts";
import type { Costs, Direction, LeaderRow, Objective } from "../lib/types";

// ---------------------------------------------------------------------------
// Objectives & leaderboard
// ---------------------------------------------------------------------------

/** Metric name without the "val/" prefix ("val/psnr" → "psnr"). */
export function bareMetric(metric: string): string {
  return metric.startsWith("val/") ? metric.slice(4) : metric;
}

/**
 * Virtual quality metric of a cross-project leaderboard: the default quality metric of
 * each row's modality (PSNR for image/video, SNR for audio — both in dB).
 */
export const AUTO_QUALITY = "quality";
const AUTO_QUALITY_KEYS = ["psnr", "snr"];

/** Value of an objective metric in a leaderboard row (summary first, then quality). */
export function leaderValue(row: Pick<LeaderRow, "summary" | "quality">, metric: string): number | null {
  if (metric === AUTO_QUALITY) {
    for (const k of AUTO_QUALITY_KEYS) {
      const v = leaderValue(row, k);
      if (v !== null) return v;
    }
    return null;
  }
  const key = bareMetric(metric);
  for (const source of [row.summary, row.quality]) {
    const v = source?.[key] ?? source?.[metric];
    if (typeof v === "number" && Number.isFinite(v)) return v;
  }
  return null;
}

/**
 * Default quality axis of a set of rows without project: PSNR or SNR when only one of
 * them is present, the virtual `AUTO_QUALITY` when modalities are mixed, else the first
 * available quality key.
 */
export function defaultQualityMetric(rows: Pick<LeaderRow, "quality">[], options: string[]): string {
  const present = AUTO_QUALITY_KEYS.filter((k) => rows.some((r) => typeof r.quality?.[k] === "number"));
  if (present.length > 1) return AUTO_QUALITY;
  if (present.length === 1) return present[0];
  return options[0] ?? "psnr";
}

/** Benchmark with the most rows (ties: first in `benchmarks`). */
export function busiestBenchmark(rows: Pick<LeaderRow, "benchmark">[], benchmarks: string[]): string {
  let best = benchmarks[0] ?? "";
  let bestCount = -1;
  for (const b of benchmarks) {
    const n = rows.filter((r) => r.benchmark === b).length;
    if (n > bestCount) {
      best = b;
      bestCount = n;
    }
  }
  return best;
}

/** Direction of an objective (explicit objective first, then known metric, else maximize). */
export function objectiveDirection(metric: string, objective?: Objective | null): Direction {
  if (objective && bareMetric(objective.metric) === bareMetric(metric)) return objective.direction;
  return metricDirection(bareMetric(metric)) ?? "maximize";
}

/** Default constraint operator for a direction (maximize ⇒ "≥"). */
export function defaultConstraintOp(direction: Direction): Constraint["op"] {
  return direction === "maximize" ? ">=" : "<=";
}

export interface Scored<T> {
  item: T;
  quality: number | null;
  robustness: number | null;
}

/**
 * Best item under a quality constraint: highest (or lowest) robustness among items whose
 * quality satisfies the constraint; ties broken on quality. Items without both values are ignored.
 */
export function bestUnderConstraint<T>(
  items: Scored<T>[],
  constraint: Constraint | null,
  robustnessDir: Direction,
  qualityDir: Direction,
): Scored<T> | null {
  let best: Scored<T> | null = null;
  for (const c of items) {
    if (c.quality === null || c.robustness === null) continue;
    if (constraint && !satisfies(c.quality, constraint)) continue;
    if (
      !best ||
      isBetter(c.robustness, best.robustness!, robustnessDir) ||
      (c.robustness === best.robustness && isBetter(c.quality, best.quality!, qualityDir))
    ) {
      best = c;
    }
  }
  return best;
}

/** Slider bounds and step for a list of values (with the constraint value included). */
export function sliderRange(values: number[], extra?: number | null): { min: number; max: number; step: number } {
  const all = values.filter((v) => Number.isFinite(v));
  if (extra != null && Number.isFinite(extra)) all.push(extra);
  if (!all.length) return { min: 0, max: 1, step: 0.01 };
  let min = Math.min(...all);
  let max = Math.max(...all);
  if (min === max) {
    const pad = Math.abs(min) * 0.05 || 1;
    min -= pad;
    max += pad;
  }
  const span = max - min;
  const raw = span / 200;
  const magnitude = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? raw;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const round = (v: number) => Number(v.toPrecision(12));
  return { min: round(lo), max: round(hi), step: round(step) };
}

// ---------------------------------------------------------------------------
// HPO
// ---------------------------------------------------------------------------

/** Running best of a sequence (nulls keep the previous best). */
export function cumulativeBest(values: (number | null)[], direction: Direction): (number | null)[] {
  let best: number | null = null;
  return values.map((v) => {
    if (v !== null && Number.isFinite(v) && (best === null || isBetter(v, best, direction))) best = v;
    return best;
  });
}

export interface SpaceRow {
  param: string;
  type: string;
  bounds: string;
  detail: string;
}

const fmtNum = (v: unknown): string => (typeof v === "number" ? formatSignificant(v, 4) : v == null ? "—" : String(v));

/** Rows of an HPO `space` mapping (param → {type, low, high, step, log, choices}). */
export function searchSpaceRows(space: unknown): SpaceRow[] {
  if (!space || typeof space !== "object" || Array.isArray(space)) return [];
  return Object.entries(space as Record<string, unknown>).map(([param, raw]) => {
    const d = raw && typeof raw === "object" && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {};
    const type = String(d.type ?? "—");
    if (type === "categorical" || Array.isArray(d.choices)) {
      const choices = Array.isArray(d.choices) ? d.choices : [];
      return {
        param,
        type: "catégoriel",
        bounds: `${choices.length} choix`,
        detail: choices.map((c) => (typeof c === "object" ? JSON.stringify(c) : fmtNum(c))).join(" · "),
      };
    }
    const parts: string[] = [];
    if (d.log === true) parts.push("échelle log");
    if (d.step != null) parts.push(`pas ${fmtNum(d.step)}`);
    return {
      param,
      type: type === "float" ? "réel" : type === "int" ? "entier" : type,
      bounds: `[${fmtNum(d.low)} ; ${fmtNum(d.high)}]`,
      detail: parts.join(" · ") || "linéaire",
    };
  });
}

// ---------------------------------------------------------------------------
// Compare
// ---------------------------------------------------------------------------

/** Keys present in every list (order of the first list). */
export function intersectKeys(lists: string[][]): string[] {
  if (!lists.length) return [];
  const [first, ...rest] = lists;
  const sets = rest.map((l) => new Set(l));
  return first.filter((k) => sets.every((s) => s.has(k)));
}

const stable = (v: unknown): string => (v === undefined ? "∅" : JSON.stringify(v));

/** Parameter keys whose values differ between runs (missing counts as a value). */
export function differingParams(runs: { params: Record<string, unknown> }[]): string[] {
  const keys = [...new Set(runs.flatMap((r) => Object.keys(r.params ?? {})))].sort();
  if (runs.length < 2) return keys;
  return keys.filter((k) => new Set(runs.map((r) => stable(r.params?.[k]))).size > 1);
}

// ---------------------------------------------------------------------------
// Budget
// ---------------------------------------------------------------------------

const DAY_MS = 86_400_000;

function parseDay(value: string | null | undefined): Date | null {
  if (!value) return null;
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00Z` : value);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "YYYY-MM" keys from `start` to `end` inclusive (UTC). */
export function monthRange(start: string, end: string): string[] {
  const s = /^(\d{4})-(\d{2})/.exec(start);
  const e = /^(\d{4})-(\d{2})/.exec(end);
  if (!s || !e) return [];
  let y = Number(s[1]);
  let m = Number(s[2]);
  const ey = Number(e[1]);
  const em = Number(e[2]);
  const out: string[] = [];
  while ((y < ey || (y === ey && m <= em)) && out.length < 240) {
    out.push(`${y}-${String(m).padStart(2, "0")}`);
    m += 1;
    if (m > 12) {
      m = 1;
      y += 1;
    }
  }
  return out;
}

function monthEnd(month: string): number {
  const [y, m] = month.split("-").map(Number);
  return Date.UTC(y, m, 1); // first day of the next month
}

export interface Burnup {
  months: string[];
  /** Cumulative spend at the end of each month (null for future months). */
  spent: (number | null)[];
  /** Linear budget consumption between start and end at the end of each month. */
  linear: (number | null)[];
  monthly: number[];
}

/** Monthly burn-up (cumulative spend) vs the linear budget between start and end. */
export function burnup(costs: Pick<Costs, "by_month" | "budget">, now: number = Date.now()): Burnup {
  const byMonth = new Map(costs.by_month.map((m) => [m.month.slice(0, 7), m.eur]));
  const known = [...byMonth.keys()].sort();
  const nowKey = new Date(now).toISOString().slice(0, 7);
  const startKey = costs.budget.start?.slice(0, 7) ?? known[0] ?? nowKey;
  const endKey = costs.budget.end?.slice(0, 7) ?? [nowKey, ...known].sort().at(-1)!;
  const months = monthRange(startKey <= endKey ? startKey : endKey, startKey <= endKey ? endKey : startKey);
  const start = parseDay(costs.budget.start)?.getTime();
  const end = parseDay(costs.budget.end)?.getTime();
  const total = costs.budget.total_eur;

  // Spending before the first displayed month is carried into it.
  let running = known.filter((k) => months.length && k < months[0]).reduce((s, k) => s + (byMonth.get(k) ?? 0), 0);
  const spent: (number | null)[] = [];
  const linear: (number | null)[] = [];
  const monthly: number[] = [];
  for (const m of months) {
    const eur = byMonth.get(m) ?? 0;
    monthly.push(eur);
    running += eur;
    spent.push(m <= nowKey ? Number(running.toFixed(2)) : null);
    if (start !== undefined && end !== undefined && end > start) {
      const frac = Math.min(1, Math.max(0, (monthEnd(m) - start) / (end - start)));
      linear.push(Number((total * frac).toFixed(2)));
    } else linear.push(null);
  }
  return { months, spent, linear, monthly };
}

/**
 * Spend projected at the end of the budget period at the pace of the last `windowDays`
 * days (items dated in the window / window length × remaining days + spent).
 */
export function projectedSpend(
  costs: Pick<Costs, "items" | "spent_eur" | "budget">,
  now: number = Date.now(),
  windowDays = 30,
): { projected: number | null; dailyRate: number } {
  const since = now - windowDays * DAY_MS;
  const recent = costs.items.reduce((sum, it) => {
    const t = parseDay(it.started_at)?.getTime();
    return t !== undefined && t >= since && t <= now ? sum + (it.eur || 0) : sum;
  }, 0);
  const dailyRate = recent / windowDays;
  const end = parseDay(costs.budget.end)?.getTime();
  if (end === undefined) return { projected: null, dailyRate };
  const remainingDays = Math.max(0, (end - now) / DAY_MS);
  return { projected: costs.spent_eur + dailyRate * remainingDays, dailyRate };
}

// ---------------------------------------------------------------------------
// Registry parameters (JSON Schema → rows)
// ---------------------------------------------------------------------------

export interface ParamRow {
  name: string;
  type: string;
  default: string;
  bounds: string;
  description: string;
  required: boolean;
}

type Schema = Record<string, unknown>;

function schemaType(s: Schema, defs: Record<string, Schema>): string {
  if (typeof s.$ref === "string") {
    const ref = defs[s.$ref.split("/").pop() ?? ""];
    return ref ? schemaType(ref, defs) : s.$ref.split("/").pop() ?? "objet";
  }
  if (Array.isArray(s.enum)) return s.enum.map((v) => JSON.stringify(v)).join(" | ");
  const variants = (s.anyOf ?? s.oneOf) as Schema[] | undefined;
  if (Array.isArray(variants)) {
    const types = variants.map((v) => schemaType(v, defs)).filter((t) => t !== "null");
    const nullable = variants.some((v) => v.type === "null");
    return `${[...new Set(types)].join(" | ")}${nullable ? " | null" : ""}`;
  }
  const t = s.type;
  if (Array.isArray(t)) return t.join(" | ");
  if (t === "array") {
    const items = s.items as Schema | undefined;
    return items ? `liste[${schemaType(items, defs)}]` : "liste";
  }
  if (t === "object") return "objet";
  if (typeof t === "string") return t === "number" ? "réel" : t === "integer" ? "entier" : t === "boolean" ? "booléen" : t === "string" ? "texte" : t;
  return "—";
}

function schemaBounds(s: Schema): string {
  const lo = s.minimum ?? s.exclusiveMinimum;
  const hi = s.maximum ?? s.exclusiveMaximum;
  if (lo === undefined && hi === undefined) {
    const variants = (s.anyOf ?? s.oneOf) as Schema[] | undefined;
    if (Array.isArray(variants)) return variants.map(schemaBounds).find(Boolean) ?? "";
    return "";
  }
  const left = lo === undefined ? "]−∞" : s.exclusiveMinimum !== undefined ? `]${fmtNum(lo)}` : `[${fmtNum(lo)}`;
  const right = hi === undefined ? "+∞[" : s.exclusiveMaximum !== undefined ? `${fmtNum(hi)}[` : `${fmtNum(hi)}]`;
  return `${left} ; ${right}`;
}

/** Parameter rows of a registry entry's `params_schema` (pydantic JSON Schema or signature-derived). */
export function schemaParams(schema: Record<string, unknown> | null | undefined): ParamRow[] {
  if (!schema || typeof schema !== "object") return [];
  const props = schema.properties as Record<string, Schema> | undefined;
  if (!props || typeof props !== "object") return [];
  const defs = ((schema.$defs ?? schema.definitions) as Record<string, Schema> | undefined) ?? {};
  const required = new Set(Array.isArray(schema.required) ? (schema.required as string[]) : []);
  return Object.entries(props).map(([name, s]) => ({
    name,
    type: schemaType(s ?? {}, defs),
    default: s && "default" in s ? JSON.stringify(s.default) : required.has(name) ? "requis" : "—",
    bounds: schemaBounds(s ?? {}),
    description: typeof s?.description === "string" ? s.description : typeof s?.title === "string" && s.title !== name ? s.title : "",
    required: required.has(name),
  }));
}

// ---------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------

/** Next free version name of a benchmark: image_v1 → image_v2 (skipping existing names). */
export function nextVersionName(name: string, version: number, existing: string[]): { name: string; version: number } {
  const m = /^(.*?)_v(\d+)$/.exec(name);
  const base = m ? m[1] : name;
  let v = Math.max(version, m ? Number(m[2]) : 1) + 1;
  const taken = new Set(existing);
  while (taken.has(`${base}_v${v}`)) v += 1;
  return { name: `${base}_v${v}`, version: v };
}

/** Lower-case identifier ("Mon essai" → "mon_essai") for spec file names. */
export function toIdentifier(s: string, sep: "_" | "-" = "_"): string {
  return s
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, sep)
    .replace(new RegExp(`^\\${sep}+|\\${sep}+$`, "g"), "")
    .slice(0, 48);
}

/** Workspace path "projects/<p>/experiments/<e>.yaml" → {project, experiment} (else null). */
export function experimentFromPath(path: string): { project: string; experiment: string } | null {
  const m = /^projects\/([^/]+)\/experiments\/([^/]+)\.ya?ml$/.exec(path);
  return m ? { project: m[1], experiment: m[2] } : null;
}
