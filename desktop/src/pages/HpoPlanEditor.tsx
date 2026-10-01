import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router";
import { CircleAlert, FileWarning, FlaskConical, History, Play, RotateCcw, Save, Wallet, Zap } from "lucide-react";
import {
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  KeyValue,
  LoadingBlock,
  Page,
  PageHeader,
  PageSkeleton,
  Section,
  Select,
  Spinner,
  useConfirm,
  useToast,
} from "../components/ui";
import { YamlEditor } from "../components/YamlEditor";
import { PhaseTimeline } from "../components/PhaseTimeline";
import { StudiesTable } from "../components/StudiesTable";
import { HpoObjectives, HpoStrategy, SearchSpaceTable, asDict } from "../components/SpecSummaries";
import { SaveState, ValidationPanel } from "../components/SpecEditorBits";
import { useLaunch } from "../components/launch/LaunchProvider";
import { toastFileSaved } from "../components/saveToast";
import { useSpecDraft } from "../components/useSpecDraft";
import { parseYamlObject } from "../components/yamlData";
import { ApiError, errorMessage } from "../lib/api";
import { formatEur, formatHours, formatInt, plural } from "../lib/format";
import { useHotkey } from "../lib/hooks";
import { useHosts, useJobEstimate, useProject, useSettings, useStudies } from "../lib/queries";
import { experimentPath, projectPath } from "../lib/runs";

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function CostCard({ path, budget, dirty }: { path: string; budget: Record<string, unknown>; dirty: boolean }) {
  const hosts = useHosts();
  const settings = useSettings();
  const [host, setHost] = useState("");
  useEffect(() => {
    if (host || !hosts.data?.length) return;
    const def = settings.data?.default_host;
    setHost(def && hosts.data.some((h) => h.name === def) ? def : hosts.data[0].name);
  }, [host, hosts.data, settings.data]);
  const estimateBody = useMemo(() => (host ? { spec_path: path, host } : null), [host, path]);
  const estimate = useJobEstimate(estimateBody);
  const price = hosts.data?.find((h) => h.name === host)?.price_eur_per_hour ?? 0;
  const maxTrials = num(budget.max_trials);
  const maxHours = num(budget.max_hours);
  const maxEur = num(budget.max_eur);
  const parallel = num(budget.parallel) ?? 1;
  const caps = [maxEur, maxHours !== null && price > 0 ? maxHours * price : null].filter((v): v is number => v !== null);
  const ceiling = price === 0 ? 0 : caps.length ? Math.min(...caps) : null;

  return (
    <Card>
      <CardHeader
        title="Budget et coût"
        icon={Wallet}
        description="La première limite atteinte arrête l'étude."
        actions={
          <Select
            size="sm"
            value={host}
            onChange={setHost}
            options={(hosts.data ?? []).map((h) => ({
              value: h.name,
              label: `${h.name} · ${h.price_eur_per_hour > 0 ? `${formatEur(h.price_eur_per_hour)}/h` : "gratuit"}`,
            }))}
            className="w-52"
          />
        }
      />
      <KeyValue
        columns={2}
        items={[
          { label: "Essais max.", value: maxTrials !== null ? formatInt(maxTrials) : "illimité" },
          { label: "Essais en parallèle", value: formatInt(parallel) },
          { label: "Durée max.", value: maxHours !== null ? formatHours(maxHours) : "illimitée" },
          { label: "Coût max.", value: maxEur !== null ? formatEur(maxEur) : "illimité" },
        ]}
      />
      <div className="mt-4 grid grid-cols-2 gap-3 border-t border-border pt-4">
        <div>
          <div className="text-[11.5px] text-muted">Estimation</div>
          <div className="tabular font-serif text-[20px] text-fg">
            {estimate.isFetching && !estimate.data ? (
              <Spinner size={15} />
            ) : estimate.data?.est_eur != null ? (
              formatEur(estimate.data.est_eur)
            ) : (
              "—"
            )}
          </div>
          <div className="text-[12px] text-muted">
            {estimate.data?.est_hours != null ? `≈ ${formatHours(estimate.data.est_hours)}` : estimate.isError ? "estimation indisponible" : ""}
          </div>
        </div>
        <div>
          <div className="text-[11.5px] text-muted">Plafond de dépense</div>
          <div className="tabular font-serif text-[20px] text-fg">{ceiling !== null ? formatEur(ceiling) : "—"}</div>
          <div className="text-[12px] text-muted">{price === 0 ? "machine gratuite" : `à ${formatEur(price)}/h`}</div>
        </div>
      </div>
      {(estimate.data?.basis || dirty) && (
        <p className="mt-3 text-[12px] leading-relaxed text-faint">
          {estimate.data?.basis}
          {dirty ? `${estimate.data?.basis ? " · " : ""}calculée sur le fichier enregistré` : ""}
        </p>
      )}
    </Card>
  );
}

