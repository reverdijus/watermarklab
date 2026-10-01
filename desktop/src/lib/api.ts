/**
 * Typed HTTP client for every route of ARCHITECTURE.md §9 and §12.
 *
 * Connection settings are detected once:
 *  1. Electron: `window.wmlab` exposed by the preload script (local backend started by the
 *     main process, or a remote server chosen through the "Serveur" menu — `mode`);
 *  2. Vite dev server in a browser: `VITE_API_BASE` / `VITE_API_TOKEN`;
 *  3. Browser served by FastAPI: `window.location.origin` (or a server URL chosen on the
 *     login screen) + `?token=` (then localStorage `wmlab.token`).
 */
import type {
  BenchmarkInfo,
  CompareResponse,
  CreatedPath,
  CreateExperimentBody,
  CreateHpoPlanBody,
  CreateJobBody,
  CreateProjectBody,
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
  LogsResponse,
  Me,
  MetricsResponse,
  OkResponse,
  Overview,
  PoweroffResult,
  ProjectDetail,
  ProjectSummary,
  RegistryResponse,
  RunDetail,
  RunsQuery,
  RunSummary,
  SaveFileResult,
  StudyDetail,
  StudySummary,
  SystemResponse,
  UserInfo,
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

function readTokenFromLocation(): string | null {
  const fromSearch = new URLSearchParams(window.location.search).get("token");
  if (fromSearch) return fromSearch;
  const hash = window.location.hash;
  const q = hash.indexOf("?");
  if (q >= 0) return new URLSearchParams(hash.slice(q + 1)).get("token");
  return null;
}

/** Remove `?token=` from the visible URL once it has been memorised. */
function scrubTokenFromLocation(): void {
  try {
    const url = new URL(window.location.href);
    if (!url.searchParams.has("token")) return;
    url.searchParams.delete("token");
    window.history.replaceState(window.history.state, "", url.toString());
  } catch {
    /* ignore */
  }
}

function storageGet(storage: "local" | "session", key: string): string | null {
  try {
    return (storage === "local" ? window.localStorage : window.sessionStorage).getItem(key);
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
  // Browser: `?token=` wins and is memorised (localStorage, then the legacy sessionStorage
  // entry of previous versions).
  let token = readTokenFromLocation();
  if (token) storageSet(TOKEN_STORAGE_KEY, token);
  else token = storageGet("local", TOKEN_STORAGE_KEY) ?? storageGet("session", TOKEN_STORAGE_KEY);
  scrubTokenFromLocation();
  const storedBase = storageGet("local", API_BASE_STORAGE_KEY);
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
 * Browser session (§12.6): memorise the token (and optionally the server URL when the page
 * is not served by the backend) after a successful login. In Electron the session is owned
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

/** Forget the token ("Se déconnecter"). The server URL is kept. */
export function clearSession(): void {
  const current = getApiConfig();
  cachedConfig = { ...current, token: "" };
  if (current.source === "browser") {
    storageSet(TOKEN_STORAGE_KEY, null);
    try {
      window.sessionStorage.removeItem(TOKEN_STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }
}

/** Event dispatched on `window` whenever a request is refused with 401 (LoginGate listens). */
export const UNAUTHORIZED_EVENT = "wmlab:unauthorized";

/** Result of `probeConnection`. */
export interface ProbeResult {
  /** The server answered `/api/health` (public route). */
  reachable: boolean;
  version: string | null;
  /** `GET /api/me` with the token: the user, or null when the token was refused. */
  me: Me | null;
  /** Diagnostic (French) when reachable is false or the token was refused. */
  error: string | null;
}

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
 * Login screen helper: checks that `apiBase` is a Watermark Lab backend (`/api/health`),
 * then that `token` is accepted (`/api/me`). Never throws. Note: an older backend without
 * `/api/me` answers 404 — the token is then considered accepted when `/api/health` returned
 * `workspace` (only sent to an authenticated request).
 */
export async function probeConnection(apiBase: string, token: string, timeoutMs = 8000): Promise<ProbeResult> {
  const base = apiBase.replace(/\/+$/, "");
  let health: { status: number; body: unknown };
  try {
    health = await fetchJson(`${base}/api/health`, token || null, timeoutMs);
  } catch {
    return { reachable: false, version: null, me: null, error: `Serveur injoignable : ${base}` };
  }
  const hb = health.body as { ok?: unknown; version?: unknown; workspace?: unknown } | null;
  if (health.status !== 200 || !hb || typeof hb !== "object" || hb.ok !== true) {
    return { reachable: false, version: null, me: null, error: `${base} ne répond pas comme un backend Watermark Lab (HTTP ${health.status}).` };
  }
  const version = typeof hb.version === "string" ? hb.version : null;
  if (!token) return { reachable: true, version, me: null, error: null };
  let me: { status: number; body: unknown };
  try {
    me = await fetchJson(`${base}/api/me`, token, timeoutMs);
  } catch {
    return { reachable: true, version, me: null, error: "Le serveur n'a pas répondu à la vérification du jeton." };
  }
  if (me.status === 200 && me.body && typeof me.body === "object" && typeof (me.body as Me).name === "string") {
    return { reachable: true, version, me: me.body as Me, error: null };
  }
  if (me.status === 404 && typeof hb.workspace === "string") {
    return { reachable: true, version, me: { name: "admin", role: "admin", hosted: false, public_url: null }, error: null };
  }
  if (me.status === 401 || me.status === 403 || me.status === 404) {
    return { reachable: true, version, me: null, error: "Jeton refusé par le serveur. Vérifiez-le ou demandez-en un nouveau à un administrateur." };
  }
  return { reachable: true, version, me: null, error: detailFromBody(me.body, statusFallback(me.status)) };
}

/** Error raised for any failed request; `detail` is the backend message (French when possible). */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
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

function statusFallback(status: number): string {
  if (status === 401) return "Jeton refusé : connectez-vous avec un jeton valide.";
  if (status === 403) return "Action réservée aux administrateurs.";
  if (status === 404) return "Ressource introuvable.";
  if (status === 409) return "Conflit : l'opération n'est pas possible dans l'état actuel.";
  if (status === 422) return "Requête invalide.";
  if (status >= 500) return `Erreur interne du backend (${status}).`;
  return `Erreur HTTP ${status}.`;
}

interface RequestOptions {
  query?: Record<string, QueryValue>;
  body?: unknown;
  signal?: AbortSignal;
}

/** Low-level request helper (JSON in, JSON out). */
export async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const { apiBase, token } = getApiConfig();
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
    if (res.status === 401) {
      try {
        window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
      } catch {
        /* no window (tests) */
      }
    }
    throw new ApiError(res.status, detailFromBody(data, statusFallback(res.status)));
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

  // §12 — users & git (hosted mode)
  me: (signal?: AbortSignal) => get<Me>("/api/me", undefined, signal),
  listUsers: () => get<UserInfo[]>("/api/users"),
  git: (signal?: AbortSignal) => get<GitState>("/api/git", undefined, signal),
  gitPull: () => post<GitState>("/api/git/pull"),
};

export type Api = typeof api;
