# IMU phone node — architecture

Native Kotlin/Compose Android app (no Flutter, no plugins). One page: components, threads,
state machine, file formats, how to test. The hard wire/file compatibility contract lives in
`docs/rebuild/PHONE.md`; this file describes how the code meets it.

## Components

```
app/src/main/kotlin/id/ac/ui/cs/csl/fams/sapper/
  MainActivity.kt          Compose host; Runtime.preload() BEFORE setContent (relaunch lands
                           on the right screen immediately); POST_NOTIFICATIONS +
                           ACTIVITY_RECOGNITION requests; back = moveTaskToBack on Status
  Runtime.kt               process-wide object: state: StateFlow<StateSnapshot> + commands
                           (connect/disconnect/silenceAlarm/resetDevice/listLocalFiles).
                           Commands queue in arrival order until the service attaches its
                           Pipeline (service start is asynchronous)
  ImuService.kt            foreground service (type health, START_STICKY), partial WakeLock +
                           high-perf WifiLock, ongoing notification re-rendered at most 1/s
                           (1 Hz ticker with change detection); notificationText() is pure
  AlarmActionReceiver.kt   notification SILENCE action → Runtime.silenceAlarm()
  ui/                      Theme (dark M3, block colours, StatusBlockColor semantics key),
                           AppRoot (Setup-vs-Status switch, owns Discovery lifetime),
                           SetupScreen (2×4 role grid, mDNS list, decimal-keyboard IP field),
                           StatusScreen (colour block, giant SILENCE, details, readiness
                           warnings, accel bar, guarded Disconnect), FilesSheet
  core/Pipeline.kt         THE state machine (see below)
  core/SessionLogic.kt     pure decision core: handle(Ev) -> List<Act>, unit-tested
  core/Link.kt             OkHttp WebSockets: ws://<ip>:8000/ws/control (DeviceRegister as
                           first binary frame) then ws://<ip>:8000/ws/telemetry; callbacks
                           are posted onto the pipeline thread
  core/Sampler.kt          accel+gyro at 5000 µs on their own HandlerThread; emits one
                           Reading every 10 ms via scheduleAtFixedRate; held detection
  core/Proto.kt            hand-written proto3 codec (defaults omitted, negative int64 =
                           canonical 10-byte two's complement, unknown fields skipped)
  core/ClockSync.kt        NTP-lite: burst 5×200 ms every connect + every 5 min, median
  core/RescueRecorder.kt   rescue CSV + events.jsonl sidecar + integrity.json
  core/FallbackBuffer.kt   offline packet buffer, 4-byte BE length framing, rotation,
                           quarantine-to-orphan (never deletes unflushed data)
  core/RecoveryUploader.kt HTTP upload/csv + upload/status, resume on 409, completion
                           markers; reads markers from both legacy dirs
  core/Identity.kt         device_identity.json + migration from Flutter device_config.json
                           and FlutterSharedPreferences.xml
  core/Checkpoint.kt       session_state.json + desired_state.json (atomic tmp+rename)
  core/Discovery.kt        NsdManager `_imu-telemetry._tcp` + MulticastLock, serialized
                           resolve chain, main-thread callbacks
  core/Alarm.kt            MediaPlayer USAGE_ALARM looping alert.wav, ok.wav chime on
                           restore (unless silenced), 80 ms vibe every 2 s
  core/StateSnapshot.kt    the immutable UI snapshot + StatusBlock derivation
  core/Sample.kt           one sample: CSV row (BigDecimal %.6f, Dart-exact rounding) and
                           SensorPacket conversion (acc ÷9.80665, gyro ×180/π in double)
```

## Threads

| Thread | Runs |
| --- | --- |
| main | UI, notification render, service lifecycle, Runtime command entry |
| `pipeline` (HandlerThread) | **all session state**: every control frame, socket event, timer, sensor reading and UI command is posted here, so events are handled strictly in arrival order and never concurrently |
| `sampler` (HandlerThread) | sensor event delivery; a ScheduledExecutorService emits 10 ms packets into the pipeline |
| OkHttp WS readers | socket I/O only; every callback hops to the pipeline handler |
| `upload` (single-thread executor) | recovery CSV uploads |
| `buffer-flush` (single-thread executor) | offline-buffer replay into the telemetry socket |

Timers (all on the pipeline handler): 1 s tick (PING + watchdogs), 1 s CSV flush, 5 s
`sampling` sidecar event, clock resync every 5 min, 6 s handshake deadline, reconnect backoff
1/2/3/3 s, snapshot publish conflated to ≤ 10/s.

## State machine

