import clsx from "clsx";
import { useState, type ReactNode } from "react";
import { Check, Copy } from "lucide-react";

export interface CodeBlockProps {
  code: string;
  /** Title bar text (file name, language…). */
  title?: ReactNode;
  maxHeight?: number | string;
  /** Soft-wrap long lines (default false: horizontal scroll). */
  wrap?: boolean;
  copy?: boolean;
  className?: string;
  /** Rendered instead of the plain code (e.g. highlighted lines). */
  children?: ReactNode;
}

/** Copy text to the clipboard (returns success). */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/** Monospace block with a copy button. */
export function CodeBlock({ code, title, maxHeight = 420, wrap, copy = true, className, children }: CodeBlockProps) {
  const [copied, setCopied] = useState(false);
  const onCopy = async () => {
    if (await copyText(code)) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <div className={clsx("group relative overflow-hidden rounded-xl border border-border bg-surface-2", className)}>
      {title && (
        <div className="flex items-center justify-between border-b border-border px-3.5 py-2 text-[12px] text-muted">
          <span className="truncate font-mono">{title}</span>
        </div>
      )}
      {copy && (
        <button
          type="button"
          onClick={onCopy}
          aria-label="Copier"
          className={clsx(
            "no-drag absolute right-2 z-[1] inline-flex h-7 items-center gap-1 rounded-md border border-border bg-surface px-2 text-[12px] text-muted",
            "opacity-0 shadow-sm transition-opacity hover:text-fg group-hover:opacity-100",
            title ? "top-10" : "top-2",
          )}
        >
          {copied ? <Check size={13} strokeWidth={2} /> : <Copy size={13} strokeWidth={1.75} />}
          {copied ? "Copié" : "Copier"}
        </button>
      )}
      <pre
        className={clsx(
          "selectable overflow-auto px-4 py-3 font-mono text-[12.5px] leading-[1.6] text-fg",
          wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre",
        )}
        style={{ maxHeight }}
      >
        {children ?? code}
      </pre>
    </div>
  );
}
