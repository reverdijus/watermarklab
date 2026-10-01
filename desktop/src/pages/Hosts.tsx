import clsx from "clsx";
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  CircleAlert,
  CircleCheck,
  Cloud,
  DownloadCloud,
  Laptop,
  ListChecks,
  Pencil,
  Plus,
  PlugZap,
  Power,
  Server,
  Terminal,
  Trash2,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  CodeBlock,
  Dialog,
  EmptyState,
  IconButton,
  Input,
  Page,
  PageHeader,
  Section,
  Segmented,
  Skeleton,
  Spinner,
  StatusBadge,
  Textarea,
  Tooltip,
  useConfirm,
  useToast,
} from "../components/ui";
import { DecommissionDialog } from "../components/hosts/DecommissionDialog";
import { HostInventoryPanel } from "../components/hosts/HostInventoryPanel";
import { errorMessage } from "../lib/api";
import { formatBytes, formatDate, formatDay, formatEur, formatInt, formatRelative, plural } from "../lib/format";
import { decommissionBlockers, describeRetrieveResult } from "../lib/hosted";
import { qk, useHosts, useJobs, useMe, usePoweroffHost, useRetrieveHost, useSaveHosts, useSetupHost, useTestHost } from "../lib/queries";
import { byNewest, isActiveStatus, JOB_KIND_LABELS } from "../lib/runs";
import type { Host, HostTestResult, Job } from "../lib/types";

const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;

type TestState = { pending: true } | { pending: false; result: HostTestResult | null; error: string | null; at: number };

function Field({ label, value, mono }: { label: string; value: string | null | undefined; mono?: boolean }) {
  return (
    <div className="grid grid-cols-[110px_minmax(0,1fr)] gap-3 py-1 text-[12.5px]">
      <dt className="text-muted">{label}</dt>
      <dd className={`selectable min-w-0 truncate text-fg ${mono ? "font-mono text-[12px]" : ""}`} title={value ?? undefined}>
        {value || <span className="text-faint">—</span>}
      </dd>
    </div>
  );
}

function TestResultBlock({ state }: { state: TestState | undefined }) {
  if (!state) return null;
  if (state.pending) return <div className="mt-3 text-[12.5px] text-muted">Test de la connexion…</div>;
  const ok = state.result?.ok && !state.error;
  const output = state.result?.output?.trim();
  const err = state.error ?? state.result?.error;
  return (
    <div className="mt-3 space-y-2">
      <Callout
        tone={ok ? "success" : "danger"}
        icon={ok ? CircleCheck : CircleAlert}
        title={ok ? `Connexion réussie${state.result?.latency_ms != null ? ` · ${formatInt(state.result.latency_ms)} ms` : ""}` : "Connexion impossible"}
      >
        {ok ? `Testé ${formatRelative(state.at)}` : err || "Aucun détail renvoyé."}
      </Callout>
      {output && <CodeBlock code={output} maxHeight={180} />}
    </div>
  );
}

/** Last retrieve job of a host: progress while active, summary once finished. */
function RetrieveStatus({ job }: { job: Job | undefined }) {
  if (!job) return null;
  const active = isActiveStatus(job.status);
  if (active) {
    return (
      <div className="mt-3 flex items-center gap-2 rounded-xl border border-accent/25 bg-accent-soft/60 px-3.5 py-2.5 text-[12.5px] text-fg">
        <Spinner size={14} />
        <span>
          Rapatriement en cours… <span className="text-muted">démarré {formatRelative(job.started_at ?? job.created_at)} — rsync complet des runs et études, puis vérification des tailles.</span>
        </span>
      </div>
    );
  }
  const summary = describeRetrieveResult(job.result);
  const missing = job.result?.missing.length ?? 0;
  const failed = job.status !== "completed" || missing > 0;
  return (
    <Callout
      tone={failed ? "warning" : "success"}
      icon={failed ? TriangleAlert : CircleCheck}
      className="mt-3"
      title={`Dernier rapatriement : ${failed ? (job.status === "completed" ? "incomplet" : "échec") : "terminé"} ${formatRelative(job.ended_at ?? job.created_at)}`}
    >
      {summary || job.error || "Aucun détail renvoyé."}
      {missing > 0 && (
        <div className="mt-1 truncate font-mono text-[11.5px]" title={job.result?.missing.join("\n")}>
          {job.result!.missing.slice(0, 3).join(", ")}
          {missing > 3 ? "…" : ""}
        </div>
      )}
    </Callout>
  );
}

