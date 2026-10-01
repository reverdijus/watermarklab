/**
 * Watermark Lab — Electron main process (CommonJS, not compiled).
 *
 * Local mode (default):
 * 1. resolves the workspace (WMLAB_WORKSPACE → userData/config.json (user choice) →
 *    resources/workspace.json (embedded default) → folder picker, then remembered);
 * 2. starts the Python backend on a free port with a random token (passed through the
 *    WMLAB_TOKEN environment variable, never on the command line);
 * 3. waits for GET /api/health, then loads the React renderer;
 * 4. stops the backend when the app quits (training jobs are independent
 *    processes and keep running).
 *
 * Remote mode (§12.6, menu "Serveur → Se connecter à un serveur distant…"): config.json
 * holds `{mode: "remote", apiBase, tokenEncrypted | token}` (token encrypted with
 * `safeStorage` when available); no Python backend is started, the renderer talks to the
 * central server. "Serveur → Utiliser le backend local" switches back.
 *
 * WMLAB_SELFTEST=1 runs a headless smoke test (always in local mode) and prints
 * "SELFTEST OK" or "SELFTEST FAIL: <raison>" before exiting with code 0 / 1.
 */
"use strict";

const { app, BrowserWindow, Menu, dialog, ipcMain, nativeTheme, safeStorage, session, shell } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const net = require("node:net");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");

const APP_NAME = "Watermark Lab";
const SELFTEST = process.env.WMLAB_SELFTEST === "1";
const DEV_URL = (process.env.VITE_DEV_SERVER_URL || "").replace(/\/+$/, "");
const HEALTH_TIMEOUT_MS = 40_000;
const SELFTEST_TIMEOUT_MS = 60_000;
const SELFTEST_READY_DELAY_MS = 3_000;
const KILL_GRACE_MS = 3_000;
const LOG_ROTATE_BYTES = 5 * 1024 * 1024;
const SETUP_COMMAND = "uv sync --extra train --extra server";
const REMOTE_TIMEOUT_MS = 10_000;

app.setName(APP_NAME);

// Isolated profile (tests, several configurations side by side): WMLAB_USER_DATA=<dir>
// replaces "~/Library/Application Support/Watermark Lab" (config.json, logs, caches). Must
// be set before the app is ready.
if (process.env.WMLAB_USER_DATA) {
  try {
    const dir = path.resolve(process.env.WMLAB_USER_DATA);
    fs.mkdirSync(dir, { recursive: true });
    app.setPath("userData", dir);
  } catch (err) {
    process.stderr.write(`WMLAB_USER_DATA ignoré : ${err && err.message ? err.message : err}\n`);
  }
}

/** Mutable application state. */
const state = {
  workspace: /** @type {string | null} */ (null),
  port: 0,
  token: crypto.randomBytes(24).toString("hex"),
  logPath: "",
  /** @type {import("node:child_process").ChildProcess | null} */
  backend: null,
  backendExit: /** @type {{ code: number | null, signal: string | null } | null} */ (null),
  /** @type {BrowserWindow | null} */
  window: null,
  starting: false,
  selftestDone: false,
  /** "local" (backend started here) or "remote" (central server, §12.6). */
  mode: /** @type {"local" | "remote"} */ ("local"),
  remote: { apiBase: "", token: "" },
  /** @type {BrowserWindow | null} */
  connectWindow: null,
};

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function isDir(p) {
  try {
    return fs.statSync(p).isDirectory();
  } catch {
    return false;
  }
}

