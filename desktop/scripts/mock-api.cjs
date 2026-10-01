#!/usr/bin/env node
/**
 * Mock backend for visual checks of the desktop build without Python (§9 + §12 routes).
 *
 *   node scripts/mock-api.cjs [--port 8781] [--token test]
 *   npm run render-check -- --base http://127.0.0.1:8781 --token test --login --member --users
 *
 * Serves `dist/` (index.html without cache, hashed assets immutable) and answers every
 * route the pages call with plausible in-memory data: three projects, a handful of runs, one
 * HPO study, jobs (including a train job whose checkpoint retrieval is partial and a finished
 * `retrieve` job), hosts (local, a Scaleway L40S with checkpoints not yet retrieved, a clean
 * H100 and a decommissioned machine), git state, accounts. Mutations are simulated in memory
 * (retrieve job that completes after ~5 s, decommission refused while checkpoints are
 * missing, power-off, `PUT /api/files` answering a git commit + push) and never touch the
 * disk.
 *
 * Authentication (§9, §12.2): `X-WMLab-Token` = server token (`--token`, default `test`:
 * implicit admin "admin", `auth: "server_token"`) or a session token `wmls_…` issued by
 * `POST /api/auth/login` (e-mail + password). Demo accounts, all with the password
 * `motdepasse-demo-123`: justin@example.com (admin), marie@example.com (member),
 * paul@example.com (pending registration), marc@example.com (disabled) and nina@example.com
 * (member, temporary password to change). The session token `member` is pre-issued for
 * marie (member checks without logging in). A member is read-only (403 on every non-GET
 * except logout / password and on the admin GET routes) and gets redacted responses (run
 * spec / env / overrides / params / plan, errors cut to their last line, job overrides,
 * host and dataset paths).
 * `?token=` is only accepted on `/api/runs/{id}/file` and `/api/costs.csv`.
 * `POST /api/mock/reset` restores the initial state (used by render-check).
 */
"use strict";

const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");

const args = process.argv.slice(2);
const opt = (name, def) => {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : def;
};
const PORT = Number(process.env.WMLAB_MOCK_PORT || opt("--port", "8781"));
/** Server token (`--token` of the real backend): implicit admin. */
const SERVER_TOKEN = process.env.WMLAB_MOCK_TOKEN || opt("--token", "test");
/** Pre-issued session of the member "marie". */
const MEMBER_TOKEN = "member";
const DEMO_PASSWORD = "motdepasse-demo-123";
const DIST = path.resolve(__dirname, "..", "dist");
const WORKSPACE = path.resolve(__dirname, "..", "..");
const VERSION = "0.5.0-mock";

// ---------------------------------------------------------------------------
// Data
// ---------------------------------------------------------------------------

const now = Date.now();
const iso = (msAgo) => new Date(now - msAgo).toISOString();
const H = 3600_000;
const MIN = 60_000;

const OBJECTIVES = {
  quality: { metric: "psnr", direction: "maximize", label: "PSNR (dB)", constraint: ">= 38" },
  robustness: { metric: "bit_acc_mean", direction: "maximize", label: "Bit acc. moyenne" },
};

const projects = [
  {
    name: "image-hidden",
    title: "Image — HiDDeN baseline",
    modality: "image",
    description: "Encodeur/décodeur convolutifs à la HiDDeN : valide le pipeline de bout en bout, puis sert de référence.",
    message_bits: 48,
    default_benchmark: "image_v1",
    tags: ["image", "baseline", "hidden"],
    objectives: OBJECTIVES,
    n_experiments: 3,
    n_hpo_plans: 2,
    n_runs: 5,
    n_running: 1,
    last_activity: iso(4 * MIN),
    cost_eur: 1287.4,
    best: { run_id: "20260929-141203-wsd_branch-7c1e", checkpoint: "step_040000", quality: 39.1, robustness: 0.931, source: "benchmark" },
  },
  {
    name: "audio-seal-lite",
    title: "Audio — AudioSeal-lite",
    modality: "audio",
    description: "Marquage audio léger (16 kHz), robuste au MP3 et au rééchantillonnage.",
    message_bits: 16,
    default_benchmark: "audio_v1",
    tags: ["audio"],
    objectives: { quality: { metric: "snr", direction: "maximize", label: "SNR (dB)" }, robustness: OBJECTIVES.robustness },
    n_experiments: 1,
    n_hpo_plans: 1,
    n_runs: 1,
    n_running: 0,
    last_activity: iso(2 * 24 * H),
    cost_eur: 212.9,
    best: { run_id: "20260927-091500-baseline-a91d", checkpoint: "last", quality: 27.6, robustness: 0.88, source: "val" },
  },
  {
    name: "video-framewise",
    title: "Vidéo — frame par frame",
    modality: "video",
    description: "Réutilise le modèle image sur chaque frame ; robustesse au H.264 et au recadrage temporel.",
    message_bits: 48,
    default_benchmark: null,
    tags: ["video"],
    objectives: OBJECTIVES,
    n_experiments: 1,
    n_hpo_plans: 0,
    n_runs: 0,
    n_running: 0,
    last_activity: null,
    cost_eur: 0,
    best: null,
  },
];

function summary(bitAcc, psnr, extra = {}) {
  return {
    "val/loss": 0.31,
    step: 1500,
    "val/psnr": psnr,
    "best/val/psnr": psnr,
    "val/ssim": 0.985,
    "val/bit_acc": Math.min(0.999, bitAcc + 0.05),
    "val/bit_acc/jpeg50": bitAcc - 0.03,
    "val/bit_acc/blur": bitAcc + 0.02,
    "val/bit_acc/crop50": bitAcc - 0.12,
    "val/bit_acc_mean": bitAcc,
    "best/val/bit_acc_mean": bitAcc,
    ...extra,
  };
}

const PARAMS = {
  "model.name": "hidden",
  "model.params.channels": 64,
  "optimizer.name": "adamw",
  "optimizer.lr": 0.001,
  "data.batch_size": 32,
  "loss.terms.msg.weight": 1.0,
  "loss.terms.img.weight": 100.0,
  seed: 0,
};

function run(o) {
  return {
    run_id: o.run_id,
    project: o.project || "image-hidden",
    experiment: o.experiment || "baseline",
    modality: o.modality || "image",
    status: o.status,
    host: o.host,
    created_at: o.created_at,
    started_at: o.started_at ?? o.created_at,
    ended_at: o.ended_at ?? null,
    duration_s: o.duration_s ?? null,
    cost_eur: o.cost_eur ?? 0,
    study: o.study ?? null,
    trial: o.trial ?? null,
    parent: o.parent ?? null,
    tags: o.tags ?? [],
    progress: o.progress ?? null,
    summary: o.summary ?? {},
    params: { ...PARAMS, ...(o.params || {}) },
    error: o.error ?? null,
    user: o.user === undefined ? "justin" : o.user,
  };
}

const RUN_RUNNING = "20260930-121702-resolution_curriculum-4b2f";
const RUN_BEST = "20260929-141203-wsd_branch-7c1e";
const RUN_PARTIAL = "20260930-080455-baseline-d3a0";
const STUDY_ID = "20260929-100000-lr_weights-3e51";

