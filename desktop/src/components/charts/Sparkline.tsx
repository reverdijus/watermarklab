import { useId } from "react";

export interface SparklineProps {
  values: (number | null)[];
  width?: number;
  height?: number;
  /** CSS color (default accent). */
  color?: string;
  area?: boolean;
  /** Dot on the last value. */
  showLast?: boolean;
  min?: number;
  max?: number;
  className?: string;
  title?: string;
}

/** Tiny SVG trend line (cheap enough for table cells). */
export function Sparkline({
  values,
  width = 96,
  height = 24,
  color = "var(--accent)",
  area = true,
  showLast = true,
  min,
  max,
  className,
  title,
}: SparklineProps) {
  const gradientId = useId();
  const pts = values
    .map((v, i) => ({ i, v }))
    .filter((p): p is { i: number; v: number } => p.v !== null && Number.isFinite(p.v));
  if (pts.length < 2) {
    return <svg width={width} height={height} className={className} aria-hidden />;
  }
  const lo = min ?? Math.min(...pts.map((p) => p.v));
  const hi = max ?? Math.max(...pts.map((p) => p.v));
  const span = hi - lo || 1;
  const pad = 2;
  const n = values.length - 1 || 1;
  const x = (i: number) => pad + (i / n) * (width - 2 * pad);
  const y = (v: number) => height - pad - ((v - lo) / span) * (height - 2 * pad);
  const d = pts.map((p, k) => `${k === 0 ? "M" : "L"}${x(p.i).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <svg width={width} height={height} className={className} role="img" aria-label={title}>
      {title && <title>{title}</title>}
      {area && (
        <>
          <defs>
            <linearGradient id={gradientId} x1="0" x2="0" y1="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.18} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <path
            d={`${d} L${x(last.i).toFixed(1)},${height - pad} L${x(pts[0].i).toFixed(1)},${height - pad} Z`}
            fill={`url(#${CSS.escape(gradientId)})`}
          />
        </>
      )}
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      {showLast && <circle cx={x(last.i)} cy={y(last.v)} r={2.2} fill={color} />}
    </svg>
  );
}
