import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The proxy target is configurable so a second checkout can run its own backend
// alongside one that already holds the default port (see .freebuff/run.md).
const apiTarget = process.env.PID_AGENT_API_TARGET ?? "http://127.0.0.1:8000";
const previewPort = Number(process.env.PID_AGENT_PREVIEW_PORT ?? 4173);

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": apiTarget,
    },
  },
  preview: {
    port: previewPort,
    proxy: {
      "/api": apiTarget,
    },
  },
});
