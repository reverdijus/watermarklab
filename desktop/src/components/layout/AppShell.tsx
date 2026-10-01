import clsx from "clsx";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router";
import { PanelLeftOpen, Plus, WifiOff } from "lucide-react";
import { Sidebar } from "./Sidebar";
import { IconButton } from "../ui";
import { useLaunch } from "../launch/LaunchProvider";
import { useHotkey, useLocalStorage } from "../../lib/hooks";
import { useHealth, usePermissions } from "../../lib/queries";

/**
 * Window layout: sidebar + scrollable content, draggable title strip for the
 * frameless macOS window. The root carries `data-app-ready` once mounted AND
 * the backend answered `/api/health` **with** `workspace`, which it only returns to a
 * request carrying the session token — so the flag proves the token reached the renderer
 * (used by the Electron self-test and the render check).
 */
export function AppShell({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useLocalStorage("wmlab.sidebar.collapsed", false);
  const [ready, setReady] = useState(false);
  const { openLaunch } = useLaunch();
  const location = useLocation();
  const scrollRef = useRef<HTMLDivElement>(null);
  const health = useHealth();
  const { canWrite } = usePermissions();

  useEffect(() => setReady(true), []);
  useHotkey("mod+\\", () => setCollapsed((c) => !c));

  // Scroll back to the top when the page changes (not on query-string changes).
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0 });
  }, [location.pathname]);

  const offline = health.isError && health.failureCount > 0;

  return (
    <div data-app-ready={ready && health.isSuccess && !!health.data?.workspace ? "true" : undefined} className="flex h-full w-full overflow-hidden bg-canvas text-fg">
      <Sidebar collapsed={collapsed} onToggle={() => setCollapsed((c) => !c)} />
      <main className="relative flex min-w-0 flex-1 flex-col">
        {/* Title strip: draggable, hosts the sidebar toggle when collapsed. */}
        <div
          className={clsx(
            "drag flex h-[52px] shrink-0 items-center gap-1 transition-[padding] duration-200",
            collapsed ? "pl-[84px]" : "pl-4",
          )}
        >
          {collapsed && (
            <>
              <IconButton icon={PanelLeftOpen} label="Afficher la barre latérale (⌘\)" size="sm" onClick={() => setCollapsed(false)} />
              {canWrite && <IconButton icon={Plus} label="Nouveau lancement (⌘N)" size="sm" onClick={() => openLaunch()} />}
            </>
          )}
          {offline && (
            <div className="no-drag ml-auto mr-4 inline-flex items-center gap-1.5 rounded-full bg-danger/10 px-2.5 py-1 text-[12px] text-danger">
              <WifiOff size={13} strokeWidth={1.75} aria-hidden />
              Backend injoignable — nouvelle tentative…
            </div>
          )}
        </div>
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto" id="main-scroll">
          {children}
        </div>
      </main>
    </div>
  );
}
