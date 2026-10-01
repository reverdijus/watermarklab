import { useState } from "react";
import { useNavigate } from "react-router";
import { CircleAlert } from "lucide-react";
import { Button, Callout, Dialog, Input, Select, useToast } from "./ui";
import { toIdentifier } from "./analysis";
import { errorMessage } from "../lib/api";
import { useCreateExperiment, useCreateHpoPlan } from "../lib/queries";
import { experimentPath, hpoPlanPath } from "../lib/runs";
import type { ExperimentInfo, HpoPlanInfo } from "../lib/types";

const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,47}$/;

function nameError(name: string, taken: string[]): string | undefined {
  if (!name) return undefined;
  if (!NAME_RE.test(name)) return "Minuscules, chiffres, « _ » et « - » uniquement (48 caractères max).";
  if (taken.includes(name)) return "Ce nom est déjà utilisé dans le projet.";
  return undefined;
}

/** Suggest a free "<base>_copy[_n]" name. */
function copyName(base: string, taken: string[]): string {
  let candidate = `${base}_copie`;
  let i = 2;
  while (taken.includes(candidate)) candidate = `${base}_copie${i++}`;
  return candidate;
}

export interface NewExperimentDialogProps {
  open: boolean;
  onClose: () => void;
  project: string;
  experiments: ExperimentInfo[];
  /** Pre-selected source experiment ("Dupliquer"). */
  from?: string | null;
}

/** Create an experiment (POST /api/projects/{name}/experiments), optionally from a copy. */
export function NewExperimentDialog({ open, onClose, project, experiments, from }: NewExperimentDialogProps) {
  if (!open) return null;
  return <NewExperimentForm key={from ?? "new"} onClose={onClose} project={project} experiments={experiments} from={from} />;
}

function NewExperimentForm({ onClose, project, experiments, from }: Omit<NewExperimentDialogProps, "open">) {
  const navigate = useNavigate();
  const toast = useToast();
  const create = useCreateExperiment(project);
  const taken = experiments.map((e) => e.name);
  const [source, setSource] = useState(from ?? "");
  const [name, setName] = useState(from ? copyName(from, taken) : "");
  const [error, setError] = useState<string | null>(null);
  const clean = toIdentifier(name);
  const invalid = nameError(clean, taken);

  const submit = async () => {
    if (!clean || invalid) return setError(invalid ?? "Indiquez un nom d'expérience.");
    setError(null);
    try {
      await create.mutateAsync({ name: clean, from: source || undefined });
      toast.success(source ? "Expérience dupliquée" : "Expérience créée", clean);
      onClose();
      navigate(experimentPath(project, clean));
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title={from ? "Dupliquer l'expérience" : "Nouvelle expérience"}
      description="Une expérience décrit un entraînement complet : modèle, données, pertes et curriculum de phases."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={create.isPending}>
            {source ? "Dupliquer" : "Créer"}
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
          placeholder="ex. baseline_wsd"
          hint={clean && clean !== name ? `Fichier : experiments/${clean}.yaml` : "Devient le nom du fichier YAML et le préfixe des runs."}
          error={invalid}
        />
        <Select
          label="Partir de"
          value={source}
          onChange={setSource}
          placeholder="Modèle vierge (squelette commenté)"
          options={experiments.map((e) => ({ value: e.name, label: `${e.name}${e.valid ? "" : " — invalide"}` }))}
          hint="Copier une expérience existante est le plus sûr : seuls les paramètres à tester changent."
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

export interface NewHpoPlanDialogProps {
  open: boolean;
  onClose: () => void;
  project: string;
  experiments: ExperimentInfo[];
  plans: HpoPlanInfo[];
  defaultExperiment?: string | null;
}

/** Create an HPO plan (POST /api/projects/{name}/hpo) on a base experiment. */
export function NewHpoPlanDialog({ open, onClose, project, experiments, plans, defaultExperiment }: NewHpoPlanDialogProps) {
  if (!open) return null;
  return (
    <NewHpoPlanForm
      onClose={onClose}
      project={project}
      experiments={experiments}
      plans={plans}
      defaultExperiment={defaultExperiment}
    />
  );
}

function NewHpoPlanForm({ onClose, project, experiments, plans, defaultExperiment }: Omit<NewHpoPlanDialogProps, "open">) {
  const navigate = useNavigate();
  const toast = useToast();
  const create = useCreateHpoPlan(project);
  const taken = plans.map((p) => p.name);
  const [name, setName] = useState("");
  const [experiment, setExperiment] = useState(
    defaultExperiment ?? (experiments.find((e) => e.valid) ?? experiments[0])?.name ?? "",
  );
  const [error, setError] = useState<string | null>(null);
  const clean = toIdentifier(name);
  const invalid = nameError(clean, taken);

  const submit = async () => {
    if (!clean || invalid) return setError(invalid ?? "Indiquez un nom de plan.");
    if (!experiment) return setError("Choisissez l'expérience de base.");
    setError(null);
    try {
      await create.mutateAsync({ name: clean, experiment });
      toast.success("Plan HPO créé", clean);
      onClose();
      navigate(hpoPlanPath(project, clean));
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Nouveau plan HPO"
      description="Un plan d'optimisation fait varier des hyperparamètres d'une expérience de base et suit un ou plusieurs objectifs."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={create.isPending} disabled={!experiments.length}>
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
          placeholder="ex. lr_et_poids"
          hint={clean && clean !== name ? `Fichier : hpo/${clean}.yaml` : undefined}
          error={invalid}
        />
        <Select
          label="Expérience de base"
          value={experiment}
          onChange={setExperiment}
          placeholder={experiments.length ? undefined : "Aucune expérience dans ce projet"}
          options={experiments.map((e) => ({ value: e.name, label: `${e.name}${e.valid ? "" : " — invalide"}` }))}
          hint="Chaque essai est un run de cette expérience avec les valeurs tirées appliquées en overrides."
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
