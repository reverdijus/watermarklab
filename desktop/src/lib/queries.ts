/**
 * React Query hooks for every §9 read (with polling for live data) and every
 * mutation (with consistent cache invalidation).
 */
import {
  QueryClient,
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type Query,
} from "@tanstack/react-query";
import { api, ApiError } from "./api";
import { useDebouncedValue } from "./hooks";
import { isActiveStatus } from "./runs";
import type {
  CreateExperimentBody,
  CreateHpoPlanBody,
  CreateJobBody,
  CreateProjectBody,
  DecommissionBody,
  EstimateBody,
  GitState,
  Host,
  Job,
  LeaderboardQuery,
  Me,
  Overview,
  ProjectSummary,
  RunDetail,
  RunsQuery,
  RunSummary,
  StudyDetail,
  StudySummary,
  ValidateBody,
  WorkspaceSettings,
} from "./types";

/** Polling periods (ms). */
export const LIVE_INTERVAL = 5000;
export const IDLE_INTERVAL = 30000;

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2000,
      retry: 1,
      refetchOnWindowFocus: true,
      refetchIntervalInBackground: false,
    },
    mutations: { retry: 0 },
  },
});

/** Query keys. Prefixes allow broad invalidation (e.g. ["run", id] covers metrics/logs). */
export const qk = {
  health: ["health"] as const,
  overview: ["overview"] as const,
  projects: ["projects"] as const,
  project: (name: string) => ["projects", name] as const,
  file: (path: string) => ["file", path] as const,
  validate: (body: ValidateBody) => ["validate", body.kind ?? null, body.path ?? null, body.content] as const,
  registry: ["registry"] as const,
  runs: (q: RunsQuery = {}) => ["runs", q] as const,
  run: (id: string) => ["run", id] as const,
  runMetrics: (id: string, keys: string[], maxPoints: number) => ["run", id, "metrics", keys, maxPoints] as const,
  runSystem: (id: string, maxPoints: number) => ["run", id, "system", maxPoints] as const,
  runLogs: (id: string, tail: number) => ["run", id, "logs", tail] as const,
  compare: (runs: string[], keys: string[], maxPoints: number) => ["compare", runs, keys, maxPoints] as const,
  studies: (project?: string) => ["studies", project ?? null] as const,
  study: (id: string) => ["study", id] as const,
  leaderboard: (q: LeaderboardQuery = {}) => ["leaderboard", q] as const,
  benchmarks: ["benchmarks"] as const,
  datasets: ["datasets"] as const,
  hosts: ["hosts"] as const,
  jobs: (status?: string) => ["jobs", status ?? null] as const,
  estimate: (body: EstimateBody) => ["estimate", body] as const,
  costs: ["costs"] as const,
  settings: ["settings"] as const,
  // §12
  me: ["me"] as const,
  users: ["users"] as const,
  git: ["git"] as const,
  hostInventory: (name: string) => ["hosts", "inventory", name] as const,
  hostInstance: (name: string) => ["hosts", "instance", name] as const,
};

const anyActive = (items: { status: string }[] | undefined) => !!items?.some((i) => isActiveStatus(i.status));

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

export function useHealth() {
  return useQuery({ queryKey: qk.health, queryFn: ({ signal }) => api.health(signal), refetchInterval: 15000, retry: 0 });
}

/** The overview describes something live (a running run or an active job). */
const overviewActive = (o: Overview | undefined) => !!o && (o.counts.running > 0 || anyActive(o.running) || anyActive(o.jobs));

/** Workspace overview (sidebar + home): 5 s while something runs, 30 s otherwise. */
export function useOverview() {
  return useQuery({
    queryKey: qk.overview,
    queryFn: api.overview,
    refetchInterval: (q: Query<Overview>) => (overviewActive(q.state.data) ? LIVE_INTERVAL : IDLE_INTERVAL),
  });
}

/** Project list: 10 s while a project has running runs, 30 s otherwise. */
export function useProjects() {
  return useQuery({
    queryKey: qk.projects,
    queryFn: api.listProjects,
    refetchInterval: (q: Query<ProjectSummary[]>) => (q.state.data?.some((p) => p.n_running > 0) ? LIVE_INTERVAL * 2 : IDLE_INTERVAL),
  });
}

export function useProject(name: string | null | undefined) {
  return useQuery({
    queryKey: qk.project(name ?? ""),
    queryFn: () => api.getProject(name!),
    enabled: !!name,
    refetchInterval: 15000,
  });
}

export function useFile(path: string | null | undefined) {
  return useQuery({
    queryKey: qk.file(path ?? ""),
    queryFn: () => api.getFile(path!),
    enabled: !!path,
    staleTime: 10000,
    refetchOnWindowFocus: false,
  });
}

