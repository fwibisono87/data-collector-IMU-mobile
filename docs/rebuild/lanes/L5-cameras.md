# Lane L5: camera service and calibration (backend)

Read these completely first: `docs/rebuild/SPEC.md` (§3, §6.4, §7, §10 V1–V3, C1–C2),
`docs/rebuild/CALIBRATION.md`, `docs/rebuild/API.md` (the camera and calibration endpoints and
models), `docs/OUTPUT_CONTRACT.md` §9 (video files, `cameras.json`, `video_report.json`),
`master_backend/app/camera/interface.py` and `fake.py` (the seam from lane L2, which you
implement), `master_backend/app/api/schema.py` (the models you must return), and
`master_backend/app/cameras.py` (the anchor writer you must call).

## Scope and hard rules
- Everything you write goes in `master_backend/app/camera/`, except:
  - new tests;
  - dependencies added to `master_backend/requirements.txt` (`Pillow`, plus `numpy` pinned to
    what scipy already pulls in);
  - `master_backend/app/camera/README.md`.
- Do NOT edit `interface.py`, `fake.py`, `app/api/schema.py`, `app/cameras.py`, `main.py`,
  `ws_handler.py` or `session_manager.py`. Lane L4 is editing the app wiring in parallel; it
  will import your `create_service()` and your `routes.router` if they exist. If the seam is
  missing something, say so in your report.
- The golden contract suite (`master_backend/tests/contract/`) must pass unmodified.
- ffmpeg and ffprobe are found via env `IMU_FFMPEG` / `IMU_FFPROBE`, then `vendor/ffmpeg/`
  next to the package, then `PATH`. On this machine they are on PATH: ffmpeg n9.0.2 with
  libvpx and libvpx-vp9, and a real webcam at `/dev/video0`. The production target is Windows
  (DirectShow), which you cannot run here, so keep the Windows code paths small and
  unit-tested on recorded text.

## 1. Discovery (`discovery.py`)
`list_devices() -> list[CameraDevice]` with `backend` set to `dshow`, `v4l2` or `fake`.
- **Windows (dshow).** Parse the stderr of
  `ffmpeg -hide_banner -list_devices true -f dshow -i dummy`. Support both output styles:
  - new style, one line per device: `[dshow @ 0x..] "Name" (video)`, then the next line
    `[dshow @ 0x..]   Alternative name "@device_pnp_\\?\usb#vid_...#{...}\global"`;
  - old style, with `DirectShow video devices` / `DirectShow audio devices` section headers.

  Skip `(audio)` and `(none)` entries. `device_key` = the alternative name; `label` = the
  friendly name. Unit-test with at least these fixtures, written out as text in the test:
  - two identical "USB Camera" devices with different alternative names;
  - an integrated camera;
  - a virtual camera such as "OBS Virtual Camera";
  - audio devices interleaved between them.
- **Linux (v4l2).** Use `/dev/v4l/by-id/*-video-index0` symlinks when present, falling back to
  `/dev/video*` devices that support capture. Probe the format with
  `ffmpeg -f v4l2 -list_formats all -i <dev>`. `device_key` = the by-id path, or the device
  path if there is none.
- **Fake.** Env `IMU_CAMERAS=fake` with `IMU_FAKE_CAMERAS=n` gives `fake:0 … fake:n-1`,
  labelled "Fake camera N".
- Re-enumerate at most once per second (cached).

## 2. Capture (`capture.py`)
One `CaptureWorker` per selected camera: a long-lived ffmpeg process writing MJPEG to stdout,
and a reader thread.
- **dshow.** `-f dshow -rtbufsize 256M -video_size 1280x720 -framerate 30 -vcodec mjpeg -i video=<alternative name> -c:v copy -f mjpeg pipe:1`.
  If no frame arrives within 4 s, or the process exits, retry once with the device's native
  format (drop `-vcodec mjpeg`, add `-c:v mjpeg -q:v 4`).
- **v4l2.** `-f v4l2 -input_format mjpeg -video_size 1280x720 -framerate 30 -i <dev> -c:v copy -f mjpeg pipe:1`,
  with the same fallback using `-c:v mjpeg -q:v 4`.
- **Fake.** `-re -f lavfi -i testsrc2=size=1344x784:rate=30,crop=1280:720:{32+dx}:{32+dy} -c:v mjpeg -q:v 4 -f mjpeg pipe:1`.
  `set_fake_offset(cam_id, dx, dy)` restarts the worker; it is used by the calibration tests.
- **Frame splitting.** Walk JPEG markers properly: SOI `FFD8`, then segments with lengths up to
  SOS, then scan the entropy-coded data for `FF` followed by a byte that is neither `00` nor
  `D0..D7`. Do not split on the first `FFD9` you see, because an EXIF thumbnail contains one.
  Stamp each frame with arrival `time.time_ns()` and parse width and height from SOF0/SOF2.
