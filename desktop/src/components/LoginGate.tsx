/**
 * Login screen of the team mode (§12.6). Shown when no token is known or when the backend
 * refuses it (`GET /api/me` → 401). In a browser the token (and the server URL when the page
 * is not served by the backend) is memorised in localStorage; in Electron the session comes
 * from the main process (`window.wmlab`): the local mode never shows this screen, the remote
 * mode points to the "Serveur" menu when the stored token is refused.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { CircleAlert, KeyRound, Laptop, LogIn, Server } from "lucide-react";
import { Button, Callout, Card, Input, Spinner } from "./ui";
import { clearSession, getApiConfig, normalizeApiBase, probeConnection, setSession, UNAUTHORIZED_EVENT } from "../lib/api";
import { errorStatus, qk, useMe } from "../lib/queries";

interface SessionActions {
  /** Forget the token and show the login screen (browser) / the connection dialog (Electron). */
  logout: () => void;
}

const SessionContext = createContext<SessionActions>({ logout: () => undefined });

/** `const { logout } = useSession()` — used by the sidebar footer and the settings page. */
export function useSession(): SessionActions {
  return useContext(SessionContext);
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <div className="relative flex h-full w-full items-center justify-center bg-canvas text-fg">
      {/* Draggable strip for the frameless macOS window. */}
      <div className="drag absolute inset-x-0 top-0 h-[52px]" aria-hidden />
      <div className="no-drag w-full max-w-[440px] px-6 pb-12">{children}</div>
    </div>
  );
}

function LoadingScreen() {
  return (
    <Frame>
      <div className="flex items-center justify-center gap-2.5 text-[13.5px] text-muted">
        <Spinner size={16} />
        Connexion au serveur…
      </div>
    </Frame>
  );
}

/** Electron, remote mode: no token stored, or the stored token was refused — the main-process dialog fixes it. */
function ElectronRefused({ apiBase, reason, onRetry }: { apiBase: string; reason: "missing" | "refused"; onRetry: () => void }) {
  const bridge = window.wmlab;
  return (
    <Frame>
      <h1 className="font-serif text-[30px] leading-tight">Watermark Lab</h1>
      <p className="mt-2 text-[14px] text-muted">
        {reason === "missing" ? "Aucun jeton n'est enregistré pour le serveur distant." : "Le serveur distant a refusé le jeton enregistré."}
      </p>
      <Card padding="lg" className="mt-6 space-y-4">
        <Callout tone={reason === "missing" ? "info" : "danger"} icon={KeyRound} title={reason === "missing" ? "Connexion requise" : "Jeton refusé"}>
          {reason === "missing" ? (
            <>
              Connectez-vous à <span className="font-mono text-[12px]">{apiBase}</span> avec votre jeton personnel (menu Serveur).
            </>
          ) : (
            <>
              <span className="font-mono text-[12px]">{apiBase}</span> ne reconnaît plus ce jeton : il a peut-être été révoqué. Demandez-en un
              nouveau à un administrateur (<span className="font-mono text-[12px]">wmlab users add</span>), puis reconnectez-vous.
            </>
          )}
        </Callout>
        <div className="flex flex-wrap gap-2">
          <Button variant="primary" icon={Server} onClick={() => bridge?.openConnectDialog()}>
            Se connecter à un serveur distant…
          </Button>
          <Button icon={Laptop} onClick={() => bridge?.useLocalBackend()}>
            Utiliser le backend local
          </Button>
          <Button variant="ghost" onClick={onRetry}>
            Réessayer
          </Button>
        </div>
      </Card>
    </Frame>
  );
}

