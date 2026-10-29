# Dashboard ↔ backend API (v3)

Part of the rebuild spec ([SPEC.md](SPEC.md)). Same origin: the dashboard is served by the
backend at `/`, so every URL below is relative. JSON everywhere; times are epoch milliseconds
(`*_ms`), sizes in bytes.

**Single source of truth**: these shapes are defined as Pydantic v2 models in
`master_backend/app/api/schema.py`. `python -m master_backend.app.api.export_schema` writes
`master_backend/app/api/schema.json`; the dashboard generates `src/api/types.ts` from it with
`json-schema-to-typescript` (`npm run gen:api`). A backend test fails if `schema.json` is stale;
a dashboard check fails if `types.ts` is stale. Nobody hand-writes these types twice.

Phone-facing endpoints (`/ws/control`, `/ws/telemetry`, `/upload/status`, `/upload/csv`) and
`/health` are unchanged and not described here.

## Errors
Every non-2xx response is `{"error": {"code": str, "message": str, "details": object|null}}`.
Codes used: `blocked` (409, START refused; `details.blockers` = list of `Issue`), `bad_state`
(409, command not valid in this state), `not_found` (404), `busy` (409, an operation of the same
kind is in progress), `invalid` (422), `internal` (500). The UI shows `message` inline.

## Shared models
```text
Issue          { code: str, message: str, subject?: str }   # subject = device_id / cam_id / field
SessionInfo    { session_id: str, subject: str, session_tag: str, operator: str,
                 started_at_ms: int, scheduled_start_ms: int,
                 label: { id: int, name: str, since_ms: int } }
Draft          { subject: str, session_tag: str, operator: str, updated_at_ms: int }
Phone          { device_id: str, role: str, model: str, app_version: str, online: bool,
                 offline_since_ms: int|null, rate_hz: float, true_hz_avg: float,
                 packets: int, telemetry_age_ms: int|null, buffering: bool }
CameraDevice   { device_key: str, label: str, backend: "dshow"|"v4l2"|"fake" }
Camera         { cam_id: str, device_key: str, label: str,
                 state: "opening"|"live"|"lost"|"error",
                 recording: bool, width: int|null, height: int|null, fps: float|null,
                 dropped_frames: int, error: str|null,
                 drift: Drift|null }                          # vs active reference, if any
Drift          { verdict: "OK"|"CHECK"|"MOVED"|"NO_SIGNAL"|"MISSING"|"RESOLUTION_CHANGED",
                 dx_px: float, dy_px: float, shift_pct: float, score: float,
                 confidence: float, at_ms: int }
FinalizeStep   { name: "close_writers"|"cameras"|"consolidate"|"validate"|"bundle"|"package"|"late_window",
                 status: "pending"|"running"|"done"|"failed"|"skipped",
                 started_at_ms: int|null, ended_at_ms: int|null, detail: str|null }
Notice         { id: str, level: "info"|"warn"|"error", code: str, message: str,
                 session_id: str|null, created_at_ms: int, resolved: bool }
Readiness      { blockers: [Issue], warnings: [Issue] }
Snapshot       { build_id: str, server_time_ms: int,
                 state: "IDLE"|"RECORDING"|"FINALIZING"|"VALIDATING"|"ERROR",
                 session: SessionInfo|null,          # the RECORDING or finalising session
                 last_session_id: str|null,          # drives the Done view after IDLE
                 draft: Draft,
                 phones: [Phone], cameras: [Camera],
                 readiness: Readiness,
                 finalize: { session_id: str, steps: [FinalizeStep] } | null,
                 notices: [Notice],                  # unresolved & undismissed only
                 disk: { path: str, free_bytes: int },
                 calibration: { active_ref_id: str|null } }
SessionSummary { session_id, subject, session_tag, operator, started_at_ms, ended_at_ms|null,
                 duration_s: float|null, phones: [str], cameras: [str],
                 verdict: "PASS"|"PARTIAL"|"FAIL"|"UNKNOWN"|"NONE",
                 folder: str, zip_path: str|null, zip_bytes: int|null,
                 outstanding_uploads: int, notices: int }
SessionDetail  SessionSummary + { finalize: [FinalizeStep], integrity_report: object|null,
                 files: [{ name: str, bytes: int, kind: str }],
                 late: [{ device_id: str, role: str, rows: int }],
                 uploads: [{ device_id: str, role: str, received_bytes: int,
                             total_bytes: int|null, complete: bool, verified: bool }],
                 consolidation: ConsolidationStatus|null }
ConsolidationStatus
               { state: "current"|"pending_phone_data"|"not_needed", detail: str,
                 can_consolidate: bool, whole: bool, late_pending: bool,
                 recovery_pending: bool, uploads_in_progress: bool,
                 rescue_uploads: int, late_rows: int }
```
The legacy `/ws/frontend` STATE_UPDATE fields are NOT carried over; nothing in the new dashboard
may depend on them.