const runs = [
  run({
    run_id: RUN_RUNNING,
    experiment: "resolution_curriculum",
    status: "running",
    host: "scw-l40s",
    created_at: iso(2 * H + 12 * MIN),
    cost_eur: 3.9,
    tags: ["res256"],
    progress: { step: 23400, total_steps: 60000, phase: "attack_ramp", phase_index: 1, n_phases: 3 },
    summary: summary(0.842, 36.4),
    params: { "data.batch_size": 48 },
  }),
  run({
    run_id: RUN_PARTIAL,
    status: "completed",
    host: "scw-l40s",
    created_at: iso(5 * H),
    ended_at: iso(3 * H + 20 * MIN),
    duration_s: 5980,
    cost_eur: 2.83,
    tags: ["baseline"],
    progress: { step: 30000, total_steps: 30000, phase: "finetune_imperceptibility", phase_index: 2, n_phases: 3 },
    summary: summary(0.903, 37.9),
    user: "marie",
  }),
  run({
    run_id: RUN_BEST,
    experiment: "wsd_branch",
    status: "completed",
    host: "scw-h100",
    created_at: iso(26 * H),
    ended_at: iso(20 * H),
    duration_s: 21400,
    cost_eur: 19.6,
    tags: ["wsd", "best"],
    parent: { run_id: RUN_PARTIAL, checkpoint: "phase_end:warmup", start_phase: "attack_ramp" },
    progress: { step: 40000, total_steps: 40000, phase: "decay", phase_index: 2, n_phases: 3 },
    summary: summary(0.931, 39.1),
  }),
  run({
    run_id: "20260929-161000-baseline-0f11",
    status: "failed",
    host: "scw-l40s",
    created_at: iso(22 * H),
    ended_at: iso(21 * H + 50 * MIN),
    duration_s: 600,
    cost_eur: 0.3,
    progress: { step: 1200, total_steps: 30000, phase: "warmup", phase_index: 0, n_phases: 3 },
    summary: summary(0.51, 30.2),
    error: 'Traceback (most recent call last):\n  File "wmlab/train/trainer.py", line 412, in step\n    loss.backward()\nRuntimeError: CUDA out of memory. Tried to allocate 2.10 GiB',
    user: "marie",
  }),
  run({
    run_id: "20260930-072458-baseline-92be",
    status: "completed",
    host: "local",
    created_at: iso(6 * H),
    ended_at: iso(6 * H - 103_000),
    duration_s: 103,
    cost_eur: 0,
    tags: ["baseline", "demo", "smoke"],
    progress: { step: 1500, total_steps: 1500, phase: "finetune_imperceptibility", phase_index: 2, n_phases: 3 },
    summary: summary(0.713, 34.2),
    user: null,
  }),
  run({
    run_id: "20260927-091500-baseline-a91d",
    project: "audio-seal-lite",
    modality: "audio",
    status: "completed",
    host: "scw-old",
    created_at: iso(3 * 24 * H),
    ended_at: iso(3 * 24 * H - 4 * H),
    duration_s: 14400,
    cost_eur: 8.4,
    progress: { step: 20000, total_steps: 20000, phase: "main", phase_index: 0, n_phases: 1 },
    summary: { "val/bit_acc_mean": 0.88, "val/snr": 27.6, "val/bit_acc": 0.93 },
    user: "marc",
  }),
];

// HPO trials (hidden unless include_trials)
const trials = Array.from({ length: 6 }).map((_, i) =>
  run({
    run_id: `20260929-10${(10 + i * 7).toString().padStart(2, "0")}00-lr_weights-t${i}`,
    experiment: "baseline",
    status: i === 2 ? "killed" : "completed",
    error: i === 2 ? "pruned" : null,
    host: "scw-h100",
    created_at: iso(30 * H - i * 40 * MIN),
    ended_at: iso(29 * H - i * 40 * MIN),
    duration_s: 2400,
    cost_eur: 2.2,
    study: STUDY_ID,
    trial: i,
    progress: { step: i === 2 ? 4000 : 10000, total_steps: 10000, phase: "attack_ramp", phase_index: 1, n_phases: 3 },
    summary: summary(0.8 + i * 0.02, 35 + i * 0.6),
    params: { "optimizer.lr": [1e-3, 5e-4, 2e-3, 8e-4, 3e-4, 1.2e-3][i], "loss.terms.img.weight": [100, 80, 150, 120, 60, 90][i] },
  }),
);

const studies = [
  {
    study_id: STUDY_ID,
    project: "image-hidden",
    plan: "lr_weights",
    experiment: "baseline",
    status: "completed",
    host: "scw-h100",
    created_at: iso(30 * H),
    ended_at: iso(26 * H),
    n_trials: 6,
    n_complete: 5,
    n_pruned: 1,
    n_failed: 0,
    n_running: 0,
    objectives: [{ metric: "val/bit_acc_mean", direction: "maximize" }],
    best: [{ trial: 5, values: [0.9], params: { "optimizer.lr": 1.2e-3, "loss.terms.img.weight": 90 }, run_id: trials[5].run_id }],
    cost_eur: 13.2,
    budget: { max_trials: 8, max_hours: 6, max_eur: 40, parallel: 2 },
    user: "justin",
  },
];

const hosts = [
  { name: "local", kind: "local", price_eur_per_hour: 0, max_concurrent_jobs: 1, gpu: null, notes: null, scaleway: null, decommissioned_at: null, unretrieved_checkpoints: 0, unretrieved_bytes: 0, inventory_at: iso(10 * MIN), active_jobs: 0 },
  {
    name: "scw-l40s",
    kind: "ssh",
    ssh_target: "scw-l40s",
    workdir: "/root/wmlab",
    data_dir: "/root/wmlab-data",
    python: ".venv/bin/python",
    price_eur_per_hour: 1.4,
    gpu: "L40S 48 Go",
    max_concurrent_jobs: 2,
    notes: "Instance fr-par-2, image Ubuntu GPU OS 12.",
    scaleway: { zone: "fr-par-2", server_id: "6a1f3c2e-8b7d-4e21-9f10-2c3d4e5f6a7b" },
    decommissioned_at: null,
    unretrieved_checkpoints: 3,
    unretrieved_bytes: 2_576_980_378,
    inventory_at: iso(18 * MIN),
    active_jobs: 1,
  },
  {
    name: "scw-h100",
    kind: "ssh",
    ssh_target: "scw-h100",
    workdir: "/root/wmlab",
    data_dir: "/root/wmlab-data",
    python: ".venv/bin/python",
    price_eur_per_hour: 3.2,
    gpu: "H100 80 Go",
    max_concurrent_jobs: 2,
    notes: null,
    scaleway: { zone: "fr-par-2", server_id: "0c9d8e7f-6a5b-4c3d-8e2f-1a0b9c8d7e6f" },
    decommissioned_at: null,
    unretrieved_checkpoints: 0,
    unretrieved_bytes: 0,
    inventory_at: iso(2 * H),
    active_jobs: 0,
  },
  {
    name: "scw-old",
    kind: "ssh",
    ssh_target: "scw-old",
    workdir: "/root/wmlab",
    data_dir: "/root/wmlab-data",
    python: ".venv/bin/python",
    price_eur_per_hour: 1.1,
    gpu: "L4 24 Go",
    max_concurrent_jobs: 1,
    notes: "Première machine du projet audio.",
    scaleway: null,
    decommissioned_at: iso(2 * 24 * H),
    unretrieved_checkpoints: 0,
    unretrieved_bytes: 0,
    inventory_at: iso(2 * 24 * H),
    active_jobs: 0,
  },
];

let jobSeq = 100;
const job = (o) => ({
  job_id: o.job_id || `job-${++jobSeq}`,
  kind: o.kind,
  host: o.host,
  target: o.target,
  overrides: o.overrides || {},
  status: o.status,
  run_id: o.run_id ?? null,
  study_id: o.study_id ?? null,
  created_at: o.created_at,
  started_at: o.started_at ?? o.created_at,
  ended_at: o.ended_at ?? null,
  max_hours: o.max_hours ?? null,
  smoke: !!o.smoke,
  error: o.error ?? null,
  pid: o.pid ?? null,
  last_sync_at: o.last_sync_at ?? null,
  user: o.user === undefined ? "justin" : o.user,
  git_ref: "git_ref" in o ? o.git_ref : "main",
  git_sha: "git_sha" in o ? o.git_sha : "3f2a9c1d7e5b4a6c8d9e0f1a2b3c4d5e6f7a8b9c",
  retrieval: o.retrieval ?? null,
  result: o.result ?? null,
});

const jobs = [
  job({
    job_id: "job-091",
    kind: "train",
    host: "scw-l40s",
    target: "projects/image-hidden/experiments/resolution_curriculum.yaml",
    status: "running",
    run_id: RUN_RUNNING,
    created_at: iso(2 * H + 13 * MIN),
    pid: 41822,
    last_sync_at: iso(1 * MIN),
    git_ref: "feature/res256",
    git_sha: "9b1e2f3a4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f",
  }),
  job({
    job_id: "job-090",
    kind: "train",
    host: "scw-l40s",
    target: "projects/image-hidden/experiments/baseline.yaml",
    status: "completed",
    run_id: RUN_PARTIAL,
    created_at: iso(5 * H),
    ended_at: iso(3 * H + 20 * MIN),
    last_sync_at: iso(3 * H + 19 * MIN),
    user: "marie",
    retrieval: { status: "partial", checkpoints: 6, bytes: 5_153_960_755, missing: ["step_020000.pt", "step_025000.pt", "last.pt"] },
  }),
  job({
    job_id: "job-089",
    kind: "retrieve",
    host: "scw-h100",
    target: "scw-h100",
    status: "completed",
    created_at: iso(2 * H + 5 * MIN),
    ended_at: iso(2 * H),
    result: { runs: 7, studies: 1, checkpoints: 41, bytes: 27_917_287_424, missing: [] },
    git_ref: null,
    git_sha: null,
  }),
  job({
    job_id: "job-088",
    kind: "hpo",
    host: "scw-h100",
    target: "projects/image-hidden/hpo/lr_weights.yaml",
    status: "completed",
    study_id: STUDY_ID,
    created_at: iso(30 * H),
    ended_at: iso(26 * H),
    retrieval: { status: "complete", checkpoints: 24, bytes: 16_106_127_360, missing: [] },
  }),
  job({
    job_id: "job-087",
    kind: "setup",
    host: "scw-l40s",
    target: "scw-l40s",
    status: "completed",
    created_at: iso(3 * 24 * H),
    ended_at: iso(3 * 24 * H - 6 * MIN),
    git_ref: null,
    git_sha: null,
  }),
];

