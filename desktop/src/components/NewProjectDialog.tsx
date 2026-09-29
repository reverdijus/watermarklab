import { useState } from "react";
import { useNavigate } from "react-router";
import { CircleAlert } from "lucide-react";
import { Button, Callout, Dialog, Field, Input, Segmented, Textarea, useToast } from "./ui";
import { errorMessage } from "../lib/api";
import { useCreateProject } from "../lib/queries";
import { MODALITY_LABELS, projectPath } from "../lib/runs";
import type { Modality } from "../lib/types";

const SLUG_RE = /^[a-z0-9][a-z0-9-]{0,47}$/;

function slugify(s: string): string {
  return s
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

/** Dialog creating a project (POST /api/projects) then opening it. */
export function NewProjectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate();
  const toast = useToast();
  const create = useCreateProject();
  const [title, setTitle] = useState("");
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [modality, setModality] = useState<Modality>("image");
  const [bits, setBits] = useState("48");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  const effectiveName = nameTouched ? name : slugify(title);
  const nameError = effectiveName && !SLUG_RE.test(effectiveName) ? "Minuscules, chiffres et tirets uniquement." : undefined;

  const reset = () => {
    setTitle("");
    setName("");
    setNameTouched(false);
    setModality("image");
    setBits("48");
    setDescription("");
    setError(null);
  };

  const submit = async () => {
    const nbits = Number(bits);
    if (!effectiveName || nameError) return setError("Indiquez un identifiant de projet valide.");
    if (!Number.isInteger(nbits) || nbits < 1 || nbits > 1024) return setError("Le nombre de bits doit être un entier entre 1 et 1024.");
    setError(null);
    try {
      const p = await create.mutateAsync({
        name: effectiveName,
        title: title.trim() || effectiveName,
        modality,
        message_bits: nbits,
        description: description.trim(),
      });
      toast.success("Projet créé", p.title || p.name);
      reset();
      onClose();
      navigate(projectPath(p.name));
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Nouveau projet"
      description="Un projet regroupe un schéma de watermarking : ses expériences, plans HPO, runs et objectifs."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={create.isPending}>
            Créer le projet
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
        <Input label="Titre" placeholder="Image — HiDDeN baseline" value={title} onChange={(e) => setTitle(e.target.value)} autoFocus />
        <Input
          label="Identifiant"
          mono
          placeholder="image-hidden"
          value={effectiveName}
          onChange={(e) => {
            setNameTouched(true);
            setName(e.target.value);
          }}
          error={nameError}
          hint="Nom du dossier projects/<identifiant>."
        />
        <div className="grid grid-cols-2 gap-4">
          <Field label="Modalité">
            <Segmented<Modality>
              size="md"
              value={modality}
              onChange={setModality}
              options={(["image", "audio", "video"] as Modality[]).map((m) => ({ value: m, label: MODALITY_LABELS[m] }))}
            />
          </Field>
          <Input label="Bits du message" type="number" min={1} max={1024} value={bits} onChange={(e) => setBits(e.target.value)} />
        </div>
        <Textarea
          label="Description"
          rows={3}
          placeholder="Objectif du schéma, contraintes…"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
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
