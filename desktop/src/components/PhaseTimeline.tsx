import clsx from "clsx";
import { useMemo, useRef, useState, type MouseEvent } from "react";
import { formatCompact, formatInt, formatNumber, formatPercent } from "../lib/format";
import type { PhasePlanPreview, PhasePreview } from "../lib/types";

/**
 * Curriculum visualisation: proportional phase bar (names, steps, progress),
 * aligned mini-curves (LR factor, attack strength, loss weights) and the
 * attacks of each phase. Hovering shows the values at any step.
 */

export interface PhaseTimelineProps {
  plan: PhasePlanPreview;
  /** Current global step (draws the progress marker). */
  currentStep?: number | null;
  /** Show the aligned curves (default true). */
  curves?: boolean;
  /** Show the attack list per phase (default true). */
  attacks?: boolean;
  className?: string;
}

type Pt = [number, number];

const phaseColor = (i: number) => `var(--chart-${(i % 6) + 1})`;
const tint = (i: number, pct: number) => `color-mix(in srgb, ${phaseColor(i)} ${pct}%, transparent)`;

/** Linear interpolation of a [step, value] series at `step`. */
function valueAt(series: Pt[], step: number): number | null {
  if (!series.length) return null;
  if (step <= series[0][0]) return series[0][1];
  for (let i = 1; i < series.length; i++) {
    const [x1, y1] = series[i];
    if (step <= x1) {
      const [x0, y0] = series[i - 1];
      return x1 === x0 ? y1 : y0 + ((y1 - y0) * (step - x0)) / (x1 - x0);
    }
  }
  return series[series.length - 1][1];
}

interface CurveRow {
  id: string;
  label: string;
  lines: { name: string; color: string; points: Pt[] }[];
  format: (v: number) => string;
}

function buildRows(phases: PhasePreview[]): CurveRow[] {
  const concat = (pick: (p: PhasePreview) => Pt[] | undefined) =>
    phases.flatMap((p) => pick(p) ?? []).sort((a, b) => a[0] - b[0]);
  const rows: CurveRow[] = [
    {
      id: "lr",
      label: "Facteur de LR",
      lines: [{ name: "LR", color: "var(--chart-2)", points: concat((p) => p.lr) }],
      format: (v) => `×${formatNumber(v, { maxDigits: 3 })}`,
    },
    {
      id: "strength",
      label: "Force d'attaque",
      lines: [{ name: "Force", color: "var(--chart-1)", points: concat((p) => p.attack_strength) }],
      format: (v) => formatPercent(v, 0),
    },
  ];
  const terms = [...new Set(phases.flatMap((p) => Object.keys(p.loss_weights ?? {})))].sort();
  if (terms.length) {
    rows.push({
      id: "weights",
      label: "Poids des pertes",
      lines: terms.map((term, i) => ({
        name: term,
        color: `var(--chart-${((i + 2) % 6) + 1})`,
        points: concat((p) => p.loss_weights?.[term]),
      })),
      format: (v) => formatNumber(v, { maxDigits: 3 }),
    });
  }
  return rows.filter((r) => r.lines.some((l) => l.points.length > 0));
}

const VIEW_W = 1000;
const VIEW_H = 100;

function Curve({ row, total }: { row: CurveRow; total: number }) {
  const all = row.lines.flatMap((l) => l.points.map((p) => p[1]));
  const max = Math.max(1e-9, ...all);
  const min = Math.min(0, ...all);
  const span = max - min || 1;
  const x = (s: number) => (s / total) * VIEW_W;
  const y = (v: number) => VIEW_H - 6 - ((v - min) / span) * (VIEW_H - 14);
  return (
    <svg viewBox={`0 0 ${VIEW_W} ${VIEW_H}`} preserveAspectRatio="none" className="absolute inset-0 h-full w-full overflow-visible">
      {row.lines.map((l) =>
        l.points.length ? (
          <g key={l.name}>
            <path
              d={`M${x(l.points[0][0])},${VIEW_H} ${l.points.map((p) => `L${x(p[0]).toFixed(2)},${y(p[1]).toFixed(2)}`).join(" ")} L${x(l.points[l.points.length - 1][0])},${VIEW_H} Z`}
              fill={`color-mix(in srgb, ${l.color} ${row.lines.length > 1 ? 6 : 10}%, transparent)`}
              stroke="none"
            />
            <polyline
              points={l.points.map((p) => `${x(p[0]).toFixed(2)},${y(p[1]).toFixed(2)}`).join(" ")}
              fill="none"
              stroke={l.color}
              strokeWidth={1.75}
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
          </g>
        ) : null,
      )}
    </svg>
  );
}

