# Phone node — native Android rebuild

Part of the rebuild spec ([SPEC.md](SPEC.md) §8). Implemented in lane L3, which only touches
`mobile_node/`, `ops/install_phones.sh` and the new `tools/gen_proto_vectors.py`.

## Why
`mobile_node/` is a Flutter app strapped to research subjects. It streams IMU data to a laptop
backend and keeps a local rescue copy. It is unstable because:
- **Two Flutter engines in one process.** The recording pipeline runs in a second engine via
  the `flutter_foreground_task` plugin, and the UI mirrors its state over isolate messages. That
  causes stale UI, connect races and an 8 s hang on relaunch. Platform calls such as haptics
  fail inside the background engine.
- **Notification storm.** `updateService()` is called on every sensor packet (100 Hz) while
  offline. Each call is a full `startForegroundService()` plus SharedPreferences writes on the
  main thread.
- **Unordered control handling.** Control messages run concurrently, so a heartbeat followed by
  START resets the sequence counter mid-stream and the backend drops real samples as duplicates.
- **Role-collision loop.** A role-collision rejection reconnects every 3 s forever.
- **Timer sampling.** Sampling runs on a Dart `Timer.periodic(10ms)`.

Rebuild it as a **fully native Kotlin app**: one foreground service that owns all acquisition,
and a Jetpack Compose UI in the same process that observes the service's state directly. No
Flutter, no platform channels. Read every file in
`mobile_node/lib/` first: the old code documents many hard-won edge cases (comments mention
"plan D3", "T12" and so on). Keep every behaviour listed under "Must preserve" below, and
fix everything under "Must fix".

## Hard compatibility contract (do not deviate)
Research data from 17 subjects already exists. Everything the backend receives and every file
the phone writes must keep its exact shape.

### Wire protocol (backend at `ws://<ip>:8000`, HTTP at `http://<ip>:8000`)
Protobuf (proto3) messages from `shared_contracts/*.proto`. Hand-write a tiny encoder and
decoder in Kotlin rather than adding protoc. Omit default values as proto3 does, and decoders
must skip unknown fields.
- `SensorPacket`:
  - fields 1–6 are `float`, i.e. fixed32 wire type 5: acc_x, acc_y, acc_z in g and gyro_x,
    gyro_y, gyro_z in deg/s;
  - 7 `timestamp_ms` int64 varint, corrected epoch ms;
  - 8 `sequence_number` uint64;
  - 9 `device_id` string;
  - 10 `schema_version` uint32, always 2;
  - 11 `raw_timestamp_ms` int64, uncorrected epoch ms;
  - 12 `acc_ts_ms` and 13 `gyro_ts_ms` int64, hardware event time in corrected epoch ms,
    0 = unknown;
  - 14 `sample_kind` uint32: 0 = fresh, 1 = held.
- `DeviceRegister`: 1 device_id, 2 device_role, 3 device_model (use `Build.MODEL`),
  4 android_version (`Build.VERSION.RELEASE`), 5 app_version (`"3.0.0"`), 6 schema_version
  (uint32 1). It must be the FIRST binary message on `/ws/control`.
- `Command`: 1 type (enum varint: PING=0, PONG=1, START_SESSION=2, STOP_SESSION=3,
  SET_LABEL=4, ACK=5, CLOCK_SYNC=6, ERROR_ALERT=7), 2 payload (JSON string), 3 issued_at_ms
  (int64), 4 command_id (string).
- Telemetry: binary SensorPacket frames on `/ws/telemetry`, one packet per frame.
- Payload JSON:
  - PONG: `{state, session_id, late_sid, telemetry_packets, telemetry_age_ms}`. Every field
    is optional, and an empty payload means "no information". States are IDLE, PREFLIGHT,
    READY, RECORDING, FINALIZING, VALIDATING, ERROR. The backend sends one unsolicited PONG
    right after registration, then a heartbeat every 2 s, plus one in reply to each PING.
  - START_SESSION: `{session_id, scheduled_start_ms, subject, session_tag, operator}`.
  - SET_LABEL: `{label_id, label_name}`.
  - CLOCK_SYNC request `{t0_ms}`; response `{t0_ms, t1_ms, t2_ms}`.
  - ERROR_ALERT: `{code, detail}`.
