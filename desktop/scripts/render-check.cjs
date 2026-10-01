#!/usr/bin/env node
/**
 * Headless rendering check of the built frontend against a running backend.
 *
 *   npx electron scripts/render-check.cjs [--base http://127.0.0.1:8799] [--token test]
 *                                         [--dark] [--launch] [--dialog] [--login] [--member]
 *                                         [--users] [--hosts] [--routes /,/runs]
 *                                         [--email E --password P] [--width 1000] [--height 640]
 *
 * For every route, an invisible offscreen BrowserWindow (1360×880 by default;
 * `--width 1000` checks the minimum window width, captures get a `_w1000` suffix) stores the
 * token in the page's localStorage (`wmlab.token`, as the login screen does — the app never
 * reads `?token=`), loads `<base>/#<route>`, waits ~2.5 s, records console errors /
 * warnings and uncaught exceptions, and saves a PNG in desktop/.selftest/.
 * `--dark` renders a second pass with `data-theme="dark"` on key routes.
 * `--launch` opens the launch dialog, captures it, submits a local smoke
 * training and checks that the run page appears. `--dialog` only opens and captures the
 * dialog (no job: usable against scripts/mock-api.cjs). `--login` exercises the login screen
 * (§12.6): form without a session, wrong password, pending account, registration form,
 * e-mail + password login (`--email` / `--password`, default: the mock's admin), forced
 * password change and refused session — the steps that create or modify an account only run
 * against the mock. `--member` logs in as a member (`--member-email`, default
 * marie@example.com) and checks the read-only interface on key routes (no write button, no
 * code, no 403). `--users` exercises Réglages → Utilisateurs (create an account, temporary
 * password, approval; mock only). `--hosts` exercises the Machines page (§12.4): inventory
 * panel, retrieve job, decommission dialog (against the mock).
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
// Accounts of scripts/mock-api.cjs (all with the same demo password).
const LOGIN_EMAIL = opt("--email", "justin@example.com");
const LOGIN_PASSWORD = opt("--password", "motdepasse-demo-123");
const MEMBER_EMAIL = opt("--member-email", "marie@example.com");
const MEMBER_PASSWORD = opt("--member-password", "motdepasse-demo-123");
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

/**
 * Store the session as the login screen does (localStorage of the backend origin). The
 * public `/api/health` JSON page gives access to that origin without booting the app.
 * `token` null: no session at all.
 */
async function primeSession(win, token) {
  await win.loadURL(`${BASE}/api/health`);
  await win.webContents.executeJavaScript(
    `(() => { try { localStorage.removeItem('wmlab.apiBase'); ${token ? `localStorage.setItem('wmlab.token', ${JSON.stringify(token)});` : "localStorage.removeItem('wmlab.token');"} } catch {} })()`,
    true,
  );
}