function readJson(p) {
  try {
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch {
    return null;
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function shellQuote(s) {
  return /^[\w@%+=:,./-]+$/.test(s) ? s : `'${String(s).replace(/'/g, "'\\''")}'`;
}

function configPath() {
  return path.join(app.getPath("userData"), "config.json");
}

function resourcesWorkspaceFile() {
  return app.isPackaged
    ? path.join(process.resourcesPath, "workspace.json")
    : path.join(__dirname, "..", "resources", "workspace.json");
}

function saveConfig(patch) {
  const current = readJson(configPath()) || {};
  fs.mkdirSync(path.dirname(configPath()), { recursive: true });
  fs.writeFileSync(configPath(), JSON.stringify({ ...current, ...patch }, null, 2));
}

/** Normalised http(s) origin (+ optional path prefix) of a remote server, or null. */
function normalizeApiBase(raw) {
  let s = String(raw || "").trim();
  if (!s) return null;
  if (!/^https?:\/\//i.test(s)) s = `${/^(localhost|127\.0\.0\.1|\[::1\])(:|\/|$)/i.test(s) ? "http" : "https"}://${s}`;
  try {
    const url = new URL(s);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return `${url.origin}${url.pathname.replace(/\/+$/, "")}`;
  } catch {
    return null;
  }
}

/** Remote session stored in config.json (token decrypted with safeStorage when it was encrypted). */
function readRemoteConfig() {
  const cfg = readJson(configPath());
  if (!cfg || cfg.mode !== "remote") return null;
  const apiBase = normalizeApiBase(cfg.apiBase);
  if (!apiBase) return null;
  let token = "";
  if (typeof cfg.tokenEncrypted === "string" && cfg.tokenEncrypted) {
    try {
      if (safeStorage.isEncryptionAvailable()) token = safeStorage.decryptString(Buffer.from(cfg.tokenEncrypted, "base64"));
    } catch {
      token = "";
    }
  } else if (typeof cfg.token === "string") {
    token = cfg.token;
  }
  return { apiBase, token };
}

/** Persist the remote session; the token is encrypted with safeStorage when available. */
function saveRemoteConfig(apiBase, token) {
  let encrypted = false;
  let stored = token;
  try {
    if (token && safeStorage.isEncryptionAvailable()) {
      stored = safeStorage.encryptString(token).toString("base64");
      encrypted = true;
    }
  } catch {
    encrypted = false;
    stored = token;
  }
  saveConfig({ mode: "remote", apiBase, token: encrypted ? null : stored, tokenEncrypted: encrypted ? stored : null });
}

function tailFile(file, maxLines = 40) {
  try {
    const stat = fs.statSync(file);
    const size = Math.min(stat.size, 64 * 1024);
    const fd = fs.openSync(file, "r");
    const buf = Buffer.alloc(size);
    fs.readSync(fd, buf, 0, size, stat.size - size);
    fs.closeSync(fd);
    return buf.toString("utf8").split("\n").slice(-maxLines).join("\n").trim();
  } catch {
    return "";
  }
}

function selftestLog(message) {
  if (SELFTEST) process.stderr.write(`[selftest] ${message}\n`);
}

// ---------------------------------------------------------------------------
// Workspace resolution
// ---------------------------------------------------------------------------

/** Workspace chosen by the user ("Choisir le workspace…"), remembered in userData/config.json. */
function rememberedWorkspace() {
  const cfg = readJson(configPath());
  return cfg && typeof cfg.workspace === "string" && isDir(cfg.workspace) ? cfg.workspace : null;
}

/** Workspace embedded at build time (resources/workspace.json): the default of a first launch. */
function bundledWorkspace() {
  const bundled = readJson(resourcesWorkspaceFile());
  return bundled && typeof bundled.workspace === "string" && isDir(bundled.workspace) ? bundled.workspace : null;
}

/**
 * Resolve the workspace root. Order: WMLAB_WORKSPACE, the workspace chosen by the user
 * (userData/config.json — a choice made through the menu must survive a restart),
 * resources/workspace.json (default embedded in the app), then a folder picker
 * (remembered in config.json). The self-test ignores config.json so that it always
 * checks the workspace it was built from.
 */
async function resolveWorkspace({ forceDialog = false } = {}) {
  if (!forceDialog) {
    const env = process.env.WMLAB_WORKSPACE;
    if (env && isDir(env)) return path.resolve(env);
    const remembered = SELFTEST ? null : rememberedWorkspace();
    if (remembered) return remembered;
    const bundled = bundledWorkspace();
    if (bundled) return bundled;
  }
  if (SELFTEST) return null;

  const options = {
    title: "Choisir le workspace Watermark Lab",
    message: "Sélectionnez le dossier du dépôt (celui qui contient workspace.yaml).",
    buttonLabel: "Utiliser ce dossier",
    properties: ["openDirectory"],
  };
  const result = state.window ? await dialog.showOpenDialog(state.window, options) : await dialog.showOpenDialog(options);
  if (result.canceled || !result.filePaths.length) return null;
  const chosen = result.filePaths[0];
  if (!fs.existsSync(path.join(chosen, "workspace.yaml"))) {
    const { response } = await dialog.showMessageBox({
      type: "warning",
      buttons: ["Choisir un autre dossier", "Utiliser quand même", "Annuler"],
      defaultId: 0,
      cancelId: 2,
      message: "Ce dossier ne contient pas de fichier workspace.yaml.",
      detail: chosen,
    });
    if (response === 0) return resolveWorkspace({ forceDialog: true });
    if (response === 2) return null;
  }
  saveConfig({ workspace: chosen });
  return chosen;
}

// ---------------------------------------------------------------------------
// Backend process
// ---------------------------------------------------------------------------

/** Free TCP port on 127.0.0.1 (OS-assigned). */
function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const address = srv.address();
      const port = typeof address === "object" && address ? address.port : 0;
      srv.close(() => resolve(port));
    });
  });
}

function pythonPath(workspace) {
  return path.join(workspace, ".venv", "bin", "python");
}

function openLog() {
  const dir = path.join(app.getPath("userData"), "logs");
  fs.mkdirSync(dir, { recursive: true });
  state.logPath = path.join(dir, "backend.log");
  try {
    if (fs.statSync(state.logPath).size > LOG_ROTATE_BYTES) fs.renameSync(state.logPath, `${state.logPath}.1`);
  } catch {
    /* no previous log */
  }
  return fs.openSync(state.logPath, "a");
}

class StartupError extends Error {
  constructor(kind, message) {
    super(message);
    this.kind = kind;
  }
}

