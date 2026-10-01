/**
 * Preload of the "Se connecter à un serveur distant…" dialog (electron/main.cjs). The page
 * itself is a static HTML form without any script (CSP `script-src 'none'`): this preload
 * wires the form to the main process, which checks the URL (`/api/health`), logs in with the
 * e-mail and password (`POST /api/auth/login`) and stores only the session token (§12.6).
 */
const { ipcRenderer } = require("electron");

window.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("form");
  const urlInput = document.getElementById("url");
  const emailInput = document.getElementById("email");
  const passwordInput = document.getElementById("password");
  const status = document.getElementById("status");
  const submit = document.getElementById("submit");
  const cancel = document.getElementById("cancel");
  if (!form || !urlInput || !emailInput || !passwordInput || !status || !submit || !cancel) return;

  try {
    const initial = ipcRenderer.sendSync("wmlab:connect-initial");
    if (initial && typeof initial.apiBase === "string") urlInput.value = initial.apiBase;
  } catch {
    /* no initial value */
  }
  (urlInput.value ? emailInput : urlInput).focus();

  const setStatus = (text, kind) => {
    status.textContent = text;
    status.className = kind || "";
  };

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const apiBase = urlInput.value.trim();
    const email = emailInput.value.trim();
    const password = passwordInput.value;
    if (!apiBase) return setStatus("Indiquez l'adresse du serveur.", "error");
    if (!email) return setStatus("Indiquez votre e-mail.", "error");
    if (!password) return setStatus("Indiquez votre mot de passe.", "error");
    submit.disabled = true;
    setStatus("Connexion…", "");
    try {
      const res = await ipcRenderer.invoke("wmlab:connect", { apiBase, email, password });
      if (res && res.ok) {
        passwordInput.value = "";
        setStatus(`Connecté en tant que ${res.name}.`, "ok");
      } else {
        setStatus((res && res.error) || "Connexion impossible.", "error");
        passwordInput.select();
      }
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