- On a role collision the backend sends ERROR_ALERT with code `ROLE_COLLISION`, then closes
  with WebSocket close code 4002. An invalid register closes with 4001.
- Rescue upload over HTTP:
  - `GET /upload/status?device_id=&session_id=` returns `{received_bytes, complete, sha256_verified}`.
  - `POST /upload/csv?device_id=&session_id=&role=&subject=&session_tag=&operator=` with
    body `application/octet-stream` and headers `X-Offset`, `X-Total`, plus `X-Complete: 1`
    and `X-Sha256` on the last chunk.
  - Responses: 200 with `{received_bytes, complete, sha256_verified}`, or 409 with
    `{expected_offset}`.
  - Copy the exact algorithm from `lib/services/recovery_uploader.dart`: 256 KiB chunks,
    retries, the zero-byte final request when offset == total, the timeouts, and the
    completion markers.

### Files on the phone (byte-identical formats)
- **Rescue CSV**, under `context.getExternalFilesDir(null)/imu_sessions/`:
  - Name is `<who>_rescue.csv`, where `who` is `[sessionId, subject?, tag?, role, deviceId]`.
    Skip subject and tag when empty, sanitise each part with `[^\w.-]+`→`_` then `_+`→`_`,
    and join with `_`.
  - A NEW file starts with this metadata line:
    `# session_id=..,role=..,device_id=..,subject=..,session_tag=..,operator=..,start_epoch_ms=<ms>,source=local_node,schema_version=2`
    (values sanitised the same way), then the header
    `timestamp_ms,acc_x_g,acc_y_g,acc_z_g,gyro_x_degs,gyro_y_degs,gyro_z_degs,label_id,label_name,sequence_number,device_id,acc_ts_ms,gyro_ts_ms,sample_kind`.
  - Each row is
    `ts,ax,ay,az,gx,gy,gz,labelId,labelName,seq,deviceId,accTs,gyroTs,sampleKind`.
    The six floats use `String.format(Locale.ROOT, "%.6f", v)`. **Always use Locale.ROOT**:
    these phones run an Indonesian locale, which would write commas. accTs and gyroTs are an
    empty string when 0. Lines end in `\n`.
  - Reopening an existing file (same session after a process restart) appends after trimming
    any partial last line. It does NOT write a second header, and the row count is recounted
    from the file.
  - Sidecars:
    - `<csv>.events.jsonl`: one JSON object per line. Event types used today are
      service_start, service_restart, sampling, connection_lost, connection_restored,
      telemetry_stale, buffer_drop and fallback_buffer_parse_warning. Keep their keys.
    - `<csv>.integrity.json`: `{session_id, rows, bytes, sha256, verified_at_ms, close_error?}`,
      written on close.
  - Prune rule: keep 20 files, never delete one without a matching upload marker.
- **Offline buffer:** length-delimited SensorPacket bytes, each record a 4-byte big-endian
  length then the proto bytes. It must stay decodable by `tools/decode_fallback_buffer.py`.
  Unflushable buffers are renamed `orphan_<sid>_<stamp>_*.bin`, never deleted (keep the
  10 newest).
- **Device identity.** On first run of the new app, migrate `device_id`, `device_role` and
  `last_server_ip` from the old Flutter store at `<filesDir parent>/app_flutter/device_config.json`
  (a JSON object). If it is missing, fall back to the legacy SharedPreferences file
  `FlutterSharedPreferences`, keys `flutter.device_id` and `flutter.last_server_ip`. Generate a
  UUIDv4 only if neither has a device_id. A phone's device_id must survive this upgrade.
  `applicationId` stays `com.example.sensors_app` so the APK installs over the old one and
  keeps its data.

## Project layout and build
Replace the Flutter project in `mobile_node/` with a plain Android Gradle project:
- Remove `lib/`, `pubspec.*`, `analysis_options.yaml`, `.metadata`, `.flutter-plugins*`, the
  generated `GeneratedPluginRegistrant.java`, the `ios/ linux/ macos/ windows/ web/` folders
  and the Flutter Gradle plugin wiring.
