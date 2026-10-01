import clsx from "clsx";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { CircleAlert, FlaskConical, GitBranch, GitCommitHorizontal, Microscope, Plus, Rocket, Trash, TriangleAlert, Zap } from "lucide-react";
import {
  Button,
  Callout,
  Dialog,
  Field,
  IconButton,
  Input,
  Kbd,
  Segmented,
  Select,
  Skeleton,
  Switch,
  useToast,
  type SelectOption,
} from "../ui";
import { PhaseTimeline } from "../PhaseTimeline";
import { errorMessage } from "../../lib/api";
import { formatEur, formatHours, formatInt, formatRelative, shortId } from "../../lib/format";
import { useHotkey } from "../../lib/hooks";
import { gitRefForLaunch, gitRevisionLabel } from "../../lib/hosted";
import {
  useBenchmarks,
  useCreateJob,
  useExperimentPlan,
  useGit,
  useHosts,
  useJobEstimate,
  useProject,
  useProjects,
  useRun,
  useRuns,
  useSettings,
} from "../../lib/queries";
import { runPath, runStatusLabel, studyPath } from "../../lib/runs";
import type { Checkpoint, CreateJobBody, EstimateBody } from "../../lib/types";
import type { LaunchKind, LaunchPreset } from "./LaunchProvider";

interface OverrideRow {
  id: number;
  key: string;
  value: string;
}

let rowSeq = 0;
const toRows = (o?: Record<string, string>): OverrideRow[] =>
  Object.entries(o ?? {}).map(([key, value]) => ({ id: ++rowSeq, key, value: String(value) }));

/** Checkpoint selectors offered for a run: tags first ("last", "best:…", "phase_end:…"), then names. */
function checkpointOptions(checkpoints: Checkpoint[]): SelectOption[] {
  const byTag = new Map<string, Checkpoint>();
  for (const c of checkpoints) for (const t of c.tags) if (!byTag.has(t)) byTag.set(t, c);
  const tagOpts = [...byTag.entries()]
    .sort(([a], [b]) => (a === "last" ? -1 : b === "last" ? 1 : a.localeCompare(b)))
    .map(([tag, c]) => ({ value: tag, label: `${tag}  ·  ${c.name}` }));
  const nameOpts = [...checkpoints]
    .sort((a, b) => b.step - a.step)
    .map((c) => ({ value: c.name, label: `${c.name}  ·  ${c.phase}${c.available_locally ? "" : " (distant)"}` }));
  return [...tagOpts, ...nameOpts];
}

function KindLabel({ icon: Icon, text }: { icon: typeof Rocket; text: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <Icon size={14} strokeWidth={1.75} aria-hidden />
      {text}
    </span>
  );
}

export interface LaunchDialogProps {
  preset: LaunchPreset;
  onClose: () => void;
}