- **State.** The worker keeps:
  - the latest frame as `(bytes, arrival_ns, w, h)`;
  - fps measured over a 2 s window;
  - a state of `opening`, `live`, `lost` or `error`, plus a `dropped_frames` counter.

  Reconnect with a 2 s backoff when the process dies. Log ffmpeg stderr through a bounded ring
  buffer and surface its last line as `error`.
- **Shutdown.** Write `q` to stdin, wait 5 s, then kill. On Windows, create children with
  `CREATE_NEW_PROCESS_GROUP` and put them in a Job Object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` via ctypes, so they die with the backend. Guard this
  behind `sys.platform == "win32"` and cover it by code review only.
- **Subscribers.** A worker can feed multiple subscribers (recorder queue, preview, calibration)
  without copying frames more than once. Subscriber queues are bounded; when one overflows,
  drop for that subscriber and count it. The reader thread must never block.

## 3. Recording (`recorder.py`)
- On `start_recording(session_id, folder)`:
  - for each live selected camera, spawn an encoder:
    `ffmpeg -hide_banner -loglevel error -f mjpeg -use_wallclock_as_timestamps 1 -i pipe:0 -c:v <codec opts> -an -f webm <folder>/<sid>_<camId>_video_sync.webm`;
  - feed it frames from the worker through a bounded queue of 2 s worth of frames. When the
    queue is full, drop the oldest and count it; never block the reader.
- Codec, from env `IMU_VIDEO_CODEC` = `vp9` (default) or `vp8`:
  - vp9: `-c:v libvpx-vp9 -deadline realtime -cpu-used 8 -row-mt 1 -b:v 2500k -g 60`, with
    `mime="video/webm;codecs=vp9"`;
  - vp8: `-c:v libvpx -deadline realtime -cpu-used 8 -b:v 2500k -g 60`, with
    `mime="video/webm;codecs=vp8"`.
- **Benchmark.** Run 5 fake cameras at 720p30 for 60 s with each codec on this machine. Report
  CPU %, real encoder fps vs 30, and dropped frames. If vp9 cannot sustain 5 cameras with
  fewer than 1 % drops, make vp8 the default and say so.
- **Anchors** go through `await master_backend.app.cameras.cameras_mark(session_id, body)`,
  where body is `{cam_id, event, ts_ms, device_id: device_key, label, mime}`:
  - `started` and `flash` (same value) = arrival time of the first frame written, in ms;
  - `stopped` = arrival time of the last frame written.
- **Per-frame timing.** Write `<folder>/_extras/<sid>_<camId>_frames.csv` with header
  `part,frame_index,arrival_epoch_ms`, buffered and flushed every second.
- **CameraEvent records.** Build them exactly as the browser did:
  - `key = f"{sessionId}__{camId}__{atMs}__{uuid4().hex[:8]}"`;
  - record `started` and `stopped` for every camera, plus `track_ended` and `write_error`
    when they happen;
  - `detail` is None unless there is text.
- **Camera lost mid-recording.** If the capture worker goes `lost` during recording:
  - record a `track_ended` `CameraEvent` and an anchor with `event="track_ended"`;
  - close the current encoder cleanly;
  - when the worker is live again, start a new encoder writing `_part2`, `_part3`, …;
  - an encoder crash is a `write_error` event and the same part rollover.
- **`stop_recording(session_id)`.**
  - Close every encoder's stdin and wait up to 15 s, so the WebM muxer writes Duration and
    Cues.
  - If an encoder had to be killed, remux in place: `ffmpeg -i f -c copy tmp.webm`, then
    replace.
  - Probe each file with `ffprobe -v error -show_entries format=duration -of json`.
  - Build each `VideoFileReport`: `ok` = the probe succeeded and duration is present;
    `bytesWritten` = file size; `durationMs` = the probed duration, rounded; `ebmlHeaders` =
    the count of `1A45DFA3` at top-level element positions (walk the EBML top level; do not
    substring-search).
  - Return a `CameraStopResult`. A camera that produced no file goes into `missed`.
  - Never raise for a single-camera failure.
  - Must return within 30 s for 5 cameras.

## 4. Service (`service.py`)
`create_service() -> CameraService`: a `RealCameraService` implementing the L2 Protocol in full.
- Selection persists in `SSD_PATH/Data_Riset_IMU/_state/cameras.json` as
  `[{cam_id, device_key}]`. Slots `cam1..cam5` are assigned in order.
- On startup, reopen the saved selection for devices that are present.
- `set_selection` while recording raises a `CameraError` with code `bad_state`.
- `cameras()` returns the API `Camera` models, including `drift` against the active reference
  (refreshed every 10 s while idle, when an active reference exists).
- `readiness()`:
  - blockers: no camera selected; a selected camera not live;
  - warnings: drift verdict is CHECK, MOVED, RESOLUTION_CHANGED or MISSING.
- `async compare(ref_id)`: used by L4's WebSocket hub for `calibration.watch`; it accepts
  `"active"` as the ref_id.
- Calibration session checks: append to `session_checks.jsonl` at start and at stop (see
  CALIBRATION.md).
- Do not report "camera moved during the session" through the stop result's events; that
  would misuse the event types. Instead, expose
  `last_session_drift_change(session_id) -> str | None` and list it in your report as a seam
  addition. L4 can poll it later.

## 5. Calibration (`calibration.py`)
Implement CALIBRATION.md exactly:
- storage layout and atomic writes;
- simultaneous capture (the first frame at or after the trigger, per camera, skew recorded);
- references: list, rename, activate, deactivate and delete (deleting the active reference is
  refused);
- precomputed `edges.png`;
- phase correlation with numpy FFT on Sobel gradient images, Hann window, sub-pixel peak, NCC
  score, and the verdict thresholds as named constants;
- at most 2 Hz per camera, and under 30 ms per camera at 320 px width.

## 6. Routes (`routes.py`, `router = APIRouter(prefix="/api")`)
Every `/api/cameras/*` and `/api/calibration/*` endpoint in API.md, using the L2 models as
`response_model`.
- Errors use the API.md shape: return a `JSONResponse` with
  `{"error": {code, message, details}}` and the right status. Do not rely on L4's handler.
- `preview.mjpg`:
  - `multipart/x-mixed-replace; boundary=frame`;
  - downscale with Pillow to the requested width (default 640) only while a client is
    connected;
  - at most `fps` (default 10) frames per second;
  - stop cleanly on client disconnect;
  - at most 4 concurrent preview clients per camera (429 beyond that).
- `latest.jpg`: the raw latest frame.
- The service instance comes from `request.app.state.camera_service` if set (L4 sets it).
  Otherwise use a module-level singleton from `create_service()`, so your tests can mount the
  router alone.

## 6b. Readiness of `/dev/video0`
On this machine the real webcam may be busy or missing. Tests that need it must skip cleanly
when it cannot be opened (`pytest.mark.skipif` after a 3 s probe).

## 7. Tests (all with fake cameras unless stated; mark slow ones with `@pytest.mark.slow`)
- **Unit:**
  - dshow parser fixtures (§1);
  - JPEG splitter: concatenated frames, an EXIF thumbnail, frames split across arbitrary read
    boundaries, and garbage between frames;
  - EBML top-level header counter on a real recorded file and on two files concatenated (= 2);
  - phase correlation: shifts (0,0), (5,−3), (20,0) and (−40,25) on a textured image within
    ±1 px; ±30 % brightness and ±15 % contrast stay OK; a 3° rotation gives CHECK or MOVED; a
    black frame gives NO_SIGNAL.
- **Integration** (2–3 fake cameras):
  - select → live within 5 s;
  - `start_recording` → 10 s → `stop_recording`; then for each file assert:
    - ffprobe duration within 1 s of `stopped - started`;
    - exactly one EBML header;
    - the `<sid>_cameras.json` anchors are written;
    - the frames.csv row count matches the frame count within 2 %.
- **Part rollover:** kill one capture process mid-recording, wait for it to come back, then
  stop. Expect `_part2`, a `track_ended` event, and both files listed in order.
- **Killed encoder:** SIGKILL an encoder → stop remuxes the file → it gets Duration and Cues.
- **Calibration:**
  - capture a reference set from 5 fake cameras and assert max |skew_ms| ≤ 1 frame + 10 ms;
  - `set_fake_offset(cam1, 20, 0)` gives MOVED with dx ≈ 20 ± 2 px; offset 0 gives OK.
- **Routes:** `TestClient` with the router mounted alone.
  - `devices`, `selection` (409 while recording), `latest.jpg`;
  - `preview.mjpg` returns at least 3 parts within 2 s;
  - the reference CRUD and compare endpoints.
- **Optional real webcam smoke test** on `/dev/video0`: open, 3 s preview, 5 s recording, a
  playable file.
- **Full suite:** `master_backend/.venv/bin/python -m pytest master_backend/tests -q -m "not slow"`
  with zero failures, then the slow tests on their own, also with zero failures.

## Deliverable
- Logical commits made with `git commit --no-gpg-sign`. Messages explain why; no trailers.
- `master_backend/app/camera/README.md` covers:
  - the process model (capture worker, subscribers, encoder);
  - Windows notes, including how to find a camera's alternative name;
  - the env variables;
  - the codec benchmark table.
- Final report:
  - the test output;
  - the benchmark results and the default codec you chose;
  - every seam addition or doubt.
