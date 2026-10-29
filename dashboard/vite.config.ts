import { execSync } from 'node:child_process';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The git short sha is stamped into the bundle so the top bar always shows which
// commit the operator is running (2026-08-11 white-screen: stale bundle vs backend).
function gitShortSha(): string {
  try {
    return execSync('git rev-parse --short HEAD').toString().trim();
  } catch {
    return 'dev';
  }
}

export default defineConfig({
  plugins: [react()],
  define: {
    __BUILD_ID__: JSON.stringify(gitShortSha()),
  },
  build: {
    // Production build is served by FastAPI from the backend package; the field
    // laptop must not need Node, so the output is committed to git.
    outDir: '../master_backend/app/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/health': 'http://127.0.0.1:8000',
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
    },
  },
});
