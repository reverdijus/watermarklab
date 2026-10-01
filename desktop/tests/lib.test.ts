/**
 * Unit tests of the pure renderer helpers (run with `npm test`, Node's
 * built-in runner with native TypeScript type stripping).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  formatBytes,
  formatDuration,
  formatEur,
  formatInt,
  formatPercent,
  formatRelative,
  formatSignificant,
  plural,
  shortId,
  toDate,
} from "../src/lib/format.ts";
import {
  formatMetric,
  groupMetricKeys,
  metricDirection,
  metricLabel,
  paretoFront,
  parseConstraint,
  pickKeyMetrics,
  qualityMetricFor,
  satisfies,
  smoothSeries,
} from "../src/lib/metrics.ts";
import { isPrunedTrial, progressFraction, runEta, runStatusLabel } from "../src/lib/runs.ts";
import { attackStrengthPoints, buildRows, valueAt } from "../src/components/phasePlan.ts";

/** Normalise the narrow/no-break spaces used by Intl fr-FR. */
const n = (s: string) => s.replace(/[  ]/g, " ");

test("formatEur uses French separators", () => {
  assert.equal(n(formatEur(1234.56)), "1 234,56 €");
  assert.equal(n(formatEur(0)), "0,00 €");
  assert.equal(formatEur(null), "—");
  assert.equal(n(formatEur(40000, { digits: 0 })), "40 000 €");
});

test("formatInt / formatPercent / formatSignificant", () => {
  assert.equal(n(formatInt(15000)), "15 000");
  assert.equal(n(formatPercent(0.934)), "93,4 %");
  assert.equal(formatSignificant(0.0003), "3,00e-4");
  assert.equal(formatSignificant(Number.NaN), "—");
});

test("formatDuration", () => {
  assert.equal(formatDuration(0.2), "< 1 s");
  assert.equal(formatDuration(42), "42 s");
  assert.equal(formatDuration(192), "3 min 12 s");
  assert.equal(formatDuration(14 * 60), "14 min");
  assert.equal(formatDuration(2 * 3600 + 14 * 60 + 5), "2 h 14 min");
  assert.equal(formatDuration(3 * 3600), "3 h");
  assert.equal(formatDuration(3 * 86400 + 4 * 3600), "3 j 4 h");
  assert.equal(formatDuration(null), "—");
});

test("formatRelative", () => {
  const now = Date.parse("2026-10-01T12:00:00Z");
  assert.equal(formatRelative("2026-10-01T11:59:50Z", now), "à l'instant");
  assert.equal(formatRelative("2026-10-01T11:57:00Z", now), "il y a 3 min");
  assert.equal(formatRelative("2026-10-01T10:00:00Z", now), "il y a 2 h");
  assert.equal(formatRelative("2026-09-30T11:00:00Z", now), "hier");
  assert.equal(formatRelative("2026-09-27T12:00:00Z", now), "il y a 4 j");
  assert.equal(formatRelative("2026-10-01T12:05:00Z", now), "dans 5 min");
  // A few seconds ahead of the (throttled) clock is still "now", not the future.
  assert.equal(formatRelative("2026-10-01T12:00:40Z", now), "à l'instant");
  // epoch seconds (JSONL "t" fields)
  assert.equal(formatRelative(now / 1000 - 120, now), "il y a 2 min");
});

test("toDate accepts ISO, epoch seconds and milliseconds", () => {
  assert.equal(toDate("2026-10-01T12:00:00Z")?.toISOString(), "2026-10-01T12:00:00.000Z");
  assert.equal(toDate(1790000000)?.getTime(), 1790000000000);
  assert.equal(toDate(1790000000000)?.getTime(), 1790000000000);
  assert.equal(toDate("nope"), null);
});

test("formatBytes / plural / shortId", () => {
  assert.equal(n(formatBytes(1536)), "1,5 Ko");
  assert.equal(n(formatBytes(3 * 1024 ** 3)), "3 Go");
  assert.equal(plural(1, "run"), "1 run");
  assert.equal(plural(3, "run"), "3 runs");
  assert.equal(plural(2, "essai HPO", "essais HPO"), "2 essais HPO");
  assert.equal(shortId("20261001-120000-baseline-3fa2"), "baseline-3fa2");
  assert.equal(shortId("job-20261001-120000-ab12"), "job-20261001-120000-ab12");
});