- Keep `android/app/src/main/assets/sounds/{alert,ok}.wav`, moved from `assets/sounds/`.
- New root: `mobile_node/` containing `settings.gradle.kts`, `build.gradle.kts`,
  `gradle/libs.versions.toml`, the Gradle wrapper and `app/`.
- Tooling: AGP 8.6.x, Kotlin 2.0.x, Gradle 8.9 (the versions already used), compileSdk 34,
  targetSdk 34, minSdk 26, Compose BOM 2024.09.x, OkHttp 4.12.x, kotlinx-coroutines,
  kotlinx-serialization-json.
- `applicationId "com.example.sensors_app"`, `versionCode 5`, `versionName "3.0.0"`.
- Release signed with the debug keystore, as today, so it installs over existing phones.
- APK output: `mobile_node/app/build/outputs/apk/release/app-release.apk`. Update the default
  path and the build hint in `ops/install_phones.sh`.

## Architecture
```
app/src/main/kotlin/com/example/sensors_app/
  MainActivity.kt        ComponentActivity hosting Compose; requests POST_NOTIFICATIONS;
                         battery-optimisation intent; back = moveTaskToBack(true) on Status
  ui/                    SetupScreen, StatusScreen, FilesSheet, theme; observe Runtime.state
  ImuService.kt          foreground service (type health), START_STICKY, partial WakeLock +
                         high-perf WifiLock while running; notification at most 1 update/s with
                         a SILENCE action while alarming
  core/Pipeline.kt       THE state machine. Owns a single HandlerThread "pipeline"; every
                         input (control frame, socket open/close, timer, sensor tick, UI
                         command) is posted to it, so nothing ever runs concurrently
  core/Proto.kt          encode/decode, pure Kotlin, JVM-testable
  core/Sampler.kt        SensorManager accel + gyro on their own HandlerThread
  core/Link.kt           OkHttp WebSockets for control + telemetry, reporting into Pipeline
  core/ClockSync.kt      NTP-lite; median of 5 samples
  core/RescueRecorder.kt rescue CSV + sidecars (buffered writer, flush every 1 s, fsync on close)
  core/FallbackBuffer.kt
  core/RecoveryUploader.kt
  core/Identity.kt       device id / role / last ip / desired endpoint + migration
  core/Checkpoint.kt     atomic (tmp + rename) {session_id, last_sequence, server_ip, ...}
  core/Alarm.kt          MediaPlayer (USAGE_ALARM) looping assets/sounds/alert.wav,
                         ok.wav on restore, Vibrator pattern; silence until next drop
  core/StateSnapshot.kt  immutable snapshot published to a process-wide StateFlow
  core/Discovery.kt      NsdManager discovery of `_imu-telemetry._tcp`
  Runtime.kt             process-wide object: `state: StateFlow<StateSnapshot>` and
                         `commands`: connect(host, role), disconnect(), silenceAlarm(),
                         resetDevice(), listLocalFiles(). Commands start the service if needed
                         and post into Pipeline
```
The UI never touches sockets, files or sensors. It renders `Runtime.state` (collected with
`collectAsStateWithLifecycle`) and calls `Runtime` commands. The snapshot is published on
change, conflated, at most 10 per second.

- Manifest:
  - Remove `RECEIVE_BOOT_COMPLETED` and the plugin's service and receiver entries.
  - Declare `ImuService` with `foregroundServiceType="health"` and `stopWithTask=false`.
  - Keep `HIGH_SAMPLING_RATE_SENSORS`, `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_HEALTH`,
    `WAKE_LOCK`, `INTERNET`, `ACCESS_NETWORK_STATE` and
    `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`.
  - Add `POST_NOTIFICATIONS`, `VIBRATE` and `CHANGE_WIFI_MULTICAST_STATE` (needed for mDNS).
  - Drop `ACCESS_FINE_LOCATION` and `ACCESS_COARSE_LOCATION`; nothing uses them.