const inventories = {
  "scw-l40s": {
    host: "scw-l40s",
    reachable: true,
    error: null,
    checked_at: iso(18 * MIN),
    active_jobs: 1,
    remote_data_dir: "/root/wmlab-data",
    runs: [
      { run_id: RUN_RUNNING, project: "image-hidden", status: "running", checkpoints_total: 4, checkpoints_local: 4, bytes_remote: 3_435_973_836, bytes_missing: 0, indexed: true },
      { run_id: RUN_PARTIAL, project: "image-hidden", status: "completed", checkpoints_total: 6, checkpoints_local: 3, bytes_remote: 5_153_960_755, bytes_missing: 2_576_980_378, indexed: true },
      { run_id: "20260929-161000-baseline-0f11", project: "image-hidden", status: "failed", checkpoints_total: 1, checkpoints_local: 1, bytes_remote: 858_993_459, bytes_missing: 0, indexed: true },
      { run_id: "20260928-233000-baseline-cli9", project: null, status: "completed", checkpoints_total: 2, checkpoints_local: 2, bytes_remote: 1_717_986_918, bytes_missing: 0, indexed: false },
    ],
    studies: [],
    unretrieved_checkpoints: 3,
    unretrieved_bytes: 2_576_980_378,
    unindexed_runs: 1,
  },
  "scw-h100": {
    host: "scw-h100",
    reachable: true,
    error: null,
    checked_at: iso(2 * H),
    active_jobs: 0,
    remote_data_dir: "/root/wmlab-data",
    runs: [
      { run_id: RUN_BEST, project: "image-hidden", status: "completed", checkpoints_total: 9, checkpoints_local: 9, bytes_remote: 7_730_941_132, bytes_missing: 0, indexed: true },
      ...trials.map((t) => ({ run_id: t.run_id, project: "image-hidden", status: t.status, checkpoints_total: 4, checkpoints_local: 4, bytes_remote: 3_435_973_836, bytes_missing: 0, indexed: true })),
    ],
    studies: [{ study_id: STUDY_ID, trials: 6, synced: true }],
    unretrieved_checkpoints: 0,
    unretrieved_bytes: 0,
    unindexed_runs: 0,
  },
  "scw-old": { host: "scw-old", reachable: false, error: "ssh: connect to host scw-old port 22: No route to host", checked_at: iso(2 * 24 * H), active_jobs: 0, remote_data_dir: null, runs: [], studies: [], unretrieved_checkpoints: 0, unretrieved_bytes: 0, unindexed_runs: 0 },
  local: { host: "local", reachable: true, error: null, checked_at: iso(10 * MIN), active_jobs: 0, remote_data_dir: null, runs: [], studies: [], unretrieved_checkpoints: 0, unretrieved_bytes: 0, unindexed_runs: 0 },
};

const instances = {
  "scw-l40s": { configured: true, state: "running", commercial_type: "L40S-1-48G", zone: "fr-par-2", error: null },
  "scw-h100": { configured: true, state: "stopped in place", commercial_type: "H100-1-80G", zone: "fr-par-2", error: null },
};

let git = {
  available: true,
  hosted: true,
  branch: "main",
  sha: "3f2a9c1d7e5b4a6c8d9e0f1a2b3c4d5e6f7a8b9c",
  short_sha: "3f2a9c1",
  dirty: false,
  remote: "git@github.com:studio-ai/watermark-lab.git",
  ahead: 0,
  behind: 1,
  last_pull_at: iso(50_000),
  last_pull_error: null,
};

// ---------------------------------------------------------------------------
// Accounts and sessions (§12.2) — passwords in clear: this is a mock, never served
// ---------------------------------------------------------------------------

function account(o) {
  return {
    id: o.id,
    name: o.name,
    email: o.email,
    role: o.role,
    status: o.status,
    password: o.password ?? DEMO_PASSWORD,
    must_change_password: !!o.must_change_password,
    created_at: o.created_at,
    approved_at: o.status === "pending" ? null : (o.approved_at ?? o.created_at),
    approved_by: o.status === "pending" ? null : (o.approved_by ?? "cli"),
    disabled_at: o.disabled_at ?? null,
    last_login_at: o.last_login_at ?? null,
  };
}

const users = [
  account({ id: "u_1f3c9a0b7d2e", name: "justin", email: "justin@example.com", role: "admin", status: "active", created_at: iso(9 * 24 * H), last_login_at: iso(2 * H) }),
  account({ id: "u_5a8e2c4f6b10", name: "marie", email: "marie@example.com", role: "member", status: "active", created_at: iso(8 * 24 * H), approved_by: "justin", last_login_at: iso(26 * H) }),
  account({ id: "u_9c3d7e1a2b4f", name: "paul", email: "paul@example.com", role: "member", status: "pending", created_at: iso(3 * H) }),
  account({ id: "u_2e6f8a0c4d1b", name: "marc", email: "marc@example.com", role: "member", status: "disabled", created_at: iso(7 * 24 * H), approved_by: "justin", disabled_at: iso(24 * H), last_login_at: iso(3 * 24 * H) }),
  account({ id: "u_7b1d3f5a9c2e", name: "nina", email: "nina@example.com", role: "member", status: "active", created_at: iso(5 * H), approved_by: "justin", must_change_password: true }),
];

/** Session token → user id (the real server stores SHA-256 digests, §12.2). */
const sessions = new Map();
function resetSessions() {
  sessions.clear();
  sessions.set(MEMBER_TOKEN, users.find((u) => u.name === "marie").id);
}
resetSessions();

/** Implicit user of the server token. */
const SERVER_USER = { id: "", name: "admin", email: "", role: "admin" };

const userInfo = (u) => {
  const { password: _secret, ...info } = u;
  return info;
};

const meOf = (auth) => ({
  id: auth.user.id,
  name: auth.user.name,
  email: auth.user.email,
  role: auth.user.role,
  hosted: settings.hosted.enabled,
  public_url: settings.hosted.public_url,
  must_change_password: !!auth.user.must_change_password,
  auth: auth.kind,
});

function openSession(user) {
  const token = `wmls_${crypto.randomBytes(24).toString("hex")}`;
  sessions.set(token, user.id);
  return token;
}

function revokeSessions(userId, keep = null) {
  for (const [token, id] of sessions) if (id === userId && token !== keep) sessions.delete(token);
}