export default function HpoPlanEditor() {
  const params = useParams();
  const project = params.name ? decodeURIComponent(params.name) : "";
  const planName = params.plan ? decodeURIComponent(params.plan) : "";
  const detail = useProject(project);
  const info = detail.data?.hpo_plans.find((p) => p.name === planName);
  const path = detail.data ? (info?.path ?? `projects/${project}/hpo/${planName}.yaml`) : null;
  const spec = useSpecDraft(path, "hpo");
  const studies = useStudies(project);
  const { openLaunch } = useLaunch();
  const confirm = useConfirm();
  const toast = useToast();

  const parsed = useMemo(() => parseYamlObject(spec.content), [spec.content]);
  const plan = spec.validation.data?.plan ?? null;
  const planStudies = (studies.data ?? []).filter((s) => s.plan === planName);
  const baseExperiment = typeof parsed?.experiment === "string" ? parsed.experiment : info?.experiment;
  const space = asDict(parsed?.space);

  const save = async (): Promise<boolean> => {
    if (spec.saving) return false;
    try {
      const res = await spec.save();
      if (res.validation.valid) toastFileSaved(toast, res, "Plan enregistré", planName);
      else toastFileSaved(toast, res, "Enregistré avec des erreurs", "Corrigez-les avant de lancer l'étude.", { forceWarning: true });
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
        message: "L'étude utilise le plan enregistré : vos modifications doivent d'abord être enregistrées.",
        confirmLabel: "Enregistrer et lancer",
      });
      if (!ok || !(await save())) return;
    } else if (spec.valid === false) {
      toast.error("Plan invalide", "Corrigez les erreurs de validation avant de lancer.");
      return;
    }
    openLaunch({ kind: "hpo", project, plan: planName, smoke });
  };

  const discard = async () => {
    const ok = await confirm({
      title: "Annuler les modifications ?",
      message: "Le plan revient à sa dernière version enregistrée.",
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
          title={notFound ? "Plan HPO introuvable" : "Impossible de charger le plan"}
          description={notFound ? `Aucun plan « ${planName} » dans le projet ${project}.` : errorMessage(err)}
          action={<Button to={projectPath(project, "hpo")}>Plans du projet</Button>}
        />
      </Page>
    );
  }

  return (
    <Page width="wide">
      <PageHeader
        back={{ to: projectPath(project, "hpo"), label: "HPO" }}
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
            {planName}
            <SaveState dirty={spec.dirty} />
          </span>
        }
        description={typeof parsed?.description === "string" && parsed.description ? parsed.description : undefined}
        meta={
          baseExperiment ? (
            <Link to={experimentPath(project, baseExperiment)} className="no-drag inline-flex items-center gap-1.5 hover:text-fg">
              <FlaskConical size={13} strokeWidth={1.75} aria-hidden />
              expérience de base <span className="font-mono">{baseExperiment}</span>
            </Link>
          ) : undefined
        }
        actions={
          <>
            {spec.dirty && (
              <Button variant="ghost" icon={RotateCcw} onClick={() => void discard()}>
                Annuler
              </Button>
            )}
            <Button icon={Save} kbd="⌘S" onClick={() => void save()} loading={spec.saving} disabled={!spec.dirty}>
              Enregistrer
            </Button>
            <Button icon={Zap} onClick={() => void launch(true)} title="Étude smoke : 3 essais, trainer en smoke">
              Essai éclair
            </Button>
            <Button variant="primary" icon={Play} onClick={() => void launch(false)}>
              Lancer l'étude
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

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
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
          <Card padding="none" className="overflow-hidden">
            <div className="px-5 pt-4">
              <CardHeader
                title="Espace de recherche"
                description={`${plural(Object.keys(space).length, "paramètre")} · clés = chemins pointés dans l'expérience de base`}
                className="mb-3"
              />
            </div>
            <SearchSpaceTable space={parsed?.space} />
          </Card>
          <Card>
            <CardHeader title="Objectifs et stratégie" />
            {parsed ? (
              <div className="space-y-4">
                <HpoObjectives plan={parsed} />
                <HpoStrategy plan={parsed} />
              </div>
            ) : (
              <div className="text-[13px] text-muted">YAML illisible : corrigez la syntaxe pour voir le résumé.</div>
            )}
          </Card>
          {path && <CostCard path={path} budget={asDict(parsed?.budget)} dirty={spec.dirty} />}
          <Card>
            <CardHeader
              title="Curriculum d'un essai"
              description={
                plan
                  ? `Expérience de base avec la fidélité appliquée · ${formatInt(plan.total_steps)} steps par essai`
                  : "Aperçu de l'expérience de base à basse fidélité"
              }
              actions={spec.validation.isFetching ? <Spinner size={14} /> : undefined}
            />
            {plan ? (
              <PhaseTimeline plan={plan} live={false} />
            ) : !spec.validation.data && spec.content.trim() ? (
              <LoadingBlock label="Calcul de l'aperçu…" />
            ) : (
              <EmptyState compact icon={FileWarning} title="Aperçu indisponible" description="Le plan ou son expérience de base contient des erreurs." />
            )}
          </Card>
        </div>
      </div>

      <Section title="Études de ce plan" className="mt-10">
        {studies.isError ? (
          <Callout tone="danger" icon={CircleAlert} title="Impossible de charger les études">
            {errorMessage(studies.error)}
          </Callout>
        ) : (
          <StudiesTable
            studies={planStudies}
            loading={studies.isLoading}
            showPlan={false}
            empty={
              <span>
                Aucune étude pour ce plan.{" "}
                <button type="button" className="text-accent hover:underline" onClick={() => void launch(false)}>
                  Lancer l'étude
                </button>
              </span>
            }
          />
        )}
      </Section>
    </Page>
  );
}
