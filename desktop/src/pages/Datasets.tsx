import clsx from "clsx";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { CircleAlert, Copy, Database, ExternalLink, FileCode, Plus, Search } from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  Dialog,
  EmptyState,
  IconButton,
  Input,
  ModalityDot,
  Page,
  PageHeader,
  Segmented,
  Skeleton,
  Tooltip,
  copyText,
  useToast,
} from "../components/ui";
import { YamlFileDrawer } from "../components/YamlFileDrawer";
import { experimentFromPath, toIdentifier } from "../components/analysis";
import { errorMessage } from "../lib/api";
import { plural } from "../lib/format";
import { toastFileSaved } from "../components/saveToast";
import { useDatasets, usePermissions, useSaveFile } from "../lib/queries";
import { experimentPath, MODALITY_LABELS } from "../lib/runs";
import type { DatasetInfo, Modality } from "../lib/types";

type ModalityFilter = "all" | Modality;

function datasetTemplate(name: string, modality: Modality): string {
  return `# Catalogue de datasets — format : ARCHITECTURE.md §5.6
# Référence dans une expérience : data.train.params.root: "@${name}"
# Le chemin est résolu selon la machine (WMLAB_HOST) : paths[<machine>], sinon paths.default.
name: ${name}
modality: ${modality}
description: ""
source: ""                # URL de la page officielle du dataset
license: ""               # licence / conditions d'utilisation
paths:
  default: "~/datasets/${name}"
  # scw-l40s: "/root/datasets/${name}"
`;
}

function UsedBy({ paths, links }: { paths: string[]; links: boolean }) {
  if (!paths.length) return <span className="text-faint">Aucune expérience</span>;
  return (
    <span className="flex flex-wrap gap-1.5">
      {paths.map((p) => {
        const exp = experimentFromPath(p);
        const label = exp ? `${exp.project}/${exp.experiment}` : p.replace(/\.ya?ml$/, "");
        const to = !links ? null : exp ? experimentPath(exp.project, exp.experiment) : p.startsWith("benchmarks/") ? "/benchmarks" : null;
        return to ? (
          <Link key={p} to={to} className="no-drag rounded-md bg-surface-2 px-1.5 py-0.5 font-mono text-[11.5px] text-muted hover:text-fg">
            {label}
          </Link>
        ) : (
          <span key={p} className="rounded-md bg-surface-2 px-1.5 py-0.5 font-mono text-[11.5px] text-muted">
            {label}
          </span>
        );
      })}
    </span>
  );
}

