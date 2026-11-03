# Lane L6: operator dashboard (Vite + React + TypeScript)

Read these completely first:
- `docs/rebuild/SPEC.md`: §1.1 (what went wrong and why), §5 (the operator flow, which is your
  primary spec), §10 B1–B5 and C1–C2;
- `docs/rebuild/API.md` (every message and endpoint) and `docs/rebuild/CALIBRATION.md` (the
  calibration UI section);
- `master_backend/app/api/schema.json` (the generated contract).

Then skim the old dashboard in `master_frontend/src/` only to learn what information operators
needed. Copy none of its structure: no state machine in localStorage, no IndexedDB, no
MediaRecorder, no `alert()`, no 900-line page component.

## Scope
- Create a new app in `dashboard/` at the repo root.
- Its production build goes to `master_backend/app/static/`. Commit the build output; the field
  laptop must not need Node.
- Touch nothing else, apart from adding `dashboard/node_modules` and other local artifacts to
  `.gitignore`. Do not modify `master_frontend/` or anything under `master_backend/` other than
  `master_backend/app/static/`.

## Stack
- **Core:** Vite 5, React 18, TypeScript (strict), react-router-dom 6, Tailwind CSS 3, and uPlot
  for the live chart. Nothing else at runtime unless you justify it in the report.
- **Types:**
  - `npm run gen:api` generates `src/api/types.ts` from `../master_backend/app/api/schema.json`
    with `json-schema-to-typescript`; commit the output.
  - `npm run check:api` regenerates to a temp file and fails if it differs.
  - No hand-written duplicates of API types anywhere.
- **Build:**
  - `vite build` with `outDir: ../master_backend/app/static` and `emptyOutDir: true`, hashed
    assets under `assets/`;
  - the git short sha is injected as `__BUILD_ID__` and shown in the top bar;
  - total gzipped JS stays under 250 kB.
- **Tests:** `vitest` + `@testing-library/react` + `jsdom`. Lint: ESLint (react-hooks rules on)
  and `tsc --noEmit`.
- **Scripts:** `dev` proxies `/api`, `/ws` and `/health` to `http://127.0.0.1:8000`;
  `dev:mock` runs with the mock transport (below); also `build`, `test`, `lint`, `typecheck`,
  `gen:api` and `check:api`.

## Architecture
- **Transport** (`src/api/transport.ts`): an interface with `getState()`, the REST commands and
  `subscribe(onMessage)`. Two implementations:
  - `HttpTransport` (fetch + WebSocket `/ws/ui`). Reconnects with backoff from 0.5 s up to
    5 s, and shows a top "Reconnecting…" bar while down. When it reconnects it simply takes the
    fresh snapshot; there is no reconciliation logic.
  - `MockTransport`, an in-memory backend with scripted scenarios, selectable in `dev:mock`
    from a small floating dev panel:
    1. idle, 3 phones (one at 72 Hz), 2 cameras, empty draft;
    2. ready to start;
    3. recording with label changes and one phone going offline;
    4. finalizing with steps progressing;
    5. done PASS with ZIP;
    6. done PARTIAL with a late-data notice;
    7. camera lost mid-session;
    8. calibration with one camera MOVED.

    Mock camera previews are canvas-generated test patterns. The mock implements the
    readiness rules from SPEC §5.1.
- **Store** (`src/state/`): a tiny external store, read with `useSyncExternalStore` and a
  selector hook `useSnapshot(sel)`, holding `{connected, snapshot}`.
  - `live` messages go to a SEPARATE store that only the chart component subscribes to.
  - At 10 Hz, live data must not cause re-renders anywhere else. Prove this with a test that
    counts renders of the page shell.
- **Browser storage:** none for session data. `localStorage` holds only view preferences
  (overlay opacity, overlay mode, chart visible), wrapped in try/catch.
- **Error boundaries:** every panel is wrapped in `<PanelBoundary name>`, which shows
  "This panel failed — Reload panel" while the rest keeps working. Unknown WS message types and
  unknown fields are ignored.
- **Routing:** the view follows backend state, not clicks.
  - `/` decides from the snapshot: IDLE goes to Setup unless `last_session_id` should show Done
    (see below); RECORDING goes to Record; FINALIZING or VALIDATING goes to Done for
    `session.session_id`.
  - Other routes: `/sessions`, `/sessions/:sid` (the same Done view for any session) and
    `/calibration`.
  - Every open browser therefore shows the same thing.
- **Done view and acknowledgement:** after a session finishes, `/` keeps showing Done for
  `last_session_id` until the operator presses **New session**. That choice is held in memory
  only (component state). A reload shows Setup when the state is IDLE, which is fine because
  the data is already on disk. Never ask the operator to download anything.

## Screens
Lay everything out for a 1366×768 laptop with no page scroll in Setup or Record. Dark theme,
high contrast, large type for status. Every control has a visible label.

**Top bar (always visible):**
- state pill: IDLE, REC with elapsed time, FINALIZING, ERROR;
- subject/tag while recording;
- phones online count;
- cameras live/selected;
- disk free;
- reconnecting indicator;
- links to Sessions and Calibration;
- build id.

**Setup (`SPEC §5.1`):** three columns.
1. **Session form.** Subject, Tag and Operator. Debounced 400 ms `PUT /api/session/draft`.
   Inputs reflect the server draft, but never overwrite the field the user is typing in.
2. **Phones.** One card per phone: role in large text, online/offline, true Hz with a red
   badge below 90, packets, app version, telemetry age. A ⋯ menu offers "Reset phone
   connections" behind a confirm dialog; it is disabled while recording.
