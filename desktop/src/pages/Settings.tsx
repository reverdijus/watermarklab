import { useState } from "react";
import {
  CircleAlert,
  Copy,
  FolderOpen,
  GitBranch,
  Globe,
  Info,
  KeyRound,
  Keyboard,
  Laptop,
  LogOut,
  Monitor,
  Moon,
  RefreshCw,
  Save,
  Server,
  Sun,
  UserRound,
  Wallet,
} from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  IconButton,
  Input,
  Kbd,
  KeyValue,
  Page,
  PageHeader,
  Segmented,
  Select,
  Skeleton,
  Switch,
  copyText,
  useToast,
} from "../components/ui";
import { PasswordChangeForm } from "../components/account/PasswordChangeForm";
import { UsersCard } from "../components/account/UsersCard";
import { useSession } from "../components/LoginGate";
import { errorMessage, getApiConfig } from "../lib/api";
import { ROLE_LABELS } from "../lib/auth";
import { formatDate, formatEur, formatRelative } from "../lib/format";
import { errorStatus, useGit, useGitPull, useHealth, useHosts, useMe, useOverview, usePermissions, useSaveSettings, useSettings } from "../lib/queries";
import { useThemePreference, type ThemePreference } from "../lib/theme";
import type { Me, WorkspaceSettings } from "../lib/types";

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function PathRow({ label, value }: { label: string; value: string | null }) {
  const toast = useToast();
  return (
    <div className="flex items-center gap-3 border-b border-border/70 py-2 last:border-b-0">
      <div className="w-36 shrink-0 text-[13px] text-muted">{label}</div>
      <div className="selectable min-w-0 flex-1 truncate font-mono text-[12px] text-fg" title={value ?? undefined}>
        {value ?? "—"}
      </div>
      {value && (
        <IconButton icon={Copy} label="Copier le chemin" size="sm" onClick={() => void copyText(value).then((ok) => ok && toast.info("Chemin copié"))} />
      )}
    </div>
  );
}

