import clsx from "clsx";
import { useEffect, useRef, type CSSProperties, type ReactNode } from "react";
import * as echarts from "echarts/core";
import { BarChart as EBar, HeatmapChart, LineChart as ELine, ParallelChart, ScatterChart } from "echarts/charts";
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  ParallelComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";
import { useChartTokens } from "./theme";
import { Spinner } from "../ui/Spinner";

echarts.use([
  ELine,
  EBar,
  ScatterChart,
  HeatmapChart,
  ParallelChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  VisualMapComponent,
  ParallelComponent,
  CanvasRenderer,
]);

export type { EChartsOption };
export type EChartsInstance = echarts.ECharts;
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type EChartEventHandler = (params: any) => void;

export interface EChartProps {
  option: EChartsOption;
  height?: number | string;
  className?: string;
  style?: CSSProperties;
  /** Event handlers, e.g. { click: (p) => … }. */
  onEvents?: Record<string, EChartEventHandler>;
  /** Replace the whole option on update (default true). */
  notMerge?: boolean;
  loading?: boolean;
  /** Overlay shown instead of the chart (e.g. "Aucune donnée"). */
  empty?: ReactNode;
  onReady?: (chart: EChartsInstance) => void;
}

/**
 * ECharts wrapper: init/dispose, ResizeObserver, base styles from the CSS tokens,
 * re-render when the light/dark theme changes.
 */
export function EChart({
  option,
  height = 280,
  className,
  style,
  onEvents,
  notMerge = true,
  loading,
  empty,
  onReady,
}: EChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<EChartsInstance | null>(null);
  const handlersRef = useRef(onEvents);
  handlersRef.current = onEvents;
  const tokens = useChartTokens();
  const eventNames = Object.keys(onEvents ?? {}).sort().join(",");

  // Init / dispose.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = echarts.init(el, null, { renderer: "canvas" });
    chartRef.current = chart;
    let raf = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        if (!chart.isDisposed()) chart.resize();
      });
    });
    ro.observe(el);
    onReady?.(chart);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Option updates (base styles merged in).
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const base: EChartsOption = {
      backgroundColor: "transparent",
      color: tokens.palette,
      textStyle: { fontFamily: tokens.fontSans, color: tokens.text },
      animationDuration: 350,
      animationDurationUpdate: 250,
    };
    chart.setOption({ ...base, ...option }, { notMerge, lazyUpdate: true });
  }, [option, notMerge, tokens]);

  // Event binding (stable wrappers reading the latest handlers).
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !eventNames) return;
    const names = eventNames.split(",");
    const wrappers = names.map((name) => {
      const fn: EChartEventHandler = (params) => handlersRef.current?.[name]?.(params);
      chart.on(name, fn);
      return [name, fn] as const;
    });
    return () => {
      wrappers.forEach(([name, fn]) => chart.off(name, fn));
    };
  }, [eventNames]);

  return (
    <div className={clsx("relative w-full", className)} style={{ height, ...style }}>
      <div ref={containerRef} className="absolute inset-0" />
      {(loading || empty) && (
        <div className="absolute inset-0 flex items-center justify-center text-[13px] text-muted">
          {loading ? <Spinner /> : empty}
        </div>
      )}
    </div>
  );
}