/** Spawn `<ws>/.venv/bin/python -m wmlab.cli server …` with output to backend.log. */
function spawnBackend() {
  const ws = state.workspace;
  const python = pythonPath(ws);
  if (!fs.existsSync(python)) {
    throw new StartupError("venv", `Environnement Python introuvable : ${python}`);
  }
  const fd = openLog();
  fs.writeSync(fd, `\n=== ${new Date().toISOString()} — démarrage du backend sur 127.0.0.1:${state.port} (workspace ${ws}) ===\n`);
  // The session token travels through the environment (WMLAB_TOKEN), not argv: `ps` shows
  // command lines to every local user.
  const args = ["-m", "wmlab.cli", "server", "--port", String(state.port), "--workspace", ws];
  state.backendExit = null;
  const child = spawn(python, args, {
    cwd: ws,
    env: { ...process.env, WMLAB_WORKSPACE: ws, WMLAB_TOKEN: state.token, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", fd, fd],
  });
  fs.closeSync(fd);
  state.backend = child;
  child.on("error", (err) => {
    state.backendExit = { code: null, signal: String(err.message) };
  });
  child.on("exit", (code, signal) => {
    state.backendExit = { code, signal };
    if (state.backend === child) state.backend = null;
    // Unexpected exit while the renderer is displayed.
    if (!app.isQuitting && !state.starting && state.window && !state.window.isDestroyed()) {
      showError({
        title: "Le backend s'est arrêté",
        message: `Le serveur Python s'est terminé de façon inattendue (code ${code ?? "—"}${signal ? `, signal ${signal}` : ""}).`,
        kind: "crash",
      });
    }
  });
  return child;
}

/** Poll GET /api/health until ok (≤ 40 s) or the process exits. */
async function waitForHealth() {
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  const url = `http://127.0.0.1:${state.port}/api/health`;
  while (Date.now() < deadline) {
    if (state.backendExit) {
      const { code, signal } = state.backendExit;
      throw new StartupError("exit", `Le backend s'est arrêté pendant le démarrage (code ${code ?? "—"}${signal ? `, ${signal}` : ""}).`);
    }
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 2000);
      const res = await fetch(url, { headers: { "X-WMLab-Token": state.token }, signal: ctrl.signal });
      clearTimeout(timer);
      if (res.ok) {
        const body = await res.json().catch(() => null);
        if (body && body.ok) return body;
      }
    } catch {
      /* not listening yet */
    }
    await sleep(300);
  }
  throw new StartupError("timeout", "Le backend n'a pas répondu à /api/health en moins de 40 secondes.");
}

/** SIGTERM, then SIGKILL after 3 s. Resolves once the process is gone. */
function stopBackend() {
  const child = state.backend;
  if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
  return new Promise((resolve) => {
    let killTimer = null;
    const done = () => {
      if (killTimer) clearTimeout(killTimer);
      resolve();
    };
    child.once("exit", done);
    try {
      child.kill("SIGTERM");
    } catch {
      done();
      return;
    }
    killTimer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        /* already gone */
      }
      setTimeout(resolve, 300);
    }, KILL_GRACE_MS);
  });
}

// ---------------------------------------------------------------------------
// Local pages (loading / error)
// ---------------------------------------------------------------------------

function palette() {
  const dark = nativeTheme.shouldUseDarkColors;
  return dark
    ? { bg: "#262624", surface: "#30302E", border: "#44433F", text: "#F5F4EE", muted: "#A6A49B", accent: "#D97757", code: "#393937", danger: "#E27D74" }
    : { bg: "#FAF9F5", surface: "#FFFFFF", border: "#E6E3D9", text: "#1F1E1D", muted: "#6E6C64", accent: "#C6613F", code: "#F5F4EF", danger: "#B3413A" };
}

function pageShell(body) {
  const c = palette();
  return `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>${APP_NAME}</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>
  html,body{margin:0;height:100%;background:${c.bg};color:${c.text};font:14px/1.55 system-ui,-apple-system,BlinkMacSystemFont,sans-serif;-webkit-font-smoothing:antialiased}
  body{-webkit-app-region:drag;display:flex;align-items:center;justify-content:center}
  main{-webkit-app-region:no-drag;max-width:720px;width:calc(100% - 96px);user-select:text}
  h1{font:400 30px/1.2 ui-serif,"New York",Georgia,serif;margin:0 0 10px}
  p{color:${c.muted};margin:0 0 14px}
  .label{font-size:12px;color:${c.muted};margin:18px 0 6px}
  pre{background:${c.code};border:1px solid ${c.border};border-radius:12px;padding:12px 14px;margin:0;font:12.5px/1.6 ui-monospace,"SF Mono",Menlo,monospace;white-space:pre-wrap;word-break:break-all;color:${c.text}}
  pre.log{max-height:260px;overflow:auto;color:${c.muted}}
  .reason{color:${c.danger}}
  .hint{margin-top:22px;font-size:13px}
  kbd{font:12px system-ui;border:1px solid ${c.border};border-radius:6px;padding:1px 6px;background:${c.surface}}
  .spinner{width:22px;height:22px;border-radius:50%;border:2.5px solid ${c.border};border-top-color:${c.accent};animation:spin .9s linear infinite;margin:0 auto 18px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .center{text-align:center}
</style></head><body><main>${body}</main></body></html>`;
}

