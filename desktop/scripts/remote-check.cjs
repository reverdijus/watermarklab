#!/usr/bin/env node
/**
 * Headless check of the Electron remote mode (§12.6) against a running backend — the mock
 * (scripts/mock-api.cjs) by default.
 *
 *   npx electron scripts/remote-check.cjs [--base http://127.0.0.1:8781] [--token test] [--keep]
 *
 * 1. creates an isolated userData folder (WMLAB_USER_DATA, honoured by electron/main.cjs)
 *    holding `config.json` = {mode: "remote", apiBase, token};
 * 2. loads the REAL main process (electron/main.cjs) with `BrowserWindow.show` neutralised
 *    (no window appears) and `child_process.spawn` instrumented: any process start — the
 *    Python backend in particular — is recorded and refused;
 * 3. once the renderer is loaded, reads `window.wmlab` (mode, apiBase, token presence),
 *    waits for `[data-app-ready]` (the token reached the renderer and /api/health answered
 *    with `workspace`), checks the sidebar shows a user and the "Serveur" menu exists;
 * 4. saves desktop/.selftest/remote.png and prints `REMOTE-CHECK OK` (exit 0) or
 *    `REMOTE-CHECK FAIL: …` (exit 1). The temporary profile is deleted unless `--keep`.
 */
"use strict";

process.env.ELECTRON_DISABLE_SECURITY_WARNINGS = "true";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const cp = require("node:child_process");

const args = process.argv.slice(2);
const opt = (name, def) => {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : def;
};
const BASE = (process.env.WMLAB_API_BASE || opt("--base", "http://127.0.0.1:8781")).replace(/\/+$/, "");
const TOKEN = process.env.WMLAB_API_TOKEN || opt("--token", "test");
const KEEP = args.includes("--keep");
const TIMEOUT_MS = Number(opt("--timeout", "30000"));
const OUT_DIR = path.resolve(__dirname, "..", ".selftest");

// 1. Isolated profile with a remote session (plain token: safeStorage can only encrypt from
//    inside Electron; main.cjs accepts `token` when `tokenEncrypted` is absent).
const userData = fs.mkdtempSync(path.join(os.tmpdir(), "wmlab-remote-check-"));
fs.writeFileSync(path.join(userData, "config.json"), JSON.stringify({ mode: "remote", apiBase: BASE, token: TOKEN }, null, 2));
process.env.WMLAB_USER_DATA = userData;
// An explicit workspace or the self-test flag would force the local mode in main.cjs.
delete process.env.WMLAB_WORKSPACE;
delete process.env.WMLAB_SELFTEST;
delete process.env.VITE_DEV_SERVER_URL;

// 2. Instrument process creation before main.cjs captures `spawn`.
const spawned = [];
const refuse = (name) =>
  function instrumented(cmd, cmdArgs) {
    const line = `${name} ${cmd} ${Array.isArray(cmdArgs) ? cmdArgs.join(" ") : ""}`.trim();
    spawned.push(line);
    throw new Error(`remote-check : lancement de process refusé (${line})`);
  };
cp.spawn = refuse("spawn");
cp.exec = refuse("exec");
cp.execFile = refuse("execFile");
cp.fork = refuse("fork");

const { app, BrowserWindow, Menu } = require("electron");
for (const method of ["show", "showInactive", "focus", "moveTop"]) {
  BrowserWindow.prototype[method] = function neutralised() {};
}

const problems = [];
const consoleErrors = [];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let finished = false;

function cleanup() {
  if (KEEP) {
    process.stderr.write(`[remote-check] profil conservé : ${userData}\n`);
    return;
  }
  try {
    fs.rmSync(userData, { recursive: true, force: true });
  } catch {
    /* ignore */
  }
}

function finish(ok, reason) {
  if (finished) return;
  finished = true;
  for (const e of consoleErrors) process.stdout.write(`[console] ${e}\n`);
  for (const p of problems) process.stdout.write(`[fail] ${p}\n`);
  process.stdout.write(ok ? "REMOTE-CHECK OK\n" : `REMOTE-CHECK FAIL: ${reason}\n`);
  cleanup();
  app.exit(ok ? 0 : 1);
}

setTimeout(() => finish(false, `délai de ${TIMEOUT_MS / 1000} s dépassé (problèmes : ${problems.join(" | ") || "aucun"})`), TIMEOUT_MS).unref();

/** Evaluate in the renderer (main world, where contextBridge exposes window.wmlab). */
const evaluate = (wc, code) => wc.executeJavaScript(code, true);

