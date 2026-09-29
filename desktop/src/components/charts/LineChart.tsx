import { useMemo, type ReactNode } from "react";
import type { EChartsOption } from "echarts";
import { EChart, type EChartEventHandler } from "./EChart";
import {
  axisBase,
  escapeHtml,
  legendBase,
  needsDash,
  seriesColor,
  tooltipBase,
  tooltipDot,
  useChartTokens,
  withAlpha,
} from "./theme";
import { formatInt, formatSignificant, formatTime } from "../../lib/format";

export interface LineSeries {
  name: string;
  x: number[];
  y: (number | null)[];
  /** Explicit color (defaults to the palette slot of the series index). */
  color?: string;
  dashed?: boolean;
  /** Light area fill under the line. */
  area?: boolean;
  width?: number;
  /** Draw as a step function. */
  step?: false | "start" | "middle" | "end";
}

export interface PhaseBand {
  name: string;
  start: number;
  end: number;
}

export interface LineChartProps {
  series: LineSeries[];
  height?: number;
  /** Curriculum phases drawn as alternating bands with their names. */
  phases?: PhaseBand[];
  /** Logarithmic y axis (non-positive values are dropped). */
  logScale?: boolean;
  /** "value" (steps) or "time" (x in epoch milliseconds). */
  xType?: "value" | "time";
  xLabel?: string;
  yLabel?: string;
  yFormatter?: (v: number) => string;
  /** Tooltip value formatter (defaults to yFormatter). */
  valueFormatter?: (v: number, seriesName: string) => string;
  /** Zoom: "inside" (pinch / ctrl+wheel, drag to pan), "slider" (plus a slider), false. */
  zoom?: false | "inside" | "slider";
  /** Show the legend (default: more than one series). */
  legend?: boolean;
  /** Vertical marker (e.g. current step). */
  markX?: { value: number; label?: string } | null;
  /** Horizontal reference lines (constraints, targets…). */
  referenceLines?: { y: number; label?: string; color?: string }[];
  yMin?: number | "dataMin";
  yMax?: number | "dataMax";
  loading?: boolean;
  empty?: ReactNode;
  className?: string;
  onEvents?: Record<string, EChartEventHandler>;
}

