/**
 * French formatting helpers (numbers, money, durations, relative dates…).
 * Pure functions without runtime imports so they can be unit-tested with `node --test`.
 */

export type DateInput = string | number | Date | null | undefined;

const LOCALE = "fr-FR";
const DASH = "—";

/** Parse an ISO string, an epoch (seconds or milliseconds) or a Date. */
export function toDate(value: DateInput): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return Number.isNaN(value.getTime()) ? null : value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return null;
    return new Date(value < 1e12 ? value * 1000 : value);
  }
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function isNum(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

const nfCache = new Map<string, Intl.NumberFormat>();
function nf(options: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = JSON.stringify(options);
  let f = nfCache.get(key);
  if (!f) {
    f = new Intl.NumberFormat(LOCALE, options);
    nfCache.set(key, f);
  }
  return f;
}

/** "1 234,5" — `digits` fixes the number of decimals, otherwise up to `maxDigits` (default 2). */
export function formatNumber(
  value: number | null | undefined,
  opts: { digits?: number; maxDigits?: number; signed?: boolean } = {},
): string {
  if (!isNum(value)) return DASH;
  const { digits, maxDigits = 2, signed = false } = opts;
  return nf({
    minimumFractionDigits: digits ?? 0,
    maximumFractionDigits: digits ?? maxDigits,
    signDisplay: signed ? "exceptZero" : "auto",
  }).format(value);
}

/** Integer with French thousands separators: "15 000". */
export function formatInt(value: number | null | undefined): string {
  if (!isNum(value)) return DASH;
  return nf({ maximumFractionDigits: 0 }).format(Math.round(value));
}

/** Compact notation: "15 k", "1,2 M". */
export function formatCompact(value: number | null | undefined): string {
  if (!isNum(value)) return DASH;
  return nf({ notation: "compact", maximumFractionDigits: 1 }).format(value);
}

/** Significant-digit formatting for metrics of unknown scale (scientific when tiny/huge). */
export function formatSignificant(value: number | null | undefined, significant = 4): string {
  if (!isNum(value)) return DASH;
  const abs = Math.abs(value);
  if (abs !== 0 && (abs < 1e-3 || abs >= 1e7)) {
    const [mantissa, exp] = value.toExponential(Math.max(0, significant - 2)).split("e");
    return `${mantissa.replace(".", ",")}e${exp.replace("+", "")}`;
  }
  return nf({ maximumSignificantDigits: significant }).format(value);
}

/** Money in euros: "1 234,56 €" (compact: "12,3 k€"). */
export function formatEur(value: number | null | undefined, opts: { compact?: boolean; digits?: number } = {}): string {
  if (!isNum(value)) return DASH;
  if (opts.compact && Math.abs(value) >= 10000) {
    return `${nf({ notation: "compact", maximumFractionDigits: 1 }).format(value)}€`;
  }
  const digits = opts.digits ?? 2;
  return nf({
    style: "currency",
    currency: "EUR",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

/** Ratio in [0, 1] as a percentage: 0.934 → "93,4 %". */
export function formatPercent(ratio: number | null | undefined, digits = 1): string {
  if (!isNum(ratio)) return DASH;
  return nf({ style: "percent", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(ratio);
}

/** Duration in seconds: "42 s", "3 min 12 s", "2 h 14 min", "3 j 4 h". */
export function formatDuration(seconds: number | null | undefined): string {
  if (!isNum(seconds) || seconds < 0) return DASH;
  if (seconds < 1) return "< 1 s";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) {
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return m < 10 && rest ? `${m} min ${rest} s` : `${m} min`;
  }
  if (s < 86400) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return m ? `${h} h ${m} min` : `${h} h`;
  }
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  return h ? `${d} j ${h} h` : `${d} j`;
}

/** Hours with one decimal: "12,5 h". */
export function formatHours(hours: number | null | undefined, digits = 1): string {
  if (!isNum(hours)) return DASH;
  return `${formatNumber(hours, { maxDigits: digits })} h`;
}

/** Relative date: "à l'instant", "il y a 3 min", "il y a 2 h", "hier", "il y a 4 j", "dans 5 min". */
export function formatRelative(value: DateInput, now: number = Date.now()): string {
  const d = toDate(value);
  if (!d) return DASH;
  const diff = (now - d.getTime()) / 1000;
  // Timestamps just ahead of `now` (clock ticks every 15–60 s, server dates rounded to the
  // second) are "now", not the future.
  const future = diff < -90;
  const abs = Math.abs(diff);
  const wrap = (s: string) => (future ? `dans ${s}` : `il y a ${s}`);
  if (abs < 45 || (!future && diff < 0)) return future ? "dans un instant" : "à l'instant";
  if (abs < 3600) return wrap(`${Math.max(1, Math.round(abs / 60))} min`);
  if (abs < 86400) return wrap(`${Math.round(abs / 3600)} h`);
  const days = Math.round(abs / 86400);
  if (days === 1) return future ? "demain" : "hier";
  if (days < 7) return wrap(`${days} j`);
  return formatDateShort(d);
}

const dfCache = new Map<string, Intl.DateTimeFormat>();
function df(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = JSON.stringify(options);
  let f = dfCache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(LOCALE, options);
    dfCache.set(key, f);
  }
  return f;
}

/** Full date and time: "29 sept. 2026, 14:05". */
export function formatDate(value: DateInput): string {
  const d = toDate(value);
  if (!d) return DASH;
  return df({ dateStyle: "medium", timeStyle: "short" }).format(d);
}

/** Short date: "29 sept." (year added when not the current year). */
export function formatDateShort(value: DateInput): string {
  const d = toDate(value);
  if (!d) return DASH;
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return df(sameYear ? { day: "numeric", month: "short" } : { day: "numeric", month: "short", year: "numeric" }).format(d);
}

/** Calendar day: "29/09/2026". */
export function formatDay(value: DateInput): string {
  const d = toDate(value);
  if (!d) return DASH;
  return df({ day: "2-digit", month: "2-digit", year: "numeric" }).format(d);
}

/** Clock time: "14:05:12". */
export function formatTime(value: DateInput, withSeconds = true): string {
  const d = toDate(value);
  if (!d) return DASH;
  return df(
    withSeconds ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" },
  ).format(d);
}

/** Month key "2026-10" → "oct. 2026". */
export function formatMonth(month: string): string {
  const m = /^(\d{4})-(\d{2})/.exec(month);
  if (!m) return month;
  return df({ month: "short", year: "numeric" }).format(new Date(Number(m[1]), Number(m[2]) - 1, 1));
}

/** File size with French units: "1,2 Go". */
export function formatBytes(bytes: number | null | undefined): string {
  if (!isNum(bytes)) return DASH;
  const units = ["o", "Ko", "Mo", "Go", "To"];
  let v = Math.abs(bytes);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${formatNumber(Math.sign(bytes) * v, { maxDigits: v < 10 && i > 0 ? 1 : 0 })} ${units[i]}`;
}

/** Step counts: "15 000" (compact: "15 k"). */
export function formatSteps(steps: number | null | undefined, compact = false): string {
  if (!isNum(steps)) return DASH;
  return compact && Math.abs(steps) >= 10000 ? formatCompact(steps) : formatInt(steps);
}

/** "3 runs", "1 run", "aucun run". */
export function plural(n: number, one: string, many?: string, zero?: string): string {
  if (n === 0 && zero) return zero;
  return `${formatInt(n)} ${Math.abs(n) >= 2 ? (many ?? `${one}s`) : one}`;
}

/** Short, readable form of a run id: "20261001-120000-baseline-3fa2" → "baseline-3fa2". */
export function shortId(id: string | null | undefined): string {
  if (!id) return DASH;
  const m = /^\d{8}-\d{6}-(.+)$/.exec(id);
  return m ? m[1] : id;
}

/** Timestamp part of an id ("20261001-120000-…") as a Date (UTC), when present. */
export function idDate(id: string): Date | null {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(id);
  if (!m) return null;
  return new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
}

/** Seconds elapsed between two dates (end defaults to now). */
export function secondsBetween(start: DateInput, end?: DateInput, now: number = Date.now()): number | null {
  const s = toDate(start);
  if (!s) return null;
  const e = toDate(end ?? null);
  return ((e ? e.getTime() : now) - s.getTime()) / 1000;
}

export { DASH };
