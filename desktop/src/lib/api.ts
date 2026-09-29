/**
 * Typed HTTP client for every route of ARCHITECTURE.md §9.
 *
 * Connection settings are detected once:
 *  1. Electron: `window.wmlab` exposed by the preload script;
 *  2. Vite dev server in a browser: `VITE_API_BASE` / `VITE_API_TOKEN`;
 *  3. Browser served by FastAPI: `window.location.origin` + `?token=` (then sessionStorage).
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
  EstimateBody,
  FileContent,
  Health,
  Host,
  HostTestResult,
  Job,
  JobEstimate,
  Leaderboard,
  LeaderboardQuery,
  LogsResponse,
  MetricsResponse,
  OkResponse,
  Overview,
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
  ValidateBody,
  ValidateResult,
  WorkspaceSettings,
} from "./types";

export interface ApiConfig {
  apiBase: string;
  token: string;
  platform: string;
  /** Where the settings came from (useful in diagnostics). */
  source: "electron" | "vite-env" | "browser";
}

const TOKEN_STORAGE_KEY = "wmlab.token";

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

function detectConfig(): ApiConfig {
  const bridge = typeof window !== "undefined" ? window.wmlab : undefined;
  if (bridge?.apiBase) {
    return {
      apiBase: bridge.apiBase.replace(/\/+$/, ""),
      token: bridge.token ?? "",
      platform: bridge.platform ?? "darwin",
      source: "electron",
    };
  }
  const envBase = import.meta.env.VITE_API_BASE;
  if (envBase) {
    return {
      apiBase: envBase.replace(/\/+$/, ""),
      token: import.meta.env.VITE_API_TOKEN ?? "",
      platform: navigator.platform.toLowerCase().includes("mac") ? "darwin" : "web",
      source: "vite-env",
    };
  }
  let token = readTokenFromLocation();
  try {
    if (token) window.sessionStorage.setItem(TOKEN_STORAGE_KEY, token);
    else token = window.sessionStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    /* sessionStorage unavailable: keep the URL token only */
  }
  scrubTokenFromLocation();
  return {
    apiBase: window.location.origin,
    token: token ?? "",
    platform: "web",
    source: "browser",
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
  if (status === 401 || status === 403) return "Accès refusé : jeton de session invalide.";
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
};

export type Api = typeof api;
