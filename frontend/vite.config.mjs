import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "node:path";

// Frontend island build — bundles React locally for the session selector only.
// The output is served as a normal /ui static asset and is cache-first within
// the active service-worker generation; no CDN or import map is involved.
export default defineConfig({
  define: {
    "process.env.NODE_ENV": JSON.stringify("production")
  },
  plugins: [react()],
  build: {
    outDir: resolve(import.meta.dirname, "../ui/vendor/react-session-dropdown"),
    emptyOutDir: true,
    sourcemap: false,
    lib: {
      entry: resolve(import.meta.dirname, "session-dropdown/index.jsx"),
      formats: ["es"],
      fileName: () => "react-session-dropdown.js"
    },
    rollupOptions: {
      output: { inlineDynamicImports: true }
    }
  }
});