/** Multi-series line chart with phase bands, optional log scale and zoom. */
export function LineChart({
  series,
  height = 260,
  phases,
  logScale,
  xType = "value",
  xLabel,
  yLabel,
  yFormatter,
  valueFormatter,
  zoom = "inside",
  legend,
  markX,
  referenceLines,
  yMin,
  yMax,
  loading,
  empty,
  className,
  onEvents,
}: LineChartProps) {
  const t = useChartTokens();
  const hasData = series.some((s) => s.y.some((v) => v !== null && Number.isFinite(v)));
  const showLegend = legend ?? series.length > 1;

  const option = useMemo<EChartsOption>(() => {
    const ax = axisBase(t);
    const fmtY = yFormatter ?? ((v: number) => formatSignificant(v, 3));
    const fmtVal = valueFormatter ?? ((v: number) => fmtY(v));
    const fmtX = xType === "time" ? (v: number) => formatTime(v, false) : (v: number) => formatInt(v);
    const bands = phases ?? [];
    const phaseAt = (x: number) => bands.find((p) => x >= p.start && x < p.end) ?? (bands.length && x >= bands[bands.length - 1].end ? bands[bands.length - 1] : undefined);

    const firstWithData = Math.max(
      0,
      series.findIndex((s) => s.y.some((v) => v !== null)),
    );

    const echSeries = series.map((s, i) => {
      const color = s.color ?? seriesColor(t, i);
      const data: [number, number | null][] = s.x.map((x, j) => {
        const v = s.y[j];
        const ok = v !== null && v !== undefined && Number.isFinite(v) && (!logScale || v > 0);
        return [x, ok ? v : null];
      });
      const dashed = s.dashed ?? needsDash(i, t);
      const item: Record<string, unknown> = {
        type: "line",
        name: s.name,
        data,
        showSymbol: data.length <= 2,
        symbolSize: 5,
        connectNulls: true,
        step: s.step || false,
        sampling: "lttb",
        lineStyle: { width: s.width ?? 2, color, type: dashed ? [6, 4] : "solid" },
        itemStyle: { color },
        emphasis: { focus: "series", lineStyle: { width: (s.width ?? 2) + 0.5 } },
        areaStyle: s.area ? { color: withAlpha(color, 0.1) } : undefined,
        z: 3,
      };
      if (i === firstWithData && bands.length) {
        item.markArea = {
          silent: true,
          data: bands.map((p, k) => [
            {
              name: p.name,
              xAxis: p.start,
              itemStyle: { color: k % 2 === 0 ? withAlpha(t.text, t.theme === "dark" ? 0.045 : 0.03) : "transparent" },
              label: {
                show: true,
                position: "insideTopLeft",
                color: t.faint,
                fontSize: 10.5,
                fontFamily: t.fontSans,
                formatter: p.name,
                overflow: "truncate",
              },
            },
            { xAxis: p.end },
          ]),
        };
      }
      if (i === firstWithData && (markX || referenceLines?.length)) {
        const lines: Record<string, unknown>[] = [];
        if (markX) {
          lines.push({
            xAxis: markX.value,
            lineStyle: { color: t.accent, type: "solid", width: 1.25 },
            label: { show: !!markX.label, formatter: markX.label ?? "", color: t.accent, fontSize: 11, position: "insideEndTop" },
          });
        }
        for (const r of referenceLines ?? []) {
          lines.push({
            yAxis: r.y,
            lineStyle: { color: r.color ?? t.warning, type: [4, 4], width: 1.25 },
            label: { show: !!r.label, formatter: r.label ?? "", color: r.color ?? t.warning, fontSize: 11, position: "insideEndTop" },
          });
        }
        item.markLine = { silent: true, symbol: "none", animation: false, data: lines };
      }
      return item;
    });

    return {
      grid: {
        left: 8,
        right: 16,
        top: showLegend ? 34 : 14,
        bottom: zoom === "slider" ? 44 : xLabel ? 26 : 8,
        containLabel: true,
      },
      legend: showLegend ? { ...legendBase(t), data: series.map((s) => s.name) } : { show: false },
      tooltip: {
        ...tooltipBase(t),
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: t.faint, width: 1 }, animation: false },
        formatter: (raw: unknown) => {
          const params = (Array.isArray(raw) ? raw : [raw]) as {
            value: [number, number | null];
            seriesName: string;
            color: string;
          }[];
          if (!params.length) return "";
          const x = params[0].value[0];
          const phase = phaseAt(x);
          const head = `${xType === "time" ? formatTime(x) : `Step ${formatInt(x)}`}${phase ? ` · <span style="color:${t.muted}">${escapeHtml(phase.name)}</span>` : ""}`;
          const rows = params
            .filter((p) => p.value[1] !== null && p.value[1] !== undefined)
            .map(
              (p) =>
                `<div style="display:flex;justify-content:space-between;gap:16px"><span>${tooltipDot(p.color)}${escapeHtml(p.seriesName)}</span><b style="font-weight:600">${escapeHtml(fmtVal(p.value[1] as number, p.seriesName))}</b></div>`,
            )
            .join("");
          return `<div style="font-weight:500;margin-bottom:4px">${head}</div>${rows}`;
        },
      },
      xAxis: {
        ...ax,
        type: xType,
        name: xLabel,
        nameLocation: "middle",
        nameGap: 26,
        min: "dataMin",
        max: "dataMax",
        splitLine: { show: false },
        axisLabel: { ...ax.axisLabel, formatter: (v: number) => fmtX(v) },
      },
      yAxis: {
        ...ax,
        type: logScale ? "log" : "value",
        logBase: 10,
        name: yLabel,
        nameLocation: "end",
        scale: true,
        min: yMin,
        max: yMax,
        axisLine: { show: false },
        axisLabel: { ...ax.axisLabel, formatter: (v: number) => fmtY(v) },
      },
      dataZoom:
        zoom === false
          ? []
          : [
              {
                type: "inside",
                xAxisIndex: 0,
                filterMode: "none",
                zoomOnMouseWheel: "ctrl",
                moveOnMouseWheel: false,
                moveOnMouseMove: true,
              },
              ...(zoom === "slider"
                ? [
                    {
                      type: "slider" as const,
                      xAxisIndex: 0,
                      filterMode: "none" as const,
                      height: 16,
                      bottom: 6,
                      borderColor: t.border,
                      backgroundColor: "transparent",
                      fillerColor: withAlpha(t.accent, 0.12),
                      handleStyle: { color: t.surface, borderColor: t.faint },
                      moveHandleStyle: { color: t.border },
                      dataBackground: { lineStyle: { color: t.faint }, areaStyle: { color: withAlpha(t.faint, 0.2) } },
                      textStyle: { color: t.muted, fontSize: 10 },
                      labelFormatter: (v: number) => fmtX(v),
                    },
                  ]
                : []),
            ],
      series: echSeries,
    } as EChartsOption;
  }, [t, series, phases, logScale, xType, xLabel, yLabel, yFormatter, valueFormatter, zoom, showLegend, markX, referenceLines, yMin, yMax]);

  return (
    <EChart
      option={option}
      height={height}
      className={className}
      loading={loading}
      empty={!loading && !hasData ? (empty ?? "Aucune donnée") : undefined}
      onEvents={onEvents}
    />
  );
}
