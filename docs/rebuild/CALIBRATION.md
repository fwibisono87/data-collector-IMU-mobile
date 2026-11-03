# Camera calibration — reference stills and ghost overlay

Part of the rebuild spec ([SPEC.md](SPEC.md) §7). Backend in lane L5, UI in lane L6.

## Purpose
Camera placement must not change between sessions (or during one) without anyone noticing,
because the video is used together with the IMU data. The operator needs to:
1. capture a **reference**: one still from every camera, all taken at the same moment;
2. later, open **calibration mode** and see each live camera with the reference laid over it as
   a ghost image, so they can bring the camera back to the original framing — and get a
   numeric verdict, not just a visual impression.

## Concepts
- **Reference set**: one capture event across all live selected cameras. Has an id, a name, a
  creation time, and one still per camera, keyed by the camera's stable **device key** (not
  the slot `camN`, because slots can be re-ordered or a camera re-plugged).
- **Active reference**: at most one reference set is active; it drives the readiness warning
  and the START/STOP drift log. The newest set is *not* activated automatically; the operator
  does that with one click (the dialog after capture offers "Make active").

## Storage (outside every session folder; not part of the frozen contract)
```
SSD_PATH/Data_Riset_IMU/_calibration/
  references/<ref_id>/
    reference.json
    <device_key_slug>.jpg          full-resolution frame exactly as captured (no re-encode
                                   when the camera delivers MJPEG)
  active.json                      {"ref_id": "...", "activated_at_ms": ...}
  session_checks.jsonl             one line per (session, moment): see below
```
`ref_id` = `ref_<YYYYMMDD-HHMMSS>_<4 hex>`. `device_key_slug` = device key with
`[^A-Za-z0-9._-]` → `_`, truncated to 80 chars + 8-char hash.

`reference.json`:
```json
{
  "ref_id": "ref_20261030-091502_a3f1",
  "name": "lab A — tripod layout",
  "created_at_ms": 1793351702000,
  "trigger_ms": 1793351702000,
  "cameras": [
    {
      "device_key": "@device_pnp_\\\\?\\usb#vid_046d&pid_085c...",
      "label": "C922 Pro Stream Webcam",
      "cam_id_at_capture": "cam1",
      "file": "<device_key_slug>.jpg",
      "width": 1280, "height": 720,
      "frame_arrival_ms": 1793351701987,
      "skew_ms": -13
    }
  ]
}
```
Writes are atomic (temp dir, then rename the directory into place).

## Capturing (simultaneity)
`POST /api/calibration/references {name?}`:
1. Record `trigger_ms = now`.
2. For each live selected camera, take the **first frame whose arrival time is ≥ trigger_ms**
   (wait up to 2 frame intervals); this makes all stills come from the same instant within one
   frame interval (~33 ms at 30 fps), rather than "latest frame", which can be up to a frame old
   on one camera and fresh on another. If a camera produces no frame within 500 ms it is
   reported as failed and the set is still saved with the others (the response lists failures).
3. Store the JPEG bytes as received; record `frame_arrival_ms` and `skew_ms = arrival - trigger`.
Allowed during IDLE and RECORDING (taking stills never interrupts recording). Not allowed while
another capture is in progress (409).

Ad-hoc use: the same button is the "take a still of all cameras" feature. Every reference set is
listed in calibration mode with thumbnails, can be renamed, activated or deleted (delete asks for
confirmation; the active set cannot be deleted without first deactivating).

## Drift measurement (backend)
For each camera in the chosen reference that is currently live:
1. Decode reference and latest live frame; convert to grayscale; resize both to 320 px width
   (keeping aspect) — the live frame is resized to the **reference's** resolution first if they
   differ (if aspect ratios differ, report `verdict = "RESOLUTION_CHANGED"`).
2. Reduce lighting sensitivity: Gaussian blur σ=1, Sobel gradient magnitude, normalise each to
   zero mean / unit variance, apply a Hann window.
3. **Phase correlation** (numpy FFT) → sub-pixel translation (dx, dy) at 320 px scale, peak
   strength `confidence ∈ [0,1]`; scale dx, dy back to reference pixels.
4. **Similarity**: normalised cross-correlation of the gradient images after shifting by
   (dx, dy) → `score ∈ [-1,1]`.