/** Debounced server-side validation of YAML content (returns errors + phase plan). */
export function useValidation(body: ValidateBody | null, debounceMs = 450) {
  const debounced = useDebouncedValue(body, debounceMs);
  return useQuery({
    queryKey: debounced ? qk.validate(debounced) : ["validate", "none"],
    queryFn: () => api.validate(debounced!),
    enabled: !!debounced && debounced.content.trim().length > 0,
    placeholderData: keepPreviousData,
    staleTime: 60000,
    refetchOnWindowFocus: false,
  });
}

/** Phase plan of an experiment file (file content → /api/validate). */
export function useExperimentPlan(path: string | null | undefined) {
  const file = useFile(path);
  const validation = useValidation(
    file.data ? { content: file.data.content, kind: "experiment", path: file.data.path } : null,
    0,
  );
  return {
    plan: validation.data?.plan ?? null,
    errors: validation.data?.errors ?? file.data?.validation.errors ?? [],
    isLoading: file.isLoading || validation.isLoading,
    error: file.error ?? validation.error,
  };
}

export function useRegistry() {
  return useQuery({ queryKey: qk.registry, queryFn: api.registry, staleTime: 60000 });
}

export function useRuns(query: RunsQuery = {}, opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: qk.runs(query),
    queryFn: () => api.listRuns(query),
    enabled: opts.enabled ?? true,
    placeholderData: keepPreviousData,
    refetchInterval: (q: Query<RunSummary[]>) => (anyActive(q.state.data) ? LIVE_INTERVAL : IDLE_INTERVAL),
  });
}

export function useRun(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.run(id ?? ""),
    queryFn: () => api.getRun(id!),
    enabled: !!id,
    refetchInterval: (q: Query<RunDetail>) => (isActiveStatus(q.state.data?.status) ? LIVE_INTERVAL : false),
  });
}

export function useRunMetrics(
  id: string | null | undefined,
  keys: string[] = [],
  opts: { maxPoints?: number; live?: boolean; enabled?: boolean } = {},
) {
  const maxPoints = opts.maxPoints ?? 1000;
  return useQuery({
    queryKey: qk.runMetrics(id ?? "", keys, maxPoints),
    queryFn: () => api.runMetrics(id!, keys, maxPoints),
    enabled: !!id && (opts.enabled ?? true),
    placeholderData: keepPreviousData,
    refetchInterval: opts.live ? LIVE_INTERVAL : false,
  });
}

export function useRunSystem(id: string | null | undefined, opts: { maxPoints?: number; live?: boolean } = {}) {
  const maxPoints = opts.maxPoints ?? 500;
  return useQuery({
    queryKey: qk.runSystem(id ?? "", maxPoints),
    queryFn: () => api.runSystem(id!, maxPoints),
    enabled: !!id,
    placeholderData: keepPreviousData,
    refetchInterval: opts.live ? LIVE_INTERVAL * 2 : false,
  });
}

export function useRunLogs(id: string | null | undefined, opts: { tail?: number; live?: boolean } = {}) {
  const tail = opts.tail ?? 500;
  return useQuery({
    queryKey: qk.runLogs(id ?? "", tail),
    queryFn: () => api.runLogs(id!, tail),
    enabled: !!id,
    placeholderData: keepPreviousData,
    refetchInterval: opts.live ? 3000 : false,
  });
}

export function useCompare(runs: string[], keys: string[] = [], opts: { maxPoints?: number; live?: boolean } = {}) {
  const maxPoints = opts.maxPoints ?? 500;
  return useQuery({
    queryKey: qk.compare(runs, keys, maxPoints),
    queryFn: () => api.compare(runs, keys, maxPoints),
    enabled: runs.length > 0,
    placeholderData: keepPreviousData,
    refetchInterval: opts.live ? LIVE_INTERVAL * 2 : false,
  });
}

export function useStudies(project?: string) {
  return useQuery({
    queryKey: qk.studies(project),
    queryFn: () => api.listStudies(project),
    refetchInterval: (q: Query<StudySummary[]>) => (anyActive(q.state.data) ? LIVE_INTERVAL : IDLE_INTERVAL),
  });
}

export function useStudy(id: string | null | undefined) {
  return useQuery({
    queryKey: qk.study(id ?? ""),
    queryFn: () => api.getStudy(id!),
    enabled: !!id,
    refetchInterval: (q: Query<StudyDetail>) => (isActiveStatus(q.state.data?.status) ? LIVE_INTERVAL : false),
  });
}

export function useLeaderboard(query: LeaderboardQuery = {}) {
  return useQuery({
    queryKey: qk.leaderboard(query),
    queryFn: () => api.leaderboard(query),
    placeholderData: keepPreviousData,
    refetchInterval: IDLE_INTERVAL,
  });
}

