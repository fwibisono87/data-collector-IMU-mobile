# Field checklist — before, during and after a session

Written after the 2026-08-07 incident, in which a 26-minute 3-device session ended with
"Application Error", no ZIP, and (apparently) no data. The IMU data turned out to be safe on
the backend all along; the video survived only because nobody pressed START again. Two of the
three phones had also been silently recording at half rate for the entire session.

Every item below exists because something actually went wrong. Where the rebuild removed the
failure, the item says so — do not delete it, the next person will re-invent the bug.

The app is now Setup → Record → Done in the browser, and the ZIP is written by the backend.
Nothing that matters is held in the browser.

## 0. Setup readiness (one screen, before anything else)

The Setup screen shows three panels — Session, Phones, Cameras — and the backend's own
readiness list. Read it; it is not decorative.

**Blockers (START is disabled until each is fixed):**

- [ ] Disk: `SSD_PATH` is writable with **≥ 5 GB** free. The message names the disk.
- [ ] At least **1 phone online** (green on its card).
- [ ] **Subject, Tag, Operator** filled. (The draft is stored on the backend, so a reload or
      a second browser shows the same values; Tag and Operator survive a session, Subject is
      cleared for the next one.)
- [ ] **≥ 1 camera selected**, and every selected camera is live (preview tile moving).
      Cameras are numbered `cam1`…`cam5` in selection order and the choice is stored on the
      server, so it survives a reload.
- [ ] No session is already recording or finalising.

**Warnings (START reads "Start anyway"; starting with them is recorded in the session):**

- [ ] **Sampling rate ≥ 90 Hz** on every phone. This is *distinct readings per second*, not
      packets: a phone can stream a confident 100 packets/sec while the OS re-delivers each
      hardware reading twice, giving 50 Hz of real data. On 2026-08-07 two devices recorded at
      ~50 Hz for 26 minutes and half of every one of those files is interpolated padding.
      If a phone fails, in this order:
      1. Battery: turn off Battery Saver / Adaptive Battery for the app (per-phone settings:
         [`REDMI_NOTE_12_SETUP.md`](REDMI_NOTE_12_SETUP.md)).
      2. Confirm the foreground-service notification is showing.
      3. Keep the screen on, or grant unrestricted background power use.
      4. Only then consider probing the sensor floor — the Android sampling period is a
         *hint* and the OS clamps it to a supported rate.
- [ ] **All phones on the same app version.** Each card shows `app <version>`; a mismatch is
      a warning here and a mystery later. (Three phones once ran a stale pre-v2 build for
      days, which left `acc_ts_ms`, `gyro_ts_ms` and `sample_kind` empty on every row, so
      nothing could tell a held sample from a real one.)
- [ ] **Clock offset known** on every phone (the app does an NTP-lite burst on connect).
- [ ] **Disk ≥ 20 GB** free — below that, STOP may fail while writing the ZIP.
- [ ] **No camera has drifted** from the active calibration reference (see §1).

## 1. Cameras: reference stills + calibration, before each subject

A bumped tripod invalidates the whole session's video geometry and is invisible in the
footage until analysis.

- [ ] Select the cameras, confirm every preview is moving.
- [ ] **Take reference stills** — one button captures the latest frame of **every** live
      selected camera at the same instant and stores it as a named set on the backend.
- [ ] Press **Calibrate**: each camera shows the reference as a ghost over the live feed.
      Use **Blend**, **Difference**, **Blink** (A/B every 0.5 s) or **Side by side**, and the
      centre crosshair; the backend estimates `Δx, Δy` and shows **OK / CHECK / MOVED**.
- [ ] Every camera must read **OK** (a few pixels is CHECK; MOVED means the camera physically
      moved — re-mount it, then take fresh reference stills).
- [ ] Make the newest set **active**. The active reference is what the readiness warning and
      the per-session `_calibration/session_checks.jsonl` compare against, so a camera bumped
      *during* a session is detectable afterwards.

Do this **before each subject**, not once per day: subjects get repositioned, the laptop gets
moved, the tripod gets kicked.

## 2. During recording (Record view)

- [ ] START is one button; when the backend is RECORDING the view switches by itself in
      every open browser.
- [ ] Check the label is the one you mean, and that the elapsed timer runs.
- [ ] **Labels** — click the 0–50 grid, or type the number and press **Enter**. `0` + Enter or
      **Esc** returns to baseline. Labels are 0–50, 0 = baseline; the active label and how
      long it has been active are shown huge. A change shows a pending state until the
      backend acknowledges it; failure appears inline (there are no `alert()` popups any
      more). Labels are applied **server-side** — the phone CSVs carry the last label the
      phone heard, so the backend CSV is the authoritative labelled copy.