5. Verdict (thresholds are config constants, expressed relative to the reference width W):
   - `OK`: |shift| ≤ 0.5 % W and score ≥ 0.6
   - `CHECK`: |shift| ≤ 1.5 % W, or score between 0.4 and 0.6
   - `MOVED`: |shift| > 1.5 % W, or score < 0.4 (likely rotated/zoomed/obstructed)
   - `NO_SIGNAL`: confidence < 0.05 (e.g. lens covered, blank scene)
   - `MISSING`: camera not connected/selected
Rotation and zoom are not estimated in this version; they show up as a low score → `MOVED`, and
the visual overlay is the tool to fix them.

Performance: computed at ≤ 2 Hz per camera, only while a calibration view is open or a check is
requested; ≤ 30 ms per camera on the target laptop (numpy only; Pillow for decode; no OpenCV
dependency unless already available).

API:
- `GET /api/calibration/compare?ref_id=<id|active>` → `{ref_id, at_ms, cameras:[{device_key,
  cam_id, verdict, dx_px, dy_px, shift_pct, score, confidence}]}`
- While at least one browser has calibration mode open it sends `{"type":"calibration.watch",
  "ref_id": ...}` on `/ws/ui`; the backend then pushes `calibration.drift` messages at 2 Hz and
  stops 5 s after the last watcher leaves.

## Readiness and session log
- If an active reference exists, readiness includes a **warning** per camera whose verdict is
  CHECK, MOVED, RESOLUTION_CHANGED or MISSING (measured once per 10 s while in Setup).
  Never a blocker.
- At START (just before cameras begin recording) and at STOP (just before they stop), the
  backend appends to `session_checks.jsonl`:
  `{"session_id", "moment": "start"|"stop", "at_ms", "ref_id", "cameras":[...same items as compare...]}`
  A START→STOP change of verdict or a shift difference > 0.5 % W raises a session notice
  "cam2 appears to have moved during the session (Δ 18 px)".

## Calibration mode (UI)
Entry: "Calibrate" button in the Setup cameras panel (also reachable during RECORDING, read-only
— no camera re-selection).

Layout: a reference picker (thumbnails + name + date + active badge) at the top, then a grid of
camera panels (1 large if one camera, 2×2 / 2×3 otherwise; click a panel to make it full-screen).
Each panel:
- Live MJPEG preview with the reference still drawn on top in a `<canvas>` or stacked `<img>`
  with CSS, same size and aspect.
- Controls (apply to all panels, overridable per panel):
  - **Opacity** slider 0–100 % (default 50 %).
  - **Mode**: *Blend* (normal alpha), *Difference* (`mix-blend-mode: difference` — anything that
    moved lights up, a perfectly aligned static scene goes black), *Blink* (alternate reference
    and live every 500 ms; space bar toggles manually), *Side by side*.
  - **Edges only** toggle: show the reference as a thin edge outline (precomputed edge PNG served
    by `GET /api/calibration/references/{ref_id}/{device_key_slug}/edges.png`) instead of the
    full photo — easiest for fine alignment.
  - Grid / crosshair overlay toggle (rule-of-thirds + centre cross).
- A verdict badge (OK green / CHECK amber / MOVED red / NO SIGNAL grey / MISSING grey) with
  `Δx, Δy` in px and an arrow indicating which way the image content has shifted, updated at
  2 Hz.
Buttons: "Take new reference" (captures a new set; asks whether to make it active), "Make
active", "Done".

## Tests
- Unit: phase correlation recovers synthetic shifts of (0,0), (5,−3), (20,0), (−40,25) px on a
  textured test image within ±1 px; ±30 % brightness and ±15 % contrast changes keep verdict OK;
  a 3° rotation yields CHECK or MOVED; a black frame yields NO_SIGNAL.
- Integration with the fake camera backend (`lavfi testsrc2` with configurable `crop`/`pad`
  offset): reference at offset 0, live at offset 20 → MOVED with Δ ≈ 20 ±2 px (acceptance C2).
- Simultaneity: 5 fake cameras, capture a set, assert max |skew_ms| ≤ 1 frame interval + 10 ms
  (acceptance C1).
- API: create / list / rename / activate / delete; delete of the active set refused; capture
  during RECORDING does not disturb the recording (frame counts continue).
