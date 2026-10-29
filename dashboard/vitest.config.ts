import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  define: {
    __BUILD_ID__: JSON.stringify('test'),
  },
  test: {
    environment: 'jsdom',
    environmentOptions: {
      jsdom: {
        // A real origin, so jsdom exposes localStorage (view preferences).
        url: 'http://localhost:3000/',
      },
    },
    setupFiles: ['src/test/setup.ts'],
    restoreMocks: true,
  },
});
