#!/usr/bin/env node
/**
 * Headless rendering check of the built frontend against a running backend.
 *
 *   npx electron scripts/render-check.cjs [--base http://127.0.0.1:8799] [--token test]
 *                                         [--dark] [--launch] [--dialog] [--login] [--hosts]
 *                                         [--routes /,/runs] [--width 1000] [--height 640]
 *
 * For every route, an invisible offscreen BrowserWindow (1360×880 by default;
 * `--width 1000` checks the minimum window width, captures get a `_w1000` suffix) loads
 * `<base>/?token=<token>#<route>`, waits ~2.5 s, records console errors /
 * warnings and uncaught exceptions, and saves a PNG in desktop/.selftest/.
 * `--dark` renders a second pass with `data-theme="dark"` on key routes.
 * `--launch` opens the launch dialog, captures it, submits a local smoke
 * training and checks that the run page appears. `--dialog` only opens and captures the
 * dialog (no job: usable against scripts/mock-api.cjs). `--login` captures the login screen
 * (§12.6) without a token, then with a refused token. `--hosts` exercises the Machines page
 * (§12.4): inventory panel, retrieve job, decommission dialog (against the mock).
 *
 * Exit code: 0 when no error was recorded, 1 otherwise. A JSON report is
 * written to desktop/.selftest/report.json.
 */
"use strict";

// The about:blank page loaded between routes has no CSP: silence Electron's dev-only warning.
process.env.ELECTRON_DISABLE_SECURITY_WARNINGS = "true";

const { app, BrowserWindow, nativeTheme } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const args = process.argv.slice(2);
const opt = (name, def) => {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : def;
};
const flag = (name) => args.includes(name);

const BASE = (process.env.WMLAB_API_BASE || opt("--base", "http://127.0.0.1:8799")).replace(/\/+$/, "");
const TOKEN = process.env.WMLAB_API_TOKEN || opt("--token", "test");
const OUT_DIR = path.resolve(__dirname, "..", ".selftest");
const SETTLE_MS = Number(opt("--settle", "2500"));
const WIDTH = Number(opt("--width", "1360"));
const HEIGHT = Number(opt("--height", "880"));
const HEADERS = { "X-WMLab-Token": TOKEN, "Content-Type": "application/json" };

const DARK_ROUTES = ["/", "/runs/{run}", "/projects/image-hidden", "/studies/{study}", "/leaderboard"];

const DEFAULT_ROUTES = [
  "/",
  "/runs",
  "/runs?trials=1",
  "/runs/{run}",
  ...["metrics", "system", "checkpoints", "evals", "samples", "logs", "config", "env", "notes"].map((t) => `/runs/{run}?tab=${t}`),
  "/projects/image-hidden",
  ...["experiments", "runs", "hpo", "leaderboard"].map((t) => `/projects/image-hidden?tab=${t}`),
  "/projects/image-hidden/experiments/baseline",
  "/projects/image-hidden/hpo/lr_weights",
  "/studies/{study}",
  "/studies/{study}?tab=notes",
  "/compare?runs={run},{run2}",
  "/leaderboard",
  "/leaderboard?source=val",
  "/library",
  "/datasets",
  "/benchmarks",
  "/hosts",
  "/budget",
  "/settings",
];

/** Console entries and exceptions of the page currently loaded. */
const issues = [];
let currentLabel = "";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(method, route, body) {
  const res = await fetch(`${BASE}/api${route}`, { method, headers: HEADERS, body: body ? JSON.stringify(body) : undefined });
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!res.ok) throw new Error(`${method} ${route} → ${res.status} ${typeof data === "string" ? data : JSON.stringify(data)}`);
  return data;
}

/** Substitute {run}, {run2}, {study} with demo ids read from the backend. */
async function resolveRoutes(routes) {
  // Prefer real (non-smoke) runs: they have several checkpoints, evaluations and samples.
  const all = await api("GET", "/runs?limit=50");
  const runs = [...all.filter((r) => !(r.tags || []).includes("smoke")), ...all.filter((r) => (r.tags || []).includes("smoke"))];
  const studies = await api("GET", "/studies");
  const run = runs[0] ? runs[0].run_id : "";
  const run2 = runs[1] ? runs[1].run_id : run;
  const study = studies[0] ? studies[0].study_id : "";
  return routes
    .filter((r) => !(r.includes("{study}") && !study) && !(r.includes("{run") && !run))
    .map((r) => r.replace(/\{run2\}/g, run2).replace(/\{run\}/g, run).replace(/\{study\}/g, study));
}

