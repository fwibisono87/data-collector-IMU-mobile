# `master_backend.app.camera` — server-side camera capture, recording and calibration

Everything the backend does with cameras: device discovery, MJPEG capture,
WebM recording with part rollover, `<sid>_cameras.json` anchors, and the
calibration reference store with drift measurement. The browser dashboard is
a pure client of the HTTP routes in `routes.py`; no video ever flows through
the browser.

## Modules

| Module | Responsibility |
| --- | --- |
| `bins.py` | Locate/spawn `ffmpeg`+`ffprobe` (env → `vendor/ffmpeg/` → `PATH`); Windows job-object membership; polite `q`-on-stdin shutdown. |
| `jpeg.py` | `JpegFrameSplitter`: incremental segment-aware MJPEG frame splitting (EXIF thumbnails are not split points); `sof_size()` geometry. |
| `webm.py` | EBML walker: `count_ebml_headers`, `has_cues`, `probe_duration_ms`, `probe_frame_count`, `remux` (recovers Duration+Cues for a truncated file). |
| `discovery.py` | Device lists: dshow (Windows, both ffmpeg output styles), v4l2 (Linux), fake (`IMU_CAMERAS=fake`). |
| `capture.py` | One `CaptureWorker` per selected camera: owns the capture ffmpeg child, splits frames, fans out to subscribers, auto-reconnects on dropout. |
| `recorder.py` | `CameraRecorder` per camera during a session: writer thread → encoder child; monitor thread → part rollover; `AnchorBridge` (private loop) → `cameras_mark`; `FramesCsv`. |
| `calibration.py` | Reference store (`references/<ref_id>/`), phase-correlation `measure_drift`, verdicts, `DriftMeter` (≤ 2 Hz per camera with cache). |
| `service.py` | `RealCameraService`: selection, recording lifecycle, drift loop (10 s), reference CRUD, session-start/end checks. |
| `routes.py` | The 13 HTTP endpoints under `/api/cameras/*` and `/api/calibration/*`. |
| `benchmark.py` | `python -m master_backend.app.camera.benchmark` — the codec benchmark below. |

## Process model

Per selected camera slot (`cam1`…`cam5`), while the backend runs:

```
ffmpeg capture child ──stdout(MJPEG)──▶ CaptureWorker thread
   (dshow/v4l2 mjpeg,                     │ JpegFrameSplitter → Frame
    1280x720@30 default)                  │ non-blocking fan-out, bounded queues
                                          ├──▶ preview subscribers (drop newest, ≤ 4 clients/slot)
                                          ├──▶ latest-frame subscriber (depth 64, drop oldest)
                                          └──▶ recorder subscription (drop oldest, counted)
```

* **Capture worker** — one thread per camera reads the capture child's stdout
  in 256 KB chunks and splits frames. If the child dies, the worker respawns
  it automatically (frames resume from the *new* process; the gap is reported
  as `track_ended` when it happens mid-recording).
* **Subscribers** — each has its own bounded queue; fan-out never blocks the
  capture thread. Overflow policy differs by consumer: the recorder keeps the
  *tail* of an overrun (`drop_oldest=True`, drops counted in
  `dropped_frames()`), previews keep the *newest* request rate.
* **Encoder** — during recording, one writer thread per camera feeds frames
  into `ffmpeg -f image2pipe … -f webm <part>.webm` over stdin, **flushing
  after every frame** (the pipe carries a 1 MB buffer; without the flush the
  encoder is fed in bursts, slow codecs stall the queue, and a killed encoder
  loses everything buffered). A monitor thread watches both children:
  * capture dropout → current part is closed, `_part2.webm`, … opened on
    reconnect; `track_ended` anchor;
  * encoder death (OOM, kill) → `write_error` anchor, rollover to a new
    part; a killed part is remuxed at `stop_recording` so it stays playable
    (Duration + Cues recovered from the raw clusters).
  Clean close = close stdin and let the muxer finalise; kill after 15 s.
