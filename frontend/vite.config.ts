import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.VITE_API_PROXY_TARGET || "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      port: 5173,
      // Optional dev proxy: lets you call "/api/..." from the browser without
      // needing CORS at all. The app defaults to VITE_API_BASE_URL instead,
      // which is what a separate deployment uses.
      proxy: {
        "/api": {
          target,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
    build: {
      outDir: "dist",
      sourcemap: false,
      // Split the heavy, rarely-changing libraries out of the app chunk so a
      // code change does not invalidate 880 kB of vendor cache.
      rollupOptions: {
        output: {
          manualChunks: {
            react: ["react", "react-dom", "react-router-dom"],
            charts: ["recharts"],
            maps: ["leaflet", "react-leaflet"],
          },
        },
      },
    },
  };
});