function fileName(label) {
  const suffix = WIDTH !== 1360 ? `_w${WIDTH}` : "";
  return `${label.replace(/[^a-z0-9]+/gi, "_").replace(/^_+|_+$/g, "") || "root"}${suffix}.png`;
}

function createWindow() {
  const win = new BrowserWindow({
    width: WIDTH,
    height: HEIGHT,
    show: false,
    // In-memory session (no "persist:" prefix): the backend serves dist/ without Cache-Control,
    // so a persisted HTTP cache could keep an index.html from a previous build whose chunk
    // hashes no longer exist ("Failed to fetch dynamically imported module").
    webPreferences: { offscreen: true, contextIsolation: true, sandbox: true, nodeIntegration: false, partition: `render-check-${process.pid}` },
  });
  win.webContents.setFrameRate(10);
  win.webContents.on("console-message", (event) => {
    const level = typeof event.level === "string" ? event.level : ["debug", "info", "warning", "error"][event.level] || String(event.level);
    if (level !== "error" && level !== "warning") return;
    const text = `${event.message}`.trim();
    // Chromium's own noise (autofill/devtools) is not an app error.
    if (/Autofill\.|DevTools/.test(text)) return;
    issues.push({ route: currentLabel, level, text: text.slice(0, 600), source: `${event.sourceId || ""}:${event.lineNumber || ""}` });
  });
  win.webContents.on("render-process-gone", (_e, details) => {
    issues.push({ route: currentLabel, level: "error", text: `render process gone (${details.reason})` });
  });
  win.webContents.on("did-fail-load", (_e, code, desc, url, isMainFrame) => {
    if (isMainFrame && code !== -3) issues.push({ route: currentLabel, level: "error", text: `did-fail-load ${code} ${desc} ${url}` });
  });
  return win;
}

/** Install a window.onerror / unhandledrejection hook that stores exceptions in a global. */
const EXCEPTION_HOOK = `
  (() => {
    if (window.__wmlabErrors) return;
    window.__wmlabErrors = [];
    window.addEventListener('error', (e) => window.__wmlabErrors.push('error: ' + (e.message || String(e))));
    window.addEventListener('unhandledrejection', (e) => window.__wmlabErrors.push('unhandledrejection: ' + (e.reason && (e.reason.stack || e.reason.message) || String(e.reason))));
  })();`;

async function load(win, route, { dark = false } = {}) {
  currentLabel = `${dark ? "dark" : "light"} ${route}`;
  const theme = dark ? "dark" : "light";
  // Both the OS preference (prefers-color-scheme) and the app override are forced,
  // so the pass does not depend on the Mac's current appearance.
  nativeTheme.themeSource = theme;
  await win.loadURL("about:blank");
  const url = `${BASE}/?token=${encodeURIComponent(TOKEN)}#${route}`;
  await win.loadURL(url);
  await win.webContents.executeJavaScript(EXCEPTION_HOOK, true);
  await win.webContents.executeJavaScript(
    `document.documentElement.setAttribute('data-theme', ${JSON.stringify(theme)}); try { localStorage.setItem('wmlab.theme', ${JSON.stringify(theme)}); } catch {}`,
    true,
  );
  await sleep(SETTLE_MS);
}

async function collectPageErrors(win, { expectReady = true } = {}) {
  const errs = await win.webContents.executeJavaScript("window.__wmlabErrors || []", true).catch(() => []);
  for (const e of errs) issues.push({ route: currentLabel, level: "error", text: `exception: ${String(e).slice(0, 600)}` });
  const ready = await win.webContents.executeJavaScript("Boolean(document.querySelector('[data-app-ready]'))", true).catch(() => false);
  if (expectReady && !ready) issues.push({ route: currentLabel, level: "error", text: "[data-app-ready] absent" });
  if (!expectReady && ready) issues.push({ route: currentLabel, level: "error", text: "[data-app-ready] présent alors que l'écran de connexion était attendu" });
  const boundary = await win.webContents
    .executeJavaScript("document.body.innerText.includes('Cette page a rencontré une erreur')", true)
    .catch(() => false);
  if (boundary) issues.push({ route: currentLabel, level: "error", text: "page error boundary displayed" });
}

async function capture(win, label) {
  const image = await win.webContents.capturePage();
  const file = path.join(OUT_DIR, fileName(label));
  fs.writeFileSync(file, image.toPNG());
  return file;
}

/** Text of the page (for quick assertions). */
const pageText = (win) => win.webContents.executeJavaScript("document.body.innerText", true);