test("metric labels, directions and formatting", () => {
  assert.equal(metricLabel("val/psnr"), "PSNR");
  assert.equal(metricLabel("val/psnr", { withGroup: true }), "PSNR (val)");
  assert.equal(metricLabel("val/bit_acc/jpeg50"), "Bit acc. · jpeg50");
  assert.equal(metricLabel("train/loss/img"), "Perte · img");
  assert.equal(metricLabel("best/val/bit_acc_mean"), "Meilleur · Bit acc. moyenne");
  assert.equal(metricLabel("tpr@1e-06_mean"), "TPR moyen @ FPR 1e-06");
  assert.equal(metricDirection("val/lpips"), "minimize");
  assert.equal(metricDirection("val/bit_acc_mean"), "maximize");
  assert.equal(metricDirection("train/lr"), null);
  assert.equal(n(formatMetric("val/bit_acc_mean", 0.9312)), "93,1 %");
  assert.equal(formatMetric("val/psnr", 38.2), "38,20 dB");
  assert.equal(n(formatMetric("gpu_util", 87.4)), "87 %");
  assert.equal(formatMetric("val/psnr", null), "—");
});

test("groupMetricKeys orders groups and important keys first", () => {
  const groups = groupMetricKeys(["val/ssim", "train/lr", "gpu_util", "val/psnr", "train/loss", "val/bit_acc_mean", "best/val/psnr", "custom"]);
  assert.deepEqual(
    groups.map((g) => g.id),
    ["train", "val", "best", "system", "other"],
  );
  assert.deepEqual(groups[0].keys, ["train/loss", "train/lr"]);
  assert.deepEqual(groups[1].keys, ["val/bit_acc_mean", "val/psnr", "val/ssim"]);
});

test("pickKeyMetrics / qualityMetricFor", () => {
  assert.equal(qualityMetricFor("audio"), "snr");
  assert.equal(qualityMetricFor("video"), "psnr");
  const picked = pickKeyMetrics({ "val/psnr": 38, "val/bit_acc_mean": 0.9, "val/zzz": 1, step: 10 }, "image", 3);
  assert.deepEqual(
    picked.map((p) => p.key),
    ["val/bit_acc_mean", "val/psnr", "val/zzz"],
  );
});

test("paretoFront keeps non-dominated points (both maximize)", () => {
  const pts = [
    { x: 38, y: 0.9 }, // front
    { x: 40, y: 0.8 }, // front
    { x: 37, y: 0.85 }, // dominated by (38, 0.9)
    { x: 35, y: 0.95 }, // front
    { x: 39, y: 0.7 }, // dominated by (40, 0.8)
  ];
  assert.deepEqual(paretoFront(pts, "maximize", "maximize"), [3, 0, 1]);
  // minimize x instead: lower x is better
  assert.deepEqual(paretoFront(pts, "minimize", "maximize"), [3]);
});

test("constraints and smoothing", () => {
  const c = parseConstraint(">= 38");
  assert.deepEqual(c, { op: ">=", value: 38 });
  assert.equal(satisfies(38.5, c!), true);
  assert.equal(satisfies(37, c!), false);
  assert.equal(parseConstraint("about 38"), null);
  assert.deepEqual(smoothSeries([1, 2, 3], 0), [1, 2, 3]);
  const s = smoothSeries([1, 1, 1, 1], 0.9);
  s.forEach((v) => assert.ok(Math.abs(v - 1) < 1e-9));
});

test("run progress and ETA", () => {
  const progress = { step: 250, total_steps: 1000, phase: "p", phase_index: 0, n_phases: 2 };
  assert.equal(progressFraction(progress), 0.25);
  assert.equal(progressFraction(null), null);
  const now = Date.parse("2026-10-01T13:00:00Z");
  const eta = runEta({ progress, started_at: "2026-10-01T12:00:00Z", status: "running" }, now);
  assert.equal(Math.round(eta!), 3 * 3600);
  assert.equal(runEta({ progress, started_at: "2026-10-01T12:00:00Z", status: "completed" }, now), null);
});

