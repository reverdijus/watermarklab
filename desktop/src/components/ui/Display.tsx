import clsx from "clsx";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Skeleton } from "./Skeleton";
import { Tooltip } from "./Tooltip";
import type { Tone } from "../../lib/runs";

// ---------------------------------------------------------------------------
// EmptyState
// ---------------------------------------------------------------------------

export interface EmptyStateProps {
  icon?: LucideIcon;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  /** Less vertical padding (inside cards). */
  compact?: boolean;
  className?: string;
}

/** Calm placeholder for empty lists and not-yet-available features. */
export function EmptyState({ icon: Icon, title, description, action, compact, className }: EmptyStateProps) {
  return (
    <div className={clsx("flex flex-col items-center justify-center text-center", compact ? "px-4 py-8" : "px-6 py-16", className)}>
      {Icon && (
        <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-xl bg-surface-2 text-muted">
          <Icon size={19} strokeWidth={1.6} aria-hidden />
        </div>
      )}
      <div className="font-serif text-[17px] text-fg">{title}</div>
      {description && <div className="mt-1.5 max-w-[440px] text-[13.5px] leading-relaxed text-muted">{description}</div>}
      {action && <div className="mt-4 flex items-center gap-2">{action}</div>}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ProgressBar
// ---------------------------------------------------------------------------

const BAR_TONES: Record<Tone, string> = {
  neutral: "bg-faint",
  accent: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
};

export interface ProgressBarProps {
  /** Fraction in [0, 1]; null renders an empty track. */
  value: number | null | undefined;
  tone?: Tone;
  size?: "xs" | "sm" | "md";
  className?: string;
  title?: string;
}

/** Thin horizontal progress bar. */
export function ProgressBar({ value, tone = "accent", size = "sm", className, title }: ProgressBarProps) {
  const pct = value == null || !Number.isFinite(value) ? 0 : Math.max(0, Math.min(1, value)) * 100;
  return (
    <div
      title={title}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(pct)}
      className={clsx(
        "w-full overflow-hidden rounded-full bg-surface-2",
        size === "xs" ? "h-1" : size === "sm" ? "h-1.5" : "h-2",
        className,
      )}
    >
      <div className={clsx("h-full rounded-full transition-[width] duration-500", BAR_TONES[tone])} style={{ width: `${pct}%` }} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Kpi
// ---------------------------------------------------------------------------

export interface KpiProps {
  label: ReactNode;
  value: ReactNode;
  /** Secondary line under the value. */
  sub?: ReactNode;
  icon?: LucideIcon;
  /** Optional progress bar (fraction 0..1). */
  progress?: number | null;
  progressTone?: Tone;
  hint?: ReactNode;
  loading?: boolean;
  onClick?: () => void;
  className?: string;
}

/** Stat tile: label, big value, optional sub-line and progress. */
export function Kpi({ label, value, sub, icon: Icon, progress, progressTone = "accent", hint, loading, onClick, className }: KpiProps) {
  const body = (
    <div
      onClick={onClick}
      className={clsx(
        "flex h-full flex-col rounded-xl border border-border bg-surface px-4 py-3.5 shadow-sm",
        onClick && "cursor-pointer transition-colors hover:border-border-strong",
        className,
      )}
    >
      <div className="flex items-center gap-1.5 text-[12.5px] text-muted">
        {Icon && <Icon size={14} strokeWidth={1.75} aria-hidden />}
        <span className="truncate">{label}</span>
      </div>
      {loading ? (
        <Skeleton className="mt-2 h-7 w-24" />
      ) : (
        <div className="tabular mt-1 truncate font-serif text-[26px] leading-tight text-fg">{value}</div>
      )}
      {sub && <div className="mt-0.5 truncate text-[12.5px] text-muted">{sub}</div>}
      {progress !== undefined && <ProgressBar value={progress} tone={progressTone} size="xs" className="mt-2.5" />}
    </div>
  );
  return hint ? (
    <Tooltip content={hint} wrapperClassName="block h-full">
      {body}
    </Tooltip>
  ) : (
    body
  );
}

// ---------------------------------------------------------------------------
// KeyValue
// ---------------------------------------------------------------------------

export interface KeyValueItem {
  label: ReactNode;
  value: ReactNode;
  mono?: boolean;
  hint?: string;
}

export interface KeyValueProps {
  items: KeyValueItem[];
  columns?: 1 | 2 | 3;
  dense?: boolean;
  className?: string;
}

/** Definition list (label → value) in one to three columns. */
export function KeyValue({ items, columns = 1, dense, className }: KeyValueProps) {
  return (
    <dl
      className={clsx(
        "grid gap-x-8",
        columns === 1 ? "grid-cols-1" : columns === 2 ? "grid-cols-1 md:grid-cols-2" : "grid-cols-1 md:grid-cols-3",
        dense ? "gap-y-1.5" : "gap-y-2.5",
        className,
      )}
    >
      {items.map((it, i) => (
        <div key={i} className="flex min-w-0 items-baseline justify-between gap-4 border-b border-border/70 pb-1.5 last:border-b-0">
          <dt className="shrink-0 text-[13px] text-muted" title={it.hint}>
            {it.label}
          </dt>
          <dd className={clsx("selectable min-w-0 truncate text-right text-[13px] text-fg", it.mono && "font-mono text-[12px]")}>
            {it.value ?? "—"}
          </dd>
        </div>
      ))}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// ModalityDot
// ---------------------------------------------------------------------------

/** Colored dot identifying a modality (image / audio / video). */
export function ModalityDot({ modality, className }: { modality: string; className?: string }) {
  const color = modality === "audio" ? "var(--chart-3)" : modality === "video" ? "var(--chart-4)" : "var(--chart-2)";
  return (
    <span
      className={clsx("inline-block h-2 w-2 shrink-0 rounded-full", className)}
      style={{ background: color }}
      title={modality === "audio" ? "Audio" : modality === "video" ? "Vidéo" : "Image"}
    />
  );
}

// ---------------------------------------------------------------------------
// Kbd
// ---------------------------------------------------------------------------

/** Keyboard shortcut chip. */
export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={clsx(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-md border border-border bg-surface-2 px-1 font-sans text-[11px] text-muted",
        className,
      )}
    >
      {children}
    </kbd>
  );
}

// ---------------------------------------------------------------------------
// Callout
// ---------------------------------------------------------------------------

const CALLOUT: Record<Tone, string> = {
  neutral: "bg-surface-2 text-fg border-border",
  accent: "bg-accent-soft text-fg border-accent/20",
  success: "bg-success/8 text-fg border-success/25",
  warning: "bg-warning/8 text-fg border-warning/25",
  danger: "bg-danger/8 text-fg border-danger/25",
  info: "bg-info/8 text-fg border-info/25",
};
const CALLOUT_ICON: Record<Tone, string> = {
  neutral: "text-muted",
  accent: "text-accent",
  success: "text-success",
  warning: "text-warning",
  danger: "text-danger",
  info: "text-info",
};

/** Inline message box (errors, warnings, tips). */
export function Callout({
  tone = "neutral",
  icon: Icon,
  title,
  children,
  action,
  className,
}: {
  tone?: Tone;
  icon?: LucideIcon;
  title?: ReactNode;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div className={clsx("flex items-start gap-3 rounded-xl border px-4 py-3 text-[13.5px]", CALLOUT[tone], className)}>
      {Icon && <Icon size={17} strokeWidth={1.75} className={clsx("mt-0.5 shrink-0", CALLOUT_ICON[tone])} aria-hidden />}
      <div className="min-w-0 flex-1">
        {title && <div className="font-medium">{title}</div>}
        {children && <div className={clsx("break-words text-muted", title && "mt-0.5")}>{children}</div>}
      </div>
      {action && <div className="shrink-0">{action}</div>}
    </div>
  );
}
