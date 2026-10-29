# IMU Collector — operator dashboard

Vite 5 + React 18 + TypeScript (strict) + Tailwind 3 + react-router-dom 6 + uPlot.
Built for a 1366×768 field laptop; the production bundle is served by the FastAPI backend
from `master_backend/app/static/` (committed — the laptop needs no Node).

The backend contract is `master_backend/app/api/schema.json` (also documented in
`docs/rebuild/API.md` and `docs/rebuild/CALIBRATION.md`). This app is a pure client of that
contract: it renders the backend's snapshot and sends commands; it owns no session state.

## Scripts

| Script              | What it does                                                                 |
| ------------------- | ---------------------------------------------------------------------------- |
| `npm run dev`       | Dev server on :5173, proxies `/api`, `/health` → `http://127.0.0.1:8000` and `/ws` (ws upgrade) to the real backend. Never run it on :8000. |
| `npm run dev:mock`  | Dev server with the in-memory **MockTransport** (`VITE_MOCK=1` via `.env.mock`). No backend needed. A floating `mock · <scenario>` button (bottom right) switches scenarios; `?scenario=<id>` selects one at load. |
| `npm run build`     | Production build → `../master_backend/app/static/` (`emptyOutDir`). Injects the git short sha as `__BUILD_ID__` (shown in the top bar so a stale cached bundle is visible at a glance). Commit the output. |
| `npm test`          | Vitest + @testing-library/react + jsdom (92 tests).                          |
| `npm run lint`      | ESLint (flat config) with `react-hooks` rules.                               |
| `npm run typecheck` | `tsc --noEmit`.                                                               |
| `npm run gen:api`   | Regenerate `src/api/types.ts` from `../master_backend/app/api/schema.json`.    |
| `npm run check:api` | Regenerate to a temp file and fail if it differs from the committed types.     |

The full gate before committing:
`npm run lint && npm run typecheck && npm run check:api && npm test && npm run build`
(total gzipped JS must stay under 250 kB — currently ≈ 98 kB).

## Type generation

`src/api/types.ts` is **generated, committed, never hand-edited**
(`scripts/gen-api.mjs` wraps `json-schema-to-typescript`; ESLint/`tsc` skip-lint it via config).
When the backend contract changes: pull the new `schema.json`, run `npm run gen:api`, fix the
compile errors, commit types and fixes together. `npm run check:api` (part of the gate) fails if
the committed types ever drift from the schema. There are no hand-written duplicates of API
types anywhere in `src/`.

## Mock scenarios (`dev:mock` and `src/scenarios.test.tsx`)

MockTransport implements the readiness rules of SPEC §5.1 and the command semantics of
API.md (409 `blocked`/`bad_state`, STOP idempotency, notice dismissal, camera selection
limits). Camera previews/stills are canvas test patterns via `mock://` URLs — the browser
never touches `getUserMedia` (cameras belong to the backend).

1. **idle** — 3 phones (one at 72 Hz → warning), 2 cameras, empty draft → START blocked.
2. **ready** — draft filled, everything green → START → RECORDING.
3. **recording** — scripted label changes at 4/9/16 s; `waist` phone drops at 12 s
   (offline chip + "data is buffering" guidance) while recording continues.
4. **finalizing** — finalize begins 1 s after load; steps progress, session lands on disk.
5. **done-pass** — finished PASS session with ZIP; history rows in `/sessions`.
6. **done-partial** — PARTIAL verdict, late-data rows, outstanding rescue upload, notices.
7. **camera-lost** — cam2 lost at 4 s ("lost — reconnecting", notice), back at 11 s
   (notice auto-resolves); recording survives.
8. **calibration** — two reference sets, cam2 verdict MOVED; `calibration.watch` pushes
   2 Hz drift messages to the badges.

`src/scenarios.test.tsx` mounts the full App under each scenario and fails on any
`console.error`/`console.warn`; the same pass was done manually in Chrome.

## Rules (why this codebase looks the way it does)

The old dashboard died as a whole page at the worst moments and kept session state in the
browser. These rules are load-bearing (SPEC §1.1, lane spec `docs/rebuild/lanes/L6-dashboard.md`):

- **No browser state for session data.** No localStorage session machines, no IndexedDB, no
  MediaRecorder, no `alert()`. `localStorage` holds *view preferences only*
  (`imu.view.overlayOpacity`, `overlayMode`, `overlayEdges`, `chartVisible`), all wrapped in
  try/catch (`src/state/prefs.ts`). Everything else lives on the server; a reload or a second
  browser shows the same truth.
- **Snapshots are replaced wholesale.** The `ui` store holds `{connected, snapshot}`; a
  `snapshot` message replaces it entirely — no patching, no reconciliation, ever. Unknown WS
  message types and unknown fields are ignored (forward compatibility).
- **Isolated high-rate stores.** 10 Hz `live` IMU samples go to their own store that ONLY the
  chart subscribes to (imperatively, via `setData`); 2 Hz `calibration.drift` goes to a store
  only the verdict badges read. Neither ever re-renders the page — proven by the render-count
  test in `src/state/store.test.tsx`.
- **Every panel is wrapped in `<PanelBoundary>`.** A render error inside one panel shows
  "This panel failed — Reload panel"; the rest of the page (REC timer, STOP, labels) keeps
  working. The top bar and reconnect bar have their own boundaries.
- **Views follow backend state, not clicks.** `/` renders Record while RECORDING, Done while
  FINALIZING/VALIDATING, and Setup when IDLE. After a session finishes, Done stays until the
  operator presses **New session** — an in-memory acknowledgement only; a reload shows Setup,
  which is fine because the data is already on disk. The operator is never asked to download
  anything.
- **Transport abstraction.** Views only talk to the `Transport` interface
  (`src/api/transport.ts`); `HttpTransport` (fetch + WS with 0.5→5 s backoff) and
  `MockTransport` are interchangeable. Errors surface inline as `ApiError` messages —
  a 409 `blocked` on START shows the returned blockers next to the button.

## Layout

```
src/
  api/        transport.ts (interface + HttpTransport), mock.ts, errors.ts, types.ts (generated)
  state/      store.ts (Store/createApp/useSnapshot/AppProvider), prefs.ts
  components/ TopBar, ReconnectBar, PanelBoundary, ConfirmDialog, CamPreview, VerdictBadge
  views/      Home (state switch), SetupView + setup/, RecordView + record/,
              DoneView, SessionsView, CalibrationView
  dev/        DevPanel + mockContext (scenario picker; only rendered under dev:mock)
  test/       setup.ts (jsdom shims), helpers.tsx (fixtures, stubTransport, renderWithApp)
```