function dataUrl(html) {
  return `data:text/html;charset=utf-8,${encodeURIComponent(html)}`;
}

function loadingPage(message = "Démarrage du backend…") {
  return pageShell(`<div class="center"><div class="spinner"></div><h1>${APP_NAME}</h1><p>${escapeHtml(message)}</p></div>`);
}

/**
 * Show a readable French error page with the command to run and the log path.
 * @param {{title: string, message: string, kind?: string}} err
 */
function showError(err) {
  const ws = state.workspace;
  const parts = [`<h1>${escapeHtml(err.title)}</h1>`, `<p class="reason">${escapeHtml(err.message)}</p>`];
  if (err.kind === "remote") {
    parts.push(`<div class="label">Serveur distant</div><pre>${escapeHtml(state.remote.apiBase)}</pre>`);
    parts.push(
      `<p class="hint">Vérifiez l'adresse et le réseau, puis <b>Serveur → Se connecter à un serveur distant…</b> pour corriger la connexion, ou <b>Serveur → Utiliser le backend local</b>.</p>`,
    );
  } else if (ws) {
    parts.push(`<div class="label">Workspace</div><pre>${escapeHtml(ws)}</pre>`);
  }
  if (err.kind === "remote") {
    /* hints already added */
  } else if (err.kind === "no-workspace") {
    parts.push(
      `<p class="hint">Choisissez le dossier du dépôt via le menu <b>${APP_NAME} → Choisir le workspace…</b>, ou définissez la variable d'environnement <code>WMLAB_WORKSPACE</code>.</p>`,
    );
  } else if (err.kind === "no-build") {
    parts.push(`<div class="label">Compilez l'interface puis relancez l'application :</div><pre>cd ${escapeHtml(shellQuote(path.join(__dirname, "..")))} &amp;&amp; npm run build</pre>`);
  } else {
    parts.push(
      `<div class="label">Installez ou mettez à jour l'environnement Python du workspace :</div><pre>cd ${escapeHtml(
        shellQuote(ws || "<workspace>"),
      )} &amp;&amp; ${SETUP_COMMAND}</pre>`,
    );
    if (state.logPath) {
      const tail = tailFile(state.logPath);
      parts.push(`<div class="label">Journal du backend</div><pre>${escapeHtml(state.logPath)}</pre>`);
      if (tail) parts.push(`<div class="label">Dernières lignes</div><pre class="log">${escapeHtml(tail)}</pre>`);
    }
    parts.push(
      `<p class="hint">Puis relancez le backend : menu <b>Affichage → Redémarrer le backend</b> (<kbd>⌥⌘R</kbd>), ou redémarrez l'application.</p>`,
    );
  }
  if (state.window && !state.window.isDestroyed()) {
    state.window.loadURL(dataUrl(pageShell(parts.join("\n")))).catch(() => undefined);
  }
  if (SELFTEST) finishSelftest(false, err.message);
}

// ---------------------------------------------------------------------------
// Window & renderer
// ---------------------------------------------------------------------------

function isInternalUrl(url) {
  return url.startsWith("file://") || url.startsWith("data:") || (DEV_URL && url.startsWith(DEV_URL));
}

/** Origin the renderer talks to: the local backend, or the remote server in remote mode. */
function backendOrigin() {
  return state.mode === "remote" ? state.remote.apiBase : `http://127.0.0.1:${state.port}`;
}

function sessionToken() {
  return state.mode === "remote" ? state.remote.token : state.token;
}

/**
 * The preload script asks for the backend origin and session token over IPC (synchronous,
 * before the renderer runs) instead of `additionalArguments`, which would expose the token
 * on the renderer's command line.
 */
