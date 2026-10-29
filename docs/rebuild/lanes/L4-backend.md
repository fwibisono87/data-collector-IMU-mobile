# Lane L4: backend core: v3 API, notices, post-session job and server-side session ZIP

Read these completely first:
- `docs/rebuild/SPEC.md`, `docs/rebuild/API.md`, `docs/OUTPUT_CONTRACT.md` (all of it, and §9 in
  particular);
- `master_backend/app/api/` and `master_backend/app/camera/` from lane L2 (the schema and the
  camera seam);
- `master_backend/app/main.py`, `ws_handler.py`, `session_manager.py`, `finalize_job.py`,
  `export.py`, `upload.py` and `cameras.py`;
- `master_frontend/src/components/EndSessionModal.tsx` (`buildStreamEntries`, `handleDownload`)
  and `master_frontend/src/lib/export_client.ts` (`DATA_KINDS` / `isDataKind`). These are the
  browser code whose ZIP you are moving to the server.

## Hard rules
- **Golden contract.** `master_backend/tests/contract/` must pass UNMODIFIED. Do not change
  `golden/session_snapshot.json`. Do not change the steps of `FinalizeJob`, the ledger format,
  or anything that writes a file listed in OUTPUT_CONTRACT.md §1–§8.
- **Legacy endpoints stay.** Do NOT delete existing endpoints or the old `master_frontend/` yet.
  `/ws/frontend`, `/ws/live`, `/session/*`, `/recovery/*`, `/export/*` and `/cameras/*` all keep
  working; lane L7 removes them later. Everything new is additive.
- **Camera code belongs to L5.** Do not implement camera capture, preview or calibration; lane
  L5 does that in parallel inside `master_backend/app/camera/`. You talk to cameras only
  through the `CameraService` protocol from L2.
- Do not edit `master_backend/app/camera/interface.py` or `fake.py`. If the seam is missing
  something, write it in your final report and work around it locally.

## 1. Idle-telemetry filter (SPEC §6.3)
In `ws_handler.telemetry_ws`, packets with `sequence_number >= 2**40` (`IDLE_SEQ_BASE`, defined
once in `csv_schema.py`):
- still feed the rate and liveness accounting that runs in every state (`increment_packets`,
  `note_sample`, `_latest_samples`);
- are never deduplicated against, never written by `write_packet` (RECORDING path), never
  written by `write_late` (late path), and never trigger `ensure_writer`/`mark_first_packet`.

Tests (`master_backend/tests/test_idle_seq_filter.py`, driving the real handler logic or a
helper you extract from it):
- idle packets during RECORDING and inside the late window do not appear in any CSV;
- they do raise `true_hz` and packet counts;
- session packets (seq < 2^40) are unaffected.

## 2. Service wiring (`master_backend/app/api/runtime.py`)
- A single `Runtime` object created in the app lifespan. It holds the `CameraService` instance,
  the notice store, the draft store and the UI hub.
- Camera service factory: env `IMU_CAMERAS` is one of `auto` (default), `fake` or `off`.
  - `auto`: try `from master_backend.app.camera.service import create_service`; that module is
    written by L5 and may not exist in your branch. If the import fails, fall back to
    `FakeCameraService(n=0)`, with a logged warning.
  - `fake`: `FakeCameraService(n=int(os.getenv("IMU_FAKE_CAMERAS", "2")))`.
  - `off`: `FakeCameraService(n=0)`.
- Mount L5's router the same way: `from master_backend.app.camera.routes import router`, only
  if importable.
- All paths come from the same `SSD_PATH` resolution the existing modules use. State files live
  in `SSD_PATH/Data_Riset_IMU/_state/` and are written atomically (tmp file + fsync + rename).

## 3. Stores
- **Draft** (`_state/draft.json`): `PUT /api/session/draft` merges the fields. After a
  successful START, `subject` is cleared; `session_tag` and `operator` are kept.
