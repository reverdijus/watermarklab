/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend base URL when the renderer runs in a plain browser through `vite` (dev only). */
  readonly VITE_API_BASE?: string;
  /** Backend token matching VITE_API_BASE (dev only). */
  readonly VITE_API_TOKEN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

interface WmlabBridge {
  apiBase: string;
  token: string;
  platform: string;
  /** "local": backend started by the app; "remote": server chosen through the "Serveur" menu (§12.6). */
  mode: "local" | "remote";
  /** Opens the main-process dialog "Se connecter à un serveur distant…". */
  openConnectDialog: () => void;
  /** Switches back to the local backend (menu "Serveur → Utiliser le backend local"). */
  useLocalBackend: () => void;
  /** Remote mode: forgets the stored token, then opens the connection dialog. */
  disconnect: () => void;
}

interface Window {
  /** Exposed by electron/preload.cjs through contextBridge. */
  wmlab?: WmlabBridge;
}