### Discovery (mDNS)
- The backend advertises `_imu-telemetry._tcp.local.` (service name "IMU Backend", port 8000,
  TXT `version`, and from v3 also `api=3`).
- The Setup screen starts `NsdManager.discoverServices` while it is visible, holding a
  `WifiManager.MulticastLock` for the duration. It resolves each service found and lists them:
  "IMU Backend — 192.168.1.23", with a tap to select.
- If exactly one is found and the last-used IP matches it, or there is no last IP, it is
  preselected.
- Manual IP entry is always available as a fallback.
- Discovery stops when leaving Setup.
- `connect` stores the resolved IP literal, not the service name, so reconnects never depend on
  mDNS.

## Behaviour
### Connection
- `connect(ip, role)`:
  - Validate the IPv4 or hostname. Persist ip, role and the desired endpoint. Start the
    service if needed.
  - If already connected with the SAME ip and role, it is a no-op. A different role or ip
    means a clean disconnect then reconnect, so the new role is actually registered.
- Open control, send DeviceRegister, then open telemetry. Each handshake times out after 6 s.
  The link is "connected" only when both sockets are open.
- PING every 1 s. Go offline when no PONG has arrived for more than 8 s, or when either socket
  closes or errors.
- Reconnect backoff: 1, 2, 3, then 3 s forever while a desired endpoint exists.
- On close code 4001 or 4002, or on ERROR_ALERT ROLE_COLLISION:
  - stop reconnecting;
  - set `error = {code, detail}` in the snapshot so the UI can show "Role X already in use
    by another phone — pick another role";
  - require an explicit `connect` to try again.
- Telemetry watchdog: while a session is active and we have sent session packets, reconnect
  if the backend's `telemetry_age_ms` is at least 12000, or if `telemetry_packets` has not
  advanced for 12 s. Log a `telemetry_stale` event.
- Clock sync: a burst of 5 requests at 200 ms spacing on every connect, repeated every 5 min.
  The offset is the median of round-trip-valid samples, `rtt = (t3-t0)-(t2-t1)` (discard
  rtt < 0), `offset = ((t1-t0)+(t2-t3))/2`. Corrected now = `System.currentTimeMillis() + offset`.
- `disconnect`:
  - Refused while a session is active; the snapshot error says only the dashboard can stop
    a session.
  - Otherwise close the sockets, CLEAR the desired endpoint (so nothing auto-reconnects
    later) and stop the service.

### Sampling
- Register the accelerometer and gyroscope at a 5000 µs sampling period (200 Hz request, the
  same HAL-tuning reason as `kSensorSamplingPeriodMs` in the old code) on a dedicated
  HandlerThread. Cache the latest values and event timestamps.
- Convert `event.timestamp` (elapsedRealtimeNanos) to epoch ms with
  `currentTimeMillis - (elapsedRealtimeNanos() - event.timestamp)/1e6`, plus the clock offset.
  Report 0 if the result is more than 1 h from corrected now.
- Emit one packet every 10 ms with a `ScheduledExecutorService.scheduleAtFixedRate`, posting
  into Pipeline. Units: acc = m/s² ÷ 9.80665 and gyro = rad/s × 180/π, computed in double.
- Held detection is unchanged: `held = sameTimestamps || sameValues`, compared with the
  previous EMITTED packet. Read `_emitPacket` in internal_sensor_manager.dart.
- Count events per sensor and publish the observed acc/gyro Hz, recomputed each 1 s window,
  in the snapshot. Write the same `sampling` event to events.jsonl every 5 s while recording
  (keys `type, time_ms, expected_hz, observed_acc_hz, observed_gyro_hz`).
- Sensors run whenever the service is running (connected or reconnecting), not only during a
  session.

### Session and sequence (the race fix)
- Session state changes ONLY on the pipeline thread, in arrival order.
- START_SESSION(sid):
  - If sid differs from the active session: set sequence = 0, store subject/tag/operator,
    open the rescue recorder, checkpoint, and log `service_start`.
  - If sid equals the active session (already adopted from a PONG): DO NOT reset the
    sequence. Just record the metadata.
