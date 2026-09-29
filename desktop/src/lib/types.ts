/**
 * API types — faithful copy of ARCHITECTURE.md §9, followed by the request /
 * response shapes of the routes table (section "Route payloads").
 */

// ---------------------------------------------------------------------------
// §9 — verbatim
// ---------------------------------------------------------------------------

export type Modality = "image" | "audio" | "video";
export type RunStatus = "queued" | "running" | "completed" | "failed" | "killed" | "lost";
export type JobStatus = "queued" | "launching" | "running" | "completed" | "failed" | "killed" | "lost";
export type Direction = "maximize" | "minimize";
export type Series = { steps: number[]; values: number[] };

export interface Objective {
  metric: string;
  direction: Direction;
  label?: string | null;
  constraint?: string | null;
}

export interface ProjectSummary {
  name: string;
  title: string;
  modality: Modality;
  description: string;
  message_bits: number;
  default_benchmark: string | null;
  tags: string[];
  objectives: { quality: Objective; robustness: Objective };
  n_experiments: number;
  n_hpo_plans: number;
  n_runs: number;
  n_running: number;
  last_activity: string | null;
  cost_eur: number;
  best: {
    run_id: string;
    checkpoint: string | null;
    quality: number | null;
    robustness: number | null;
    source: "benchmark" | "val";
  } | null;
}

export interface ValidationError {
  loc: string;
  msg: string;
}

export interface ExperimentInfo {
  name: string;
  path: string;
  description: string;
  hypothesis: string;
  model: string;
  n_phases: number;
  total_steps: number;
  valid: boolean;
  errors: ValidationError[];
  n_runs: number;
}

export interface HpoPlanInfo {
  name: string;
  path: string;
  experiment: string;
  description: string;
  n_params: number;
  objectives: Objective[];
  valid: boolean;
  errors: ValidationError[];
  n_studies: number;
}

export interface PhasePreview {
  name: string;
  start: number;
  end: number;
  steps: number;
  lr: [number, number][]; // [global step, factor]
  attack_strength: [number, number][];
  loss_weights: Record<string, [number, number][]>;
  attacks: string[]; // labels / names
}

export interface PhasePlanPreview {
  total_steps: number;
  phases: PhasePreview[];
}

export interface Progress {
  step: number;
  total_steps: number;
  phase: string;
  phase_index: number;
  n_phases: number;
}

export interface RunSummary {
  run_id: string;
  project: string;
  experiment: string;
  modality: Modality;
  status: RunStatus;
  host: string;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  duration_s: number | null;
  cost_eur: number;
  study: string | null;
  trial: number | null;
  parent: { run_id: string; checkpoint: string; start_phase?: string | number | null } | null;
  tags: string[];
  progress: Progress | null;
  summary: Record<string, number>;
  params: Record<string, string | number | boolean | null>;
  error: string | null;
}

export interface Checkpoint {
  name: string;
  step: number;
  phase: string;
  metrics: Record<string, number>;
  tags: string[];
  created_at: string;
  size_bytes: number;
  available_locally: boolean;
}

export interface EvalResult {
  run_id: string;
  checkpoint: string;
  benchmark: string;
  benchmark_version: number;
  created_at: string;
  duration_s: number | null;
  nbits: number;
  n_items: number;
  quality: Record<string, number>;
  robustness: Record<string, Record<string, number>>; // attack → {bit_acc, "tpr@…"}
  summary: Record<string, number>;
}

export interface Sample {
  step: number;
  kind: "image" | "audio" | "video";
  name: string;
  path: string;
  group: string;
}

export interface RunEvent {
  t: number;
  type: string;
  [k: string]: unknown;
}