/** `X-WMLab-Token` → {user, kind, token} or null (a session of a non-active account is invalid). */
function resolveAuth(token) {
  if (!token) return null;
  if (token === SERVER_TOKEN) return { user: SERVER_USER, kind: "server_token", token: null };
  const id = sessions.get(token);
  const user = id && users.find((u) => u.id === id);
  if (!user || user.status !== "active") return null;
  return { user, kind: "session", token };
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const normalizeEmail = (e) => String(e || "").trim().toLowerCase();

function passwordPolicy(password, email, name) {
  if (typeof password !== "string" || password.length < 12) return "Le mot de passe doit contenir au moins 12 caractères.";
  if (password.length > 256) return "Le mot de passe ne peut pas dépasser 256 caractères.";
  const lower = password.toLowerCase();
  if (lower === normalizeEmail(email) || lower === String(name || "").toLowerCase()) return "Le mot de passe ne peut pas être l'e-mail ou l'identifiant.";
  return null;
}

function identityError(email, name) {
  if (!EMAIL_RE.test(email)) return "Adresse e-mail invalide.";
  if (!NAME_RE.test(String(name || ""))) return "Identifiant invalide : lettres, chiffres et _ - . (64 caractères au plus).";
  if (String(name).toLowerCase() === "admin") return "« admin » est réservé au jeton du serveur.";
  return null;
}

function temporaryPassword() {
  const alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  return Array.from(crypto.randomBytes(16), (b) => alphabet[b % alphabet.length]).join("");
}

/** Sliding-window limiter: `key` → timestamps; resolves the Retry-After (s) once `max` is reached. */
const attempts = new Map();
function limited(key, max, windowMs) {
  const t = Date.now();
  const list = (attempts.get(key) || []).filter((x) => t - x < windowMs);
  attempts.set(key, list);
  return list.length >= max ? Math.ceil((list[0] + windowMs - t) / 1000) : 0;
}
const recordAttempt = (key) => attempts.set(key, [...(attempts.get(key) || []), Date.now()]);

const activeAdmins = () => users.filter((u) => u.role === "admin" && u.status === "active");

/** Member policy (§9): GET only, minus the admin GET routes; logout / password are allowed. */
const ADMIN_GET = [/^\/files$/, /^\/registry$/, /^\/runs\/[^/]+\/logs$/, /^\/users$/, /^\/git$/, /^\/settings$/, /^\/hosts\/[^/]+\/(inventory|instance)$/];
const MEMBER_WRITES = ["/auth/logout", "/auth/password"];
function forbiddenForMember(method, p) {
  if (method !== "GET") return !MEMBER_WRITES.includes(p);
  return ADMIN_GET.some((re) => re.test(p));
}

// Redacted responses for a member (§9): nothing that describes the code or the infrastructure.
/** Last non-empty line of an error (no traceback for a member). */
const lastLine = (e) => (typeof e === "string" ? e.trim().split("\n").filter(Boolean).slice(-1)[0] ?? "" : e);
const redactSummary = (r) => ({ ...r, params: {}, error: r.error ? lastLine(r.error) : r.error });
const redactRun = (d) => ({
  ...redactSummary(d),
  spec: {},
  spec_yaml: "",
  spec_path: null,
  overrides: {},
  plan: null,
  events: d.events.map((e) => (e.error || e.message ? { ...e, ...(e.error ? { error: lastLine(e.error) } : {}), ...(e.message ? { message: lastLine(e.message) } : {}) } : e)),
  env: { git: null, git_diff: null, hardware: d.env.hardware, pip_freeze_lines: 0, command: null, launch: null },
});
const redactJob = (j) => ({ ...j, overrides: {} });
const redactHost = (h) => ({ ...h, ssh_target: null, workdir: null, data_dir: null, python: null, scaleway: null, notes: null });
const redactDataset = (d) => ({ ...d, paths: {} });

const settings = {
  name: "Studio AI — watermarking",
  budget: { total_eur: 40000, start: "2026-09-01", end: "2027-02-28", manual_entries: [{ label: "Crédits Scaleway", eur: 120, date: "2026-09-10", project: null }] },
  data_dir: ".wmlab",
  sync_interval_s: 60,
  default_host: "scw-l40s",
  hosted: { enabled: true, git_branch: "main", auto_pull_s: 60, auto_commit: true, public_url: "https://lab.studio-ai.example", allow_registration: true },
};

const benchmarks = [
  {
    name: "image_v1",
    path: "benchmarks/image_v1.yaml",
    modality: "image",
    version: 1,
    description: "JPEG, flou, recadrage, bruit — 256 px.",
    attacks: [
      { label: "jpeg50", name: "jpeg", params: { quality: 50 } },
      { label: "blur", name: "gaussian_blur", params: { sigma: 1.0 } },
      { label: "crop50", name: "crop", params: { ratio: 0.5 } },
    ],
    quality_metrics: ["psnr", "ssim"],
    fpr: [0.01, 0.001],
    n_items: 500,
    valid: true,
    errors: [],
  },
  {
    name: "audio_v1",
    path: "benchmarks/audio_v1.yaml",
    modality: "audio",
    version: 1,
    description: "MP3 64k, rééchantillonnage, bruit blanc.",
    attacks: [{ label: "mp3_64", name: "mp3", params: { bitrate: 64 } }],
    quality_metrics: ["snr"],
    fpr: [0.01],
    n_items: 200,
    valid: true,
    errors: [],
  },
];

const datasets = [
  { name: "coco2017", path: "datasets/coco2017.yaml", modality: "image", description: "COCO 2017 train (118 k images).", license: "CC BY 4.0", source: "https://cocodataset.org", paths: { local: "~/datasets/coco2017", "scw-l40s": "/data/coco2017" }, used_by: ["projects/image-hidden/experiments/resolution_curriculum.yaml"], valid: true, errors: [] },
  { name: "div2k_valid", path: "datasets/div2k_valid.yaml", modality: "image", description: "DIV2K validation (100 images HR).", license: "académique", source: "https://data.vision.ee.ethz.ch/cvl/DIV2K/", paths: { local: "~/datasets/div2k" }, used_by: [], valid: true, errors: [] },
];

const leaderboard = {
  benchmarks: ["image_v1"],
  quality_metrics: ["psnr", "ssim"],
  robustness_metrics: ["bit_acc_mean"],
  attacks: ["jpeg50", "blur", "crop50"],
  rows: [RUN_BEST, RUN_PARTIAL].map((id, i) => ({
    run_id: id,
    project: "image-hidden",
    experiment: i === 0 ? "wsd_branch" : "baseline",
    checkpoint: i === 0 ? "step_040000" : "last",
    benchmark: "image_v1",
    source: "benchmark",
    created_at: iso((20 - i) * H),
    quality: { psnr: i === 0 ? 39.1 : 37.9, ssim: 0.99 },
    summary: { bit_acc_mean: i === 0 ? 0.931 : 0.903, psnr: i === 0 ? 39.1 : 37.9 },
    robustness: { jpeg50: { bit_acc: 0.9, "tpr@0.01": 0.98 }, blur: { bit_acc: 0.95, "tpr@0.01": 0.99 }, crop50: { bit_acc: 0.82, "tpr@0.01": 0.9 } },
  })),
};

const costs = {
  total_eur: 40000,
  spent_eur: 1500.3,
  remaining_eur: 38499.7,
  running_eur_per_hour: 1.4,
  by_project: [
    { project: "image-hidden", eur: 1287.4, gpu_hours: 640 },
    { project: "audio-seal-lite", eur: 212.9, gpu_hours: 190 },
  ],
  by_host: [
    { host: "scw-l40s", eur: 700.2, gpu_hours: 500 },
    { host: "scw-h100", eur: 680.1, gpu_hours: 212 },
    { host: "scw-old", eur: 120, gpu_hours: 118 },
  ],
  by_month: [{ month: "2026-09", eur: 1500.3, gpu_hours: 830 }],
  items: runs.map((r) => ({ kind: "run", id: r.run_id, project: r.project, host: r.host, started_at: r.started_at, hours: (r.duration_s || 0) / 3600, eur: r.cost_eur })),
  budget: { total_eur: 40000, start: "2026-09-01", end: "2027-02-28" },
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function series(n, f) {
  const steps = [];
  const values = [];
  for (let i = 0; i <= n; i += Math.max(1, Math.floor(n / 120))) {
    steps.push(i);
    values.push(f(i / n));
  }
  return { steps, values };
}

function planFor(total) {
  const p = [
    ["warmup", 0.2],
    ["attack_ramp", 0.6],
    ["finetune_imperceptibility", 0.2],
  ];
  let start = 0;
  const phases = p.map(([name, frac]) => {
    const steps = Math.round(total * frac);
    const ph = {
      name,
      start,
      end: start + steps,
      steps,
      lr: [[start, name === "finetune_imperceptibility" ? 0.3 : 1], [start + steps, name === "warmup" ? 1 : 0.3]],
      attack_strength: [[start, name === "warmup" ? 0 : name === "attack_ramp" ? 0 : 1], [start + steps, name === "warmup" ? 0 : 1]],
      loss_weights: { img: [[start, name === "warmup" ? 0.1 : 1], [start + steps, 1]] },
      attacks: name === "warmup" ? [] : ["jpeg", "blur", "crop"],
    };
    start += steps;
    return ph;
  });
  return { total_steps: total, phases };
}

function runDetail(r) {
  const total = r.progress ? r.progress.total_steps : 1500;
  const specPath = `projects/${r.project}/experiments/${r.experiment}.yaml`;
  let specYaml = "";
  try {
    specYaml = fs.readFileSync(path.join(WORKSPACE, specPath), "utf8");
  } catch {
    specYaml = `name: ${r.experiment}\nmodel: {name: hidden}\n`;
  }
  const checkpoints = [1, 2, 3, 4, 5, 6]
    .filter((i) => (i * total) / 6 <= (r.progress ? r.progress.step : total))
    .map((i) => ({
      name: `step_${String(Math.round((i * total) / 6)).padStart(6, "0")}`,
      step: Math.round((i * total) / 6),
      phase: i <= 1 ? "warmup" : i <= 4 ? "attack_ramp" : "finetune_imperceptibility",
      metrics: { "val/bit_acc_mean": (r.summary["val/bit_acc_mean"] || 0.7) - (6 - i) * 0.02, "val/psnr": (r.summary["val/psnr"] || 34) - (6 - i) * 0.4 },
      tags: i === 6 ? ["last", "best:val/bit_acc_mean"] : i === 1 ? ["phase_end:warmup"] : [],
      created_at: r.created_at,
      size_bytes: 858_993_459,
      available_locally: !(r.run_id === RUN_PARTIAL && i >= 4),
    }));
  return {
    ...r,
    spec: { name: r.experiment, model: { name: "hidden", params: { channels: 64 } }, phases: [] },
    spec_yaml: specYaml,
    spec_path: specPath,
    env: {
      git: { sha: "3f2a9c1d7e5b4a6c8d9e0f1a2b3c4d5e6f7a8b9c", branch: "main", dirty: false, remote: git.remote },
      git_diff: null,
      hardware: { gpu: r.host === "local" ? "Apple M4 (MPS)" : r.host === "scw-h100" ? "NVIDIA H100 80GB" : "NVIDIA L40S 48GB", cpu: 16, ram_gb: 64 },
      pip_freeze_lines: 142,
      command: `python -m wmlab.cli train ${specPath}`,
      launch: { host: r.host, user: r.user, git_ref: "main" },
    },
    plan: planFor(total),
    events: [
      { t: 0, type: "status", status: "running" },
      { t: 1, type: "phase_start", phase: "warmup" },
      { t: 600, type: "phase_end", phase: "warmup" },
      { t: 601, type: "phase_start", phase: "attack_ramp" },
      { t: 900, type: "checkpoint", name: "step_010000", tags: ["phase_end:warmup"] },
      ...(r.error ? [{ t: 1200, type: "error", message: r.error }] : []),
      ...(r.ended_at ? [{ t: r.duration_s || 1500, type: "status", status: r.status }] : []),
    ],
    checkpoints,
    evals:
      r.run_id === RUN_BEST || r.run_id === RUN_PARTIAL
        ? [
            {
              run_id: r.run_id,
              checkpoint: checkpoints[checkpoints.length - 1]?.name || "last",
              benchmark: "image_v1",
              benchmark_version: 1,
              created_at: r.ended_at || r.created_at,
              duration_s: 240,
              nbits: 48,
              n_items: 500,
              quality: { psnr: r.summary["val/psnr"] + 1, ssim: 0.99 },
              robustness: { jpeg50: { bit_acc: 0.9, "tpr@0.01": 0.98 }, blur: { bit_acc: 0.95, "tpr@0.01": 0.99 }, crop50: { bit_acc: 0.82, "tpr@0.01": 0.9 } },
              summary: { bit_acc_mean: r.summary["val/bit_acc_mean"] + 0.02, psnr: r.summary["val/psnr"] + 1 },
            },
          ]
        : [],
    notes: r.run_id === RUN_BEST ? "## Conclusion\n\nMeilleur compromis à ce jour : **39,1 dB / 0,931**. Le branchement depuis `phase_end:warmup` économise ~2 h de GPU.\n\n- [x] Évaluer sur image_v1\n- [ ] Comparer avec resolution_curriculum" : "",
    samples: [],
    metric_keys: ["train/loss", "train/bit_acc", "val/loss", "val/bit_acc", "val/bit_acc_mean", "val/psnr", "val/ssim", "lr", "attack/strength"],
    price_eur_per_hour: hosts.find((h) => h.name === r.host)?.price_eur_per_hour ?? null,
    datasets: { train: "eb3e4d694098", val: "b4b53215af55" },
    overrides: {},
  };
}

function metricsFor(r, keys) {
  const total = r.progress ? r.progress.step : 1500;
  const all = {
    "train/loss": series(total, (t) => 2.2 * Math.exp(-3 * t) + 0.25 + 0.05 * Math.sin(t * 40)),
    "train/bit_acc": series(total, (t) => 0.5 + 0.48 * (1 - Math.exp(-4 * t))),
    "val/loss": series(total, (t) => 1.8 * Math.exp(-3 * t) + 0.3),
    "val/bit_acc": series(total, (t) => 0.5 + 0.47 * (1 - Math.exp(-4 * t))),
    "val/bit_acc_mean": series(total, (t) => 0.5 + ((r.summary["val/bit_acc_mean"] || 0.8) - 0.5) * (1 - Math.exp(-3.5 * t))),
    "val/psnr": series(total, (t) => 28 + ((r.summary["val/psnr"] || 36) - 28) * Math.min(1, t * 1.3) - (t > 0.2 && t < 0.8 ? 2 : 0)),
    "val/ssim": series(total, (t) => 0.9 + 0.09 * t),
    lr: series(total, (t) => (t < 0.2 ? 1e-3 : t < 0.8 ? 1e-3 : 3e-4)),
    "attack/strength": series(total, (t) => (t < 0.2 ? 0 : t < 0.8 ? (t - 0.2) / 0.6 : 1)),
  };
  const out = {};
  for (const k of keys.length ? keys : Object.keys(all)) if (all[k]) out[k] = all[k];
  const plan = planFor(r.progress ? r.progress.total_steps : 1500);
  return { series: out, phases: plan.phases.map((p) => ({ name: p.name, start: p.start, end: p.end })) };
}

function systemFor(r) {
  const n = 120;
  const t = Array.from({ length: n }, (_, i) => i * 30);
  const total = r.duration_s || 3600;
  return {
    t: t.map((x) => (x * total) / (n * 30)),
    series: {
      "gpu/util": t.map((_, i) => 82 + 10 * Math.sin(i / 5)),
      "gpu/mem_gb": t.map(() => 31.5),
      "cpu/util": t.map((_, i) => 35 + 8 * Math.cos(i / 7)),
      "io/read_mb_s": t.map((_, i) => 120 + 40 * Math.sin(i / 3)),
    },
  };
}

function studyDetail(s) {
  return {
    ...s,
    spec: { name: s.plan, experiment: s.experiment, search_space: { "optimizer.lr": { type: "loguniform", low: 1e-4, high: 3e-3 }, "loss.terms.img.weight": { type: "int", low: 50, high: 150 } } },
    trials: trials.map((t) => ({
      number: t.trial,
      state: t.status === "killed" ? "PRUNED" : "COMPLETE",
      params: { "optimizer.lr": t.params["optimizer.lr"], "loss.terms.img.weight": t.params["loss.terms.img.weight"] },
      values: t.status === "killed" ? null : [t.summary["val/bit_acc_mean"]],
      intermediate: [
        [2000, 0.6],
        [4000, 0.7],
        [10000, t.summary["val/bit_acc_mean"]],
      ],
      run_id: t.run_id,
      duration_s: t.duration_s,
    })),
    importances: { "optimizer.lr": 0.7, "loss.terms.img.weight": 0.3 },
    pareto: [5],
    notes: "## Lecture\n\nLe LR domine (70 %). Zone utile : 8e-4 – 1,3e-3.",
  };
}

function projectDetail(p) {
  const expDir = path.join(WORKSPACE, "projects", p.name, "experiments");
  const hpoDir = path.join(WORKSPACE, "projects", p.name, "hpo");
  const listYaml = (dir) => {
    try {
      return fs
        .readdirSync(dir)
        .filter((f) => /\.ya?ml$/.test(f))
        .map((f) => f.replace(/\.ya?ml$/, ""));
    } catch {
      return [];
    }
  };
  const experiments = (listYaml(expDir).length ? listYaml(expDir) : ["baseline"]).map((name) => ({
    name,
    path: `projects/${p.name}/experiments/${name}.yaml`,
    description: name === "baseline" ? "HiDDeN + curriculum 3 phases" : `Variante ${name}`,
    hypothesis: "",
    model: "hidden",
    n_phases: 3,
    total_steps: name === "baseline" ? 30000 : 60000,
    valid: true,
    errors: [],
    n_runs: runs.filter((r) => r.project === p.name && r.experiment === name).length,
  }));
  const hpo_plans = listYaml(hpoDir).map((name) => ({
    name,
    path: `projects/${p.name}/hpo/${name}.yaml`,
    experiment: "baseline",
    description: `Plan ${name}`,
    n_params: 2,
    objectives: [{ metric: "val/bit_acc_mean", direction: "maximize" }],
    valid: true,
    errors: [],
    n_studies: studies.filter((s) => s.project === p.name && s.plan === name).length,
  }));
  let notes = "";
  try {
    notes = fs.readFileSync(path.join(WORKSPACE, "projects", p.name, "notes.md"), "utf8");
  } catch {
    notes = "";
  }
  return { summary: p, spec: { name: p.name, modality: p.modality, message_bits: p.message_bits }, experiments, hpo_plans, notes };
}

function overview() {
  const all = [...runs, ...trials];
  return {
    workspace: { name: settings.name, path: "/opt/wmlab" },
    budget: { total_eur: costs.total_eur, spent_eur: costs.spent_eur, remaining_eur: costs.remaining_eur, running_eur_per_hour: costs.running_eur_per_hour },
    counts: { projects: projects.length, runs: all.length, running: runs.filter((r) => r.status === "running").length, studies: studies.length, gpu_hours: 830 },
    running: runs.filter((r) => r.status === "running"),
    recent: runs.slice(0, 8),
    projects,
    jobs: [...jobs].sort((a, b) => (a.created_at < b.created_at ? 1 : -1)),
  };
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------

const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".woff2": "font/woff2", ".json": "application/json" };

function send(res, status, body, headers = {}) {
  const data = typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
    "Access-Control-Expose-Headers": "Retry-After",
    ...headers,
  });
  res.end(data);
}

