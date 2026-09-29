import { useMemo, type ReactNode } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "./EChart";
import { escapeHtml, mixHex, tooltipBase, useChartTokens, withAlpha } from "./theme";
import { formatSignificant } from "../../lib/format";

export interface ParallelDimension {
  key: string;
  label: string;
  type?: "value" | "category";
  /** Category values (for type "category"). */
  categories?: string[];
  log?: boolean;
  /** Flip the axis (e.g. so that "better" is always up). */
  inverse?: boolean;
  formatter?: (v: number) => string;
}

export interface ParallelRow {
  id: string;
  /** One value per dimension (same order). */
  values: (number | string | null)[];
  /** Drawn on top with the accent color. */
  highlight?: boolean;
  label?: string;
}

export interface ParallelCoordsProps {
  dimensions: ParallelDimension[];
  rows: ParallelRow[];
  /** Index of the dimension used for the color ramp (default: last). */
  colorBy?: number;
  height?: number;
  onLineClick?: (row: ParallelRow) => void;
  empty?: ReactNode;
  className?: string;
}

/** Parallel coordinates (e.g. HPO trials: hyper-parameters → objective). */
export function ParallelCoords({ dimensions, rows, colorBy, height = 340, onLineClick, empty, className }: ParallelCoordsProps) {
  const t = useChartTokens();

  const option = useMemo<EChartsOption>(() => {
    const cIdx = colorBy ?? dimensions.length - 1;
    const colorVals = rows.map((r) => r.values[cIdx]).filter((v): v is number => typeof v === "number" && Number.isFinite(v));
    const lo = colorVals.length ? Math.min(...colorVals) : 0;
    const hi = colorVals.length ? Math.max(...colorVals) : 1;
    const low = mixHex(t.faint, t.surface, 0.3);
    const lineColor = (v: unknown) => {
      if (typeof v !== "number" || !Number.isFinite(v)) return withAlpha(t.faint, 0.4);
      const f = hi > lo ? (v - lo) / (hi - lo) : 1;
      return mixHex(low, t.accent, f);
    };
    const toData = (r: ParallelRow) => ({
      value: r.values.map((v) => (v === null ? (undefined as unknown as number) : v)),
      row: r,
      lineStyle: r.highlight ? { color: t.text, width: 2.5, opacity: 1 } : { color: lineColor(r.values[cIdx]), width: 1.4, opacity: 0.7 },
    });
    const axisCommon = {
      nameLocation: "start" as const,
      nameGap: 16,
      nameTextStyle: { color: t.text, fontSize: 11.5, fontFamily: t.fontSans },
      axisLine: { lineStyle: { color: t.border } },
      axisTick: { show: false },
      axisLabel: { color: t.muted, fontSize: 10.5, fontFamily: t.fontSans },
      splitLine: { show: false },
    };
    return {
      parallel: { left: 48, right: 56, top: 44, bottom: 24, parallelAxisDefault: { ...axisCommon } },
      parallelAxis: dimensions.map((d, i) => ({
        ...axisCommon,
        dim: i,
        name: d.label,
        type: d.type === "category" ? "category" : d.log ? "log" : "value",
        data: d.categories,
        inverse: d.inverse,
        scale: true,
        axisLabel: {
          ...axisCommon.axisLabel,
          formatter: d.type === "category" ? undefined : (v: number) => (d.formatter ?? ((x: number) => formatSignificant(x, 3)))(v),
        },
      })),
      tooltip: {
        ...tooltipBase(t),
        trigger: "item",
        formatter: (raw: unknown) => {
          const row = (raw as { data?: { row?: ParallelRow } }).data?.row;
          if (!row) return "";
          const lines = dimensions
            .map((d, i) => {
              const v = row.values[i];
              const s = v === null ? "—" : typeof v === "number" ? (d.formatter ?? ((x: number) => formatSignificant(x, 4)))(v) : v;
              return `<div style="display:flex;justify-content:space-between;gap:16px"><span style="color:${t.muted}">${escapeHtml(d.label)}</span><span>${escapeHtml(s)}</span></div>`;
            })
            .join("");
          return `<div style="font-weight:600;margin-bottom:4px">${escapeHtml(row.label ?? row.id)}</div>${lines}`;
        },
      },
      series: [
        {
          type: "parallel",
          smooth: false,
          inactiveOpacity: 0.08,
          activeOpacity: 1,
          emphasis: { lineStyle: { width: 3, opacity: 1 } },
          data: [...rows.filter((r) => !r.highlight), ...rows.filter((r) => r.highlight)].map(toData),
        },
      ],
    } as EChartsOption;
  }, [t, dimensions, rows, colorBy]);

  return (
    <EChart
      option={option}
      height={height}
      className={className}
      empty={rows.length === 0 || dimensions.length === 0 ? (empty ?? "Aucune donnée") : undefined}
      onEvents={
        onLineClick
          ? {
              click: (p: { data?: { row?: ParallelRow } }) => {
                if (p.data?.row) onLineClick(p.data.row);
              },
            }
          : undefined
      }
    />
  );
}