async function load(win, route, { dark = false, token = TOKEN } = {}) {
  currentLabel = `${dark ? "dark" : "light"} ${route}`;
  const theme = dark ? "dark" : "light";
  // Both the OS preference (prefers-color-scheme) and the app override are forced,
  // so the pass does not depend on the Mac's current appearance.
  nativeTheme.themeSource = theme;
  await win.loadURL("about:blank");
  await primeSession(win, token);
  await win.loadURL(`${BASE}/#${route}`);
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

/** Set the value of the input labelled `label` (React-controlled: native setter + input event). */
const FILL = (label, value, scope = "document") => `
  (() => {
    const root = ${scope};
    const lab = [...root.querySelectorAll('label')].find((l) => l.textContent.trim() === ${JSON.stringify(label)});
    const el = lab && (lab.htmlFor ? document.getElementById(lab.htmlFor) : lab.querySelector('input'));
    if (!el) return false;
    const proto = el.tagName === 'SELECT' ? window.HTMLSelectElement.prototype : window.HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, ${JSON.stringify(value)});
    el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
    return true;
  })()`;

/** Fill several labelled fields; records an issue for a missing one. */
async function fillForm(win, fields, scope) {
  for (const [label, value] of Object.entries(fields)) {
    const ok = await win.webContents.executeJavaScript(FILL(label, value, scope), true);
    if (!ok) issues.push({ route: currentLabel, level: "error", text: `champ « ${label} » introuvable` });
  }
}

/** Click the submit button of the (first) form whose text matches. */
const SUBMIT = (text) => `
  (() => {
    const b = [...document.querySelectorAll('form button[type="submit"]')].find((e) => (e.innerText || '').trim() === ${JSON.stringify(text)});
    if (!b) return false;
    b.click();
    return true;
  })()`;

/** Wait until the page text matches `re` (or the timeout); resolves the last text. */
async function waitText(win, re, timeoutMs = 6000) {
  let text = "";
  for (let t = 0; t < timeoutMs; t += 250) {
    text = await pageText(win).catch(() => "");
    if (re.test(text)) return text;
    await sleep(250);
  }
  return text;
}

const expectText = (text, expected, what) => {
  for (const e of expected) if (!text.includes(e)) issues.push({ route: currentLabel, level: "error", text: `${what} : « ${e} » absent` });
};

/** Load the app without a session (login screen expected). */
async function loadLoggedOut(win, token = null) {
  nativeTheme.themeSource = "light";
  await win.loadURL("about:blank");
  await primeSession(win, token);
  await win.loadURL(`${BASE}/#/`);
  await win.webContents.executeJavaScript(EXCEPTION_HOOK, true);
  await win.webContents.executeJavaScript("document.documentElement.setAttribute('data-theme', 'light')", true);
  await sleep(SETTLE_MS);
}

/** Log in through the form; resolves the session token stored by the app (or null). */
async function loginThroughForm(win, email, password) {
  await fillForm(win, { "E-mail": email, "Mot de passe": password });
  if (!(await win.webContents.executeJavaScript(SUBMIT("Se connecter"), true))) {
    issues.push({ route: currentLabel, level: "error", text: "bouton « Se connecter » introuvable" });
    return null;
  }
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    const token = await win.webContents.executeJavaScript("(() => { try { return localStorage.getItem('wmlab.token'); } catch { return null; } })()", true);
    if (token) return token;
  }
  return null;
}

/**
 * Login screen (§12.6): form without a session, wrong password, pending account, registration,
 * e-mail + password login, forced password change (mock) and refused session.
 */