function installSessionIpc() {
  const fromMainWindow = (event) => {
    const win = state.window;
    return !!win && !win.isDestroyed() && event.sender === win.webContents;
  };
  ipcMain.on("wmlab:session", (event) => {
    event.returnValue = fromMainWindow(event)
      ? { apiBase: backendOrigin(), token: sessionToken(), mode: state.mode }
      : { apiBase: "", token: "", mode: "local" };
  });
  // Commands of the "Serveur" menu, also reachable from the renderer (login screen).
  ipcMain.on("wmlab:open-connect-dialog", (event) => {
    if (fromMainWindow(event)) openConnectDialog();
  });
  ipcMain.on("wmlab:use-local-backend", (event) => {
    if (fromMainWindow(event)) void useLocalBackend();
  });
  ipcMain.on("wmlab:disconnect", (event) => {
    if (fromMainWindow(event)) void disconnectRemote();
  });
  installConnectIpc();
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1360,
    height: 880,
    minWidth: 1000,
    minHeight: 640,
    show: false,
    title: APP_NAME,
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 16, y: 18 },
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#262624" : "#FAF9F5",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: true,
    },
  });
  state.window = win;

  win.once("ready-to-show", () => {
    if (!SELFTEST) win.show();
  });
  win.on("closed", () => {
    if (state.window === win) state.window = null;
  });

  const wc = win.webContents;
  // External links open in the default browser; backend files are downloaded.
  wc.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(backendOrigin())) wc.downloadURL(url);
    else if (/^(https?:|mailto:)/i.test(url)) shell.openExternal(url).catch(() => undefined);
    return { action: "deny" };
  });
  wc.on("will-navigate", (event, url) => {
    if (isInternalUrl(url)) return;
    event.preventDefault();
    if (url.startsWith(backendOrigin())) wc.downloadURL(url);
    else if (/^(https?:|mailto:)/i.test(url)) shell.openExternal(url).catch(() => undefined);
  });
  // The renderer sets a `beforeunload` guard while a note / YAML draft is unsaved
  // (src/lib/drafts.ts). Without this handler Electron would silently ignore ⌘R and the
  // window close; ask instead, and let the user leave explicitly.
  wc.on("will-prevent-unload", (event) => {
    if (SELFTEST) {
      event.preventDefault();
      return;
    }
    // showMessageBoxSync returns the index of the clicked button.
    const leave = dialog.showMessageBoxSync(win, {
      type: "question",
      buttons: ["Quitter sans enregistrer", "Continuer l'édition"],
      defaultId: 1,
      cancelId: 1,
      message: "Des modifications ne sont pas enregistrées.",
      detail: "Une note ou un fichier YAML a été modifié sans être enregistré : quitter cette page les perdra.",
    });
    if (leave === 0) event.preventDefault();
  });

  wc.on("did-finish-load", () => {
    const url = wc.getURL();
    if (!SELFTEST || url.startsWith("data:")) return;
    selftestLog(`page chargée : ${url}`);
    setTimeout(async () => {
      if (state.selftestDone || win.isDestroyed()) return;
      try {
        // Set by the renderer once React is mounted and /api/health answered with the token.
        const ready = await wc.executeJavaScript("Boolean(document.querySelector('[data-app-ready]'))", true);
        finishSelftest(ready === true, "élément [data-app-ready] absent après le chargement (interface non montée ou backend injoignable depuis le renderer)");
      } catch (err) {
        finishSelftest(false, `évaluation impossible dans la page (${err && err.message ? err.message : err})`);
      }
    }, SELFTEST_READY_DELAY_MS);
  });
  wc.on("did-fail-load", (_event, code, description, url, isMainFrame) => {
    if (!isMainFrame || code === -3 /* aborted by a new navigation */) return;
    if (SELFTEST) finishSelftest(false, `chargement impossible de ${url} (${description})`);
  });
  wc.on("render-process-gone", (_event, details) => {
    if (SELFTEST) finishSelftest(false, `process de rendu arrêté (${details.reason})`);
  });

  win.loadURL(dataUrl(loadingPage())).catch(() => undefined);
  return win;
}

function loadRenderer() {
  const win = state.window;
  if (!win || win.isDestroyed()) return;
  if (DEV_URL) {
    win.loadURL(DEV_URL).catch(() => undefined);
    return;
  }
  const index = path.join(__dirname, "..", "dist", "index.html");
  if (!fs.existsSync(index)) {
    showError({ title: "Interface non compilée", message: `Fichier introuvable : ${index}`, kind: "no-build" });
    return;
  }
  win.loadFile(index).catch(() => undefined);
}

/** Remote mode: check the server answers /api/health, then load the UI (no backend). */
async function bootRemote() {
  if (state.starting) return;
  state.starting = true;
  try {
    if (state.window && !state.window.isDestroyed()) {
      await state.window.loadURL(dataUrl(loadingPage("Connexion au serveur distant…"))).catch(() => undefined);
    }
    await stopBackend();
    selftestLog(`mode distant : ${state.remote.apiBase}`);
    const res = await fetchJson(`${state.remote.apiBase}/api/health`, state.remote.token, REMOTE_TIMEOUT_MS);
    if (res.status !== 200 || !res.body || res.body.ok !== true) {
      throw new StartupError("remote", `${state.remote.apiBase} ne répond pas comme un backend Watermark Lab (HTTP ${res.status}).`);
    }
    state.starting = false;
    loadRenderer();
  } catch (err) {
    showError({
      title: "Serveur distant injoignable",
      message: err && err.message ? err.message : String(err),
      kind: "remote",
    });
  } finally {
    state.starting = false;
  }
}

/** Resolve workspace (if needed), start the backend, wait for health, load the UI. */
async function boot({ forceDialog = false } = {}) {
  if (state.mode === "remote") return bootRemote();
  if (state.starting) return;
  state.starting = true;
  try {
    if (!state.workspace || forceDialog) {
      const ws = await resolveWorkspace({ forceDialog });
      if (ws) state.workspace = ws;
    }
    if (!state.workspace) {
      showError({
        title: "Aucun workspace sélectionné",
        message: "Watermark Lab a besoin du dossier du dépôt pour démarrer son backend.",
        kind: "no-workspace",
      });
      return;
    }
    selftestLog(`workspace : ${state.workspace}`);
    if (state.window && !state.window.isDestroyed()) await state.window.loadURL(dataUrl(loadingPage())).catch(() => undefined);
    await stopBackend();
    spawnBackend();
    selftestLog(`backend lancé sur le port ${state.port}`);
    await waitForHealth();
    selftestLog("backend prêt (/api/health)");
    state.starting = false;
    loadRenderer();
  } catch (err) {
    const kind = err instanceof StartupError ? err.kind : "unknown";
    await stopBackend();
    showError({
      title: kind === "venv" ? "Environnement Python manquant" : "Le backend n'a pas pu démarrer",
      message: err && err.message ? err.message : String(err),
      kind,
    });
  } finally {
    state.starting = false;
  }
}