export function useBenchmarks() {
  return useQuery({ queryKey: qk.benchmarks, queryFn: api.listBenchmarks, staleTime: 30000 });
}

export function useDatasets() {
  return useQuery({ queryKey: qk.datasets, queryFn: api.listDatasets, staleTime: 30000 });
}

export function useHosts() {
  return useQuery({ queryKey: qk.hosts, queryFn: api.listHosts, staleTime: 30000 });
}

export function useJobs(status?: string) {
  return useQuery({
    queryKey: qk.jobs(status),
    queryFn: () => api.listJobs(status),
    refetchInterval: (q: Query<Job[]>) => (anyActive(q.state.data) ? LIVE_INTERVAL : LIVE_INTERVAL * 3),
  });
}

/** Cost / duration estimate of a launch (null body ⇒ disabled). */
export function useJobEstimate(body: EstimateBody | null) {
  const debounced = useDebouncedValue(body, 400);
  return useQuery({
    queryKey: debounced ? qk.estimate(debounced) : ["estimate", "none"],
    queryFn: () => api.estimateJob(debounced!),
    enabled: !!debounced?.spec_path && !!debounced.host,
    staleTime: 30000,
    retry: 0,
    refetchOnWindowFocus: false,
  });
}

export function useCosts() {
  return useQuery({ queryKey: qk.costs, queryFn: api.costs, refetchInterval: IDLE_INTERVAL });
}

export function useSettings() {
  return useQuery({ queryKey: qk.settings, queryFn: api.getSettings, staleTime: 30000 });
}

// §12 — hosted mode -----------------------------------------------------------

/** Statut HTTP d'une erreur de requête (0 hors ApiError). */
export const errorStatus = (err: unknown): number => (err instanceof ApiError ? err.status : 0);

/**
 * Current user (`GET /api/me`). Never retried: a 401 means the token is refused and the
 * LoginGate takes over; any other error (older backend without the route, network) leaves
 * the app usable with an unknown user.
 */
export function useMe(opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: qk.me,
    queryFn: ({ signal }) => api.me(signal),
    enabled: opts.enabled ?? true,
    retry: 0,
    staleTime: 30000,
    refetchInterval: 60000,
    refetchOnWindowFocus: true,
  });
}

/** Convenience: `{name, role}` of the current user, or null while unknown. */
export function useCurrentUser(): Me | null {
  const me = useMe();
  return me.data ?? null;
}

export function useUsers(enabled = true) {
  return useQuery({ queryKey: qk.users, queryFn: api.listUsers, enabled, staleTime: 30000, retry: 0 });
}

/** Git state of the server checkout (`GET /api/git`); polled slowly (auto pull runs server-side). */
export function useGit(opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: qk.git,
    queryFn: ({ signal }) => api.git(signal),
    enabled: opts.enabled ?? true,
    retry: 0,
    staleTime: 10000,
    refetchInterval: IDLE_INTERVAL,
  });
}

export function useGitPull() {
  const qc = useQueryClient();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: () => api.gitPull(),
    onSuccess: (git: GitState) => {
      qc.setQueryData(qk.git, git);
      return invalidate(qk.projects, qk.benchmarks, qk.datasets, qk.registry, ["file"]);
    },
  });
}

/** Host inventory (§12.4), loaded on demand (`enabled`) and after each retrieve job. */
export function useHostInventory(name: string | null | undefined, opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: qk.hostInventory(name ?? ""),
    queryFn: ({ signal }) => api.hostInventory(name!, signal),
    enabled: !!name && (opts.enabled ?? true),
    retry: 0,
    staleTime: 15000,
    refetchOnWindowFocus: false,
  });
}

/** Scaleway instance state (§12.5), loaded with the inventory panel. */
export function useHostInstance(name: string | null | undefined, opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: qk.hostInstance(name ?? ""),
    queryFn: ({ signal }) => api.hostInstance(name!, signal),
    enabled: !!name && (opts.enabled ?? true),
    retry: 0,
    staleTime: 15000,
    refetchOnWindowFocus: false,
  });
}

export function useRetrieveHost() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (name: string) => api.retrieveHost(name),
    onSuccess: () => invalidate(["jobs"], qk.overview),
  });
}

export function useDecommissionHost() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: DecommissionBody }) => api.decommissionHost(name, body),
    onSuccess: (_res, { name }) => invalidate(qk.hosts, qk.hostInventory(name), qk.hostInstance(name), ["jobs"], qk.overview),
  });
}

export function usePoweroffHost() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (name: string) => api.poweroffHost(name),
    onSuccess: (_res, name) => invalidate(qk.hostInstance(name)),
  });
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

