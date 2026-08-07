# Field checklist — before, during and after a session

Written after the 2026-08-07 incident, in which a 26-minute 3-device session ended with
"Application Error", no ZIP, and (apparently) no data. The IMU data turned out to be safe on
the backend all along; the video survived only because nobody pressed START again. Two of the
three phones had also been silently recording at half rate for the entire session.

Every item below exists because something actually went wrong.

## Before START

1. **Check each phone's build on the dashboard.** Each device card shows `v<version>` under
   its packet counters. All handsets must be on the same, current build. Three phones ran a
   stale pre-v2 build for days, which left `acc_ts_ms`, `gyro_ts_ms` and `sample_kind` empty
   on every row — so nothing could tell a held sample from a real one.

2. **Watch the "Sampling rate healthy" preflight check.** It measures *distinct* accelerometer
   readings per second, not packets. A phone can stream a confident 100 packets/sec while the
   OS re-delivers each hardware reading twice, giving 50 Hz of real data. START is blocked
   while any device is below 90 Hz, and the offending devices are named.

   If a device fails, in this order:
   - Turn off Battery Saver and Adaptive Battery for the app.
   - Confirm the foreground-service notification is showing.
   - Keep the screen on, or grant the app unrestricted background power use.
   - Only then consider lowering `kSensorSamplingPeriodMs` (currently 10 ms) in
     `mobile_node/lib/services/internal_sensor_manager.dart` to probe the true ceiling — the
     Android sampling period is a *hint* and the OS clamps it to a supported rate.

3. **Confirm disk space** on `SSD_PATH` before a long run.

4. **Check the unsaved-footage banner is clear.** If the dashboard reports footage from an
   earlier session, save it first. Starting a new session is the point at which confirmed-saved
   footage is reclaimed.

## During

- The offline banner means a phone is buffering locally and will re-send on reconnect. Do not
  stop the session while it is showing.
- Device cards flag any device whose real rate drops below threshold mid-session.

## After STOP

1. The export modal cannot be dismissed until a save completes. Use **Download all as .zip** —
   it streams directly to disk and never buffers the archive in memory.
2. If the browser has no File System Access API (Firefox/Safari), the modal falls back to an
   in-memory download and says so. That path cannot confirm the write, so the footage stays
   marked unsaved and is never auto-deleted. Prefer Chrome or Edge.
3. Remux the video before analysis:
   ```
   ./tools/fix_webm.sh <session>_cam*_video_sync.webm
   ```
   Raw `MediaRecorder` WebM is a live bytestream with no duration and no cues — it plays but
   cannot be scrubbed. `fix_webm.sh` runs `ffmpeg -c copy`, which rebuilds the container index
   losslessly without re-encoding.
4. Check the integrity report. `PARTIAL`/`FAIL` names the device and reason.

## If the dashboard crashes

**Do not start another session.** That is the one action that reclaims footage.

1. Reopen the dashboard **at the same URL** the session was recorded on. IndexedDB is scoped
   per origin *and* per browser profile — `http://localhost:3000` and `http://<lan-ip>:3000`
   are different stores, and footage recorded on one is invisible from the other.
2. Open **Recover buffered video**, confirm the session is listed, and save every camera.
3. The IMU data is unaffected by any dashboard failure. It is fsynced during recording and
   closed in `stop_recording()` before the export modal ever opens. Pull it with:
   ```
   curl http://<backend>:8000/export/<session_id>/manifest
   curl "http://<backend>:8000/export/<session_id>/file?name=<file>" -o <file>
   ```
4. Each phone also keeps its own complete copy at
   `/storage/emulated/0/Android/data/com.example.sensors_app/files/imu_sessions/`, pullable
   over adb. Note these carry **no labels** — labels are applied server-side in
   `io_manager.label_at()`, so the backend CSV is the only labelled copy.
