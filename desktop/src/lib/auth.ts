/**
 * Pure helpers of the accounts / roles model (ARCHITECTURE.md §12.2, §12.6): permissions
 * derived from `GET /api/me`, local validation of the login / registration / password forms
 * (mirror of the server policy, which stays the authority) and labels. No React, unit-tested.
 */
import type { Me, UserInfo, UserRole, UserStatus } from "./types";

// ---------------------------------------------------------------------------
// Permissions
// ---------------------------------------------------------------------------

export interface Permissions {
  /** Role of the current user, null while unknown. */
  role: UserRole | null;
  isAdmin: boolean;
  /** Launch, edit, stop, annotate… (member: read-only). */
  canWrite: boolean;
  /** YAML specs, registries, logs, git (member: never). */
  canViewCode: boolean;
  /** `GET /api/me` has not answered yet. */
  loading: boolean;
}

/**
 * Permissions of the interface. `implicitAdmin`: Electron in local mode (backend started by
 * the app with the server token — admin even without `/api/me`). Unknown user (loading,
 * older backend, network error): read-only, not admin — the server stays the authority.
 */
export function permissionsFor(
  me: Pick<Me, "role"> | null | undefined,
  opts: { implicitAdmin?: boolean; loading?: boolean } = {},
): Permissions {
  const role: UserRole | null = opts.implicitAdmin ? "admin" : (me?.role ?? null);
  const isAdmin = role === "admin";
  return { role, isAdmin, canWrite: isAdmin, canViewCode: isAdmin, loading: !opts.implicitAdmin && !me && !!opts.loading };
}

// ---------------------------------------------------------------------------
// Form validation (server policy mirror, §9 / §12.2)
// ---------------------------------------------------------------------------

export const PASSWORD_MIN = 12;
export const PASSWORD_MAX = 256;
/** Team identifier (`UserInfo.name`): attribution of runs, git author. */
export const USER_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
/** Reserved for the implicit user of the server token. */
export const RESERVED_USER_NAMES = ["admin"];
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

/** E-mail as stored by the server (trimmed, lower case). */
export function normalizeEmail(raw: string): string {
  return raw.trim().toLowerCase();
}

export function isValidEmail(raw: string): boolean {
  const email = normalizeEmail(raw);
  return email.length <= 254 && EMAIL_RE.test(email);
}

/** French error for an invalid team identifier, or null. */
export function userNameError(raw: string): string | null {
  const name = raw.trim();
  if (!name) return "Indiquez un identifiant.";
  if (!USER_NAME_RE.test(name)) return "Identifiant invalide : lettres, chiffres et _ - . (64 caractères au plus, commence par une lettre ou un chiffre).";
  if (RESERVED_USER_NAMES.includes(name.toLowerCase())) return `« ${name} » est réservé : choisissez un autre identifiant.`;
  return null;
}

/** French error for a password that the server would refuse, or null. */
export function passwordError(password: string, identity: { email?: string | null; name?: string | null } = {}): string | null {
  if (!password) return "Indiquez un mot de passe.";
  if (password.length < PASSWORD_MIN) return `Le mot de passe doit contenir au moins ${PASSWORD_MIN} caractères.`;
  if (password.length > PASSWORD_MAX) return `Le mot de passe ne peut pas dépasser ${PASSWORD_MAX} caractères.`;
  const lower = password.toLowerCase();
  const email = identity.email ? normalizeEmail(identity.email) : "";
  const name = identity.name ? identity.name.trim().toLowerCase() : "";
  if ((email && lower === email) || (name && lower === name)) return "Le mot de passe ne peut pas être votre e-mail ou votre identifiant.";
  return null;
}

export type FieldErrors<K extends string> = Partial<Record<K, string>>;

export interface LoginFields {
  email: string;
  password: string;
}

