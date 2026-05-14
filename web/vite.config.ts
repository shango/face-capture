import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the SPA runs on 5173 and proxies /api, /health, and /storage
// (LocalStorage signed-URL serving route) to FastAPI on 8000. In prod, the
// built bundle is served directly by FastAPI (see app/main.py spa_catch_all),
// so no proxy is involved.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/health": "http://localhost:8000",
      "/storage": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
