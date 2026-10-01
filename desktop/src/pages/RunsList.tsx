import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import { CircleAlert, GitCompareArrows, Rocket, Search, X } from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Input,
  Page,
  PageHeader,
  Select,
  StatusBadge,
  Switch,
  Table,
  type Column,
} from "../components/ui";
import { MetricValue, RunProgress } from "../components/RunBits";
import { useLaunch } from "../components/launch/LaunchProvider";
import { errorMessage } from "../lib/api";
import { formatEur, formatRelative, plural, shortId, toDate } from "../lib/format";
import { useNow } from "../lib/hooks";
import { qualityMetricFor } from "../lib/metrics";
import { useHosts, useMe, useProjects, useRuns } from "../lib/queries";
import { isPrunedTrial, progressFraction, runPath, RUN_STATUSES, STATUS_LABELS } from "../lib/runs";
import type { RunSummary } from "../lib/types";

/** Column classes hiding a secondary column under the `xl` breakpoint (1280 px). */
const NARROW_HIDDEN = { className: "hidden xl:table-cell", headerClassName: "hidden xl:table-cell" };

export default function RunsList() {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const { openLaunch } = useLaunch();
  const now = useNow(30000);
  const [selected, setSelected] = useState<string[]>([]);

  const project = params.get("project") ?? "";
  const status = params.get("status") ?? "";
  const host = params.get("host") ?? "";
  const search = params.get("q") ?? "";
  const includeTrials = params.get("trials") === "1";
  const mineOnly = params.get("mine") === "1";
  const me = useMe();
  const myName = me.data?.name ?? null;

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    setParams(next, { replace: true });
  };

  const runs = useRuns({
    project: project || undefined,
    status: status || undefined,
    host: host || undefined,
    include_trials: includeTrials,
    limit: 500,
  });
  const projects = useProjects();
  const hosts = useHosts();

  const filtered = useMemo(() => {
    let list = runs.data ?? [];
    // "Mes runs": compares the author recorded at launch with the current user (/api/me).
    if (mineOnly && myName) list = list.filter((r) => r.user === myName);
    const q = search.trim().toLowerCase();
    if (!q) return list;
    return list.filter((r) =>
      [r.run_id, r.experiment, r.project, r.host, r.user ?? "", ...r.tags, r.study ?? ""].some((s) => s.toLowerCase().includes(q)),
    );
  }, [runs.data, search, mineOnly, myName]);

  const nRunning = filtered.filter((r) => r.status === "running").length;
  const hasFilters = !!(project || status || host || search || includeTrials || mineOnly);

  const columns: Column<RunSummary>[] = [
    {
      id: "status",
      header: "Statut",
      width: 110,
      cell: (r) => <StatusBadge status={r.status} pruned={isPrunedTrial(r)} />,
      sortValue: (r) => RUN_STATUSES.indexOf(r.status),
    },
    {
      id: "run",
      header: "Run",
      // The only flexible column: it absorbs the remaining width and truncates, but never
      // below 180 px (the table then scrolls horizontally instead of crushing the name).
      className: "w-full min-w-[180px] max-w-0",
      headerClassName: "w-full min-w-[180px]",
      cell: (r) => (
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="truncate font-medium text-fg">{r.experiment}</span>
            {r.trial != null && <Badge tone="info">essai {r.trial}</Badge>}
            {r.parent && <Badge tone="neutral" title={`Branché depuis ${r.parent.run_id}`}>branche</Badge>}
          </div>
          <div className="truncate text-[11.5px] text-muted">
            <span className="font-mono">{shortId(r.run_id)}</span>
            {r.user && <span className="ml-1.5">· par {r.user}</span>}
            {r.tags.slice(0, 3).map((t) => (
              <span key={t} className="ml-1.5 text-[11px] text-muted">
                #{t}
              </span>
            ))}
          </div>
        </div>
      ),
      sortValue: (r) => r.experiment,
    },
    // Secondary columns: hidden under 1280 px (the minimum window width is 1000 px).
    { id: "project", header: "Projet", cell: (r) => <span className="text-muted">{r.project}</span>, sortValue: (r) => r.project, ...NARROW_HIDDEN },
    { id: "host", header: "Machine", cell: (r) => <span className="text-muted">{r.host}</span>, sortValue: (r) => r.host, ...NARROW_HIDDEN },
    {
      id: "progress",
      header: "Progression",
      width: 120,
      cell: (r) => <RunProgress progress={r.progress} status={r.status} />,
      sortValue: (r) => progressFraction(r.progress),
    },
    {
      id: "bitacc",
      header: "Bit acc.",
      align: "right",
      title: "val/bit_acc_mean — moyenne sur les attaques de validation",
      cell: (r) => <MetricValue metric="val/bit_acc_mean" value={r.summary["val/bit_acc_mean"]} />,
      sortValue: (r) => r.summary["val/bit_acc_mean"],
    },
    {
      id: "quality",
      header: "Qualité",
      align: "right",
      title: "Métrique de qualité par défaut de la modalité (PSNR / SNR)",
      cell: (r) => {
        const k = `val/${qualityMetricFor(r.modality)}`;
        return <MetricValue metric={k} value={r.summary[k]} />;
      },
      sortValue: (r) => r.summary[`val/${qualityMetricFor(r.modality)}`],
    },
    {
      id: "cost",
      header: "Coût",
      align: "right",
      cell: (r) => <span className="tabular text-muted">{formatEur(r.cost_eur)}</span>,
      sortValue: (r) => r.cost_eur,
    },
    {
      id: "date",
      header: "Date",
      align: "right",
      cell: (r) => <span className="whitespace-nowrap text-muted">{formatRelative(r.created_at, now)}</span>,
      sortValue: (r) => toDate(r.created_at)?.getTime(),
    },
  ];

  return (
    <Page width="wide">
      <PageHeader
        title="Runs"
        description="Tous les entraînements du workspace, locaux et distants."
        actions={
          <Button variant="primary" icon={Rocket} onClick={() => openLaunch()}>
            Nouveau lancement
          </Button>
        }
      />

      <div className="mb-4 flex flex-wrap items-end gap-3">
        <Input
          icon={Search}
          size="sm"
          placeholder="Rechercher (run, expérience, tag…)"
          value={search}
          onChange={(e) => setParam("q", e.target.value)}
          className="w-60"
        />
        <Select
          size="sm"
          value={project}
          onChange={(v) => setParam("project", v)}
          placeholder="Tous les projets"
          options={(projects.data ?? []).map((p) => ({ value: p.name, label: p.title || p.name }))}
          className="w-44"
        />
        <Select
          size="sm"
          value={status}
          onChange={(v) => setParam("status", v)}
          placeholder="Tous les statuts"
          options={RUN_STATUSES.map((s) => ({ value: s, label: STATUS_LABELS[s] }))}
          className="w-36"
        />
        <Select
          size="sm"
          value={host}
          onChange={(v) => setParam("host", v)}
          placeholder="Toutes les machines"
          options={(hosts.data ?? []).map((h) => ({ value: h.name, label: h.name }))}
          className="w-44"
        />
        <Switch size="sm" checked={includeTrials} onChange={(v) => setParam("trials", v ? "1" : "")} label="Essais HPO" className="mb-1.5" />
        {myName && <Switch size="sm" checked={mineOnly} onChange={(v) => setParam("mine", v ? "1" : "")} label="Mes runs" className="mb-1.5" />}
        {hasFilters && (
          <Button size="sm" variant="ghost" icon={X} onClick={() => setParams(new URLSearchParams(), { replace: true })} className="mb-0.5">
            Réinitialiser
          </Button>
        )}
        <div className="mb-1.5 ml-auto flex items-center gap-3 text-[12.5px] text-muted">
          {runs.data && (
            <span>
              {plural(filtered.length, "run")}
              {nRunning > 0 && ` · ${nRunning} en cours`}
            </span>
          )}
        </div>
      </div>

      {selected.length > 0 && (
        <div className="mb-3 flex animate-fade-in items-center gap-3 rounded-xl border border-accent/25 bg-accent-soft/60 px-4 py-2 text-[13px]">
          <span className="text-fg">{plural(selected.length, "run sélectionné", "runs sélectionnés")}</span>
          <Button
            size="sm"
            variant="primary"
            icon={GitCompareArrows}
            disabled={selected.length < 2}
            onClick={() => navigate(`/compare?runs=${selected.map(encodeURIComponent).join(",")}`)}
          >
            Comparer
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setSelected([])}>
            Désélectionner
          </Button>
          {selected.length < 2 && <span className="text-muted">Sélectionnez au moins deux runs pour les comparer.</span>}
        </div>
      )}

      {runs.isError && (
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Impossible de charger les runs"
          className="mb-4"
          action={
            <Button size="sm" onClick={() => void runs.refetch()}>
              Réessayer
            </Button>
          }
        >
          {errorMessage(runs.error)}
        </Callout>
      )}

      <Table
        columns={columns}
        rows={filtered}
        rowKey={(r) => r.run_id}
        onRowClick={(r, e) => {
          if (e.metaKey || e.ctrlKey) {
            setSelected((s) => (s.includes(r.run_id) ? s.filter((x) => x !== r.run_id) : [...s, r.run_id]));
          } else navigate(runPath(r.run_id));
        }}
        selectable
        selected={selected}
        onSelectionChange={setSelected}
        loading={runs.isLoading}
        dense
        initialSort={{ id: "date", desc: true }}
        empty={
          hasFilters ? (
            "Aucun run ne correspond à ces filtres."
          ) : (
            <span>
              Aucun run pour l'instant.{" "}
              <button type="button" className="text-accent hover:underline" onClick={() => openLaunch()}>
                Lancer un entraînement
              </button>
            </span>
          )
        }
      />
    </Page>
  );
}
