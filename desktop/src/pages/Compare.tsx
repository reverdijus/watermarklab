import clsx from "clsx";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { useQueries } from "@tanstack/react-query";
import { ChartLine, CircleAlert, GitCompareArrows, X } from "lucide-react";
import {
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  Page,
  PageHeader,
  Select,
  Skeleton,
  StatusBadge,
  Switch,
  Table,
  Tooltip,
  type Column,
} from "../components/ui";
import { LineChart, needsDash, seriesColor, useChartTokens, type LineSeries } from "../components/charts";
import { differingParams, intersectKeys } from "../components/analysis";
import { api, errorMessage } from "../lib/api";
import { formatDuration, formatEur, formatNumber, shortId } from "../lib/format";
import { formatMetric, formatMetricAxis, groupMetricKeys, isBetter, metricDirection, metricLabel, qualityMetricFor, smoothSeries } from "../lib/metrics";
import { LIVE_INTERVAL, qk, useCompare, usePermissions, useRuns } from "../lib/queries";
import { isActiveStatus, runDuration, runPath } from "../lib/runs";
import type { RunDetail } from "../lib/types";

const fmtParam = (v: unknown): string =>
  v === undefined ? "—" : v === null ? "null" : typeof v === "number" ? formatNumber(v, { maxDigits: 6 }) : typeof v === "boolean" ? (v ? "oui" : "non") : String(v);

interface RowDef {
  key: string;
  label: string;
  mono?: boolean;
  values: (string | number | null)[];
  /** Index of the best run (summaries), when defined. */
  best?: number | null;
  format?: (v: number | null) => string;
}

function ComparisonTable({ runs, rows, colors, empty }: { runs: RunDetail[]; rows: RowDef[]; colors: string[]; empty: string }) {
  const columns: Column<RowDef>[] = [
    {
      id: "key",
      header: "Clé",
      cell: (r) => (
        <Tooltip content={<span className="font-mono">{r.key}</span>} side="right">
          <span className={clsx("text-fg", r.mono && "font-mono text-[12px]")}>{r.label}</span>
        </Tooltip>
      ),
      sortValue: (r) => r.label,
    },
    ...runs.map<Column<RowDef>>((run, i) => ({
      id: run.run_id,
      header: (
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full" style={{ background: colors[i] }} />
          {shortId(run.run_id)}
        </span>
      ),
      align: "right",
      cell: (r) => {
        const v = r.values[i];
        const text = r.format ? r.format(typeof v === "number" ? v : null) : fmtParam(v);
        return (
          <span className={clsx("tabular", r.best === i ? "font-semibold text-success" : "text-fg", v === null && "text-faint")}>{text}</span>
        );
      },
    })),
  ];
  return <Table bare dense columns={columns} rows={rows} rowKey={(r) => r.key} empty={empty} maxHeight={520} />;
}

