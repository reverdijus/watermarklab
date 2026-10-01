import clsx from "clsx";
import type { HTMLAttributes, KeyboardEvent, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";

const PADDINGS = { none: "", sm: "p-3", md: "p-5", lg: "p-6" } as const;

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  padding?: keyof typeof PADDINGS;
  /** Hover affordance for clickable cards. */
  interactive?: boolean;
  /** Softer variant on surface-2, without shadow. */
  muted?: boolean;
}

/** Rounded surface with a thin border and a very light shadow. */
export function Card({ padding = "md", interactive, muted, className, children, onClick, onKeyDown, ...rest }: CardProps) {
  // A clickable card is reachable from the keyboard (Enter / Space) like a button.
  const clickable = interactive && !!onClick;
  const handleKey = clickable
    ? (e: KeyboardEvent<HTMLDivElement>) => {
        onKeyDown?.(e);
        if (e.defaultPrevented || e.target !== e.currentTarget) return;
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.currentTarget.click();
        }
      }
    : onKeyDown;
  return (
    <div
      {...rest}
      role={clickable ? "button" : rest.role}
      tabIndex={clickable ? (rest.tabIndex ?? 0) : rest.tabIndex}
      onClick={onClick}
      onKeyDown={handleKey}
      className={clsx(
        "rounded-xl border border-border",
        muted ? "bg-surface-2" : "bg-surface shadow-sm",
        PADDINGS[padding],
        interactive && "cursor-pointer transition-[border-color,box-shadow] duration-150 hover:border-border-strong hover:shadow-md",
        clickable && "no-drag focus-visible:border-border-strong",
        className,
      )}
    >
      {children}
    </div>
  );
}

export interface CardHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  icon?: LucideIcon;
  actions?: ReactNode;
  className?: string;
}

/** Title row for a Card (title, optional icon/description, right-aligned actions). */
export function CardHeader({ title, description, icon: Icon, actions, className }: CardHeaderProps) {
  return (
    <div className={clsx("mb-4 flex items-start justify-between gap-3", className)}>
      <div className="flex min-w-0 items-start gap-2.5">
        {Icon && <Icon size={16} strokeWidth={1.75} className="mt-0.5 shrink-0 text-muted" aria-hidden />}
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold text-fg">{title}</div>
          {description && <div className="mt-0.5 text-[13px] text-muted">{description}</div>}
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}