export interface RunDetail extends RunSummary {
  spec: Record<string, unknown>;
  spec_yaml: string;
  spec_path: string | null;
  env: {
    git: Record<string, unknown> | null;
    git_diff: string | null;
    hardware: Record<string, unknown> | null;
    pip_freeze_lines: number;
    command: string | null;
    launch: Record<string, unknown> | null;
  };
  plan: PhasePlanPreview | null;
  events: RunEvent[];
  checkpoints: Checkpoint[];
  evals: EvalResult[];
  notes: string;
  samples: Sample[];
  metric_keys: string[];
  price_eur_per_hour: number | null;
  datasets: Record<string, string>;
  overrides: Record<string, string>;
}

export interface Trial {
  number: number;
  state: "COMPLETE" | "PRUNED" | "FAIL" | "RUNNING" | "WAITING";
  params: Record<string, unknown>;
  values: number[] | null;
  intermediate: [number, number][];
  run_id: string | null;
  duration_s: number | null;
}

export interface StudySummary {
  study_id: string;
  project: string;
  plan: string;
  experiment: string;
  status: RunStatus;
  host: string;
  created_at: string;
  ended_at: string | null;
  n_trials: number;
  n_complete: number;
  n_pruned: number;
  n_failed: number;
  n_running: number;
  objectives: Objective[];
  best: { trial: number; values: number[]; params: Record<string, unknown>; run_id: string | null }[];
  cost_eur: number;
  budget: { max_trials: number | null; max_hours: number | null; max_eur: number | null; parallel: number };
}

export interface StudyDetail extends StudySummary {
  spec: Record<string, unknown>;
  trials: Trial[];
  importances: Record<string, number> | null;
  pareto: number[]; // trial numbers on the front
}

export interface LeaderRow {
  run_id: string;
  project: string;
  experiment: string;
  checkpoint: string | null;
  benchmark: string | null;
  source: "benchmark" | "val";
  created_at: string;
  quality: Record<string, number>;
  summary: Record<string, number>;
  robustness: Record<string, Record<string, number>>;
}

export interface Leaderboard {
  benchmarks: string[];
  quality_metrics: string[];
  robustness_metrics: string[];
  attacks: string[];
  rows: LeaderRow[];
}

export interface RegistryEntry {
  name: string;
  kind: string;
  modality: string[] | null;
  description: string;
  params_schema: Record<string, unknown> | null;
  source: string;
  builtin: boolean;
}

export interface BenchmarkInfo {
  name: string;
  path: string;
  modality: Modality;
  version: number;
  description: string;
  attacks: { label: string; name: string; params: Record<string, unknown> }[];
  quality_metrics: string[];
  fpr: number[];
  n_items: number | null;
  valid: boolean;
  errors: ValidationError[];
}

export interface DatasetInfo {
  name: string;
  path: string;
  modality: Modality;
  description: string;
  license: string;
  source: string;
  paths: Record<string, string>;
  used_by: string[]; // experiment paths using it
  valid: boolean;
  errors: ValidationError[];
}

export interface Host {
  name: string;
  kind: "local" | "ssh";
  ssh_target?: string | null;
  workdir?: string | null;
  data_dir?: string | null;
  python?: string | null;
  price_eur_per_hour: number;
  gpu?: string | null;
  max_concurrent_jobs: number;
  notes?: string | null;
}

export interface Job {
  job_id: string;
  kind: "train" | "hpo" | "eval" | "setup";
  host: string;
  target: string;
  overrides: Record<string, string>;
  status: JobStatus;
  run_id: string | null;
  study_id: string | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  max_hours: number | null;
  smoke: boolean;
  error: string | null;
  pid: number | null;
  last_sync_at: string | null;
}

export interface CostItem {
  kind: "run" | "study" | "eval" | "manual";
  id: string;
  project: string | null;
  host: string | null;
  started_at: string | null;
  hours: number;
  eur: number;
  label?: string;
}

export interface Costs {
  total_eur: number;
  spent_eur: number;
  remaining_eur: number;
  running_eur_per_hour: number;
  by_project: { project: string; eur: number; gpu_hours: number }[];
  by_host: { host: string; eur: number; gpu_hours: number }[];
  by_month: { month: string; eur: number; gpu_hours: number }[];
  items: CostItem[];
  budget: { total_eur: number; start: string | null; end: string | null };
}

