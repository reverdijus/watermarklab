import clsx from "clsx";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import type { EChartsOption } from "echarts";
import { CalendarClock, CircleAlert, Download, Flame, PiggyBank, Plus, Trash2, TrendingUp, Wallet } from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  IconButton,
  Input,
  Kpi,
  Page,
  PageHeader,
  Section,
  Segmented,
  Select,
  Skeleton,
  Table,
  useConfirm,
  useToast,
  type Column,
} from "../components/ui";
import { BarChart, EChart, axisBase, escapeHtml, legendBase, tooltipBase, useChartTokens, withAlpha } from "../components/charts";
import { burnup, projectedSpend } from "../components/analysis";
import { costsCsvUrl, errorMessage } from "../lib/api";
import { formatDate, formatDay, formatEur, formatHours, formatMonth, formatPercent, plural, shortId, toDate } from "../lib/format";
import { useNow } from "../lib/hooks";
import { useCosts, useProjects, useSaveSettings, useSettings } from "../lib/queries";
import { runPath, studyPath } from "../lib/runs";
import type { CostItem, Costs, WorkspaceSettings } from "../lib/types";

type KindFilter = "all" | CostItem["kind"];
const KIND_LABELS: Record<CostItem["kind"], string> = { run: "Run", study: "Étude HPO", eval: "Évaluation", manual: "Saisie manuelle" };
const KIND_TONES: Record<CostItem["kind"], "neutral" | "info" | "accent" | "warning"> = {
  run: "neutral",
  study: "info",
  eval: "accent",
  manual: "warning",
};

const BUTTON_LOOK =
  "no-drag inline-flex h-9 shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-[10px] border border-border bg-surface px-3.5 text-[14px] font-medium text-fg shadow-sm transition-colors hover:border-border-strong hover:bg-surface-2";

function BurnupChart({ costs, now }: { costs: Costs; now: number }) {
  const t = useChartTokens();
  const data = useMemo(() => burnup(costs, now), [costs, now]);
  const option = useMemo<EChartsOption>(() => {
    const ax = axisBase(t);
    const labels = data.months.map(formatMonth);
    const fmt = (v: number) => formatEur(v, { digits: 0 });
    return {
      grid: { left: 8, right: 24, top: 40, bottom: 8, containLabel: true },
      legend: { ...legendBase(t) },
      tooltip: {
        ...tooltipBase(t),
        trigger: "axis",
        formatter: (raw: unknown) => {
          const params = (Array.isArray(raw) ? raw : [raw]) as { seriesName: string; value: number | null; name: string; color: string }[];
          if (!params.length) return "";
          const i = labels.indexOf(params[0].name);
          const rows = params
            .filter((p) => p.value !== null && p.value !== undefined)
            .map(
              (p) =>
                `<div style="display:flex;justify-content:space-between;gap:16px"><span style="color:${t.muted}">${escapeHtml(p.seriesName)}</span><span>${escapeHtml(fmt(p.value as number))}</span></div>`,
            )
            .join("");
          const month = i >= 0 ? `<div style="display:flex;justify-content:space-between;gap:16px"><span style="color:${t.muted}">Dépensé ce mois</span><span>${escapeHtml(formatEur(data.monthly[i]))}</span></div>` : "";
          return `<div style="font-weight:600;margin-bottom:4px">${escapeHtml(params[0].name)}</div>${rows}${month}`;
        },
      },
      xAxis: { ...ax, type: "category", data: labels, boundaryGap: false, splitLine: { show: false } },
      yAxis: {
        ...ax,
        type: "value",
        axisLine: { show: false },
        max: (v: { max: number }) => Math.max(v.max, costs.budget.total_eur) * 1.05,
        axisLabel: { ...ax.axisLabel, formatter: (v: number) => formatEur(v, { compact: true, digits: 0 }) },
      },
      series: [
        {
          id: "linear",
          name: "Budget linéaire",
          type: "line",
          data: data.linear,
          showSymbol: false,
          lineStyle: { color: t.muted, width: 1.5, type: [6, 4] },
          itemStyle: { color: t.muted },
          z: 2,
          markLine: {
            silent: true,
            symbol: "none",
            animation: false,
            lineStyle: { color: t.warning, type: [4, 4], width: 1.25 },
            label: { formatter: `Budget total ${fmt(costs.budget.total_eur)}`, color: t.warning, fontSize: 11, position: "insideEndTop" },
            data: [{ yAxis: costs.budget.total_eur }],
          },
        },
        {
          id: "spent",
          name: "Dépensé (cumulé)",
          type: "line",
          data: data.spent,
          showSymbol: true,
          symbolSize: 5,
          lineStyle: { color: t.accent, width: 2.25 },
          itemStyle: { color: t.accent },
          areaStyle: { color: withAlpha(t.accent, 0.1) },
          z: 3,
        },
      ],
    } as EChartsOption;
  }, [t, data, costs.budget.total_eur]);
  return <EChart option={option} height={320} empty={data.months.length ? undefined : "Définissez les dates du budget dans les réglages."} />;
}

