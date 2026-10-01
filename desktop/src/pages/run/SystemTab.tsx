import { useMemo } from "react";
import { Cpu } from "lucide-react";
import { Card, CardHeader, EmptyState, Kpi, LoadingBlock } from "../../components/ui";
import { LineChart } from "../../components/charts";
import { errorMessage } from "../../lib/api";
import { formatMetric, formatMetricAxis, metricLabel } from "../../lib/metrics";
import { useRunSystem } from "../../lib/queries";
import type { RunDetail } from "../../lib/types";

const ORDER = ["gpu_util", "gpu_mem_gb", "cpu_percent", "ram_gb", "proc_ram_gb"];

export function SystemTab({ run, live }: { run: RunDetail; live: boolean }) {
  const system = useRunSystem(run.run_id, { live, maxPoints: 600 });

  const keys = useMemo(() => {
    const all = Object.keys(system.data?.series ?? {});
    return [...ORDER.filter((k) => all.includes(k)), ...all.filter((k) => !ORDER.includes(k)).sort()];
  }, [system.data]);

  if (system.isLoading) return <LoadingBlock label="Chargement des mesures système…" />;
  if (system.isError) {
    return (
      <Card padding="none">
        <EmptyState compact icon={Cpu} title="Mesures indisponibles" description={errorMessage(system.error)} />
      </Card>
    );
  }
  const t = system.data?.t ?? [];
  if (!t.length || !keys.length) {
    return (
      <Card padding="none">
        <EmptyState
          icon={Cpu}
          title="Aucune mesure système"
          description="Le moniteur système (toutes les 15 s) n'a encore rien écrit dans system.jsonl."
        />
      </Card>
    );
  }
  const xs = t.map((s) => s * 1000);
  const hw = run.env.hardware as { gpus?: { name?: string; memory_gb?: number }[]; device?: string } | null;

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {keys.slice(0, 4).map((k) => {
          const vals = system.data!.series[k].filter((v) => Number.isFinite(v));
          const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
          return (
            <Kpi
              key={k}
              label={metricLabel(k)}
              value={formatMetric(k, vals.at(-1))}
              sub={mean != null ? `moyenne ${formatMetric(k, mean)}` : undefined}
            />
          );
        })}
      </div>
      {hw?.gpus?.length ? (
        <div className="text-[12.5px] text-muted">
          Matériel : {hw.gpus.map((g) => `${g.name ?? "GPU"}${g.memory_gb ? ` (${g.memory_gb} Go)` : ""}`).join(", ")}
          {hw.device ? ` · device ${hw.device}` : ""}
        </div>
      ) : null}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {keys.map((k) => (
          <Card key={k} padding="sm" className="px-4 pt-3">
            <CardHeader title={metricLabel(k)} description={<span className="font-mono text-[11px]">{k}</span>} className="mb-1" />
            <LineChart
              height={220}
              xType="time"
              series={[{ name: metricLabel(k), x: xs, y: system.data!.series[k], area: true }]}
              legend={false}
              yMin={k === "gpu_util" || k === "cpu_percent" ? 0 : undefined}
              yMax={k === "gpu_util" || k === "cpu_percent" ? 100 : undefined}
              yFormatter={(v) => formatMetricAxis(k, v)}
              valueFormatter={(v) => formatMetric(k, v)}
            />
          </Card>
        ))}
      </div>
    </div>
  );
}