async function restartBackend() {
  if (!state.window || state.window.isDestroyed()) createWindow();
  await boot();
}

async function changeWorkspace() {
  const ws = await resolveWorkspace({ forceDialog: true });
  if (!ws) return;
  state.workspace = ws;
  // Choosing a workspace means running its backend: leave the remote mode if needed.
  if (state.mode === "remote") {
    saveConfig({ mode: "local" });
    state.mode = "local";
    state.remote = { apiBase: "", token: "" };
    buildMenu();
  }
  await boot();
}

// ---------------------------------------------------------------------------
// Remote server (§12.6): connection dialog, mode switching
// ---------------------------------------------------------------------------

/** GET `url` with the token header; resolves {status, body} (body parsed as JSON when possible). */
async function fetchJson(url, token, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const headers = { Accept: "application/json" };
    if (token) headers["X-WMLab-Token"] = token;
    const res = await fetch(url, { headers, signal: ctrl.signal });
    const text = await res.text();
    let body = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      body = text;
    }
    return { status: res.status, body };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Validate a remote connection: /api/health must answer {ok: true}, then /api/me with the
 * token must return the user (an older backend without /api/me is accepted when /api/health
 * returned `workspace`, only sent to an authenticated request).
 */
async function validateRemote(apiBase, token) {
  let health;
  try {
    health = await fetchJson(`${apiBase}/api/health`, token, REMOTE_TIMEOUT_MS);
  } catch {
    return { ok: false, error: `Serveur injoignable : ${apiBase}` };
  }
  if (health.status !== 200 || !health.body || health.body.ok !== true) {
    return { ok: false, error: `${apiBase} ne répond pas comme un backend Watermark Lab (HTTP ${health.status}).` };
  }
  let me;
  try {
    me = await fetchJson(`${apiBase}/api/me`, token, REMOTE_TIMEOUT_MS);
  } catch {
    return { ok: false, error: "Le serveur n'a pas répondu à la vérification du jeton." };
  }
  if (me.status === 200 && me.body && typeof me.body.name === "string") return { ok: true, name: me.body.name, role: me.body.role };
  if (me.status === 404 && typeof health.body.workspace === "string") return { ok: true, name: "admin", role: "admin" };
  if (me.status === 401 || me.status === 403 || me.status === 404) {
    return { ok: false, error: "Jeton refusé par le serveur. Vérifiez-le ou demandez-en un nouveau à un administrateur (wmlab users add)." };
  }
  const detail = me.body && typeof me.body === "object" && typeof me.body.detail === "string" ? me.body.detail : `HTTP ${me.status}`;
  return { ok: false, error: `Vérification du jeton impossible : ${detail}` };
}

function connectPage() {
  const c = palette();
  return `<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Se connecter à un serveur distant</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; form-action 'none'">
<style>
  html,body{margin:0;height:100%;background:${c.bg};color:${c.text};font:13.5px/1.5 system-ui,-apple-system,BlinkMacSystemFont,sans-serif;-webkit-font-smoothing:antialiased}
  main{padding:22px 24px 20px;user-select:text}
  h1{font:400 22px/1.2 ui-serif,"New York",Georgia,serif;margin:0 0 6px}
  p.lead{color:${c.muted};margin:0 0 16px;font-size:13px}
  label{display:block;font-weight:500;font-size:13px;margin:12px 0 5px}
  input{width:100%;box-sizing:border-box;height:34px;padding:0 10px;border:1px solid ${c.border};border-radius:10px;background:${c.surface};color:${c.text};font:13px ui-monospace,"SF Mono",Menlo,monospace;outline:none}
  input:focus{border-color:${c.accent}}
  .hint{color:${c.muted};font-size:12px;margin-top:5px}
  #status{min-height:20px;margin-top:12px;font-size:12.5px;color:${c.muted}}
  #status.error{color:${c.danger}}
  #status.ok{color:${c.accent}}
  .row{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
  button{height:32px;padding:0 14px;border-radius:9px;border:1px solid ${c.border};background:${c.surface};color:${c.text};font:500 13px system-ui;cursor:default}
  button.primary{background:${c.accent};border-color:transparent;color:#fff}
  button:disabled{opacity:.5}
</style></head><body><main>
<h1>Se connecter à un serveur distant</h1>
<p class="lead">Le backend et les données restent sur le serveur de l'équipe ; aucun backend local n'est lancé.</p>
<form id="form" autocomplete="off">
  <label for="url">Adresse du serveur</label>
  <input id="url" type="text" inputmode="url" autocapitalize="off" autocorrect="off" placeholder="https://lab.example.com" spellcheck="false">
  <div class="hint">URL publique du backend Watermark Lab (HTTPS).</div>
  <label for="token">Jeton personnel</label>
  <input id="token" type="password" placeholder="wml_…">
  <div class="hint">Fourni par un administrateur (<code>wmlab users add &lt;nom&gt;</code>). Stocké chiffré sur ce Mac.</div>
  <div id="status" role="status"></div>
  <div class="row"><button type="button" id="cancel">Annuler</button><button type="submit" id="submit" class="primary">Se connecter</button></div>
</form>
</main></body></html>`;
}

