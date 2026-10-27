import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// VITE_SITE=pages builds the GitHub Pages site (landing + replay, served under /<repo>/);
// the default build is the console that talks to the API.
const pages = process.env.VITE_SITE === "pages";

export default defineConfig({
  base: pages ? "/sre-blackboard/" : "/",
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: {
    proxy: { "/api": { target: process.env.SRE_API_URL ?? "http://localhost:8000", changeOrigin: true } },
  },
  build: {
    target: "es2022",
    manifest: true,
    rollupOptions: {
      output: {
        // Long-lived vendor chunks: an app change does not invalidate React in the cache.
        manualChunks(id) {
          if (/node_modules\/(react|react-dom|scheduler|react-router)\//.test(id)) return "react";
          if (id.includes("node_modules/@tanstack/react-query") || id.includes("node_modules/@tanstack/query-core")) return "query";
        },
      },
    },
  },
  test: { environment: "jsdom", include: ["src/**/*.test.ts?(x)"] },
});