- PONG resync:
  - Server says RECORDING with sid ≠ active: adopt it (sequence = 0, recorder opened with
    whatever metadata is known).
  - Server is not RECORDING while we are active: we missed STOP, so stop exactly like
    STOP_SESSION.
- STOP_SESSION:
  - Close the recorder (integrity.json), clear the checkpoint and stop the alarm.
  - Then trigger a rescue upload for that session, off the pipeline thread on a single
    worker executor.
- SET_LABEL updates the label used in rescue CSV rows.
- **Idle packets.** Packets sent while no session is active use
  `sequence = IDLE_SEQ_BASE + idleCounter`, with `IDLE_SEQ_BASE = 1L shl 40`. They go only
  to the live telemetry socket: never to the rescue CSV, never to the offline buffer. The
  backend will drop sequence numbers ≥ 2^40 from session and late files, which is done in a
  separate backend lane. Session packets use 0,1,2,…
- Session packets:
  - Always written to the rescue CSV, whether or not the network is up.
  - Sent on telemetry when connected, otherwise appended to the offline buffer.
  - Checkpoint `last_sequence` every 250 packets.
- After a process restart or START_STICKY restart, with a checkpoint saying RECORDING:
  - resume the same session with `sequence = last + 1`;
  - reopen the rescue CSV in append mode;
  - log `service_restart`;
  - reconnect to the saved endpoint.
- Reconnect flush, after the first PONG of a new connection:
  - If the buffer's session == the active RECORDING sid, or == `late_sid`, replay it on
    telemetry and delete it only after a clean parse.
  - Otherwise quarantine it to orphan files, log `buffer_drop`, and tell the UI.
  - Then upload any finished, unmarked rescue CSVs.

### Alarm and notification
- Connection lost during a session: log `connection_lost`, start the looping alarm and
  vibration, and show a high-priority notification with a SILENCE action.
- Restored: log `connection_restored` with `duration_ms`, stop the alarm and play ok.wav
  unless the alarm was silenced.
- Notification text, updated at most 1/s:
  - `Idle — connected to <ip>`
  - `● REC <role> · <n> sent`
  - `⚠ OFFLINE <mm:ss> · <n> buffered locally`
  - `Reconnecting…`

## UI (Jetpack Compose, Material 3, dark, large touch targets, English only)
The phone is usually strapped to a person and glanced at from a metre away. Prioritise a huge,
unambiguous status over detail.

- **Setup screen**, shown when there is no desired endpoint:
  - **Role picker**: a 2×4 grid of big buttons with human labels: Chest, Waist, Left thigh,
    Right thigh, Left ankle, Right ankle, Left wrist, Right wrist. They map to chest, waist,
    thigh_left, thigh_right, ankle_left, ankle_right, wrist_left and wrist_right.
  - **Backend**: the mDNS results list (see Discovery) with the found laptop preselected.
    Underneath it, "Enter IP manually", a text field using
    `KeyboardOptions(keyboardType = KeyboardType.Decimal)` so "." exists on Samsung keyboards.
    It is validated and prefilled with the last IP.
  - **CONNECT**: one big button with an inline spinner. Errors are shown inline in plain
    language, such as "Laptop not reachable — same Wi-Fi? Backend running?" or "Role Chest is
    already used by another phone".
- **Status screen**, shown whenever a desired endpoint exists, including on every app launch.
  The choice is read synchronously from `Runtime.state` before the first frame, so there is no
  waiting.
  - **State block**: full-width and colour-coded:
    - grey READY (connected, idle);
    - red RECORDING, with elapsed time;
    - amber OFFLINE / RECONNECTING, with mm:ss;
    - purple ERROR (role collision and similar), with a "Change role" button that returns to
      Setup.
  - **Details**: the role name large, then current label, short session id, sent/buffered
    counters, local backup rows with OK/FAILED, observed sampling Hz and clock offset.
  - **SILENCE**: a giant button while the alarm is sounding.
  - **Readiness checklist**: shown as WARNINGS, never blocking. It covers battery % (from
    `BatteryManager`), battery-optimisation exemption, notifications allowed, sampling ≥ 90 Hz
    and clock synced. Each item has a "Fix" button that opens the relevant system screen where
    one exists.
  - **No live charts**: at most one small acc-magnitude bar to prove the sensor is alive.
  - **Disconnect**: a text button at the bottom behind a confirm dialog. It is disabled during
    RECORDING, with the reason shown.
  - **System back** moves the task to the background; it does not return to Setup.