- [ ] **Phone strip** — one chip per phone: online, or `OFFLINE mm:ss` with
      *"data is buffering on the phone — do not stop yet"*. An offline phone is still
      recording to its own rescue CSV; it re-sends when the network returns. **Do not stop
      the session while a phone is offline** unless you must — wait for it to come back.
- [ ] **Camera strip** — live thumbnails with a REC dot. A camera that drops shows
      *"lost — reconnecting"*; if it comes back, recording continues automatically into a
      `_part2` file and `cameras.json` records the gap.
- [ ] **STOP is a 1 second press-and-hold** (or hold **Space**): *"Hold to stop · 1 s"*. It is
      deliberately hard to hit by accident, and it sends one request and returns immediately.
- [ ] Watch for `below 90 Hz` on a chip mid-session — that phone is recording padded samples.

## 3. After STOP (Done view)

- [ ] Finalize steps run with live status (close CSVs, stop cameras and finalise video,
      validate, late window, write ZIP). Every step shows its duration; a failure shows its
      error text instead.
- [ ] Read the **verdict**: PASS / PARTIAL / FAIL, with per-phone reasons. PARTIAL/FAIL names
      the device and reason — write it on the session sheet.
- [ ] **The ZIP is already on disk.** The Done view shows the session folder and the ZIP path,
      with **Open folder** (opens Explorer on the laptop). **Download ZIP** exists but is
      never required; there is no modal and nothing to acknowledge.
- [ ] Late data and rescue uploads appear as rows ("chest: 1,204 late rows merged",
      "rescue upload 4.1 MB · verified ✓"). If one arrives later, the ZIP is rebuilt
      automatically — the row updates live, no popup.
- [ ] **New session** returns to Setup.

A reload of the page during or after STOP is harmless: the Done view reappears with the same
progress, because it is driven by the backend's per-session record.

## 4. Where the data lives

**On the laptop** (`SSD_PATH`, usually `D:\IMU_Data_SSD`):

```
Data_Riset_IMU/<subject>_<tag>/
  <sid>_<role>_sensor_data.csv            IMU, one per phone (frozen format)
  <sid>_<role>_sensor_data_late.csv       buffered rows delivered after STOP, if any
  <sid>_cam<1..5>_video_sync[_partN].webm video, one file per uninterrupted run
  <sid>_cameras.json                      anchors, camera events, per-file report
  <subject>_<tag>_<sid>.zip               the deliverable (built by the backend)
  _extras/                                per-frame timestamps, camera result — outside the ZIP
Data_Riset_IMU/_calibration/              reference stills, overlays, session drift checks
Data_Riset_IMU/_state/                    notices, draft, session ledger (not research data)
```

**On each phone** (`/sdcard/Android/data/id.ac.ui.cs.csl.fams.sapper/files/imu_sessions/`): the
rescue CSV for every session the phone took part in, plus `events.jsonl` and
`integrity.json` sidecars. This copy is written from START to STOP regardless of Wi-Fi and
**carries no labels** (labels are applied server-side), but it is the only copy guaranteed to
be complete if the phone never got its data out:

```bash
adb pull /sdcard/Android/data/id.ac.ui.cs.csl.fams.sapper/files/imu_sessions ./local_backup
```

Pulling it does not remove it; the app keeps the newest ~20 sessions.

## 5. If the dashboard crashes

**Just reload it (F5).** Nothing important lives in the browser any more: no IndexedDB, no
`MediaRecorder`, no ZIP built in a tab, no "unsaved footage" banner. All session state, video,
notices and the ZIP are on the laptop.

1. Reload. The view comes back where the session actually is (Setup / Record with the right
   elapsed time / Done with the same finalize progress).
2. If the browser itself is broken, open the dashboard in another browser or on another
   machine: `http://<laptop-ip>:8000/`. It is the same backend and the same truth.
3. If recording continued while every browser was closed (which is normal — the backend keeps
   recording), reopening shows the Record view with the correct elapsed time and label.
4. Then, if something still looks wrong:
   - `GET http://<laptop>:8000/health` — session state, phones online, build id.
   - **Sessions** page — every session on disk, newest first. Open the row: same Done view.
   - Rebuild a missing ZIP without a browser:
     `curl -X POST http://<laptop>:8000/api/sessions/<sid>/package`
   - `python tools/analyze_session.py <session folder>` — rows, sequence gaps, true rate.
5. Only if a video is unplayable/inscrubbable (e.g. the backend was killed):
   `./tools/fix_webm.sh <file>.webm` remuxes it losslessly (`ffmpeg -c copy`) and rebuilds
   Duration + Cues. The backend does this itself on restart; the script is the manual fallback.

Do **not** start a new session before the old one's data has been looked at — not because
START reclaims anything any more (it does not), but because the operator's attention is the
scarce resource at that moment.