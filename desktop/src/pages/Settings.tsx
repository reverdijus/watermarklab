import { useState } from "react";
import {
  CircleAlert,
  Copy,
  FolderOpen,
  GitBranch,
  Globe,
  Info,
  Keyboard,
  Laptop,
  LogOut,
  Monitor,
  Moon,
  RefreshCw,
  Save,
  Server,
  Sun,
  Users,
  Wallet,
} from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  CodeBlock,
  IconButton,
  Input,
  Kbd,
  KeyValue,
  Page,
  PageHeader,
  Segmented,
  Select,
  Skeleton,
  Table,
  copyText,
  useToast,
  type Column,
} from "../components/ui";
import { useSession } from "../components/LoginGate";
import { errorMessage, getApiConfig } from "../lib/api";
import { formatDate, formatEur, formatRelative } from "../lib/format";
import { errorStatus, useGit, useGitPull, useHealth, useHosts, useMe, useOverview, useSaveSettings, useSettings, useUsers } from "../lib/queries";
import { useThemePreference, type ThemePreference } from "../lib/theme";
import type { UserInfo, WorkspaceSettings } from "../lib/types";

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
  const [error, setError] = useState<string | null>(null);

  const dirty =
    name !== settings.name ||
    total !== String(settings.budget.total_eur) ||
    start !== (settings.budget.start ?? "") ||
    end !== (settings.budget.end ?? "") ||
    sync !== String(settings.sync_interval_s) ||
    defaultHost !== settings.default_host;

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

/** §12.3 — server checkout: branch, sha, last pull, "Tirer maintenant", public URL. */
function HostedCard() {
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

/** §12.2 — users (admin): list + reminder of the CLI command. */
function UsersCard() {
  const me = useMe();
  const isAdmin = me.data?.role === "admin";
  const users = useUsers(!!me.data && isAdmin);
  const columns: Column<UserInfo>[] = [
    { id: "name", header: "Nom", cell: (u) => <span className={u.revoked_at ? "text-muted line-through" : "font-medium text-fg"}>{u.name}</span>, sortValue: (u) => u.name },
    {
      id: "role",
      header: "Rôle",
      width: 110,
      cell: (u) => <Badge tone={u.role === "admin" ? "accent" : "neutral"}>{u.role === "admin" ? "administrateur" : "membre"}</Badge>,
      sortValue: (u) => u.role,
    },
    { id: "created", header: "Créé", align: "right", cell: (u) => <span className="whitespace-nowrap text-muted">{formatDate(u.created_at)}</span>, sortValue: (u) => u.created_at },
    {
      id: "status",
      header: "État",
      align: "right",
      width: 120,
      cell: (u) => (u.revoked_at ? <span className="whitespace-nowrap text-danger">révoqué {formatRelative(u.revoked_at)}</span> : <span className="text-success">actif</span>),
      sortValue: (u) => (u.revoked_at ? 1 : 0),
    },
  ];
  if (!me.data) return null;
  return (
    <Card>
      <CardHeader
        title="Utilisateurs"
        icon={Users}
        description={isAdmin ? "Jetons personnels de l'équipe (.wmlab/users.yaml) ; seul leur empreinte est stockée." : "Réservé aux administrateurs."}
      />
      {isAdmin ? (
        <>
          {users.isError ? (
            <Callout tone="danger" icon={CircleAlert}>
              {errorStatus(users.error) === 404 ? "Le backend ne gère pas encore les utilisateurs (version antérieure au mode hébergé)." : errorMessage(users.error)}
            </Callout>
          ) : (
            <Table columns={columns} rows={users.data ?? []} rowKey={(u) => u.name} loading={users.isLoading} dense empty="Aucun utilisateur : seul le jeton du serveur est accepté." initialSort={{ id: "name", desc: false }} />
          )}
          <div className="mt-4 text-[12.5px] text-muted">Ajouter un membre (sur le serveur ; le jeton n'est affiché qu'une fois) :</div>
          <CodeBlock code={"wmlab users add <nom> [--role admin|member]\nwmlab users list\nwmlab users revoke <nom>"} className="mt-2" />
        </>
      ) : (
        <div className="text-[13px] text-muted">
          Connecté en tant que <span className="font-medium text-fg">{me.data.name}</span> (membre). Demandez à un administrateur pour ajouter ou révoquer des
          jetons.
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
            value: me.data ? `${me.data.name} · ${me.data.role === "admin" ? "administrateur" : "membre"}` : me.isError ? "inconnu" : "—",
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
      {!electron && me.data?.public_url && (
        <div className="mt-3 inline-flex items-center gap-1.5 text-[12.5px] text-muted">
          <Globe size={13} strokeWidth={1.75} aria-hidden /> Partagez {me.data.public_url} avec l'équipe : chaque membre se connecte avec son jeton.
        </div>
      )}
    </Card>
  );
}

export default function Settings() {
  const settings = useSettings();
  const overview = useOverview();
  const health = useHealth();
  const [theme, setTheme] = useThemePreference();
  const config = getApiConfig();

  const workspacePath = overview.data?.workspace.path ?? health.data?.workspace ?? null;
  const dataDir = settings.data?.data_dir ?? null;
  const dataPath = dataDir && workspacePath && !dataDir.startsWith("/") && !dataDir.startsWith("~") ? `${workspacePath.replace(/\/+$/, "")}/${dataDir}` : dataDir;

  return (
    <Page>
      <PageHeader title="Réglages" description="Workspace, budget, mode hébergé, utilisateurs, connexion et apparence." />
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <div className="space-y-4 lg:col-span-3">
          {settings.isError ? (
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
          ) : settings.data ? (
            <WorkspaceForm key={JSON.stringify(settings.data)} settings={settings.data} />
          ) : (
            <Skeleton className="h-96 rounded-xl" />
          )}
          <HostedCard />
          <UsersCard />
        </div>
        <div className="space-y-4 lg:col-span-2">
          <ConnectionCard />
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
    </Page>
  );
}