- **Files sheet**: lists local rescue CSVs with size and uploaded/not-uploaded status.
- The version shown comes from `BuildConfig.VERSION_NAME`; it is never hard-coded.

## Must preserve (from the old code)
- Every bullet under "Wire protocol" and "Files on the phone".
- Dedup-safe resume after a restart.
- Never deleting un-uploaded data; quarantining instead of deleting.
- The upload timeouts.
- The role-collision message.
- The backend-authoritative state model: PONG is the source of truth.

## Must fix
- No notification or service update faster than 1/s.
- A single runtime with no plugin engines.
- Strictly ordered control handling.
- No reconnect loop on role collision.
- No sequence reset for an already-adopted session.
- Disconnect clears the desired endpoint.
- App relaunch lands on the right screen immediately.
- Alarm audio and vibration are native and work with the screen off.

## Tests (must exist and pass)
1. JVM unit tests (`mobile_node/app/src/test/kotlin/...`), run with
   `cd mobile_node && ./gradlew :app:testDebugUnitTest`:
   - **Proto**: encode/decode round-trip for every message, plus these fixed byte vectors:
     - `Command(type=PING, issued_at_ms=1)` encodes to hex `1801`;
     - `Command(type=PONG, payload="{}")` encodes to hex `080112027b7d`;
     - a SensorPacket with acc_x=1.0f and every other field default encodes to `0d0000803f`;
     - unknown fields are skipped when decoding.
   - **Cross-check against Python**: add a test resource `proto_vectors.json` produced by
     `tools/gen_proto_vectors.py`. The script uses `master_backend/proto` to encode about 10
     representative messages: negative int64, a large uint64 at or above `IDLE_SEQ_BASE`,
     unicode strings, and all 14 SensorPacket fields set. Kotlin must encode to identical
     bytes and decode them back.
   - **RescueRecorder**:
     - the file name and metadata line for a given input;
     - row formatting, including negative values, -0.0 and empty hardware timestamps;
     - the ROOT locale even when `Locale.setDefault(Locale("in","ID"))`;
     - append after a partial last line;
     - the row recount.
   - **FallbackBuffer**: framing round-trip; a truncated tail is reported, not thrown; quarantine
     renames the files.
   - **SessionLogic**: extract the decision logic into a pure class
     `SessionLogic.handle(event) -> List<Action>` and test:
     - START then START with the same sid does not reset the sequence;
     - PONG adoption then START with the same sid does not reset the sequence;
     - a missed STOP detected via PONG;
     - flush vs quarantine decisions;
     - idle sequence numbering;
     - a role-collision close stops reconnecting;
     - disconnect is refused during a session;
     - the resume-from-checkpoint sequence.
   - **ClockSync**: the median, and rejection of samples with rtt < 0.
   - **Identity migration**: reads the old Flutter `device_config.json` and the legacy
     SharedPreferences values.
2. Compose UI tests (Robolectric, JVM) for Setup and Status driven by a fake `Runtime` state.
   The Status test covers every state-block colour and the SILENCE button.
3. `cd mobile_node && ./gradlew lint assembleRelease` succeeds with no lint errors. Warnings
   are reported in the final summary.

## Deliverable
- Commit in logical steps: core+tests, service, UI, then cleanup and old-code removal.
  - Use `git commit --no-gpg-sign -m "..."`. Messages say what changed and why, with no
    Co-Authored-By or generated-by trailers.
  - If git metadata is read-only, leave the change uncommitted and say so.
  - Never run `git config`, never push.
- Write `mobile_node/ARCHITECTURE.md`, one page: components, threads, state machine, file
  formats, and how to test.
- Final report:
  - the test output;
  - the APK path and size;
  - anything from the old code you deliberately did not carry over, and why;
  - anything you were unsure about.
