/** Small building blocks shared by the spec editors (experiment, HPO plan). */
import { CircleAlert, CircleCheck } from "lucide-react";
import { Badge, Spinner } from "./ui";
import type { SpecDraft } from "./useSpecDraft";
import { plural } from "../lib/format";
import type { ValidationError } from "../lib/types";

/** Validation status block (valid / checking / error list). */
export function ValidationPanel({ spec, errors }: { spec: SpecDraft; errors: ValidationError[] }) {
  const checking = spec.validation.isFetching;
  if (spec.valid && errors.length === 0) {
    return (
      <div className="flex items-center gap-2 text-[13px] text-success">
        <CircleCheck size={15} strokeWidth={1.75} aria-hidden />
        Spécification valide
        {checking && <Spinner size={13} className="ml-1" />}
      </div>
    );
  }
  if (spec.valid === undefined && !errors.length) {
    return (
      <div className="flex items-center gap-2 text-[13px] text-muted">
        <Spinner size={13} /> Validation…
      </div>
    );
  }
  return (
    <div>
      <div className="flex items-center gap-2 text-[13px] font-medium text-danger">
        <CircleAlert size={15} strokeWidth={1.75} aria-hidden />
        {errors.length ? plural(errors.length, "erreur de validation", "erreurs de validation") : "Spécification invalide"}
        {checking && <Spinner size={13} className="ml-1" />}
      </div>
      {errors.length > 0 && (
        <ul className="mt-2 max-h-56 space-y-1.5 overflow-y-auto">
          {errors.map((e, i) => (
            <li key={i} className="selectable text-[12.5px] leading-snug">
              {e.loc && <code className="mr-2 font-mono text-[11.5px] text-muted">{e.loc}</code>}
              <span className="text-fg">{e.msg}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Header status: unsaved changes / saved. */
export function SaveState({ dirty }: { dirty: boolean }) {
  return dirty ? (
    <Badge tone="warning" dot size="md">
      Non enregistré
    </Badge>
  ) : (
    <Badge tone="neutral" size="md">
      Enregistré
    </Badge>
  );
}
