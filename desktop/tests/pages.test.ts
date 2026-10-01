/**
 * Unit tests of the pure helpers behind the project / leaderboard / HPO / budget pages
 * (src/components/analysis.ts, src/components/yamlData.ts). Run with `npm test`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AUTO_QUALITY,
  bareMetric,
  bestUnderConstraint,
  burnup,
  busiestBenchmark,
  defaultQualityMetric,
  cumulativeBest,
  differingParams,
  experimentFromPath,
  intersectKeys,
  leaderValue,
  monthRange,
  nextVersionName,
  objectiveDirection,
  projectedSpend,
  schemaParams,
  searchSpaceRows,
  sliderRange,
  toIdentifier,
} from "../src/components/analysis.ts";
import { parseYaml, parseYamlObject, resolvePlainScalar, setTopLevelScalar } from "../src/components/yamlData.ts";

// ---------------------------------------------------------------------------
// Leaderboard objectives
// ---------------------------------------------------------------------------

test("leaderValue reads summary then quality, with or without val/ prefix", () => {
  const row = { summary: { bit_acc_mean: 0.91, psnr: 38.2 }, quality: { ssim: 0.97 } };
  assert.equal(bareMetric("val/psnr"), "psnr");
  assert.equal(leaderValue(row, "psnr"), 38.2);
  assert.equal(leaderValue(row, "val/bit_acc_mean"), 0.91);
  assert.equal(leaderValue(row, "ssim"), 0.97);
  assert.equal(leaderValue(row, "lpips"), null);
});

test("objectiveDirection prefers the project objective, then known metrics", () => {
  assert.equal(objectiveDirection("psnr", { metric: "val/psnr", direction: "minimize" }), "minimize");
  assert.equal(objectiveDirection("lpips"), "minimize");
  assert.equal(objectiveDirection("unknown_metric"), "maximize");
});

test("bestUnderConstraint picks the most robust feasible point", () => {
  const items = [
    { item: "a", quality: 40, robustness: 0.8 },
    { item: "b", quality: 37, robustness: 0.95 },
    { item: "c", quality: 38.5, robustness: 0.9 },
    { item: "d", quality: null, robustness: 0.99 },
    { item: "e", quality: 39, robustness: 0.9 },
  ];
  const best = bestUnderConstraint(items, { op: ">=", value: 38 }, "maximize", "maximize");
  assert.equal(best?.item, "e"); // tie on robustness broken by quality
  assert.equal(bestUnderConstraint(items, null, "maximize", "maximize")?.item, "b");
  assert.equal(bestUnderConstraint(items, { op: ">=", value: 50 }, "maximize", "maximize"), null);
});

test("sliderRange covers values and the constraint with a round step", () => {
  const r = sliderRange([36.2, 41.7], 38);
  assert.ok(r.min <= 36.2 && r.max >= 41.7);
  assert.ok(r.step > 0 && r.step <= 0.05);
  const single = sliderRange([], null);
  assert.deepEqual(single, { min: 0, max: 1, step: 0.01 });
  const flat = sliderRange([5, 5]);
  assert.ok(flat.min < 5 && flat.max > 5);
});

// ---------------------------------------------------------------------------
// HPO & compare
// ---------------------------------------------------------------------------

test("cumulativeBest keeps the running optimum", () => {
  assert.deepEqual(cumulativeBest([0.5, null, 0.7, 0.6, 0.9], "maximize"), [0.5, 0.5, 0.7, 0.7, 0.9]);
  assert.deepEqual(cumulativeBest([null, 3, 1, 2], "minimize"), [null, 3, 1, 1]);
});

test("searchSpaceRows describes float/int/categorical parameters", () => {
  const rows = searchSpaceRows({
    "optimizer.lr": { type: "float", low: 1e-5, high: 1e-2, log: true },
    "phases.0.steps": { type: "int", low: 500, high: 3000, step: 500 },
    "model.params.channels": { type: "categorical", choices: [32, 64] },
  });
  assert.equal(rows.length, 3);
  assert.equal(rows[0].type, "réel");
  assert.match(rows[0].detail, /log/);
  assert.equal(rows[1].type, "entier");
  assert.match(rows[1].detail, /pas 500/);
  assert.equal(rows[2].type, "catégoriel");
  assert.equal(rows[2].bounds, "2 choix");
  assert.deepEqual(searchSpaceRows(null), []);
});

test("differingParams and intersectKeys", () => {
  const runs = [
    { params: { "optimizer.lr": 0.001, seed: 0, "model.name": "hidden" } },
    { params: { "optimizer.lr": 0.0003, seed: 0, "model.name": "hidden", extra: 1 } },
  ];
  assert.deepEqual(differingParams(runs), ["extra", "optimizer.lr"]);
  assert.deepEqual(intersectKeys([["a", "b", "c"], ["c", "a"], ["a", "c", "d"]]), ["a", "c"]);
  assert.deepEqual(intersectKeys([]), []);
});

// ---------------------------------------------------------------------------
// Budget
// ---------------------------------------------------------------------------

test("monthRange enumerates months across years", () => {
  assert.deepEqual(monthRange("2026-11-15", "2027-02-01"), ["2026-11", "2026-12", "2027-01", "2027-02"]);
  assert.deepEqual(monthRange("bad", "2027-01"), []);
});

test("burnup cumulates spend and interpolates the linear budget", () => {
  const costs = {
    by_month: [
      { month: "2026-10", eur: 100, gpu_hours: 10 },
      { month: "2026-11", eur: 50, gpu_hours: 5 },
    ],
    budget: { total_eur: 1200, start: "2026-10-01", end: "2027-09-30" },
  };
  const now = Date.UTC(2026, 10, 20); // 20 Nov 2026
  const b = burnup(costs, now);
  assert.equal(b.months.length, 12);
  assert.deepEqual(b.spent.slice(0, 3), [100, 150, null]);
  assert.ok(b.linear[0]! > 90 && b.linear[0]! < 110); // ~1/12 of the budget after October
  assert.equal(b.linear[11], 1200);
});

test("projectedSpend extrapolates the last 30 days to the end of the period", () => {
  const now = Date.UTC(2026, 11, 1);
  const day = 86_400_000;
  const costs = {
    spent_eur: 500,
    budget: { total_eur: 40000, start: "2026-10-01", end: new Date(now + 100 * day).toISOString().slice(0, 10) },
    items: [
      { kind: "run" as const, id: "r1", project: "p", host: "h", started_at: new Date(now - 10 * day).toISOString(), hours: 1, eur: 300 },
      { kind: "run" as const, id: "r0", project: "p", host: "h", started_at: new Date(now - 60 * day).toISOString(), hours: 1, eur: 200 },
    ],
  };
  const p = projectedSpend(costs, now);
  assert.equal(p.dailyRate, 10);
  assert.ok(Math.abs((p.projected ?? 0) - 1500) < 1);
  assert.equal(projectedSpend({ ...costs, budget: { ...costs.budget, end: null } }, now).projected, null);
});

// ---------------------------------------------------------------------------
// Registry parameters & names
// ---------------------------------------------------------------------------

test("schemaParams reads pydantic JSON Schema properties", () => {
  const rows = schemaParams({
    type: "object",
    properties: {
      channels: { type: "integer", default: 64, minimum: 1, title: "Channels" },
      strength: { type: "number", default: 1.0, exclusiveMinimum: 0, description: "Residual amplitude" },
      aggregate: { enum: ["mean", "median"], default: "mean" },
      image_model: { anyOf: [{ type: "string" }, { type: "null" }], default: null },
      root: { type: "string" },
    },
    required: ["root"],
  });
  const byName = Object.fromEntries(rows.map((r) => [r.name, r]));
  assert.equal(byName.channels.type, "entier");
  assert.equal(byName.channels.default, "64");
  assert.match(byName.channels.bounds, /^\[1/);
  assert.equal(byName.strength.description, "Residual amplitude");
  assert.match(byName.strength.bounds, /^\]0/);
  assert.equal(byName.aggregate.type, '"mean" | "median"');
  assert.equal(byName.image_model.type, "texte | null");
  assert.equal(byName.root.required, true);
  assert.equal(byName.root.default, "requis");
  assert.deepEqual(schemaParams(null), []);
});

test("nextVersionName bumps the suffix and skips existing names", () => {
  assert.deepEqual(nextVersionName("image_v1", 1, ["image_v1"]), { name: "image_v2", version: 2 });
  assert.deepEqual(nextVersionName("image_v1", 1, ["image_v1", "image_v2"]), { name: "image_v3", version: 3 });
  assert.deepEqual(nextVersionName("custom", 1, []), { name: "custom_v2", version: 2 });
});

test("toIdentifier and experimentFromPath", () => {
  assert.equal(toIdentifier("Mon Essai été"), "mon_essai_ete");
  assert.equal(toIdentifier("--x--", "-"), "x");
  assert.deepEqual(experimentFromPath("projects/image-hidden/experiments/baseline.yaml"), {
    project: "image-hidden",
    experiment: "baseline",
  });
  assert.equal(experimentFromPath("benchmarks/image_v1.yaml"), null);
});

// ---------------------------------------------------------------------------
// YAML
// ---------------------------------------------------------------------------

test("resolvePlainScalar follows the safe loader", () => {
  assert.equal(resolvePlainScalar("~"), null);
  assert.equal(resolvePlainScalar("true"), true);
  assert.equal(resolvePlainScalar("off"), false);
  assert.equal(resolvePlainScalar("42"), 42);
  assert.equal(resolvePlainScalar("1.0e-3"), 0.001);
  assert.equal(resolvePlainScalar("val/psnr"), "val/psnr");
  assert.equal(resolvePlainScalar(">= 38"), ">= 38");
});

test("parseYaml handles block/flow collections, quotes, block scalars and comments", () => {
  const doc = parseYaml(`# comment
name: lr_weights            # trailing comment
experiment: baseline
description: >-
  folded line one
  line two
note: |
  a
  b
quoted: 'it''s'
dq: "tab\\tend"
empty:
objectives:
  - {metric: val/psnr, direction: maximize}
  - metric: val/bit_acc_mean
    direction: maximize
space:
  optimizer.lr: {type: float, low: 1.0e-5, high: 1.0e-2, log: true}
  model.params.channels: {type: categorical, choices: [32, 64]}
budget: {max_trials: 40, max_hours: 12, max_eur: 200, parallel: 2}
anchor: &a {x: 1}
alias: *a
`);
  assert.deepEqual(doc, {
    name: "lr_weights",
    experiment: "baseline",
    description: "folded line one line two",
    note: "a\nb\n",
    quoted: "it's",
    dq: "tab\tend",
    empty: null,
    objectives: [
      { metric: "val/psnr", direction: "maximize" },
      { metric: "val/bit_acc_mean", direction: "maximize" },
    ],
    space: {
      "optimizer.lr": { type: "float", low: 1e-5, high: 1e-2, log: true },
      "model.params.channels": { type: "categorical", choices: [32, 64] },
    },
    budget: { max_trials: 40, max_hours: 12, max_eur: 200, parallel: 2 },
    anchor: { x: 1 },
    alias: { x: 1 },
  });
  assert.equal(parseYaml(""), null);
  assert.equal(parseYamlObject("- a\n- b"), null);
});

test("parseYaml never throws on broken YAML", () => {
  assert.doesNotThrow(() => parseYaml("a: [1, 2\nb: {c"));
  const partial = parseYamlObject("a: 1\nb: [\n");
  assert.equal(partial?.a, 1);
});

test("setTopLevelScalar replaces a top-level value and keeps its comment", () => {
  const src = "# header\nname: image_v1   # keep me\nversion: 1\nnested:\n  name: inner\n";
  const out = setTopLevelScalar(setTopLevelScalar(src, "name", "image_v2"), "version", "2");
  assert.equal(out, "# header\nname: image_v2   # keep me\nversion: 2\nnested:\n  name: inner\n");
  assert.equal(setTopLevelScalar("a: 1\n", "b", "2"), "b: 2\na: 1\n");
});

// ---------------------------------------------------------------------------
// Cross-project leaderboard defaults (modality-aware quality axis)
// ---------------------------------------------------------------------------

test("leaderValue: virtual quality axis picks PSNR, else SNR", () => {
  const image = { summary: { psnr: 38, bit_acc_mean: 0.9 }, quality: { psnr: 38, ssim: 0.97 } };
  const audio = { summary: { snr: 26, bit_acc_mean: 0.95 }, quality: { snr: 26, si_snr: 27 } };
  assert.equal(leaderValue(image, AUTO_QUALITY), 38);
  assert.equal(leaderValue(audio, AUTO_QUALITY), 26);
  assert.equal(leaderValue({ summary: {}, quality: { ssim: 0.9 } }, AUTO_QUALITY), null);
});

test("defaultQualityMetric follows the rows' modality", () => {
  const image = { quality: { psnr: 38, ssim: 0.97 } };
  const audio = { quality: { snr: 26 } };
  assert.equal(defaultQualityMetric([image, image], ["psnr", "ssim"]), "psnr");
  assert.equal(defaultQualityMetric([audio], ["si_snr", "snr"]), "snr");
  assert.equal(defaultQualityMetric([image, audio], [AUTO_QUALITY, "psnr", "snr"]), AUTO_QUALITY);
  assert.equal(defaultQualityMetric([{ quality: { lpips: 0.1 } }], ["lpips"]), "lpips");
  assert.equal(defaultQualityMetric([], []), "psnr");
});

test("busiestBenchmark prefers the benchmark with the most rows", () => {
  const rows = [{ benchmark: "audio_v1" }, { benchmark: "image_v1" }, { benchmark: "image_v1" }, { benchmark: null }];
  assert.equal(busiestBenchmark(rows, ["audio_v1", "image_v1", "video_v1"]), "image_v1");
  assert.equal(busiestBenchmark([], ["audio_v1", "image_v1"]), "audio_v1");
  assert.equal(busiestBenchmark(rows, []), "");
});
