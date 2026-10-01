/**
 * Unit tests of the hosted-mode helpers (src/lib/hosted.ts) and the URL normalisation of
 * the login screen (src/lib/api.ts is browser-only: the function is re-implemented here
 * through the same contract in electron/main.cjs). Run with `npm test`.
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  decommissionBlockers,
  describeFileSave,
  describeRetrieveResult,
  gitRefForLaunch,
  gitRevisionLabel,
  hasRetrievalIssue,
  retrievalNotice,
} from "../src/lib/hosted.ts";

/** Normalise the narrow/no-break spaces used by Intl fr-FR. */
const n = (s: string) => s.replace(/[  ]/g, " ");

const GIT = { available: true, hosted: true, branch: "main", sha: "3f2a9c1d7e5b4a6c8d9e0f1a2b3c4d5e6f7a8b9c", short_sha: "3f2a9c1", dirty: false, remote: null, ahead: 0, behind: 0, last_pull_at: null, last_pull_error: null };

test("gitRevisionLabel: branch @ short sha", () => {
  assert.equal(gitRevisionLabel(GIT), "main @ 3f2a9c1");
  assert.equal(gitRevisionLabel({ branch: null, sha: "abcdef0123", short_sha: null }), "abcdef0");
  assert.equal(gitRevisionLabel({ branch: "dev", sha: null, short_sha: null }), "dev");
  assert.equal(gitRevisionLabel(null), "");
});

test("gitRefForLaunch: unchanged field pins the sha in hosted mode, sends nothing otherwise", () => {
  const label = gitRevisionLabel(GIT);
  assert.equal(gitRefForLaunch(label, label, GIT), GIT.sha);
  assert.equal(gitRefForLaunch("", label, GIT), GIT.sha);
  assert.equal(gitRefForLaunch(label, label, { ...GIT, hosted: false }), undefined);
  assert.equal(gitRefForLaunch("  ", label, { ...GIT, hosted: false }), undefined);
});

test("gitRefForLaunch: explicit branch or sha, 'branch @ sha' keeps the sha", () => {
  const label = gitRevisionLabel(GIT);
  assert.equal(gitRefForLaunch("feature/res256", label, GIT), "feature/res256");
  assert.equal(gitRefForLaunch(" 9b1e2f3 ", label, GIT), "9b1e2f3");
  assert.equal(gitRefForLaunch("feature/x @ 9b1e2f3a", label, GIT), "9b1e2f3a");
  assert.equal(gitRefForLaunch("main @ notasha", label, GIT), "main @ notasha");
});

test("describeFileSave: plain, committed + pushed, committed only, not committed", () => {
  assert.deepEqual(describeFileSave(null, "Enregistré"), { tone: "success", title: "Enregistré", description: null });
  assert.deepEqual(describeFileSave(undefined, "Notes enregistrées", "x"), { tone: "success", title: "Notes enregistrées", description: "x" });
  const ok = describeFileSave({ committed: "abc1234def", pushed: true, error: null }, "Enregistré");
  assert.equal(ok.tone, "success");
  assert.equal(ok.title, "Enregistré · commité abc1234 · poussé");
  const unpushed = describeFileSave({ committed: "abc1234def", pushed: false, error: "rejected" }, "Enregistré");
  assert.equal(unpushed.tone, "warning");
  assert.equal(unpushed.title, "Enregistré · commité abc1234 mais non poussé");
  assert.match(unpushed.description ?? "", /rejected/);
  const none = describeFileSave({ committed: null, pushed: false, error: "git: not a repository" }, "Enregistré");
  assert.equal(none.tone, "warning");
  assert.equal(none.title, "Enregistré, mais non commité");
  assert.equal(none.description, "git: not a repository");
});

test("retrievalNotice / hasRetrievalIssue", () => {
  assert.equal(retrievalNotice(null), null);
  assert.equal(retrievalNotice({ status: "complete", checkpoints: 3, bytes: 1, missing: [] }), null);
  assert.equal(retrievalNotice({ status: "n/a", checkpoints: 0, bytes: 0, missing: [] }), null);
  assert.match(retrievalNotice({ status: "partial", checkpoints: 3, bytes: 1, missing: ["a.pt", "b.pt"] }) ?? "", /^Rapatriement partiel : 2 checkpoints manquants/);
  assert.equal(retrievalNotice({ status: "failed", checkpoints: 0, bytes: 0, missing: [] }), "Rapatriement des checkpoints échoué");
  assert.equal(hasRetrievalIssue({ kind: "train", retrieval: { status: "partial", checkpoints: 1, bytes: 1, missing: ["a"] } }), true);
  assert.equal(hasRetrievalIssue({ kind: "train", retrieval: { status: "complete", checkpoints: 1, bytes: 1, missing: [] } }), false);
  assert.equal(hasRetrievalIssue({ kind: "retrieve", retrieval: { status: "failed", checkpoints: 0, bytes: 0, missing: [] } }), false);
  assert.equal(hasRetrievalIssue({ kind: "eval", retrieval: null }), false);
});

test("describeRetrieveResult", () => {
  assert.equal(describeRetrieveResult(null), "");
  assert.equal(n(describeRetrieveResult({ runs: 12, studies: 1, checkpoints: 37, bytes: 18.2 * 1024 ** 3, missing: [] })), "12 runs · 1 étude · 37 checkpoints · 18 Go");
  assert.match(n(describeRetrieveResult({ runs: 1, studies: 0, checkpoints: 2, bytes: 1024, missing: ["x", "y"] })), /2 manquants$/);
});

test("decommissionBlockers mirrors the server guards", () => {
  assert.deepEqual(decommissionBlockers({ kind: "ssh", unretrieved_checkpoints: 0, unretrieved_bytes: 0, decommissioned_at: null }, 0), []);
  assert.deepEqual(decommissionBlockers({ kind: "ssh", unretrieved_checkpoints: null, unretrieved_bytes: null, decommissioned_at: null }, 0), []);
  const b = decommissionBlockers({ kind: "ssh", unretrieved_checkpoints: 3, unretrieved_bytes: 2.4 * 1024 ** 3, decommissioned_at: null }, 1);
  assert.equal(b.length, 2);
  assert.match(n(b[0]), /^1 job actif/);
  assert.match(n(b[1]), /^3 checkpoints non rapatriés \(2,4 Go\) — lancez le rapatriement\./);
  assert.match(decommissionBlockers({ kind: "local", decommissioned_at: null }, 0)[0], /locale/);
  assert.match(decommissionBlockers({ kind: "ssh", decommissioned_at: "2026-09-28T00:00:00Z" }, 0)[0], /déjà retirée/);
});
