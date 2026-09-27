import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const BACKEND = "http://127.0.0.1:8877";

// DASH_VITE_TUNNEL=1: expose the dev server through a reverse tunnel (e.g. a
// Cloudflare quick tunnel) for remote walkthroughs. Accepts any Host header and
// forwards it unchanged so the backend Origin whitelist (security.local_ports /
// extra origins) can match the tunnel hostname. Off by default: with it on the
// dev server is reachable by any hostname that resolves to this machine.
const TUNNEL = process.env.DASH_VITE_TUNNEL === "1";

// Dev server proxies REST /api → our existing FastAPI backend (server.py on 8877).
// PTY WebSocket traffic connects directly to :8877 from ChatTerminal; routing it
// through Vite's WS proxy caused EPIPE/ECONNRESET stalls during long streams.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5174,
    // Polling watcher: edits made through the Cowork folder mount (the AI dev
    // agent's write path) do not emit fsevents, so chokidar never saw them and
    // Vite kept serving stale transforms. Polling ~20 source files is cheap.
    watch: { usePolling: true, interval: 1000 },
    ...(TUNNEL ? { allowedHosts: true as const } : {}),
    proxy: {
      "/api": { target: BACKEND, changeOrigin: !TUNNEL },
    },
  },
});
