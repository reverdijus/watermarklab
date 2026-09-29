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
}

interface Window {
  /** Exposed by electron/preload.cjs through contextBridge. */
  wmlab?: WmlabBridge;
}
