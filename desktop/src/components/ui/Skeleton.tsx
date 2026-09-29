import clsx from "clsx";

/** Pulsing placeholder block. */
export function Skeleton({ className, lines }: { className?: string; lines?: number }) {
  if (lines && lines > 1) {
    return (
      <div className={clsx("space-y-2", className)}>
        {Array.from({ length: lines }).map((_, i) => (
          <div key={i} className="h-3.5 animate-pulse rounded-md bg-surface-2" style={{ width: `${90 - ((i * 17) % 40)}%` }} />
        ))}
      </div>
    );
  }
  return <div className={clsx("animate-pulse rounded-md bg-surface-2", className ?? "h-4 w-full")} />;
}

/** Full page placeholder used as Suspense fallback. */
export function PageSkeleton() {
  return (
    <div className="mx-auto w-full max-w-[1200px] px-10 pt-10">
      <Skeleton className="h-8 w-72" />
      <Skeleton className="mt-3 h-4 w-96" />
      <div className="mt-8 grid grid-cols-4 gap-4">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-24 rounded-xl" />
        ))}
      </div>
      <Skeleton className="mt-6 h-64 rounded-xl" />
    </div>
  );
}
