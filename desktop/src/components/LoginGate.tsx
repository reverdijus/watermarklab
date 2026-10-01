/**
 * Login screen of the team mode (§12.6). Shown when no session is known or when the backend
 * refuses it (`GET /api/me` → 401): e-mail + password (`POST /api/auth/login`), and the
 * self-registration form (`POST /api/auth/register`, hidden when `/api/health` says
 * `registration: false`). In a browser the session token (and the server URL when the page is
 * not served by the backend) is memorised in localStorage; in Electron the session comes from
 * the main process (`window.wmlab`): the local mode never shows this screen, the remote mode
 * points to the "Serveur" menu (connection dialog of the main process). A temporary password
 * (`Me.must_change_password`) blocks the app behind « Définir un nouveau mot de passe ».
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type FormEvent, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CircleAlert, CircleCheck, KeyRound, Laptop, LogIn, LogOut, Server, UserPlus } from "lucide-react";
import { Button, Callout, Card, Input, Spinner } from "./ui";
import { PasswordChangeForm } from "./account/PasswordChangeForm";
import { api, ApiError, clearSession, errorMessage, getApiConfig, normalizeApiBase, probeConnection, setSession, UNAUTHORIZED_EVENT } from "../lib/api";
import {
  isValid,
  normalizeEmail,
  PASSWORD_MIN,
  validateLogin,
  validateRegistration,
  type FieldErrors,
  type LoginFields,
  type RegisterFields,
} from "../lib/auth";
import { useDebouncedValue } from "../lib/hooks";
import { errorStatus, qk, useMe } from "../lib/queries";
import type { Me } from "../lib/types";

interface SessionActions {
  /** Revoke the session (best effort) and forget it: login screen (browser) / connection dialog (Electron). */
  logout: () => void;
}

const SessionContext = createContext<SessionActions>({ logout: () => undefined });

/** `const { logout } = useSession()` — used by the sidebar footer and the settings page. */
export function useSession(): SessionActions {
  return useContext(SessionContext);
}