export interface WorkspaceSettings {
  name: string;
  budget: {
    total_eur: number;
    start: string | null;
    end: string | null;
    manual_entries: { label: string; eur: number; date: string; project: string | null }[];
  };
  data_dir: string;
  sync_interval_s: number;
  default_host: string;
}

export interface Overview {
  workspace: { name: string; path: string };
  budget: { total_eur: number; spent_eur: number; remaining_eur: number; running_eur_per_hour: number };
  counts: { projects: number; runs: number; running: number; studies: number; gpu_hours: number };
  running: RunSummary[];
  recent: RunSummary[];
  projects: ProjectSummary[];
  jobs: Job[];
}

// ---------------------------------------------------------------------------
// Route payloads (bodies and responses listed in the §9 routes table)
// ---------------------------------------------------------------------------

export type JobKind = Job["kind"];
export type SpecKind = "project" | "experiment" | "hpo" | "benchmark" | "dataset" | "workspace" | "hosts";

export interface Health {
  ok: true;
  version: string;
  workspace: string;
}

export interface CreateProjectBody {
  name: string;
  title: string;
  modality: Modality;
  message_bits: number;
  description: string;
}

export interface ProjectDetail {
  summary: ProjectSummary;
  spec: Record<string, unknown>;
  experiments: ExperimentInfo[];
  hpo_plans: HpoPlanInfo[];
  notes: string;
}

export interface CreateExperimentBody {
  name: string;
  from?: string;
}

export interface CreateHpoPlanBody {
  name: string;
  experiment: string;
}

export interface CreatedPath {
  path: string;
}

export interface FileValidation {
  valid: boolean;
  errors: ValidationError[];
}

export interface FileContent {
  path: string;
  content: string;
  kind: string;
  validation: FileValidation;
}

export interface SaveFileResult {
  saved: true;
  validation: FileValidation;
}

export interface ValidateBody {
  content: string;
  kind?: string;
  path?: string;
}

export interface ValidateResult {
  valid: boolean;
  errors: ValidationError[];
  plan: PhasePlanPreview | null;
}

export interface RegistryResponse {
  entries: Record<string, RegistryEntry[]>;
  errors: { module: string; error: string }[];
}

export interface RunsQuery {
  project?: string;
  experiment?: string;
  status?: string;
  study?: string;
  host?: string;
  include_trials?: boolean;
  limit?: number;
}

export interface PhaseBound {
  name: string;
  start: number;
  end: number;
}

export interface MetricsResponse {
  series: Record<string, Series>;
  phases: PhaseBound[];
}

export interface SystemResponse {
  t: number[];
  series: Record<string, number[]>;
}

export interface LogsResponse {
  lines: string[];
}

export interface OkResponse {
  ok: true;
}

export interface CompareResponse {
  runs: RunSummary[];
  series: Record<string, Record<string, Series>>;
}

export interface LeaderboardQuery {
  project?: string;
  benchmark?: string;
  source?: "benchmark" | "val";
}

export interface HostTestResult {
  ok: boolean;
  latency_ms: number | null;
  output: string | null;
  error: string | null;
}

export interface InitFrom {
  run: string;
  checkpoint: string;
  start_phase?: string | number | null;
}

export interface CreateJobBody {
  kind: JobKind;
  host: string;
  spec_path?: string;
  run_id?: string;
  checkpoint?: string;
  benchmark?: string;
  overrides?: Record<string, string>;
  max_hours?: number | null;
  smoke?: boolean;
  init_from?: InitFrom;
}

export interface EstimateBody {
  spec_path: string;
  host: string;
  overrides?: Record<string, string>;
}

export interface JobEstimate {
  total_steps: number;
  est_hours: number | null;
  est_eur: number | null;
  basis: string;
}
