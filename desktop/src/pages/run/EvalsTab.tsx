import { useMemo, useState } from "react";
import { Microscope } from "lucide-react";
import { Button, Card, CardHeader, EmptyState, KeyValue, Select, Table, type Column } from "../../components/ui";
import { Heatmap } from "../../components/charts";
import { useLaunch } from "../../components/launch/LaunchProvider";
import { formatDuration, formatInt, formatRelative, toDate } from "../../lib/format";
import { formatMetric, metricLabel } from "../../lib/metrics";
import { usePermissions } from "../../lib/queries";
import type { EvalResult, RunDetail } from "../../lib/types";

function robustnessKeys(evals: EvalResult[]): string[] {
  const keys = new Set<string>();
  for (const e of evals) for (const v of Object.values(e.robustness)) Object.keys(v).forEach((k) => keys.add(k));
  return [...keys].sort((a, b) => (a === "bit_acc" ? -1 : b === "bit_acc" ? 1 : a.localeCompare(b, "en", { numeric: true })));
}

export function EvalsTab({ run }: { run: RunDetail }) {
  const { openLaunch } = useLaunch();
  const { canWrite } = usePermissions();
  const benchmarks = useMemo(() => [...new Set(run.evals.map((e) => e.benchmark))].sort(), [run.evals]);
  const [benchmark, setBenchmark] = useState(benchmarks[0] ?? "");
  const current = benchmarks.includes(benchmark) ? benchmark : (benchmarks[0] ?? "");

  const stepOf = useMemo(() => new Map(run.checkpoints.map((c) => [c.name, c.step])), [run.checkpoints]);
  const evals = useMemo(
    () =>
      run.evals
        .filter((e) => e.benchmark === current)
        .sort(
          (a, b) =>
            (stepOf.get(a.checkpoint) ?? Number.MAX_SAFE_INTEGER) - (stepOf.get(b.checkpoint) ?? Number.MAX_SAFE_INTEGER) ||
            (toDate(a.created_at)?.getTime() ?? 0) - (toDate(b.created_at)?.getTime() ?? 0),
        ),
    [run.evals, current, stepOf],
  );
  const rKeys = useMemo(() => robustnessKeys(evals), [evals]);
  const [metric, setMetric] = useState("bit_acc");
  const rMetric = rKeys.includes(metric) ? metric : (rKeys[0] ?? "bit_acc");
  const [focus, setFocus] = useState<string | null>(null);
  const focused = evals.find((e) => e.checkpoint === focus) ?? evals[evals.length - 1];

  const evaluate = () => openLaunch({ kind: "eval", project: run.project, evalRun: run.run_id, evalCheckpoint: "last" });

  if (!run.evals.length) {
    return (
      <Card padding="none">
        <EmptyState
          icon={Microscope}
          title="Aucune évaluation"
          description="Évaluez un checkpoint sur un benchmark figé pour mesurer qualité et robustesse attaque par attaque."
          action={
            canWrite ? (
              <Button variant="primary" icon={Microscope} onClick={evaluate} disabled={!run.checkpoints.length}>
                Évaluer un checkpoint
              </Button>
            ) : undefined
          }
        />
      </Card>
    );
  }

  const attacks = [...new Set(evals.flatMap((e) => Object.keys(e.robustness)))];
  const columns = evals.map((e) => e.checkpoint);
  const matrix = attacks.map((a) => evals.map((e) => e.robustness[a]?.[rMetric] ?? null));
  const qualityKeys = [...new Set(evals.flatMap((e) => Object.keys(e.quality)))];
  const summaryKeys = [...new Set(evals.flatMap((e) => Object.keys(e.summary)))].filter((k) => !qualityKeys.includes(k));

  const tableColumns: Column<EvalResult>[] = [
    {
      id: "ckpt",
      header: "Checkpoint",
      cell: (e) => (
        <div>
          <div className="font-mono text-[12.5px] text-fg">{e.checkpoint}</div>
          {stepOf.has(e.checkpoint) && <div className="text-[11.5px] text-muted">step {formatInt(stepOf.get(e.checkpoint))}</div>}
        </div>
      ),
      sortValue: (e) => stepOf.get(e.checkpoint) ?? null,
    },
    ...qualityKeys.map<Column<EvalResult>>((k) => ({
      id: `q-${k}`,
      header: metricLabel(k),
      align: "right",
      cell: (e) => <span className="tabular">{formatMetric(k, e.quality[k])}</span>,
      sortValue: (e) => e.quality[k],
    })),
    ...summaryKeys.map<Column<EvalResult>>((k) => ({
      id: `s-${k}`,
      header: metricLabel(k),
      align: "right",
      cell: (e) => <span className="tabular">{formatMetric(k, e.summary[k])}</span>,
      sortValue: (e) => e.summary[k],
    })),
    {
      id: "date",
      header: "Évalué",
      align: "right",
      cell: (e) => <span className="whitespace-nowrap text-muted">{formatRelative(e.created_at)}</span>,
      sortValue: (e) => toDate(e.created_at)?.getTime(),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end gap-3">
        {benchmarks.length > 1 && (
          <Select
            label="Benchmark"
            size="sm"
            value={current}
            onChange={setBenchmark}
            options={benchmarks.map((b) => ({ value: b, label: b }))}
            className="w-56"
          />
        )}
        <Select
          label="Métrique de robustesse"
          size="sm"
          value={rMetric}
          onChange={setMetric}
          options={rKeys.map((k) => ({ value: k, label: metricLabel(k) }))}
          className="w-56"
        />
        {canWrite && (
          <Button icon={Microscope} onClick={evaluate} className="ml-auto">
            Nouvelle évaluation
          </Button>
        )}
      </div>

      <Card>
        <CardHeader
          title={`Robustesse par attaque — ${metricLabel(rMetric)}`}
          description={`Benchmark ${current}${evals[0] ? ` v${evals[0].benchmark_version} · ${formatInt(evals[0].n_items)} éléments · ${evals[0].nbits} bits` : ""}. Cliquez une colonne pour le détail.`}
        />
        <Heatmap
          rows={attacks}
          columns={columns}
          values={matrix}
          min={rMetric === "bit_acc" ? 0.5 : 0}
          max={1}
          formatter={(v) => formatMetric(rMetric, v)}
          onCellClick={(_, col) => setFocus(col)}
        />
      </Card>

      <Card padding="none" className="overflow-hidden">
        <div className="px-5 pt-4">
          <CardHeader title="Qualité et synthèse" description="Moyennes calculées hors attaque « none » (identité, sans attaque)." className="mb-3" />
        </div>
        <Table
          bare
          columns={tableColumns}
          rows={evals}
          rowKey={(e) => `${e.checkpoint}__${e.benchmark}`}
          onRowClick={(e) => setFocus(e.checkpoint)}
          rowClassName={(e) => (focused && e.checkpoint === focused.checkpoint ? "bg-accent-soft/40" : undefined)}
          dense
        />
      </Card>

      {focused && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Card className="xl:col-span-2">
            <CardHeader title={`Détail · ${focused.checkpoint}`} description="Toutes les métriques de robustesse, attaque par attaque." />
            <Heatmap
              rows={Object.keys(focused.robustness)}
              columns={rKeys}
              values={Object.keys(focused.robustness).map((a) => rKeys.map((k) => focused.robustness[a]?.[k] ?? null))}
              min={0}
              max={1}
              formatter={(v, _r, col) => formatMetric(col, v)}
            />
          </Card>
          <Card>
            <CardHeader title="Qualité" />
            <KeyValue
              items={[
                ...Object.entries(focused.quality).map(([k, v]) => ({ label: metricLabel(k), value: formatMetric(k, v) })),
                { label: "Durée", value: formatDuration(focused.duration_s) },
                { label: "Items", value: formatInt(focused.n_items) },
                { label: "Évalué", value: formatRelative(focused.created_at) },
              ]}
            />
          </Card>
        </div>
      )}
    </div>
  );
}
