# IMU Collector — rebuild specification ("make-proper")

Status: approved direction, 2026-10-29. Integration branch: `make-proper`.
Companion documents in this folder: [API.md](API.md) (dashboard ↔ backend contract),
[PHONE.md](PHONE.md) (Android node), [CALIBRATION.md](CALIBRATION.md) (camera setup check).
The frozen output format is specified by [`docs/OUTPUT_CONTRACT.md`](../OUTPUT_CONTRACT.md) and
enforced by the golden tests in `master_backend/tests/contract/`.

---

## 1. Why we are rebuilding

The system records synchronised IMU (3–8 phones strapped to a subject) and webcam video (1–5
USB cameras) for a research study. Seventeen subjects have been recorded. The current system was
built incrementally and fails in the field in ways that cost data and operator trust. Every
failure below was traced in code during the 2026-10-29 study, not inferred.

### 1.1 Web dashboard (main source of crashes)
| Symptom seen in the field | Root cause in today's code |
|---|---|
| Page dies ("Application error") when ending a session | At STOP the browser tab stops 1–5 `MediaRecorder`s, flushes their chunks to IndexedDB, re-parses every WebM through `ts-ebml` on the main thread to add Duration/Cues, and streams a multi-hundred-MB ZIP through the File System Access API — while `page.tsx` (919 lines, one component) re-renders at 20 fps from live samples. The heaviest work of the session runs in the most fragile process. |
| Notices about old sessions that can never be cleared | The "unsaved footage" banner is derived from IndexedDB, which is **per origin and per browser profile**. A session saved from `localhost:3000` is invisible from `127.0.0.1:3000` or the LAN IP; a save whose validation failed never clears; there is no dismiss. |
| Forced to download a session again after downloading it | "Already exported" is **one** `localStorage` key holding **one** session id (`imu.acked-end-session.v1`). On every reconnect the dashboard reopens the non-dismissible export modal for any terminal session that is not that single id and has no validated IndexedDB marker in *this* origin. Acknowledging B forgets A; saving through the recovery screen never updates the key. |
| White screen on 2026-08-11 | Browser executed a stale cached Next.js bundle against a newer backend. |
| Chrome/Edge only | START is blocked without `showSaveFilePicker`. |

Flow problems: an IP-entry screen on a page served by the same machine; a 256 px rail stacking
session form, devices, alerts, preflight, two recovery buttons, START, Disconnect and Reset; 51
unlabelled label buttons; `alert()` popups; nine START conditions spread over two code paths.

### 1.2 Phone app
Two Flutter engines in one process (UI + `flutter_foreground_task` engine) mirrored over isolate
messages; `updateService()` called per sensor packet (100 Hz) while offline, each a full
`startForegroundService()` + SharedPreferences writes on the main thread; concurrent control
message handling (a heartbeat before START resets the sequence mid-stream → backend drops real
samples as duplicates); endless 3 s reconnect loop on role collision; Dart `Timer.periodic(10ms)`
sampling; a "START RECORDING SESSION" button that starts nothing; disconnect where Back usually
is; number keyboard without a "." on Samsung; relaunch lands on the wrong screen and hangs 8 s.

## 2. Goals and non-goals

**Goals**
1. **Zero change to recorded data** (§3). Old and new sessions must be analysable by the same code.
2. **Nothing important lives in a browser.** All session state, video, exports and notices live
   on the backend; any page can be closed, reloaded or opened from another origin at any time
   without loss or duplicate prompts.
3. **One deployable unit on the laptop** (backend + dashboard + ffmpeg), one APK on the phones.
4. **A guided operator flow**: Setup → Record → Done, with calibration available from Setup.
5. **Camera calibration**: simultaneous reference stills and a ghost-overlay check that the
   camera setup has not moved (§7).
6. Every failure mode in §1 has an acceptance test (§10).

**Non-goals**: changing the analysis pipeline; changing label semantics (labels stay numeric
0–50, 0 = baseline); multi-laptop setups (cameras, backend and dashboard are on one Windows
laptop); iOS.

## 3. Frozen contract

Everything listed in `docs/OUTPUT_CONTRACT.md` is frozen **byte-for-byte in structure**: file
names, CSV metadata lines, headers, column order, number formatting, JSON key trees and types —
for the per-device sensor CSVs, late CSVs, consolidated CSVs, every JSON report, `cameras.json`,
the backend bundle, the phone rescue CSV + sidecars, and the **session ZIP** whose layout is
today assembled in the browser (`<subject>_<tag>_<sid>.zip` with `videos/`, `data/`,
`data/recovery/`, `cameras.json`, `video_report.json`, optional `missed_cameras.txt`).

