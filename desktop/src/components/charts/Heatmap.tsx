import { useMemo, type ReactNode } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "./EChart";
import { axisBase, escapeHtml, luminance, mixHex, tooltipBase, useChartTokens } from "./theme";
import { formatSignificant } from "../../lib/format";
import type { Direction } from "../../lib/types";

export interface HeatmapProps {
  /** Row labels (top to bottom). */
  rows: string[];
  /** Column labels (left to right). */
  columns: string[];
  /** values[row][col]; null = missing cell. */
  values: (number | null)[][];
  /** Cell text / tooltip formatter. */
  formatter?: (value: number, row: string, column: string) => string;
  min?: number;
  max?: number;
  /** "minimize" inverts the ramp so that darker always means better. */
  direction?: Direction;
  /** Display values inside cells (default true). */
  showValues?: boolean;
  height?: number;
  onCellClick?: (row: string, column: string, value: number | null) => void;
  empty?: ReactNode;
  className?: string;
}

/** Rows × columns heatmap on a single-hue sequential ramp (surface → accent). */
export function Heatmap({
  rows,
  columns,
  values,
  formatter,
  min,
  max,
  direction = "maximize",
  showValues = true,
  height,
  onCellClick,
  empty,
  className,
}: HeatmapProps) {
  const t = useChartTokens();
  const flat = values.flat().filter((v): v is number => v !== null && Number.isFinite(v));
  const lo = min ?? (flat.length ? Math.min(...flat) : 0);
  const hi = max ?? (flat.length ? Math.max(...flat) : 1);
  const computedHeight = height ?? Math.max(160, rows.length * 32 + 70);

  const option = useMemo<EChartsOption>(() => {
    const ax = axisBase(t);
    const fmt = formatter ?? ((v: number) => formatSignificant(v, 3));
    const low = t.theme === "dark" ? t.surface2 : mixHex(t.surface2, "#ffffff", 0.3);
    const colorFor = (v: number) => {
      let f = hi > lo ? (v - lo) / (hi - lo) : 1;
      if (direction === "minimize") f = 1 - f;
      return mixHex(low, t.accent, 0.08 + 0.92 * Math.max(0, Math.min(1, f)));
    };
    const data = [];
    for (let r = 0; r < rows.length; r++) {
      for (let c = 0; c < columns.length; c++) {
        const v = values[r]?.[c] ?? null;
        if (v === null || !Number.isFinite(v)) {
          data.push({ value: [c, r, "-"], itemStyle: { color: "transparent", borderColor: t.border, borderWidth: 1, borderType: [2, 2] } });
          continue;
        }
        const color = colorFor(v);
        data.push({
          value: [c, r, v],
          itemStyle: { color, borderColor: t.surface, borderWidth: 2, borderRadius: 4 },
          label: { color: luminance(color) > 0.4 ? t.theme === "dark" ? "#1f1e1d" : t.text : "#ffffff" },
        });
      }
    }
    return {
      grid: { left: 8, right: 8, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        ...tooltipBase(t),
        trigger: "item",
        formatter: (raw: unknown) => {
          const p = raw as { value: [number, number, number | string] };
          const [c, r, v] = p.value;
          const val = typeof v === "number" ? fmt(v, rows[r], columns[c]) : "Non mesuré";
          return `<div style="font-weight:600">${escapeHtml(rows[r])}</div><div style="color:${t.muted}">${escapeHtml(columns[c])}</div><div style="margin-top:2px">${escapeHtml(val)}</div>`;
        },
      },
      xAxis: {
        ...ax,
        type: "category",
        data: columns,
        position: "top",
        splitLine: { show: false },
        axisLine: { show: false },
        axisLabel: { ...ax.axisLabel, interval: 0, color: t.muted },
      },
      yAxis: {
        ...ax,
        type: "category",
        data: rows,
        inverse: true,
        splitLine: { show: false },
        axisLine: { show: false },
        axisLabel: { ...ax.axisLabel, interval: 0, color: t.text },
      },
      series: [
        {
          type: "heatmap",
          data,
          label: {
            show: showValues,
            fontSize: 11,
            fontFamily: t.fontSans,
            formatter: (p: { value: [number, number, number | string] }) =>
              typeof p.value[2] === "number" ? fmt(p.value[2], rows[p.value[1]], columns[p.value[0]]) : "",
          },
          emphasis: { itemStyle: { borderColor: t.text, borderWidth: 1.5 } },
        },
      ],
    } as EChartsOption;
  }, [t, rows, columns, values, formatter, lo, hi, direction, showValues]);

  return (
    <EChart
      option={option}
      height={computedHeight}
      className={className}
      empty={rows.length === 0 || columns.length === 0 ? (empty ?? "Aucune donnée") : undefined}
      onEvents={
        onCellClick
          ? {
              click: (p: { value: [number, number, number | string] }) => {
                const [c, r, v] = p.value;
                onCellClick(rows[r], columns[c], typeof v === "number" ? v : null);
              },
            }
          : undefined
      }
    />
  );
}