- **Notices** (`_state/notices.json`): `NoticeStore.raise_(code, level, message, session_id=None, key=None)`
  is idempotent on `key` and updates the message. `resolve(key)` sets `resolved=True`;
  `dismiss(id)` sets `dismissed_at_ms`. The snapshot lists notices that are neither resolved
  nor dismissed.
  Wire these sources:
  - a FinalizeJob step failed;
  - a package step failed;
  - late delivery received (info, with the row counts);
  - a phone rescue upload outstanding for a finished session (resolved when it completes);
  - a camera reported `track_ended` or `write_error` during the session;
  - video validation failed (a report with `ok=False` or `ebmlHeaders != 1`);
  - disk free below 20 GB (warn) or 5 GB (error), resolved when it recovers.

## 4. Post-session job (`master_backend/app/api/post_session.py`)
This job is separate from FinalizeJob so the frozen ledger is untouched. It has its own record,
`_state/sessions/<sid>.post.json`, with steps `cameras` and `package`, each holding status,
timestamps and detail.
- **STOP.** On a successful STOP via `POST /api/session/stop`, call
  `session_manager.stop_recording(reason)` exactly as the legacy path does. That starts
  FinalizeJob as today. Concurrently, run step `cameras`: `await camera_service.stop_recording(sid)`.
  Persist the returned `CameraStopResult` as `<session_folder>/_extras/<sid>_camera_result.json`.
- **Package** runs when BOTH FinalizeJob has finished (observe the session-manager events or
  poll its ledger state) and `cameras` is done or failed.
- **Package** also re-runs, debounced by 5 s, whenever late data or a verified rescue upload
  changes the session's data files. It can be triggered manually via
  `POST /api/sessions/{sid}/package`; a second request while one is running returns 409 `busy`.
- **Restart.** On backend start, any post record not finished is resumed. If `cameras` never
  ran because the backend died mid-recording, run it with the files found on disk: build the
  result from `<sid>_cameras.json` anchors plus the `*_video_sync*.webm` files, remux each file
  with `ffmpeg -i in -c copy out` (write to a tmp file and replace) when ffprobe reports no
  duration, then package.
- **Progress.** The steps shown to the UI (`FinalizeProgress.steps`) are FinalizeJob's steps
  followed by `cameras` and `package`. Every change is broadcast as a `finalize` WS message.

## 5. Server-side session ZIP (the deliverable; OUTPUT_CONTRACT §9)
Write `<session_folder>/<subject>_<tag>_<sid>.zip`.
- The name follows the browser exactly: whitespace becomes `_`, and empty values default to
  `subject` and `session`.
- Stream from disk with `zipfile.ZipFile(..., "w", ZIP_DEFLATED)` and `zf.open(name, "w")`,
  copying in 1 MiB chunks. Write `.tmp`, fsync, then `os.replace`. Never hold a file in memory.
- Store video entries as ZIP_STORED (WebM is already compressed).

Entries, in the browser's order:
1. **Videos.** `videos/<sid>_<camId>_video_sync[_partN].webm`, one per file in each
   `CameraRecording.files`, part order kept.
2. **Data files.** `data/<name>` for every manifest file whose kind passes `isDataKind`. Port
   `DATA_KINDS` exactly and reuse export.py's manifest builder; do not re-implement
   classification.
3. **Recovery files.** `data/recovery/<device_id>.csv` for every complete recovery upload whose
   CSV exists.
4. **Manifest.** `data/manifest.json`, serialised like `JSON.stringify(m, null, 2)`. Implement
   `js_json_dumps(obj)` in `master_backend/app/api/jsjson.py`. It must match JavaScript
   exactly:
   - 2-space indent, `", "` and `": "` separators, `ensure_ascii=False`, no trailing newline,
     key order preserved;
   - integral floats print without `.0` (`1.0` → `1`);
   - exponents follow JS rules (`1e21`, `1e-7`, never `1e+21`);
   - `NaN`/`Infinity` → `null`.
   Unit-test it against a table of JS outputs you write down from the JS spec, including
   `0.1`, `1e21`, `1.5e-7`, `-0.0` → `0`, `123456789012345680000`, nested empty containers
   `[]`/`{}` (JS prints `[]` and `{}`, not `[\n]`), and unicode.