function ManualEntries({ settings, projects }: { settings: WorkspaceSettings; projects: { value: string; label: string }[] }) {
  const save = useSaveSettings();
  const confirm = useConfirm();
  const toast = useToast();
  const today = new Date().toISOString().slice(0, 10);
  const [label, setLabel] = useState("");
  const [eur, setEur] = useState("");
  const [date, setDate] = useState(today);
  const [project, setProject] = useState("");
  const [error, setError] = useState<string | null>(null);
  const entries = settings.budget.manual_entries;

  const persist = (manual_entries: WorkspaceSettings["budget"]["manual_entries"]) =>
    save.mutateAsync({ ...settings, budget: { ...settings.budget, manual_entries } });

  const add = async () => {
    const amount = Number(eur.replace(",", ".").replace(/\s/g, ""));
    if (!label.trim()) return setError("Indiquez un libellé.");
    if (!Number.isFinite(amount) || amount === 0) return setError("Indiquez un montant en euros (négatif pour un avoir).");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return setError("Indiquez une date valide.");
    setError(null);
    try {
      await persist([...entries, { label: label.trim(), eur: amount, date, project: project || null }]);
      toast.success("Dépense ajoutée", `${label.trim()} · ${formatEur(amount)}`);
      setLabel("");
      setEur("");
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const remove = async (index: number) => {
    const entry = entries[index];
    const ok = await confirm({
      title: "Supprimer cette dépense ?",
      message: `${entry.label} · ${formatEur(entry.eur)} (${formatDay(entry.date)})`,
      confirmLabel: "Supprimer",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await persist(entries.filter((_, i) => i !== index));
      toast.success("Dépense supprimée");
    } catch (err) {
      toast.error("Suppression impossible", errorMessage(err));
    }
  };

  return (
    <Card>
      <CardHeader
        title="Entrées manuelles"
        icon={PiggyBank}
        description="Dépenses hors runs (stockage, instance oubliée, crédits…) enregistrées dans workspace.yaml."
      />
      {entries.length ? (
        <ul className="mb-4 divide-y divide-border rounded-xl border border-border">
          {entries.map((e, i) => (
            <li key={`${e.label}-${e.date}-${i}`} className="flex items-center gap-3 px-3 py-2 text-[13px]">
              <div className="min-w-0 flex-1">
                <div className="truncate text-fg">{e.label}</div>
                <div className="text-[12px] text-muted">
                  {formatDay(e.date)}
                  {e.project ? ` · ${e.project}` : ""}
                </div>
              </div>
              <span className="tabular shrink-0 text-fg">{formatEur(e.eur)}</span>
              <IconButton icon={Trash2} label="Supprimer" size="sm" onClick={() => void remove(i)} />
            </li>
          ))}
        </ul>
      ) : (
        <p className="mb-4 text-[13px] text-muted">Aucune entrée manuelle.</p>
      )}
      <form
        className="grid grid-cols-2 gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          void add();
        }}
      >
        <Input size="sm" label="Libellé" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="ex. Stockage objet octobre" className="col-span-2" />
        <Input size="sm" label="Montant" value={eur} onChange={(e) => setEur(e.target.value)} suffix="€" inputMode="decimal" />
        <Input size="sm" label="Date" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
        <Select size="sm" label="Projet" value={project} onChange={setProject} placeholder="Aucun" options={projects} className="col-span-2" />
        {error && (
          <Callout tone="danger" icon={CircleAlert} className="col-span-2">
            {error}
          </Callout>
        )}
        <div className="col-span-2">
          <Button type="submit" size="sm" variant="primary" icon={Plus} loading={save.isPending}>
            Ajouter la dépense
          </Button>
        </div>
      </form>
    </Card>
  );
}

function itemLink(item: CostItem): string | null {
  if (item.kind === "run") return runPath(item.id);
  if (item.kind === "study") return studyPath(item.id);
  if (item.kind === "eval") return runPath(item.id.split("/")[0], "evals");
  return null;
}

export default function Budget() {
  const costs = useCosts();
  const settings = useSettings();
  const projects = useProjects();
  const now = useNow(60000);
  const [kind, setKind] = useState<KindFilter>("all");
  const data = costs.data;

  const projection = data ? projectedSpend(data, now) : null;
  const items = useMemo(() => (data?.items ?? []).filter((i) => kind === "all" || i.kind === kind), [data, kind]);
  const period =
    data?.budget.start && data.budget.end ? `du ${formatDay(data.budget.start)} au ${formatDay(data.budget.end)}` : "période non définie";

  const columns: Column<CostItem>[] = [
    {
      id: "kind",
      header: "Type",
      width: 130,
      cell: (i) => <Badge tone={KIND_TONES[i.kind]}>{KIND_LABELS[i.kind]}</Badge>,
      sortValue: (i) => i.kind,
    },
    {
      id: "id",
      header: "Poste",
      cell: (i) => {
        const to = itemLink(i);
        const text = i.kind === "manual" ? (i.label ?? i.id) : i.kind === "eval" ? i.id.split("/").slice(1).join("/") || i.id : shortId(i.id);
        return to ? (
          <Link to={to} className="no-drag font-mono text-[12px] text-fg hover:underline">
            {text}
          </Link>
        ) : (
          <span className="text-fg">{text}</span>
        );
      },
      sortValue: (i) => i.label ?? i.id,
    },
    { id: "project", header: "Projet", cell: (i) => <span className="text-muted">{i.project ?? "—"}</span>, sortValue: (i) => i.project },
    { id: "host", header: "Machine", cell: (i) => <span className="text-muted">{i.host ?? "—"}</span>, sortValue: (i) => i.host },
    {
      id: "date",
      header: "Début",
      align: "right",
      cell: (i) => (
        <span className="whitespace-nowrap text-muted">
          {i.kind === "manual" || /^\d{4}-\d{2}-\d{2}$/.test(i.started_at ?? "") ? formatDay(i.started_at) : formatDate(i.started_at)}
        </span>
      ),
      sortValue: (i) => toDate(i.started_at)?.getTime(),
    },
    {
      id: "hours",
      header: "Heures",
      align: "right",
      cell: (i) => <span className="tabular text-muted">{i.kind === "manual" ? "—" : formatHours(i.hours, 2)}</span>,
      sortValue: (i) => i.hours,
    },
    { id: "eur", header: "Montant", align: "right", cell: (i) => <span className="tabular font-medium">{formatEur(i.eur)}</span>, sortValue: (i) => i.eur },
  ];

  return (
    <Page width="wide">
      <PageHeader
        title="Budget"
        description={`Dépenses GPU ${period} : runs (hors essais HPO), études, évaluations et saisies manuelles.`}
        actions={
          <div className="flex flex-col items-end gap-1">
            <a href={costsCsvUrl()} download="watermark-lab-couts.csv" target="_blank" rel="noreferrer" className={BUTTON_LOOK}>
              <Download size={16} strokeWidth={1.75} aria-hidden />
              Exporter en CSV
            </a>
            <span className="text-[11.5px] text-muted">Utile pour la justification des dépenses BPI</span>
          </div>
        }
      />

      {costs.isError && (
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Impossible de charger les coûts"
          className="mb-6"
          action={
            <Button size="sm" onClick={() => void costs.refetch()}>
              Réessayer
            </Button>
          }
        >
          {errorMessage(costs.error)}
        </Callout>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-3 2xl:grid-cols-5">
        <Kpi icon={Wallet} label="Budget total" value={data ? formatEur(data.total_eur, { digits: 0 }) : "—"} sub={period} loading={costs.isLoading} />
        <Kpi
          icon={TrendingUp}
          label="Dépensé"
          value={data ? formatEur(data.spent_eur, { digits: 0 }) : "—"}
          sub={data && data.total_eur > 0 ? `${formatPercent(data.spent_eur / data.total_eur)} du budget` : undefined}
          progress={data && data.total_eur > 0 ? data.spent_eur / data.total_eur : null}
          progressTone={data && data.spent_eur > data.total_eur ? "danger" : "accent"}
          loading={costs.isLoading}
        />
        <Kpi
          icon={PiggyBank}
          label="Restant"
          value={data ? formatEur(data.remaining_eur, { digits: 0 }) : "—"}
          sub={data && data.remaining_eur < 0 ? "budget dépassé" : "disponible"}
          loading={costs.isLoading}
        />
        <Kpi
          icon={Flame}
          label="Rythme en cours"
          value={data ? `${formatEur(data.running_eur_per_hour)}/h` : "—"}
          sub={data && data.running_eur_per_hour > 0 ? `≈ ${formatEur(data.running_eur_per_hour * 24, { digits: 0 })} par jour` : "aucun job payant actif"}
          loading={costs.isLoading}
        />
        <Kpi
          icon={CalendarClock}
          label="Projection fin de période"
          value={projection?.projected != null ? formatEur(projection.projected, { digits: 0 }) : "—"}
          sub={
            projection
              ? projection.projected != null && data && projection.projected > data.total_eur
                ? `dépassement de ${formatEur(projection.projected - data.total_eur, { digits: 0 })}`
                : `au rythme des 30 derniers jours (${formatEur(projection.dailyRate, { digits: projection.dailyRate < 10 ? 2 : 0 })}/j)`
              : undefined
          }
          progress={projection?.projected != null && data && data.total_eur > 0 ? projection.projected / data.total_eur : undefined}
          progressTone={projection?.projected != null && data && projection.projected > data.total_eur ? "danger" : "success"}
          hint="Dépenses des 30 derniers jours ramenées à un rythme quotidien, prolongé jusqu'à la fin de la période."
          loading={costs.isLoading}
        />
      </div>

      <Card className="mt-6">
        <CardHeader title="Consommation cumulée" description="Dépenses cumulées par mois face à une consommation linéaire du budget entre le début et la fin de la période." />
        {costs.isLoading ? <Skeleton className="h-[320px] rounded-xl" /> : data ? <BurnupChart costs={data} now={now} /> : null}
      </Card>

      {data && (
        <div className="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-3">
          <Card>
            <CardHeader title="Par projet" description={`${formatHours(data.by_project.reduce((s, p) => s + p.gpu_hours, 0))} GPU au total`} />
            <BarChart
              horizontal
              categories={data.by_project.map((p) => p.project)}
              series={[{ name: "Dépensé", values: data.by_project.map((p) => p.eur) }]}
              valueFormatter={(v) => formatEur(v, { digits: 0 })}
              empty="Aucune dépense"
            />
          </Card>
          <Card>
            <CardHeader title="Par machine" description={plural(data.by_host.length, "machine")} />
            <BarChart
              horizontal
              categories={data.by_host.map((h) => h.host)}
              series={[{ name: "Dépensé", values: data.by_host.map((h) => h.eur) }]}
              valueFormatter={(v) => formatEur(v, { digits: 0 })}
              empty="Aucune dépense"
            />
          </Card>
          <Card>
            <CardHeader title="Par mois" description="Dépenses mensuelles" />
            <BarChart
              categories={data.by_month.map((m) => formatMonth(m.month))}
              series={[{ name: "Dépensé", values: data.by_month.map((m) => m.eur) }]}
              valueFormatter={(v) => formatEur(v, { digits: 0 })}
              empty="Aucune dépense"
            />
          </Card>
        </div>
      )}

      <div className="mt-10 grid grid-cols-1 gap-6 xl:grid-cols-3">
        <Section
          title="Postes de dépense"
          className="xl:col-span-2"
          actions={
            <Segmented<KindFilter>
              value={kind}
              onChange={setKind}
              options={[
                { value: "all", label: "Tous" },
                { value: "run", label: "Runs" },
                { value: "study", label: "Études" },
                { value: "eval", label: "Évaluations" },
                { value: "manual", label: "Manuels" },
              ]}
            />
          }
        >
          {data && !data.items.length ? (
            <Card padding="none">
              <EmptyState
                compact
                icon={Wallet}
                title="Aucune dépense enregistrée"
                description="Les runs, études et évaluations apparaissent ici avec leur coût (durée × prix de la machine)."
                action={<Button to="/hosts">Configurer les machines</Button>}
              />
            </Card>
          ) : (
            <Table
              columns={columns}
              rows={items}
              rowKey={(i) => `${i.kind}:${i.id}`}
              loading={costs.isLoading}
              initialSort={{ id: "date", desc: true }}
              dense
              maxHeight={560}
              rowClassName={(i) => clsx(i.eur === 0 && "opacity-70")}
              empty="Aucun poste pour ce filtre."
            />
          )}
        </Section>
        <div>
          {settings.data ? (
            <ManualEntries settings={settings.data} projects={(projects.data ?? []).map((p) => ({ value: p.name, label: p.title || p.name }))} />
          ) : settings.isError ? (
            <Callout tone="danger" icon={CircleAlert} title="Réglages indisponibles">
              {errorMessage(settings.error)}
            </Callout>
          ) : (
            <Skeleton className="h-80 rounded-xl" />
          )}
        </div>
      </div>
    </Page>
  );
}