function HostCard({
  host,
  jobs,
  test,
  isAdmin,
  onTest,
  onSetup,
  onEdit,
  onDelete,
  onRetrieve,
  onPoweroff,
  onDecommission,
}: {
  host: Host;
  jobs: Job[];
  test: TestState | undefined;
  isAdmin: boolean;
  onTest: () => void;
  onSetup: () => void;
  onEdit: () => void;
  onDelete: () => void;
  onRetrieve: () => Promise<void>;
  onPoweroff: () => Promise<void>;
  onDecommission: () => void;
}) {
  const qc = useQueryClient();
  const retired = !!host.decommissioned_at;
  const active = jobs.filter((j) => isActiveStatus(j.status) && j.kind !== "retrieve");
  const lastSetup = [...jobs].sort(byNewest).find((j) => j.kind === "setup");
  const lastRetrieve = [...jobs].sort(byNewest).find((j) => j.kind === "retrieve");
  const retrieving = !!lastRetrieve && isActiveStatus(lastRetrieve.status);
  const isSsh = host.kind === "ssh";
  const Icon = isSsh ? Cloud : Laptop;
  const [showInventory, setShowInventory] = useState(false);
  const [retrievePending, setRetrievePending] = useState(false);
  const [poweroffPending, setPoweroffPending] = useState(false);

  // When a retrieve job ends, the inventory and the host counters are stale: reload them
  // (and open the panel so the outcome is visible).
  const wasRetrieving = useRef(retrieving);
  useEffect(() => {
    if (wasRetrieving.current && !retrieving) {
      void qc.invalidateQueries({ queryKey: qk.hostInventory(host.name) });
      void qc.invalidateQueries({ queryKey: qk.hosts });
      setShowInventory(true);
    }
    wasRetrieving.current = retrieving;
  }, [retrieving, qc, host.name]);

  const unretrieved = host.unretrieved_checkpoints ?? 0;
  const blockers = decommissionBlockers(host, active.length);

  return (
    <Card className={clsx("flex flex-col", retired && "opacity-60")}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-3">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-surface-2 text-muted">
            <Icon size={18} strokeWidth={1.6} aria-hidden />
          </div>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate font-mono text-[14.5px] font-medium text-fg">{host.name}</span>
              <Badge tone={isSsh ? "info" : "neutral"}>{isSsh ? "SSH" : "Local"}</Badge>
              {host.scaleway && (
                <Badge tone="neutral" icon={Cloud} title={`Instance ${host.scaleway.server_id} (${host.scaleway.zone})`}>
                  Scaleway
                </Badge>
              )}
              {retired && (
                <Badge tone="neutral" icon={Archive} title={`Retirée le ${formatDate(host.decommissioned_at)}`}>
                  retirée le {formatDay(host.decommissioned_at)}
                </Badge>
              )}
              {!retired && unretrieved > 0 && (
                <Tooltip content={`${plural(unretrieved, "checkpoint", "checkpoints")} (${formatBytes(host.unretrieved_bytes)}) encore sur la machine — rapatriez-les avant de la retirer.`}>
                  <Badge tone="warning" icon={TriangleAlert}>
                    {plural(unretrieved, "checkpoint non rapatrié", "checkpoints non rapatriés")}
                  </Badge>
                </Tooltip>
              )}
            </div>
            <div className="mt-0.5 truncate text-[12.5px] text-muted">{host.gpu || (isSsh ? "GPU non renseigné" : "Ce Mac (MPS / CPU)")}</div>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-0.5">
          {!retired && <IconButton icon={Pencil} label="Modifier" size="sm" onClick={onEdit} />}
          {host.name !== "local" && <IconButton icon={Trash2} label={retired ? "Supprimer de la liste" : "Supprimer"} size="sm" onClick={onDelete} />}
        </div>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-3 border-y border-border py-3">
        <div>
          <div className="text-[11.5px] text-muted">Prix</div>
          <div className="tabular font-serif text-[18px] text-fg">
            {host.price_eur_per_hour > 0 ? `${formatEur(host.price_eur_per_hour)}/h` : "gratuit"}
          </div>
        </div>
        <div>
          <div className="text-[11.5px] text-muted">Jobs actifs</div>
          <div className="tabular font-serif text-[18px] text-fg">
            {Math.max(active.length, host.active_jobs ?? 0)}
            <span className="text-[13px] text-muted"> / {host.max_concurrent_jobs}</span>
          </div>
        </div>
        <div>
          <div className="text-[11.5px] text-muted">Préparation</div>
          <div className="mt-1">
            {lastSetup ? (
              <StatusBadge status={lastSetup.status} />
            ) : (
              <span className="text-[12.5px] text-faint">{isSsh ? "jamais lancée" : "inutile"}</span>
            )}
          </div>
        </div>
      </div>

      <dl className="mt-3">
        {isSsh && <Field label="Cible SSH" value={host.ssh_target} mono />}
        {isSsh && <Field label="Dossier du code" value={host.workdir} mono />}
        {isSsh && <Field label="Données" value={host.data_dir} mono />}
        {isSsh && <Field label="Python" value={host.python} mono />}
        {host.scaleway && <Field label="Instance" value={`${host.scaleway.server_id} · ${host.scaleway.zone}`} mono />}
        {isSsh && host.inventory_at && <Field label="Inventaire" value={`vérifié ${formatRelative(host.inventory_at)}`} />}
        {host.notes && <Field label="Notes" value={host.notes} />}
      </dl>

      {active.length > 0 && (
        <div className="mt-2 text-[12px] text-muted">
          En cours : {active.map((j) => `${JOB_KIND_LABELS[j.kind]} ${j.target.split("/").pop()?.replace(/\.ya?ml$/, "") ?? ""}`).join(" · ")}
        </div>
      )}

      <TestResultBlock state={test} />
      {isSsh && <RetrieveStatus job={lastRetrieve} />}
      {isSsh && <HostInventoryPanel host={host} enabled={showInventory} />}

      {!retired && (
        <div className="mt-auto flex flex-wrap items-center gap-2 pt-4">
          <Button size="sm" icon={PlugZap} onClick={onTest} loading={test?.pending}>
            Tester la connexion
          </Button>
          {isSsh && (
            <Button size="sm" icon={Wrench} onClick={onSetup}>
              Préparer la machine
            </Button>
          )}
          {isSsh && (
            <Button size="sm" icon={ListChecks} onClick={() => setShowInventory((v) => !v)}>
              {showInventory ? "Masquer l'inventaire" : "Inventaire"}
            </Button>
          )}
          {isSsh && (
            <Button
              size="sm"
              icon={DownloadCloud}
              loading={retrievePending || retrieving}
              onClick={() => {
                setRetrievePending(true);
                void onRetrieve().finally(() => setRetrievePending(false));
              }}
            >
              {retrieving ? "Rapatriement…" : "Rapatrier les checkpoints"}
            </Button>
          )}
          {isSsh && host.scaleway && isAdmin && (
            <Button
              size="sm"
              icon={Power}
              loading={poweroffPending}
              disabled={active.length > 0}
              title={active.length > 0 ? "Impossible tant qu'un job est actif" : "Arrête l'instance (volumes conservés, facturation GPU stoppée)"}
              onClick={() => {
                setPoweroffPending(true);
                void onPoweroff().finally(() => setPoweroffPending(false));
              }}
            >
              Éteindre l'instance
            </Button>
          )}
          {isSsh && isAdmin && (
            <Tooltip content={blockers.length ? blockers.join(" ") : "Vérifie l'inventaire, retire la machine du lancement et, si demandé, supprime l'instance Scaleway."} wrapperClassName="ml-auto inline-flex">
              <Button size="sm" variant="danger" icon={Archive} disabled={blockers.length > 0} onClick={onDecommission}>
                Retirer la machine
              </Button>
            </Tooltip>
          )}
        </div>
      )}
      {!retired && isSsh && isAdmin && blockers.length > 0 && (
        <div className="mt-2 text-[12px] text-muted">Retrait bloqué : {blockers.join(" ")}</div>
      )}
    </Card>
  );
}