async function inspect(win) {
  const wc = win.webContents;
  const url = wc.getURL();
  process.stderr.write(`[remote-check] page chargée : ${url}\n`);
  if (!url.startsWith("file://")) {
    problems.push(`le renderer n'est pas chargé depuis dist/ (${url.slice(0, 80)})`);
  }
  // 3a. window.wmlab
  const bridge = await evaluate(
    wc,
    `(() => { const w = window.wmlab; return w ? { present: true, mode: w.mode, apiBase: w.apiBase, hasToken: !!w.token, platform: w.platform, fns: ['openConnectDialog', 'useLocalBackend', 'disconnect'].filter((k) => typeof w[k] === 'function') } : { present: false }; })()`,
  );
  if (!bridge.present) problems.push("window.wmlab absent (preload non exécuté)");
  else {
    if (bridge.mode !== "remote") problems.push(`window.wmlab.mode = ${JSON.stringify(bridge.mode)} (attendu « remote »)`);
    if (bridge.apiBase !== BASE) problems.push(`window.wmlab.apiBase = ${JSON.stringify(bridge.apiBase)} (attendu ${BASE})`);
    if (!bridge.hasToken) problems.push("window.wmlab.token vide : le jeton de config.json n'a pas atteint le renderer");
    if (bridge.fns.length !== 3) problems.push(`commandes du menu Serveur absentes du bridge (${bridge.fns.join(", ") || "aucune"})`);
  }
  // 3b. The app mounted and the backend answered with the token.
  let ready = false;
  for (let i = 0; i < 40 && !ready; i++) {
    await sleep(250);
    ready = await evaluate(wc, "Boolean(document.querySelector('[data-app-ready]'))").catch(() => false);
  }
  if (!ready) problems.push("[data-app-ready] absent : interface non montée ou serveur distant injoignable depuis le renderer");
  await sleep(1500);
  const text = await evaluate(wc, "document.body.innerText").catch(() => "");
  if (/Aucun jeton n'est enregistré|a refusé le jeton/.test(text)) problems.push("écran « connexion requise » affiché alors que le jeton est valide");
  if (!/Se déconnecter|Watermark Lab/.test(text)) problems.push("barre latérale absente");
  // The sidebar footer names the user returned by /api/me.
  const me = await fetch(`${BASE}/api/me`, { headers: { "X-WMLab-Token": TOKEN } })
    .then((r) => (r.ok ? r.json() : null))
    .catch(() => null);
  if (me && me.name && !text.includes(me.name)) problems.push(`nom de l'utilisateur « ${me.name} » absent de la page`);
  // 3c. "Serveur" menu.
  const menu = Menu.getApplicationMenu();
  const server = menu && menu.items.find((i) => i.label === "Serveur");
  const labels = server && server.submenu ? server.submenu.items.map((i) => i.label) : [];
  if (!server) problems.push("menu « Serveur » absent");
  else {
    if (!labels.includes("Se connecter à un serveur distant…")) problems.push("entrée « Se connecter à un serveur distant… » absente");
    const local = server.submenu.items.find((i) => i.label === "Utiliser le backend local");
    if (!local) problems.push("entrée « Utiliser le backend local » absente");
    else if (!local.enabled) problems.push("« Utiliser le backend local » devrait être activée en mode distant");
    if (!labels.some((l) => l.startsWith("Connecté à "))) problems.push("état « Connecté à <serveur> » absent du menu Serveur");
  }
  // 3d. No process was started (the Python backend in particular).
  if (spawned.length) problems.push(`process lancé en mode distant : ${spawned.join(" ; ")}`);
  // 4. Capture.
  try {
    fs.mkdirSync(OUT_DIR, { recursive: true });
    const image = await wc.capturePage();
    if (!image.isEmpty()) {
      const file = path.join(OUT_DIR, "remote.png");
      fs.writeFileSync(file, image.toPNG());
      process.stderr.write(`[remote-check] capture : ${file}\n`);
    }
  } catch (err) {
    process.stderr.write(`[remote-check] capture impossible : ${err && err.message ? err.message : err}\n`);
  }
  finish(problems.length === 0, problems.join(" | "));
}

app.on("browser-window-created", (_event, win) => {
  const wc = win.webContents;
  wc.on("console-message", (event) => {
    const level = typeof event.level === "string" ? event.level : ["debug", "info", "warning", "error"][event.level] || String(event.level);
    if (level !== "error") return;
    const msg = String(event.message).trim();
    if (/Autofill\.|DevTools/.test(msg)) return;
    consoleErrors.push(msg.slice(0, 400));
  });
  wc.on("did-finish-load", () => {
    const url = wc.getURL();
    if (url.startsWith("data:")) {
      // Loading page, or the error page of main.cjs: report the latter.
      void evaluate(wc, "document.body.innerText").then((text) => {
        if (/injoignable|n'a pas pu démarrer|manquant/.test(text)) finish(false, `page d'erreur affichée : ${text.replace(/\s+/g, " ").slice(0, 200)}`);
      }).catch(() => undefined);
      return;
    }
    // One inspection only (the renderer is loaded once in remote mode).
    if (!finished && !inspect.started) {
      inspect.started = true;
      void inspect(win).catch((err) => finish(false, `inspection impossible : ${err && err.message ? err.message : err}`));
    }
  });
  wc.on("render-process-gone", (_e, details) => finish(false, `process de rendu arrêté (${details.reason})`));
});

app.whenReady().then(() => {
  if (app.dock) app.dock.hide();
});

process.stderr.write(`[remote-check] profil isolé : ${userData} → ${BASE}\n`);
// The real main process: it reads config.json from WMLAB_USER_DATA and boots in remote mode.
require(path.join(__dirname, "..", "electron", "main.cjs"));
