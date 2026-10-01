import { useState, type FormEvent } from "react";
import { CircleAlert, KeyRound } from "lucide-react";
import { Button, Callout, Input } from "../ui";
import { errorMessage } from "../../lib/api";
import { isValid, PASSWORD_MIN, validatePasswordChange, type FieldErrors, type PasswordChangeFields } from "../../lib/auth";
import { useChangePassword } from "../../lib/queries";
import type { Me } from "../../lib/types";

export interface PasswordChangeFormProps {
  me: Pick<Me, "email" | "name">;
  /** Called after `POST /api/auth/password` succeeded. */
  onChanged?: () => void;
  submitLabel?: string;
  autoFocus?: boolean;
}

/**
 * Current / new / confirmation password form (`POST /api/auth/password`, §12.2): used by the
 * forced « Définir un nouveau mot de passe » screen and the « Mon compte » card. The other
 * sessions of the account are revoked by the server; the current one stays valid.
 */
export function PasswordChangeForm({ me, onChanged, submitLabel = "Changer le mot de passe", autoFocus }: PasswordChangeFormProps) {
  const change = useChangePassword();
  const [fields, setFields] = useState<PasswordChangeFields>({ current: "", next: "", confirm: "" });
  const [errors, setErrors] = useState<FieldErrors<keyof PasswordChangeFields>>({});
  const [error, setError] = useState<string | null>(null);
  const set = (k: keyof PasswordChangeFields) => (e: { target: { value: string } }) => setFields((f) => ({ ...f, [k]: e.target.value }));

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const errs = validatePasswordChange(fields, me);
    setErrors(errs);
    setError(null);
    if (!isValid(errs)) return;
    try {
      await change.mutateAsync({ current_password: fields.current, new_password: fields.next });
      setFields({ current: "", next: "", confirm: "" });
      onChanged?.();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <form className="space-y-4" onSubmit={(e) => void submit(e)}>
      {/* Lets password managers attach the new password to the right account. */}
      <input type="email" name="username" autoComplete="username" value={me.email} readOnly hidden />
      <Input
        label="Mot de passe actuel"
        type="password"
        value={fields.current}
        onChange={set("current")}
        error={errors.current}
        autoComplete="current-password"
        autoFocus={autoFocus}
      />
      <Input
        label="Nouveau mot de passe"
        type="password"
        value={fields.next}
        onChange={set("next")}
        error={errors.next}
        hint={`${PASSWORD_MIN} caractères minimum ; une phrase de passe convient très bien.`}
        autoComplete="new-password"
      />
      <Input label="Confirmation" type="password" value={fields.confirm} onChange={set("confirm")} error={errors.confirm} autoComplete="new-password" />
      {error && (
        <Callout tone="danger" icon={CircleAlert}>
          {error}
        </Callout>
      )}
      <Button type="submit" variant="primary" icon={KeyRound} loading={change.isPending}>
        {submitLabel}
      </Button>
    </form>
  );
}
