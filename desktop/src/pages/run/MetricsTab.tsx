import clsx from "clsx";
import { useMemo, useState } from "react";
import { ChartLine, Search } from "lucide-react";
import { Button, Card, EmptyState, Input, Segmented, Skeleton, Switch, Tooltip } from "../../components/ui";
import { LineChart, seriesColor, useChartTokens, withAlpha, type LineSeries } from "../../components/charts";
import { formatNumber } from "../../lib/format";
import { useLocalStorage } from "../../lib/hooks";
import { formatMetric, formatMetricAxis, groupMetricKeys, metricInfo, metricLabel, qualityMetricFor, smoothSeries } from "../../lib/metrics";
import { useRunMetrics } from "../../lib/queries";
import type { RunDetail } from "../../lib/types";

type Layout = "grid" | "overlay";

function defaultKeys(run: RunDetail): string[] {
  const q = qualityMetricFor(run.modality);
  const wanted = ["train/loss", "val/bit_acc_mean", `val/${q}`, "val/bit_acc", "train/lr", "train/attack_strength"];
  const present = wanted.filter((k) => run.metric_keys.includes(k));
  return present.length ? present : run.metric_keys.slice(0, 4);
}

export function MetricsTab({ run, live }: { run: RunDetail; live: boolean }) {
  const t = useChartTokens();
  const [stored, setStored] = useLocalStorage<string[]>("wmlab.metrics.selection", []);
  const [filter, setFilter] = useState("");
  const [layout, setLayout] = useLocalStorage<Layout>("wmlab.metrics.layout", "grid");
  const [logScale, setLogScale] = useLocalStorage("wmlab.metrics.log", false);
  const [smoothing, setSmoothing] = useLocalStorage("wmlab.metrics.smoothing", 0);

  const selected = useMemo(() => {
    const valid = stored.filter((k) => run.metric_keys.includes(k));
    return valid.length ? valid : defaultKeys(run);
  }, [stored, run]);

  const groups = useMemo(() => groupMetricKeys(run.metric_keys), [run.metric_keys]);
  const metrics = useRunMetrics(run.run_id, selected, { live, maxPoints: 1500, enabled: selected.length > 0 });
  const phases = metrics.data?.phases ?? run.plan?.phases.map((p) => ({ name: p.name, start: p.start, end: p.end })) ?? [];

  const toggle = (key: string) => {
    const next = selected.includes(key) ? selected.filter((k) => k !== key) : [...selected, key];
    setStored(next);
  };

  const buildSeries = (key: string, index: number, withRaw: boolean): LineSeries[] => {
    const s = metrics.data?.series[key];
    if (!s) return [];
    const color = seriesColor(t, index);
    const label = metricLabel(key, { withGroup: true });
    if (smoothing <= 0) return [{ name: label, x: s.steps, y: s.values, color }];
    const smooth: LineSeries = { name: label, x: s.steps, y: smoothSeries(s.values, smoothing), color };
    return withRaw ? [{ name: `${label} (brut)`, x: s.steps, y: s.values, color: withAlpha(color, 0.28), width: 1 }, smooth] : [smooth];
  };

  const q = filter.trim().toLowerCase();

  if (!run.metric_keys.length) {
    return (
      <Card padding="none">
        <EmptyState icon={ChartLine} title="Aucune métrique" description="Ce run n'a encore écrit aucune ligne dans metrics.jsonl." />
      </Card>
    );
  }

  return (
    <div className="flex gap-6">
      {/* Key selector */}
      <aside className="w-[250px] shrink-0">
        <div className="sticky top-2 space-y-3">
          <Input icon={Search} size="sm" placeholder="Filtrer les métriques" value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="max-h-[calc(100vh-260px)] space-y-4 overflow-y-auto pr-1">
            {groups.map((g) => {
              const keys = g.keys.filter((k) => !q || k.toLowerCase().includes(q) || metricLabel(k).toLowerCase().includes(q));
              if (!keys.length) return null;
              return (
                <div key={g.id}>
                  <div className="mb-1 flex items-center justify-between text-[12px] font-medium text-muted">
                    <span>{g.label}</span>
                    <span className="text-faint">{keys.length}</span>
                  </div>
                  <ul className="space-y-px">
                    {keys.map((k) => {
                      const on = selected.includes(k);
                      const idx = selected.indexOf(k);
                      return (
                        <li key={k}>
                          <Tooltip content={<span className="font-mono">{k}</span>} side="right" wrapperClassName="flex w-full">
                            <button
                              type="button"
                              onClick={() => toggle(k)}
                              className={clsx(
                                "no-drag flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[12.5px] transition-colors",
                                on ? "bg-active text-fg" : "text-muted hover:bg-hover hover:text-fg",
                              )}
                            >
                              <span
                                className="h-2 w-2 shrink-0 rounded-full border"
                                style={on ? { background: seriesColor(t, idx), borderColor: seriesColor(t, idx) } : { borderColor: "var(--border-strong)" }}
                              />
                              <span className="truncate">{metricLabel(k)}</span>
                            </button>
                          </Tooltip>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              );
            })}
          </div>
          <Button size="sm" variant="ghost" onClick={() => setStored([])}>
            Sélection par défaut
          </Button>
        </div>
      </aside>

      {/* Charts */}
      <div className="min-w-0 flex-1">
        <div className="mb-4 flex flex-wrap items-center gap-4">
          <Segmented<Layout>
            value={layout}
            onChange={setLayout}
            options={[
              { value: "grid", label: "Séparés" },
              { value: "overlay", label: "Superposés" },
            ]}
          />
          <Switch size="sm" checked={logScale} onChange={setLogScale} label="Échelle log" />
          <label className="flex items-center gap-2 text-[13px] text-muted">
            Lissage
            <input
              type="range"
              min={0}
              max={0.95}
              step={0.05}
              value={smoothing}
              onChange={(e) => setSmoothing(Number(e.target.value))}
              className="no-drag w-28 accent-[var(--accent)]"
            />
            <span className="tabular w-8 text-fg">{formatNumber(smoothing, { digits: 2 })}</span>
          </label>
          <span className="ml-auto text-[12px] text-faint">Pincer ou ctrl + molette pour zoomer, glisser pour se déplacer</span>
        </div>

        {selected.length === 0 ? (
          <EmptyState compact title="Aucune métrique sélectionnée" description="Choisissez des clés dans la liste de gauche." />
        ) : metrics.isLoading ? (
          <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
            {selected.map((k) => (
              <Skeleton key={k} className="h-[280px] rounded-xl" />
            ))}
          </div>
        ) : layout === "overlay" ? (
          <Card padding="sm" className="px-4 pt-3">
            <LineChart
              height={440}
              series={selected.flatMap((k, i) => buildSeries(k, i, false))}
              phases={phases}
              logScale={logScale}
              zoom="slider"
              markX={live && run.progress ? { value: run.progress.step } : null}
            />
          </Card>
        ) : (
          <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
            {selected.map((k, i) => {
              const last = metrics.data?.series[k]?.values.at(-1);
              return (
                <Card key={k} padding="sm" className="px-4 pt-3">
                  <div className="mb-1 flex items-baseline justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-[13.5px] font-medium text-fg">{metricLabel(k, { withGroup: true })}</div>
                      <div className="truncate font-mono text-[11px] text-faint">{k}</div>
                    </div>
                    <div className="shrink-0 text-right">
                      <div className="tabular font-serif text-[17px] text-fg">{formatMetric(k, last)}</div>
                      {metricInfo(k).direction && (
                        <div className="text-[11px] text-faint">{metricInfo(k).direction === "maximize" ? "à maximiser" : "à minimiser"}</div>
                      )}
                    </div>
                  </div>
                  <LineChart
                    height={250}
                    series={buildSeries(k, i, true)}
                    legend={false}
                    phases={phases}
                    logScale={logScale}
                    markX={live && run.progress ? { value: run.progress.step } : null}
                    yFormatter={(v) => formatMetricAxis(k, v)}
                    valueFormatter={(v) => formatMetric(k, v)}
                  />
                </Card>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
