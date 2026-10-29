/// <reference types="vite/client" />

/** Git short sha of the build, injected by vite.config.ts (shown in the top bar). */
declare const __BUILD_ID__: string;

interface ImportMetaEnv {
  /** '1' in `npm run dev:mock` (see .env.mock): use the in-memory MockTransport. */
  readonly VITE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
