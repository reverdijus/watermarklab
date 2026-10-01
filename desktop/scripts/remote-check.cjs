#!/usr/bin/env node
/**
 * Headless check of the Electron remote mode (§12.6) against a running backend — the mock
 * (scripts/mock-api.cjs) by default.
 *
 *   npx electron scripts/remote-check.cjs [--base http://127.0.0.1:8781] [--token test] [--keep]
 *                                         [--login [--email E --password P]]
 *
 * 1. creates an isolated userData folder (WMLAB_USER_DATA, honoured by electron/main.cjs)
 *    holding `config.json` = {mode: "remote", apiBase, token} — with `--login`, no token:
 *    the connection dialog must open by itself; the script fills the e-mail and password
 *    (default: the mock's admin), submits, and the main process logs in
 *    (`POST /api/auth/login`) and stores only the session token (safeStorage is disabled
 *    in this check: no Keychain access, the token lands in `token`);
 * 2. loads the REAL main process (electron/main.cjs) with `BrowserWindow.show` neutralised
 *    (no window appears) and `child_process.spawn` instrumented: any process start — the
 *    Python backend in particular — is recorded and refused;
 * 3. once the renderer is loaded, reads `window.wmlab` (mode, apiBase, token presence),
 *    waits for `[data-app-ready]` (the token reached the renderer and /api/health answered
 *    with `workspace`), checks the sidebar shows a user and the "Serveur" menu exists;
 * 4. with `--login`: config.json holds the session token and never the password; « Se
 *    déconnecter » (`window.wmlab.disconnect()`) revokes the session on the server
 *    (`/api/me` → 401) and forgets it;
 * 5. saves desktop/.selftest/remote.png and prints `REMOTE-CHECK OK` (exit 0) or
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
const LOGIN = args.includes("--login");
const EMAIL = opt("--email", "justin@example.com");
const PASSWORD = opt("--password", "motdepasse-demo-123");
const TIMEOUT_MS = Number(opt("--timeout", "30000"));
const OUT_DIR = path.resolve(__dirname, "..", ".selftest");

// 1. Isolated profile with a remote session (plain token: safeStorage can only encrypt from
//    inside Electron; main.cjs accepts `token` when `tokenEncrypted` is absent). --login:
//    remote mode without any session.
const userData = fs.mkdtempSync(path.join(os.tmpdir(), "wmlab-remote-check-"));
const configFile = path.join(userData, "config.json");
fs.writeFileSync(configFile, JSON.stringify(LOGIN ? { mode: "remote", apiBase: BASE } : { mode: "remote", apiBase: BASE, token: TOKEN }, null, 2));
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

const { app, BrowserWindow, Menu, safeStorage } = require("electron");
for (const method of ["show", "showInactive", "focus", "moveTop"]) {
  BrowserWindow.prototype[method] = function neutralised() {};
}
// No Keychain access from a check: the session token is stored in clear in the temporary profile.
try {
  safeStorage.isEncryptionAvailable = () => false;
} catch {
  /* read-only binding: the check may then touch the Keychain */
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

/** Connection dialog (--login): fill e-mail + password and submit (the URL is pre-filled). */
async function submitConnectDialog(wc) {
  const prefilled = await evaluate(wc, "document.getElementById('url').value");
  if (prefilled !== BASE) problems.push(`dialogue de connexion : adresse pré-remplie ${JSON.stringify(prefilled)} (attendu ${BASE})`);
  const labels = await evaluate(wc, "[...document.querySelectorAll('label')].map((l) => l.textContent.trim())");
  for (const expected of ["Adresse du serveur", "E-mail", "Mot de passe"]) {
    if (!labels.includes(expected)) problems.push(`dialogue de connexion : champ « ${expected} » absent`);
  }
  if (labels.some((l) => /jeton/i.test(l))) problems.push("dialogue de connexion : un champ « jeton » subsiste");
  // Wrong password first: the backend message must be relayed.
  await evaluate(wc, `document.getElementById('email').value = ${JSON.stringify(EMAIL)}; document.getElementById('password').value = 'pas-le-bon-mot-de-passe'; document.getElementById('form').requestSubmit(); true`);
  let status = "";
  for (let i = 0; i < 40 && !/incorrect|refus/i.test(status); i++) {
    await sleep(150);
    status = await evaluate(wc, "document.getElementById('status').textContent").catch(() => "");
  }
  if (!/incorrect|refus/i.test(status)) problems.push(`dialogue de connexion : message du serveur non relayé pour un mauvais mot de passe (« ${status} »)`);
  await evaluate(wc, `document.getElementById('password').value = ${JSON.stringify(PASSWORD)}; document.getElementById('form').requestSubmit(); true`);
  for (let i = 0; i < 40 && !/Connecté en tant que/.test(status); i++) {
    await sleep(100);
    status = await evaluate(wc, "document.getElementById('status').textContent").catch(() => status);
  }
  process.stderr.write(`[remote-check] dialogue de connexion : ${status}\n`);
  if (!/Connecté en tant que/.test(status)) problems.push(`dialogue de connexion : « Connecté en tant que … » absent (« ${status} »)`);
}

