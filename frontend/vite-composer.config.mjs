import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';
export default defineConfig({
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  plugins: [react()],
  build: {
    outDir: resolve(import.meta.dirname, '../ui/vendor/react-composer'),
    emptyOutDir: true,
    sourcemap: false,
    lib: { entry: resolve(import.meta.dirname, 'composer/index.jsx'), formats: ['es'], fileName: () => 'react-composer.js' },
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