async function loginScenario(win, captured, { mock }) {
  if (mock) await api("POST", "/mock/reset").catch(() => undefined);

  currentLabel = "login (sans session)";
  await loadLoggedOut(win);
  let text = await pageText(win);
  expectText(text, ["E-mail", "Mot de passe", "Se connecter", "Créer un compte"], "écran de connexion");
  if (/Jeton/.test(text)) issues.push({ route: currentLabel, level: "error", text: "l'écran de connexion parle encore de « jeton »" });
  captured.push(await capture(win, "login"));
  await collectPageErrors(win, { expectReady: false });

  currentLabel = "login (mot de passe faux)";
  await fillForm(win, { "E-mail": LOGIN_EMAIL, "Mot de passe": "pas-le-bon-mot-de-passe" });
  await win.webContents.executeJavaScript(SUBMIT("Se connecter"), true);
  text = await waitText(win, /incorrect|refus|invalide/i);
  if (!/incorrect|refus|invalide/i.test(text)) issues.push({ route: currentLabel, level: "error", text: "mot de passe faux : aucun message d'erreur" });
  captured.push(await capture(win, "login wrong password"));

  if (mock) {
    currentLabel = "login (compte en attente)";
    await fillForm(win, { "E-mail": "paul@example.com", "Mot de passe": LOGIN_PASSWORD });
    await win.webContents.executeJavaScript(SUBMIT("Se connecter"), true);
    text = await waitText(win, /attente/i);
    if (!/attente/i.test(text)) issues.push({ route: currentLabel, level: "error", text: "compte en attente : message 403 absent" });
  }

  currentLabel = "register (formulaire)";
  await win.webContents.executeJavaScript(CLICK_TEXT("button", "Créer un compte"), true);
  await sleep(400);
  text = await pageText(win);
  expectText(text, ["Identifiant", "E-mail", "Mot de passe", "Confirmation", "Demander un compte"], "formulaire d'inscription");
  if (mock) {
    // Local validation first (12 characters minimum), then a real request.
    await fillForm(win, { Identifiant: "zoe", "E-mail": "zoe@example.com", "Mot de passe": "court", Confirmation: "court" });
    await win.webContents.executeJavaScript(SUBMIT("Demander un compte"), true);
    await sleep(300);
    text = await pageText(win);
    if (!text.includes("au moins 12 caractères")) issues.push({ route: currentLabel, level: "error", text: "validation locale du mot de passe absente" });
    await fillForm(win, { "Mot de passe": "une phrase de passe assez longue", Confirmation: "une phrase de passe assez longue" });
    captured.push(await capture(win, "register"));
    await win.webContents.executeJavaScript(SUBMIT("Demander un compte"), true);
    text = await waitText(win, /Demande enregistrée/);
    if (!text.includes("un administrateur doit approuver votre compte")) issues.push({ route: currentLabel, level: "error", text: "message « Demande enregistrée » absent" });
    captured.push(await capture(win, "register done"));
  } else {
    captured.push(await capture(win, "register"));
  }
  await collectPageErrors(win, { expectReady: false });

  currentLabel = "login (e-mail + mot de passe)";
  await loadLoggedOut(win);
  const token = await loginThroughForm(win, LOGIN_EMAIL, LOGIN_PASSWORD);
  if (!token) issues.push({ route: currentLabel, level: "error", text: `connexion de ${LOGIN_EMAIL} impossible (aucune session mémorisée)` });
  else if (mock && !token.startsWith("wmls_")) issues.push({ route: currentLabel, level: "error", text: `jeton de session inattendu : ${token.slice(0, 8)}…` });
  await sleep(SETTLE_MS);
  text = await pageText(win);
  if (mock && !text.includes("justin")) issues.push({ route: currentLabel, level: "error", text: "nom de l'utilisateur connecté absent de la barre latérale" });
  captured.push(await capture(win, "login ok"));
  await collectPageErrors(win);

  if (mock) {
    currentLabel = "login (mot de passe temporaire)";
    await loadLoggedOut(win);
    await loginThroughForm(win, "nina@example.com", LOGIN_PASSWORD);
    text = await waitText(win, /Définir un nouveau mot de passe/);
    if (!text.includes("Définir un nouveau mot de passe")) issues.push({ route: currentLabel, level: "error", text: "écran « Définir un nouveau mot de passe » absent" });
    captured.push(await capture(win, "login must change password"));
    await fillForm(win, { "Mot de passe actuel": LOGIN_PASSWORD, "Nouveau mot de passe": "nouvelle phrase de passe 42", Confirmation: "nouvelle phrase de passe 42" });
    await win.webContents.executeJavaScript(SUBMIT("Enregistrer le mot de passe"), true);
    let ready = false;
    for (let i = 0; i < 24 && !ready; i++) {
      await sleep(250);
      ready = await win.webContents.executeJavaScript("Boolean(document.querySelector('[data-app-ready]'))", true).catch(() => false);
    }
    if (!ready) issues.push({ route: currentLabel, level: "error", text: "l'application ne s'ouvre pas après le changement de mot de passe" });
    await collectPageErrors(win);
  }

  currentLabel = "login (session refusée)";
  await loadLoggedOut(win, "wmls_session_inconnue");
  text = await pageText(win);
  if (!/expiré|révoqué/i.test(text)) issues.push({ route: currentLabel, level: "error", text: "session refusée : aucune explication affichée" });
  captured.push(await capture(win, "login refused"));
  await collectPageErrors(win, { expectReady: false });
  if (mock) {
    // Registration closed (hosted.allow_registration = false): no « Créer un compte » link.
    currentLabel = "login (inscription fermée)";
    const settings = await api("GET", "/settings");
    await api("PUT", "/settings", { ...settings, hosted: { ...settings.hosted, allow_registration: false } });
    await loadLoggedOut(win);
    text = await pageText(win);
    if (text.includes("Créer un compte")) issues.push({ route: currentLabel, level: "error", text: "lien « Créer un compte » affiché alors que l'inscription est fermée" });
    await collectPageErrors(win, { expectReady: false });
  }
  // Leave the origin clean for a following pass; restore the mock's accounts and settings.
  await primeSession(win, null);
  if (mock) await api("POST", "/mock/reset").catch(() => undefined);
}

/**
 * Member (§12.6): logs in through the form, then checks the read-only interface — no write
 * button, no code (YAML, logs, registries), Réglages reduced — and that no route answers 403
 * (a 403 would show up as a console error).
 */
