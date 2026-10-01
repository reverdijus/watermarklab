import clsx from "clsx";
import { useState } from "react";
import { Link, NavLink } from "react-router";
import {
  Activity,
  Database,
  Gauge,
  House,
  Library,
  LogOut,
  PanelLeftClose,
  Plus,
  Server,
  Settings,
  Trophy,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { Button, IconButton, ModalityDot, ProgressBar, Skeleton, Tooltip } from "../ui";
import { NewProjectDialog } from "../NewProjectDialog";
import { useLaunch } from "../launch/LaunchProvider";
import { useSession } from "../LoginGate";
import { getApiConfig } from "../../lib/api";
import { pendingCount, ROLE_LABELS } from "../../lib/auth";
import { formatEur, plural } from "../../lib/format";
import { useMe, useOverview, usePermissions, useProjects, useUsers } from "../../lib/queries";
import { isActiveStatus, projectPath } from "../../lib/runs";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
  /** Code-related page (registries): hidden for a member (§12.6). */
  code?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "Accueil", icon: House, end: true },
  { to: "/runs", label: "Runs", icon: Activity },
  { to: "/leaderboard", label: "Classement", icon: Trophy },
  { to: "/library", label: "Bibliothèque", icon: Library, code: true },
  { to: "/datasets", label: "Datasets", icon: Database },
  { to: "/benchmarks", label: "Benchmarks", icon: Gauge },
  { to: "/hosts", label: "Machines", icon: Server },
  { to: "/budget", label: "Budget", icon: Wallet },
  { to: "/settings", label: "Réglages", icon: Settings },
];

const itemClass = ({ isActive }: { isActive: boolean }) =>
  clsx(
    "no-drag group flex h-8 items-center gap-2.5 rounded-lg px-2.5 text-[13.5px] transition-colors",
    isActive ? "bg-active font-medium text-fg" : "text-fg/80 hover:bg-hover hover:text-fg",
  );

export interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
}

