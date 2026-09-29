import { useMemo, type ReactNode } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "./EChart";
import { axisBase, escapeHtml, legendBase, seriesColor, tooltipBase, tooltipDot, useChartTokens, withAlpha } from "./theme";
import { paretoFront, satisfies, type Constraint } from "../../lib/metrics";
import { formatSignificant } from "../../lib/format";
import type { Direction } from "../../lib/types";

export interface ParetoPoint {
  id: string;
  x: number;
  y: number;
  /** Title of the tooltip (defaults to id). */
  label?: string;
  /** Series / color group (e.g. project or experiment). */
  group?: string;
  /** Extra rows displayed in the tooltip. */
  meta?: Record<string, string | number | null | undefined>;
}

export interface ScatterParetoProps {
  points: ParetoPoint[];
  xLabel: string;
  yLabel: string;
  xDirection?: Direction;
  yDirection?: Direction;
  xFormatter?: (v: number) => string;
  yFormatter?: (v: number) => string;
  /** Constraints: infeasible points are faded and a guide line is drawn. */
  xConstraint?: Constraint | null;
  yConstraint?: Constraint | null;
  /** Point to emphasise (e.g. current run). */
  highlightId?: string | null;
  onPointClick?: (point: ParetoPoint) => void;
  /** Draw the Pareto front (default true). */
  showFront?: boolean;
  height?: number;
  loading?: boolean;
  empty?: ReactNode;
  className?: string;
}

const opSymbol = (op: string) => (op === ">=" ? "≥" : op === "<=" ? "≤" : op);

