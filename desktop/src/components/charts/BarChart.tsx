import { useMemo, type ReactNode } from "react";
import type { EChartsOption } from "echarts";
import { EChart } from "./EChart";
import { axisBase, escapeHtml, legendBase, seriesColor, tooltipBase, tooltipDot, useChartTokens } from "./theme";
import { formatSignificant } from "../../lib/format";

export interface BarSeries {
  name: string;
  values: (number | null)[];
  color?: string;
}

export interface BarChartProps {
  categories: string[];
  series: BarSeries[];
  /** Horizontal bars (categories on the y axis). */
  horizontal?: boolean;
  stacked?: boolean;
  valueFormatter?: (v: number) => string;
  /** Show values at bar ends (default: single series with ≤ 12 bars). */
  showValues?: boolean;
  legend?: boolean;
  height?: number;
  onBarClick?: (category: string, seriesName: string) => void;
  empty?: ReactNode;
  className?: string;
}

/** Vertical or horizontal bars, optionally stacked. */
export function BarChart({
  categories,
  series,
  horizontal,
  stacked,
  valueFormatter,
  showValues,
  legend,
  height,
  onBarClick,
  empty,
  className,
}: BarChartProps) {
  const t = useChartTokens();
  const showLegend = legend ?? series.length > 1;
  const labels = showValues ?? (series.length === 1 && categories.length <= 12);
  const computedHeight = height ?? (horizontal ? Math.max(140, categories.length * 30 + 60) : 260);

  const option = useMemo<EChartsOption>(() => {
    const ax = axisBase(t);
    const fmt = valueFormatter ?? ((v: number) => formatSignificant(v, 3));
    const catAxis = {
      ...ax,
      type: "category" as const,
      data: categories,
      splitLine: { show: false },
      axisLabel: { ...ax.axisLabel, interval: 0, color: t.muted },
      inverse: horizontal,
    };
    const valAxis = {
      ...ax,
      type: "value" as const,
      axisLine: { show: false },
      axisLabel: { ...ax.axisLabel, formatter: (v: number) => fmt(v) },
    };
    const radius = horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0];
    return {
      grid: { left: 8, right: labels && horizontal ? 56 : 16, top: showLegend ? 34 : 14, bottom: 8, containLabel: true },
      legend: showLegend ? { ...legendBase(t) } : { show: false },
      tooltip: {
        ...tooltipBase(t),
        trigger: "axis",
        axisPointer: { type: "shadow", shadowStyle: { color: t.theme === "dark" ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.03)" } },
        formatter: (raw: unknown) => {
          const params = (Array.isArray(raw) ? raw : [raw]) as { name: string; seriesName: string; value: number | null; color: string }[];
          if (!params.length) return "";
          const rows = params
            .filter((p) => p.value !== null && p.value !== undefined)
            .map(
              (p) =>
                `<div style="display:flex;justify-content:space-between;gap:16px"><span>${tooltipDot(p.color)}${escapeHtml(p.seriesName)}</span><b style="font-weight:600">${escapeHtml(fmt(p.value as number))}</b></div>`,
            )
            .join("");
          return `<div style="font-weight:500;margin-bottom:4px">${escapeHtml(params[0].name)}</div>${rows}`;
        },
      },
      xAxis: horizontal ? valAxis : catAxis,
      yAxis: horizontal ? catAxis : valAxis,
      series: series.map((s, i) => ({
        id: `bar-${i}-${s.name}`,
        type: "bar",
        name: s.name,
        data: s.values,
        stack: stacked ? "total" : undefined,
        barMaxWidth: 28,
        barGap: "20%",
        itemStyle: {
          color: s.color ?? seriesColor(t, i),
          borderRadius: stacked && i < series.length - 1 ? 0 : radius,
          borderColor: t.surface,
          borderWidth: stacked ? 1 : 0,
        },
        label: labels
          ? {
              show: true,
              position: horizontal ? "right" : "top",
              color: t.muted,
              fontSize: 11,
              formatter: (p: { value: number | null }) => (p.value == null ? "" : fmt(p.value)),
            }
          : undefined,
        emphasis: { focus: "series" },
      })),
    } as EChartsOption;
  }, [t, categories, series, horizontal, stacked, valueFormatter, labels, showLegend]);

  return (
    <EChart
      option={option}
      height={computedHeight}
      className={className}
      empty={categories.length === 0 ? (empty ?? "Aucune donnée") : undefined}
      onEvents={
        onBarClick
          ? { click: (p: { name: string; seriesName: string }) => onBarClick(p.name, p.seriesName) }
          : undefined
      }
    />
  );
}