`SessionLogic` decides, `Pipeline` executes. Events (`Ev`): Connect, LinkEstablished,
LinkLost, Pong, Start, Stop, Label, ErrorAlert, Tick, BufferProbe, ReconnectTimer,
RestoreDesired, ResumeCheckpoint, DisconnectRequest, ResetDevice. Each `handle(ev)` returns
`Act`s (OpenSockets, OpenRecorder, SaveCheckpoint, FlushBuffer, QuarantineBuffer, LogEvent,
StartAlarm, …) that Pipeline applies to sockets, files, sensors and the alarm, then publishes
a `StateSnapshot` to `Runtime.state`.

Key invariants (all unit-tested in SessionLogicTest):
- Backend is authoritative: the first PONG after connect adopts/resyncs the session state.
- START with the active sid never resets the sequence; resume continues at `last + 1`.
- Session packets: always to the rescue CSV; to telemetry when up, else to the buffer;
  checkpoint every 250 packets on both paths.
- Idle packets use `sequence = 2^40 + n` and go to the live socket only.
- Flush after reconnect only when the buffer's session matches; otherwise quarantine +
  `buffer_drop` + UI notice. Buffer files are deleted only after a clean parse.
- Close 4001/4002 or ERROR_ALERT ROLE_COLLISION stops reconnecting and sets the sticky
  purple error; only an explicit connect clears it.
- Disconnect is refused during a session; otherwise it clears `desired_state.json` so
  nothing auto-reconnects, and stops the service.

## Files on the phone (byte-identical to the old app)

| Path | Content |
| --- | --- |
| `getExternalFilesDir(null)/imu_sessions/<sid>_<tag>_<subject>_<role>_<devid>_rescue.csv` | `#`-metadata line, CSV header, rows with `%.6f` cells (BigDecimal HALF_UP on the exact binary value, `-0.000000` keeps its sign), appended + row-recounted on resume |
| `<csv>.events.jsonl` | sidecar events: service_start/restart, connection_lost/restored, telemetry_stale, buffer_drop, sampling, fallback_buffer_parse_warning |
| `<csv>.integrity.json` | written on session close |
| `filesDir/fallback_buffer{.N}.bin` + `fallback_buffer.meta.json` | offline buffer (`meta`: session_id, current_file_index, started_at_ms); `.bin.flush.<ts>` snapshots during replay; `orphan_<sid>_<stamp>_*` quarantine (10 newest kept) |
| `filesDir/session_state.json` | RECORDING checkpoint {session_id, device_id, server_ip, clock_offset_ms, last_sequence_number, device_role, state, saved_at_ms} |
| `filesDir/desired_state.json` | {server_ip, device_role} — presence = Status screen + auto-reconnect |
| `filesDir/device_identity.json` | device UUID (migrated from Flutter `device_config.json` / shared prefs) |
| `filesDir/recovery_uploaded_<sid>_<dev>.json` | upload completion markers (legacy dirs are also read) |

Upgrade path: the app id is `id.ac.ui.cs.csl.fams.sapper`, so it installs alongside the old
Flutter build (`com.example.sensors_app`) rather than over it, and cannot read that app's
files. The Flutter-migration code in `Identity.kt` (moving `filesDir/app_flutter` identity,
markers, checkpoints and `fallback_buffer*`) is therefore inert on field phones: each phone
starts with a fresh identity. Later `sapper` builds signed with the same key upgrade in place.

## How to test

```sh
cd mobile_node
./gradlew :app:testDebugUnitTest   # JVM: proto vectors vs Python, recorder, buffer, logic,
                                   # clock, identity, float6 table; Robolectric: Setup/Status
./gradlew lint assembleRelease     # APK at app/build/outputs/apk/release/app-release.apk
```

Release signing: create `mobile_node/keystore.properties` from `keystore.properties.example`
(key once: `keytool -genkeypair -v -keystore imu-release.jks -alias imu -keyalg RSA -keysize 4096 -validity 36500`)
or set `IMU_KEYSTORE_FILE`/`IMU_KEYSTORE_PASSWORD`/`IMU_KEY_ALIAS`/`IMU_KEY_PASSWORD`.
With neither, Gradle signs with the debug key, warns loudly and the version becomes
`…-debugsigned`; never deploy that build. Back the `.jks` up — field phones accept updates
only from the same key, and uninstalling deletes their rescue CSVs.

Proto vectors: `python3 tools/gen_proto_vectors.py` (repo root) regenerates
`app/src/test/resources/proto_vectors.json` using `master_backend/proto` — Kotlin must encode
to identical bytes and decode them back.