async function memberScenario(win, captured) {
  currentLabel = "member login";
  await loadLoggedOut(win);
  const token = await loginThroughForm(win, MEMBER_EMAIL, MEMBER_PASSWORD);
  if (!token) {
    issues.push({ route: currentLabel, level: "error", text: `connexion de ${MEMBER_EMAIL} impossible` });
    return;
  }
  // Redacted payloads (§9): no params, no plan, errors cut to their last line.
  const runs = await fetch(`${BASE}/api/runs?include_trials=true`, { headers: { "X-WMLab-Token": token } }).then((r) => r.json()).catch(() => []);
  if (!Array.isArray(runs) || !runs.length) issues.push({ route: currentLabel, level: "error", text: "GET /api/runs (membre) : aucune donnée" });
  for (const r of Array.isArray(runs) ? runs : []) {
    if (r.params && Object.keys(r.params).length) issues.push({ route: currentLabel, level: "error", text: `run ${r.run_id} : params non expurgés pour un membre` });
    if (typeof r.error === "string" && r.error.includes("\n")) issues.push({ route: currentLabel, level: "error", text: `run ${r.run_id} : traceback complète envoyée à un membre` });
  }
  const failed = (Array.isArray(runs) ? runs : []).find((r) => r.error);
  if (failed) {
    const detail = await fetch(`${BASE}/api/runs/${encodeURIComponent(failed.run_id)}`, { headers: { "X-WMLab-Token": token } }).then((r) => r.json()).catch(() => null);
    if (!detail || detail.plan !== null) issues.push({ route: currentLabel, level: "error", text: `run ${failed.run_id} : plan non expurgé pour un membre` });
    await load(win, `/runs/${encodeURIComponent(failed.run_id)}`, { token });
    currentLabel = `member /runs/${failed.run_id}`;
    const text = await pageText(win);
    if (/Traceback|\bDétails\b/.test(text)) issues.push({ route: currentLabel, level: "error", text: "traceback ou bouton « Détails » affiché à un membre" });
    if (!text.includes(failed.error)) issues.push({ route: currentLabel, level: "error", text: "dernière ligne de l'erreur absente" });
    await collectPageErrors(win);
    captured.push(await capture(win, "member failed run"));
  }
  const forbidden = ["Nouveau lancement", "Bibliothèque", "Nouvelle expérience", "Nouveau plan", "Ajouter une machine", "Modifier le YAML", "Voir le YAML", "Arrêter", "Brancher"];
  const checks = {
    "/": { absent: forbidden },
    "/runs/{run}": { absent: [...forbidden, "Évaluer", "Logs", "Curriculum", "Plan de phases indisponible"], present: ["Configuration", "Notes"] },
    "/runs/{run}?tab=config": { present: ["Configuration réservée aux administrateurs"], absent: ["Paramètres", "Overrides"] },
    "/compare?runs={run},{run2}": { present: ["Contexte"], absent: ["Paramètres"] },
    "/runs/{run}?tab=logs": { absent: ["Logs"] },
    "/runs/{run}?tab=notes": { absent: ["Enregistrer"] },
    "/runs/{run}?tab=env": { present: ["Réservé aux administrateurs"] },
    "/projects/image-hidden?tab=experiments": { absent: [...forbidden, "Dupliquer", "Lancer"] },
    "/projects/image-hidden?tab=hpo": { absent: [...forbidden, "Lancer"] },
    "/projects/image-hidden/experiments/baseline": { present: ["Réservé aux administrateurs"] },
    "/projects/image-hidden/hpo/lr_weights": { present: ["Réservé aux administrateurs"] },
    "/studies/{study}": { absent: [...forbidden, "Relancer", "Ouvrir le plan"] },
    "/studies/{study}?tab=notes": { absent: ["Enregistrer"] },
    "/library": { present: ["Réservé aux administrateurs"] },
    "/hosts": { absent: [...forbidden, "Tester la connexion", "Rapatrier les checkpoints", "Retirer la machine"] },
    "/datasets": { absent: [...forbidden, "Nouveau dataset"] },
    "/benchmarks": { absent: [...forbidden, "Nouvelle version"] },
    "/budget": { absent: ["Ajouter la dépense"], present: ["Entrées manuelles"] },
    "/leaderboard": { absent: forbidden },
    "/settings": { present: ["Mon compte", "Connexion", "membre"], absent: ["Utilisateurs", "Workspace", "Mode hébergé"] },
  };
  for (const route of await resolveRoutes(Object.keys(checks))) {
    const key = Object.keys(checks).find((k) => route === k || (k.includes("{") && new RegExp(`^${k.replace(/[?]/g, "\\?").replace(/\{run2?\}|\{study\}/g, "[^/?,]+")}$`).test(route)));
    const spec = checks[key] || {};
    await load(win, route, { token });
    currentLabel = `member ${route}`;
    const text = await pageText(win);
    for (const word of spec.absent || []) {
      const re = new RegExp(`(^|\\n)\\s*${word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*($|\\n)`);
      if (re.test(text)) issues.push({ route: currentLabel, level: "error", text: `lecture seule : « ${word} » affiché` });
    }
    for (const word of spec.present || []) if (!text.includes(word)) issues.push({ route: currentLabel, level: "error", text: `« ${word} » absent` });
    await collectPageErrors(win);
    captured.push(await capture(win, `member ${route}`));
  }
  await primeSession(win, null);
}