/** The curriculum visualisation. */
export function PhaseTimeline({ plan, currentStep, curves = true, attacks = true, className }: PhaseTimelineProps) {
  const total = Math.max(1, plan.total_steps);
  const phases = plan.phases;
  const rows = useMemo(() => (curves ? buildRows(phases) : []), [phases, curves]);
  const trackRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{ step: number; frac: number } | null>(null);

  const step = currentStep != null && Number.isFinite(currentStep) ? Math.max(0, Math.min(total, currentStep)) : null;
  const activeIndex = step == null ? -1 : phases.findIndex((p) => step >= p.start && step < p.end);

  const onMove = (e: MouseEvent<HTMLDivElement>) => {
    const el = trackRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const frac = (e.clientX - r.left) / r.width;
    if (frac < 0 || frac > 1) {
      setHover(null);
      return;
    }
    setHover({ frac, step: Math.round(frac * total) });
  };

  if (!phases.length) {
    return <div className={clsx("text-[13px] text-muted", className)}>Aucune phase définie.</div>;
  }

  const hoverPhase = hover ? (phases.find((p) => hover.step >= p.start && hover.step < p.end) ?? phases[phases.length - 1]) : null;
  const LABEL_W = "w-[118px]";

  return (
    <div className={clsx("select-none", className)} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
      {/* Phase bar */}
      <div className="flex items-stretch gap-3">
        <div className={clsx(LABEL_W, "shrink-0 pt-2 text-[12px] text-muted")}>Phases</div>
        <div ref={trackRef} className="relative min-w-0 flex-1">
          <div className="flex h-[46px] gap-[2px]">
            {phases.map((p, i) => {
              const done = step == null ? 0 : Math.max(0, Math.min(1, (step - p.start) / Math.max(1, p.end - p.start)));
              const active = i === activeIndex;
              return (
                <div
                  key={`${p.name}-${i}`}
                  className={clsx(
                    "relative min-w-[28px] overflow-hidden px-2.5 py-1.5",
                    i === 0 && "rounded-l-[10px]",
                    i === phases.length - 1 && "rounded-r-[10px]",
                  )}
                  style={{ flexGrow: Math.max(1, p.steps), flexBasis: 0, background: tint(i, 13) }}
                  title={`${p.name} — ${formatInt(p.steps)} steps (${formatInt(p.start)} → ${formatInt(p.end)})`}
                >
                  {done > 0 && (
                    <div className="absolute inset-y-0 left-0" style={{ width: `${done * 100}%`, background: tint(i, 26) }} aria-hidden />
                  )}
                  <div className="absolute inset-x-0 top-0 h-[3px]" style={{ background: phaseColor(i) }} aria-hidden />
                  <div className="relative truncate text-[12.5px] leading-tight text-fg">
                    <span className={clsx(active && "font-semibold")}>{p.name}</span>
                  </div>
                  <div className="relative truncate text-[11px] leading-tight text-muted">
                    {formatCompact(p.steps)} steps{active ? " · en cours" : ""}
                  </div>
                </div>
              );
            })}
          </div>
          {/* Boundary ticks */}
          <div className="relative mt-1 h-4 text-[10.5px] text-faint">
            <span className="absolute left-0">0</span>
            {phases.map((p, i) => (
              <span
                key={i}
                className={clsx("absolute", i === phases.length - 1 ? "right-0" : "-translate-x-1/2")}
                style={i === phases.length - 1 ? undefined : { left: `${(p.end / total) * 100}%` }}
              >
                {formatCompact(p.end)}
              </span>
            ))}
          </div>
          {/* Progress marker across the bar */}
          {step != null && (
            <div
              className="pointer-events-none absolute -bottom-0.5 top-[-4px] w-[2px] -translate-x-1/2 rounded-full bg-accent"
              style={{ left: `${(step / total) * 100}%` }}
              aria-hidden
            />
          )}
          {hover && <div className="pointer-events-none absolute inset-y-0 w-px bg-fg/30" style={{ left: `${hover.frac * 100}%` }} aria-hidden />}
        </div>
      </div>

      {/* Curves */}
      {rows.map((row) => (
        <div key={row.id} className="mt-2.5 flex items-stretch gap-3">
          <div className={clsx(LABEL_W, "shrink-0 pt-1")}>
            <div className="text-[12px] text-muted">{row.label}</div>
            {row.lines.length > 1 && (
              <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5">
                {row.lines.map((l) => (
                  <span key={l.name} className="inline-flex items-center gap-1 text-[10.5px] text-muted">
                    <span className="inline-block h-[3px] w-2.5 rounded-full" style={{ background: l.color }} />
                    {l.name}
                  </span>
                ))}
              </div>
            )}
          </div>
          <div className="relative h-[52px] min-w-0 flex-1 rounded-lg bg-surface-2/60">
            {phases.slice(0, -1).map((p, i) => (
              <div key={i} className="absolute inset-y-0 w-px bg-border" style={{ left: `${(p.end / total) * 100}%` }} aria-hidden />
            ))}
            <Curve row={row} total={total} />
            {step != null && (
              <div className="pointer-events-none absolute inset-y-0 w-[2px] -translate-x-1/2 bg-accent/70" style={{ left: `${(step / total) * 100}%` }} />
            )}
            {hover && <div className="pointer-events-none absolute inset-y-0 w-px bg-fg/30" style={{ left: `${hover.frac * 100}%` }} />}
          </div>
        </div>
      ))}

      {/* Attacks */}
      {attacks && (
        <div className="mt-2.5 flex items-stretch gap-3">
          <div className={clsx(LABEL_W, "shrink-0 pt-1 text-[12px] text-muted")}>Attaques</div>
          <div className="flex min-w-0 flex-1 gap-[2px]">
            {phases.map((p, i) => (
              <div
                key={`${p.name}-${i}`}
                className="min-w-[28px] overflow-hidden px-1.5"
                style={{ flexGrow: Math.max(1, p.steps), flexBasis: 0 }}
                title={p.attacks.length ? p.attacks.join(", ") : "Identité (aucune attaque)"}
              >
                <div className="flex flex-wrap gap-1">
                  {p.attacks.length === 0 ? (
                    <span className="truncate text-[11.5px] italic text-faint">identité</span>
                  ) : (
                    p.attacks.map((a, k) => (
                      <span
                        key={`${a}-${k}`}
                        className="max-w-full truncate rounded-md border border-border bg-surface px-1.5 py-px font-mono text-[10.5px] text-fg"
                      >
                        {a}
                      </span>
                    ))
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Hover read-out */}
      <div className="mt-3 flex h-5 items-center gap-4 pl-[130px] text-[12px] text-muted">
        {hover && hoverPhase ? (
          <>
            <span className="text-fg">
              Step <span className="tabular">{formatInt(hover.step)}</span>
            </span>
            <span>{hoverPhase.name}</span>
            {rows.map((row) =>
              row.lines.map((l) => {
                const v = valueAt(l.points, hover.step);
                return v == null ? null : (
                  <span key={`${row.id}-${l.name}`} className="inline-flex items-center gap-1">
                    <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: l.color }} />
                    {row.lines.length > 1 ? l.name : row.label} <span className="tabular text-fg">{row.format(v)}</span>
                  </span>
                );
              }),
            )}
          </>
        ) : step != null ? (
          <span>
            Progression : <span className="tabular text-fg">{formatInt(step)}</span> / {formatInt(total)} steps (
            {formatPercent(step / total, 0)})
          </span>
        ) : (
          <span>
            {formatInt(total)} steps au total · {phases.length} phase{phases.length > 1 ? "s" : ""} — survolez pour lire les valeurs
          </span>
        )}
      </div>
    </div>
  );
}
