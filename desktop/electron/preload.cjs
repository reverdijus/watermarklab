/**
 * Preload (sandboxed): exposes the backend URL, the session token and the connection mode
 * as `window.wmlab`, plus three commands of the "Serveur" menu (§12.6). The session is
 * fetched from the main process over a synchronous IPC call (before the renderer runs)
 * rather than read from `additionalArguments`, so the token never appears on a process
 * command line.
 */
const { contextBridge, ipcRenderer } = require("electron");

let sessionInfo = { apiBase: "", token: "", mode: "local" };
try {
  const reply = ipcRenderer.sendSync("wmlab:session");
  if (reply && typeof reply === "object") {
    sessionInfo = {
      apiBase: String(reply.apiBase || ""),
      token: String(reply.token || ""),
      mode: reply.mode === "remote" ? "remote" : "local",
    };
  }
} catch {
  // Main process without the handler (tests): the renderer falls back to its stored session.
}

contextBridge.exposeInMainWorld("wmlab", {
  apiBase: sessionInfo.apiBase,
  token: sessionInfo.token,
  platform: process.platform,
  mode: sessionInfo.mode,
  /** Menu "Serveur → Se connecter à un serveur distant…". */
  openConnectDialog: () => ipcRenderer.send("wmlab:open-connect-dialog"),
  /** Menu "Serveur → Utiliser le backend local". */
  useLocalBackend: () => ipcRenderer.send("wmlab:use-local-backend"),
  /** Remote mode: revoke the session (best effort), forget it, then reopen the connection dialog. */
  disconnect: () => ipcRenderer.send("wmlab:disconnect"),
});
