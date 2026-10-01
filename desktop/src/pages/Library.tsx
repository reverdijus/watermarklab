import { useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import { useQueryClient } from "@tanstack/react-query";
import { Blocks, CircleAlert, PackagePlus, RefreshCw, Search, TriangleAlert } from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  CodeBlock,
  EmptyState,
  Input,
  Page,
  PageHeader,
  Segmented,
  Skeleton,
  Table,
  Tabs,
  useToast,
  type Column,
  type TabItem,
} from "../components/ui";
import { schemaParams, type ParamRow } from "../components/analysis";
import { errorMessage, request } from "../lib/api";
import { plural } from "../lib/format";
import { qk, useRegistry } from "../lib/queries";
import { MODALITY_LABELS } from "../lib/runs";
import type { Modality, RegistryEntry, RegistryResponse } from "../lib/types";

type Kind = "model" | "attack" | "dataset" | "preprocess" | "loss" | "metric";
const KINDS: { id: Kind; label: string; plugin: string; example: string }[] = [
  { id: "model", label: "Modèles", plugin: "models", example: "mon_modele" },
  { id: "attack", label: "Attaques", plugin: "attacks", example: "mon_attaque" },
  { id: "dataset", label: "Datasets", plugin: "datasets", example: "mon_dataset" },
  { id: "preprocess", label: "Prétraitements", plugin: "preprocess", example: "mon_pretraitement" },
  { id: "loss", label: "Pertes", plugin: "losses", example: "ma_perte" },
  { id: "metric", label: "Métriques", plugin: "metrics", example: "ma_metrique" },
];
type ModalityFilter = "all" | Modality;

const paramColumns: Column<ParamRow>[] = [
  {
    id: "name",
    header: "Paramètre",
    cell: (p) => (
      <span className="font-mono text-[12px] text-fg">
        {p.name}
        {p.required && <span className="text-danger">*</span>}
      </span>
    ),
  },
  { id: "type", header: "Type", wrap: true, cell: (p) => <span className="text-muted">{p.type}</span> },
  { id: "default", header: "Défaut", cell: (p) => <span className="font-mono text-[12px]">{p.default}</span> },
  { id: "bounds", header: "Bornes", cell: (p) => <span className="tabular text-muted">{p.bounds || "—"}</span> },
  {
    id: "desc",
    header: "Description",
    wrap: true,
    className: "w-full min-w-[160px]",
    headerClassName: "w-full",
    cell: (p) => <span className="text-muted">{p.description || "—"}</span>,
  },
];

function EntryCard({ entry }: { entry: RegistryEntry }) {
  const params = schemaParams(entry.params_schema);
  return (
    <Card padding="none" className="overflow-hidden">
      <div className="px-5 pb-3 pt-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="selectable font-mono text-[14px] font-medium text-fg">{entry.name}</span>
          {entry.builtin ? <Badge tone="neutral">intégré</Badge> : <Badge tone="accent">plugin</Badge>}
          {entry.modality === null ? (
            <Badge tone="neutral" variant="outline">
              toutes modalités
            </Badge>
          ) : (
            entry.modality.map((m) => (
              <Badge key={m} tone="info" variant="outline">
                {MODALITY_LABELS[m as Modality] ?? m}
              </Badge>
            ))
          )}
        </div>
        <p className="mt-1.5 text-[13px] leading-relaxed text-fg">{entry.description || <span className="text-faint">Sans description</span>}</p>
        <div className="selectable mt-1 truncate font-mono text-[11px] text-faint" title={entry.source}>
          {entry.source}
        </div>
      </div>
      {params.length > 0 ? (
        <div className="border-t border-border">
          <Table
            bare
            dense
            columns={params.some((p) => p.bounds) ? paramColumns : paramColumns.filter((c) => c.id !== "bounds")}
            rows={params}
            rowKey={(p) => p.name}
          />
        </div>
      ) : (
        <div className="border-t border-border px-5 py-2.5 text-[12px] text-faint">Aucun paramètre</div>
      )}
    </Card>
  );
}