function useInvalidate() {
  const qc = useQueryClient();
  return (...keys: readonly (readonly unknown[])[]) =>
    Promise.all(keys.map((queryKey) => qc.invalidateQueries({ queryKey })));
}

export function useCreateProject() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: CreateProjectBody) => api.createProject(body),
    onSuccess: () => invalidate(qk.projects, qk.overview),
  });
}

export function useCreateExperiment(project: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: CreateExperimentBody) => api.createExperiment(project, body),
    onSuccess: () => invalidate(qk.projects),
  });
}

export function useCreateHpoPlan(project: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: CreateHpoPlanBody) => api.createHpoPlan(project, body),
    onSuccess: () => invalidate(qk.projects),
  });
}

export function useSaveFile() {
  const qc = useQueryClient();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: ({ path, content }: { path: string; content: string }) => api.saveFile(path, content),
    onSuccess: (res, { path, content }) => {
      qc.setQueryData(qk.file(path), (old: unknown) =>
        old && typeof old === "object" ? { ...(old as object), content, validation: res.validation } : old,
      );
      return invalidate(qk.projects, qk.benchmarks, qk.datasets, qk.registry, qk.overview);
    },
  });
}

export function useValidate() {
  return useMutation({ mutationFn: (body: ValidateBody) => api.validate(body) });
}

export function useSetRunNotes(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (notes: string) => api.setRunNotes(runId, notes),
    onSuccess: (_res, notes) => {
      qc.setQueryData(qk.run(runId), (old: RunDetail | undefined) => (old ? { ...old, notes } : old));
    },
  });
}

export function useSetStudyNotes(studyId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (notes: string) => api.setStudyNotes(studyId, notes),
    onSuccess: (_res, notes) => {
      qc.setQueryData(qk.study(studyId), (old: StudyDetail | undefined) => (old ? { ...old, notes } : old));
    },
  });
}

export function useSetRunTags(runId: string) {
  const qc = useQueryClient();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (tags: string[]) => api.setRunTags(runId, tags),
    onSuccess: (_res, tags) => {
      qc.setQueryData(qk.run(runId), (old: RunDetail | undefined) => (old ? { ...old, tags } : old));
      return invalidate(["runs"], qk.overview);
    },
  });
}

export function useStopRun() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (runId: string) => api.stopRun(runId),
    onSuccess: (_res, runId) => invalidate(qk.run(runId), ["runs"], ["jobs"], qk.overview, qk.projects),
  });
}

export function useFetchCheckpoint(runId: string) {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (name: string) => api.fetchCheckpoint(runId, name),
    onSuccess: () => invalidate(qk.run(runId)),
  });
}

export function useSaveHosts() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (hosts: Host[]) => api.saveHosts(hosts),
    onSuccess: (hosts) => {
      qc.setQueryData(qk.hosts, hosts);
      return qc.invalidateQueries({ queryKey: qk.hosts });
    },
  });
}

export function useTestHost() {
  return useMutation({ mutationFn: (name: string) => api.testHost(name) });
}

export function useSetupHost() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (name: string) => api.setupHost(name),
    onSuccess: () => invalidate(["jobs"], qk.overview),
  });
}

export function useCreateJob() {
  const qc = useQueryClient();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (body: CreateJobBody) => api.createJob(body),
    onSuccess: (job) => {
      qc.setQueryData(qk.overview, (old: Overview | undefined) =>
        old ? { ...old, jobs: [job, ...old.jobs.filter((j) => j.job_id !== job.job_id)] } : old,
      );
      return invalidate(["jobs"], ["runs"], ["studies"], qk.overview, qk.projects, qk.costs);
    },
  });
}

export function useEstimateJob() {
  return useMutation({ mutationFn: (body: EstimateBody) => api.estimateJob(body) });
}

export function useCancelJob() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (jobId: string) => api.cancelJob(jobId),
    onSuccess: (job) =>
      invalidate(["jobs"], ["runs"], ["studies"], qk.overview, ...(job.run_id ? [qk.run(job.run_id)] : [])),
  });
}

export function useSyncJob() {
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (jobId: string) => api.syncJob(jobId),
    onSuccess: (job) =>
      invalidate(["jobs"], ["runs"], ["studies"], qk.overview, ...(job.run_id ? [qk.run(job.run_id)] : [])),
  });
}

export function useSaveSettings() {
  const qc = useQueryClient();
  const invalidate = useInvalidate();
  return useMutation({
    mutationFn: (settings: WorkspaceSettings) => api.saveSettings(settings),
    onSuccess: (settings) => {
      qc.setQueryData(qk.settings, settings);
      return invalidate(qk.overview, qk.costs);
    },
  });
}
