import { useMemo, useState } from "react";
import { CircleAlert, CopyPlus, FileCode, Gauge, Lock, Microscope } from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  Dialog,
  EmptyState,
  Input,
  ModalityDot,
  Page,
  PageHeader,
  Segmented,
  Skeleton,
  Tooltip,
  useToast,
} from "../components/ui";
import { useLaunch } from "../components/launch/LaunchProvider";
import { YamlFileDrawer } from "../components/YamlFileDrawer";
import { nextVersionName, toIdentifier } from "../components/analysis";
import { fmtValue } from "../components/SpecSummaries";
import { setTopLevelScalar } from "../components/yamlData";
import { api, errorMessage } from "../lib/api";
import { formatInt, formatSignificant, plural } from "../lib/format";
import { metricLabel } from "../lib/metrics";
import { toastFileSaved } from "../components/saveToast";
import { useBenchmarks, useLeaderboard, usePermissions, useSaveFile } from "../lib/queries";
import { MODALITY_LABELS } from "../lib/runs";
import type { BenchmarkInfo, Modality } from "../lib/types";

type ModalityFilter = "all" | Modality;

function BenchmarkCard({
  bench,
  nEvals,
  onView,
  onDuplicate,
}: {
  bench: BenchmarkInfo;
  /** Evaluations recorded on this version (null while unknown). */
  nEvals: number | null;
  onView: () => void;
  onDuplicate: () => void;
}) {
  const { openLaunch } = useLaunch();
  const { canWrite } = usePermissions();
  return (
    <Card className="flex flex-col">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <ModalityDot modality={bench.modality} />
            <span className="selectable truncate font-mono text-[14.5px] font-medium text-fg">{bench.name}</span>
            <Badge tone="accent">v{bench.version}</Badge>
          </div>
          <div className="text-[12px] text-muted">
            {MODALITY_LABELS[bench.modality] ?? bench.modality}
            {bench.n_items != null && ` · ${formatInt(bench.n_items)} éléments`}
            {nEvals != null && ` · ${plural(nEvals, "évaluation", undefined, "aucune évaluation")}`}
          </div>
        </div>
        {bench.valid ? (
          <Tooltip content="Version publiée : ne pas modifier">
            <Badge tone="neutral" icon={Lock}>
              figé
            </Badge>
          </Tooltip>
        ) : (
          <Tooltip content={bench.errors.map((e) => `${e.loc ? `${e.loc} : ` : ""}${e.msg}`).join(" · ") || "Suite invalide"}>
            <Badge tone="danger">{plural(bench.errors.length || 1, "erreur")}</Badge>
          </Tooltip>
        )}
      </div>
      <p className="mt-2 line-clamp-2 text-[13px] leading-relaxed text-fg">{bench.description || <span className="text-faint">Sans description</span>}</p>

      <div className="mt-4 space-y-3 border-t border-border pt-3 text-[12.5px]">
        <div>
          <div className="mb-1.5 text-muted">{plural(bench.attacks.length, "attaque")}</div>
          <div className="flex flex-wrap gap-1">
            {bench.attacks.map((a) => (
              <Tooltip key={a.label} content={<span className="font-mono">{`${a.name} ${fmtValue(a.params)}`}</span>}>
                <Badge tone={a.name === "identity" ? "neutral" : "info"} variant="outline">
                  {a.label}
                </Badge>
              </Tooltip>
            ))}
          </div>
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <div className="text-muted">Métriques de qualité</div>
            <div className="mt-0.5 text-fg">{bench.quality_metrics.map((m) => metricLabel(m)).join(", ") || "—"}</div>
          </div>
          <div>
            <div className="text-muted">FPR (TPR à…)</div>
            <div className="tabular mt-0.5 text-fg">{bench.fpr.map((f) => formatSignificant(f, 2)).join(" · ") || "—"}</div>
          </div>
        </div>
      </div>

      {canWrite && (
      <div className="mt-auto flex flex-wrap gap-2 pt-4">
        <Button size="sm" icon={FileCode} onClick={onView}>
          Voir le YAML
        </Button>
        <Button size="sm" icon={CopyPlus} onClick={onDuplicate}>
          Nouvelle version
        </Button>
        <Button size="sm" variant="ghost" icon={Microscope} disabled={!bench.valid} onClick={() => openLaunch({ kind: "eval", benchmark: bench.name })}>
          Évaluer
        </Button>
      </div>
      )}
    </Card>
  );
}