/** `onEdit` null: member (read-only, `paths` withheld by the server, §9). */
function DatasetCard({ ds, onEdit }: { ds: DatasetInfo; onEdit: (() => void) | null }) {
  const toast = useToast();
  const ref = `@${ds.name}`;
  const isUrl = /^https?:\/\//i.test(ds.source);
  // Long descriptions (download instructions…) are clamped to 3 lines, with a toggle to read them.
  const [expanded, setExpanded] = useState(false);
  const longDescription = ds.description.length > 180 || ds.description.split("\n").length > 3;
  return (
    <Card className="flex flex-col">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ModalityDot modality={ds.modality} />
            <span className="selectable truncate font-mono text-[14.5px] font-medium text-fg">{ds.name}</span>
            <IconButton
              icon={Copy}
              label={`Copier la référence ${ref}`}
              size="sm"
              onClick={() => void copyText(ref).then((ok) => ok && toast.info("Référence copiée", ref))}
            />
          </div>
          <div className="text-[12px] text-muted">{MODALITY_LABELS[ds.modality] ?? ds.modality}</div>
        </div>
        {ds.valid ? (
          <Badge tone="success">Valide</Badge>
        ) : (
          <Tooltip content={ds.errors.map((e) => `${e.loc ? `${e.loc} : ` : ""}${e.msg}`).join(" · ") || "Entrée invalide"}>
            <Badge tone="danger">{plural(ds.errors.length || 1, "erreur")}</Badge>
          </Tooltip>
        )}
      </div>
      <p className={clsx("selectable mt-2 whitespace-pre-line text-[13px] leading-relaxed text-fg", !expanded && "line-clamp-3")}>
        {ds.description || <span className="text-faint">Sans description</span>}
      </p>
      {longDescription && (
        <button type="button" className="no-drag mt-1 self-start text-[12.5px] font-medium text-accent hover:underline" onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Réduire" : "Lire la suite"}
        </button>
      )}
      <dl className="mt-4 space-y-2 border-t border-border pt-3 text-[12.5px]">
        <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-3">
          <dt className="text-muted">Licence</dt>
          <dd className="selectable text-fg">{ds.license || <span className="text-faint">non renseignée</span>}</dd>
        </div>
        <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-3">
          <dt className="text-muted">Source</dt>
          <dd className="min-w-0 truncate">
            {isUrl ? (
              <a href={ds.source} target="_blank" rel="noreferrer" className="no-drag inline-flex items-center gap-1 text-accent hover:underline">
                {ds.source.replace(/^https?:\/\//, "")}
                <ExternalLink size={12} strokeWidth={1.75} aria-hidden />
              </a>
            ) : (
              <span className="text-fg">{ds.source || <span className="text-faint">non renseignée</span>}</span>
            )}
          </dd>
        </div>
        <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-3">
          <dt className="text-muted">Chemins</dt>
          <dd className="min-w-0 space-y-1">
            {Object.entries(ds.paths).length ? (
              Object.entries(ds.paths).map(([host, path]) => (
                <div key={host} className="flex min-w-0 items-baseline gap-2">
                  <span className="shrink-0 text-muted">{host === "default" ? "par défaut" : host}</span>
                  <span className="selectable truncate font-mono text-[11.5px] text-fg" title={path}>
                    {path}
                  </span>
                </div>
              ))
            ) : !onEdit ? (
              <span className="text-faint" title="Réservé aux administrateurs">—</span>
            ) : (
              <span className="text-faint">aucun</span>
            )}
          </dd>
        </div>
        <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-3">
          <dt className="text-muted">Utilisé par</dt>
          <dd className="min-w-0">
            <UsedBy paths={ds.used_by} links={!!onEdit} />
          </dd>
        </div>
      </dl>
      {onEdit && (
        <div className="mt-auto pt-4">
          <Button size="sm" icon={FileCode} onClick={onEdit}>
            Modifier le YAML
          </Button>
        </div>
      )}
    </Card>
  );
}

function NewDatasetDialog({ open, onClose, taken, onCreated }: { open: boolean; onClose: () => void; taken: string[]; onCreated: (path: string) => void }) {
  const save = useSaveFile();
  const toast = useToast();
  const [name, setName] = useState("");
  const [modality, setModality] = useState<Modality>("image");
  const [error, setError] = useState<string | null>(null);
  const clean = toIdentifier(name);
  const nameError = clean && taken.includes(clean) ? "Ce nom existe déjà dans le catalogue." : undefined;

  const submit = async () => {
    if (!clean) return setError("Indiquez un nom de dataset.");
    if (nameError) return setError(nameError);
    setError(null);
    const path = `datasets/${clean}.yaml`;
    try {
      const res = await save.mutateAsync({ path, content: datasetTemplate(clean, modality) });
      toastFileSaved(toast, res, "Dataset ajouté au catalogue", clean);
      setName("");
      onClose();
      onCreated(path);
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Nouveau dataset"
      description="Ajoute une entrée au catalogue (aucune donnée n'est téléchargée) : vous complèterez ensuite chemins, licence et source."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={save.isPending}>
            Créer
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Input
          label="Nom"
          autoFocus
          mono
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="ex. div2k_train"
          hint={clean ? `Référence : @${clean}` : "Utilisé comme référence « @nom » dans les expériences."}
          error={nameError}
        />
        <div>
          <div className="mb-1.5 text-[13px] font-medium text-fg">Modalité</div>
          <Segmented<Modality>
            size="md"
            value={modality}
            onChange={setModality}
            options={(["image", "audio", "video"] as Modality[]).map((m) => ({ value: m, label: MODALITY_LABELS[m] }))}
          />
        </div>
        {error && (
          <Callout tone="danger" icon={CircleAlert}>
            {error}
          </Callout>
        )}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

export default function Datasets() {
  const datasets = useDatasets();
  const { canWrite } = usePermissions();
  const [search, setSearch] = useState("");
  const [modality, setModality] = useState<ModalityFilter>("all");
  const [editing, setEditing] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const list = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (datasets.data ?? [])
      .filter((d) => modality === "all" || d.modality === modality)
      .filter((d) => !q || [d.name, d.description, d.license, d.source].some((s) => s.toLowerCase().includes(q)))
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [datasets.data, search, modality]);
  const editingInfo = datasets.data?.find((d) => d.path === editing);

  return (
    <Page width="wide">
      <PageHeader
        title="Datasets"
        description="Catalogue des données : chemins par machine, licence et source. Une expérience y fait référence avec « @nom »."
        actions={
          canWrite ? (
            <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
              Nouveau dataset
            </Button>
          ) : undefined
        }
      />

      <div className="mb-5 flex flex-wrap items-center gap-3">
        <Input icon={Search} size="sm" placeholder="Rechercher un dataset" value={search} onChange={(e) => setSearch(e.target.value)} className="w-72" />
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
        {datasets.data && <span className="ml-auto text-[12.5px] text-muted">{plural(list.length, "dataset")}</span>}
      </div>

      {datasets.isError ? (
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Impossible de charger le catalogue"
          action={
            <Button size="sm" onClick={() => void datasets.refetch()}>
              Réessayer
            </Button>
          }
        >
          {errorMessage(datasets.error)}
        </Callout>
      ) : datasets.isLoading ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-64 rounded-xl" />
          ))}
        </div>
      ) : list.length ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {list.map((d) => (
            <DatasetCard key={d.path} ds={d} onEdit={canWrite ? () => setEditing(d.path) : null} />
          ))}
        </div>
      ) : (
        <Card padding="none">
          <EmptyState
            icon={Database}
            title={search || modality !== "all" ? "Aucun dataset ne correspond" : "Catalogue vide"}
            description={
              search || modality !== "all"
                ? "Modifiez la recherche ou le filtre de modalité."
                : "Déclarez vos jeux de données (COCO, LibriSpeech…) avec leurs chemins sur chaque machine."
            }
            action={
              canWrite ? (
                <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
                  Nouveau dataset
                </Button>
              ) : undefined
            }
          />
        </Card>
      )}

      {canWrite && (
      <>
      <NewDatasetDialog
        open={creating}
        onClose={() => setCreating(false)}
        taken={(datasets.data ?? []).map((d) => d.name)}
        onCreated={(path) => setEditing(path)}
      />
      <YamlFileDrawer
        open={!!editing}
        onClose={() => setEditing(null)}
        path={editing}
        kind="dataset"
        title={editingInfo ? `Dataset ${editingInfo.name}` : "Dataset"}
      />
      </>
      )}
    </Page>
  );
}