/** --login: the session is stored without the password, and « Se déconnecter » revokes it. */
async function checkSessionLifecycle(wc, sessionToken) {
  const cfg = JSON.parse(fs.readFileSync(configFile, "utf8"));
  const raw = fs.readFileSync(configFile, "utf8");
  if (cfg.token !== sessionToken && !cfg.tokenEncrypted) problems.push("config.json ne contient pas la session reçue par le renderer");
  if (raw.includes(PASSWORD) || /password/i.test(raw)) problems.push("config.json contient le mot de passe");
  if (!/^wmls_/.test(sessionToken)) problems.push(`jeton de session inattendu : ${String(sessionToken).slice(0, 6)}…`);
  await evaluate(wc, "window.wmlab.disconnect(); true");
  let status = 0;
  for (let i = 0; i < 30; i++) {
    await sleep(200);
    status = await fetch(`${BASE}/api/me`, { headers: { "X-WMLab-Token": sessionToken } }).then((r) => r.status).catch(() => 0);
    if (status === 401) break;
  }
  if (status !== 401) problems.push(`« Se déconnecter » n'a pas révoqué la session sur le serveur (/api/me → ${status})`);
  const after = JSON.parse(fs.readFileSync(configFile, "utf8"));
  if (after.token || after.tokenEncrypted) problems.push("« Se déconnecter » n'a pas oublié la session dans config.json");
}

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
  const sessionToken = LOGIN ? await evaluate(wc, "window.wmlab ? window.wmlab.token : ''").catch(() => "") : TOKEN;
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
  if (/Aucune session n'est enregistrée|a refusé la session/.test(text)) problems.push("écran « connexion requise » affiché alors que la session est valide");
  if (!/Se déconnecter|Watermark Lab/.test(text)) problems.push("barre latérale absente");
  // The sidebar footer names the user returned by /api/me.
  const me = await fetch(`${BASE}/api/me`, { headers: { "X-WMLab-Token": sessionToken } })
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
  if (LOGIN) await checkSessionLifecycle(wc, sessionToken);
  finish(problems.length === 0, problems.join(" | "));
}

let rendererLoads = 0;
let connectSubmitted = false;
// --login: the « Connexion requise » screen is inspected before the dialog is submitted.
let markFirstScreenChecked;
const firstScreenChecked = new Promise((resolve) => {
  markFirstScreenChecked = resolve;
  setTimeout(resolve, 8000).unref();
});

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
      void evaluate(wc, "[Boolean(document.getElementById('form') && document.getElementById('password')), document.body.innerText]").then(([isConnect, text]) => {
        if (isConnect) {
          // Connection dialog of the main process.
          if (!LOGIN) problems.push("le dialogue de connexion s'est ouvert alors qu'une session valide était enregistrée");
          else if (!connectSubmitted) {
            connectSubmitted = true;
            void firstScreenChecked
              .then(() => submitConnectDialog(wc))
              .catch((err) => problems.push(`dialogue de connexion : ${err && err.message ? err.message : err}`));
          }
          return;
        }
        // Loading page, or the error page of main.cjs: report the latter.
        if (/injoignable|n'a pas pu démarrer|manquant/.test(text)) finish(false, `page d'erreur affichée : ${text.replace(/\s+/g, " ").slice(0, 200)}`);
      }).catch(() => undefined);
      return;
    }
    rendererLoads += 1;
    if (LOGIN && rendererLoads === 1) {
      // No session yet: « Connexion requise » while the dialog opens.
      void (async () => {
        let text = "";
        for (let i = 0; i < 30 && !/Connexion requise/.test(text); i++) {
          await sleep(200);
          text = await evaluate(wc, "document.body.innerText").catch(() => "");
        }
        if (!/Connexion requise/.test(text)) problems.push(`sans session : écran « Connexion requise » absent (${text.replace(/\s+/g, " ").slice(0, 120)})`);
        if (/Nouveau lancement|Bibliothèque/.test(text)) problems.push("sans session : l'interface s'affiche avant la connexion");
      })().finally(() => markFirstScreenChecked());
      return;
    }
    // One inspection only (the renderer is loaded once with a session).
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