/** Quality × robustness scatter with client-side Pareto front and constraint guides. */
export function ScatterPareto({
  points,
  xLabel,
  yLabel,
  xDirection = "maximize",
  yDirection = "maximize",
  xFormatter,
  yFormatter,
  xConstraint,
  yConstraint,
  highlightId,
  onPointClick,
  showFront = true,
  height = 380,
  loading,
  empty,
  className,
}: ScatterParetoProps) {
  const t = useChartTokens();
  const valid = useMemo(() => points.filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y)), [points]);

  const option = useMemo<EChartsOption>(() => {
    const ax = axisBase(t);
    const fx = xFormatter ?? ((v: number) => formatSignificant(v, 4));
    const fy = yFormatter ?? ((v: number) => formatSignificant(v, 4));
    const feasible = (p: ParetoPoint) =>
      (!xConstraint || satisfies(p.x, xConstraint)) && (!yConstraint || satisfies(p.y, yConstraint));
    const frontIdx = showFront ? paretoFront(valid.filter(feasible), xDirection, yDirection) : [];
    const feasiblePts = valid.filter(feasible);
    const frontPts = frontIdx.map((i) => feasiblePts[i]);
    const frontIds = new Set(frontPts.map((p) => p.id));

    // Stable group → color mapping (alphabetical, so colors follow entities).
    const groups = [...new Set(valid.map((p) => p.group ?? "Runs"))].sort((a, b) => a.localeCompare(b, "fr"));
    const colorOf = new Map(groups.map((g, i) => [g, seriesColor(t, i)]));

    const toDatum = (p: ParetoPoint) => {
      const ok = feasible(p);
      const onFront = frontIds.has(p.id);
      const isHi = p.id === highlightId;
      const color = colorOf.get(p.group ?? "Runs")!;
      return {
        value: [p.x, p.y],
        id: p.id,
        point: p,
        symbolSize: isHi ? 16 : onFront ? 11 : 8,
        itemStyle: {
          color: ok ? color : withAlpha(color, 0.3),
          borderColor: isHi ? t.text : t.surface,
          borderWidth: isHi ? 2.5 : 1.5,
          opacity: ok ? 0.95 : 0.6,
        },
        label: isHi
          ? { show: true, formatter: p.label ?? p.id, position: "top", color: t.text, fontSize: 11, distance: 8 }
          : undefined,
      };
    };

    const series: Record<string, unknown>[] = groups.map((g) => ({
      type: "scatter",
      name: g,
      data: valid.filter((p) => (p.group ?? "Runs") === g).map(toDatum),
      emphasis: { scale: 1.3, itemStyle: { borderColor: t.text, borderWidth: 2 } },
      z: 3,
    }));

    if (frontPts.length > 1) {
      series.push({
        type: "line",
        name: "Front de Pareto",
        data: frontPts.map((p) => [p.x, p.y]),
        showSymbol: false,
        silent: true,
        lineStyle: { color: t.accent, width: 1.5, type: [6, 4] },
        itemStyle: { color: t.accent },
        z: 2,
        tooltip: { show: false },
      });
    }

    const guides: Record<string, unknown>[] = [];
    if (xConstraint)
      guides.push({
        xAxis: xConstraint.value,
        label: { formatter: `${opSymbol(xConstraint.op)} ${fx(xConstraint.value)}`, color: t.warning, fontSize: 11, position: "insideEndTop" },
      });
    if (yConstraint)
      guides.push({
        yAxis: yConstraint.value,
        label: { formatter: `${opSymbol(yConstraint.op)} ${fy(yConstraint.value)}`, color: t.warning, fontSize: 11, position: "insideEndTop" },
      });
    if (guides.length && series.length) {
      series[0] = {
        ...series[0],
        markLine: {
          silent: true,
          symbol: "none",
          animation: false,
          lineStyle: { color: t.warning, type: [4, 4], width: 1.25 },
          data: guides,
        },
      };
    }

    return {
      grid: { left: 12, right: 24, top: groups.length > 1 || frontPts.length > 1 ? 36 : 16, bottom: 30, containLabel: true },
      legend: groups.length > 1 || frontPts.length > 1 ? { ...legendBase(t) } : { show: false },
      tooltip: {
        ...tooltipBase(t),
        trigger: "item",
        formatter: (raw: unknown) => {
          const d = (raw as { data?: { point?: ParetoPoint }; color?: string }).data?.point;
          if (!d) return "";
          const rows = [
            [xLabel, fx(d.x)],
            [yLabel, fy(d.y)],
            ...Object.entries(d.meta ?? {}).map(([k, v]) => [k, v == null ? "—" : String(v)]),
          ]
            .map(
              ([k, v]) =>
                `<div style="display:flex;justify-content:space-between;gap:18px"><span style="color:${t.muted}">${escapeHtml(k)}</span><span>${escapeHtml(v)}</span></div>`,
            )
            .join("");
          const badge = frontIds.has(d.id) ? ` <span style="color:${t.accent};font-size:11px">· front de Pareto</span>` : "";
          return `<div style="font-weight:600;margin-bottom:4px">${tooltipDot(colorOf.get(d.group ?? "Runs")!)}${escapeHtml(d.label ?? d.id)}${badge}</div>${rows}`;
        },
      },
      xAxis: {
        ...ax,
        type: "value",
        name: xLabel,
        nameLocation: "middle",
        nameGap: 28,
        scale: true,
        axisLabel: { ...ax.axisLabel, formatter: (v: number) => fx(v) },
      },
      yAxis: {
        ...ax,
        type: "value",
        name: yLabel,
        nameLocation: "end",
        scale: true,
        axisLine: { show: false },
        axisLabel: { ...ax.axisLabel, formatter: (v: number) => fy(v) },
      },
      series,
    } as EChartsOption;
  }, [t, valid, xLabel, yLabel, xDirection, yDirection, xFormatter, yFormatter, xConstraint, yConstraint, highlightId, showFront]);

  return (
    <EChart
      option={option}
      height={height}
      className={className}
      loading={loading}
      empty={!loading && valid.length === 0 ? (empty ?? "Aucun point à afficher") : undefined}
      onEvents={
        onPointClick
          ? {
              click: (p: { data?: { point?: ParetoPoint } }) => {
                if (p.data?.point) onPointClick(p.data.point);
              },
            }
          : undefined
      }
    />
  );
}