function LoginForm({ initialError, onLoggedIn }: { initialError: string | null; onLoggedIn: () => void }) {
  const config = getApiConfig();
  const servedByBackend = typeof window !== "undefined" && config.apiBase === window.location.origin;
  const [apiBase, setApiBase] = useState(config.apiBase);
  const [showUrl, setShowUrl] = useState(!servedByBackend);
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(initialError);
  const [busy, setBusy] = useState(false);

  // The page may be opened from a file or another origin: when the origin is not a backend,
  // ask for the server URL.
  useEffect(() => {
    if (!servedByBackend) return;
    let cancelled = false;
    void probeConnection(window.location.origin, "", 4000).then((r) => {
      if (!cancelled && !r.reachable) setShowUrl(true);
    });
    return () => {
      cancelled = true;
    };
  }, [servedByBackend]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const base = normalizeApiBase(apiBase);
    if (showUrl && !base) return setError("Indiquez l'adresse du serveur (https://…).");
    const t = token.trim();
    if (!t) return setError("Indiquez votre jeton.");
    setBusy(true);
    setError(null);
    try {
      const target = showUrl ? base! : config.apiBase;
      const res = await probeConnection(target, t);
      if (!res.reachable || !res.me) {
        setError(res.error ?? "Connexion refusée.");
        return;
      }
      setSession(t, target);
      onLoggedIn();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Frame>
      <h1 className="font-serif text-[30px] leading-tight">Watermark Lab</h1>
      <p className="mt-2 text-[14px] text-muted">Connectez-vous au serveur de l'équipe avec votre jeton personnel.</p>
      <Card padding="lg" className="mt-6">
        <form className="space-y-4" onSubmit={(e) => void submit(e)}>
          {showUrl && (
            <Input
              label="Serveur"
              mono
              value={apiBase}
              onChange={(e) => setApiBase(e.target.value)}
              placeholder="https://lab.example.com"
              hint="Adresse publique du backend Watermark Lab."
              autoComplete="url"
              spellCheck={false}
            />
          )}
          <Input
            label="Jeton"
            type="password"
            mono
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="wml_…"
            hint="Fourni par un administrateur (wmlab users add <nom>). Mémorisé dans ce navigateur."
            autoComplete="current-password"
            autoFocus
          />
          {error && (
            <Callout tone="danger" icon={CircleAlert}>
              {error}
            </Callout>
          )}
          <Button type="submit" variant="primary" icon={LogIn} fullWidth loading={busy}>
            Se connecter
          </Button>
        </form>
      </Card>
      {!showUrl && (
        <button type="button" className="mt-3 text-[12.5px] text-muted hover:text-fg" onClick={() => setShowUrl(true)}>
          Se connecter à un autre serveur…
        </button>
      )}
    </Frame>
  );
}

/**
 * Wraps the app: renders the login screen until a token is known and accepted. Electron in
 * local mode is passed through untouched (the token comes from the main process).
 */
export function LoginGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [, bump] = useState(0);
  const config = getApiConfig();
  const electronLocal = config.source === "electron" && config.mode === "local";
  const hasToken = !!config.token;
  const me = useMe({ enabled: hasToken && !electronLocal });

  // A 401 anywhere (token revoked while the app is open) brings the screen back.
  useEffect(() => {
    if (electronLocal) return;
    const onUnauthorized = () => void qc.invalidateQueries({ queryKey: qk.me });
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [qc, electronLocal]);

  const reset = useCallback(() => {
    qc.clear();
    bump((v) => v + 1);
  }, [qc]);

  const logout = useCallback(() => {
    if (config.source === "electron") {
      window.wmlab?.disconnect();
      return;
    }
    clearSession();
    reset();
  }, [config.source, reset]);

  const actions = useMemo(() => ({ logout }), [logout]);

  if (electronLocal) return <SessionContext.Provider value={actions}>{children}</SessionContext.Provider>;

  const unauthorized = me.isError && errorStatus(me.error) === 401;
  if (!hasToken || unauthorized) {
    if (config.source === "electron") {
      return <ElectronRefused apiBase={config.apiBase} reason={hasToken ? "refused" : "missing"} onRetry={() => void qc.invalidateQueries({ queryKey: qk.me })} />;
    }
    return (
      <LoginForm
        key={String(unauthorized)}
        initialError={unauthorized && hasToken ? "Le serveur a refusé le jeton mémorisé : il a peut-être été révoqué." : null}
        onLoggedIn={reset}
      />
    );
  }
  if (me.isPending) return <LoadingScreen />;
  return <SessionContext.Provider value={actions}>{children}</SessionContext.Provider>;
}