/** Click the first element whose text matches (buttons / links). */
const CLICK_TEXT = (selector, text) => `
  (() => {
    const els = [...document.querySelectorAll(${JSON.stringify(selector)})];
    const el = els.find((e) => (e.innerText || e.textContent || '').trim().includes(${JSON.stringify(text)}));
    if (!el) return false;
    el.click();
    return true;
  })()`;

/** Open the launch dialog and capture it (no job submitted). */
async function dialogScenario(win, captured) {
  currentLabel = "launch dialog (capture)";
  await load(win, "/");
  const opened = await win.webContents.executeJavaScript(CLICK_TEXT("button", "Nouveau lancement"), true);
  if (!opened) {
    issues.push({ route: currentLabel, level: "error", text: "bouton « Nouveau lancement » introuvable" });
    return;
  }
  await sleep(3000);
  const text = await pageText(win);
  for (const expected of ["Projet", "Machine", "Expérience", "Révision git", "Durée estimée"]) {
    if (!text.includes(expected)) issues.push({ route: currentLabel, level: "error", text: `dialogue : « ${expected} » absent` });
  }
  captured.push(await capture(win, "launch dialog"));
  await collectPageErrors(win);
}

/**
 * Login screen (§12.6): the page loaded without a token must show the token form (and no
 * app shell); a refused token must bring the form back with an explanation.
 */
async function loginScenario(win, captured) {
  currentLabel = "login (sans jeton)";
  nativeTheme.themeSource = "light";
  await win.loadURL("about:blank");
  // Same origin as the app: forget any token memorised by the previous routes.
  await win.loadURL(`${BASE}/#/`);
  await win.webContents.executeJavaScript("try { localStorage.removeItem('wmlab.token'); localStorage.removeItem('wmlab.apiBase'); sessionStorage.clear(); } catch {}", true);
  await win.loadURL("about:blank");
  await win.loadURL(`${BASE}/#/`);
  await win.webContents.executeJavaScript(EXCEPTION_HOOK, true);
  await win.webContents.executeJavaScript("document.documentElement.setAttribute('data-theme', 'light')", true);
  await sleep(SETTLE_MS);
  let text = await pageText(win);
  for (const expected of ["Jeton", "Se connecter"]) {
    if (!text.includes(expected)) issues.push({ route: currentLabel, level: "error", text: `écran de connexion : « ${expected} » absent` });
  }
  captured.push(await capture(win, "login"));
  await collectPageErrors(win, { expectReady: false });

  currentLabel = "login (jeton refusé)";
  await win.loadURL("about:blank");
  await win.loadURL(`${BASE}/?token=refuse-moi#/`);
  await win.webContents.executeJavaScript(EXCEPTION_HOOK, true);
  await sleep(SETTLE_MS);
  text = await pageText(win);
  if (!/refus/i.test(text)) issues.push({ route: currentLabel, level: "error", text: "jeton refusé : aucune explication affichée" });
  captured.push(await capture(win, "login refused"));
  await collectPageErrors(win, { expectReady: false });
  // Leave the origin clean for a following pass.
  await win.webContents.executeJavaScript("try { localStorage.removeItem('wmlab.token'); } catch {}", true);
}

/** Click a button by text inside the host card whose title is `host`. */
const CLICK_IN_CARD = (host, text) => `
  (() => {
    const cards = [...document.querySelectorAll('main .rounded-xl')];
    const card = cards.find((c) => c.querySelector('.font-mono') && c.querySelector('.font-mono').textContent.trim() === ${JSON.stringify(host)});
    if (!card) return 'card missing';
    const btn = [...card.querySelectorAll('button')].find((b) => (b.innerText || '').trim().startsWith(${JSON.stringify(text)}));
    if (!btn) return 'button missing';
    if (btn.disabled) return 'disabled';
    btn.click();
    return 'ok';
  })()`;