/** Réglages → Utilisateurs (admin, mock only): create an account (temporary password), approve a registration. */
async function usersScenario(win, captured) {
  await api("POST", "/mock/reset").catch(() => undefined);
  await load(win, "/settings");
  currentLabel = "users table";
  let text = await pageText(win);
  expectText(text, ["Utilisateurs", "paul@example.com", "en attente", "Approuver", "Créer un compte", "Inscription ouverte"], "réglages (admin)");
  await win.webContents.executeJavaScript(
    "(() => { const h = [...document.querySelectorAll('main div')].find((d) => d.textContent.trim() === 'Utilisateurs'); if (h) h.scrollIntoView({ block: 'start' }); })()",
    true,
  );
  await sleep(300);
  captured.push(await capture(win, "users table"));
  await collectPageErrors(win);

  currentLabel = "users create";
  await win.webContents.executeJavaScript(CLICK_TEXT("button", "Créer un compte"), true);
  await sleep(400);
  await fillForm(win, { "E-mail": "lucie@example.com", Identifiant: "lucie" }, "document.querySelector('[role=\"dialog\"]')");
  await win.webContents.executeJavaScript(CLICK_TEXT('[role="dialog"] button', "Créer le compte"), true);
  text = await waitText(win, /Affiché une seule fois/);
  expectText(text, ["Compte créé", "Affiché une seule fois", "lucie@example.com"], "mot de passe temporaire");
  const pwd = await win.webContents.executeJavaScript("(document.querySelector('[data-temporary-password]')?.textContent || '').trim()", true);
  if (pwd.length < 12) issues.push({ route: currentLabel, level: "error", text: `mot de passe temporaire non affiché (${pwd.length} caractères)` });
  captured.push(await capture(win, "users temporary password"));
  await win.webContents.executeJavaScript(CLICK_TEXT('[role="dialog"] button', "J'ai noté le mot de passe"), true);
  await sleep(400);

  currentLabel = "users approve";
  await win.webContents.executeJavaScript(CLICK_TEXT("button", "Approuver"), true);
  await sleep(400);
  text = await pageText(win);
  expectText(text, ["Approuver paul", "Rôle"], "dialogue d'approbation");
  captured.push(await capture(win, "users approve dialog"));
  await win.webContents.executeJavaScript(CLICK_TEXT('[role="dialog"] button', "Approuver"), true);
  text = await waitText(win, /Compte approuvé/);
  if (!text.includes("Compte approuvé")) issues.push({ route: currentLabel, level: "error", text: "toast « Compte approuvé » absent" });
  await collectPageErrors(win);
  await api("POST", "/mock/reset").catch(() => undefined);
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
  let mock = false;
  try {
    const health = await api("GET", "/health");
    mock = /mock/.test(String(health.version));
    process.stderr.write(`[render-check] backend ${BASE} ok (${health.workspace})${mock ? " — backend factice" : ""}\n`);
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
    if (flag("--users")) {
      if (mock) {
        await usersScenario(win, captured);
        process.stderr.write("[render-check] réglages → utilisateurs exercés\n");
      } else process.stderr.write("[render-check] --users ignoré : réservé au backend factice (modifie des comptes)\n");
    }
    if (flag("--member")) {
      await memberScenario(win, captured);
      process.stderr.write("[render-check] interface membre (lecture seule) vérifiée\n");
    }
    if (flag("--login")) {
      await loginScenario(win, captured, { mock });
      process.stderr.write("[render-check] écran de connexion exercé\n");
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
