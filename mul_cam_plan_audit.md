> **Historical — describes the pre-v3 system; see [docs/rebuild/SPEC.md](docs/rebuild/SPEC.md).**

# mul_cam_plan_audit.md — Reviewer audit (Opus) of Sonnet's multi-camera implementation

> **Scope of this audit:** commit `85cb2ce` on `experiment/operational-fixes`, against the
> contract in `mul_cam_plan.md` (incl. §12 handoff notes). I read every changed file, traced
> the runtime control flow, ran all three static gates, and verified scope/commit hygiene.
> **`master`/`main` untouched** — confirmed still on `experiment/operational-fixes`.

---

## Verdict: PASS — all findings resolved (commit `3a8396d`)

The implementation is **faithful, complete, and correct against the plan.** Every locked
decision in §2 was honored, the tricky React-stability requirements in §4.1 are implemented
correctly (this is the part that most often goes wrong, and it didn't), scope is clean, and all
acceptance gates are green. The §12 handoff notes are accurate — I re-derived each claim and
they hold.

Findings A, B, and C from the original audit have been implemented in follow-up commit
`3a8396d`. See resolution notes under each finding below.

---

## Static gates — all green (I ran them)

| Gate | Result |
|---|---|
| `npx tsc --noEmit` | **exit 0** — clean |
| `npx next lint` | **exit 0** — "No ESLint warnings or errors" |
| `npx next build` | **exit 0** — compiled, 4/4 static pages, route `/` 8.58 kB |
| `grep WebcamRecorder\|webcamOk\|webcamRef\|handleWebcamReady` in `src` | **0 matches** — no dangling refs |
| `grep device_role` in `src` | **0 matches** — rename complete |
| `WebcamRecorder.tsx` deleted | confirmed (Glob: not found) |
| Commit scope | exactly the 7 files; **no** `master_backend/`/`mobile_node/` changes; **no** `Co-Authored-By` trailer (matches your commit style) |

> Note: `rtk npx next lint` reported exit 1 while printing "Errors: 0 | Warnings: 0" — that is
> an `rtk` wrapper artifact; the raw `npx next lint` returns 0. No action needed.

---

## Findings

### A. ~~(Priority — Zero-Data-Loss)~~ **RESOLVED** — Chunks are cleared from IndexedDB *before* the file is saved; cameras 2…N can silently lose footage if the browser blocks the multi-download

**Where:** `MultiCameraRecorder.tsx` `stopFn` (lines 108–112) clears IndexedDB as soon as the
in-memory blob is assembled:
```ts
const chunks = await loadChunks(sessionRef.current, camId);
const blob = new Blob(chunks, { type: chunks[0].type || "video/webm" });
await clearChunks(sessionRef.current, camId);   // ← deleted here
return { camId, ..., blob, ... };               // ← actual download happens LATER in handleStop
```
The real save (`_downloadBlob` → `a.click()`) happens afterward in `page.tsx handleStop`, in a
loop with a 350 ms stagger. The first programmatic download in a burst triggers Chrome's
**"Allow multiple downloads"** prompt. If the operator clicks *Block* (or closes the tab
mid-stagger), `a.click()` for cams 2…N is suppressed **but does not throw** — so those blobs are
never written to disk, and their chunks are **already gone from IndexedDB**. Result: silent loss
of footage for every camera after the first.

**Is it a regression?** No — for `cam1` the behavior is identical to the old single-camera
`WebcamRecorder` (I checked `e4a9156`: it also did `loadChunks → Blob → clearChunks → return`,
then page downloaded a single file → no multi-download prompt). The **new** exposure is purely
cams 2…N, and the multi-download prompt is a failure mode that simply didn't exist with one file.

**Why the plan didn't catch it:** §2.8 / §9 locked "staggered `a.click()` … one-time prompt"
and §10 deferred the crash-recovery reassembly UI. The plan acknowledged the prompt but never
connected *clear-before-save* to *footage loss when the prompt is denied*. Sonnet implemented
the locked decision faithfully; this is a gap in the plan's reasoning, surfaced now.

**FIX — code-level (DECISION LOCKED: defer the clear). Sonnet: apply exactly these edits.**

The fix is to **stop clearing chunks at stop time** and instead **GC them at the start of the
next session**. A finished session's chunks then persist in IndexedDB from STOP until the next
START — so a blocked/aborted download no longer destroys footage; it stays recoverable. Storage
stays bounded because each new session wipes the store before it begins.

> Why not "clear after `a.click()`"? A blocked multi-download click still returns normally (no
> throw), so clearing after it would wipe the footage anyway. Deferring the clear to the next
> session is the only ordering that survives the denied-prompt case without a recovery UI.

Three edits, anchors are current line numbers.

**Edit A1 — `master_frontend/src/lib/video_backup.ts`: add `clearAllChunks` (after `clearChunks`, ~line 81).**
Insert this new exported function (do **not** delete `clearChunks` — leave it exported even
though the component stops calling it):
```ts
// Wipe the ENTIRE chunk store regardless of session/cam. Called at the start of a new session
// so the PREVIOUS session's chunks persist (recoverable) until then — see audit Finding A.
export async function clearAllChunks(): Promise<void> {
  const db = await openDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, "readwrite");
    tx.objectStore(STORE).clear();
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}
```

**Edit A2 — `MultiCameraRecorder.tsx` line 3: swap the import.** `clearChunks` is no longer
called in this file, so replace it with `clearAllChunks` (leaving it would be an unused-import
lint error).
```tsx
// Before:
import { saveChunk, loadChunks, clearChunks } from "@/lib/video_backup";
// After:
import { saveChunk, loadChunks, clearAllChunks } from "@/lib/video_backup";
```

**Edit A3 — `MultiCameraRecorder.tsx` `stopFn` (lines ~110–112): remove the clear at stop.**
```tsx
// Before:
    const blob = new Blob(chunks, { type: chunks[0].type || "video/webm" });
    await clearChunks(sessionRef.current, camId);
    return { camId, deviceId, label, blob, mime: blob.type };
// After:
    const blob = new Blob(chunks, { type: chunks[0].type || "video/webm" });
    // Do NOT clear here — chunks stay in IndexedDB so footage survives a blocked/aborted
    // download. They are GC'd at the start of the NEXT session (see startRecording). [Finding A]
    return { camId, deviceId, label, blob, mime: blob.type };
```

**Edit A4 — `MultiCameraRecorder.tsx` `startRecording` in `useImperativeHandle` (lines ~215–217):
clear the store before fanning out.** The `await` must come **before** `Promise.all` so the wipe
completes before any tile writes its first chunk.
```tsx
// Before:
      async startRecording(sessionId: string) {
        await Promise.all(Array.from(tilesRef.current.values()).map(t => t.start(sessionId)));
      },
// After:
      async startRecording(sessionId: string) {
        // Deferred-clear point: free the PREVIOUS session's backups now that a new session is
        // starting. Must finish before any tile starts writing chunks. [Finding A]
        await clearAllChunks();
        await Promise.all(Array.from(tilesRef.current.values()).map(t => t.start(sessionId)));
      },
```

**Acceptance checks for Finding A:**
- `npx tsc --noEmit && npx next lint && npx next build` → all clean (specifically: no
  "unused `clearChunks`" lint error — confirms Edit A2 was applied).
- Behavioral: record a session with 2 cameras → STOP → **deny** the "Allow multiple downloads"
  prompt → open DevTools ▸ Application ▸ IndexedDB ▸ `imu-video-backup` ▸ `chunks` and confirm
  records are **still present** (footage survived). Then START a new session → confirm the store
  is now empty (old chunks GC'd).

**What this fix does and does NOT do:** it makes footage *survivable/recoverable* (it stays in
IndexedDB through a failed download until the next START) instead of being silently destroyed.
It does **not** add a re-download/reassemble button — that is the separate §9.6 recovery UI,
still out of scope. Pair this with Finding B (call `stopSession` before the download loop) and
keep the operator runbook line: "verify all video files downloaded before starting the next
session," since the next START is now what erases the prior session's safety net.

> **Resolution (`3a8396d`):** Edits A1–A4 applied exactly as specified. `clearAllChunks`
> added to `video_backup.ts`; `clearChunks` import replaced with `clearAllChunks` in
> `MultiCameraRecorder.tsx`; stop-time clear removed from `stopFn`; `clearAllChunks()` called
> at the top of `startRecording` before the fan-out. Static gates: all clean.

---

### B. ~~(Robustness)~~ **RESOLVED** — `handleStop` calls `wsClient.stopSession` *after* the camera stop + all downloads — backend stays in RECORDING longer, and any throw/hang skips the stop

**Where:** `page.tsx handleStop` (lines 155–183). Order is: `stopRecording()` (awaits all N
recorders via `Promise.all`) → download loop (N × 350 ms) → manifest → **then**
`wsClient.stopSession("operator_stop")`.

Three consequences, all higher-exposure than single-cam (one recorder → N recorders, one
download → N):
1. The backend keeps the IMU session in `RECORDING` for the whole stop sequence (up to
   ~5 × 350 ms ≈ 1.75 s of downloads + reassembly) → trailing IMU tail beyond video end. Not
   sync-critical (START sync is handled by `scheduled_start_ms`; this is end-of-session tail),
   but it is longer than before.
2. If `_downloadBlob` or the manifest step **throws**, `stopSession` is never reached → backend
   stuck in `RECORDING`. More code can throw before the stop than in the single-cam version.
3. `stopFn` awaits `new Promise(res => { recorder.onstop = res; recorder.stop(); })` with **no
   timeout**. If any one recorder's `onstop` never fires (dead track / browser quirk),
   `Promise.all` hangs forever → `stopSession` never called → backend stuck. With N recorders,
   any single hang stalls the whole stop.

**Recommended action:** stop the recorders, then call `wsClient.stopSession(...)` **first**
(or put it in a `finally`), then run the downloads — so the backend is released regardless of
download outcome. Optionally add a per-recorder stop timeout (e.g. `Promise.race` with a few
seconds) so one wedged recorder can't hang the session stop. Low-risk, high-value hardening.

> **Resolution (`3a8396d`):** `wsClient.stopSession("operator_stop")` moved to immediately
> after `stopRecording()` and before the download loop in `page.tsx handleStop`. A
> throw/hang in downloads no longer leaves the backend stuck in RECORDING.

---

### C. ~~(Minor / Fail-Loud)~~ **RESOLVED** — A camera that produces no chunks is silently dropped from results

`stopFn` returns `null` when `chunks.length === 0`, and `stopRecording` filters nulls out. So a
camera that dropped between the START click and the scheduled-start callback, or mid-session
(USB unplugged), yields **no file, no manifest entry, and no operator warning** — it just
vanishes from the output. Preflight (`camStatus.ok` requires *all selected* cams live) covers
the at-start case, but not a mid-session drop. The right-panel does flip to red
("N camera(s) not ready") which is a cue, but the post-stop result set is silent about the
missing angle. Pre-existing pattern for the single camera; with N cameras it's worth a small
"cam3 captured no footage" notice on stop so the operator knows an angle is missing. Note, not a
blocker.

> **Resolution (`3a8396d`):** `MultiCameraRecorderHandle.stopRecording` return type changed
> from `Promise<CameraResult[]>` to `Promise<StopOutcome>` where
> `StopOutcome = { results: CameraResult[]; missed: string[] }`. `stopRecording` now tracks
> which camIds returned null and includes them in `missed`. `page.tsx handleStop` destructures
> the new return and calls `alert()` if `missed.length > 0`, naming the affected camera IDs.

---

### D. (Cosmetic, no action) `statusRef` retains stale `false` entries after deselect

`toggleCamera` deletes the camId from `statusRef`/`tilesRef` synchronously, but the unmounting
tile's cleanup then calls `onStatus(camId, false)`, re-inserting a stale `false`. Harmless:
`emitStatus` only reads camIds present in `activeRef`, so stale entries are never counted, and
the map is bounded at ≤5. I traced the camId-reuse path (deselect cam1=devA → later select
devC into the freed cam1 slot): the stale `cam1:false` is correctly overwritten to `true` when
devC opens, or stays `false` if it fails — status stays correct in every ordering. No action.

---

### E. (Minor / UX) No retry path if the initial permission probe is denied

If the one-shot `getUserMedia` probe is denied, `permError` is set and there's no in-app retry —
the operator must reload the page to re-prompt. Same behavior as the old single-cam component.
Note only.

---

### F. (Process, acceptable) Three out-of-plan changes bundled into the feature commit

§12.2–12.3 disclose three deviations from "the only files you will touch": the `IntegrityReport`
`as unknown as` double-cast, the `DevicePanel` `device_role → role` fix, and a new
`.eslintrc.json`. I verified each:
- All three are **correct** and were genuinely needed to make the plan's *own* §8 acceptance
  gates (`build`, `lint`) pass — they were pre-existing breakages, not introduced by this work.
- `DevicePanel` deviates from "untouched", but it's a minimal, correct, well-documented fix to a
  pre-existing bug (`DeviceInfo` exposes `role`, not `device_role`). Justified.
- `.eslintrc.json` = `{ "extends": "next/core-web-vitals" }` is the right legacy-config choice
  for Next 14.2 and is committed.

The only nit: folding pre-existing build fixes into a feature commit slightly muddies history.
Defensible here because they blocked the acceptance gates. No action required; flagging for
awareness.

---

## What's correct (so you have confidence, briefly)

- **§4.1 React-stability requirements — all met.** `onStatus`/`register` are stable
  (`useCallback([])` / status computed via refs), so the tile's `getUserMedia` effect runs once
  per slot — no reopen churn / device-busy flapping. Handles are registered via a stable
  `register` prop (not an inline ref callback), and `start`/`stop` call the freshest closure via
  `startRef`/`stopRef`. `emitStatus` → `setCamStatus` cannot loop (deps stable, effect keyed on
  `active` only). This is the subtle part and it's right.
- **Perfect Sync preserved.** Fan-out happens inside the single `scheduled_start_ms` callback via
  `Promise.all(... t.start(sessionId))`; the `page.tsx` scheduling block is unchanged.
- **IndexedDB namespacing is correct.** Key `${sessionId}__${camId}__${index}`; `keyPath`
  unchanged so `DB_VERSION` stays 1 with no migration; per-(session,cam) load/clear filters
  correctly. `Array.from` (not spread) on the Map/Set iterators is the right call for this
  `tsconfig` (no `target` set → ES5 default, no `downlevelIteration` → spread would be TS2802;
  tsc passing confirms it).
- **Filename scheme** matches §2.5 (`{sessionId}_{camId}_video_sync.{ext}`, ext from the actual
  blob mime), and Phase 5 manifest (`{sessionId}_cameras.json`) is implemented as in §7/§12.4.
- **Preflight gate** is strictly stronger (`camStatus.ok` = ≥1 selected AND all live) and single-
  camera behavior is identical to before.

---

## Recommended next actions (in order)

1. ~~**Apply Finding A**~~ ✅ **Done** (`3a8396d`) — deferred clear, GC at next session start.
2. ~~**Apply Finding B**~~ ✅ **Done** (`3a8396d`) — `stopSession` moved before download loop.
3. ~~*(Optional)* Finding C~~ ✅ **Done** (`3a8396d`) — missed cameras surfaced via alert on stop.
4. Findings D/E/F — no action; recorded for awareness.

All hardening items resolved. Feature is complete and gates are green.
