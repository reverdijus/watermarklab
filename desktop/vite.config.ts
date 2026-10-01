import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/**
 * Content-Security-Policy injected in production builds only (the dev server
 * needs inline scripts for React Fast Refresh). The local backend listens on
 * 127.0.0.1 with a random port; in remote mode (§12.6) the renderer — loaded from
 * file:// by Electron or from another origin in a browser — talks to the team server
 * over http(s), hence the scheme sources for connections and media. Scripts and styles
 * stay restricted to the build itself.
 */
const CSP = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: http: https:",
  "media-src 'self' data: blob: http: https:",
  "connect-src 'self' http: https:",
  "font-src 'self' data:",
  "object-src 'none'",
  "base-uri 'self'",
].join("; ");

function cspPlugin(): Plugin {
  return {
    name: "wmlab-csp",
    apply: "build",
    transformIndexHtml(html) {
      return html.replace(
        "<head>",
        `<head>\n    <meta http-equiv="Content-Security-Policy" content="${CSP}" />`,
      );
    },
  };
}

export default defineConfig({
  // Relative base: the production build is loaded through file:// by Electron.
  base: "./",
  plugins: [react(), tailwindcss(), cspPlugin()],
  server: { port: 5317, strictPort: true, host: "127.0.0.1" },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 1600,
    target: "es2022",
  },
});
