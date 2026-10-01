/**
 * Typed HTTP client for every route of ARCHITECTURE.md §9 and §12.
 *
 * Connection settings are detected once:
 *  1. Electron: `window.wmlab` exposed by the preload script (local backend started by the
 *     main process, or a remote server chosen through the "Serveur" menu — `mode`);
 *  2. Vite dev server in a browser: `VITE_API_BASE` / `VITE_API_TOKEN`;
 *  3. Browser served by FastAPI: `window.location.origin` (or a server URL chosen on the
 *     login screen) + the session token obtained by e-mail + password (`POST /api/auth/login`),
 *     memorised in localStorage `wmlab.token`. `?token=` is never read from the URL (§10): a
 *     token in a URL ends up in logs and history; only `fileUrl()` / `costsCsvUrl()` carry it.
 */
import type {
  BenchmarkInfo,
  CompareResponse,
  ChangePasswordBody,
  CreatedPath,
  CreatedUser,
  CreateExperimentBody,
  CreateHpoPlanBody,
  CreateJobBody,
  CreateProjectBody,
  CreateUserBody,
  Costs,
  DatasetInfo,
  DecommissionBody,
  DecommissionResult,
  EstimateBody,
  FileContent,
  GitState,
  Health,
  Host,
  HostInstance,
  HostInventory,
  HostTestResult,
  Job,
  JobEstimate,
  Leaderboard,
  LeaderboardQuery,
  LoginBody,
  LoginResult,
  LogsResponse,
  Me,
  MetricsResponse,
  OkResponse,
  Overview,
  PoweroffResult,
  ProjectDetail,
  ProjectSummary,
  RegisterBody,
  RegisterResult,
  RegistryResponse,
  RunDetail,
  RunsQuery,
  RunSummary,
  SaveFileResult,
  StudyDetail,
  StudySummary,
  SystemResponse,
  UserInfo,
  UserRole,
  ValidateBody,
  ValidateResult,
  WorkspaceSettings,
} from "./types";

export type ConnectionMode = "local" | "remote";

export interface ApiConfig {
  apiBase: string;
  token: string;
  platform: string;
  /** Where the settings came from (useful in diagnostics). */
  source: "electron" | "vite-env" | "browser";
  /** Electron: local backend started by the app, or remote server (§12.6). Browser: always "remote". */
  mode: ConnectionMode;
}

/** localStorage keys of the browser session (§12.6). */
export const TOKEN_STORAGE_KEY = "wmlab.token";
export const API_BASE_STORAGE_KEY = "wmlab.apiBase";

function storageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode, quota): the session lives in memory only */
  }
}

