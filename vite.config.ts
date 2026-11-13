import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Assets are served by Django from the app's static directory.
  base: "./",
  define: {
    // Library mode leaves process.env reads for the consumer to resolve, but
    // this bundle runs directly in a browser. Fix the single constant.
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: fileURLToPath(new URL("./chat/static/chat/dist", import.meta.url)),
    emptyOutDir: true,
    copyPublicDir: false,
    license: { fileName: "licenses.md" },
    lib: {
      entry: fileURLToPath(new URL("./frontend/src/main.tsx", import.meta.url)),
      formats: ["es"],
      fileName: () => "chat.js",
      cssFileName: "chat",
    },
    rollupOptions: {
      output: {
        chunkFileNames: "chunks/[name]-[hash].js",
      },
    },
  },
});