Rules:
- The golden tests in `master_backend/tests/contract/` must pass on every lane, unmodified. A
  lane may **add** golden coverage, never regenerate existing golden files. Regenerating golden
  files requires explicit sign-off from the project lead, with the diff shown.
- New information goes into **new** files outside the frozen set (session folder subdirectory
  `_extras/`, or `Data_Riset_IMU/_calibration/`). New files are **not** added to the session ZIP
  unless the lead approves (open question Q1).
- Video stays WebM, one file per uninterrupted recording run, finalised with Duration and Cues,
  named `<sid>_<camId>_video_sync[_partN].webm`. `cameras.json` keeps every field it has today;
  the field `browser_label` keeps its name and carries the camera's device label.
- The phone↔backend wire protocol stays compatible (protobuf messages in `shared_contracts/`),
  extended only additively (§6.3).

## 4. Target architecture

```
                         Windows laptop (one process tree)
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │  imu-collector  (Python 3.12, FastAPI, uvicorn, single port 8000)            │
 │                                                                              │
 │  ┌─────────────┐  ┌───────────────┐  ┌──────────────┐  ┌──────────────────┐   │
 │  │ phone link  │  │ session core  │  │ camera svc   │  │ dashboard (static│   │
 │  │ /ws/control │→ │ SessionManager│← │ 1 ffmpeg per │  │ Vite+React build │   │
 │  │ /ws/telemetry│ │ IoManager     │  │ camera, MJPEG│  │ served at "/")   │   │
 │  │ /upload/*   │  │ Finalize+ZIP  │  │ frames→recorder│ └──────────────────┘   │
 │  └─────────────┘  │ (frozen core) │  │ + preview +   │          ▲             │
 │                   └───────────────┘  │ calibration   │   /api/* + /ws/ui      │
 │                                      └──────────────┘                         │
 │   disk: SSD_PATH/Data_Riset_IMU/<subject>_<tag>/<sid>_* (+ _extras/, _calibration/)
 └──────────────────────────────────────────────────────────────────────────────┘
        ▲  Wi-Fi, mDNS `_imu-telemetry._tcp`                      browser (any, same laptop
        │                                                         or LAN) — holds no data
  Android phones: native Kotlin foreground service + Compose UI
```

### 4.1 Stack decisions
| Area | Decision | Reason |
|---|---|---|
| Backend | Python 3.12 + FastAPI, existing package `master_backend` | The data-producing core lives here and is pinned by golden tests. |
| Dashboard | **Vite + React 18 + TypeScript**, built to static files, served by FastAPI at `/` from `master_backend/app/static/` | Removes the Next.js server, Node at runtime, SSR/hydration issues and stale-bundle skew. Same origin → no IP screen, no CORS. |
| Built dashboard | The built `static/` is committed (stamped with the git sha) until the packaged exe exists | The field laptop must not need Node. Backend and dashboard are always the same commit. |
| API types | Pydantic models are the single source; TS types generated from their JSON Schema (`npm run gen:api`); a test fails if the generated file is stale | Today's crashes include the dashboard dereferencing a field the backend no longer sends. |
| Live charts | uPlot (or plain canvas) in an isolated component fed by its own WS message type | Live data must never re-render the page. |
| Video | ffmpeg per camera (DirectShow on Windows; v4l2 on Linux; `lavfi testsrc` fake camera for tests), MJPEG frames piped to Python, re-encoded to WebM by a per-recording encoder ffmpeg | Preview, stills and recording share one capture; the camera is opened once; nothing touches the browser. |
| Phone | Native Kotlin: foreground service + Jetpack Compose UI, OkHttp, hand-written protobuf codec, NSD (mDNS) discovery | One process, one runtime, ordered state machine, no plugin glue. |
| Packaging | Phase 1: `python run.py` (venv) with `ffmpeg.exe` on PATH or in `vendor/`. Phase 2 (last lane): PyInstaller one-folder `IMU-Collector/` with ffmpeg bundled | One thing to install and update. |

### 4.2 What happens to the existing code
- **Kept, behind golden tests (may be refactored, not rewritten):** `csv_schema.py`,
  `io_manager.py`, `integrity_validator.py`, `sampling_analysis.py`, `finalize_job.py`,
  `dedup_store.py`, `session_ledger.py`, the consolidate/bundle parts of `export.py`,
  `upload.py` (phone rescue upload protocol), `proto/`.
