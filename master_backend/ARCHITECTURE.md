# Backend architecture (post-rebuild)

One FastAPI process serves the phones (binary WebSockets), the dashboard (same-origin
static build + JSON/WebSocket API) and the data files on the SSD. Everything below is
additive to the pre-rebuild backend; the frozen output contract and the legacy endpoints
are unchanged.

## Modules

| Path | Role |
|---|---|
| `app/main.py` | App wiring: routers, error envelope, static hosting, lifespan (runtime, post-session job, UI hub, watchdog). |
| `app/ws_handler.py` | Phone `/ws/telemetry` + `/ws/control`, legacy dashboard `/ws/frontend` + `/ws/live`. Also the shared START/label/RESET actions the v3 API reuses. |
| `app/session_manager.py` | Recording lifecycle, device registry, lifecycle events, durable session ledger (`.sessions/<sid>.state.json`). |
| `app/io_manager.py` | CSV writers, late-delivery window, rescue writers, label timeline, session folder. |
| `app/finalize_job.py` | Ordered, resumable finalize steps: `close_writers`, `validate`, `bundle` (+ ledger write). |
| `app/export.py` | Artifact manifest/classification, consolidation, data bundle (`<sid>_bundle.zip`, never video). |
| `app/cameras.py` | Durable per-camera wall-clock anchors (`<sid>_cameras.json`). |
| `app/camera/` | L2 seam: `interface.py` (types + `CameraService`), `fake.py` (tests/demo), L5's real service + `/api/cameras` + `/api/calibration` router when present. |
| `app/api/schema.py` | The single definition of every API shape (exported to `schema.json`). |
| `app/api/routes.py` | v3 REST: state, draft, start/stop/label, notices, phone reset. |
| `app/api/sessions.py` | Session history/detail, ZIP download (Range), package, open-folder. |
| `app/api/readiness.py` | START blockers/warnings (disk, phones, draft, state, cameras). |
| `app/api/snapshot.py` | Whole-dashboard `Snapshot` builder. |
| `app/api/ws_ui.py` | `/ws/ui` hub: change-detected snapshots (rebuilt at ≤5 Hz, 5 s heartbeat), live 10 Hz, finalize events, calibration drift. |
| `app/api/post_session.py` | The `cameras` + `package` job that follows STOP (separate from FinalizeJob). |
| `app/session_zip.py` | Server-side deliverable ZIP, byte-shaped like the browser's (OUTPUT_CONTRACT §9). |
| `app/api/jsjson.py` | `JSON.stringify`-compatible serialiser for files inside that ZIP. |
| `app/api/runtime.py` | Runtime object, notice/draft stores, camera factory, notice sources, watchdog. |
| `app/api/errors.py`, `static_site.py` | `/api` error envelope; `app/static/` hosting with SPA fallback. |

## START → ZIP

1. `POST /api/session/start` recomputes readiness. Blockers → `409 blocked` with
   `details.blockers`; warnings are passed as `preflight_failed` and recorded in the CSV.
2. `SessionManager.start_recording` opens the CSV writers, transitions to RECORDING and
   persists the ledger; the shared `broadcast_start_to_phones()` sends START to phones.
3. The cameras start in the same folder. A failure raises a notice, retries once, and
   returns `warnings` — the IMU session keeps recording.
4. During recording, packets are written server-side and labels are checkpointed through
   the ledger. Phones that buffer offline upload rescue CSVs later (`/upload/csv`).
5. `POST /api/session/stop` accepts STOP once (idempotent for 10 s), starts FinalizeJob
   and, concurrently, the post-session `cameras` step (`camera_service.stop_recording`,
   result persisted to `<folder>/_extras/<sid>_camera_result.json`).
6. When FinalizeJob has settled the ledger AND cameras are done, the `package` step builds
   `<folder>/<subject>_<tag>_<sid>.zip` from the manifest + camera result, fsyncs and
   renames it. Late telemetry or a verified rescue upload schedules a 5 s debounced
   rebuild; `POST /api/sessions/{sid}/package` rebuilds on demand (`409 busy` if running).
7. `/ws/ui` pushes a whole snapshot on connect and then whenever it changes (rebuilt at
   ≤5 Hz, sent at least every 5 s), plus `live` samples at 10 Hz; the finalize/cameras/package
   steps appear as `finalize` messages and in `GET /api/sessions/{sid}`.
8. `GET /api/sessions/{sid}/zip` streams the archive with Range support; a shutdown that
   finalises a recording starts steps 5–6 itself, so a restart never loses the deliverable.

State that must survive a restart: the session ledger, `<sid>_cameras.json`, the video
files, and `_state/sessions/<sid>.post.json`. On startup the backend recovers interrupted
sessions, then resumes unfinished post records — rebuilding the camera result from the
anchors + video files (ffprobe, `ffmpeg -c copy` remux when the duration is unreadable).

Runtime state files under `<SSD>/Data_Riset_IMU/_state/`: `notices.json`, `draft.json`,
`sessions/<sid>.post.json` (plus L5's `cameras.json` selection).

## Legacy endpoints (L7 deletes these with the old dashboard/app)

The new dashboard uses `/api/*` and `/ws/ui` only. Still present for the old clients:

- `/ws/frontend`, `/ws/live` — old dashboard channels.
- `/session`, `/session/recovery`, `/session/recovery/{sid}` — lifecycle readouts.
- `/recovery/*`, `/export/*` — old export/consolidation screens; the data bundle endpoint
  (`POST /export/{sid}/bundle`) is still the integrity-safe fallback and writes
  `_bundle.zip` (no video, never nested).
- `/cameras/{sid}/mark`, `/cameras/{sid}` — anchors posted by the old dashboard.

Kept on purpose, not legacy: `/health`, `/ws/telemetry`, `/ws/control`, `/upload/status`,
`/upload/csv` (phone-facing, unchanged).

## Running with fake cameras

```bash
IMU_CAMERAS=fake IMU_FAKE_CAMERAS=4 python master_backend/run.py  # synthetic cameras, real pipeline
IMU_CAMERAS=stub python master_backend/run.py     # in-memory test double, no ffmpeg
IMU_CAMERAS=off python master_backend/run.py      # seam active, no devices
```

`auto` (the default) mounts L5's real service when `master_backend.app.camera.service` is
importable and otherwise falls back to a device-less fake so the seam stays exercised.
Tests: `master_backend/.venv/bin/python -m pytest master_backend/tests -q`.