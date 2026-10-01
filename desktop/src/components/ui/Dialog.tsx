import clsx from "clsx";
import { useEffect, useId, useRef, type ReactNode, type RefObject } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import { IconButton } from "./Button";

/** Stack of open overlays so that Escape only closes the top-most one. */
const overlayStack: number[] = [];
let overlaySeq = 0;

const FOCUSABLE =
  "a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])";

/** Keyboard handling shared by Dialog and Drawer: Escape closes, Tab stays inside the panel. */
function useOverlay(open: boolean, onClose: () => void, panelRef: RefObject<HTMLElement | null>) {
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const id = ++overlaySeq;
    overlayStack.push(id);
    const previous = document.activeElement as HTMLElement | null;
    // Focus the first focusable element (autoFocus inputs win because they focus earlier).
    const raf = requestAnimationFrame(() => {
      const panel = panelRef.current;
      if (panel && !panel.contains(document.activeElement)) {
        const target = panel.querySelector<HTMLElement>(
          "[data-autofocus], input:not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled])",
        );
        (target ?? panel).focus();
      }
    });
    const onKey = (e: KeyboardEvent) => {
      if (overlayStack[overlayStack.length - 1] !== id) return;
      if (e.key === "Escape") {
        e.stopPropagation();
        onCloseRef.current();
        return;
      }
      // Simple focus trap: Tab / Shift+Tab cycle inside the top-most overlay.
      if (e.key === "Tab") {
        const panel = panelRef.current;
        if (!panel) return;
        const focusable = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === panel);
        if (!focusable.length) {
          e.preventDefault();
          panel.focus();
          return;
        }
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const current = document.activeElement as HTMLElement | null;
        if (e.shiftKey && (current === first || !panel.contains(current))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && (current === last || !panel.contains(current))) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("keydown", onKey);
      const idx = overlayStack.indexOf(id);
      if (idx >= 0) overlayStack.splice(idx, 1);
      previous?.focus?.();
    };
  }, [open, panelRef]);
}

const SIZES = {
  sm: "max-w-[420px]",
  md: "max-w-[560px]",
  lg: "max-w-[760px]",
  xl: "max-w-[1040px]",
} as const;

export interface DialogProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  /** Footer (usually right-aligned buttons). */
  footer?: ReactNode;
  size?: keyof typeof SIZES;
  /** Close when clicking the backdrop (default true). */
  closeOnOverlay?: boolean;
  className?: string;
  bodyClassName?: string;
}

/** Centered modal dialog (portal, Escape to close, backdrop click). */
export function Dialog({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  closeOnOverlay = true,
  className,
  bodyClassName,
}: DialogProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  useOverlay(open, onClose, panelRef);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto px-4 pb-10 pt-[9vh]">
      <div
        className="fixed inset-0 animate-fade-in bg-[var(--overlay)] backdrop-blur-[2px]"
        onMouseDown={closeOnOverlay ? onClose : undefined}
        aria-hidden
      />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        className={clsx(
          "no-drag relative flex w-full animate-pop-in flex-col rounded-2xl border border-border bg-surface shadow-lg outline-none",
          "max-h-[84vh]",
          SIZES[size],
          className,
        )}
      >
        {(title || description) && (
          <div className="flex items-start justify-between gap-4 px-6 pb-2 pt-5">
            <div className="min-w-0">
              {title && (
                <h2 id={titleId} className="font-serif text-[20px] leading-tight text-fg">
                  {title}
                </h2>
              )}
              {description && (
                <p id={descriptionId} className="mt-1 text-[13.5px] text-muted">
                  {description}
                </p>
              )}
            </div>
            <IconButton icon={X} label="Fermer" size="sm" onClick={onClose} tooltip={false} className="-mr-2 -mt-1" />
          </div>
        )}
        <div className={clsx("min-h-0 flex-1 overflow-y-auto px-6 py-4", bodyClassName)}>{children}</div>
        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-border px-6 py-3.5">{footer}</div>
        )}
      </div>
    </div>,
    document.body,
  );
}

/** Alias kept for readability in pages. */
export const Modal = Dialog;

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  /** Width in px (default 480). */
  width?: number;
  className?: string;
}

/** Right side panel. */
export function Drawer({ open, onClose, title, description, children, footer, width = 480, className }: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  useOverlay(open, onClose, panelRef);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-50">
      <div className="absolute inset-0 animate-fade-in bg-[var(--overlay)]" onMouseDown={onClose} aria-hidden />
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={title ? titleId : undefined}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        style={{ width: `min(${width}px, 92vw)` }}
        className={clsx(
          "no-drag absolute bottom-0 right-0 top-0 flex animate-slide-in flex-col border-l border-border bg-surface shadow-lg outline-none",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-4 border-b border-border px-5 pb-3.5 pt-4">
          <div className="min-w-0">
            {title && (
              <h2 id={titleId} className="font-serif text-[19px] leading-tight text-fg">
                {title}
              </h2>
            )}
            {description && (
              <p id={descriptionId} className="mt-1 text-[13px] text-muted">
                {description}
              </p>
            )}
          </div>
          <IconButton icon={X} label="Fermer" size="sm" onClick={onClose} tooltip={false} className="-mr-1.5" />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex items-center justify-end gap-2 border-t border-border px-5 py-3">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