3. **Cameras.**
   - a device checklist (at most 5, disabled while recording; `PUT /api/cameras/selection`);
   - a preview tile per selected camera: `<img src=/api/cameras/{cam}/preview.mjpg?w=480&fps=10>`,
     with the state, resolution, fps and drift badge;
   - **Take reference stills**: `POST /api/calibration/references`, then a dialog with
     thumbnails and a "Make active" button;
   - **Calibrate** → `/calibration`.

Under the columns sits the **Readiness** list:
- blockers in red, each with its message;
- warnings in amber;
- the START button: disabled while blockers exist; labelled "Start anyway" when only warnings
  exist; otherwise "Start session". It sends `POST /api/session/start`, and a 409 shows the
  returned blockers inline.

**Record (`SPEC §5.2`):**
- a large elapsed timer and subject/tag;
- **Label panel:**
  - the active label huge, with its own elapsed time;
  - a 0–50 grid of large buttons;
  - keyboard: digits accumulate in an input buffer shown on screen; Enter applies; Esc or `0`
    then Enter goes back to 0; Backspace edits;
  - shortcuts are disabled while a text input is focused;
  - optimistic pending state until the REST call resolves, then failure shown inline.
- a phone strip (online, offline with mm:ss and the message "data is buffering on the phone —
  do not stop yet", true Hz);
- a camera strip of small previews (`w=240&fps=5`) with REC dot, fps and lost/reconnecting
  state;
- the live acc-magnitude chart (uPlot, last 10 s, one series per phone, isolated store);
- **STOP**: a press-and-hold button (1 s, with a fill animation) that also responds to the
  keyboard (hold Space). On release before 1 s nothing happens. On completion it sends
  `POST /api/session/stop`.

**Done (`SPEC §5.3`):**
- the step list from `finalize` (FinalizeJob steps + `cameras` + `package`) with status icons,
  durations and error text;
- the integrity verdict with per-phone reasons, rendered safely from the report object (it can
  be partial, so tolerate missing keys);
- the session folder path and ZIP path with Copy buttons, **Open folder**
  (`POST /api/sessions/{sid}/open-folder`, and if that returns 501, say "open it manually"),
  and an optional **Download ZIP** link;
- late data and uploads as inline rows from `GET /api/sessions/{sid}`, refreshed when a
  `finalize` message arrives or every 5 s while this view is open;
- the session's notices with Dismiss;
- **New session**.

**Sessions (`SPEC §5.4`):** a paginated table of `SessionSummary` rows (newest first). A row
opens `/sessions/:sid`. Above the table, a notices panel lists every notice with Dismiss.

**Calibration (`CALIBRATION.md`, UI section):**
- a reference picker with thumbnails, name, date and active badge, plus rename, activate and
  delete (with confirm);
- a camera panel grid: 1 large panel, or 2×2/2×3, and click to make one full-screen;
- each panel shows the live preview (`w=960&fps=10`) with the reference image overlaid, either
  the photo or `edges.png` when "Edges only" is on;
- **Overlay controls:**
  - opacity slider (default 50 %);
  - mode: Blend (alpha), Difference (`mix-blend-mode: difference`), Blink (swap every 500 ms;
    Space toggles manually) or Side by side;
  - grid/crosshair toggle;
  - global settings with a per-panel override.
- **Verdict badge** from `calibration.drift` messages: send `calibration.watch` on mount and
  `unwatch` on unmount. It shows OK, CHECK, MOVED, NO SIGNAL, MISSING or RESOLUTION CHANGED,
  with Δx/Δy and an arrow pointing in the direction of the shift.
- buttons: "Take new reference" and "Make active".
- read-only during recording: no selection changes, but taking stills is allowed.

## Tests (must pass: `npm run lint && npm run typecheck && npm run check:api && npm test && npm run build`)
- **Store:** snapshot replacement; the live store isolated from page renders (render count
  test).
- **Transport:** with a fake WebSocket, verify reconnect backoff, the reconnecting bar, the
  fresh snapshot after reconnect, and that unknown message types are ignored.
- **Readiness/START:** blockers disable START; warnings-only shows "Start anyway"; a 409 shows
  the blockers.
- **Label panel:** keyboard buffer, Enter, Esc, shortcuts disabled while typing in an input,
  pending state, failure message.
- **STOP:** releasing before 1 s sends nothing; a full hold sends once; a double hold does not
  send twice.
- **Done view:** renders from a partial integrity report without crashing; Open folder 501
  shows the fallback text.
- **Calibration:** mode switching, the opacity slider applied to the overlay, watch/unwatch sent
  on mount/unmount, badge text per verdict.
- **Error boundary:** a throwing panel leaves the other panels visible.
- **Routing:** RECORDING in the snapshot → Record view regardless of URL `/`; FINALIZING → Done.
- Run the mock app in a browser yourself (`npm run dev:mock`) and fix every console error or
  warning. Report which scenarios you checked.

## Deliverable
- Logical commits with `git commit --no-gpg-sign`. Messages explain why, with no trailers. The
  final commit contains the production build in `master_backend/app/static/`.
- `dashboard/README.md`: scripts, the mock scenarios, how types are generated, and the rules
  (no browser state, isolated live store, panel boundaries).
- Final report:
  - test output;
  - bundle size;
  - screenshots are not needed, but list the mock scenarios you checked;
  - anything in API.md you found ambiguous.
