/**
 * Accounts of the team (§12.2, admin): table e-mail / identifier / role / state / last login /
 * creation, per-row actions (approve with a role, change role, disable / enable, reset the
 * password) and « Créer un compte ». A temporary password is shown once, in a dialog.
 */
import { useState, type FormEvent } from "react";
import { CircleAlert, Copy, KeyRound, Shield, TriangleAlert, UserCheck, UserPlus, Users, UserX } from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  Dialog,
  IconButton,
  Input,
  Select,
  Table,
  Tooltip,
  copyText,
  useConfirm,
  useToast,
  type Column,
} from "../ui";
import { errorMessage } from "../../lib/api";
import {
  isValid,
  normalizeEmail,
  pendingCount,
  ROLE_LABELS,
  USER_STATUS_LABELS,
  USER_STATUS_TONES,
  userRowActions,
  validateNewUser,
  type FieldErrors,
  type NewUserFields,
} from "../../lib/auth";
import { formatDate, formatDay, formatRelative, plural } from "../../lib/format";
import { errorStatus, useCreateUser, useResetUserPassword, useUserAction, useUsers } from "../../lib/queries";
import type { CreatedUser, UserInfo, UserRole } from "../../lib/types";

const ROLE_OPTIONS = [
  { value: "member", label: "Membre — lecture seule" },
  { value: "admin", label: "Administrateur — accès complet" },
];

const ROLE_HINTS: Record<UserRole, string> = {
  member: "Suit les runs, études, classement, budget et machines, sans accès au code, aux specs YAML ni aux réglages.",
  admin: "Lance et arrête des entraînements, édite les specs, gère les machines, les réglages et les comptes.",
};

function RoleBadge({ role }: { role: UserRole }) {
  return <Badge tone={role === "admin" ? "accent" : "neutral"}>{ROLE_LABELS[role] ?? role}</Badge>;
}

/** Shown once after a creation or a reset: the server never returns this password again. */
function TemporaryPasswordDialog({ created, title, onClose }: { created: CreatedUser | null; title: string; onClose: () => void }) {
  const toast = useToast();
  if (!created) return null;
  const { user, temporary_password: password } = created;
  return (
    <Dialog
      open
      onClose={onClose}
      closeOnOverlay={false}
      size="sm"
      title={title}
      description={
        <>
          {user.name} · {user.email}
        </>
      }
      footer={
        <Button variant="primary" onClick={onClose}>
          J'ai noté le mot de passe
        </Button>
      }
    >
      <div className="space-y-4">
        <div className="flex items-center gap-2 rounded-[10px] border border-border bg-surface-2 px-3 py-2.5">
          <span className="selectable min-w-0 flex-1 break-all font-mono text-[15px] tracking-wide text-fg" data-temporary-password>
            {password}
          </span>
          <IconButton
            icon={Copy}
            label="Copier le mot de passe"
            size="sm"
            onClick={() => void copyText(password).then((ok) => ok && toast.info("Mot de passe copié"))}
          />
        </div>
        <Callout tone="warning" icon={TriangleAlert} title="Affiché une seule fois">
          Transmettez-le à {user.name} par un canal sûr. Il devra choisir un nouveau mot de passe à sa première connexion.
        </Callout>
      </div>
    </Dialog>
  );
}

function CreateUserDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (c: CreatedUser) => void }) {
  const create = useCreateUser();
  const [fields, setFields] = useState<NewUserFields>({ email: "", name: "" });
  const [role, setRole] = useState<UserRole>("member");
  const [errors, setErrors] = useState<FieldErrors<keyof NewUserFields>>({});
  const [error, setError] = useState<string | null>(null);

  const close = () => {
    setFields({ email: "", name: "" });
    setRole("member");
    setErrors({});
    setError(null);
    onClose();
  };

  const submit = async (e?: FormEvent) => {
    e?.preventDefault();
    const errs = validateNewUser(fields);
    setErrors(errs);
    setError(null);
    if (!isValid(errs)) return;
    try {
      const res = await create.mutateAsync({ email: normalizeEmail(fields.email), name: fields.name.trim(), role });
      close();
      onCreated(res);
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Dialog
      open={open}
      onClose={close}
      size="sm"
      title="Créer un compte"
      description="Le compte est actif tout de suite, avec un mot de passe temporaire à changer à la première connexion."
      footer={
        <>
          <Button variant="ghost" onClick={close}>
            Annuler
          </Button>
          <Button variant="primary" icon={UserPlus} onClick={() => void submit()} loading={create.isPending}>
            Créer le compte
          </Button>
        </>
      }
    >
      <form className="space-y-4" onSubmit={(e) => void submit(e)}>
        <Input
          label="E-mail"
          type="email"
          value={fields.email}
          onChange={(e) => setFields((f) => ({ ...f, email: e.target.value }))}
          error={errors.email}
          placeholder="prenom@exemple.com"
          autoComplete="off"
          spellCheck={false}
          autoFocus
        />
        <Input
          label="Identifiant"
          mono
          value={fields.name}
          onChange={(e) => setFields((f) => ({ ...f, name: e.target.value }))}
          error={errors.name}
          hint="Lettres, chiffres et _ - . ; affiché comme auteur des runs."
          placeholder="prenom"
          autoComplete="off"
          spellCheck={false}
        />
        <Select label="Rôle" value={role} onChange={(v) => setRole(v as UserRole)} options={ROLE_OPTIONS} hint={ROLE_HINTS[role]} />
        {error && (
          <Callout tone="danger" icon={CircleAlert}>
            {error}
          </Callout>
        )}
        <button type="submit" hidden aria-hidden tabIndex={-1} />
      </form>
    </Dialog>
  );
}

/** « Approuver » (pending account) or « Changer le rôle ». */
function RoleDialog({ target, onClose }: { target: { user: UserInfo; mode: "approve" | "role" } | null; onClose: () => void }) {
  const action = useUserAction();
  const toast = useToast();
  const [role, setRole] = useState<UserRole>(target?.mode === "role" ? target.user.role : "member");
  const [error, setError] = useState<string | null>(null);
  if (!target) return null;
  const { user, mode } = target;

  const submit = async () => {
    setError(null);
    try {
      await action.mutateAsync({ kind: mode, id: user.id, role });
      toast.success(mode === "approve" ? "Compte approuvé" : "Rôle modifié", `${user.name} · ${ROLE_LABELS[role]}`);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="sm"
      title={mode === "approve" ? `Approuver ${user.name}` : `Rôle de ${user.name}`}
      description={mode === "approve" ? `Inscription de ${user.email} ${formatRelative(user.created_at)} : choisissez son rôle.` : user.email}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button
            variant="primary"
            icon={mode === "approve" ? UserCheck : Shield}
            onClick={() => void submit()}
            loading={action.isPending}
            disabled={mode === "role" && role === user.role}
          >
            {mode === "approve" ? "Approuver" : "Changer le rôle"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Select label="Rôle" value={role} onChange={(v) => setRole(v as UserRole)} options={ROLE_OPTIONS} hint={ROLE_HINTS[role]} />
        {error && (
          <Callout tone="danger" icon={CircleAlert}>
            {error}
          </Callout>
        )}
      </div>
    </Dialog>
  );
}

export function UsersCard({ meId }: { meId: string | null }) {
  const users = useUsers(true);
  const action = useUserAction();
  const reset = useResetUserPassword();
  const confirm = useConfirm();
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [roleTarget, setRoleTarget] = useState<{ user: UserInfo; mode: "approve" | "role" } | null>(null);
  const [shown, setShown] = useState<{ created: CreatedUser; title: string } | null>(null);
  const pending = pendingCount(users.data);

  const onDisable = async (u: UserInfo) => {
    const ok = await confirm({
      title: u.status === "pending" ? `Refuser l'inscription de ${u.name} ?` : `Désactiver le compte de ${u.name} ?`,
      message:
        u.status === "pending"
          ? "Le compte est désactivé sans avoir été approuvé : la personne ne peut pas se connecter. Vous pourrez le réactiver."
          : "Ses sessions sont révoquées immédiatement ; ses runs et études restent attribués à son nom. Vous pourrez le réactiver.",
      confirmLabel: u.status === "pending" ? "Refuser" : "Désactiver",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await action.mutateAsync({ kind: "disable", id: u.id });
      toast.success(u.status === "pending" ? "Inscription refusée" : "Compte désactivé", u.name);
    } catch (err) {
      toast.error("Action impossible", errorMessage(err));
    }
  };

  const onEnable = async (u: UserInfo) => {
    try {
      await action.mutateAsync({ kind: "enable", id: u.id });
      toast.success("Compte réactivé", u.name);
    } catch (err) {
      toast.error("Réactivation impossible", errorMessage(err));
    }
  };

  const onReset = async (u: UserInfo) => {
    const ok = await confirm({
      title: `Réinitialiser le mot de passe de ${u.name} ?`,
      message: "Un mot de passe temporaire est généré et toutes ses sessions sont révoquées : il devra en choisir un nouveau à sa prochaine connexion.",
      confirmLabel: "Réinitialiser",
      tone: "danger",
    });
    if (!ok) return;
    try {
      const res = await reset.mutateAsync(u.id);
      setShown({ created: res, title: "Mot de passe réinitialisé" });
    } catch (err) {
      toast.error("Réinitialisation impossible", errorMessage(err));
    }
  };

  const columns: Column<UserInfo>[] = [
    {
      id: "email",
      header: "E-mail",
      cell: (u) => <span className={u.status === "disabled" ? "text-muted" : "text-fg"}>{u.email}</span>,
      sortValue: (u) => u.email,
    },
    {
      id: "name",
      header: "Identifiant",
      cell: (u) => (
        <span className="inline-flex items-center gap-1.5">
          <span className="font-mono text-[12.5px] text-fg">{u.name}</span>
          {meId && u.id === meId && <Badge tone="info">vous</Badge>}
        </span>
      ),
      sortValue: (u) => u.name,
    },
    { id: "role", header: "Rôle", width: 130, cell: (u) => <RoleBadge role={u.role} />, sortValue: (u) => u.role },
    {
      id: "status",
      header: "État",
      width: 150,
      cell: (u) => (
        <span className="inline-flex flex-col items-start gap-0.5">
          <Badge tone={USER_STATUS_TONES[u.status] ?? "neutral"} dot={u.status === "pending"}>
            {USER_STATUS_LABELS[u.status] ?? u.status}
          </Badge>
          {u.status === "active" && u.must_change_password && <span className="text-[11.5px] text-muted">mot de passe temporaire</span>}
          {u.status === "disabled" && u.disabled_at && <span className="text-[11.5px] text-muted">{formatRelative(u.disabled_at)}</span>}
        </span>
      ),
      sortValue: (u) => ({ pending: 0, active: 1, disabled: 2 })[u.status] ?? 3,
    },
    {
      id: "login",
      header: "Dernière connexion",
      align: "right",
      cell: (u) => (
        <span className="whitespace-nowrap text-muted" title={u.last_login_at ? formatDate(u.last_login_at) : undefined}>
          {u.last_login_at ? formatRelative(u.last_login_at) : "jamais"}
        </span>
      ),
      sortValue: (u) => u.last_login_at ?? "",
    },
    {
      id: "created",
      header: "Créé",
      align: "right",
      cell: (u) => (
        <span className="whitespace-nowrap text-muted" title={u.approved_by ? `Approuvé par ${u.approved_by}${u.approved_at ? ` le ${formatDate(u.approved_at)}` : ""}` : undefined}>
          {formatDay(u.created_at)}
        </span>
      ),
      sortValue: (u) => u.created_at,
    },
    {
      id: "actions",
      header: "",
      align: "right",
      width: 190,
      cell: (u) => {
        const a = userRowActions(u, meId);
        if (a.self) {
          return (
            <Tooltip content="On ne peut ni changer son propre rôle ni se désactiver ; votre mot de passe se change dans « Mon compte »." side="left">
              <span className="text-[12px] text-muted">Votre compte</span>
            </Tooltip>
          );
        }
        return (
          <div className="flex items-center justify-end gap-0.5">
            {a.approve && (
              <Button size="sm" variant="primary" icon={UserCheck} onClick={() => setRoleTarget({ user: u, mode: "approve" })}>
                Approuver
              </Button>
            )}
            {a.enable && (
              <Button size="sm" icon={UserCheck} onClick={() => void onEnable(u)}>
                Réactiver
              </Button>
            )}
            {a.changeRole && <IconButton icon={Shield} label="Changer le rôle" size="sm" onClick={() => setRoleTarget({ user: u, mode: "role" })} />}
            {a.resetPassword && <IconButton icon={KeyRound} label="Réinitialiser le mot de passe" size="sm" onClick={() => void onReset(u)} />}
            {a.disable && (
              <IconButton icon={UserX} label={u.status === "pending" ? "Refuser l'inscription" : "Désactiver"} size="sm" onClick={() => void onDisable(u)} />
            )}
          </div>
        );
      },
    },
  ];

  return (
    <Card>
      <CardHeader
        title="Utilisateurs"
        icon={Users}
        description="Comptes de l'équipe (e-mail et mot de passe). Une inscription attend l'approbation d'un administrateur ; un membre a un accès en lecture seule, sans le code."
        actions={
          <Button size="sm" variant="primary" icon={UserPlus} onClick={() => setCreating(true)}>
            Créer un compte
          </Button>
        }
      />
      {pending > 0 && (
        <Callout tone="warning" icon={UserCheck} className="mb-4">
          {plural(pending, "inscription attend", "inscriptions attendent")} votre approbation.
        </Callout>
      )}
      {users.isError ? (
        <Callout tone="danger" icon={CircleAlert}>
          {errorStatus(users.error) === 404 ? "Le backend ne gère pas encore les comptes (version antérieure au mode hébergé)." : errorMessage(users.error)}
        </Callout>
      ) : (
        <Table
          columns={columns}
          rows={users.data ?? []}
          rowKey={(u) => u.id || u.email}
          loading={users.isLoading}
          dense
          empty="Aucun compte : seul le jeton du serveur est accepté. Créez le premier compte."
        />
      )}
      <CreateUserDialog open={creating} onClose={() => setCreating(false)} onCreated={(c) => setShown({ created: c, title: "Compte créé" })} />
      <RoleDialog key={roleTarget ? `${roleTarget.user.id}:${roleTarget.mode}` : "none"} target={roleTarget} onClose={() => setRoleTarget(null)} />
      <TemporaryPasswordDialog created={shown?.created ?? null} title={shown?.title ?? ""} onClose={() => setShown(null)} />
    </Card>
  );
}