function serveStatic(req, res, pathname) {
  let rel = decodeURIComponent(pathname === "/" ? "/index.html" : pathname);
  const file = path.normalize(path.join(DIST, rel));
  if (!file.startsWith(DIST)) return send(res, 403, { detail: "Chemin refusé." });
  if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    rel = "/index.html";
  }
  const target = rel === "/index.html" ? path.join(DIST, "index.html") : file;
  if (!fs.existsSync(target)) return send(res, 404, { detail: `Build absent : ${DIST} (npm run build).` });
  const ext = path.extname(target);
  res.writeHead(200, {
    "Content-Type": MIME[ext] || "application/octet-stream",
    "Cache-Control": rel === "/index.html" ? "no-cache" : "public, max-age=31536000, immutable",
  });
  fs.createReadStream(target).pipe(res);
}

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const text = Buffer.concat(chunks).toString("utf8");
      try {
        resolve(text ? JSON.parse(text) : null);
      } catch {
        resolve(text);
      }
    });
  });
}

/** Initial state, restored by POST /api/mock/reset (the retrieve / decommission / stop actions mutate it). */
const SNAPSHOT = JSON.stringify({ projects, runs, hosts, inventories, instances, jobs, git, settings, users });
function resetState() {
  const s = JSON.parse(SNAPSHOT);
  users.splice(0, users.length, ...s.users);
  resetSessions();
  attempts.clear();
  projects.splice(0, projects.length, ...s.projects);
  runs.splice(0, runs.length, ...s.runs);
  hosts.splice(0, hosts.length, ...s.hosts);
  jobs.splice(0, jobs.length, ...s.jobs);
  Object.assign(inventories, s.inventories);
  Object.assign(instances, s.instances);
  Object.assign(settings, s.settings);
  git = s.git;
}

