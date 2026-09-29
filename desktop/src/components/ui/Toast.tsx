import clsx from "clsx";
import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { CircleAlert, CircleCheck, Info, TriangleAlert, X } from "lucide-react";

export type ToastTone = "info" | "success" | "warning" | "error";

export interface ToastOptions {
  title: ReactNode;
  description?: ReactNode;
  tone?: ToastTone;
  /** Display duration in ms (default 4500, errors 7000). 0 = sticky. */
  duration?: number;
  action?: { label: string; onClick: () => void };
}

interface ToastItem extends ToastOptions {
  id: number;
}

export interface ToastApi {
  toast: (opts: ToastOptions) => number;
  success: (title: ReactNode, description?: ReactNode) => number;
  error: (title: ReactNode, description?: ReactNode) => number;
  info: (title: ReactNode, description?: ReactNode) => number;
  warning: (title: ReactNode, description?: ReactNode) => number;
  dismiss: (id: number) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

const ICONS = { info: Info, success: CircleCheck, warning: TriangleAlert, error: CircleAlert };
const ICON_COLORS = { info: "text-info", success: "text-success", warning: "text-warning", error: "text-danger" };

/** Provides `useToast()`; renders notifications bottom-right. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const dismiss = useCallback((id: number) => setItems((list) => list.filter((t) => t.id !== id)), []);

  const toast = useCallback(
    (opts: ToastOptions) => {
      const id = ++seq.current;
      const tone = opts.tone ?? "info";
      setItems((list) => [...list.slice(-4), { ...opts, tone, id }]);
      const duration = opts.duration ?? (tone === "error" ? 7000 : 4500);
      if (duration > 0) window.setTimeout(() => dismiss(id), duration);
      return id;
    },
    [dismiss],
  );

  const api = useMemo<ToastApi>(
    () => ({
      toast,
      dismiss,
      success: (title, description) => toast({ title, description, tone: "success" }),
      error: (title, description) => toast({ title, description, tone: "error" }),
      info: (title, description) => toast({ title, description, tone: "info" }),
      warning: (title, description) => toast({ title, description, tone: "warning" }),
    }),
    [toast, dismiss],
  );

  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <div className="pointer-events-none fixed bottom-5 right-5 z-[90] flex w-[360px] max-w-[calc(100vw-40px)] flex-col gap-2">
          {items.map((t) => {
            const tone = t.tone ?? "info";
            const Icon = ICONS[tone];
            return (
              <div
                key={t.id}
                role={tone === "error" ? "alert" : "status"}
                className="no-drag pointer-events-auto flex animate-pop-in items-start gap-3 rounded-xl border border-border bg-surface px-4 py-3 shadow-lg"
              >
                <Icon size={17} strokeWidth={1.75} className={clsx("mt-0.5 shrink-0", ICON_COLORS[tone])} aria-hidden />
                <div className="min-w-0 flex-1">
                  <div className="text-[13.5px] font-medium text-fg">{t.title}</div>
                  {t.description && <div className="mt-0.5 break-words text-[12.5px] text-muted">{t.description}</div>}
                  {t.action && (
                    <button
                      type="button"
                      className="mt-1.5 text-[12.5px] font-medium text-accent hover:underline"
                      onClick={() => {
                        t.action!.onClick();
                        dismiss(t.id);
                      }}
                    >
                      {t.action.label}
                    </button>
                  )}
                </div>
                <button
                  type="button"
                  aria-label="Fermer"
                  className="-mr-1 rounded-md p-0.5 text-faint hover:bg-hover hover:text-fg"
                  onClick={() => dismiss(t.id)}
                >
                  <X size={14} strokeWidth={1.75} />
                </button>
              </div>
            );
          })}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

/** Access notifications: `const toast = useToast(); toast.success("Enregistré")`. */
export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast doit être utilisé dans <ToastProvider>");
  return ctx;
}
