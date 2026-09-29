import clsx from "clsx";
import { LoaderCircle } from "lucide-react";

/** Small rotating loader. */
export function Spinner({ size = 16, className, label }: { size?: number; className?: string; label?: string }) {
  return (
    <span role="status" aria-label={label ?? "Chargement"} className={clsx("inline-flex text-muted", className)}>
      <LoaderCircle size={size} strokeWidth={1.75} className="animate-spin" aria-hidden />
    </span>
  );
}

/** Centered spinner with an optional caption, for panels being loaded. */
export function LoadingBlock({ label = "Chargement…", className }: { label?: string; className?: string }) {
  return (
    <div className={clsx("flex items-center justify-center gap-2 py-12 text-[13px] text-muted", className)}>
      <Spinner size={16} />
      <span>{label}</span>
    </div>
  );
}