/** Machines page (§12.4): inventory, retrieve job, decommission dialog. Needs the mock (or a host named scw-l40s / scw-h100). */
async function hostsScenario(win, captured) {
  currentLabel = "hosts inventory";
  // The scenario mutates the mock (retrieve job): start from its initial state when it offers a reset.
  await api("POST", "/mock/reset").catch(() => undefined);
  await load(win, "/hosts");
  let r = await win.webContents.executeJavaScript(CLICK_IN_CARD("scw-l40s", "Inventaire"), true);
  if (r !== "ok") issues.push({ route: currentLabel, level: "error", text: `bouton « Inventaire » : ${r}` });
  await sleep(1500);
  let text = await pageText(win);
  for (const expected of ["checkpoints non rapatriés", "Non rapatriés", "Instance Scaleway", "non indexé"]) {
    if (!text.includes(expected)) issues.push({ route: currentLabel, level: "error", text: `inventaire : « ${expected} » absent` });
  }
  captured.push(await capture(win, "hosts inventory"));
  await collectPageErrors(win);

  currentLabel = "hosts retrieve";
  r = await win.webContents.executeJavaScript(CLICK_IN_CARD("scw-l40s", "Rapatrier"), true);
  if (r !== "ok") issues.push({ route: currentLabel, level: "error", text: `bouton « Rapatrier les checkpoints » : ${r}` });
  await sleep(1500);
  text = await pageText(win);
  if (!text.includes("Rapatriement en cours")) issues.push({ route: currentLabel, level: "error", text: "progression du rapatriement absente" });
  captured.push(await capture(win, "hosts retrieving"));
  // The mock completes the job after ~5 s; the job list polls every 5 s.
  await sleep(9000);
  text = await pageText(win);
  if (!text.includes("Dernier rapatriement : terminé")) issues.push({ route: currentLabel, level: "error", text: "résumé du rapatriement absent après la fin du job" });
  captured.push(await capture(win, "hosts retrieved"));
  await collectPageErrors(win);

  currentLabel = "hosts decommission dialog";
  r = await win.webContents.executeJavaScript(CLICK_IN_CARD("scw-h100", "Retirer la machine"), true);
  if (r !== "ok") issues.push({ route: currentLabel, level: "error", text: `bouton « Retirer la machine » : ${r}` });
  await sleep(1500);
  text = await pageText(win);
  for (const expected of ["Retirer scw-h100", "Tout est sur le serveur", "Supprimer aussi l'instance Scaleway"]) {
    if (!text.includes(expected)) issues.push({ route: currentLabel, level: "error", text: `dialogue de retrait : « ${expected} » absent` });
  }
  captured.push(await capture(win, "hosts decommission dialog"));
  await win.webContents.executeJavaScript(`(() => { const b = [...document.querySelectorAll('[role="dialog"] button')].find((e) => e.innerText.trim() === 'Annuler'); if (b) b.click(); })()`, true);
  await sleep(300);
  await collectPageErrors(win);
}

/** Open the launch dialog, capture it, submit a smoke training and check the run page. */
async function launchScenario(win, captured) {
  currentLabel = "launch dialog";
  await load(win, "/");
  const opened = await win.webContents.executeJavaScript(CLICK_TEXT("button", "Nouveau lancement"), true);
  if (!opened) {
    issues.push({ route: currentLabel, level: "error", text: "bouton « Nouveau lancement » introuvable" });
    return;
  }
  await sleep(3000);
  const text = await pageText(win);
  for (const expected of ["Projet", "Machine", "Expérience", "Steps", "Durée estimée", "Coût estimé"]) {
    if (!text.includes(expected)) issues.push({ route: currentLabel, level: "error", text: `dialogue : « ${expected} » absent` });
  }
  const estimate = await win.webContents.executeJavaScript(
    `(() => { const t = document.body.innerText; const m = /Steps\\s*\\n\\s*([^\\n]+)/.exec(t); return m ? m[1] : null; })()`,
    true,
  );
  if (!estimate || estimate.trim() === "—") issues.push({ route: currentLabel, level: "error", text: `estimation non affichée (${estimate})` });
  captured.push(await capture(win, "launch dialog"));

  // Smoke mode on, then launch (⌘↵ equivalent: the footer button).
  const smokeOn = await win.webContents.executeJavaScript(
    `(() => { const sw = [...document.querySelectorAll('[role="switch"]')].find((s) => (s.closest('label, div')?.innerText || '').includes('Mode smoke')); if (!sw) return false; if (sw.getAttribute('aria-checked') !== 'true') sw.click(); return true; })()`,
    true,
  );
  if (!smokeOn) issues.push({ route: currentLabel, level: "error", text: "interrupteur « Mode smoke » introuvable" });
  await sleep(300);
  const launched = await win.webContents.executeJavaScript(
    `(() => { const btns = [...document.querySelectorAll('[role="dialog"] button')]; const b = btns.find((e) => e.innerText.trim() === 'Lancer'); if (!b) return false; b.click(); return true; })()`,
    true,
  );
  if (!launched) {
    issues.push({ route: currentLabel, level: "error", text: "bouton « Lancer » introuvable" });
    return;
  }
  // Wait for the navigation to /runs/<id> and for the run page to show progress.
  let runId = null;
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    const hash = await win.webContents.executeJavaScript("location.hash", true);
    const m = /^#\/runs\/([^?]+)/.exec(hash);
    if (m) {
      runId = decodeURIComponent(m[1]);
      break;
    }
  }
  currentLabel = "launch smoke run";
  if (!runId) {
    const err = await win.webContents
      .executeJavaScript("(document.querySelector('[role=\"dialog\"]')?.innerText || '').slice(0, 400)", true)
      .catch(() => "");
    issues.push({ route: currentLabel, level: "error", text: `pas de navigation vers le run après le lancement (${err.replace(/\n/g, " | ")})` });
    return;
  }
  await sleep(4000);
  captured.push(await capture(win, "launch smoke running"));
  // Wait for completion (smoke runs take ~20-60 s).
  let status = null;
  for (let i = 0; i < 180; i++) {
    await sleep(1000);
    try {
      const run = await api("GET", `/runs/${encodeURIComponent(runId)}`);
      status = run.status;
      if (status !== "running" && status !== "queued") break;
    } catch {
      /* not indexed yet */
    }
  }
  await sleep(6000);
  const finalText = await pageText(win);
  const label = { completed: "Terminé", failed: "Échec", killed: "Arrêté", lost: "Perdu" }[status] || status;
  if (status !== "completed") issues.push({ route: currentLabel, level: "error", text: `run smoke ${runId} terminé en « ${status} »` });
  if (label && !finalText.includes(label)) issues.push({ route: currentLabel, level: "error", text: `statut « ${label} » absent de la page du run` });
  captured.push(await capture(win, "launch smoke done"));
  await collectPageErrors(win);
  return runId;
}