* **Anchors** — `AnchorBridge` runs a private asyncio loop on its own thread
  so recorder threads can call `cameras_mark` (async) the moment an event
  happens, even when the caller's loop is between awaits. Anchors arrive in
  `<session folder>/<sid>_cameras.json` (or `Data_Riset_IMU/_pending_cameras/`
  until the session folder exists — see the sort-order caveat below).
* **frames.csv** — `_extras/<sid>_<camId>_frames.csv` with
  `part,frame_index,arrival_epoch_ms` per written frame, flushed 1×/s.

### Sort-order caveat for session folders (production, not test-only)

`cameras.py::_cameras_path` resolves the session folder through
`export._session_folders`, which returns the **first sorted** match — and a
pending anchor file makes `_pending_cameras` itself a match. `"_"` (0x5F)
sorts before lowercase letters but *after* digits and uppercase, so session
folders must sort before `_pending_cameras` (e.g. start with a digit or
uppercase letter) or anchors will be trapped in the pending folder forever.
Real session ids like `S001_...` are fine; a lowercase `subj_...`-style
folder is not. Flagged to the L2/export owner — `cameras.py` and `export.py`
are not editable in this lane.

## Windows notes (dshow)

* Discovery runs `ffmpeg -hide_banner -list_devices true -f dshow -i dummy`
  and parses **stderr**. Both ffmpeg output styles are understood: the new
  per-device style (`"Name" (video)` + `Alternative name` on the next line)
  and the old section style (`DirectShow video devices` header decides video
  vs audio). The `[dshow @ …]` log prefix is accepted with or without the
  `0x` pointer prefix (older builds print bare hex).
* **Finding a camera's alternative name** — run that same command in a
  terminal; the `Alternative name "@device_pnp_\\?\usb#vid_…"` line printed
  directly under the device label *is* the name to use. It is the only stable
  identifier: two identical USB cameras share the label ("USB Camera" twice)
  but have different alternative names, which is why `device_key` is the
  alternative name whenever present (falls back to the label).
* Capturing then uses `-f dshow -i video="@device_pnp_\\?\…"`; audio devices
  (`@device_cm_…\wave_…`) and `(none)` entries are filtered out of discovery.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `IMU_FFMPEG` / `IMU_FFPROBE` | `vendor/ffmpeg/`, then `PATH` | Explicit binary paths. |
| `IMU_CAMERAS` | (real discovery) | `fake` → synthetic lavfi testsrc2 cameras (development, tests, benchmark). |
| `IMU_FAKE_CAMERAS` | `2` | Number of fake cameras (`fake:0`…`fake:n-1`). |
| `IMU_VIDEO_CODEC` | `vp9` | `vp9` or `vp8`; read at each recording start (`recorder.codec_config`). |
| `IMU_CAM_WIDTH` / `IMU_CAM_HEIGHT` / `IMU_CAM_FPS` | `1280` / `720` / `30` | Capture geometry request. |
| `SSD_PATH` | `./data` | Root for session folders, selection state (`_state/cameras.json`) and calibration references. |

## Codec benchmark — default is **vp9**

Spec §3: 5 fake 720p30 cameras, 60 s per codec, CPU measured per process
from `/proc`. Reproduce with:

```
master_backend/.venv/bin/python -m master_backend.app.camera.benchmark \
    --duration 60 --cameras 5 --codecs vp9,vp8
```

Result on the development machine (12 cores, ffmpeg n9.0.2, 2026-10-30):

| Codec | Encoder fps (target 30) | Frames dropped | Backend+capture+encode CPU | % of machine | Verdict |
| --- | --- | --- | --- | --- | --- |
| **vp9** (`libvpx-vp9 -deadline realtime -cpu-used 8 -row-mt 1 -b:v 2500k -g 60`) | **30.0** | **0 / 9244 = 0.00 %** | 448.7 % of one core (backend 46 %, ~22 % per capture, ~58 % per encoder) | **37.4 %** | **SUSTAINS** |
| vp8 (`libvpx -deadline realtime -cpu-used 8 -b:v 2500k -g 60`) | 2.7 | 5907 / 7275 = 81.20 % | 1119.7 % of one core (backend 33 %, ~15 % per capture, ~200 % per encoder) | 93.3 % | FAILS |

