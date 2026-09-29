import clsx from "clsx";
import type { ReactNode } from "react";
import { Link } from "react-router";
import { ChevronLeft } from "lucide-react";

export interface PageProps {
  children: ReactNode;
  /** "default" = 1200 px, "wide" = 1480 px, "full" = no max width. */
  width?: "default" | "wide" | "full";
  className?: string;
}

/** Centered page container with the standard gutters. */
export function Page({ children, width = "default", className }: PageProps) {
  return (
    <div
      className={clsx(
        "mx-auto w-full px-10 pb-16 pt-8",
        width === "default" && "max-w-[1200px]",
        width === "wide" && "max-w-[1480px]",
        className,
      )}
    >
      {children}
    </div>
  );
}

export interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  /** Small line above the title (e.g. project name). */
  eyebrow?: ReactNode;
  /** Back link displayed above the title. */
  back?: { to: string; label: string };
  actions?: ReactNode;
  /** Metadata row below the description (badges, links…). */
  meta?: ReactNode;
  className?: string;
}

/** Page title in serif with optional description, actions and metadata. */
export function PageHeader({ title, description, eyebrow, back, actions, meta, className }: PageHeaderProps) {
  return (
    <header className={clsx("mb-7", className)}>
      {back && (
        <Link
          to={back.to}
          className="no-drag mb-3 inline-flex items-center gap-1 text-[13px] text-muted transition-colors hover:text-fg"
        >
          <ChevronLeft size={14} strokeWidth={1.75} aria-hidden />
          {back.label}
        </Link>
      )}
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0 flex-1">
          {eyebrow && <div className="mb-1 text-[13px] text-muted">{eyebrow}</div>}
          <h1 className="selectable font-serif text-[30px] font-normal leading-[1.15] tracking-[-0.01em] text-fg">{title}</h1>
          {description && <p className="mt-2 max-w-[720px] text-[14.5px] leading-relaxed text-muted">{description}</p>}
        </div>
        {actions && <div className="no-drag flex shrink-0 flex-wrap items-center gap-2 pt-1">{actions}</div>}
      </div>
      {meta && <div className="mt-3.5 flex flex-wrap items-center gap-x-4 gap-y-2 text-[13px] text-muted">{meta}</div>}
    </header>
  );
}

export interface SectionProps {
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}

/** Titled block inside a page. */
export function Section({ title, description, actions, children, className }: SectionProps) {
  return (
    <section className={clsx("mt-9 first:mt-0", className)}>
      {(title || actions) && (
        <div className="mb-3.5 flex items-end justify-between gap-4">
          <div className="min-w-0">
            {title && <h2 className="font-serif text-[19px] leading-tight text-fg">{title}</h2>}
            {description && <p className="mt-1 text-[13px] text-muted">{description}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}