- **Rewritten:** `main.py` (app assembly), `ws_handler.py` frontend half (`/ws/frontend`,
  `/ws/live` → `/ws/ui`), session orchestration surface in `session_manager.py` (commands,
  readiness, notices), `cameras.py` (becomes the camera service).
- **Deleted:** `master_frontend/` (Next.js), `mobile_node/` Flutter code, browser recovery
  endpoints that exist only to rescue browser state (`/session/recovery*`, `/cameras/{sid}/mark`,
  `/recovery/*` UI paths are replaced by `/api/sessions/*`). `/ws/control`, `/ws/telemetry`,
  `/upload/status`, `/upload/csv` and `/health` stay.
- Tools under `tools/` keep working (`device_simulator.py`, `decode_fallback_buffer.py`,
  `analyze_session.py`, `fix_webm.sh`, `recover_video.py`).

## 5. Operator flow (dashboard)

Open `http://<laptop>:8000/` in any browser. No connect screen. A persistent top bar shows:
backend state, session state, phones online/expected, cameras live/selected, disk free, build.

### 5.1 Setup
Three panels on one screen, laid out for a 1366×768 laptop without scrolling:
1. **Session** — Subject, Tag, Operator. Saved to the backend as a *draft* on every edit (debounced),
   so reload or a second browser shows the same values. Tag and Operator are kept after a session;
   Subject is cleared.
2. **Phones** — one card per connected phone, large role name, online state, true sampling rate
   (Hz, from distinct readings), packets, app version, telemetry age. Version mismatch and
   < 90 Hz are shown on the card itself. "Reset phone connections" lives in a ⋯ menu with a
   confirm dialog; disabled while recording.
3. **Cameras** — detected devices with a checkbox each (max 5, slots cam1..cam5 assigned in
   order, persisted server-side by stable device key), live preview tiles (MJPEG from the
   backend, ~10 fps), resolution/fps, and two buttons: **Take reference stills** and
   **Calibrate** (§7).

**Readiness** is one list computed by the backend (single source of truth, `readiness` in the
snapshot), split into:
- *Blockers* (START disabled, each says how to fix it): backend can write to `SSD_PATH` with
  ≥ 5 GB free; ≥ 1 phone online; Subject/Tag/Operator filled; ≥ 1 camera selected and every
  selected camera live; no session currently recording or finalising.