const EMPTY_HOST: Host = {
  name: "",
  kind: "ssh",
  ssh_target: "",
  workdir: "/root/wmlab",
  data_dir: "/root/wmlab-data",
  python: ".venv/bin/python",
  price_eur_per_hour: 0,
  gpu: "",
  max_concurrent_jobs: 1,
  notes: "",
  scaleway: null,
};

function HostDialog({
  initial,
  hosts,
  onClose,
}: {
  initial: Host | null;
  hosts: Host[];
  onClose: () => void;
}) {
  const save = useSaveHosts();
  const toast = useToast();
  const editing = !!initial;
  const [form, setForm] = useState<Host>(initial ?? EMPTY_HOST);
  const [price, setPrice] = useState(String(initial?.price_eur_per_hour ?? 0));
  const [maxJobs, setMaxJobs] = useState(String(initial?.max_concurrent_jobs ?? 1));
  const [zone, setZone] = useState(initial?.scaleway?.zone ?? "");
  const [serverId, setServerId] = useState(initial?.scaleway?.server_id ?? "");
  const [error, setError] = useState<string | null>(null);
  const set = <K extends keyof Host>(key: K, value: Host[K]) => setForm((f) => ({ ...f, [key]: value }));
  const isLocal = initial?.name === "local";
  const isSsh = form.kind === "ssh";

  const nameError =
    form.name && !NAME_RE.test(form.name)
      ? "Minuscules, chiffres, « _ » et « - » (32 caractères max.)."
      : hosts.some((h) => h.name === form.name && h.name !== initial?.name)
        ? "Une machine porte déjà ce nom."
        : undefined;

  const submit = async () => {
    const p = Number(price.replace(",", "."));
    const m = Number(maxJobs);
    if (!form.name || nameError) return setError(nameError ?? "Indiquez un nom de machine.");
    if (isSsh && !form.ssh_target?.trim()) return setError("Indiquez la cible SSH (alias de ~/.ssh/config ou utilisateur@adresse).");
    if (!Number.isFinite(p) || p < 0) return setError("Le prix doit être un nombre positif (€/h).");
    if (!Number.isInteger(m) || m < 1) return setError("Le nombre de jobs simultanés doit être un entier ≥ 1.");
    const z = zone.trim();
    const sid = serverId.trim();
    if (isSsh && (z || sid) && !(z && sid)) return setError("Renseignez la zone et l'identifiant de l'instance Scaleway, ou aucun des deux.");
    if (isSsh && z && !/^[a-z]{2}-[a-z]{3}-\d$/.test(z)) return setError("Zone Scaleway invalide (ex. fr-par-2).");
    if (isSsh && sid && !/^[0-9a-f-]{36}$/i.test(sid)) return setError("Identifiant d'instance Scaleway invalide (UUID attendu).");
    const clean: Host = {
      name: form.name,
      kind: form.kind,
      price_eur_per_hour: p,
      max_concurrent_jobs: m,
      gpu: form.gpu?.trim() || null,
      notes: form.notes?.trim() || null,
      ...(isSsh
        ? {
            ssh_target: form.ssh_target?.trim() || null,
            workdir: form.workdir?.trim() || null,
            data_dir: form.data_dir?.trim() || null,
            python: form.python?.trim() || null,
            scaleway: z && sid ? { zone: z, server_id: sid } : null,
          }
        : {}),
      ...(initial?.decommissioned_at ? { decommissioned_at: initial.decommissioned_at } : {}),
    };
    const next = editing ? hosts.map((h) => (h.name === initial!.name ? clean : h)) : [...hosts, clean];
    setError(null);
    try {
      await save.mutateAsync(next);
      toast.success(editing ? "Machine modifiée" : "Machine ajoutée", clean.name);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      size="lg"
      title={editing ? `Modifier ${initial!.name}` : "Ajouter une machine"}
      description="Les accès SSH passent par vos clés et ~/.ssh/config : aucun secret n'est stocké par l'application."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Annuler
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={save.isPending}>
            Enregistrer
          </Button>
        </>
      }
    >
      <form
        className="grid grid-cols-1 gap-4 md:grid-cols-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Input
          label="Nom"
          mono
          autoFocus={!editing}
          value={form.name}
          disabled={isLocal}
          onChange={(e) => set("name", e.target.value.trim())}
          placeholder="ex. scw-l40s"
          hint="Aussi utilisé pour résoudre les chemins de datasets (paths.<nom>)."
          error={nameError}
        />
        <div>
          <div className="mb-1.5 text-[13px] font-medium text-fg">Type</div>
          <Segmented<Host["kind"]>
            size="md"
            value={form.kind}
            onChange={(v) => !isLocal && set("kind", v)}
            options={[
              { value: "ssh", label: "Serveur SSH" },
              { value: "local", label: "Local" },
            ]}
          />
        </div>
        {isSsh && (
          <>
            <Input
              label="Cible SSH"
              mono
              value={form.ssh_target ?? ""}
              onChange={(e) => set("ssh_target", e.target.value)}
              placeholder="scw-l40s ou root@51.15.0.1"
              hint="Alias de ~/.ssh/config de préférence."
              className="md:col-span-2"
            />
            <Input label="Dossier du code (workdir)" mono value={form.workdir ?? ""} onChange={(e) => set("workdir", e.target.value)} />
            <Input label="Dossier des données (WMLAB_DATA)" mono value={form.data_dir ?? ""} onChange={(e) => set("data_dir", e.target.value)} />
            <Input
              label="Python"
              mono
              value={form.python ?? ""}
              onChange={(e) => set("python", e.target.value)}
              hint="Relatif au workdir ou absolu."
            />
            <div className="md:col-span-2 -mb-2 text-[12.5px] text-muted">
              Instance Scaleway (facultatif) — permet d'éteindre ou de supprimer l'instance depuis l'application (clé <span className="font-mono">SCW_SECRET_KEY</span> côté serveur).
            </div>
            <Input label="Zone Scaleway" mono value={zone} onChange={(e) => setZone(e.target.value)} placeholder="fr-par-2" />
            <Input
              label="Identifiant de l'instance"
              mono
              value={serverId}
              onChange={(e) => setServerId(e.target.value)}
              placeholder="11111111-2222-3333-4444-555555555555"
              hint="Console Scaleway → Instances → onglet Aperçu."
            />
          </>
        )}
        <Input label="GPU" value={form.gpu ?? ""} onChange={(e) => set("gpu", e.target.value)} placeholder="ex. L40S 48GB" />
        <Input label="Prix" value={price} onChange={(e) => setPrice(e.target.value)} suffix="€/h" inputMode="decimal" hint="Sert au calcul du coût des runs." />
        <Input label="Jobs simultanés max." value={maxJobs} onChange={(e) => setMaxJobs(e.target.value)} inputMode="numeric" />
        <Textarea label="Notes" rows={2} value={form.notes ?? ""} onChange={(e) => set("notes", e.target.value)} className="md:col-span-2" />
        {error && (
          <Callout tone="danger" icon={CircleAlert} className="md:col-span-2">
            {error}
          </Callout>
        )}
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

const SSH_CONFIG = `# ~/.ssh/config
Host scw-l40s
  HostName 51.15.0.1          # IP publique de l'instance
  User root
  IdentityFile ~/.ssh/id_ed25519
  ServerAliveInterval 30
  ServerAliveCountMax 4`;

function Help() {
  return (
    <Section title="Aide" description="Préparer un serveur GPU distant en quelques minutes." className="mt-10">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader title="Configurer ~/.ssh/config" icon={Terminal} description="L'application utilise ssh et rsync en mode non interactif (BatchMode)." />
          <CodeBlock code={SSH_CONFIG} title="~/.ssh/config" />
          <ul className="mt-3 list-disc space-y-1 pl-5 text-[13px] leading-relaxed text-muted">
            <li>
              Créez une clé si besoin : <span className="font-mono text-fg">ssh-keygen -t ed25519</span>.
            </li>
            <li>
              Connectez-vous une première fois dans un terminal (<span className="font-mono text-fg">ssh scw-l40s</span>) pour accepter
              l'empreinte du serveur : sans cela, le test échoue.
            </li>
            <li>
              Utilisez ensuite l'alias (<span className="font-mono text-fg">scw-l40s</span>) comme cible SSH de la machine.
            </li>
          </ul>
        </Card>
        <Card>
          <CardHeader title="Louer une instance GPU Scaleway" icon={Server} description="Facturation à l'heure : pensez à éteindre l'instance après usage." />
          <ol className="list-decimal space-y-2 pl-5 text-[13px] leading-relaxed text-muted">
            <li>
              Console Scaleway → <span className="text-fg">Instances</span> → <span className="text-fg">Créer</span> : type GPU (L40S, H100…),
              image « Ubuntu GPU OS » (pilotes NVIDIA inclus), zone fr-par-2.
            </li>
            <li>Ajoutez votre clé publique SSH au projet (Identifiants → Clés SSH) avant la création.</li>
            <li>Déclarez l'alias dans ~/.ssh/config avec l'IP publique, puis ajoutez la machine ici avec son prix horaire.</li>
            <li>
              <span className="text-fg">Tester la connexion</span>, puis <span className="text-fg">Préparer la machine</span> : synchronisation du
              code, installation de uv, création du venv et vérification du GPU.
            </li>
            <li>
              Datasets : copiez-les sur le volume de l'instance et renseignez leur chemin (<span className="font-mono">paths.&lt;machine&gt;</span>)
              dans le catalogue.
            </li>
            <li>
              Avant de supprimer l'instance : <span className="text-fg">Inventaire</span>, puis <span className="text-fg">Rapatrier les checkpoints</span>,
              enfin <span className="text-fg">Retirer la machine</span> (avec la zone et l'identifiant Scaleway renseignés, l'instance peut être
              supprimée depuis l'application).
            </li>
          </ol>
          <a
            href="https://console.scaleway.com/instance/servers"
            target="_blank"
            rel="noreferrer"
            className="no-drag mt-3 inline-block text-[13px] text-accent hover:underline"
          >
            Ouvrir la console Scaleway
          </a>
        </Card>
      </div>
    </Section>
  );
}

export default function Hosts() {
  const hosts = useHosts();
  const jobs = useJobs();
  const me = useMe();
  const test = useTestHost();
  const setup = useSetupHost();
  const retrieve = useRetrieveHost();
  const poweroff = usePoweroffHost();
  const save = useSaveHosts();
  const confirm = useConfirm();
  const toast = useToast();
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [dialog, setDialog] = useState<{ host: Host | null } | null>(null);
  const [decommission, setDecommission] = useState<Host | null>(null);
  const list = hosts.data ?? [];
  // Unknown user (backend without /api/me): the buttons stay visible, the server decides.
  const isAdmin = !me.data || me.data.role === "admin";

  const runRetrieve = async (host: Host) => {
    try {
      const job = await retrieve.mutateAsync(host.name);
      toast.success("Rapatriement lancé", `Job ${job.job_id} — tous les runs et études présents sur ${host.name} sont synchronisés (checkpoints inclus).`);
    } catch (err) {
      toast.error("Rapatriement impossible", errorMessage(err));
    }
  };

  const runPoweroff = async (host: Host) => {
    const ok = await confirm({
      title: `Éteindre l'instance de ${host.name} ?`,
      message: `L'instance Scaleway ${host.scaleway?.server_id ?? ""} est arrêtée (stop) : les volumes sont conservés, la facturation GPU s'arrête. Redémarrez-la depuis la console Scaleway.`,
      confirmLabel: "Éteindre l'instance",
      tone: "danger",
    });
    if (!ok) return;
    try {
      const res = await poweroff.mutateAsync(host.name);
      toast.success("Extinction demandée", `État de l'instance : ${res.instance_state ?? "inconnu"}.`);
    } catch (err) {
      toast.error("Extinction impossible", errorMessage(err));
    }
  };

  const runTest = async (name: string) => {
    setTests((t) => ({ ...t, [name]: { pending: true } }));
    try {
      const result = await test.mutateAsync(name);
      setTests((t) => ({ ...t, [name]: { pending: false, result, error: null, at: Date.now() } }));
    } catch (err) {
      setTests((t) => ({ ...t, [name]: { pending: false, result: null, error: errorMessage(err), at: Date.now() } }));
    }
  };

  const runSetup = async (host: Host) => {
    const ok = await confirm({
      title: `Préparer ${host.name} ?`,
      message: `Le code est synchronisé (rsync) vers ${host.workdir || "le dossier de travail"}, puis scripts/remote_bootstrap.sh installe ce qui manque (uv, venv Python, dépendances) et vérifie le GPU. Opération sans risque, relançable, qui prend quelques minutes.`,
      confirmLabel: "Préparer la machine",
    });
    if (!ok) return;
    try {
      const job = await setup.mutateAsync(host.name);
      toast.success("Préparation lancée", `Job ${job.job_id} — suivez-le depuis l'accueil.`);
    } catch (err) {
      toast.error("Préparation impossible", errorMessage(err));
    }
  };

  const remove = async (host: Host) => {
    const ok = await confirm({
      title: `Supprimer ${host.name} ?`,
      message: host.decommissioned_at
        ? "L'entrée est retirée de hosts.yaml : les coûts déjà comptabilisés restent attribués à ce nom."
        : (host.unretrieved_checkpoints ?? 0) > 0
          ? `Attention : ${plural(host.unretrieved_checkpoints ?? 0, "checkpoint", "checkpoints")} (${formatBytes(host.unretrieved_bytes)}) ne sont pas rapatriés. Préférez « Retirer la machine », qui vérifie l'inventaire avant. La suppression ne touche pas au serveur distant.`
          : "La machine est retirée de la liste (les runs déjà synchronisés sont conservés ; rien n'est supprimé sur le serveur). Pour une machine GPU, préférez « Retirer la machine », qui vérifie d'abord les checkpoints.",
      confirmLabel: "Supprimer",
      tone: "danger",
    });
    if (!ok) return;
    try {
      await save.mutateAsync(list.filter((h) => h.name !== host.name));
      toast.success("Machine supprimée", host.name);
    } catch (err) {
      toast.error("Suppression impossible", errorMessage(err));
    }
  };

  const jobsByHost = (name: string) => (jobs.data ?? []).filter((j) => j.host === name);

  return (
    <Page width="wide">
      <PageHeader
        title="Machines"
        description="Où tournent les entraînements : ce Mac et les serveurs GPU accessibles en SSH (Scaleway…). Aucune machine n'est retirée tant que ses checkpoints ne sont pas rapatriés."
        actions={
          <Button variant="primary" icon={Plus} onClick={() => setDialog({ host: null })}>
            Ajouter une machine
          </Button>
        }
      />

      {hosts.isError ? (
        <Callout
          tone="danger"
          icon={CircleAlert}
          title="Impossible de charger les machines"
          action={
            <Button size="sm" onClick={() => void hosts.refetch()}>
              Réessayer
            </Button>
          }
        >
          {errorMessage(hosts.error)}
        </Callout>
      ) : hosts.isLoading ? (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Skeleton className="h-72 rounded-xl" />
          <Skeleton className="h-72 rounded-xl" />
        </div>
      ) : list.length ? (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] text-muted">
            <span>
              {plural(list.length, "machine")} · {plural((jobs.data ?? []).filter((j) => isActiveStatus(j.status)).length, "job actif", "jobs actifs")}
            </span>
            {list.some((h) => (h.unretrieved_checkpoints ?? 0) > 0 && !h.decommissioned_at) && (
              <span className="inline-flex items-center gap-1 text-warning">
                <TriangleAlert size={13} strokeWidth={2} aria-hidden />
                {formatInt(list.reduce((n, h) => n + (h.decommissioned_at ? 0 : (h.unretrieved_checkpoints ?? 0)), 0))} checkpoints à rapatrier
              </span>
            )}
          </div>
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {[...list]
              .sort((a, b) => Number(!!a.decommissioned_at) - Number(!!b.decommissioned_at))
              .map((h) => (
                <HostCard
                  key={h.name}
                  host={h}
                  jobs={jobsByHost(h.name)}
                  test={tests[h.name]}
                  isAdmin={isAdmin}
                  onTest={() => void runTest(h.name)}
                  onSetup={() => void runSetup(h)}
                  onEdit={() => setDialog({ host: h })}
                  onDelete={() => void remove(h)}
                  onRetrieve={() => runRetrieve(h)}
                  onPoweroff={() => runPoweroff(h)}
                  onDecommission={() => setDecommission(h)}
                />
              ))}
          </div>
        </>
      ) : (
        <Card padding="none">
          <EmptyState
            icon={Server}
            title="Aucune machine"
            description="Ajoutez ce Mac (local) et vos serveurs GPU pour lancer des entraînements."
            action={
              <Button variant="primary" icon={Plus} onClick={() => setDialog({ host: null })}>
                Ajouter une machine
              </Button>
            }
          />
        </Card>
      )}

      <Help />

      {dialog && <HostDialog initial={dialog.host} hosts={list} onClose={() => setDialog(null)} />}
      {decommission && (
        <DecommissionDialog
          host={decommission}
          activeJobs={jobsByHost(decommission.name).filter((j) => isActiveStatus(j.status) && j.kind !== "retrieve").length}
          onClose={() => setDecommission(null)}
        />
      )}
    </Page>
  );
}