## REST
| Method & path | Body → Response | Notes |
|---|---|---|
| `GET /api/state` | → `Snapshot` | Same content as the WS snapshot. |
| `PUT /api/session/draft` | `{subject?, session_tag?, operator?}` → `Draft` | Allowed in any state; ignored fields keep values. |
| `POST /api/session/start` | `{}` → `{session_id, scheduled_start_ms, warnings: [Issue]}` | Uses the draft. 409 `blocked` with blockers. Records warnings as `preflight_failed` exactly as today. |
| `POST /api/session/stop` | `{reason?: str}` → `{session_id}` | Returns as soon as STOP is accepted; finalize runs in the background. 409 `bad_state` if not RECORDING. Idempotent for 10 s after a successful stop (second call returns the same body). |
| `POST /api/session/label` | `{label_id: 0..50}` → `{label_id, since_ms}` | 409 `bad_state` unless RECORDING. |
| `GET /api/sessions?limit=50&before=<sid>` | → `{sessions: [SessionSummary], next: str|null}` | Scans disk + ledger; cached, invalidated on change. |
| `GET /api/sessions/{sid}` | → `SessionDetail` | |
| `POST /api/sessions/{sid}/consolidate` | → `{zip_path}` | Merge late rows + verified phone rescue uploads into the ZIP now (the Done view's "Consolidate now"); every package runs the same decision first. 409 `busy` while the post-session job runs, 409 `bad_state` while the session is still RECORDING. |
| `POST /api/sessions/{sid}/package` | → `{zip_path}` | Re-run the package step (normally automatic). 409 `busy` if running. |
| `GET /api/sessions/{sid}/zip` | → file stream | `Content-Disposition: attachment; filename="<subject>_<tag>_<sid>.zip"`; supports Range. |
| `POST /api/sessions/{sid}/open-folder` | → `{}` | Windows `explorer /select,<zip>`; Linux `xdg-open`; 501 elsewhere. |
| `POST /api/notices/{id}/dismiss` | → `{}` | Persists server-side; broadcast in the next snapshot. |
| `POST /api/phones/reset` | → `{}` | Close all phone links + clear connectivity history. 409 `bad_state` while RECORDING/FINALIZING. |
| `GET /api/cameras/devices` | → `{devices: [CameraDevice]}` | Re-enumerates (≤ 1/s). |
| `PUT /api/cameras/selection` | `{cameras: [{device_key, cam_id?}]}` (≤ 5) → `{cameras: [Camera]}` | 409 `bad_state` while RECORDING. Slots cam1..cam5 assigned in list order unless given. Persisted in `_state/cameras.json`. |
| `GET /api/cameras/{cam_id}/preview.mjpg?w=640&fps=10` | → `multipart/x-mixed-replace` | Downscaled, rate-limited; only costs CPU while a client is connected. |
| `GET /api/cameras/{cam_id}/latest.jpg` | → `image/jpeg` | Full resolution latest frame. |
| `POST /api/calibration/references` | `{name?}` → `Reference` | See CALIBRATION.md. 409 `busy`. |
| `GET /api/calibration/references` | → `{references: [Reference], active_ref_id}` | |
| `PATCH /api/calibration/references/{ref_id}` | `{name}` → `Reference` | |
| `POST /api/calibration/references/{ref_id}/activate` | → `{}` | |
| `POST /api/calibration/deactivate` | → `{}` | |
| `DELETE /api/calibration/references/{ref_id}` | → `{}` | 409 if active. |
| `GET /api/calibration/references/{ref_id}/{key_slug}.jpg` and `/edges.png` | → image | |
| `GET /api/calibration/compare?ref_id=` | → `{ref_id, at_ms, cameras: [Drift + {device_key, cam_id}]}` | |

`Reference` = the `reference.json` object from CALIBRATION.md plus `thumb_urls`.

## WebSocket `/ws/ui`
Server → client messages (`type` discriminates):
| type | payload | rate |
|---|---|---|
| `snapshot` | `Snapshot` | On connect; then whenever anything in it changes, coalesced to ≤ 5 Hz; at least every 5 s as a heartbeat. Always the full snapshot (small; no patch logic). |
| `live` | `{t_ms, phones: {device_id: {acc: [x,y,z], gyro: [x,y,z], ts_ms}}}` | 10 Hz, only latest sample per phone. |
| `finalize` | `{session_id, steps: [FinalizeStep]}` | On every step change (also reflected in the next snapshot). |
| `calibration.drift` | `{ref_id, at_ms, cameras: [...]}` | 2 Hz while any client watches. |
| `ack` | `{req_id, ok: bool, error?: Error}` | Reply to client messages that carry `req_id`. |

Client → server messages:
| type | payload |
|---|---|
| `calibration.watch` | `{ref_id: str \| "active", req_id?}` |
| `calibration.unwatch` | `{req_id?}` |
| `ping` | `{req_id}` |

Commands (start/stop/label/…) go over REST, not the WebSocket, so they always get a status code.

Client rules (enforced in the dashboard, reviewed in L6):
- The UI renders only from the latest `snapshot` (+ `live` for charts). No browser storage of
  session state. `localStorage` may hold pure view preferences (overlay opacity, chart toggle).
- On socket loss: show a "reconnecting" bar, retry with backoff 0.5→5 s, and on reconnect use the
  fresh snapshot — no reconciliation logic.
- Unknown `type` values and unknown fields are ignored (forward compatibility).
- A render error in any panel is contained by an error boundary showing "This panel failed —
  reload" while the rest of the page keeps working.