- *Warnings* (START allowed, button reads "Start anyway", warnings are recorded into the session
  exactly as today's `preflight_failed` field): a phone below 90 Hz true rate; phone app versions
  differ; clock offset unknown on a phone; a camera drifted from the active calibration
  reference; disk < 20 GB.

**START** is one big button. It sends `POST /api/session/start`; the backend refuses with the
list of blockers if any appeared in the meantime.

### 5.2 Record
Switches automatically when the backend state is RECORDING (in every open browser).
- Big elapsed timer; subject/tag; REC indicator.
- **Labels**: the active label shown huge with how long it has been active. A 0–50 grid of large
  buttons, plus keyboard entry: type digits then Enter to set; `0` then Enter or `Esc` returns to
  baseline 0. Label changes are optimistic with a visible pending state until the backend ACKs;
  failure shows inline (never `alert()`).
- Phone health strip: one chip per phone (online / offline mm:ss / buffering), true Hz.
  Offline phones show the existing guidance ("data is buffering on the phone, do not stop yet").
- Camera strip: small live thumbnails with REC dot and fps; a camera that drops shows "lost —
  reconnecting" and recording resumes into a `_partN` file automatically when it returns.
- Compact live IMU chart per phone (acc magnitude), isolated from page rendering.
- **STOP** requires a confirm (dialog or 1 s press-and-hold). It sends one request and returns
  immediately.

### 5.3 Done
Shown when a session reaches FINALIZING and after; survives reload because it is driven by the
backend's per-session record.
- Finalize steps with live status (close & checksum CSVs, stop cameras & finalise video,
  consolidate late/phone data, validate, late-window, write session ZIP), each with duration and
  error text if failed.
- Integrity verdict (PASS / PARTIAL / FAIL) with per-phone reasons, as today's report.
- Where the data is: the session folder path and the ZIP path on the laptop, with **Open folder**
  (backend runs `explorer` on Windows) and an optional **Download ZIP** link. Downloading is
  never required; the ZIP is already on disk.
- Late phone data and rescue uploads appear here as inline status rows ("chest: 1,204 late rows
  merged") and update live; no popups.
- Consolidation status (current / pending phone data / not needed) with **Consolidate now**, which
  pulls late rows and queued phone rescue uploads into the ZIP without waiting for the next
  automatic pass; it is disabled while a run is in progress or while the consolidation is current.
- **New session** returns to Setup.

### 5.4 Sessions (history)
A page listing every session found on disk (newest first): subject, tag, date, duration,
phones, cameras, verdict, ZIP present, outstanding phone uploads. Opening a row shows the same
Done view. Notices are server-side records with a **Dismiss** button that sets a flag on the
server; a notice never depends on browser storage.

## 6. Backend

### 6.1 Session lifecycle
`IDLE → RECORDING → FINALIZING → VALIDATING → IDLE` (plus ERROR), as today. START, STOP and
LABEL are plain commands (`/api/session/*`) with typed results; the phone-side behaviour is
unchanged. Finalize (existing `FinalizeJob`, resumable) gains three steps:
1. `cameras` — stop every camera recording, finalise each WebM (clean ffmpeg exit writes
   Duration+Cues; if a process died, remux with `ffmpeg -c copy` as `tools/fix_webm.sh` does),
   probe each file (ffprobe: duration, frames, EBML header count) and write `cameras.json`.
2. `consolidate` — decide from the manifest whether the session is whole or already consolidated
   (the old end-session modal's `canConsolidate` / `hasPendingPhoneData` rules, ported to
   `export.consolidation_decision`); if not, wait up to 60 s (polling every 3 s, breaking early
   when a transfer stops moving) for phone rescue uploads, then merge late rows and verified
   uploads into the consolidated CSVs with the existing `export_consolidate`. Skipped with
   detail "session is whole" when nothing is owed.
3. `package` — write `<SSD_PATH>/Data_Riset_IMU/<subject>_<tag>/<subject>_<tag>_<sid>.zip` with
   exactly the layout in OUTPUT_CONTRACT.md, streaming from disk (never in memory), including
   `video_report.json` with the same fields as today's `FinalizeResult` (`ok, bytesWritten,
   durationMs, ebmlHeaders, error?` plus `path, camId`) and `healthy`. Re-run automatically when
   late data or a rescue upload changes the session's data files; the previous ZIP is replaced
   atomically (write `.tmp`, fsync, rename).
Every step records start/end/error in the session ledger, so a backend restart resumes.

### 6.2 Notices
A small persistent store (`Data_Riset_IMU/_state/notices.json`, atomic writes): `{id, level,
session_id?, code, message, created_at_ms, dismissed_at_ms?}`. Sources: finalize failure, late
delivery, outstanding phone rescue upload, camera lost during recording, disk low, video
validation failure. Notices auto-resolve when their condition clears (e.g. upload completes) and
can be dismissed manually. They are part of the snapshot.

### 6.3 Phone protocol additions (additive, old phones unaffected)
- **Idle telemetry**: phones stream while connected (so the true-rate check works before START).
  Idle packets carry `sequence_number ≥ 2^40` (`IDLE_SEQ_BASE`). The backend counts them for
  rate/liveness but **never writes them** to a session CSV or late CSV (filter in both the
  RECORDING path and the late path of `/ws/telemetry`). Session packets keep 0,1,2,…
- mDNS advertisement stays `_imu-telemetry._tcp.local.`, TXT gains `api=3`.
- Nothing else in the wire protocol changes.

### 6.4 Camera service
- **Discovery**: Windows `ffmpeg -hide_banner -list_devices true -f dshow -i dummy`; each video
  device has a friendly name and an *alternative name* (`@device_pnp_…`) — the alternative name
  is the stable **device key** (two identical webcams have identical friendly names). Linux:
  `/dev/v4l/by-id/*`. Test backend: `fake:<n>` → `lavfi testsrc2` with a moving pattern.
- **Capture**: one long-lived ffmpeg per selected camera, opened when selected, emitting MJPEG
  frames on stdout (`-c:v copy` when the camera delivers MJPEG, else `-c:v mjpeg -q:v 4`), 1280×720
  @ 30 fps requested (configurable). Python splits frames on JPEG SOI/EOI and stamps each with
  arrival `time.time_ns()`. A reader thread per camera keeps the **latest frame** in memory and,
  when recording, pushes frames into the encoder queue (bounded; overflow is counted and reported,
  never blocks the reader).
- **Preview**: `GET /api/cameras/{cam_id}/preview.mjpg` — multipart MJPEG at ≤ 10 fps, frames
  downscaled to ≤ 640 px wide (Pillow/turbojpeg) only when a viewer is connected.
- **Recording**: at START, for each camera spawn an encoder
  `ffmpeg -f mjpeg -use_wallclock_as_timestamps 1 -i pipe:0 -c:v libvpx-vp9 -deadline realtime
  -cpu-used 8 -row-mt 1 -b:v 2500k <sid>_<camId>_video_sync.webm` (fall back to `libvpx` VP8 if
  the benchmark in the lane shows VP9 cannot keep up with 5 cameras on the target laptop; record
  the codec in `mime` exactly like today: `video/webm;codecs=vp9` or `video/webm;codecs=vp8`).
  `started_at_ms` = arrival time of the first frame written; `flash_at_ms` = same value (there is
  no screen flash any more; field kept); `stopped_at_ms` = arrival time of the last frame.
  Per-frame arrival times go to `_extras/<sid>_<camId>_frames.csv` (`frame_index,arrival_epoch_ms`)
  — new data, outside the frozen set.
- **Failure**: if a capture ffmpeg exits during recording, mark the camera lost, emit a notice and
  a `track_ended` camera event, retry every 2 s; on success start a new encoder writing
  `_part2`, `_part3`… exactly as the browser did.
- `camera_events` in `cameras.json` keep today's item shape `{key, sessionId, camId, type, atMs,
  detail?}` with types `started | stopped | track_ended | write_error`.
- Windows specifics: a webcam can be opened by one process at a time — the dashboard never calls
  `getUserMedia`. ffmpeg child processes are created in a Job Object (or equivalent) so they die
  with the backend; shut down by writing `q` to stdin, then terminate after 5 s.

## 7. Calibration (camera setup check)
Specified in detail in [CALIBRATION.md](CALIBRATION.md). Summary:
- **Take reference stills**: one button captures the latest frame of **every live selected camera
  at the same instant** (one timestamp; skew ≤ one frame interval, recorded per camera) and saves
  them as a named reference set under `Data_Riset_IMU/_calibration/<ref_id>/`. The newest set can
  be made *active*.
- **Calibration mode**: a full-screen view per camera showing the live feed with the reference
  still as a **ghost overlay** — opacity slider, modes *Blend*, *Difference*, *Blink* (A/B every
  0.5 s) and *Side by side*, plus a centre crosshair/grid. The backend continuously estimates the
  shift between reference and live (edge-based phase correlation) and shows per camera
  `Δx, Δy` in pixels and a verdict **OK / CHECK / MOVED**.
- The active reference feeds a readiness *warning* ("cam2 moved 14 px since reference
  'lab-A 2026-10-30'"). At START and at STOP the backend logs every camera's drift to
  `_calibration/session_checks.jsonl`, so a bumped tripod during a session is detectable later.

## 8. Phone (Android)
Specified in [PHONE.md](PHONE.md). Summary: native Kotlin foreground service owns sensors,
sockets, rescue CSV, offline buffer, uploads and the alarm in one ordered state machine; three
Compose screens (Setup with mDNS auto-discovery and a big role grid; Status with a huge
colour-coded state block; Files); idle streaming with `IDLE_SEQ_BASE`; device identity migrated
from the Flutter app so `device_id` survives the upgrade; same `applicationId`.

## 9. Delivery plan (lanes)
Each lane is a **standalone local clone** (`git clone -b <lane> <repo> .lanes-data-collector-IMU-mobile/<lane>`,
push URL disabled) branched from `make-proper`, implemented by an OpenCode agent on the Alibaba
token plan, and reviewed by the lead (diff, tests, mutation probes where useful) before merge.
Not a `git worktree`: OpenCode treated the main checkout as the project root from a worktree
and edited it (2026-10-29). Lanes never push; the lead fetches the lane branch from the clone.

| # | Lane | Depends on | Model (first choice) |
|---|---|---|---|
| L1 | Output contract golden tests + OUTPUT_CONTRACT.md — **done** (`09aaa16`, 7 tests, mutation-probed) | — | deepseek-v4.1-flash-1017 |
| L2 | API schema: Pydantic models + generated TS types + schema test | L1 | lead writes API.md; deepseek-v4.1-flash-1017 implements |
| L3 | Phone: native service + Compose UI | — (protocol frozen) | qwen3.8-max |
| L4 | Backend: idle-seq filter, notices, `/api/*`, `/ws/ui`, static hosting, finalize `package` step, server-side session ZIP matching the contract | L1, L2 | qwen3.8-max |
| L5 | Camera service + calibration backend (fake camera for tests; dshow on Windows) | L2 | qwen3.8-max |
| L6 | Dashboard (Vite+React): Setup / Record / Done / Sessions / Calibration | L2 (mocks), then L4+L5 | qwen3.8-flash, escalate to qwen3.8-max |
| L7 | Integration: end-to-end soak on real phone + fake and real cameras; delete old frontend & Flutter app; update STARTUP/DEPLOY/FIELD_CHECKLIST; `ops/install_phones.sh` APK path | L3–L6 | lead + deepseek-v4.1-flash-1017 |
| L8 | Packaging: PyInstaller `IMU-Collector/` with ffmpeg, start script, version stamp | L7 | deepseek-v4.1-flash-1017 |

Fallback order when a model fails or stalls: deepseek-v4.1-flash-1017 → qwen3.8-flash →
qwen3.8-max.

## 10. Acceptance tests (the rebuild is not done until all pass)

**Data**
- A1. Golden contract suite passes unmodified on `make-proper`.
- A2. An end-to-end session produced by the NEW system (simulated phones + fake cameras) passes
  the format-level contract assertions and its session ZIP has exactly the documented layout;
  `tools/analyze_session.py` runs on it unchanged.
- A3. Idle packets (seq ≥ 2^40) arriving after START and inside the late window never appear in
  any CSV.

**Browser holds no state** (regressions of §1.1)
- B1. 30-minute session, 5 fake cameras at 720p30, STOP: dashboard heap stays < 300 MB
  throughout (Chrome DevTools), no console errors, ZIP on disk with 5 playable, seekable videos.
- B2. Reload the dashboard during STOP/finalize: the Done view reappears with the same progress;
  nothing is lost or duplicated.
- B3. Open the dashboard from `localhost` and from the LAN IP simultaneously: both show identical
  state, notices and drafts; dismissing a notice in one removes it in the other.
- B4. Two sessions back to back, then reload, then a new browser profile: no dialog asks to
  download anything; no stale notice reappears.
- B5. Close every browser during RECORDING for 2 minutes: recording continues; reopening shows
  the Record view with correct elapsed time and label.

**Video**
- V1. Every video is WebM with one EBML header, Duration and Cues present (ffprobe), duration
  within 1 s of `stopped_at_ms - started_at_ms`.
- V2. Unplug a camera (or kill its capture process) mid-session for 10 s: recording resumes into
  `_part2`; `cameras.json` lists both files and a `track_ended` event.
- V3. Kill the backend mid-recording, restart: finalize resumes; video files are remuxed to be
  seekable; the ZIP is produced.

**Calibration**
- C1. Reference stills from 5 cameras carry capture timestamps within one frame interval.
- C2. With the fake camera shifted by a known offset (e.g. 20 px), calibration reports Δ within
  ±2 px and verdict MOVED; unshifted reports OK; a global brightness change of ±30 % stays OK.

**Phone** (real Galaxy A56 over `adb reverse tcp:8000 tcp:8000`, plus simulator)
- P1. 30-minute session; toggle Wi-Fi off 3× for 30 s each: no crash, alarm sounds and silences,
  the backend CSV has no gaps after flush, the rescue CSV matches the backend rows.
- P2. `adb shell am kill` / force-stop during RECORDING: service restarts and resumes with
  `sequence = checkpoint + 1`; no duplicate-dropped samples.
- P3. Role collision: clear error on the phone, no reconnect loop (backend log shows one attempt).
- P4. Upgrade install over the old Flutter app keeps the same `device_id`.
- P5. 100 START/STOP cycles from the dashboard with 3 simulated phones + 1 real phone: no
  crash, no stuck state, every session finalises.

## 11. Open questions (defaults apply until answered)
- Q1. Should calibration results and per-frame video timestamps also go into the session ZIP
  (under a new `extras/` folder)? **Default: no** — kept next to the session on disk only.
- Q2. Required phone roles per study protocol (to show "expected 3, online 2")? **Default:** no
  fixed list; show online phones only.
- Q4. `finalize.finished_ms` is always `0` in the persisted session ledger (frozen as-is by L1,
  OUTPUT_CONTRACT.md §10.4). It looks like a bug, but the ledger is not research data. **Default:**
  L4 fixes it and updates that one golden value, with the lead's sign-off.
- Q3. Target laptop CPU, to choose VP9 vs VP8 for 5 cameras. **Default:** benchmark in L5,
  prefer VP9 (matches existing recordings), fall back to VP8.