test("draft store: restore on same base, forget on server change, dirty guard", async () => {
  const drafts = await import("../src/lib/drafts.ts");
  drafts.resetDrafts();
  assert.equal(drafts.hasDirtyDrafts(), false);
  // A draft equal to its base is not a draft.
  drafts.rememberDraft("run-notes:a", "same", "same");
  assert.equal(drafts.restoreDraft("run-notes:a", "same"), null);
  // Dirty draft: restored while the server content is unchanged…
  drafts.rememberDraft("run-notes:a", "## Conclusion", "");
  assert.equal(drafts.hasDirtyDrafts(), true);
  assert.deepEqual(drafts.dirtyDraftKeys(), ["run-notes:a"]);
  assert.equal(drafts.restoreDraft("run-notes:a", ""), "## Conclusion");
  // …and forgotten (not restored) once the server content changed.
  assert.equal(drafts.restoreDraft("run-notes:a", "edited elsewhere"), null);
  assert.equal(drafts.hasDirtyDrafts(), false);
  // Saving (draft === base) clears the guard.
  drafts.rememberDraft("file:projects/p/experiments/e.yaml", "x: 1", "x: 0");
  drafts.rememberDraft("file:projects/p/experiments/e.yaml", "x: 1", "x: 1");
  assert.equal(drafts.hasDirtyDrafts(), false);
});

test("pruned HPO trials are labelled « Élagué », not « Arrêté »", () => {
  const pruned = { trial: 3, status: "killed" as const, error: "pruned" };
  assert.equal(isPrunedTrial(pruned), true);
  assert.equal(runStatusLabel(pruned), "Élagué");
  // A killed run outside a study, or a trial stopped for another reason, keeps "Arrêté".
  assert.equal(isPrunedTrial({ trial: null, status: "killed", error: "pruned" }), false);
  assert.equal(isPrunedTrial({ trial: 2, status: "killed", error: "max_hours reached" }), false);
  assert.equal(runStatusLabel({ trial: 2, status: "killed", error: "SIGTERM reçu" }), "Arrêté");
  assert.equal(isPrunedTrial({ trial: 2, status: "completed", error: null }), false);
});

test("phase plan curves: no attack strength without attacks, log scale for loss weights", () => {
  const warmup = {
    name: "warmup",
    start: 0,
    end: 100,
    steps: 100,
    lr: [[0, 0.1], [99, 1]] as [number, number][],
    attack_strength: [[0, 1], [99, 1]] as [number, number][],
    loss_weights: { msg: [[0, 1], [99, 1]] as [number, number][], img: [[0, 10], [99, 300]] as [number, number][] },
    attacks: [],
  };
  const ramp = { ...warmup, name: "ramp", start: 100, end: 200, attack_strength: [[100, 0], [199, 1]] as [number, number][], attacks: ["jpeg"] };
  // The plan reports strength = 1 on the warm-up phase although it has no attack: drawn as 0.
  assert.deepEqual(attackStrengthPoints(warmup), [[0, 0], [99, 0]]);
  assert.deepEqual(attackStrengthPoints(ramp), [[100, 0], [199, 1]]);
  const rows = buildRows([warmup, ramp]);
  const strength = rows.find((r) => r.id === "strength")!;
  // Each phase's series is held up to the phase boundary (100, 200).
  assert.deepEqual(strength.lines[0].points, [[0, 0], [99, 0], [100, 0], [100, 0], [199, 1], [200, 1]]);
  const weights = rows.find((r) => r.id === "weights")!;
  assert.equal(weights.logScale, true); // img (300) vs msg (1): 300× apart
  assert.equal(weights.lines.map((l) => l.name).join(","), "img,msg");
  // Comparable magnitudes keep a linear scale.
  const even = buildRows([{ ...warmup, loss_weights: { a: [[0, 1], [99, 2]], b: [[0, 3], [99, 4]] } }]);
  assert.equal(even.find((r) => r.id === "weights")!.logScale, false);
  // Interpolation used by the hover read-out.
  assert.equal(valueAt([[100, 0], [199, 1]], 100), 0);
  assert.ok(Math.abs(valueAt([[100, 0], [200, 1]], 150)! - 0.5) < 1e-9);
  assert.equal(valueAt([], 5), null);
});

test("system / gradient metric labels and units", () => {
  assert.equal(metricLabel("train/grad_norm"), "Norme du gradient");
  assert.equal(metricLabel("proc_ram_gb"), "RAM du process");
  assert.equal(n(formatMetric("proc_ram_gb", 0.3257)), "0,3 Go");
  assert.deepEqual(groupMetricKeys(["proc_ram_gb", "train/loss"]).map((g) => g.id), ["train", "system"]);
});