const findRun = (id) => [...runs, ...trials].find((r) => r.run_id === id);
const findHost = (name) => hosts.find((h) => h.name === name);

/** Public routes (§9): registration and login. */
async function handleAuth(req, res, p) {
  const ip = req.socket.remoteAddress || "?";
  const b = (await readBody(req)) || {};
  if (p === "/auth/register") {
    if (!settings.hosted.allow_registration) return send(res, 403, { detail: "Les inscriptions sont fermées : demandez un compte à un administrateur." });
    const wait = limited(`register:${ip}`, 5, H);
    if (wait) return send(res, 429, { detail: "Trop d'inscriptions depuis cette adresse : réessayez plus tard." }, { "Retry-After": String(wait) });
    recordAttempt(`register:${ip}`);
    const email = normalizeEmail(b.email);
    const name = String(b.name || "").trim();
    const err = identityError(email, name) || passwordPolicy(b.password, email, name);
    if (err) return send(res, 400, { detail: err });
    // Same answer when the e-mail or the name already exists (no enumeration).
    if (!users.some((u) => u.email === email || u.name.toLowerCase() === name.toLowerCase())) {
      users.push(account({ id: `u_${crypto.randomBytes(6).toString("hex")}`, name, email, role: "member", status: "pending", created_at: new Date().toISOString(), password: b.password }));
    }
    return send(res, 202, { ok: true, status: "pending", message: "Demande enregistrée : un administrateur doit approuver votre compte." });
  }
  // /auth/login
  const email = normalizeEmail(b.email);
  const wait = limited(`login:${email}`, 10, 15 * MIN) || limited(`login-ip:${ip}`, 30, 15 * MIN);
  if (wait) return send(res, 429, { detail: `Trop de tentatives de connexion : réessayez dans ${Math.ceil(wait / 60)} min.` }, { "Retry-After": String(wait) });
  const user = users.find((u) => u.email === email);
  if (!user || user.password !== b.password) {
    recordAttempt(`login:${email}`);
    recordAttempt(`login-ip:${ip}`);
    return send(res, 401, { detail: "E-mail ou mot de passe incorrect." });
  }
  if (user.status === "pending") return send(res, 403, { detail: "Compte en attente d'approbation par un administrateur." });
  if (user.status === "disabled") return send(res, 403, { detail: "Compte désactivé : contactez un administrateur." });
  user.last_login_at = new Date().toISOString();
  const token = openSession(user);
  return send(res, 200, { token, user: meOf({ user, kind: "session" }) });
}

/** §12.2 — accounts (admin) and the current session (logout, password). */
async function handleAccounts(req, res, p, seg, auth) {
  const m = req.method;
  const me = auth.user;
  if (p === "/me") return send(res, 200, meOf(auth));
  if (p === "/auth/logout" && m === "POST") {
    if (auth.token) sessions.delete(auth.token);
    return send(res, 200, { ok: true });
  }
  if (p === "/auth/password" && m === "POST") {
    if (auth.kind === "server_token") return send(res, 400, { detail: "Le jeton du serveur n'a pas de mot de passe : connectez-vous avec un compte." });
    const b = (await readBody(req)) || {};
    if (b.current_password !== me.password) return send(res, 401, { detail: "Mot de passe actuel incorrect." });
    const err = passwordPolicy(b.new_password, me.email, me.name);
    if (err) return send(res, 400, { detail: err });
    me.password = b.new_password;
    me.must_change_password = false;
    revokeSessions(me.id, auth.token);
    return send(res, 200, { ok: true });
  }
  if (p === "/users" && m === "GET") {
    const order = { pending: 0, active: 1, disabled: 1 };
    return send(res, 200, [...users].sort((a, b) => order[a.status] - order[b.status] || a.name.localeCompare(b.name)).map(userInfo));
  }
  if (p === "/users" && m === "POST") {
    const b = (await readBody(req)) || {};
    const email = normalizeEmail(b.email);
    const name = String(b.name || "").trim();
    const err = identityError(email, name);
    if (err) return send(res, 400, { detail: err });
    if (b.role !== "admin" && b.role !== "member") return send(res, 400, { detail: "Rôle invalide : admin ou member." });
    if (users.some((u) => u.email === email)) return send(res, 409, { detail: `Un compte utilise déjà l'e-mail ${email}.` });
    if (users.some((u) => u.name.toLowerCase() === name.toLowerCase())) return send(res, 409, { detail: `L'identifiant « ${name} » est déjà utilisé.` });
    const password = temporaryPassword();
    const u = account({ id: `u_${crypto.randomBytes(6).toString("hex")}`, name, email, role: b.role, status: "active", created_at: new Date().toISOString(), approved_by: me.name, must_change_password: true, password });
    users.push(u);
    return send(res, 200, { user: userInfo(u), temporary_password: password });
  }
  const u = users.find((x) => x.id === seg[1]);
  if (!u) return send(res, 404, { detail: "Compte introuvable." });
  const action = seg[2];
  const b = (await readBody(req)) || {};
  const self = u.id === me.id;
  if (action === "approve") {
    if (u.status !== "pending") return send(res, 409, { detail: `Le compte de ${u.name} n'est pas en attente.` });
    if (b.role !== "admin" && b.role !== "member") return send(res, 400, { detail: "Rôle invalide : admin ou member." });
    Object.assign(u, { status: "active", role: b.role, approved_at: new Date().toISOString(), approved_by: me.name });
    return send(res, 200, userInfo(u));
  }
  if (action === "role") {
    if (self) return send(res, 400, { detail: "Vous ne pouvez pas changer votre propre rôle." });
    if (b.role !== "admin" && b.role !== "member") return send(res, 400, { detail: "Rôle invalide : admin ou member." });
    if (u.role === "admin" && b.role !== "admin" && u.status === "active" && activeAdmins().length <= 1) return send(res, 409, { detail: "Dernier administrateur actif : impossible de le rétrograder." });
    u.role = b.role;
    return send(res, 200, userInfo(u));
  }
  if (action === "disable") {
    if (self) return send(res, 400, { detail: "Vous ne pouvez pas désactiver votre propre compte." });
    if (u.role === "admin" && u.status === "active" && activeAdmins().length <= 1) return send(res, 409, { detail: "Dernier administrateur actif : impossible de le désactiver." });
    Object.assign(u, { status: "disabled", disabled_at: new Date().toISOString() });
    revokeSessions(u.id);
    return send(res, 200, userInfo(u));
  }
  if (action === "enable") {
    Object.assign(u, { status: "active", disabled_at: null });
    return send(res, 200, userInfo(u));
  }
  if (action === "reset-password") {
    const password = temporaryPassword();
    Object.assign(u, { password, must_change_password: true });
    revokeSessions(u.id);
    return send(res, 200, { user: userInfo(u), temporary_password: password });
  }
  return send(res, 404, { detail: `Route inconnue dans le mock : ${m} ${p}` });
}