/** "Nouveau lancement": training, HPO study or evaluation job. */
export function LaunchDialog({ preset, onClose }: LaunchDialogProps) {
  const navigate = useNavigate();
  const toast = useToast();

  const [kind, setKind] = useState<LaunchKind>(preset.kind ?? "train");
  const [project, setProject] = useState(preset.project ?? "");
  const [experiment, setExperiment] = useState(preset.experiment ?? "");
  const [plan, setPlan] = useState(preset.plan ?? "");
  const [host, setHost] = useState(preset.host ?? "");
  const [overrides, setOverrides] = useState<OverrideRow[]>(() => toRows(preset.overrides));
  const [smoke, setSmoke] = useState(preset.smoke ?? false);
  const [maxHours, setMaxHours] = useState(preset.maxHours != null ? String(preset.maxHours) : "");
  const [branch, setBranch] = useState(!!preset.initFrom);
  const [initRun, setInitRun] = useState(preset.initFrom?.run ?? "");
  const [initCheckpoint, setInitCheckpoint] = useState(preset.initFrom?.checkpoint ?? "last");
  const [startPhase, setStartPhase] = useState(preset.initFrom?.start_phase != null ? String(preset.initFrom.start_phase) : "");
  const [resume, setResume] = useState(!!preset.initFrom?.resume);
  const [evalRun, setEvalRun] = useState(preset.evalRun ?? "");
  const [evalCheckpoint, setEvalCheckpoint] = useState(preset.evalCheckpoint ?? "last");
  const [benchmark, setBenchmark] = useState(preset.benchmark ?? "");
  const [formError, setFormError] = useState<string | null>(null);
  // "Révision git" (§12.3): pre-filled with `branch @ short_sha` once /api/git answered.
  const [gitRef, setGitRef] = useState("");
  const [gitRefTouched, setGitRefTouched] = useState(false);

  const projects = useProjects();
  const projectDetail = useProject(project || null);
  const hosts = useHosts();
  const settings = useSettings();
  const benchmarks = useBenchmarks();
  const createJob = useCreateJob();
  const git = useGit();
  const gitLabel = gitRevisionLabel(git.data);
  useEffect(() => {
    if (!gitRefTouched && gitLabel) setGitRef(gitLabel);
  }, [gitLabel, gitRefTouched]);
  const gitAvailable = !!git.data?.available;

  // Run whose detail we need (to list checkpoints): eval target or branch source.
  const presetRunDetail = useRun(preset.evalRun ?? preset.initFrom?.run ?? null);
  const runs = useRuns({ project: project || undefined, limit: 200, include_trials: true }, { enabled: !!project && (kind === "eval" || branch) });
  const sourceRunId = kind === "eval" ? evalRun : branch ? initRun : "";
  const sourceRun = useRun(sourceRunId || null);

  // Default project: preset run's project, else the first project.
  useEffect(() => {
    if (project) return;
    const fromRun = presetRunDetail.data?.project;
    if (fromRun) setProject(fromRun);
    else if (projects.data?.length && !preset.evalRun && !preset.initFrom) setProject(projects.data[0].name);
  }, [project, projects.data, presetRunDetail.data, preset.evalRun, preset.initFrom]);

  // Default host: workspace default_host, else first (non-retired) host.
  useEffect(() => {
    // Wait for the workspace settings (default_host) unless they failed to load.
    if (host || !hosts.data?.length || settings.isPending) return;
    const usable = hosts.data.filter((h) => !h.decommissioned_at);
    if (!usable.length) return;
    const def = settings.data?.default_host;
    setHost(def && usable.some((h) => h.name === def) ? def : usable[0].name);
  }, [host, hosts.data, settings.data, settings.isPending]);

  const experiments = projectDetail.data?.experiments ?? [];
  const plans = projectDetail.data?.hpo_plans ?? [];
  const selectedExperiment = experiments.find((e) => e.name === experiment) ?? null;
  const selectedPlan = plans.find((p) => p.name === plan) ?? null;
  const summary = projectDetail.data?.summary ?? projects.data?.find((p) => p.name === project) ?? null;

  // Default experiment / plan / benchmark when the project changes.
  useEffect(() => {
    if (!projectDetail.data) return;
    if (!experiments.some((e) => e.name === experiment)) {
      const firstValid = experiments.find((e) => e.valid) ?? experiments[0];
      setExperiment(firstValid?.name ?? "");
    }
    if (!plans.some((p) => p.name === plan)) setPlan((plans.find((p) => p.valid) ?? plans[0])?.name ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectDetail.data]);

  const modality = summary?.modality;
  const benchOptions = useMemo(
    () =>
      (benchmarks.data ?? [])
        .filter((b) => !modality || b.modality === modality)
        .map((b) => ({ value: b.name, label: `${b.name} (v${b.version})${b.valid ? "" : " — invalide"}`, disabled: !b.valid })),
    [benchmarks.data, modality],
  );
  useEffect(() => {
    if (benchmark || !benchOptions.length) return;
    const def = summary?.default_benchmark;
    setBenchmark(def && benchOptions.some((b) => b.value === def) ? def : benchOptions[0].value);
  }, [benchmark, benchOptions, summary]);

  const experimentPlan = useExperimentPlan(kind === "train" ? selectedExperiment?.path : null);
  const phaseNames = experimentPlan.plan?.phases.map((p) => p.name) ?? [];

  // Suggest the phase following a "phase_end:<x>" checkpoint.
  useEffect(() => {
    if (!branch || resume || startPhase) return;
    const m = /^phase_end:(.+)$/.exec(initCheckpoint);
    if (m && phaseNames.length) {
      const idx = phaseNames.indexOf(m[1]);
      if (idx >= 0 && idx + 1 < phaseNames.length) setStartPhase(phaseNames[idx + 1]);
    }
  }, [branch, resume, initCheckpoint, phaseNames, startPhase]);

  const cleanOverrides = useMemo(() => {
    const out: Record<string, string> = {};
    for (const r of overrides) if (r.key.trim()) out[r.key.trim()] = r.value.trim();
    return out;
  }, [overrides]);

  const specPath = kind === "train" ? selectedExperiment?.path : kind === "hpo" ? selectedPlan?.path : undefined;
  // The estimate describes the full run: a smoke run (≤ 6 steps per phase) is not estimated.
  const estimateBody = useMemo<EstimateBody | null>(
    () => (specPath && host && kind !== "eval" && !smoke ? { spec_path: specPath, host, overrides: cleanOverrides } : null),
    [specPath, host, kind, cleanOverrides, smoke],
  );
  const estimate = useJobEstimate(estimateBody);

  const runOptions = useMemo<SelectOption[]>(
    () =>
      (runs.data ?? []).map((r) => ({
        value: r.run_id,
        label: `${shortId(r.run_id)} · ${r.experiment} · ${runStatusLabel(r)} · ${formatRelative(r.created_at)}`,
      })),
    [runs.data],
  );
  const withCurrent = (opts: SelectOption[], current: string) =>
    current && !opts.some((o) => o.value === current) ? [{ value: current, label: current }, ...opts] : opts;
  const ckptOptions = useMemo(() => checkpointOptions(sourceRun.data?.checkpoints ?? []), [sourceRun.data]);

  // Retired machines (§12.4) are no longer offered.
  const hostOptions = (hosts.data ?? [])
    .filter((h) => !h.decommissioned_at)
    .map((h) => ({
      value: h.name,
      label: `${h.name}${h.gpu ? ` · ${h.gpu}` : ""} · ${h.price_eur_per_hour > 0 ? `${formatEur(h.price_eur_per_hour)}/h` : "gratuit"}`,
    }));

  const validate = (): string | null => {
    if (!project) return "Choisissez un projet.";
    if (!host) return "Choisissez une machine.";
    const mh = maxHours.trim() ? Number(maxHours.replace(",", ".")) : null;
    if (mh !== null && (!Number.isFinite(mh) || mh <= 0)) return "La durée maximale doit être un nombre d'heures positif.";
    if (kind === "train") {
      if (!selectedExperiment) return "Choisissez une expérience.";
      if (!selectedExperiment.valid) return "L'expérience sélectionnée n'est pas valide : corrigez-la avant de lancer.";
      if (overrides.some((r) => !r.key.trim() && r.value.trim())) return "Chaque override doit avoir une clé.";
      if (branch && (!initRun || !initCheckpoint)) return "Choisissez le run et le checkpoint de départ.";
    }
    if (kind === "hpo") {
      if (!selectedPlan) return "Choisissez un plan HPO.";
      if (!selectedPlan.valid) return "Le plan HPO sélectionné n'est pas valide.";
      if (overrides.some((r) => !r.key.trim() && r.value.trim())) return "Chaque override doit avoir une clé.";
    }
    if (kind === "eval") {
      if (!evalRun) return "Choisissez le run à évaluer.";
      if (!evalCheckpoint) return "Choisissez un checkpoint.";
      if (!benchmark) return "Choisissez un benchmark.";
    }
    return null;
  };

  const overridesField = (
    <Field
      label="Overrides"
      hint={
        kind === "hpo"
          ? "Appliqués à l'expérience de base de chaque essai (avant l'espace de recherche), ex. data.batch_size = 16."
          : "Chemins pointés de la spec, ex. optimizer.lr = 3e-4 ou phases.1.steps = 5000."
      }
    >
      <div className="space-y-2">
        {overrides.map((row) => (
          <div key={row.id} className="flex items-center gap-2">
            <Input
              size="sm"
              mono
              placeholder="optimizer.lr"
              value={row.key}
              className="flex-1"
              onChange={(e) =>
                setOverrides((list) => list.map((r) => (r.id === row.id ? { ...r, key: e.target.value } : r)))
              }
            />
            <span className="text-muted">=</span>
            <Input
              size="sm"
              mono
              placeholder="3e-4"
              value={row.value}
              className="flex-1"
              onChange={(e) =>
                setOverrides((list) => list.map((r) => (r.id === row.id ? { ...r, value: e.target.value } : r)))
              }
            />
            <IconButton
              icon={Trash}
              label="Retirer"
              size="sm"
              onClick={() => setOverrides((list) => list.filter((r) => r.id !== row.id))}
            />
          </div>
        ))}
        <Button
          size="sm"
          variant="ghost"
          icon={Plus}
          onClick={() => setOverrides((list) => [...list, { id: ++rowSeq, key: "", value: "" }])}
        >
          Ajouter un override
        </Button>
      </div>
    </Field>
  );

  // One job per submission: ⌘↵ (or a double click) while the request is in flight must not
  // create a second job (two full trainings billed on a GPU host).
  const submittingRef = useRef(false);
  const submit = async () => {
    if (submittingRef.current || createJob.isPending) return;
    const problem = validate();
    setFormError(problem);
    if (problem) return;
    submittingRef.current = true;
    const mh = maxHours.trim() ? Number(maxHours.replace(",", ".")) : null;
    const gitRefValue = gitAvailable ? gitRefForLaunch(gitRef, gitLabel, git.data) : undefined;
    let body: CreateJobBody;
    if (kind === "train") {
      body = {
        kind,
        host,
        spec_path: selectedExperiment!.path,
        overrides: cleanOverrides,
        max_hours: mh,
        smoke,
        init_from: branch
          ? {
              run: initRun,
              checkpoint: initCheckpoint,
              start_phase: resume || startPhase === "" ? null : /^\d+$/.test(startPhase) ? Number(startPhase) : startPhase,
              ...(resume ? { resume: true } : {}),
            }
          : undefined,
      };
    } else if (kind === "hpo") {
      // Overrides apply to the base experiment of every trial; max_hours caps the study budget.
      body = { kind, host, spec_path: selectedPlan!.path, overrides: cleanOverrides, max_hours: mh, smoke };
    } else {
      body = { kind, host, run_id: evalRun, checkpoint: evalCheckpoint, benchmark, smoke };
    }
    if (gitRefValue) body.git_ref = gitRefValue;
    try {
      const job = await createJob.mutateAsync(body);
      onClose();
      if (kind === "eval") {
        toast.success("Évaluation lancée", `${benchmark} sur ${evalCheckpoint}`);
        navigate(runPath(evalRun, "evals"));
      } else if (job.study_id) {
        toast.success("Étude HPO lancée", job.study_id);
        navigate(studyPath(job.study_id));
      } else if (job.run_id) {
        toast.success("Entraînement lancé", shortId(job.run_id));
        navigate(runPath(job.run_id));
      } else {
        toast.success("Lancement demandé", `Job ${job.job_id} — le run apparaîtra dans quelques secondes.`);
        navigate("/runs");
      }
    } catch (err) {
      setFormError(errorMessage(err));
    } finally {
      submittingRef.current = false;
    }
  };

  useHotkey("mod+enter", () => void submit(), { allowInInputs: true, enabled: !createJob.isPending });

  const noProjects = projects.isSuccess && projects.data.length === 0;

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title="Nouveau lancement"
      description="Entraînement, étude d'hyperparamètres ou évaluation d'un checkpoint — en local ou sur un serveur GPU."
      footer={
        <>
          <span className="mr-auto hidden items-center gap-1 text-[12px] text-faint md:inline-flex">
            <Kbd>⌘</Kbd>
            <Kbd>↵</Kbd> pour lancer
          </span>
          <Button variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button variant="primary" icon={Rocket} onClick={() => void submit()} loading={createJob.isPending} disabled={noProjects}>
            Lancer
          </Button>
        </>
      }
    >
      <div className="space-y-5">
        <Segmented<LaunchKind>
          size="md"
          value={kind}
          onChange={(k) => {
            setKind(k);
            setFormError(null);
          }}
          options={[
            { value: "train", label: <KindLabel icon={Zap} text="Entraînement" /> },
            { value: "hpo", label: <KindLabel icon={FlaskConical} text="Étude HPO" /> },
            { value: "eval", label: <KindLabel icon={Microscope} text="Évaluation" /> },
          ]}
        />

        {noProjects && (
          <Callout tone="info" icon={CircleAlert} title="Aucun projet">
            Créez d'abord un projet depuis la barre latérale (« Nouveau projet »).
          </Callout>
        )}

        <div className="grid grid-cols-2 gap-4">
          <Select
            label="Projet"
            value={project}
            onChange={(v) => {
              setProject(v);
              setExperiment("");
              setPlan("");
              setInitRun("");
              setEvalRun("");
              setBenchmark("");
            }}
            placeholder={projects.isLoading ? "Chargement…" : "Choisir un projet"}
            options={(projects.data ?? []).map((p) => ({ value: p.name, label: `${p.title || p.name} (${p.modality})` }))}
          />
          <Select
            label="Machine"
            value={host}
            onChange={setHost}
            placeholder={hosts.isLoading ? "Chargement…" : "Choisir une machine"}
            options={hostOptions}
            error={hosts.error ? errorMessage(hosts.error) : undefined}
          />
        </div>

        {gitAvailable && (
          <div className="space-y-2">
            <Input
              label={
                <span className="inline-flex items-center gap-1.5">
                  <GitCommitHorizontal size={14} strokeWidth={1.75} aria-hidden /> Révision git
                </span>
              }
              mono
              size="sm"
              value={gitRef}
              onChange={(e) => {
                setGitRef(e.target.value);
                setGitRefTouched(true);
              }}
              placeholder={gitLabel || "main"}
              spellCheck={false}
              hint={
                git.data?.hosted
                  ? "Nom de branche ou sha exporté sur la machine (git archive). Poussez votre branche, puis indiquez son nom ou un sha."
                  : "Nom de branche ou sha ; laissée telle quelle, la révision courante du workspace est synchronisée."
              }
            />
            {git.data?.dirty && !git.data.hosted && (
              <Callout tone="warning" icon={TriangleAlert} title="Modifications non commitées">
                Le dépôt contient des changements non commités : la révision affichée ne les reflète pas. Sans révision explicite, le
                workspace est synchronisé tel quel (modifications incluses, diff enregistré avec le run) ; une révision explicite exporte un
                commit propre.
              </Callout>
            )}
            {git.data?.hosted && git.data.behind != null && git.data.behind > 0 && (
              <div className="text-[12px] text-muted">
                Le serveur a {git.data.behind} commit{git.data.behind > 1 ? "s" : ""} de retard sur le dépôt distant : un nom de branche est
                résolu après le prochain pull (Réglages → Tirer maintenant).
              </div>
            )}
          </div>
        )}

        {kind === "train" && (
          <>
            <Field
              label="Expérience"
              hint={selectedExperiment ? selectedExperiment.description || selectedExperiment.hypothesis || undefined : undefined}
              error={
                selectedExperiment && !selectedExperiment.valid
                  ? `Invalide : ${selectedExperiment.errors.map((e) => `${e.loc} ${e.msg}`).join(" · ")}`
                  : undefined
              }
            >
              <Select
                value={experiment}
                onChange={setExperiment}
                placeholder={projectDetail.isLoading ? "Chargement…" : experiments.length ? "Choisir une expérience" : "Aucune expérience dans ce projet"}
                options={experiments.map((e) => ({
                  value: e.name,
                  label: `${e.name} · ${e.model} · ${e.n_phases} phase${e.n_phases > 1 ? "s" : ""} · ${formatInt(e.total_steps)} steps${e.valid ? "" : " — invalide"}`,
                }))}
              />
            </Field>

            {selectedExperiment && (
              <div className="rounded-xl border border-border bg-surface-2/50 px-4 py-3">
                {experimentPlan.isLoading ? (
                  <Skeleton className="h-12 w-full" />
                ) : experimentPlan.plan ? (
                  <PhaseTimeline plan={experimentPlan.plan} curves={false} />
                ) : (
                  <div className="text-[12.5px] text-muted">Aperçu du curriculum indisponible.</div>
                )}
              </div>
            )}

            {overridesField}

            <div className="rounded-xl border border-border px-4 py-3">
              <Switch
                checked={branch}
                onChange={setBranch}
                label={
                  <span className="inline-flex items-center gap-1.5">
                    <GitBranch size={14} strokeWidth={1.75} aria-hidden /> Brancher depuis un checkpoint
                  </span>
                }
                description="Reprend les poids d'un run existant et démarre à la phase choisie (init_from)."
              />
              {branch && (
                <div className="mt-3 grid grid-cols-3 gap-3">
                  <Select
                    label="Run source"
                    size="sm"
                    value={initRun}
                    onChange={(v) => {
                      setInitRun(v);
                      setInitCheckpoint("last");
                    }}
                    placeholder={runs.isLoading ? "Chargement…" : "Choisir un run"}
                    options={withCurrent(runOptions, initRun)}
                  />
                  <Select
                    label="Checkpoint"
                    size="sm"
                    value={initCheckpoint}
                    onChange={(v) => {
                      setInitCheckpoint(v);
                      setStartPhase("");
                    }}
                    options={withCurrent(ckptOptions, initCheckpoint)}
                    hint={sourceRun.isLoading ? "Chargement des checkpoints…" : undefined}
                  />
                  <Select
                    label="Phase de départ"
                    size="sm"
                    value={resume ? "" : startPhase}
                    onChange={setStartPhase}
                    placeholder={resume ? "Phase du checkpoint" : "Première phase"}
                    disabled={resume}
                    options={withCurrent(
                      phaseNames.map((n, i) => ({ value: n, label: `${i + 1}. ${n}` })),
                      resume ? "" : startPhase,
                    )}
                  />
                </div>
              )}
              {branch && (
                <Switch
                  className="mt-3"
                  size="sm"
                  checked={resume}
                  onChange={setResume}
                  label="Reprendre au step du checkpoint (resume)"
                  description="Restaure l'optimiseur et reprend la phase en cours au step enregistré, au lieu de rejouer le curriculum depuis la phase de départ."
                />
              )}
            </div>
          </>
        )}

        {kind === "hpo" && (
          <Field
            label="Plan HPO"
            hint={selectedPlan ? `${selectedPlan.description || "—"} · ${selectedPlan.n_params} paramètre(s) · expérience ${selectedPlan.experiment}` : undefined}
            error={selectedPlan && !selectedPlan.valid ? `Invalide : ${selectedPlan.errors.map((e) => `${e.loc} ${e.msg}`).join(" · ")}` : undefined}
          >
            <Select
              value={plan}
              onChange={setPlan}
              placeholder={projectDetail.isLoading ? "Chargement…" : plans.length ? "Choisir un plan" : "Aucun plan HPO dans ce projet"}
              options={plans.map((p) => ({
                value: p.name,
                label: `${p.name} · ${p.objectives.map((o) => o.metric).join(" + ")}${p.valid ? "" : " — invalide"}`,
              }))}
            />
          </Field>
        )}
        {kind === "hpo" && overridesField}

        {kind === "eval" && (
          <div className="grid grid-cols-3 gap-3">
            <Select
              label="Run"
              value={evalRun}
              onChange={(v) => {
                setEvalRun(v);
                setEvalCheckpoint("last");
              }}
              placeholder={runs.isLoading ? "Chargement…" : "Choisir un run"}
              options={withCurrent(runOptions, evalRun)}
            />
            <Select
              label="Checkpoint"
              value={evalCheckpoint}
              onChange={setEvalCheckpoint}
              options={withCurrent(ckptOptions, evalCheckpoint)}
              hint={sourceRun.isLoading ? "Chargement…" : undefined}
            />
            <Select
              label="Benchmark"
              value={benchmark}
              onChange={setBenchmark}
              placeholder={benchmarks.isLoading ? "Chargement…" : "Choisir"}
              options={withCurrent(benchOptions, benchmark)}
            />
          </div>
        )}

        <div className="flex flex-wrap items-start gap-6">
          <Switch
            checked={smoke}
            onChange={setSmoke}
            label="Mode smoke"
            description="Test rapide : données synthétiques, quelques steps."
            className="flex-1"
          />
          {kind !== "eval" && (
            <Input
              label="Durée maximale"
              size="sm"
              suffix="h"
              inputMode="decimal"
              placeholder="illimitée"
              value={maxHours}
              onChange={(e) => setMaxHours(e.target.value)}
              className="w-40"
            />
          )}
        </div>

        {kind !== "eval" && smoke && (
          <Callout tone="info" icon={Zap} title="Essai éclair">
            {kind === "hpo"
              ? "3 essais en mode smoke : données synthétiques, au plus 6 steps par phase — quelques secondes, coût négligeable. L'estimation du plan complet ne s'applique pas."
              : "Données synthétiques, au plus 6 steps par phase — quelques secondes, coût négligeable. L'estimation du run complet ne s'applique pas."}
          </Callout>
        )}

        {kind !== "eval" && !smoke && estimateBody && (
          <div className={clsx("grid grid-cols-3 gap-3 rounded-xl bg-surface-2 px-4 py-3")}>
            <EstimateCell label="Steps" value={estimate.data ? formatInt(estimate.data.total_steps) : null} loading={estimate.isLoading} />
            <EstimateCell
              label="Durée estimée"
              value={estimate.data ? (estimate.data.est_hours != null ? formatHours(estimate.data.est_hours) : "inconnue") : null}
              loading={estimate.isLoading}
            />
            <EstimateCell
              label="Coût estimé"
              value={estimate.data ? (estimate.data.est_eur != null ? formatEur(estimate.data.est_eur) : "inconnu") : null}
              loading={estimate.isLoading}
            />
            {(estimate.data?.basis || estimate.error) && (
              <div className="col-span-3 text-[12px] text-muted">
                {estimate.error ? `Estimation indisponible : ${errorMessage(estimate.error)}` : estimate.data?.basis}
              </div>
            )}
          </div>
        )}

        {formError && (
          <Callout tone="danger" icon={CircleAlert}>
            {formError}
          </Callout>
        )}
      </div>
    </Dialog>
  );
}

function EstimateCell({ label, value, loading }: { label: string; value: string | null; loading: boolean }) {
  return (
    <div>
      <div className="text-[12px] text-muted">{label}</div>
      {loading && !value ? (
        <Skeleton className="mt-1 h-5 w-20" />
      ) : (
        <div className="tabular mt-0.5 font-serif text-[18px] text-fg">{value ?? "—"}</div>
      )}
    </div>
  );
}
