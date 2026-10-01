import clsx from "clsx";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { STATUS_LABELS, STATUS_TONES, type Tone } from "../../lib/runs";
import type { JobStatus, RunStatus } from "../../lib/types";

export type { Tone };

const SOFT: Record<Tone, string> = {
  neutral: "bg-surface-2 text-muted border-border",
  accent: "bg-accent-soft text-accent border-transparent",
  success: "bg-success/12 text-success border-transparent",
  warning: "bg-warning/12 text-warning border-transparent",
  danger: "bg-danger/12 text-danger border-transparent",
  info: "bg-info/12 text-info border-transparent",
};

const OUTLINE: Record<Tone, string> = {
  neutral: "text-muted border-border",
  accent: "text-accent border-accent/40",
  success: "text-success border-success/40",
  warning: "text-warning border-warning/40",
  danger: "text-danger border-danger/40",
  info: "text-info border-info/40",
};

const DOT: Record<Tone, string> = {
  neutral: "bg-faint",
  accent: "bg-accent",
  success: "bg-success",
  warning: "bg-warning",
  danger: "bg-danger",
  info: "bg-info",
};

export interface BadgeProps {
  children?: ReactNode;
  tone?: Tone;
  variant?: "soft" | "outline";
  size?: "sm" | "md";
  icon?: LucideIcon;
  /** Leading colored dot. */
  dot?: boolean;
  /** Animated dot (live state). */
  pulse?: boolean;
  mono?: boolean;
  className?: string;
  title?: string;
}

/** Small pill label. */
export function Badge({
  children,
  tone = "neutral",
  variant = "soft",
  size = "sm",
  icon: Icon,
  dot,
  pulse,
  mono,
  className,
  title,
}: BadgeProps) {
  return (
    <span
      title={title}
      className={clsx(
        "inline-flex max-w-full shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border font-medium",
        size === "sm" ? "h-5 px-2 text-[11.5px]" : "h-6 px-2.5 text-[12.5px]",
        variant === "soft" ? SOFT[tone] : OUTLINE[tone],
        mono && "font-mono text-[11px]",
        className,
      )}
    >
      {dot && (
        <span className="relative inline-flex h-1.5 w-1.5 shrink-0">
          {pulse && <span className={clsx("absolute inset-0 rounded-full animate-pulse-dot", DOT[tone])} />}
          <span className={clsx("relative inline-flex h-1.5 w-1.5 rounded-full", DOT[tone])} />
        </span>
      )}
      {Icon && <Icon size={12} strokeWidth={2} aria-hidden />}
      {children != null && <span className="truncate">{children}</span>}
    </span>
  );
}

export interface StatusBadgeProps {
  status: RunStatus | JobStatus;
  size?: "sm" | "md";
  className?: string;
  /** Dot only (no label), e.g. in dense lists. */
  dotOnly?: boolean;
  /** HPO trial stopped by the pruner (`isPrunedTrial`): labelled "Élagué" instead of "Arrêté". */
  pruned?: boolean;
}

/** Run/job status with French label; running states get an animated dot. */
export function StatusBadge({ status, size = "sm", className, dotOnly, pruned }: StatusBadgeProps) {
  const tone = STATUS_TONES[status] ?? "neutral";
  const live = status === "running" || status === "launching";
  const label = pruned ? "Élagué" : (STATUS_LABELS[status] ?? status);
  if (dotOnly) {
    return (
      <span title={label} className={clsx("relative inline-flex h-2 w-2 shrink-0", className)}>
        {live && <span className={clsx("absolute inset-0 rounded-full animate-pulse-dot", DOT[tone])} />}
        <span className={clsx("relative inline-flex h-2 w-2 rounded-full", DOT[tone])} />
      </span>
    );
  }
  return (
    <Badge tone={tone} size={size} dot pulse={live} className={className} title={pruned ? "Essai élagué par le pruner de l'étude HPO" : undefined}>
      {label}
    </Badge>
  );
}