/** Small modal dialog: server URL + token, validated by `validateRemote` (see connect-preload.cjs). */
function openConnectDialog() {
  if (state.connectWindow && !state.connectWindow.isDestroyed()) {
    state.connectWindow.focus();
    return;
  }
  const parent = state.window && !state.window.isDestroyed() ? state.window : undefined;
  const win = new BrowserWindow({
    width: 480,
    height: 430,
    parent,
    modal: !!parent,
    show: false,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    title: "Se connecter à un serveur distant",
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#262624" : "#FAF9F5",
    webPreferences: {
      preload: path.join(__dirname, "connect-preload.cjs"),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  state.connectWindow = win;
  win.setMenuBarVisibility(false);
  win.once("ready-to-show", () => win.show());
  win.on("closed", () => {
    if (state.connectWindow === win) state.connectWindow = null;
  });
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event) => event.preventDefault());
  win.loadURL(dataUrl(connectPage())).catch(() => undefined);
}

function closeConnectDialog() {
  const win = state.connectWindow;
  if (win && !win.isDestroyed()) win.close();
  state.connectWindow = null;
}

function installConnectIpc() {
  const fromConnectWindow = (event) => {
    const win = state.connectWindow;
    return !!win && !win.isDestroyed() && event.sender === win.webContents;
  };
  ipcMain.on("wmlab:connect-initial", (event) => {
    event.returnValue = fromConnectWindow(event) ? { apiBase: state.remote.apiBase || (readRemoteConfig() || {}).apiBase || "" } : { apiBase: "" };
  });
  ipcMain.on("wmlab:connect-cancel", (event) => {
    if (fromConnectWindow(event)) closeConnectDialog();
  });
  ipcMain.handle("wmlab:connect", async (event, payload) => {
    if (!fromConnectWindow(event)) return { ok: false, error: "Requête refusée." };
    const apiBase = normalizeApiBase(payload && payload.apiBase);
    const token = payload && typeof payload.token === "string" ? payload.token.trim() : "";
    if (!apiBase) return { ok: false, error: "Adresse invalide : indiquez une URL http(s)." };
    if (!token) return { ok: false, error: "Indiquez votre jeton." };
    const res = await validateRemote(apiBase, token);
    if (!res.ok) return res;
    saveRemoteConfig(apiBase, token);
    state.mode = "remote";
    state.remote = { apiBase, token };
    buildMenu();
    setTimeout(() => {
      closeConnectDialog();
      if (!state.window || state.window.isDestroyed()) createWindow();
      void boot();
    }, 400);
    return { ok: true, name: res.name };
  });
}

/** Menu "Serveur → Utiliser le backend local": back to the workspace backend. */
async function useLocalBackend() {
  closeConnectDialog();
  if (state.mode !== "remote") {
    await boot();
    return;
  }
  saveConfig({ mode: "local" });
  state.mode = "local";
  state.remote = { apiBase: "", token: "" };
  buildMenu();
  if (!state.window || state.window.isDestroyed()) createWindow();
  await boot();
}

/** "Se déconnecter" in remote mode: forget the token (the URL is kept), reopen the dialog. */
async function disconnectRemote() {
  if (state.mode !== "remote") return;
  saveConfig({ token: null, tokenEncrypted: null });
  state.remote = { apiBase: state.remote.apiBase, token: "" };
  if (state.window && !state.window.isDestroyed()) {
    state.window.loadURL(dataUrl(pageShell(`<h1>Déconnecté</h1><p>Le jeton a été oublié. <b>Serveur → Se connecter à un serveur distant…</b> pour vous reconnecter, ou <b>Serveur → Utiliser le backend local</b>.</p>`))).catch(() => undefined);
  }
  openConnectDialog();
}

// ---------------------------------------------------------------------------
// Self-test
// ---------------------------------------------------------------------------

function finishSelftest(ok, reason) {
  if (!SELFTEST || state.selftestDone) return;
  state.selftestDone = true;
  process.stdout.write(ok ? "SELFTEST OK\n" : `SELFTEST FAIL: ${reason}\n`);
  app.isQuitting = true;
  stopBackend().finally(() => app.exit(ok ? 0 : 1));
}

// ---------------------------------------------------------------------------
// Menu
// ---------------------------------------------------------------------------

function buildMenu() {
  const sep = { type: "separator" };
  const template = [
    {
      label: APP_NAME,
      submenu: [
        { role: "about", label: `À propos de ${APP_NAME}` },
        sep,
        { label: "Choisir le workspace…", click: () => void changeWorkspace() },
        {
          label: "Ouvrir le dossier du workspace",
          click: () => state.workspace && shell.openPath(state.workspace),
        },
        sep,
        { role: "services", label: "Services" },
        sep,
        { role: "hide", label: `Masquer ${APP_NAME}` },
        { role: "hideOthers", label: "Masquer les autres" },
        { role: "unhide", label: "Tout afficher" },
        sep,
        { role: "quit", label: `Quitter ${APP_NAME}` },
      ],
    },
    {
      label: "Édition",
      submenu: [
        { role: "undo", label: "Annuler" },
        { role: "redo", label: "Rétablir" },
        sep,
        { role: "cut", label: "Couper" },
        { role: "copy", label: "Copier" },
        { role: "paste", label: "Coller" },
        { role: "pasteAndMatchStyle", label: "Coller et adapter le style" },
        { role: "delete", label: "Supprimer" },
        { role: "selectAll", label: "Tout sélectionner" },
      ],
    },
    {
      label: "Serveur",
      submenu: [
        { label: "Se connecter à un serveur distant…", click: () => openConnectDialog() },
        { label: "Utiliser le backend local", enabled: state.mode === "remote", click: () => void useLocalBackend() },
        sep,
        {
          label: state.mode === "remote" ? `Connecté à ${state.remote.apiBase}` : "Backend local (ce Mac)",
          enabled: false,
        },
      ],
    },
    {
      label: "Affichage",
      submenu: [
        { role: "reload", label: "Recharger" },
        { role: "forceReload", label: "Forcer le rechargement" },
        { role: "toggleDevTools", label: "Outils de développement" },
        sep,
        {
          label: state.mode === "remote" ? "Reconnecter le serveur" : "Redémarrer le backend",
          accelerator: "CmdOrCtrl+Alt+R",
          click: () => void restartBackend(),
        },
        {
          label: "Ouvrir le journal du backend",
          click: () => state.logPath && shell.openPath(state.logPath),
        },
        sep,
        { role: "resetZoom", label: "Taille réelle" },
        { role: "zoomIn", label: "Zoom avant" },
        { role: "zoomOut", label: "Zoom arrière" },
        sep,
        { role: "togglefullscreen", label: "Plein écran" },
      ],
    },
    {
      label: "Fenêtre",
      role: "window",
      submenu: [
        { role: "minimize", label: "Réduire" },
        { role: "zoom", label: "Zoom" },
        sep,
        { role: "front", label: "Tout ramener au premier plan" },
      ],
    },
    {
      label: "Aide",
      role: "help",
      submenu: [
        {
          label: "Contrat d'architecture (ARCHITECTURE.md)",
          click: () => state.workspace && shell.openPath(path.join(state.workspace, "ARCHITECTURE.md")),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

if (!SELFTEST && !app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const win = state.window;
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    if (SELFTEST && app.dock) app.dock.hide();
    if (SELFTEST) {
      setTimeout(() => finishSelftest(false, `délai global de ${SELFTEST_TIMEOUT_MS / 1000} s dépassé`), SELFTEST_TIMEOUT_MS).unref();
    }
    app.setAboutPanelOptions({ applicationName: APP_NAME, applicationVersion: app.getVersion(), copyright: "Studio AI" });
    if (!app.isPackaged && process.platform === "darwin" && app.dock) {
      const icon = path.join(__dirname, "..", "build", "icon.png");
      if (fs.existsSync(icon)) app.dock.setIcon(icon);
    }
    // Deny every permission except clipboard writes and fullscreen.
    session.defaultSession.setPermissionRequestHandler((_wc, permission, callback) => {
      callback(permission === "clipboard-sanitized-write" || permission === "fullscreen");
    });
    // Remote mode is remembered in config.json; an explicit WMLAB_WORKSPACE (dev, render
    // check) and the self-test always run the local backend.
    if (!SELFTEST && !process.env.WMLAB_WORKSPACE) {
      const remote = readRemoteConfig();
      if (remote) {
        state.mode = "remote";
        state.remote = remote;
      }
    }
    buildMenu();
    installSessionIpc();
    try {
      state.port = await freePort();
    } catch (err) {
      process.stderr.write(`Aucun port libre : ${err}\n`);
      if (SELFTEST) finishSelftest(false, "aucun port TCP libre");
      else app.exit(1);
      return;
    }
    createWindow();
    await boot();
  });

  app.on("window-all-closed", () => {
    app.quit();
  });

  app.on("before-quit", () => {
    app.isQuitting = true;
  });

  // Give the backend a chance to stop cleanly (SIGTERM, then SIGKILL after 3 s).
  app.on("will-quit", (event) => {
    const child = state.backend;
    if (child && child.exitCode === null && child.signalCode === null) {
      event.preventDefault();
      stopBackend().finally(() => app.quit());
    }
  });

  process.on("exit", () => {
    const child = state.backend;
    if (child && child.exitCode === null) {
      try {
        child.kill("SIGKILL");
      } catch {
        /* ignore */
      }
    }
  });
}
