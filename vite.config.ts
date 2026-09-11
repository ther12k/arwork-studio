import path from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 3000,
    strictPort: true,
    host: true,
    // The Caddy gateway (:81) forwards the browser's Host header verbatim,
    // so the dev server must not filter by host.
    allowedHosts: true,
  },
  preview: {
    port: 3000,
    strictPort: true,
    host: true,
    allowedHosts: true,
  },
});
