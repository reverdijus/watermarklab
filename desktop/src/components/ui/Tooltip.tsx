import clsx from "clsx";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

export type TooltipSide = "top" | "bottom" | "left" | "right";

export interface TooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: TooltipSide;
  /** Delay before showing (ms). */
  delay?: number;
  className?: string;
  /** Extra classes on the inline wrapper. */
  wrapperClassName?: string;
  disabled?: boolean;
}

const GAP = 6;
const MARGIN = 8;

/** Lightweight hover/focus tooltip rendered in a portal (never clipped). */
export function Tooltip({ content, children, side = "top", delay = 300, className, wrapperClassName, disabled }: TooltipProps) {
  const anchorRef = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const timer = useRef<number | undefined>(undefined);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);

  const show = () => {
    if (disabled || content == null || content === "") return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setOpen(true), delay);
  };
  const hide = () => {
    window.clearTimeout(timer.current);
    setOpen(false);
    setPos(null);
  };

  useLayoutEffect(() => {
    if (!open || !anchorRef.current || !tipRef.current) return;
    const a = anchorRef.current.getBoundingClientRect();
    const t = tipRef.current.getBoundingClientRect();
    let left = 0;
    let top = 0;
    if (side === "top" || side === "bottom") {
      left = a.left + a.width / 2 - t.width / 2;
      top = side === "top" ? a.top - t.height - GAP : a.bottom + GAP;
      if (side === "top" && top < MARGIN) top = a.bottom + GAP;
      if (side === "bottom" && top + t.height > window.innerHeight - MARGIN) top = a.top - t.height - GAP;
    } else {
      top = a.top + a.height / 2 - t.height / 2;
      left = side === "left" ? a.left - t.width - GAP : a.right + GAP;
    }
    left = Math.max(MARGIN, Math.min(left, window.innerWidth - t.width - MARGIN));
    top = Math.max(MARGIN, Math.min(top, window.innerHeight - t.height - MARGIN));
    setPos({ left, top });
  }, [open, side, content]);

  return (
    <span
      ref={anchorRef}
      className={clsx("inline-flex", wrapperClassName)}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
      onMouseDown={hide}
    >
      {children}
      {open &&
        createPortal(
          <div
            ref={tipRef}
            role="tooltip"
            style={{ left: pos?.left ?? -9999, top: pos?.top ?? -9999 }}
            className={clsx(
              "pointer-events-none fixed z-[100] max-w-[300px] rounded-lg border border-border bg-surface px-2.5 py-1.5",
              "text-[12px] leading-snug text-fg shadow-md",
              pos && "animate-fade-in",
              className,
            )}
          >
            {content}
          </div>,
          document.body,
        )}
    </span>
  );
}
