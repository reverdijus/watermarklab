import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { installDraftGuard } from "./lib/drafts";
import { applyThemePreference } from "./lib/theme";
import "./index.css";

applyThemePreference();
// Reload / close asks for confirmation while a note or YAML draft is unsaved.
installDraftGuard();

const root = document.getElementById("root");
if (!root) throw new Error("Élément #root introuvable");

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