function NewVersionDialog({
  source,
  existing,
  onClose,
  onCreated,
}: {
  source: BenchmarkInfo;
  existing: string[];
  onClose: () => void;
  onCreated: (path: string) => void;
}) {
  const save = useSaveFile();
  const toast = useToast();
  const proposal = nextVersionName(source.name, source.version, existing);
  const [name, setName] = useState(proposal.name);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const clean = toIdentifier(name);
  const versionFromName = /_v(\d+)$/.exec(clean);
  const version = versionFromName ? Number(versionFromName[1]) : source.version + 1;
  const nameError = clean && existing.includes(clean) ? "Ce benchmark existe déjà." : undefined;

  const submit = async () => {
    if (!clean) return setError("Indiquez un nom.");
    if (nameError) return setError(nameError);
    setError(null);
    setBusy(true);
    try {
      const file = await api.getFile(source.path);
      let content = setTopLevelScalar(file.content, "name", clean);
      content = setTopLevelScalar(content, "version", String(version));
      const path = `benchmarks/${clean}.yaml`;
      const res = await save.mutateAsync({ path, content });
      toastFileSaved(toast, res, "Nouvelle version créée", `${clean} (v${version})`);
      onClose();
      onCreated(path);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Nouvelle version du benchmark"
      description={`Copie de ${source.name} (v${source.version}) à modifier librement tant qu'aucune évaluation ne l'utilise.`}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={busy}>
            Créer et éditer
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
          label="Nom de la nouvelle version"
          autoFocus
          mono
          value={name}
          onChange={(e) => setName(e.target.value)}
          hint={clean ? `Fichier benchmarks/${clean}.yaml · version ${version}` : undefined}
          error={nameError}
        />
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

export default function Benchmarks() {
  const benchmarks = useBenchmarks();
  const { canWrite } = usePermissions();
  const [modality, setModality] = useState<ModalityFilter>("all");
  const [editing, setEditing] = useState<{ path: string; locked: boolean } | null>(null);
  const [duplicating, setDuplicating] = useState<BenchmarkInfo | null>(null);

  const list = useMemo(
    () =>
      (benchmarks.data ?? [])
        .filter((b) => modality === "all" || b.modality === modality)
        .sort((a, b) => a.modality.localeCompare(b.modality) || a.name.localeCompare(b.name, "en", { numeric: true })),
    [benchmarks.data, modality],
  );
  const names = (benchmarks.data ?? []).map((b) => b.name);
  const editingInfo = benchmarks.data?.find((b) => b.path === editing?.path);

  // Evaluations already recorded per benchmark: a version used by at least one evaluation
  // must not change (its results would no longer be comparable) — only a new version can.
  const lb = useLeaderboard({ source: "benchmark" });
  const evalCount = (name: string): number | null => (lb.data ? lb.data.rows.filter((r) => r.benchmark === name).length : null);
  const editingEvals = editingInfo ? evalCount(editingInfo.name) : null;
  const sealed = !!editing?.locked && editingEvals != null && editingEvals > 0;

  return (
    <Page width="wide">
      <PageHeader
        title="Benchmarks"
        description="Suites d'évaluation figées et versionnées : mêmes données, mêmes attaques, mêmes seuils — des résultats comparables dans le temps."
      />
      {canWrite && (
        <Callout tone="warning" icon={Lock} title="Une version publiée ne se modifie pas" className="mb-6">
          Les évaluations déjà enregistrées ne seraient plus comparables entre elles. Pour changer de données, d'attaques ou de métriques,
          créez une nouvelle version (ex. image_v1 → image_v2) puis éditez-la.
        </Callout>
      )}

      <div className="mb-5 flex items-center gap-3">
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
        {benchmarks.data && <span className="ml-auto text-[12.5px] text-muted">{plural(list.length, "suite")}</span>}
      </div>

      {benchmarks.isError ? (
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Impossible de charger les benchmarks"
          action={
            <Button size="sm" onClick={() => void benchmarks.refetch()}>
              Réessayer
            </Button>
          }
        >
          {errorMessage(benchmarks.error)}
        </Callout>
      ) : benchmarks.isLoading ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-72 rounded-xl" />
          ))}
        </div>
      ) : list.length ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2 2xl:grid-cols-3">
          {list.map((b) => (
            <BenchmarkCard
              key={b.path}
              bench={b}
              nEvals={evalCount(b.name)}
              onView={() => setEditing({ path: b.path, locked: true })}
              onDuplicate={() => setDuplicating(b)}
            />
          ))}
        </div>
      ) : (
        <Card padding="none">
          <EmptyState
            icon={Gauge}
            title={modality !== "all" ? "Aucun benchmark pour cette modalité" : "Aucun benchmark"}
            description="Un benchmark se déclare dans benchmarks/<nom>.yaml (données, attaques, métriques de qualité, FPR). Le skill wmlab-experiment aide à en créer un."
            action={modality !== "all" ? <Button onClick={() => setModality("all")}>Toutes les modalités</Button> : undefined}
          />
        </Card>
      )}

      {canWrite && duplicating && (
        <NewVersionDialog
          source={duplicating}
          existing={names}
          onClose={() => setDuplicating(null)}
          onCreated={(path) => setEditing({ path, locked: false })}
        />
      )}
      {canWrite && (
      <YamlFileDrawer
        open={!!editing}
        onClose={() => setEditing(null)}
        path={editing?.path ?? null}
        kind="benchmark"
        title={editingInfo ? `Benchmark ${editingInfo.name} (v${editingInfo.version})` : "Nouvelle version"}
        locked={editing?.locked}
        unlockable={!sealed}
        lockMessage={
          sealed
            ? `Version publiée, utilisée par ${plural(editingEvals ?? 0, "évaluation")} : elle ne se modifie plus (les résultats ne seraient plus comparables). Créez une nouvelle version.`
            : "Version publiée : ne la modifiez que si aucune évaluation ne l'utilise encore. Sinon, créez une nouvelle version."
        }
        lockAction={
          editingInfo ? (
            <Button
              size="sm"
              icon={CopyPlus}
              onClick={() => {
                setEditing(null);
                setDuplicating(editingInfo);
              }}
            >
              Nouvelle version
            </Button>
          ) : undefined
        }
      />
      )}
    </Page>
  );
}