export default function Compare() {
  const [params, setParams] = useSearchParams();
  const t = useChartTokens();
  const runIds = useMemo(
    () => [...new Set((params.get("runs") ?? "").split(",").map((s) => decodeURIComponent(s.trim())).filter(Boolean))],
    [params],
  );
  const [showAll, setShowAll] = useState(false);
  const { canViewCode } = usePermissions();
  const [smoothing, setSmoothing] = useState(0);

  const details = useQueries({
    queries: runIds.map((id) => ({
      queryKey: qk.run(id),
      queryFn: () => api.getRun(id),
      refetchInterval: (q: { state: { data?: RunDetail } }) => (isActiveStatus(q.state.data?.status) ? LIVE_INTERVAL : false),
    })),
  });
  const loaded = details.map((d) => d.data).filter((d): d is RunDetail => !!d);
  const failed = details.map((d, i) => (d.isError ? { id: runIds[i], error: d.error } : null)).filter(Boolean) as { id: string; error: unknown }[];
  const loading = details.some((d) => d.isLoading);
  const live = loaded.some((r) => isActiveStatus(r.status));

  const setRuns = (ids: string[]) => {
    const next = new URLSearchParams(params);
    if (ids.length) next.set("runs", ids.join(","));
    else next.delete("runs");
    setParams(next, { replace: true });
  };

  const common = useMemo(() => intersectKeys(loaded.map((r) => r.metric_keys)), [loaded]);
  const keysParam = params.get("keys");
  const selectedKeys = useMemo(() => {
    const fromUrl = (keysParam ?? "").split(",").filter((k) => common.includes(k));
    if (fromUrl.length) return fromUrl;
    const q = qualityMetricFor(loaded[0]?.modality);
    const preferred = ["val/bit_acc_mean", `val/${q}`, "train/loss"].filter((k) => common.includes(k));
    return preferred.length ? preferred : common.slice(0, 2);
  }, [keysParam, common, loaded]);
  const setKeys = (keys: string[]) => {
    const next = new URLSearchParams(params);
    if (keys.length) next.set("keys", keys.join(","));
    else next.delete("keys");
    setParams(next, { replace: true });
  };
  const toggleKey = (key: string) =>
    setKeys(selectedKeys.includes(key) ? selectedKeys.filter((k) => k !== key) : [...selectedKeys, key]);

  const compare = useCompare(selectedKeys.length ? loaded.map((r) => r.run_id) : [], selectedKeys, { maxPoints: 600, live });
  const colors = loaded.map((_, i) => seriesColor(t, i));
  const runLabel = (r: RunDetail) => `${r.experiment} · ${shortId(r.run_id)}`;

  const candidates = useRuns({ limit: 100 });
  const addOptions = (candidates.data ?? [])
    .filter((r) => !runIds.includes(r.run_id))
    .map((r) => ({ value: r.run_id, label: `${r.project} · ${r.experiment} · ${shortId(r.run_id)}` }));

  // Params that differ (or all), plus a few context rows. A member gets no params (§9):
  // context rows only.
  const paramKeys = !canViewCode ? [] : showAll ? [...new Set(loaded.flatMap((r) => Object.keys(r.params ?? {})))].sort() : differingParams(loaded);
  const contextRows: RowDef[] = ([
    { key: "project", label: "Projet", values: loaded.map((r) => r.project) },
    { key: "experiment", label: "Expérience", values: loaded.map((r) => r.experiment) },
    { key: "host", label: "Machine", values: loaded.map((r) => r.host) },
    { key: "duration", label: "Durée", values: loaded.map((r) => runDuration(r)), format: (v: number | null) => formatDuration(v) },
    { key: "cost", label: "Coût", values: loaded.map((r) => r.cost_eur), format: (v: number | null) => formatEur(v) },
  ] as RowDef[]).filter((row) => showAll || new Set(row.values.map(String)).size > 1 || row.key === "experiment");
  const paramRows: RowDef[] = [
    ...contextRows,
    ...paramKeys.map((key) => ({
      key,
      label: key,
      mono: true,
      values: loaded.map((r) => {
        const v = r.params?.[key];
        return v === undefined ? null : typeof v === "number" ? v : v === null ? "null" : String(v);
      }),
      format: undefined,
    })),
  ];

  const summaryKeys = groupMetricKeys([...new Set(loaded.flatMap((r) => Object.keys(r.summary ?? {})))].filter((k) => k !== "step")).flatMap(
    (g) => g.keys,
  );
  const summaryRows: RowDef[] = summaryKeys.map((key) => {
    const values = loaded.map((r) => (typeof r.summary?.[key] === "number" ? r.summary[key] : null));
    const dir = metricDirection(key);
    let best: number | null = null;
    if (dir && values.filter((v) => v !== null).length > 1) {
      values.forEach((v, i) => {
        if (v !== null && (best === null || isBetter(v, values[best]!, dir))) best = i;
      });
    }
    return { key, label: metricLabel(key, { withGroup: true }), values, best, format: (v) => formatMetric(key, v) };
  });

  const seriesFor = (key: string): LineSeries[] =>
    loaded.flatMap((r, i) => {
      const s = compare.data?.series[r.run_id]?.[key];
      if (!s) return [];
      return [{ name: runLabel(r), x: s.steps, y: smoothing > 0 ? smoothSeries(s.values, smoothing) : s.values, color: colors[i], dashed: needsDash(i, t) }];
    });

  const header = (
    <PageHeader
      back={{ to: "/runs", label: "Runs" }}
      title="Comparer des runs"
      description="Courbes superposées sur les clés communes, paramètres qui diffèrent et résumés côte à côte."
    />
  );

  if (runIds.length < 2 && !loading) {
    return (
      <Page width="wide">
        {header}
        <Card padding="none">
          <EmptyState
            icon={GitCompareArrows}
            title="Sélectionnez au moins deux runs"
            description="Dans la liste des runs, cochez plusieurs lignes (ou ⌘-clic) puis « Comparer »."
            action={
              <>
                <Button variant="primary" to="/runs">
                  Choisir des runs
                </Button>
                {addOptions.length > 0 && (
                  <Select
                    size="md"
                    value=""
                    onChange={(v) => v && setRuns([...runIds, v])}
                    placeholder="Ajouter un run…"
                    options={addOptions}
                    className="w-72"
                  />
                )}
              </>
            }
          />
        </Card>
      </Page>
    );
  }

  return (
    <Page width="wide">
      {header}

      <div className="mb-6 flex flex-wrap items-center gap-2">
        {runIds.map((id) => {
          const r = loaded.find((x) => x.run_id === id);
          const i = loaded.findIndex((x) => x.run_id === id);
          return (
            <span key={id} className="inline-flex h-8 items-center gap-2 rounded-full border border-border bg-surface pl-3 pr-1.5 text-[13px] shadow-sm">
              <span className="h-2.5 w-2.5 rounded-full" style={{ background: i >= 0 ? colors[i] : "var(--faint)" }} />
              {r ? (
                <Link to={runPath(id)} className="no-drag hover:underline">
                  {r.experiment} <span className="font-mono text-[11.5px] text-muted">{shortId(id)}</span>
                </Link>
              ) : (
                <span className="font-mono text-[12px] text-muted">{shortId(id)}</span>
              )}
              {r && <StatusBadge status={r.status} dotOnly />}
              <button
                type="button"
                aria-label="Retirer de la comparaison"
                className="no-drag rounded-full p-1 text-faint hover:bg-hover hover:text-fg"
                onClick={() => setRuns(runIds.filter((x) => x !== id))}
              >
                <X size={12} strokeWidth={2} />
              </button>
            </span>
          );
        })}
        {addOptions.length > 0 && (
          <Select size="sm" value="" onChange={(v) => v && setRuns([...runIds, v])} placeholder="Ajouter un run…" options={addOptions} className="w-64" />
        )}
      </div>

      {failed.length > 0 && (
        <Callout tone="danger" icon={CircleAlert} title="Certains runs n'ont pas pu être chargés" className="mb-6">
          {failed.map((f) => `${shortId(f.id)} : ${errorMessage(f.error)}`).join(" · ")}
        </Callout>
      )}

      {loading && !loaded.length ? (
        <div className="space-y-4">
          <Skeleton className="h-10 w-2/3" />
          <Skeleton className="h-72 rounded-xl" />
        </div>
      ) : (
        <div className="space-y-6">
          <Card>
            <CardHeader
              title="Courbes"
              icon={ChartLine}
              description={`${common.length} clé${common.length > 1 ? "s" : ""} commune${common.length > 1 ? "s" : ""} à tous les runs ; choisissez celles à superposer.`}
              actions={
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
              }
            />
            {common.length ? (
              <div className="mb-4 flex max-h-28 flex-wrap gap-1.5 overflow-y-auto">
                {common.map((key) => {
                  const on = selectedKeys.includes(key);
                  return (
                    <Tooltip key={key} content={<span className="font-mono">{key}</span>}>
                      <button
                        type="button"
                        onClick={() => toggleKey(key)}
                        className={clsx(
                          "no-drag h-6 rounded-full border px-2.5 text-[12px] transition-colors",
                          on ? "border-accent/40 bg-accent-soft text-fg" : "border-border text-muted hover:border-border-strong hover:text-fg",
                        )}
                      >
                        {metricLabel(key, { withGroup: true })}
                      </button>
                    </Tooltip>
                  );
                })}
              </div>
            ) : (
              <EmptyState compact title="Aucune clé commune" description="Ces runs n'ont aucune métrique en commun (modalités ou versions différentes)." />
            )}
            {selectedKeys.length > 0 && (
              <div className="grid grid-cols-1 gap-4 2xl:grid-cols-2">
                {selectedKeys.map((key) => (
                  <div key={key} className="rounded-xl border border-border px-3 pt-3">
                    <div className="mb-1 flex items-baseline justify-between gap-3 px-1">
                      <span className="text-[13.5px] font-medium text-fg">{metricLabel(key, { withGroup: true })}</span>
                      <span className="font-mono text-[11px] text-faint">{key}</span>
                    </div>
                    <LineChart
                      height={260}
                      series={seriesFor(key)}
                      loading={compare.isLoading}
                      yFormatter={(v) => formatMetricAxis(key, v)}
                      valueFormatter={(v) => formatMetric(key, v)}
                      empty="Pas de données pour cette clé."
                    />
                  </div>
                ))}
              </div>
            )}
            {compare.isError && (
              <Callout tone="danger" icon={CircleAlert} className="mt-4">
                {errorMessage(compare.error)}
              </Callout>
            )}
          </Card>

          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            <Card padding="none" className="overflow-hidden">
              <div className="px-5 pt-4">
                <CardHeader
                  title={canViewCode ? "Paramètres" : "Contexte"}
                  description={
                    !canViewCode
                      ? showAll
                        ? "Projet, expérience, machine, durée et coût."
                        : "Seules les valeurs qui diffèrent sont affichées."
                      : showAll
                        ? "Tous les paramètres aplatis (run.json)."
                        : "Seuls les paramètres qui diffèrent sont affichés."
                  }
                  actions={<Switch size="sm" checked={showAll} onChange={setShowAll} label="Tout afficher" />}
                  className="mb-3"
                />
              </div>
              <ComparisonTable runs={loaded} rows={paramRows} colors={colors} empty={canViewCode ? "Tous les paramètres sont identiques." : "Rien ne diffère."} />
            </Card>
            <Card padding="none" className="overflow-hidden">
              <div className="px-5 pt-4">
                <CardHeader title="Résumés" description="Dernières valeurs de validation et meilleures valeurs ; le meilleur run est en vert." className="mb-3" />
              </div>
              <ComparisonTable runs={loaded} rows={summaryRows} colors={colors} empty="Aucun résumé disponible." />
            </Card>
          </div>
        </div>
      )}
    </Page>
  );
}