export default function Library() {
  const registry = useRegistry();
  const queryClient = useQueryClient();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [modality, setModality] = useState<ModalityFilter>("all");
  const [reloading, setReloading] = useState(false);
  const kindParam = params.get("kind") as Kind | null;
  const kind: Kind = kindParam && KINDS.some((k) => k.id === kindParam) ? kindParam : "model";
  const setKind = (k: Kind) => setParams(k === "model" ? new URLSearchParams() : new URLSearchParams({ kind: k }), { replace: true });

  const entries = registry.data?.entries ?? {};
  const pluginErrors = registry.data?.errors ?? [];
  const list = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (entries[kind] ?? [])
      .filter((e) => modality === "all" || e.modality === null || e.modality.includes(modality))
      .filter((e) => !q || [e.name, e.description, e.source].some((s) => s.toLowerCase().includes(q)))
      .sort((a, b) => Number(a.builtin) - Number(b.builtin) || a.name.localeCompare(b.name));
  }, [entries, kind, modality, search]);

  const reload = async () => {
    setReloading(true);
    try {
      const res = await request<RegistryResponse>("GET", "/api/registry", { query: { reload: 1 } });
      queryClient.setQueryData(qk.registry, res);
      const n = Object.values(res.entries).reduce((s, l) => s + l.length, 0);
      if (res.errors.length) toast.warning("Plugins rechargés avec des erreurs", plural(res.errors.length, "module en erreur", "modules en erreur"));
      else toast.success("Plugins rechargés", plural(n, "composant"));
    } catch (err) {
      toast.error("Rechargement impossible", errorMessage(err));
    } finally {
      setReloading(false);
    }
  };

  const tabs: TabItem<Kind>[] = KINDS.map((k) => ({ id: k.id, label: k.label, count: entries[k.id]?.length ?? null }));
  const current = KINDS.find((k) => k.id === kind)!;

  return (
    <Page width="wide">
      <PageHeader
        title="Bibliothèque"
        description="Composants enregistrés dans les registres : briques intégrées et plugins de lab/, utilisables dans les expériences par leur nom."
        actions={
          <Button icon={RefreshCw} onClick={() => void reload()} loading={reloading}>
            Recharger les plugins
          </Button>
        }
      />

      {pluginErrors.length > 0 && (
        <Callout tone="danger" icon={TriangleAlert} title={`${plural(pluginErrors.length, "plugin n'a", "plugins n'ont")} pas pu être chargé${pluginErrors.length > 1 ? "s" : ""}`} className="mb-6">
          <div className="mt-2 space-y-2">
            {pluginErrors.map((e) => (
              <div key={e.module}>
                <div className="font-mono text-[12px] text-fg">{e.module}</div>
                <CodeBlock code={e.error} maxHeight={160} wrap className="mt-1" />
              </div>
            ))}
          </div>
        </Callout>
      )}

      {registry.isError && (
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Impossible de charger les registres"
          className="mb-6"
          action={
            <Button size="sm" onClick={() => void registry.refetch()}>
              Réessayer
            </Button>
          }
        >
          {errorMessage(registry.error)}
        </Callout>
      )}

      <Tabs items={tabs} value={kind} onChange={setKind} className="mb-5" />

      <div className="mb-5 flex flex-wrap items-center gap-3">
        <Input
          icon={Search}
          size="sm"
          placeholder="Rechercher (nom, description, module)"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-80"
        />
        <Segmented<ModalityFilter>
          value={modality}
          onChange={setModality}
          options={[
            { value: "all", label: "Toutes" },
            { value: "image", label: "Image" },
            { value: "audio", label: "Audio" },
            { value: "video", label: "Vidéo" },
          ]}
        />
        {registry.data && <span className="ml-auto text-[12.5px] text-muted">{plural(list.length, "composant")}</span>}
      </div>

      {registry.isLoading ? (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-40 rounded-xl" />
          ))}
        </div>
      ) : list.length ? (
        <div className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
          {list.map((e) => (
            <EntryCard key={`${e.kind}:${e.name}`} entry={e} />
          ))}
        </div>
      ) : (
        <Card padding="none">
          <EmptyState
            icon={search || modality !== "all" ? Search : Blocks}
            title={search || modality !== "all" ? "Aucun composant ne correspond" : `Aucun composant « ${current.label.toLowerCase()} »`}
            description={
              search || modality !== "all"
                ? "Modifiez la recherche ou le filtre de modalité."
                : `Ajoutez un plugin dans lab/${current.plugin}/ puis rechargez les plugins.`
            }
            action={
              search || modality !== "all" ? (
                <Button
                  onClick={() => {
                    setSearch("");
                    setModality("all");
                  }}
                >
                  Réinitialiser les filtres
                </Button>
              ) : (
                <Button icon={RefreshCw} onClick={() => void reload()} loading={reloading}>
                  Recharger les plugins
                </Button>
              )
            }
          />
        </Card>
      )}

      <Card muted className="mt-10">
        <div className="flex gap-3">
          <PackagePlus size={18} strokeWidth={1.75} className="mt-0.5 shrink-0 text-muted" aria-hidden />
          <div className="min-w-0 flex-1">
            <div className="text-[14px] font-medium text-fg">Ajouter un composant</div>
            <p className="mt-1 text-[13px] leading-relaxed text-muted">
              Les plugins vivent dans <span className="font-mono">lab/</span> et sont découverts automatiquement. Générez un squelette
              compatible, vérifiez-le (formes, plages, gradients), puis rechargez cette page. Le skill Claude{" "}
              <span className="font-mono">wmlab-experiment</span> guide la création de bout en bout.
            </p>
            <CodeBlock
              className="mt-3"
              code={`.venv/bin/python -m wmlab.cli new plugin ${kind} ${current.example} --modality image\n.venv/bin/python -m wmlab.cli plugin-test ${kind} ${current.example} --modality image`}
            />
          </div>
        </div>
      </Card>
    </Page>
  );
}