app.whenReady().then(async () => {
  if (app.dock) app.dock.hide();
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const captured = [];
  let routes;
  try {
    const health = await api("GET", "/health");
    process.stderr.write(`[render-check] backend ${BASE} ok (${health.workspace})\n`);
    const wanted = opt("--routes", null);
    routes = await resolveRoutes(wanted ? wanted.split(",") : DEFAULT_ROUTES);
  } catch (err) {
    process.stderr.write(`[render-check] backend injoignable : ${err.message}\n`);
    app.exit(2);
    return;
  }
  const win = createWindow();
  const started = Date.now();
  try {
    for (const route of routes) {
      await load(win, route);
      await collectPageErrors(win);
      captured.push(await capture(win, `light ${route}`));
      process.stderr.write(`[render-check] ${route}\n`);
    }
    if (flag("--dark")) {
      for (const route of await resolveRoutes(DARK_ROUTES)) {
        await load(win, route, { dark: true });
        await collectPageErrors(win);
        captured.push(await capture(win, `dark ${route}`));
        process.stderr.write(`[render-check] (sombre) ${route}\n`);
      }
    }
    if (flag("--dialog")) {
      await dialogScenario(win, captured);
      process.stderr.write("[render-check] dialogue de lancement capturé\n");
    }
    if (flag("--launch")) {
      const runId = await launchScenario(win, captured);
      process.stderr.write(`[render-check] lancement smoke : ${runId || "échec"}\n`);
    }
    if (flag("--hosts")) {
      await hostsScenario(win, captured);
      process.stderr.write("[render-check] page Machines exercée\n");
    }
    if (flag("--login")) {
      await loginScenario(win, captured);
      process.stderr.write("[render-check] écran de connexion capturé\n");
    }
  } catch (err) {
    issues.push({ route: currentLabel, level: "error", text: `script: ${err && err.stack ? err.stack : err}` });
  }
  const errors = issues.filter((i) => i.level === "error");
  const report = { base: BASE, routes, seconds: Math.round((Date.now() - started) / 1000), captures: captured, issues };
  fs.writeFileSync(path.join(OUT_DIR, "report.json"), JSON.stringify(report, null, 2));
  for (const i of issues) process.stdout.write(`[${i.level}] ${i.route} — ${i.text}${i.source && i.source !== ":" ? ` (${i.source})` : ""}\n`);
  process.stdout.write(`RENDER-CHECK ${errors.length === 0 ? "OK" : "FAIL"}: ${routes.length} routes, ${errors.length} erreur(s), ${issues.length - errors.length} avertissement(s), captures dans ${OUT_DIR}\n`);
  win.destroy();
  app.exit(errors.length === 0 ? 0 : 1);
});