/** Left navigation: title, launch button, sections, projects, jobs and budget. */
export function Sidebar({ collapsed, onToggle }: SidebarProps) {
  const { openLaunch } = useLaunch();
  const projects = useProjects();
  const overview = useOverview();
  const me = useMe();
  const perms = usePermissions();
  // Registrations waiting for an admin (badge next to « Réglages »).
  const users = useUsers(perms.isAdmin);
  const pending = pendingCount(users.data);
  const { logout } = useSession();
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  // Electron with its own backend: the session is implicit (no user to disconnect).
  const config = getApiConfig();
  const canLogout = !(config.source === "electron" && config.mode === "local");

  const jobs = overview.data?.jobs ?? [];
  const activeJobs = jobs.filter((j) => isActiveStatus(j.status)).length;
  const budget = overview.data?.budget;
  const spentFrac = budget && budget.total_eur > 0 ? budget.spent_eur / budget.total_eur : null;

  return (
    <aside
      aria-hidden={collapsed}
      className={clsx(
        "flex h-full shrink-0 flex-col overflow-hidden border-r border-border bg-sidebar transition-[width,opacity] duration-200 ease-out",
        collapsed ? "w-0 border-r-0 opacity-0" : "w-[260px] opacity-100",
      )}
    >
      <div className="flex h-full w-[260px] flex-col">
        {/* Title bar area (traffic lights at the top-left). */}
        <div className="drag flex h-[52px] shrink-0 items-center justify-end px-2.5">
          <IconButton icon={PanelLeftClose} label="Masquer la barre latérale (⌘\)" size="sm" onClick={onToggle} tooltipSide="right" />
        </div>

        <div className="px-4 pb-3">
          <Link to="/" className="no-drag block font-serif text-[21px] leading-tight tracking-[-0.01em] text-fg">
            Watermark Lab
          </Link>
        </div>

        {perms.canWrite && (
          <div className="px-3 pb-3">
            <Button variant="primary" icon={Plus} fullWidth onClick={() => openLaunch()} kbd="⌘N" className="justify-start">
              Nouveau lancement
            </Button>
          </div>
        )}

        <nav className="space-y-0.5 px-2" aria-label="Navigation principale">
          {NAV_ITEMS.filter((item) => !item.code || perms.canViewCode).map((item) => (
            <NavLink key={item.to} to={item.to} end={item.end} className={itemClass}>
              <item.icon size={16} strokeWidth={1.6} className="shrink-0 text-muted group-hover:text-fg" aria-hidden />
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              {item.to === "/settings" && pending > 0 && (
                <Tooltip content={`${plural(pending, "inscription attend", "inscriptions attendent")} une approbation`} side="right">
                  <span className="inline-flex h-[18px] items-center rounded-full bg-warning/12 px-1.5 text-[11px] font-medium text-warning">
                    {pending} en attente
                  </span>
                </Tooltip>
              )}
            </NavLink>
          ))}
        </nav>

        <div className="mt-5 flex min-h-0 flex-1 flex-col">
          <div className="flex h-7 items-center justify-between px-4 pb-1">
            <span className="text-[12px] font-medium text-muted">Projets</span>
            {perms.canWrite && <IconButton icon={Plus} label="Nouveau projet" size="sm" onClick={() => setNewProjectOpen(true)} tooltipSide="right" />}
          </div>
          <div className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
            {projects.isLoading &&
              Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="mx-2.5 my-2 h-4 w-40" />)}
            {projects.isError && <div className="px-2.5 py-1 text-[12.5px] text-danger">Projets indisponibles</div>}
            {projects.data?.length === 0 &&
              (perms.canWrite ? (
                <button
                  type="button"
                  onClick={() => setNewProjectOpen(true)}
                  className="no-drag w-full rounded-lg px-2.5 py-1.5 text-left text-[13px] text-muted hover:bg-hover hover:text-fg"
                >
                  Créer un premier projet…
                </button>
              ) : (
                <div className="px-2.5 py-1.5 text-[13px] text-muted">Aucun projet</div>
              ))}
            {projects.data?.map((p) => (
              <NavLink key={p.name} to={projectPath(p.name)} className={itemClass} title={p.description || p.title}>
                <ModalityDot modality={p.modality} />
                <span className="min-w-0 flex-1 truncate">{p.title || p.name}</span>
                {p.n_running > 0 && (
                  <Tooltip content={`${plural(p.n_running, "run actif", "runs actifs")}`} side="right">
                    <span className="tabular inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-accent-soft px-1.5 text-[11px] font-medium text-accent">
                      {p.n_running}
                    </span>
                  </Tooltip>
                )}
              </NavLink>
            ))}
          </div>
        </div>

        <div className="space-y-2.5 border-t border-border px-4 py-3">
          <Link to="/runs?status=running" className="no-drag flex items-center gap-2 text-[12.5px] text-muted hover:text-fg">
            <span className="relative inline-flex h-2 w-2">
              {activeJobs > 0 && <span className="absolute inset-0 animate-pulse-dot rounded-full bg-accent" />}
              <span className={clsx("relative inline-flex h-2 w-2 rounded-full", activeJobs > 0 ? "bg-accent" : "bg-faint")} />
            </span>
            {activeJobs > 0 ? plural(activeJobs, "job en cours", "jobs en cours") : "Aucun job en cours"}
          </Link>
          <Link to="/budget" className="no-drag block">
            <div className="flex items-baseline justify-between text-[12.5px]">
              <span className="text-muted">Budget</span>
              <span className="tabular text-fg">
                {budget ? (
                  <>
                    {formatEur(budget.spent_eur, { compact: true, digits: 0 })}
                    <span className="text-muted"> / {formatEur(budget.total_eur, { compact: true, digits: 0 })}</span>
                  </>
                ) : (
                  "—"
                )}
              </span>
            </div>
            <ProgressBar
              value={spentFrac}
              size="xs"
              tone={spentFrac != null && spentFrac > 0.9 ? "danger" : spentFrac != null && spentFrac > 0.75 ? "warning" : "accent"}
              className="mt-1.5"
            />
          </Link>
          {(me.data || canLogout) && (
            <div className="flex items-center justify-between gap-2 text-[12.5px]">
              <Link
                to="/settings"
                className="no-drag flex min-w-0 items-center gap-2 text-muted hover:text-fg"
                title={me.data ? `Connecté en tant que ${me.data.name}${me.data.email ? ` (${me.data.email})` : ""} · ${ROLE_LABELS[me.data.role] ?? me.data.role}` : undefined}
              >
                <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-accent-soft text-[10.5px] font-semibold uppercase text-accent" aria-hidden>
                  {(me.data?.name ?? "?").slice(0, 1)}
                </span>
                <span className="truncate">
                  {me.data ? me.data.name : "Utilisateur inconnu"}
                  {me.data && (me.data.role !== "admin" || me.data.name !== "admin") && (
                    <span className="text-faint"> · {me.data.role === "admin" ? "admin" : "membre"}</span>
                  )}
                </span>
              </Link>
              {canLogout && <IconButton icon={LogOut} label="Se déconnecter" size="sm" onClick={logout} tooltipSide="right" />}
            </div>
          )}
        </div>
      </div>
      {perms.canWrite && <NewProjectDialog open={newProjectOpen} onClose={() => setNewProjectOpen(false)} />}
    </aside>
  );
}