/** Normalise a server URL typed by a user ("lab.example.com/" → "https://lab.example.com"). */
export function normalizeApiBase(raw: string): string | null {
  let s = raw.trim();
  if (!s) return null;
  // No scheme: https, except for loopback addresses (a local backend has no certificate).
  if (!/^https?:\/\//i.test(s)) s = `${/^(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i.test(s) ? "http" : "https"}://${s}`;
  try {
    const url = new URL(s);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    url.hash = "";
    url.search = "";
    return `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
  } catch {
    return null;
  }
}

function detectConfig(): ApiConfig {
  const bridge = typeof window !== "undefined" ? window.wmlab : undefined;
  if (bridge?.apiBase) {
    return {
      apiBase: bridge.apiBase.replace(/\/+$/, ""),
      token: bridge.token ?? "",
      platform: bridge.platform ?? "darwin",
      source: "electron",
      mode: bridge.mode === "remote" ? "remote" : "local",
    };
  }
  const envBase = import.meta.env.VITE_API_BASE;
  if (envBase) {
    return {
      apiBase: envBase.replace(/\/+$/, ""),
      token: import.meta.env.VITE_API_TOKEN ?? "",
      platform: navigator.platform.toLowerCase().includes("mac") ? "darwin" : "web",
      source: "vite-env",
      mode: "remote",
    };
  }
  // Browser: session token memorised by the login screen (never read from the URL).
  const token = storageGet(TOKEN_STORAGE_KEY);
  const storedBase = storageGet(API_BASE_STORAGE_KEY);
  return {
    apiBase: (storedBase && normalizeApiBase(storedBase)) || window.location.origin,
    token: token ?? "",
    platform: "web",
    source: "browser",
    mode: "remote",
  };
}

let cachedConfig: ApiConfig | null = null;

/** Current connection settings (detected lazily, then cached). */
export function getApiConfig(): ApiConfig {
  if (!cachedConfig) cachedConfig = detectConfig();
  return cachedConfig;
}

/** True when running inside the Electron shell. */
export function isElectron(): boolean {
  return getApiConfig().source === "electron";
}

/**
 * Browser session (§12.6): memorise the session token (and optionally the server URL when the
 * page is not served by the backend) after a successful login. In Electron the session is owned
 * by the main process (config.json); this only updates the in-memory settings.
 */
export function setSession(token: string, apiBase?: string | null): void {
  const current = getApiConfig();
  const base = apiBase ? normalizeApiBase(apiBase) : null;
  cachedConfig = { ...current, token, apiBase: base ?? current.apiBase };
  if (current.source === "browser") {
    storageSet(TOKEN_STORAGE_KEY, token);
    storageSet(API_BASE_STORAGE_KEY, base && base !== window.location.origin ? base : null);
  }
}

/** Forget the session token ("Se déconnecter"). The server URL is kept. */
export function clearSession(): void {
  const current = getApiConfig();
  cachedConfig = { ...current, token: "" };
  if (current.source === "browser") storageSet(TOKEN_STORAGE_KEY, null);
}

/** Event dispatched on `window` whenever a request is refused with 401 (LoginGate listens). */
export const UNAUTHORIZED_EVENT = "wmlab:unauthorized";

/** Result of `probeConnection`. */
export interface ProbeResult {
  /** The server answered `/api/health` (public route). */
  reachable: boolean;
  version: string | null;
  /** `Health.registration`: self-registration open (null when unknown / older backend). */
  registration: boolean | null;
  /** `GET /api/me` with the token: the user, or null when the token was refused. */
  me: Me | null;
  /** Diagnostic (French) when reachable is false or the token was refused. */
  error: string | null;
}

/** Implicit user of an older backend without `/api/me` (token accepted, admin). */
const LEGACY_ADMIN: Me = {
  id: "",
  name: "admin",
  email: "",
  role: "admin",
  hosted: false,
  public_url: null,
  must_change_password: false,
  auth: "server_token",
};

async function fetchJson(url: string, token: string | null, timeoutMs: number): Promise<{ status: number; body: unknown }> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (token) headers["X-WMLab-Token"] = token;
    const res = await fetch(url, { headers, signal: ctrl.signal });
    const text = await res.text();
    let body: unknown = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = text;
    }
    return { status: res.status, body };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Login screen helper: checks that `apiBase` is a Watermark Lab backend (`/api/health`, public:
 * also tells whether self-registration is open), then — with a token — that the session is
 * accepted (`/api/me`). Never throws. An older backend without `/api/me` answers 404: the
 * token is then considered accepted when `/api/health` returned `workspace` (only sent to an
 * authenticated request).
 */
export async function probeConnection(apiBase: string, token: string, timeoutMs = 8000): Promise<ProbeResult> {
  const base = apiBase.replace(/\/+$/, "");
  const unreachable = (error: string): ProbeResult => ({ reachable: false, version: null, registration: null, me: null, error });
  let health: { status: number; body: unknown };
  try {
    health = await fetchJson(`${base}/api/health`, token || null, timeoutMs);
  } catch {
    return unreachable(`Serveur injoignable : ${base}`);
  }
  const hb = health.body as { ok?: unknown; version?: unknown; workspace?: unknown; registration?: unknown } | null;
  if (health.status !== 200 || !hb || typeof hb !== "object" || hb.ok !== true) {
    return unreachable(`${base} ne répond pas comme un backend Watermark Lab (HTTP ${health.status}).`);
  }
  const version = typeof hb.version === "string" ? hb.version : null;
  const registration = typeof hb.registration === "boolean" ? hb.registration : null;
  const ok = { reachable: true, version, registration };
  if (!token) return { ...ok, me: null, error: null };
  let me: { status: number; body: unknown };
  try {
    me = await fetchJson(`${base}/api/me`, token, timeoutMs);
  } catch {
    return { ...ok, me: null, error: "Le serveur n'a pas répondu à la vérification de la session." };
  }
  if (me.status === 200 && me.body && typeof me.body === "object" && typeof (me.body as Me).name === "string") {
    return { ...ok, me: me.body as Me, error: null };
  }
  if (me.status === 404 && typeof hb.workspace === "string") return { ...ok, me: LEGACY_ADMIN, error: null };
  if (me.status === 401 || me.status === 403 || me.status === 404) {
    return { ...ok, me: null, error: "Session refusée par le serveur (expirée ou révoquée) : reconnectez-vous." };
  }
  return { ...ok, me: null, error: detailFromBody(me.body, statusFallback(me.status)) };
}

/** Error raised for any failed request; `detail` is the backend message (French when possible). */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;
  /** `Retry-After` (seconds) of a 429, when the server sent it. */
  readonly retryAfter: number | null;

  constructor(status: number, detail: string, retryAfter: number | null = null) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter;
  }
}

/** Human readable message for any thrown value. */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  if (err instanceof Error) return err.message;
  if (typeof err === "string") return err;
  return "Erreur inconnue";
}

type QueryValue = string | number | boolean | null | undefined;

function buildQuery(params?: Record<string, QueryValue>): string {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    usp.set(key, String(value));
  }
  const s = usp.toString();
  return s ? `?${s}` : "";
}

function detailFromBody(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      // FastAPI request validation errors: [{loc: [...], msg}]
      return detail
        .map((d) => {
          if (d && typeof d === "object") {
            const loc = Array.isArray((d as { loc?: unknown }).loc)
              ? ((d as { loc: unknown[] }).loc).filter((p) => p !== "body").join(".")
              : "";
            const msg = String((d as { msg?: unknown }).msg ?? "");
            return loc ? `${loc} : ${msg}` : msg;
          }
          return String(d);
        })
        .join(" · ");
    }
    if (detail != null) return JSON.stringify(detail);
  }
  return fallback;
}

function statusFallback(status: number, retryAfter: number | null = null): string {
  if (status === 401) return "Session expirée ou refusée : reconnectez-vous.";
  if (status === 403) return "Action réservée aux administrateurs.";
  if (status === 404) return "Ressource introuvable.";
  if (status === 409) return "Conflit : l'opération n'est pas possible dans l'état actuel.";
  if (status === 422) return "Requête invalide.";
  if (status === 429) {
    return retryAfter ? `Trop de tentatives : réessayez dans ${Math.max(1, Math.ceil(retryAfter / 60))} min.` : "Trop de tentatives : réessayez plus tard.";
  }
  if (status >= 500) return `Erreur interne du backend (${status}).`;
  return `Erreur HTTP ${status}.`;
}

interface RequestOptions {
  query?: Record<string, QueryValue>;
  body?: unknown;
  signal?: AbortSignal;
}

interface RequestTarget {
  /** Server to call instead of the current session's (login screen with another URL). */
  apiBase?: string;
  /** Public route (§9): no token header. */
  anonymous?: boolean;
}

/** Low-level request helper (JSON in, JSON out). */
export async function request<T>(method: string, path: string, opts: RequestOptions & RequestTarget = {}): Promise<T> {
  const config = getApiConfig();
  const apiBase = (opts.apiBase ?? config.apiBase).replace(/\/+$/, "");
  const token = opts.anonymous ? "" : config.token;
  const headers: Record<string, string> = { Accept: "application/json" };
  if (token) headers["X-WMLab-Token"] = token;
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  let res: Response;
  try {
    res = await fetch(`${apiBase}${path}${buildQuery(opts.query)}`, {
      method,
      headers,
      body,
      signal: opts.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "Impossible de joindre le backend Watermark Lab. Vérifiez qu'il est bien démarré.");
  }
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    // A 401 on an authenticated route means the session is gone (expired, revoked, account
    // disabled): the LoginGate takes over. The /api/auth/* routes answer 401 for a wrong
    // password: that is a form error, not a lost session.
    if (res.status === 401 && token && !path.startsWith("/api/auth/")) {
      try {
        window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
      } catch {
        /* no window (tests) */
      }
    }
    const retryAfterHeader = Number(res.headers.get("Retry-After"));
    const retryAfter = Number.isFinite(retryAfterHeader) && retryAfterHeader > 0 ? retryAfterHeader : null;
    throw new ApiError(res.status, detailFromBody(data, statusFallback(res.status, retryAfter)), retryAfter);
  }
  return data as T;
}

const get = <T>(path: string, query?: Record<string, QueryValue>, signal?: AbortSignal) =>
  request<T>("GET", path, { query, signal });
const post = <T>(path: string, body?: unknown) => request<T>("POST", path, { body: body ?? {} });
const put = <T>(path: string, body: unknown) => request<T>("PUT", path, { body });
const enc = encodeURIComponent;

/** URL of a file inside a run folder (images, audio, video…); the token travels in the query. */
export function fileUrl(runId: string, path: string): string {
  const { apiBase, token } = getApiConfig();
  return `${apiBase}/api/runs/${enc(runId)}/file${buildQuery({ path, token })}`;
}

/** URL of the costs CSV export (token in the query for plain links). */
export function costsCsvUrl(): string {
  const { apiBase, token } = getApiConfig();
  return `${apiBase}/api/costs.csv${buildQuery({ token })}`;
}

/** Download the costs CSV through an authenticated fetch (works even if the query token is refused). */
export async function downloadCostsCsv(filename = "watermark-lab-couts.csv"): Promise<void> {
  const { apiBase, token } = getApiConfig();
  let res: Response;
  try {
    res = await fetch(`${apiBase}/api/costs.csv`, { headers: { "X-WMLab-Token": token } });
  } catch {
    throw new ApiError(0, "Impossible de joindre le backend Watermark Lab.");
  }
  if (!res.ok) throw new ApiError(res.status, statusFallback(res.status));
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

const userAction = <T>(id: string, action: string, body?: unknown) => post<T>(`/api/users/${enc(id)}/${action}`, body);

/** All §9 routes. */
export const api = {
  // Health & overview
  health: (signal?: AbortSignal) => get<Health>("/api/health", undefined, signal),
  overview: () => get<Overview>("/api/overview"),

  // Projects
  listProjects: () => get<ProjectSummary[]>("/api/projects"),
  createProject: (body: CreateProjectBody) => post<ProjectSummary>("/api/projects", body),
  getProject: (name: string) => get<ProjectDetail>(`/api/projects/${enc(name)}`),
  createExperiment: (project: string, body: CreateExperimentBody) =>
    post<CreatedPath>(`/api/projects/${enc(project)}/experiments`, body),
  createHpoPlan: (project: string, body: CreateHpoPlanBody) =>
    post<CreatedPath>(`/api/projects/${enc(project)}/hpo`, body),

  // Files & validation
  getFile: (path: string) => get<FileContent>("/api/files", { path }),
  saveFile: (path: string, content: string) => put<SaveFileResult>("/api/files", { path, content }),
  validate: (body: ValidateBody) => post<ValidateResult>("/api/validate", body),

  // Registry
  registry: () => get<RegistryResponse>("/api/registry"),

  // Runs
  listRuns: (query: RunsQuery = {}) =>
    get<RunSummary[]>("/api/runs", {
      ...query,
      include_trials: query.include_trials ? "true" : undefined,
    }),
  getRun: (id: string) => get<RunDetail>(`/api/runs/${enc(id)}`),
  runMetrics: (id: string, keys?: string[], maxPoints = 1000) =>
    get<MetricsResponse>(`/api/runs/${enc(id)}/metrics`, {
      keys: keys && keys.length ? keys.join(",") : undefined,
      max_points: maxPoints,
    }),
  runSystem: (id: string, maxPoints = 500) =>
    get<SystemResponse>(`/api/runs/${enc(id)}/system`, { max_points: maxPoints }),
  runLogs: (id: string, tail = 500) => get<LogsResponse>(`/api/runs/${enc(id)}/logs`, { tail }),
  setRunNotes: (id: string, notes: string) => put<OkResponse>(`/api/runs/${enc(id)}/notes`, { notes }),
  setStudyNotes: (id: string, notes: string) => put<OkResponse>(`/api/studies/${enc(id)}/notes`, { notes }),
  setRunTags: (id: string, tags: string[]) => put<OkResponse>(`/api/runs/${enc(id)}/tags`, { tags }),
  stopRun: (id: string) => post<Job | OkResponse>(`/api/runs/${enc(id)}/stop`),
  fetchCheckpoint: (id: string, name: string) =>
    post<OkResponse>(`/api/runs/${enc(id)}/checkpoints/${enc(name)}/fetch`),

  // Comparison
  compare: (runs: string[], keys: string[] = [], maxPoints = 500) =>
    get<CompareResponse>("/api/compare", {
      runs: runs.join(","),
      keys: keys.length ? keys.join(",") : undefined,
      max_points: maxPoints,
    }),

  // Studies
  listStudies: (project?: string) => get<StudySummary[]>("/api/studies", { project }),
  getStudy: (id: string) => get<StudyDetail>(`/api/studies/${enc(id)}`),

  // Leaderboard, benchmarks, datasets
  leaderboard: (query: LeaderboardQuery = {}) => get<Leaderboard>("/api/leaderboard", { ...query }),
  listBenchmarks: () => get<BenchmarkInfo[]>("/api/benchmarks"),
  listDatasets: () => get<DatasetInfo[]>("/api/datasets"),

  // Hosts
  listHosts: () => get<Host[]>("/api/hosts"),
  saveHosts: (hosts: Host[]) => put<Host[]>("/api/hosts", hosts),
  testHost: (name: string) => post<HostTestResult>(`/api/hosts/${enc(name)}/test`),
  setupHost: (name: string) => post<Job>(`/api/hosts/${enc(name)}/setup`),
  // §12.4 — inventory, retrieval, decommission, Scaleway instance
  hostInventory: (name: string, signal?: AbortSignal) => get<HostInventory>(`/api/hosts/${enc(name)}/inventory`, undefined, signal),
  retrieveHost: (name: string) => post<Job>(`/api/hosts/${enc(name)}/retrieve`),
  decommissionHost: (name: string, body: DecommissionBody) => post<DecommissionResult>(`/api/hosts/${enc(name)}/decommission`, body),
  poweroffHost: (name: string) => post<PoweroffResult>(`/api/hosts/${enc(name)}/poweroff`),
  hostInstance: (name: string, signal?: AbortSignal) => get<HostInstance>(`/api/hosts/${enc(name)}/instance`, undefined, signal),

  // Jobs
  listJobs: (status?: string) => get<Job[]>("/api/jobs", { status }),
  createJob: (body: CreateJobBody) => post<Job>("/api/jobs", body),
  estimateJob: (body: EstimateBody) => post<JobEstimate>("/api/jobs/estimate", body),
  cancelJob: (id: string) => post<Job>(`/api/jobs/${enc(id)}/cancel`),
  syncJob: (id: string) => post<Job>(`/api/jobs/${enc(id)}/sync`),

  // Costs & settings
  costs: () => get<Costs>("/api/costs"),
  getSettings: () => get<WorkspaceSettings>("/api/settings"),
  saveSettings: (settings: WorkspaceSettings) => put<WorkspaceSettings>("/api/settings", settings),

  // §12.2 — accounts and sessions. `login` / `register` are public and may target another
  // server than the current session (login screen with a server URL).
  login: (body: LoginBody, apiBase?: string) => request<LoginResult>("POST", "/api/auth/login", { body, apiBase, anonymous: true }),
  register: (body: RegisterBody, apiBase?: string) =>
    request<RegisterResult>("POST", "/api/auth/register", { body, apiBase, anonymous: true }),
  logout: () => post<OkResponse>("/api/auth/logout"),
  changePassword: (body: ChangePasswordBody) => post<OkResponse>("/api/auth/password", body),
  me: (signal?: AbortSignal) => get<Me>("/api/me", undefined, signal),
  listUsers: () => get<UserInfo[]>("/api/users"),
  createUser: (body: CreateUserBody) => post<CreatedUser>("/api/users", body),
  approveUser: (id: string, role: UserRole) => userAction<UserInfo>(id, "approve", { role }),
  setUserRole: (id: string, role: UserRole) => userAction<UserInfo>(id, "role", { role }),
  disableUser: (id: string) => userAction<UserInfo>(id, "disable"),
  enableUser: (id: string) => userAction<UserInfo>(id, "enable"),
  resetUserPassword: (id: string) => userAction<CreatedUser>(id, "reset-password"),

  // §12.3 — git (hosted mode)
  git: (signal?: AbortSignal) => get<GitState>("/api/git", undefined, signal),
  gitPull: () => post<GitState>("/api/git/pull"),
};

export type Api = typeof api;