function WorkspaceForm({ settings }: { settings: WorkspaceSettings }) {
  const hosts = useHosts();
  const save = useSaveSettings();
  const toast = useToast();
  const [name, setName] = useState(settings.name);
  const [total, setTotal] = useState(String(settings.budget.total_eur));
  const [start, setStart] = useState(settings.budget.start ?? "");
  const [end, setEnd] = useState(settings.budget.end ?? "");
  const [sync, setSync] = useState(String(settings.sync_interval_s));
  const [defaultHost, setDefaultHost] = useState(settings.default_host);
  // §12.1 — only offered when the backend reports the field (older backends reject unknown keys).
  const hasRegistration = typeof settings.hosted?.allow_registration === "boolean";
  const [allowRegistration, setAllowRegistration] = useState(settings.hosted?.allow_registration ?? true);
  const [error, setError] = useState<string | null>(null);

  const dirty =
    name !== settings.name ||
    total !== String(settings.budget.total_eur) ||
    start !== (settings.budget.start ?? "") ||
    end !== (settings.budget.end ?? "") ||
    sync !== String(settings.sync_interval_s) ||
    defaultHost !== settings.default_host ||
    (hasRegistration && allowRegistration !== settings.hosted?.allow_registration);

  const submit = async () => {
    const totalEur = Number(total.replace(",", ".").replace(/\s/g, ""));
    const syncS = Number(sync);
    if (!name.trim()) return setError("Le nom du workspace est obligatoire.");
    if (!Number.isFinite(totalEur) || totalEur < 0) return setError("Le budget total doit être un montant positif.");
    if ((start && !DATE_RE.test(start)) || (end && !DATE_RE.test(end))) return setError("Les dates doivent être au format AAAA-MM-JJ.");
    if (start && end && end <= start) return setError("La fin de période doit être postérieure au début.");
    if (!Number.isInteger(syncS) || syncS < 5) return setError("L'intervalle de synchronisation doit être un entier ≥ 5 secondes.");
    setError(null);
    try {
      await save.mutateAsync({
        ...settings,
        name: name.trim(),
        budget: { ...settings.budget, total_eur: totalEur, start: start || null, end: end || null },
        sync_interval_s: syncS,
        default_host: defaultHost,
        ...(hasRegistration && settings.hosted ? { hosted: { ...settings.hosted, allow_registration: allowRegistration } } : {}),
      });
      toast.success("Réglages enregistrés");
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const hostOptions = (hosts.data ?? []).map((h) => ({ value: h.name, label: `${h.name}${h.gpu ? ` · ${h.gpu}` : ""}` }));
  if (defaultHost && !hostOptions.some((o) => o.value === defaultHost)) hostOptions.unshift({ value: defaultHost, label: defaultHost });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
      className="space-y-4"
    >
      <Card>
        <CardHeader title="Workspace" icon={FolderOpen} description="Enregistré dans workspace.yaml à la racine du dépôt." />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Input label="Nom" value={name} onChange={(e) => setName(e.target.value)} className="md:col-span-2" />
          <Select
            label="Machine par défaut"
            value={defaultHost}
            onChange={setDefaultHost}
            options={hostOptions}
            hint="Pré-sélectionnée dans « Nouveau lancement »."
          />
          <Input
            label="Intervalle de synchronisation"
            value={sync}
            onChange={(e) => setSync(e.target.value)}
            suffix="s"
            inputMode="numeric"
            hint="Rapatriement des runs distants (rsync)."
          />
        </div>
      </Card>
      <Card>
        <CardHeader title="Budget GPU" icon={Wallet} description="Sert au suivi, à la projection et au burn-up de la page Budget." />
        <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
          <Input
            label="Budget total"
            value={total}
            onChange={(e) => setTotal(e.target.value)}
            suffix="€"
            inputMode="decimal"
            hint={Number.isFinite(Number(total.replace(",", "."))) ? formatEur(Number(total.replace(",", ".")), { digits: 0 }) : undefined}
          />
          <Input label="Début de période" type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          <Input label="Fin de période" type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
        </div>
      </Card>
      <HostedCard registration={hasRegistration ? { value: allowRegistration, onChange: setAllowRegistration } : null} />
      {error && (
        <Callout tone="danger" icon={CircleAlert}>
          {error}
        </Callout>
      )}
      <div className="flex items-center gap-3">
        <Button type="submit" variant="primary" icon={Save} loading={save.isPending} disabled={!dirty}>
          Enregistrer
        </Button>
        {dirty && <span className="text-[12.5px] text-warning">Modifications non enregistrées</span>}
      </div>
    </form>
  );
}

/**
 * §12.1 / §12.3 — server checkout (branch, sha, last pull, "Tirer maintenant"), public URL and
 * self-registration switch (`hosted.allow_registration`, saved with the settings form).
 */
function HostedCard({ registration = null }: { registration?: { value: boolean; onChange: (v: boolean) => void } | null }) {
  const git = useGit();
  const me = useMe();
  const settings = useSettings();
  const pull = useGitPull();
  const toast = useToast();
  const hosted = settings.data?.hosted;
  const g = git.data;
  const unavailable = git.isError && (errorStatus(git.error) === 404 || errorStatus(git.error) === 0);
  const publicUrl = me.data?.public_url ?? hosted?.public_url ?? null;

  const onPull = async () => {
    try {
      const res = await pull.mutateAsync();
      toast.success("Dépôt à jour", res.short_sha ? `${res.branch ?? ""} @ ${res.short_sha}` : undefined);
    } catch (err) {
      toast.error("Pull impossible", errorMessage(err));
    }
  };

  return (
    <Card>
      <CardHeader
        title="Mode hébergé"
        icon={GitBranch}
        description={
          g?.hosted || hosted?.enabled
            ? "Serveur central : le code vient du dépôt git, les modifications faites ici sont commitées et poussées."
            : "Poste local : le workspace est utilisé tel quel (hosted.enabled = false dans workspace.yaml)."
        }
        actions={
          g?.available ? (
            <Button size="sm" icon={RefreshCw} onClick={() => void onPull()} loading={pull.isPending} disabled={g.dirty} title={g.dirty ? "Arbre de travail modifié : le pull est refusé" : "git pull --ff-only"}>
              Tirer maintenant
            </Button>
          ) : undefined
        }
      />
      {registration && (
        <Switch
          className="mb-4 border-b border-border pb-4"
          checked={registration.value}
          onChange={registration.onChange}
          label="Inscription ouverte"
          description={
            registration.value
              ? "Toute personne qui connaît l'adresse du serveur peut demander un compte ; il reste en attente jusqu'à l'approbation d'un administrateur."
              : "Seuls les administrateurs créent des comptes (Réglages → Utilisateurs) ; le lien « Créer un compte » est masqué."
          }
        />
      )}
      {git.isLoading ? (
        <Skeleton lines={4} />
      ) : unavailable ? (
        <div className="text-[13px] text-muted">Le backend ne renseigne pas l'état git (version antérieure au mode hébergé).</div>
      ) : git.isError ? (
        <Callout tone="danger" icon={CircleAlert}>
          {errorMessage(git.error)}
        </Callout>
      ) : g && !g.available ? (
        <div className="text-[13px] text-muted">Le workspace n'est pas un dépôt git : les révisions des lancements ne sont pas suivies.</div>
      ) : g ? (
        <>
          <KeyValue
            dense
            items={[
              { label: "Branche", value: g.branch ?? "—", mono: true },
              { label: "Révision", value: g.short_sha ?? g.sha ?? "—", mono: true, hint: g.sha ?? undefined },
              {
                label: "Arbre de travail",
                value: g.dirty ? <span className="text-warning">modifié (non commité)</span> : "propre",
              },
              { label: "Dépôt distant", value: g.remote ?? "—", mono: true },
              {
                label: "Avance / retard",
                value: g.ahead == null && g.behind == null ? "—" : `${g.ahead ?? 0} en avance · ${g.behind ?? 0} en retard`,
              },
              {
                label: "Dernier pull",
                value: g.last_pull_at ? `${formatRelative(g.last_pull_at)} (${formatDate(g.last_pull_at)})` : hosted?.auto_pull_s ? "jamais" : "automatique désactivé",
              },
              ...(hosted ? [{ label: "Pull automatique", value: hosted.auto_pull_s > 0 ? `toutes les ${hosted.auto_pull_s} s` : "désactivé" }] : []),
              ...(hosted ? [{ label: "Commit automatique", value: hosted.auto_commit ? "activé (PUT /api/files)" : "désactivé" }] : []),
              { label: "URL publique", value: publicUrl ?? "—", mono: true },
            ]}
          />
          {g.last_pull_error && (
            <Callout tone="warning" icon={CircleAlert} title="Dernier pull en erreur" className="mt-3">
              {g.last_pull_error}
            </Callout>
          )}
        </>
      ) : null}
    </Card>
  );
}

/** §12.2 — « Mon compte »: identity, role and password change (sessions only, not the server token). */
function AccountCard({ me }: { me: Me }) {
  const toast = useToast();
  const [changing, setChanging] = useState(false);
  return (
    <Card>
      <CardHeader
        title="Mon compte"
        icon={UserRound}
        actions={
          !changing ? (
            <Button size="sm" icon={KeyRound} onClick={() => setChanging(true)}>
              Changer le mot de passe
            </Button>
          ) : undefined
        }
      />
      <KeyValue
        dense
        items={[
          { label: "Identifiant", value: me.name, mono: true },
          { label: "E-mail", value: me.email || "—" },
          { label: "Rôle", value: <Badge tone={me.role === "admin" ? "accent" : "neutral"}>{ROLE_LABELS[me.role] ?? me.role}</Badge> },
          ...(me.role === "member"
            ? [{ label: "Accès", value: "lecture seule : runs, études, classement, budget, machines" }]
            : []),
        ]}
      />
      {changing && (
        <div className="mt-4 border-t border-border pt-4">
          <PasswordChangeForm
            me={me}
            autoFocus
            onChanged={() => {
              setChanging(false);
              toast.success("Mot de passe modifié", "Vos autres sessions ont été déconnectées.");
            }}
          />
          <Button size="sm" variant="ghost" className="mt-2" onClick={() => setChanging(false)}>
            Annuler
          </Button>
        </div>
      )}
    </Card>
  );
}

/** §12.6 — connection: local / remote mode, server, user, "Se déconnecter". */
function ConnectionCard() {
  const config = getApiConfig();
  const me = useMe();
  const health = useHealth();
  const { logout } = useSession();
  const electron = config.source === "electron";
  const local = electron && config.mode === "local";
  const bridge = typeof window !== "undefined" ? window.wmlab : undefined;
  return (
    <Card>
      <CardHeader
        title="Connexion"
        icon={local ? Laptop : Server}
        description={
          local
            ? "Backend lancé par l'application sur ce Mac."
            : electron
              ? "Application connectée au serveur de l'équipe ; aucun backend local n'est lancé."
              : "Interface servie dans le navigateur."
        }
        actions={
          !local ? (
            <Button size="sm" icon={LogOut} onClick={logout}>
              Se déconnecter
            </Button>
          ) : undefined
        }
      />
      <KeyValue
        dense
        items={[
          { label: "Mode", value: local ? "local" : electron ? "distant (application)" : "navigateur" },
          { label: "Serveur", value: config.apiBase, mono: true },
          { label: "Backend wmlab", value: health.data?.version ?? (health.isError ? "injoignable" : "—"), mono: true },
          {
            label: "Utilisateur",
            value: me.data
              ? `${me.data.name} · ${ROLE_LABELS[me.data.role] ?? me.data.role}${me.data.auth === "server_token" ? " (jeton du serveur)" : ""}`
              : me.isError
                ? "inconnu"
                : "—",
          },
          ...(me.data?.public_url ? [{ label: "URL publique", value: me.data.public_url, mono: true }] : []),
        ]}
      />
      {electron && (
        <div className="mt-4 flex flex-wrap gap-2">
          <Button size="sm" icon={Server} onClick={() => bridge?.openConnectDialog()}>
            Se connecter à un serveur distant…
          </Button>
          {!local && (
            <Button size="sm" icon={Laptop} onClick={() => bridge?.useLocalBackend()}>
              Utiliser le backend local
            </Button>
          )}
        </div>
      )}
      {!electron && me.data?.public_url && me.data.role === "admin" && (
        <div className="mt-3 flex items-start gap-1.5 text-[12.5px] text-muted">
          <Globe size={13} strokeWidth={1.75} className="mt-0.5 shrink-0" aria-hidden />
          <span>
            Partagez {me.data.public_url} avec l'équipe : chacun demande un compte (ou reçoit un mot de passe temporaire) puis se connecte avec son
            e-mail.
          </span>
        </div>
      )}
    </Card>
  );
}

function AppearanceCard() {
  const [theme, setTheme] = useThemePreference();
  return (
    <Card>
      <CardHeader title="Apparence" description="Préférence propre à cet ordinateur." />
      <Segmented<ThemePreference>
        size="md"
        value={theme}
        onChange={setTheme}
        options={[
          { value: "system", label: "Système", icon: Monitor },
          { value: "light", label: "Clair", icon: Sun },
          { value: "dark", label: "Sombre", icon: Moon },
        ]}
      />
    </Card>
  );
}

/** Member (read-only, §12.6): « Mon compte », « Connexion » and the local appearance only. */
function MemberSettings({ me }: { me: Me | null }) {
  return (
    <Page>
      <PageHeader title="Réglages" description="Votre compte, la connexion au serveur de l'équipe et l'apparence." />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <div className="space-y-4 lg:col-span-3">
          {me && me.auth !== "server_token" ? (
            <AccountCard me={me} />
          ) : (
            <Callout tone="info" icon={UserRound}>
              Compte inconnu : le serveur n'a pas renvoyé l'utilisateur connecté.
            </Callout>
          )}
        </div>
        <div className="space-y-4 lg:col-span-2">
          <ConnectionCard />
          <AppearanceCard />
        </div>
      </div>
    </Page>
  );
}

export default function Settings() {
  const perms = usePermissions();
  const me = useMe();
  const settings = useSettings({ enabled: perms.isAdmin });
  const overview = useOverview();
  const health = useHealth();
  const config = getApiConfig();

  if (!perms.isAdmin) return <MemberSettings me={me.data ?? null} />;

  const account = me.data && me.data.auth !== "server_token" ? me.data : null;
  const workspacePath = overview.data?.workspace.path ?? health.data?.workspace ?? null;
  const dataDir = settings.data?.data_dir ?? null;
  const dataPath = dataDir && workspacePath && !dataDir.startsWith("/") && !dataDir.startsWith("~") ? `${workspacePath.replace(/\/+$/, "")}/${dataDir}` : dataDir;

  return (
    <Page>
      <PageHeader title="Réglages" description="Workspace, budget, mode hébergé, comptes de l'équipe, connexion et apparence." />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <div className="space-y-4 lg:col-span-3">
          {settings.isError ? (
            <>
              <Callout
                tone="danger"
                icon={CircleAlert}
                title="Impossible de charger les réglages"
                action={
                  <Button size="sm" onClick={() => void settings.refetch()}>
                    Réessayer
                  </Button>
                }
              >
                {errorMessage(settings.error)}
              </Callout>
              <HostedCard />
            </>
          ) : settings.data ? (
            <WorkspaceForm key={JSON.stringify(settings.data)} settings={settings.data} />
          ) : (
            <Skeleton className="h-96 rounded-xl" />
          )}
        </div>
        <div className="space-y-4 lg:col-span-2">
          {account && <AccountCard me={account} />}
          <ConnectionCard />
          <AppearanceCard />
          <Card>
            <CardHeader title="Chemins" icon={FolderOpen} />
            <PathRow label="Workspace" value={workspacePath} />
            <PathRow label="Données (runs, études)" value={dataPath} />
          </Card>
          <Card>
            <CardHeader title="Version" icon={Info} />
            <KeyValue
              dense
              items={[
                { label: "Backend wmlab", value: health.data?.version ?? (health.isError ? "injoignable" : "—"), mono: true },
                { label: "Interface", value: config.source === "electron" ? "application de bureau" : "navigateur" },
                { label: "Plateforme", value: config.platform, mono: true },
              ]}
            />
          </Card>
          <Card>
            <CardHeader title="Raccourcis" icon={Keyboard} />
            <ul className="space-y-2 text-[13px]">
              {[
                ["Nouveau lancement", "⌘N"],
                ["Enregistrer (éditeurs)", "⌘S"],
                ["Afficher / masquer la barre latérale", "⌘\\"],
              ].map(([label, key]) => (
                <li key={label} className="flex items-center justify-between gap-3">
                  <span className="text-muted">{label}</span>
                  <Kbd>{key}</Kbd>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
      <div className="mt-6">
        <UsersCard meId={me.data?.id || null} />
      </div>
    </Page>
  );
}
