import clsx from "clsx";
import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

export interface TabItem<T extends string = string> {
  id: T;
  label: ReactNode;
  icon?: LucideIcon;
  /** Small counter displayed after the label. */
  count?: number | null;
  disabled?: boolean;
}

export interface TabsProps<T extends string> {
  items: TabItem<T>[];
  value: T;
  onChange: (id: T) => void;
  className?: string;
}

/** Underlined tab bar (page-level navigation between panels). */
export function Tabs<T extends string>({ items, value, onChange, className }: TabsProps<T>) {
  return (
    // The bottom rule is an inset shadow (not a border): the tabs sit exactly on it without
    // a negative margin, so nothing overflows vertically and `overflow-x-auto` (which implies
    // `overflow-y: auto`) never shows a stray vertical scrollbar.
    <div
      role="tablist"
      className={clsx(
        "no-drag flex items-center gap-0.5 overflow-x-auto overflow-y-hidden shadow-[inset_0_-1px_0_0_var(--color-border)]",
        className,
      )}
    >
      {items.map((item) => {
        const active = item.id === value;
        const Icon = item.icon;
        return (
          <button
            key={item.id}
            role="tab"
            type="button"
            aria-selected={active}
            disabled={item.disabled}
            onClick={() => onChange(item.id)}
            className={clsx(
              "relative inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap px-2 text-[13.5px] transition-colors",
              "border-b-2 disabled:opacity-40",
              active ? "border-accent font-medium text-fg" : "border-transparent text-muted hover:text-fg",
            )}
          >
            {Icon && <Icon size={15} strokeWidth={1.75} aria-hidden />}
            {item.label}
            {item.count != null && (
              <span className={clsx("tabular rounded-full px-1.5 text-[11px]", active ? "bg-accent-soft text-accent" : "bg-surface-2 text-muted")}>
                {item.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export interface SegmentedProps<T extends string> {
  options: { value: T; label: ReactNode; icon?: LucideIcon; title?: string }[];
  value: T;
  onChange: (value: T) => void;
  size?: "sm" | "md";
  className?: string;
}

/** Compact pill switcher for small option sets (e.g. "linéaire / log"). */
export function Segmented<T extends string>({ options, value, onChange, size = "sm", className }: SegmentedProps<T>) {
  return (
    <div
      role="radiogroup"
      className={clsx("no-drag inline-flex items-center gap-0.5 rounded-[10px] border border-border bg-surface-2 p-0.5", className)}
    >
      {options.map((o) => {
        const active = o.value === value;
        const Icon = o.icon;
        return (
          <button
            key={o.value}
            type="button"
            role="radio"
            aria-checked={active}
            title={o.title}
            onClick={() => onChange(o.value)}
            className={clsx(
              "inline-flex items-center gap-1.5 whitespace-nowrap rounded-lg font-medium transition-colors",
              size === "sm" ? "h-6 px-2 text-[12.5px]" : "h-8 px-3 text-[13.5px]",
              active ? "bg-surface text-fg shadow-sm" : "text-muted hover:text-fg",
            )}
          >
            {Icon && <Icon size={size === "sm" ? 13 : 15} strokeWidth={1.75} aria-hidden />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
