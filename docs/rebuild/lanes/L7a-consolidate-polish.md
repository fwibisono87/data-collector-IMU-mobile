# Lane L7a: automatic consolidation + UX polish

Found during end-to-end integration testing on `make-proper`. Read first:
- `docs/rebuild/SPEC.md` §5.3 and §6.1;
- `docs/OUTPUT_CONTRACT.md`, especially the consolidation artifacts and §9 (the session ZIP);
- `master_backend/app/export.py`: the manifest builder with its `whole` / `late_pending` /
  `recovery_pending` flags, and the consolidate implementation behind
  `POST /export/{sid}/consolidate`;
- `master_backend/app/api/post_session.py` and `master_backend/app/session_zip.py`;
- `master_frontend/src/components/EndSessionModal.tsx`: `handleConsolidate`, `canConsolidate`,
  `hasPendingPhoneData`, `isWhole`. This is the OLD browser flow you are replacing.

## Hard rules
- The golden contract suites in `master_backend/tests/contract/` must pass UNMODIFIED.
- Consolidation must produce byte-identical artifacts to today. You call the existing
  consolidate code; you do not re-implement it.
- Never change a file listed in OUTPUT_CONTRACT.md except by calling the code that writes it
  today.

## 1. Consolidation in the post-session job (the important part)
**Problem.** In the old flow, when a session was not `whole` (a phone dropped out and later
uploaded its rescue CSV, or late data arrived), the operator pressed "Pull & consolidate".
That ran the consolidate endpoint, and the session ZIP then contained
`data/<sid>_consolidated.csv`, `<sid>_<role>_consolidated.csv`, `<sid>_consolidation.json`,
`<sid>_consolidated_validation.json` and `data/recovery/<device_id>.csv`. The v3 system has no
consolidation path at all, so those artifacts are never produced.

**Behaviour to build:**
- **Decide.** Before the `package` step builds the ZIP, build the manifest and decide whether
  consolidation is needed:
  - If the session is `whole`, it is not needed (exactly like the old UI, which only offered the
    button when not whole).
  - If not whole: it is needed when there is something to merge (a complete, verified recovery
    upload, or late rows) AND the consolidation is not already current. "Current" must be
    decided exactly as the old UI did (`canConsolidate` / `hasConsolidatedData` /
    `hasPendingPhoneData`). Port that logic to Python next to the manifest builder, with a test
    table derived from the TS code.
- **Wait for phones first.** Wait for pending phone data the way the old flow did: up to 60 s
  while `recovery_pending` or `uploads_in_progress` is true, polling every 3 s. Then consolidate
  with whatever has arrived. Later arrivals already trigger an automatic re-package; that
  re-package must re-consolidate when the consolidation is no longer current.
- **New step.** Add the step `consolidate` between `cameras` and `package`:
  - in the post-session record;
  - in `FinalizeStep.name` (update `master_backend/app/api/schema.py`, regenerate
    `schema.json`, then `cd dashboard && npm run gen:api`);
  - in the progress list.

  Status is `skipped` (with detail "session is whole") when not needed. Detail on success is
  e.g. "merged 1 rescue upload, 1,204 late rows".
- **Manual trigger.** Add `POST /api/sessions/{sid}/consolidate`, which runs the consolidate +
  package chain on demand. It returns 409 `busy` while the post job runs and 409 `bad_state`
  while the session is still recording.
- **Notices.** Resolve the "phone rescue upload outstanding" notice once consolidation includes
  that upload.

Tests:
- a not-whole session where a verified rescue upload arrives within 60 s: the ZIP contains the
  consolidated CSVs, `consolidation.json`, `consolidated_validation.json` and
  `data/recovery/<device_id>.csv`, byte-identical to calling the old consolidate endpoint
  directly. Build the expected bytes by calling it in a parallel tmp dir with the same frozen
  clock;
- a whole session: `consolidate` is skipped and the ZIP is unchanged;
- a rescue upload arriving after packaging: re-consolidate and re-package automatically;
- the manual endpoint, including both 409 cases;
- the staleness table.

## 2. Done view (dashboard)
- Show the `consolidate` step like the others.
- When the session is not whole, show a "Consolidation" row: its state (current / pending phone
  data / not needed) and a **Consolidate now** button calling the new endpoint. The button is
  disabled while it runs or while the consolidation is current.
- Tests for both states.

## 3. UX polish (all found in the browser on real hardware)
1. **Camera labels on Linux.** A v4l2 device label must be the human card name, not the by-id
   string. Read `/sys/class/video4linux/<videoN>/name` for the device the by-id symlink resolves
   to (e.g. "Integrated Camera"). Keep the by-id path as `device_key`. When two devices share a
   name, append ` (2)`, ` (3)` in device_key order. Windows dshow already gives friendly names;
   leave it.
2. **Disk readiness messages** show the data root (`SSD_PATH`), not the internal `_state` dir.
   Abbreviate long paths in the message to `…/<last two components>`, keeping the full path in
   the Issue's `subject`.
3. **Phone cards:** the ONLINE badge must not overflow when the column is narrow (test at
   1024×768 and 1366×768; the role name may truncate with an ellipsis, the badge must not clip).
4. **Camera device list:** it is fetched once on mount, so a hot-plugged camera needs a reload.
   Re-fetch every 5 s while Setup is visible (pause when the tab is hidden) and add a small
   Refresh button. The backend caches enumeration (≤ 1/s), so this is cheap.
5. **Live chart (Record view):**
   - Y-axis labels are clipped (`1.005` renders as `005`): give the axis enough width, or
     format to 2 decimals;
   - the legend overflows below the panel: make it compact (role swatches only, no
     "Time: --");
   - keep the chart isolated from page renders, as today.

## 4. Done criteria
- Backend: `master_backend/.venv/bin/python -m pytest master_backend/tests -q -m "not slow"`
  passes with zero failures, and the contract suites are unmodified.
- Dashboard: `cd dashboard && npm run lint && npm run typecheck && npm run check:api && npm test && npm run build`
  passes. Commit the rebuilt `master_backend/app/static/`.
- Commit in logical steps with `git commit --no-gpg-sign -m "..."`. Messages explain why, with
  no trailers.
- Final report: the tests, and how "consolidation is current" was ported (cite the TS lines).
