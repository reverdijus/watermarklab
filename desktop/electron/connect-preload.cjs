/**
 * Preload of the "Se connecter à un serveur distant…" dialog (electron/main.cjs). The page
 * itself is a static HTML form without any script (CSP `script-src 'none'`): this preload
 * wires the form to the main process, which validates the URL + token (`/api/health`, then
 * `/api/me`) and stores them (§12.6).
 */
const { ipcRenderer } = require("electron");

window.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("form");
  const urlInput = document.getElementById("url");
  const tokenInput = document.getElementById("token");
  const status = document.getElementById("status");
  const submit = document.getElementById("submit");
  const cancel = document.getElementById("cancel");
  if (!form || !urlInput || !tokenInput || !status || !submit || !cancel) return;

  try {
    const initial = ipcRenderer.sendSync("wmlab:connect-initial");
    if (initial && typeof initial.apiBase === "string") urlInput.value = initial.apiBase;
  } catch {
    /* no initial value */
  }
  (urlInput.value ? tokenInput : urlInput).focus();

  const setStatus = (text, kind) => {
    status.textContent = text;
    status.className = kind || "";
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const apiBase = urlInput.value.trim();
    const token = tokenInput.value.trim();
    if (!apiBase) return setStatus("Indiquez l'adresse du serveur.", "error");
    if (!token) return setStatus("Indiquez votre jeton.", "error");
    submit.disabled = true;
    setStatus("Vérification…", "");
    try {
      const res = await ipcRenderer.invoke("wmlab:connect", { apiBase, token });
      if (res && res.ok) setStatus(`Connecté en tant que ${res.name}.`, "ok");
      else setStatus((res && res.error) || "Connexion impossible.", "error");
    } catch (err) {
      setStatus(`Connexion impossible : ${err && err.message ? err.message : err}`, "error");
    } finally {
      submit.disabled = false;
    }
  });
  cancel.addEventListener("click", () => ipcRenderer.send("wmlab:connect-cancel"));
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape") ipcRenderer.send("wmlab:connect-cancel");
  });
});