vp9 sustains 5×720p30 with zero drops at ~37 % of the machine; vp8 cannot
keep up on this ffmpeg build (each encoder burns ~2 cores and still only
manages ~2.7 fps, the rest of the frames are dropped by the bounded
recorder queues). Per the spec's rule, **vp9 stays the default codec**
(`IMU_VIDEO_CODEC` unset → vp9). An earlier vp8 run before the per-frame
stdin flush showed similar failure, so the drops are genuine encoder
throughput, not a feeding artifact.

## Reference image URLs (exact)

Each camera of a reference stores two images under
`<SSD_PATH>/Data_Riset_IMU/_calibration/references/<ref_id>/`, both
served by dedicated routes. `key_slug` is `device_key_slug(device_key)`:
every `[^A-Za-z0-9._-]` → `_`, truncated to 80 chars, then `_` + the first
8 hex digits of `sha256(device_key)` (e.g. `fake:4` → `fake_4_0de5fe0a`).

* **JPEG still** — `GET /api/calibration/references/{ref_id}/{key_slug}.jpg`
  This exact URL is what `Reference.thumb_urls[device_key]` returns
  (`calibration.py::_thumb_urls` builds it from the stored filename).
* **Edges PNG** — `GET /api/calibration/references/{ref_id}/{key_slug}.edges.png`
  The dashboard derives it from `thumb_urls` by replacing the trailing
  **`.jpg`** with **`.edges.png`** (both routes are registered side by side
  in `routes.py`, the file is rendered by `render_edges_png` at capture
  time). The two URLs always share the same `{ref_id}/{key_slug}` prefix, so
  the derivation is exact.

Concrete example (reference `ref_20261030-014512_a1b2`, device `fake:4`):

```
still:  /api/calibration/references/ref_20261030-014512_a1b2/fake_4_0de5fe0a.jpg
edges:  /api/calibration/references/ref_20261030-014512_a1b2/fake_4_0de5fe0a.edges.png
```

## Calibration notes

* Drift sign convention: `live(x, y) ≈ ref(x + dx, y + dy)`; a fake crop
  offset of `+20` therefore measures `dx = +20.0` (± 1 px).
* Verdict ladder (`verdict_for`): shift ≤ 0.5 % and score ≥ 0.6 → OK;
  ≤ 1.5 % and ≥ 0.4 → CHECK; else MOVED; correlation confidence < 0.05 →
  NO_SIGNAL. Compares are cached 0.5 s per camera (≤ 2 Hz).
* `activate_reference` triggers an immediate drift refresh plus a 10 s
  background loop; the very first compare after activation may serve a
  result cached within the last 0.5 s (dashboards poll, so this is
  invisible in practice).
* The fake source (lavfi `testsrc2`) is **animated** — it has a ~0.35 s
  vertical scroll period, so a compare within one period of the reference
  still can lock onto a scroll alias (dy ≈ −64…−88 px) and the score
  oscillates 0.64–0.89, near the OK threshold. Tests poll compare until
  verdicts settle; real cameras are unaffected.

## Tests

`master_backend/tests/camera/` — fast unit/route tests plus a `slow`-marked
integration suite (10 s recording, part rollover, SIGKILLed encoder,
calibration C1/C2, optional `/dev/video0` real-webcam smoke that skips
cleanly when the device is absent or busy):

```
master_backend/.venv/bin/python -m pytest master_backend/tests -q -m "not slow"
master_backend/.venv/bin/python -m pytest master_backend/tests -q -m slow
```

The directory is a package (`__init__.py`) on purpose: without it pytest
imports this folder's `conftest.py` under the plain name `conftest`,
shadowing `tests/conftest.py` and breaking
`from conftest import FakeWs` in the session-lifecycle and contract suites.
