import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API_TARGET: the mirror-api to proxy /api and /ws to during `npm run dev` / `npm run preview`.
// In production the backend serves dist/ itself, so no proxy is involved.
const target = process.env.API_TARGET || "http://127.0.0.1:8000";
const proxy = {
  "/api": { target, changeOrigin: false },
  "/ws": { target: target.replace(/^http/, "ws"), ws: true },
};

export default defineConfig({
  plugins: [react()],
  server: { host: true, proxy },
  preview: { host: true, port: 4173, proxy },
  build: { target: "safari15", sourcemap: false, assetsInlineLimit: 2048 },
});