5. **cameras.json.** `js_json_dumps({session_id, cameras, camera_events})`. Each camera is
   `{session_id, cam_id, device_id: device_key, browser_label: label, mime, file: <first ZIP video path>, files: [<all ZIP video paths>], started_at_ms, flash_at_ms, stopped_at_ms}`,
   in that key order. `camera_events` lists `CameraEvent` items with keys
   `key, sessionId, camId, type, atMs, detail`, omitting `detail` when it is None or empty, exactly as
   the IndexedDB records did.
6. **missed_cameras.txt.** Only when `missed` is non-empty: one cam id per line plus a
   trailing newline.
7. **video_report.json.** `js_json_dumps({session_id, videos:[...], healthy})`, where `healthy`
   means every report has `ok` and `ebmlHeaders == 1`. The report key order is
   `ok, bytesWritten, durationMs, ebmlHeaders, error?, path, camId`. Check `webm_seekable.ts`
   and the browser code for the real order, and match whatever it does.

If the manifest cannot be built, write `data/export_error.txt` with the browser's text instead
of the data entries.

**Golden coverage (additive, allowed).** Add
`master_backend/tests/contract/test_session_zip_contract.py` with its own golden file
`golden/session_zip_snapshot.json`. Run the same deterministic scenario as
`test_output_contract.py`, importing its helpers rather than copying them, with
`FakeCameraService(n=2)` plus one camera failing so `_part2`, `track_ended` and missed paths
are all exercised. Snapshot every entry name and the JSON entries' full content. Never edit the
existing golden file.