export function validateLogin(f: LoginFields): FieldErrors<keyof LoginFields> {
  const errors: FieldErrors<keyof LoginFields> = {};
  if (!f.email.trim()) errors.email = "Indiquez votre e-mail.";
  else if (!isValidEmail(f.email)) errors.email = "Adresse e-mail invalide.";
  if (!f.password) errors.password = "Indiquez votre mot de passe.";
  return errors;
}

export interface RegisterFields {
  name: string;
  email: string;
  password: string;
  confirm: string;
}

export function validateRegistration(f: RegisterFields): FieldErrors<keyof RegisterFields> {
  const errors: FieldErrors<keyof RegisterFields> = {};
  const nameErr = userNameError(f.name);
  if (nameErr) errors.name = nameErr;
  if (!f.email.trim()) errors.email = "Indiquez votre e-mail.";
  else if (!isValidEmail(f.email)) errors.email = "Adresse e-mail invalide.";
  const pwdErr = passwordError(f.password, { email: f.email, name: f.name });
  if (pwdErr) errors.password = pwdErr;
  else if (f.confirm !== f.password) errors.confirm = "Les deux mots de passe ne correspondent pas.";
  return errors;
}

export interface PasswordChangeFields {
  current: string;
  next: string;
  confirm: string;
}

export function validatePasswordChange(
  f: PasswordChangeFields,
  identity: { email?: string | null; name?: string | null } = {},
): FieldErrors<keyof PasswordChangeFields> {
  const errors: FieldErrors<keyof PasswordChangeFields> = {};
  if (!f.current) errors.current = "Indiquez votre mot de passe actuel.";
  const pwdErr = passwordError(f.next, identity);
  if (pwdErr) errors.next = pwdErr;
  else if (f.current && f.next === f.current) errors.next = "Le nouveau mot de passe doit être différent de l'actuel.";
  else if (f.confirm !== f.next) errors.confirm = "Les deux mots de passe ne correspondent pas.";
  return errors;
}

export interface NewUserFields {
  email: string;
  name: string;
}

export function validateNewUser(f: NewUserFields): FieldErrors<keyof NewUserFields> {
  const errors: FieldErrors<keyof NewUserFields> = {};
  if (!f.email.trim()) errors.email = "Indiquez l'e-mail.";
  else if (!isValidEmail(f.email)) errors.email = "Adresse e-mail invalide.";
  const nameErr = userNameError(f.name);
  if (nameErr) errors.name = nameErr;
  return errors;
}

/** True when the form has no error. */
export const isValid = (errors: Record<string, string | undefined>): boolean => Object.values(errors).every((e) => !e);

// ---------------------------------------------------------------------------
// Labels and per-row actions of the users table
// ---------------------------------------------------------------------------

export const ROLE_LABELS: Record<UserRole, string> = { admin: "administrateur", member: "membre" };

export const USER_STATUS_LABELS: Record<UserStatus, string> = { pending: "en attente", active: "actif", disabled: "désactivé" };

export const USER_STATUS_TONES: Record<UserStatus, "warning" | "success" | "neutral"> = {
  pending: "warning",
  active: "success",
  disabled: "neutral",
};

export interface UserRowActions {
  /** The row is the current user: every action is refused (§12.2 guards). */
  self: boolean;
  approve: boolean;
  changeRole: boolean;
  disable: boolean;
  enable: boolean;
  resetPassword: boolean;
}

/** Actions offered on a row of the users table (self: none — use « Mon compte »). */
export function userRowActions(user: Pick<UserInfo, "id" | "status">, meId: string | null | undefined): UserRowActions {
  const self = !!meId && user.id === meId;
  if (self) return { self, approve: false, changeRole: false, disable: false, enable: false, resetPassword: false };
  return {
    self,
    approve: user.status === "pending",
    changeRole: user.status !== "pending",
    disable: user.status !== "disabled",
    enable: user.status === "disabled",
    resetPassword: user.status !== "pending",
  };
}

/** Number of registrations waiting for an admin (sidebar badge). */
export function pendingCount(users: Pick<UserInfo, "status">[] | null | undefined): number {
  return (users ?? []).filter((u) => u.status === "pending").length;
}