async function handleApi(req, res, url, auth) {
  const m = req.method;
  const p = url.pathname.replace(/^\/api/, "");
  const q = url.searchParams;
  const seg = p.split("/").filter(Boolean).map(decodeURIComponent);
  const user = auth.user;
  const isAdmin = user.role === "admin";
  const forbid = () => send(res, 403, { detail: "Action réservée aux administrateurs." });

  if (p === "/mock/reset" && m === "POST") {
    resetState();
    return send(res, 200, { ok: true });
  }
  if (p === "/me" || p.startsWith("/auth/") || p === "/users" || seg[0] === "users") return handleAccounts(req, res, p, seg, auth);
  if (p === "/overview") {
    const o = overview();
    return send(res, 200, isAdmin ? o : { ...o, running: o.running.map(redactSummary), recent: o.recent.map(redactSummary), jobs: o.jobs.map(redactJob) });
  }
  if (p === "/git" && m === "GET") return send(res, 200, git);
  if (p === "/git/pull" && m === "POST") {
    if (git.dirty) return send(res, 409, { detail: "Arbre de travail modifié : pull refusé." });
    git = { ...git, behind: 0, sha: "a7c4e2f0b9d8c7e6f5a4b3c2d1e0f9a8b7c6d5e4", short_sha: "a7c4e2f", last_pull_at: new Date().toISOString(), last_pull_error: null };
    return send(res, 200, git);
  }
  if (p === "/projects" && m === "GET") return send(res, 200, projects);
  if (p === "/projects" && m === "POST") {
    const b = await readBody(req);
    const np = { ...projects[2], name: b.name, title: b.title, modality: b.modality, message_bits: b.message_bits, description: b.description, n_runs: 0, cost_eur: 0, best: null, tags: [] };
    projects.push(np);
    return send(res, 200, np);
  }
  if (seg[0] === "projects" && seg.length === 2) {
    const pr = projects.find((x) => x.name === seg[1]);
    return pr ? send(res, 200, projectDetail(pr)) : send(res, 404, { detail: "Projet introuvable." });
  }
  if (seg[0] === "projects" && seg.length === 3 && m === "POST") {
    const b = await readBody(req);
    return send(res, 200, { path: `projects/${seg[1]}/${seg[2] === "hpo" ? "hpo" : "experiments"}/${b.name}.yaml` });
  }
  if (p === "/files" && m === "GET") {
    const rel = q.get("path") || "";
    const file = path.normalize(path.join(WORKSPACE, rel));
    if (!file.startsWith(WORKSPACE) || !fs.existsSync(file)) return send(res, 404, { detail: `Fichier introuvable : ${rel}` });
    const kind = /\/experiments\//.test(rel) ? "experiment" : /\/hpo\//.test(rel) ? "hpo" : /^benchmarks\//.test(rel) ? "benchmark" : /^datasets\//.test(rel) ? "dataset" : "project";
    return send(res, 200, { path: rel, content: fs.readFileSync(file, "utf8"), kind, validation: { valid: true, errors: [] } });
  }
  if (p === "/files" && m === "PUT") {
    const b = await readBody(req);
    const sha = Math.random().toString(16).slice(2, 9);
    return send(res, 200, { saved: true, validation: { valid: true, errors: [] }, git: { committed: `${sha}e1d2c3b4a5f6e7d8c9b0a1f2e3d4c5b6a7f8e9`, pushed: !/unpushed/.test(String(b && b.content)), error: /unpushed/.test(String(b && b.content)) ? "rejected: remote contains work that you do not have locally" : null } });
  }
  if (p === "/validate" && m === "POST") {
    const b = await readBody(req);
    return send(res, 200, { valid: true, errors: [], plan: b && b.kind === "hpo" ? null : planFor(30000) });
  }
  if (p === "/registry") {
    return send(res, 200, {
      entries: {
        model: [{ name: "hidden", kind: "model", modality: ["image", "video"], description: "HiDDeN (Zhu et al. 2018).", params_schema: null, source: "wmlab.models.hidden", builtin: true }],
        attack: [{ name: "jpeg", kind: "attack", modality: ["image"], description: "Compression JPEG différentiable.", params_schema: null, source: "wmlab.attacks.jpeg", builtin: true }],
      },
      errors: [],
    });
  }
  if (p === "/runs" && m === "GET") {
    let list = q.get("include_trials") === "true" ? [...runs, ...trials] : runs;
    for (const key of ["project", "experiment", "status", "study", "host"]) {
      const v = q.get(key);
      if (v) list = list.filter((r) => String(r[key]) === v);
    }
    list = list.slice(0, Number(q.get("limit") || 500));
    return send(res, 200, isAdmin ? list : list.map(redactSummary));
  }
  if (seg[0] === "runs" && seg.length >= 2) {
    const r = findRun(seg[1]);
    if (!r) return send(res, 404, { detail: `Run introuvable : ${seg[1]}` });
    if (seg.length === 2) return send(res, 200, isAdmin ? runDetail(r) : redactRun(runDetail(r)));
    if (seg[2] === "metrics") return send(res, 200, metricsFor(r, (q.get("keys") || "").split(",").filter(Boolean)));
    if (seg[2] === "system") return send(res, 200, systemFor(r));
    if (seg[2] === "logs") return send(res, 200, { lines: Array.from({ length: 40 }, (_, i) => `[${String(i).padStart(4, "0")}] step ${i * 250} loss 0.${900 - i * 12} bit_acc 0.${600 + i * 8}`) });
    if (seg[2] === "notes" && m === "PUT") return send(res, 200, { ok: true });
    if (seg[2] === "tags" && m === "PUT") {
      const b = await readBody(req);
      r.tags = b.tags;
      return send(res, 200, { ok: true });
    }
    if (seg[2] === "stop" && m === "POST") {
      r.status = "killed";
      return send(res, 200, { ok: true });
    }
    if (seg[2] === "checkpoints" && m === "POST") return send(res, 200, { ok: true });
    if (seg[2] === "file") return send(res, 404, { detail: "Aucun fichier dans le mock." });
  }
  if (p === "/compare") {
    const ids = (q.get("runs") || "").split(",").filter(Boolean);
    const found = ids.map(findRun).filter(Boolean);
    const keys = (q.get("keys") || "").split(",").filter(Boolean);
    const out = {};
    for (const r of found) out[r.run_id] = metricsFor(r, keys).series;
    return send(res, 200, { runs: isAdmin ? found : found.map(redactSummary), series: out });
  }
  if (p === "/studies" && m === "GET") return send(res, 200, q.get("project") ? studies.filter((s) => s.project === q.get("project")) : studies);
  if (seg[0] === "studies" && seg.length >= 2) {
    const s = studies.find((x) => x.study_id === seg[1]);
    if (!s) return send(res, 404, { detail: "Étude introuvable." });
    if (seg.length === 2) return send(res, 200, isAdmin ? studyDetail(s) : { ...studyDetail(s), spec: {} });
    if (seg[2] === "notes") return send(res, 200, { ok: true });
  }
  if (p === "/leaderboard") return send(res, 200, q.get("source") === "val" ? { ...leaderboard, rows: leaderboard.rows.map((r) => ({ ...r, source: "val", benchmark: null })) } : leaderboard);
  if (p === "/benchmarks") return send(res, 200, benchmarks);
  if (p === "/datasets") return send(res, 200, isAdmin ? datasets : datasets.map(redactDataset));
  if (p === "/hosts" && m === "GET") return send(res, 200, isAdmin ? hosts : hosts.map(redactHost));
  if (p === "/hosts" && m === "PUT") {
    if (!isAdmin) return forbid();
    const b = await readBody(req);
    hosts.length = 0;
    for (const h of b) hosts.push({ unretrieved_checkpoints: 0, unretrieved_bytes: 0, inventory_at: null, active_jobs: 0, decommissioned_at: null, scaleway: null, ...h });
    return send(res, 200, hosts);
  }
  if (seg[0] === "hosts" && seg.length === 3) {
    const h = findHost(seg[1]);
    if (!h) return send(res, 404, { detail: `Machine inconnue : ${seg[1]}` });
    const action = seg[2];
    if (action === "test" && m === "POST") {
      await new Promise((r) => setTimeout(r, 400));
      return h.name === "scw-old"
        ? send(res, 200, { ok: false, latency_ms: null, output: null, error: "ssh: connect to host scw-old port 22: No route to host" })
        : send(res, 200, { ok: true, latency_ms: 38, output: h.kind === "local" ? "Darwin 25.5.0 · Python 3.12.6" : "Linux 6.8.0 · NVIDIA-SMI 560.35 · Python 3.12.6\n" + (h.gpu || ""), error: null });
    }
    if (action === "setup" && m === "POST") {
      const j = job({ kind: "setup", host: h.name, target: h.name, status: "running", created_at: new Date().toISOString(), user: user.name, git_ref: null, git_sha: null });
      jobs.unshift(j);
      setTimeout(() => {
        j.status = "completed";
        j.ended_at = new Date().toISOString();
      }, 4000);
      return send(res, 200, j);
    }
    if (action === "inventory" && m === "GET") {
      await new Promise((r) => setTimeout(r, 300));
      const inv = inventories[h.name] || { ...inventories.local, host: h.name };
      return send(res, 200, { ...inv, checked_at: new Date().toISOString() });
    }
    if (action === "instance" && m === "GET") {
      return send(res, 200, h.scaleway ? instances[h.name] || { configured: true, state: null, commercial_type: null, zone: h.scaleway.zone, error: "SCW_SECRET_KEY absente sur le serveur" } : { configured: false, state: null, commercial_type: null, zone: null, error: null });
    }
    if (action === "retrieve" && m === "POST") {
      if (h.decommissioned_at) return send(res, 400, { detail: "Machine retirée." });
      const j = job({ kind: "retrieve", host: h.name, target: h.name, status: "running", created_at: new Date().toISOString(), user: user.name, git_ref: null, git_sha: null });
      jobs.unshift(j);
      setTimeout(() => {
        const inv = inventories[h.name];
        const bytes = inv ? inv.unretrieved_bytes : 0;
        const ck = inv ? inv.unretrieved_checkpoints : 0;
        if (inv) {
          for (const r of inv.runs) {
            r.checkpoints_local = r.checkpoints_total;
            r.bytes_missing = 0;
            r.indexed = true;
          }
          inv.unretrieved_checkpoints = 0;
          inv.unretrieved_bytes = 0;
          inv.unindexed_runs = 0;
        }
        h.unretrieved_checkpoints = 0;
        h.unretrieved_bytes = 0;
        h.inventory_at = new Date().toISOString();
        j.status = "completed";
        j.ended_at = new Date().toISOString();
        j.result = { runs: inv ? inv.runs.length : 0, studies: inv ? inv.studies.length : 0, checkpoints: ck + (inv ? inv.runs.reduce((n, r) => n + r.checkpoints_total, 0) : 0), bytes: bytes + 5_000_000_000, missing: [] };
        const partial = jobs.find((x) => x.retrieval && x.retrieval.status === "partial" && x.host === h.name);
        if (partial) partial.retrieval = { ...partial.retrieval, status: "complete", missing: [] };
      }, 5000);
      return send(res, 200, j);
    }
    if (action === "poweroff" && m === "POST") {
      if (!isAdmin) return forbid();
      if (h.active_jobs > 0) return send(res, 409, { detail: `${h.active_jobs} job actif sur ${h.name} : impossible d'éteindre l'instance.` });
      if (instances[h.name]) instances[h.name].state = "stopped";
      return send(res, 200, { ok: true, instance_state: "stopped" });
    }
    if (action === "decommission" && m === "POST") {
      if (!isAdmin) return forbid();
      const b = await readBody(req);
      if (!b || b.confirm !== true) return send(res, 400, { detail: "confirm: true attendu." });
      if (h.active_jobs > 0) return send(res, 409, { detail: `${h.active_jobs} job actif sur ${h.name}.` });
      const inv = inventories[h.name];
      if (inv && inv.unretrieved_checkpoints > 0) return send(res, 409, { detail: `${inv.unretrieved_checkpoints} checkpoints (${(inv.unretrieved_bytes / 2 ** 30).toFixed(1)} Go) non rapatriés — lancez le rapatriement.` });
      if (inv && !inv.reachable && !b.force) return send(res, 409, { detail: "Machine injoignable : inventaire impossible (force: true pour retirer quand même)." });
      h.decommissioned_at = new Date().toISOString();
      const del = !!(b.delete_instance && h.scaleway);
      if (del && instances[h.name]) instances[h.name].state = "deleted";
      return send(res, 200, { ok: true, deleted_instance: del, instance_state: del ? "deleted" : instances[h.name]?.state ?? null, message: del ? `Instance ${h.scaleway.server_id} supprimée.` : "Machine retirée ; supprimez l'instance manuellement si besoin." });
    }
  }
  if (p === "/jobs" && m === "GET") {
    const st = q.get("status");
    const list = st ? jobs.filter((j) => j.status === st) : jobs;
    return send(res, 200, isAdmin ? list : list.map(redactJob));
  }
  if (p === "/jobs" && m === "POST") {
    const b = await readBody(req);
    const h = findHost(b.host);
    if (!h) return send(res, 400, { detail: "Machine inconnue." });
    if (h.decommissioned_at) return send(res, 400, { detail: `${h.name} a été retirée : choisissez une autre machine.` });
    if (b.git_ref && !/^(main|feature\/[\w-]+|[0-9a-f]{7,40})$/.test(b.git_ref)) return send(res, 400, { detail: `Révision git inconnue : ${b.git_ref}` });
    if (b.git_ref && h.kind === "local" && b.git_ref !== git.sha && b.git_ref !== git.branch) return send(res, 400, { detail: "La machine locale exécute la révision courante du serveur." });
    const j = job({ kind: b.kind, host: b.host, target: b.spec_path || b.run_id || "", status: "queued", created_at: new Date().toISOString(), smoke: b.smoke, max_hours: b.max_hours ?? null, overrides: b.overrides || {}, user: user.name, git_ref: b.git_ref || "main", git_sha: git.sha });
    jobs.unshift(j);
    return send(res, 200, j);
  }
  if (p === "/jobs/estimate" && m === "POST") {
    const b = await readBody(req);
    const h = findHost(b.host);
    const hours = /resolution/.test(b.spec_path || "") ? 11.5 : /hpo/.test(b.spec_path || "") ? 6 : 4.2;
    return send(res, 200, { total_steps: /resolution/.test(b.spec_path || "") ? 60000 : 30000, est_hours: hours, est_eur: h ? hours * h.price_eur_per_hour : null, basis: "d'après les 3 derniers runs de cette expérience sur cette machine" });
  }
  if (seg[0] === "jobs" && seg.length === 3 && m === "POST") {
    const j = jobs.find((x) => x.job_id === seg[1]);
    if (!j) return send(res, 404, { detail: "Job introuvable." });
    if (seg[2] === "cancel") {
      j.status = "killed";
      j.ended_at = new Date().toISOString();
    }
    if (seg[2] === "sync") j.last_sync_at = new Date().toISOString();
    return send(res, 200, j);
  }
  if (p === "/costs") return send(res, 200, costs);
  if (p === "/costs.csv") return send(res, 200, "type;id;projet;machine;heures;eur\nrun;x;image-hidden;scw-l40s;2,1;2,94\n", { "Content-Type": "text/csv; charset=utf-8" });
  if (p === "/settings" && m === "GET") return send(res, 200, settings);
  if (p === "/settings" && m === "PUT") {
    if (!isAdmin) return forbid();
    Object.assign(settings, await readBody(req));
    return send(res, 200, settings);
  }
  return send(res, 404, { detail: `Route inconnue dans le mock : ${m} ${p}` });
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
  if (req.method === "OPTIONS") return send(res, 204, "");
  if (!url.pathname.startsWith("/api/")) return serveStatic(req, res, url.pathname);
  const p = url.pathname.replace(/^\/api/, "");
  // `?token=` only on the media files of a run and the CSV export (§9).
  const queryTokenAllowed = req.method === "GET" && (/^\/runs\/[^/]+\/file$/.test(p) || p === "/costs.csv");
  const token = String(req.headers["x-wmlab-token"] || (queryTokenAllowed ? url.searchParams.get("token") : "") || "");
  const auth = resolveAuth(token);
  try {
    if (p === "/health") {
      return send(res, 200, { ok: true, version: VERSION, hosted: settings.hosted.enabled, registration: !!settings.hosted.allow_registration, ...(auth ? { workspace: "/opt/wmlab" } : {}) });
    }
    if (req.method === "POST" && (p === "/auth/login" || p === "/auth/register")) return await handleAuth(req, res, p);
    if (!auth) return send(res, 401, { detail: "Session absente, expirée ou révoquée : connectez-vous." });
    if (auth.user.role !== "admin" && forbiddenForMember(req.method, p)) return send(res, 403, { detail: "Action réservée aux administrateurs." });
    await handleApi(req, res, url, auth);
  } catch (err) {
    send(res, 500, { detail: `Erreur du mock : ${err && err.message ? err.message : err}` });
  }
});

server.listen(PORT, "127.0.0.1", () => {
  process.stderr.write(
    `[mock-api] http://127.0.0.1:${PORT}/ — jeton serveur : ${SERVER_TOKEN} ; comptes : justin@example.com (admin), marie@example.com (membre), mot de passe ${DEMO_PASSWORD} — dist : ${DIST}\n`,
  );
});