## 6. v3 REST + `/ws/ui` (API.md)
Implement every endpoint in API.md EXCEPT `/api/cameras/*` and `/api/calibration/*` (those are
L5's router), using the L2 Pydantic models as `response_model`, with the error format from
API.md (a custom exception handler for the `/api` prefix only).
- **`POST /api/session/start`.**
  - Recompute readiness. If there are blockers, return 409 `blocked`.
  - Otherwise call `session_manager.start_recording` with the same payload keys the legacy
    `/ws/frontend` START uses, which you will find in ws_handler: subject/tag/operator from the
    draft and `preflight_failed` holding the warning messages.
  - Then `await camera_service.start_recording(sid, folder)`, where folder is the session
    folder io_manager opened.
  - If cameras fail, the IMU session KEEPS recording. Raise an error notice, retry once after
    1 s, and return the warning in `StartResponse.warnings`.
  - Do not broadcast START to phones differently from today: reuse whatever the legacy START
    path does after `start_recording`. Read `_handle_frontend_command` and extract a shared
    function; do not duplicate it.
- **`POST /api/session/stop`.** As in §4. It is idempotent for 10 s.
- **`POST /api/session/label`.** Calls the same code the legacy SET_LABEL path uses (extract a
  shared function).
- **`GET /api/sessions`.** Scans `Data_Riset_IMU/*/` for `<sid>_*` files plus the ledger. Cache
  per folder by mtime. `verdict` comes from the integrity report. `outstanding_uploads` comes
  from upload.py's status.
- **`POST /api/sessions/{sid}/open-folder`.** Windows: `subprocess.Popen(["explorer", "/select,", zip])`.
  Linux: `xdg-open <folder>`. Otherwise 501. Never block.
- **`POST /api/phones/reset`.** Reuse the legacy RESET logic.
- **Readiness** (backend part; merge with `camera_service.readiness()`):
  - Blockers, each with an operator-readable message that says how to fix it:
    - SSD_PATH not writable (probe with a tmp file), or less than 5 GB free;
    - no phone online;
    - subject, tag or operator empty;
    - state is not IDLE (map PREFLIGHT and READY to IDLE).
  - Warnings:
    - a streaming phone (packets > 0) with `true_hz_avg < 90`;
    - phone `app_version`s differ;
    - less than 20 GB free.
- **Snapshot builder.** Builds `Snapshot` from session_manager, io_manager, runtime stores and
  `camera_service.cameras()`. `state` maps PREFLIGHT and READY to IDLE. `phones[].buffering`
  is true when the phone is offline during RECORDING. `build_id` comes from the existing
  `/health` logic.
- **`/ws/ui` hub.**
  - Send a full snapshot on connect.
  - Rebuild the snapshot at most every 200 ms when something changed: hook session-manager
    observers, notice and draft changes, and post-session progress, plus a 1 s poll that
    compares a hash of the snapshot JSON. Send only if changed, and at least every 5 s as a
    heartbeat.
  - `live` at 10 Hz from `ws_handler._latest_samples`, only while there is at least one
    client.
  - `finalize` messages on every step change.
  - `calibration.watch`: if the camera service has an `async compare(ref_id)` method, call it
    every 0.5 s while at least one client watches and send `calibration.drift`. Stop 5 s after
    the last watcher leaves. If the method is absent, reply with an `ack` error code
    `not_found`.
  - `ping` → `ack`.
  - One slow client must never block the others: give each client its own bounded queue of
    10 and drop the oldest snapshot or live messages when it is full.
- **Static dashboard.** Serve `master_backend/app/static/` at `/`.
  - `index.html` gets `Cache-Control: no-store`; `/assets/*` gets
    `Cache-Control: public, max-age=31536000, immutable`.
  - SPA fallback: any GET not under `/api`, `/ws`, `/upload`, `/export`, `/session`,
    `/recovery`, `/cameras`, `/health`, `/docs` or `/openapi.json` serves `index.html`.
  - If the static directory or `index.html` is missing, serve a small built-in HTML page:
    "Dashboard not built — run `npm run build` in dashboard/".
  - Resolve the path relative to the package so it works when frozen by PyInstaller later.
- `run.py` keeps working with no new required arguments.

## 7. Q4 fix (isolated, pending sign-off)
`finalize.finished_ms` is always 0 in the ledger (OUTPUT_CONTRACT §10.4). Fix it so it records
the real finish time, in ONE separate final commit that contains only this fix and the
corresponding golden value change. That way the commit can be dropped if the project lead
rejects it. It is the ONLY permitted change to the existing golden file: update exactly that
value, and mention the diff in your final report. If the fix changes anything else in the
golden, revert it and report instead. Every other commit in this lane must pass the contract
suite with the ORIGINAL golden file.

## 8. Tests (all must pass)
- **Unit:** `jsjson`; notice store (idempotent raise, resolve, dismiss, persistence across
  reload); draft store; readiness rules (a table-driven test); snapshot state mapping.
- **API** (`TestClient` with the `rig` fixture and `IMU_CAMERAS=fake`):
  - draft → start (blocked, then OK) → label → stop → wait for finalize and post-session →
    `GET /api/sessions/{sid}` shows `package` done and the ZIP path exists;
  - a second stop returns the same body;
  - label outside a session → 409;
  - error bodies match API.md.
- **WS:** connect → receive a snapshot; trigger a draft change → receive an updated snapshot
  within 1 s; ping → ack; calibration.watch without support → ack error.
- **Restart:** kill mid-session (simulate by building a post record with `cameras` not run) →
  resume produces the ZIP.
- Golden: `test_session_zip_contract.py` (new) passes; the existing contract suite passes, with
  only the Q4 value changed.
- Full suite: `master_backend/.venv/bin/python -m pytest master_backend/tests -q`, zero failures.

## Deliverable
- Small, logical commits with `git commit --no-gpg-sign`. Each message explains why, with no
  trailers.
- Write `master_backend/ARCHITECTURE.md` (one page): modules, the v3 flow from START to ZIP,
  which legacy endpoints remain for L7 to delete, and how to run with fake cameras
  (`IMU_CAMERAS=fake python master_backend/run.py`).
- Final report:
  - the test output;
  - the Q4 golden diff;
  - anything in API.md or the L2 seam you found missing or ambiguous, and how you resolved it.
