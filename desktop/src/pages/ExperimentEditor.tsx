import { useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { CircleAlert, Copy, FileWarning, History, Play, RotateCcw, Save, Zap } from "lucide-react";
import {
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  LoadingBlock,
  Page,
  PageHeader,
  PageSkeleton,
  Section,
  Spinner,
  useConfirm,
  useToast,
} from "../components/ui";
import { YamlEditor } from "../components/YamlEditor";
import { PhaseTimeline } from "../components/PhaseTimeline";
import { RunsTable } from "../components/RunsTable";
import { ExperimentSummary } from "../components/SpecSummaries";
import { NewExperimentDialog } from "../components/SpecDialogs";
import { useLaunch } from "../components/launch/LaunchProvider";
import { SaveState, ValidationPanel } from "../components/SpecEditorBits";
import { toastFileSaved } from "../components/saveToast";
import { useSpecDraft } from "../components/useSpecDraft";
import { parseYamlObject } from "../components/yamlData";
import { ApiError, errorMessage } from "../lib/api";
import { formatInt, plural } from "../lib/format";
import { useHotkey } from "../lib/hooks";
import { useProject, useRuns } from "../lib/queries";
import { projectPath } from "../lib/runs";

export default function ExperimentEditor() {
  const params = useParams();
  const project = params.name ? decodeURIComponent(params.name) : "";
  const exp = params.exp ? decodeURIComponent(params.exp) : "";
  const detail = useProject(project);
  const info = detail.data?.experiments.find((e) => e.name === exp);
  const path = detail.data ? (info?.path ?? `projects/${project}/experiments/${exp}.yaml`) : null;
  const spec = useSpecDraft(path, "experiment");
  const runs = useRuns({ project, experiment: exp, limit: 200 });
  const { openLaunch } = useLaunch();
  const confirm = useConfirm();
  const toast = useToast();
  const [duplicateOpen, setDuplicateOpen] = useState(false);

  const parsed = useMemo(() => parseYamlObject(spec.content), [spec.content]);
  const plan = spec.validation.data?.plan ?? null;

  const save = async (): Promise<boolean> => {
    if (spec.saving) return false;
    try {
      const res = await spec.save();
      if (res.validation.valid) toastFileSaved(toast, res, "Expérience enregistrée", exp);
      else toastFileSaved(toast, res, "Enregistrée avec des erreurs", "Corrigez-les avant de lancer.", { forceWarning: true });
      return res.validation.valid;
    } catch (err) {
      toast.error("Enregistrement impossible", errorMessage(err));
      return false;
    }
  };

  useHotkey("mod+s", () => {
    if (spec.dirty) void save();
  });

  const launch = async (smoke = false) => {
    if (spec.dirty) {
      const ok = await confirm({
        title: "Enregistrer avant de lancer ?",
        message: "Le lancement utilise le fichier enregistré : vos modifications en cours doivent d'abord être enregistrées.",
        confirmLabel: "Enregistrer et lancer",
      });
      if (!ok) return;
      if (!(await save())) return;
    } else if (spec.valid === false) {
      toast.error("Expérience invalide", "Corrigez les erreurs de validation avant de lancer.");
      return;
    }
    openLaunch({ kind: "train", project, experiment: exp, smoke });
  };

  const discard = async () => {
    const ok = await confirm({
      title: "Annuler les modifications ?",
      message: "Le fichier revient à sa dernière version enregistrée.",
      confirmLabel: "Annuler les modifications",
      cancelLabel: "Garder",
      tone: "danger",
    });
    if (ok) spec.reset();
  };

  if (detail.isLoading || (path && spec.file.isLoading)) return <PageSkeleton />;
  if (detail.isError || spec.file.isError) {
    const err = detail.error ?? spec.file.error;
    const notFound = err instanceof ApiError && err.status === 404;
    return (
      <Page>
        <EmptyState
          icon={notFound ? FileWarning : CircleAlert}
          title={notFound ? "Expérience introuvable" : "Impossible de charger l'expérience"}
          description={notFound ? `Aucun fichier pour « ${exp} » dans le projet ${project}.` : errorMessage(err)}
          action={<Button to={projectPath(project, "experiments")}>Expériences du projet</Button>}
        />
      </Page>
    );
  }

  const nRuns = runs.data?.length ?? info?.n_runs ?? 0;

  return (
    <Page width="wide">
      <PageHeader
        back={{ to: projectPath(project, "experiments"), label: "Expériences" }}
        eyebrow={
          <span>
            <Link to={projectPath(project)} className="no-drag hover:text-fg">
              {project}
            </Link>
            <span className="mx-1.5 text-faint">/</span>
            <span className="font-mono">{path}</span>
          </span>
        }
        title={
          <span className="inline-flex flex-wrap items-center gap-3">
            {exp}
            <SaveState dirty={spec.dirty} />
          </span>
        }
        description={typeof parsed?.description === "string" && parsed.description ? parsed.description : undefined}
        actions={
          <>
            {spec.dirty && (
              <Button variant="ghost" icon={RotateCcw} onClick={() => void discard()}>
                Annuler
              </Button>
            )}
            <Button icon={Copy} onClick={() => setDuplicateOpen(true)}>
              Dupliquer
            </Button>
            <Button icon={Save} kbd="⌘S" onClick={() => void save()} loading={spec.saving} disabled={!spec.dirty}>
              Enregistrer
            </Button>
            <Button icon={Zap} onClick={() => void launch(true)} title="Lancement smoke : données synthétiques, ≤ 6 steps par phase">
              Essai éclair
            </Button>
            <Button variant="primary" icon={Play} onClick={() => void launch(false)}>
              Lancer
            </Button>
          </>
        }
      />

      {spec.restored && (
        <Callout
          tone="info"
          icon={History}
          className="mb-5"
          action={
            <Button size="sm" variant="ghost" onClick={() => spec.reset()}>
              Revenir au fichier
            </Button>
          }
        >
          Brouillon non enregistré restauré depuis votre dernière visite.
        </Callout>
      )}

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
        <div className="min-w-0">
          <YamlEditor
            value={spec.content}
            onChange={spec.setContent}
            height="calc(100vh - 230px)"
            minHeight="520px"
            className="xl:sticky xl:top-2"
          />
        </div>
        <div className="min-w-0 space-y-4">
          <Card padding="sm" className="px-4">
            <ValidationPanel spec={spec} errors={spec.errors} />
          </Card>
          <Card>
            <CardHeader
              title="Curriculum"
              description={plan ? `${plural(plan.phases.length, "phase")} · ${formatInt(plan.total_steps)} steps` : "Aperçu recalculé à chaque modification"}
              actions={spec.validation.isFetching ? <Spinner size={14} /> : undefined}
            />
            {plan ? (
              <PhaseTimeline plan={plan} live={false} />
            ) : !spec.validation.data && spec.content.trim() ? (
              <LoadingBlock label="Calcul de l'aperçu…" />
            ) : (
              <EmptyState compact icon={FileWarning} title="Aperçu indisponible" description="Corrigez les erreurs de validation pour afficher la timeline des phases." />
            )}
          </Card>
          <Card>
            <CardHeader title="Résumé" description="Lecture du brouillon : modèle, données et attaques par phase." />
            <ExperimentSummary spec={parsed} />
          </Card>
        </div>
      </div>

      <Section title="Runs de cette expérience" description={nRuns ? plural(nRuns, "run") : undefined} className="mt-10">
        <RunsTable
          runs={runs.data ?? []}
          loading={runs.isLoading}
          showExperiment={false}
          selectable
          empty={
            <span>
              Aucun run pour cette expérience.{" "}
              <button type="button" className="text-accent hover:underline" onClick={() => void launch(false)}>
                Lancer un entraînement
              </button>
            </span>
          }
        />
      </Section>

      <NewExperimentDialog
        open={duplicateOpen}
        onClose={() => setDuplicateOpen(false)}
        project={project}
        experiments={detail.data?.experiments ?? []}
        from={exp}
      />
    </Page>
  );
}