function Frame({ children }: { children: ReactNode }) {
  return (
    <div className="relative flex h-full w-full items-center justify-center overflow-y-auto bg-canvas text-fg">
      {/* Draggable strip for the frameless macOS window. */}
      <div className="drag absolute inset-x-0 top-0 h-[52px]" aria-hidden />
      <div className="no-drag w-full max-w-[440px] px-6 py-12">{children}</div>
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

/** Electron, remote mode: no session stored, or the stored session was refused — the main-process dialog fixes it. */
function ElectronRefused({ apiBase, reason, onRetry }: { apiBase: string; reason: "missing" | "refused"; onRetry: () => void }) {
  const bridge = window.wmlab;
  return (
    <Frame>
      <h1 className="font-serif text-[30px] leading-tight">Watermark Lab</h1>
      <p className="mt-2 text-[14px] text-muted">
        {reason === "missing" ? "Aucune session n'est enregistrée pour le serveur distant." : "Le serveur distant a refusé la session enregistrée."}
      </p>
      <Card padding="lg" className="mt-6 space-y-4">
        <Callout tone={reason === "missing" ? "info" : "warning"} icon={KeyRound} title={reason === "missing" ? "Connexion requise" : "Session expirée"}>
          {reason === "missing" ? (
            <>
              Connectez-vous à <span className="font-mono text-[12px]">{apiBase}</span> avec votre e-mail et votre mot de passe (menu Serveur).
            </>
          ) : (
            <>
              <span className="font-mono text-[12px]">{apiBase}</span> ne reconnaît plus cette session : elle a expiré, a été révoquée (déconnexion,
              changement de mot de passe) ou le compte a été désactivé. Reconnectez-vous.
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

/** Message of a failed login / registration request (the backend `detail` as is). */
function requestError(err: unknown, target: string): string {
  if (err instanceof ApiError && err.status === 0) return `Serveur injoignable : ${target}`;
  if (err instanceof ApiError && err.status === 404) return `${target} ne propose pas la connexion par e-mail : le serveur est-il à jour ?`;
  return errorMessage(err);
}

type Mode = "login" | "register" | "registered";

function LoginScreen({ notice, onLoggedIn }: { notice: string | null; onLoggedIn: (user: Me) => void }) {
  const config = getApiConfig();
  const servedByBackend = typeof window !== "undefined" && config.apiBase === window.location.origin;
  const [apiBase, setApiBase] = useState(config.apiBase);
  const [showUrl, setShowUrl] = useState(!servedByBackend);
  const [mode, setMode] = useState<Mode>("login");
  const [registration, setRegistration] = useState<boolean | null>(null);
  const [login, setLogin] = useState<LoginFields>({ email: "", password: "" });
  const [reg, setReg] = useState<RegisterFields>({ name: "", email: "", password: "", confirm: "" });
  const [loginErrors, setLoginErrors] = useState<FieldErrors<keyof LoginFields>>({});
  const [regErrors, setRegErrors] = useState<FieldErrors<keyof RegisterFields>>({});
  const [error, setError] = useState<string | null>(null);
  const [registeredMessage, setRegisteredMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const target = showUrl ? normalizeApiBase(apiBase) : config.apiBase;
  const probeTarget = useDebouncedValue(target, 500);

  // Public health probe: is the target a backend (otherwise ask for the server URL), and is
  // self-registration open?
  useEffect(() => {
    if (!probeTarget) {
      setRegistration(null);
      return;
    }
    let cancelled = false;
    void probeConnection(probeTarget, "", 4000).then((r) => {
      if (cancelled) return;
      setRegistration(r.registration);
      if (!r.reachable && probeTarget === window.location.origin) setShowUrl(true);
    });
    return () => {
      cancelled = true;
    };
  }, [probeTarget]);

  const editReg = (key: keyof RegisterFields, value: string) => {
    setReg((r) => ({ ...r, [key]: value }));
    setRegErrors((errs) => ({ ...errs, [key]: undefined }));
  };

  const switchMode = (next: Mode) => {
    setError(null);
    setLoginErrors({});
    setRegErrors({});
    if (next === "register" && !reg.email && login.email) setReg((r) => ({ ...r, email: login.email }));
    if (next === "login" && !login.email && reg.email) setLogin((l) => ({ ...l, email: reg.email }));
    setMode(next);
  };

  const submitLogin = async (e: FormEvent) => {
    e.preventDefault();
    if (!target) return setError("Indiquez l'adresse du serveur (https://…).");
    const errs = validateLogin(login);
    setLoginErrors(errs);
    setError(null);
    if (!isValid(errs)) return;
    setBusy(true);
    try {
      const res = await api.login({ email: normalizeEmail(login.email), password: login.password }, target);
      setSession(res.token, target);
      onLoggedIn(res.user);
    } catch (err) {
      setError(requestError(err, target));
    } finally {
      setBusy(false);
    }
  };

  const submitRegister = async (e: FormEvent) => {
    e.preventDefault();
    if (!target) return setError("Indiquez l'adresse du serveur (https://…).");
    const errs = validateRegistration(reg);
    setRegErrors(errs);
    setError(null);
    if (!isValid(errs)) return;
    setBusy(true);
    try {
      const res = await api.register({ email: normalizeEmail(reg.email), name: reg.name.trim(), password: reg.password }, target);
      setRegisteredMessage(res?.message || null);
      setLogin({ email: reg.email, password: "" });
      setReg({ name: "", email: "", password: "", confirm: "" });
      setMode("registered");
    } catch (err) {
      setError(requestError(err, target));
    } finally {
      setBusy(false);
    }
  };

  const urlField = showUrl && mode !== "registered" && (
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
  );
  const errorBox = error && (
    <Callout tone="danger" icon={CircleAlert}>
      {error}
    </Callout>
  );

  return (
    <Frame>
      <h1 className="font-serif text-[30px] leading-tight">Watermark Lab</h1>
      <p className="mt-2 text-[14px] text-muted">
        {mode === "register" ? "Demandez un compte sur le serveur de l'équipe." : "Connectez-vous au serveur de l'équipe."}
      </p>
      {notice && mode === "login" && (
        <Callout tone="warning" icon={KeyRound} className="mt-6">
          {notice}
        </Callout>
      )}
      <Card padding="lg" className="mt-6">
        {mode === "login" && (
          <form className="space-y-4" onSubmit={(e) => void submitLogin(e)}>
            {urlField}
            <Input
              label="E-mail"
              type="email"
              value={login.email}
              onChange={(e) => {
                setLogin((l) => ({ ...l, email: e.target.value }));
                setLoginErrors((errs) => ({ ...errs, email: undefined }));
              }}
              error={loginErrors.email}
              placeholder="prenom@exemple.com"
              autoComplete="username"
              spellCheck={false}
              autoFocus
            />
            <Input
              label="Mot de passe"
              type="password"
              value={login.password}
              onChange={(e) => {
                setLogin((l) => ({ ...l, password: e.target.value }));
                setLoginErrors((errs) => ({ ...errs, password: undefined }));
              }}
              error={loginErrors.password}
              autoComplete="current-password"
            />
            {errorBox}
            <Button type="submit" variant="primary" icon={LogIn} fullWidth loading={busy}>
              Se connecter
            </Button>
          </form>
        )}
        {mode === "register" && (
          <form className="space-y-4" onSubmit={(e) => void submitRegister(e)}>
            {urlField}
            <Input
              label="Identifiant"
              mono
              value={reg.name}
              onChange={(e) => editReg("name", e.target.value)}
              error={regErrors.name}
              hint="Lettres, chiffres et _ - . ; affiché comme auteur de vos runs."
              placeholder="prenom"
              autoComplete="nickname"
              spellCheck={false}
              autoFocus
            />
            <Input
              label="E-mail"
              type="email"
              value={reg.email}
              onChange={(e) => editReg("email", e.target.value)}
              error={regErrors.email}
              placeholder="prenom@exemple.com"
              autoComplete="email"
              spellCheck={false}
            />
            <Input
              label="Mot de passe"
              type="password"
              value={reg.password}
              onChange={(e) => editReg("password", e.target.value)}
              error={regErrors.password}
              hint={`${PASSWORD_MIN} caractères minimum ; une phrase de passe convient très bien.`}
              autoComplete="new-password"
            />
            <Input
              label="Confirmation"
              type="password"
              value={reg.confirm}
              onChange={(e) => editReg("confirm", e.target.value)}
              error={regErrors.confirm}
              autoComplete="new-password"
            />
            {errorBox}
            <Button type="submit" variant="primary" icon={UserPlus} fullWidth loading={busy}>
              Demander un compte
            </Button>
          </form>
        )}
        {mode === "registered" && (
          <div className="space-y-4">
            <Callout tone="success" icon={CircleCheck} title="Demande enregistrée : un administrateur doit approuver votre compte">
              {registeredMessage && registeredMessage !== "Demande enregistrée : un administrateur doit approuver votre compte"
                ? registeredMessage
                : "Vous pourrez vous connecter dès que votre compte sera approuvé."}
            </Callout>
            <Button variant="primary" icon={ArrowLeft} fullWidth onClick={() => switchMode("login")}>
              Retour à la connexion
            </Button>
          </div>
        )}
      </Card>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-[12.5px]">
        {mode === "login" && registration !== false ? (
          <button type="button" className="no-drag text-muted hover:text-fg" onClick={() => switchMode("register")}>
            Créer un compte
          </button>
        ) : mode === "register" ? (
          <button type="button" className="no-drag text-muted hover:text-fg" onClick={() => switchMode("login")}>
            J'ai déjà un compte
          </button>
        ) : (
          <span />
        )}
        {!showUrl && mode !== "registered" && (
          <button type="button" className="no-drag text-muted hover:text-fg" onClick={() => setShowUrl(true)}>
            Se connecter à un autre serveur…
          </button>
        )}
      </div>
    </Frame>
  );
}

/** Temporary password (`Me.must_change_password`): nothing else is shown until it is changed. */
function ChangePasswordScreen({ me, onChanged, onLogout }: { me: Me; onChanged: () => void; onLogout: () => void }) {
  return (
    <Frame>
      <h1 className="font-serif text-[30px] leading-tight">Définir un nouveau mot de passe</h1>
      <p className="mt-2 text-[14px] text-muted">
        Connecté en tant que <span className="font-medium text-fg">{me.name}</span>
        {me.email && <> ({me.email})</>}. Votre mot de passe est temporaire : choisissez-en un nouveau pour continuer.
      </p>
      <Card padding="lg" className="mt-6">
        <PasswordChangeForm me={me} onChanged={onChanged} submitLabel="Enregistrer le mot de passe" autoFocus />
      </Card>
      <button type="button" className="no-drag mt-3 inline-flex items-center gap-1.5 text-[12.5px] text-muted hover:text-fg" onClick={onLogout}>
        <LogOut size={13} strokeWidth={1.75} aria-hidden /> Se déconnecter
      </button>
    </Frame>
  );
}

/**
 * Wraps the app: renders the login screen until a session is known and accepted, then the
 * forced password change when needed. Electron in local mode is passed through untouched
 * (server token from the main process, implicit admin).
 */
export function LoginGate({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const [, bump] = useState(0);
  const config = getApiConfig();
  const electronLocal = config.source === "electron" && config.mode === "local";
  const hasToken = !!config.token;
  const me = useMe({ enabled: hasToken && !electronLocal });

  // A 401 anywhere (session revoked or expired while the app is open) brings the screen back.
  useEffect(() => {
    if (electronLocal) return;
    const onUnauthorized = () => void qc.invalidateQueries({ queryKey: qk.me });
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [qc, electronLocal]);

  const reset = useCallback(
    (user?: Me) => {
      qc.clear();
      if (user) qc.setQueryData(qk.me, user);
      bump((v) => v + 1);
    },
    [qc],
  );

  const logout = useCallback(() => {
    const current = getApiConfig();
    if (current.source === "electron") {
      // The main process revokes the session (POST /api/auth/logout) and forgets it.
      window.wmlab?.disconnect();
      return;
    }
    // Best effort: the request carries the token before it is forgotten; errors are ignored.
    if (current.token) void api.logout().catch(() => undefined);
    clearSession();
    reset();
  }, [reset]);

  const actions = useMemo(() => ({ logout }), [logout]);

  if (electronLocal) return <SessionContext.Provider value={actions}>{children}</SessionContext.Provider>;

  const unauthorized = me.isError && errorStatus(me.error) === 401;
  if (!hasToken || unauthorized) {
    if (config.source === "electron") {
      return <ElectronRefused apiBase={config.apiBase} reason={hasToken ? "refused" : "missing"} onRetry={() => void qc.invalidateQueries({ queryKey: qk.me })} />;
    }
    return (
      <LoginScreen
        key={String(unauthorized)}
        notice={unauthorized && hasToken ? "Votre session a expiré ou a été révoquée : reconnectez-vous." : null}
        onLoggedIn={reset}
      />
    );
  }
  if (me.isPending) return <LoadingScreen />;
  if (me.data?.must_change_password) {
    return (
      <SessionContext.Provider value={actions}>
        <ChangePasswordScreen
          me={me.data}
          onChanged={() => qc.setQueryData(qk.me, (old: Me | undefined) => (old ? { ...old, must_change_password: false } : old))}
          onLogout={logout}
        />
      </SessionContext.Provider>
    );
  }
  return <SessionContext.Provider value={actions}>{children}</SessionContext.Provider>;
}
