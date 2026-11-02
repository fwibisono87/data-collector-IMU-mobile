package com.example.sensors_app.core

import android.content.Context
import android.os.BatteryManager
import android.os.Handler
import android.os.HandlerThread
import com.example.sensors_app.BuildConfig
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.OkHttpClient
import java.io.File
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * THE state machine. Owns a single [HandlerThread] named "pipeline"; every input — control
 * frame, socket open/close, timer, sensor reading, UI command — is posted onto it, so nothing
 * ever runs concurrently and session state changes only in arrival order (the race fix).
 *
 * Division of labour: [SessionLogic] DECIDES (pure, unit-tested); this class EXECUTES the
 * returned [Act]s against the Android world (sockets, files, sensors, alarm) and publishes
 * [StateSnapshot]s to [stateFlow]. Blocking work (buffer replay, uploads) runs on two
 * single-thread executors and reports back via the handler.
 *
 * Timers, all on the pipeline handler:
 *  - 1 s tick: PING while connected + [Ev.Tick] (PONG timeout, telemetry watchdog, flush probe);
 *  - 1 s rescue-CSV flush;
 *  - 5 s `sampling` sidecar event while recording;
 *  - clock-sync burst 5×200 ms on every connect, repeated every 5 min;
 *  - 6 s handshake deadline per connect attempt;
 *  - snapshot publishing conflated to at most 10/s.
 */
class Pipeline(
    private val app: Context,
    private val stateFlow: MutableStateFlow<StateSnapshot>,
    /** Called on the pipeline thread when the logic asks for the service to stop. */
    private val onStopService: () -> Unit,
) {
    companion object {
        const val TICK_MS = 1000L
        const val FLUSH_MS = 1000L
        const val SAMPLING_EVENT_MS = 5000L
        const val HANDSHAKE_TIMEOUT_MS = 6000L
        const val NOTICE_MS = 5000L
        const val PUBLISH_MIN_INTERVAL_MS = 100L // ≤ 10 snapshot publishes per second
        const val BATTERY_CACHE_MS = 10_000L
        private val IPV4 =
            Regex("^(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)(\\.(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)){3}$")
        private val HOSTNAME = Regex("^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
    }

    private val thread = HandlerThread("pipeline").also { it.start() }
    private val handler = Handler(thread.looper)

    private val filesDir: File = app.filesDir
    /** Legacy Flutter documents-dir candidates: `<filesDir>` and `<filesDir>/app_flutter`
     *  (which one it was depends on the path_provider version the old build used). */
    private val legacyDirs: List<File> = listOf(filesDir, File(filesDir, "app_flutter"))
    private val sessionsDir: File = File(app.getExternalFilesDir(null) ?: filesDir, "imu_sessions")
    private val prefsXml: File
        get() = File(filesDir.parent ?: filesDir.path, "shared_prefs/FlutterSharedPreferences.xml")

    private val logic = SessionLogic()
    private val clockSync = ClockSync()
    private val identityStore = IdentityStore(filesDir)
    private val checkpoint = CheckpointStore(legacyDirs)
    private val recorder = RescueRecorder(sessionsDir, legacyDirs)
    private val buffer = FallbackBuffer(filesDir)
    private val alarm = Alarm(app)
    private val engineId = newEngineId()

    private val linkClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        // No read timeout: liveness is the app-level PING/PONG watchdog's job (8 s), exactly
        // like the Dart client, whose socket also never timed out on idle.
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false)
        .build()

    // ── pipeline-thread state ───────────────────────────────────────────────
    private lateinit var identity: Identity
    private var controlOpen = false
    private var telemetryOpen = false
    private var everUpThisAttempt = false // LinkEstablished fired for the current connect
    private var flushing = false
    private var lastQuarantineCount = 0
    private var lastQuarantineSid: String? = null
    private var lastMagnitude = 0.0
    private var sessionStartedAtMs: Long? = null

    private var packetsSent = 0L
    private var sessionPacketsSent = 0L
    private var uploadsFinished = 0

    // Copies of the backend telemetry counters from the last PONG, for the `telemetry_stale`
    // sidecar event's detail keys (SessionLogic keeps its own private copies for decisions).
    private var backendTelemetryPackets: Long? = null
    private var backendTelemetryAgeMs: Long? = null

    private var connectError: String? = null
    private var notice: String? = null
    private var batteryPercent = -1
    private var batteryReadAtMs = 0L
    private var lastPublishMs = 0L
    private var publishScheduled = false
    private val syncOffsets = mutableListOf<Long>()
    private val pendingSyncs = mutableMapOf<String, Long>() // commandId → t0Ms

    // ── link listener (declared before `link`: property initialisers run in order and the
    //    Link constructor captures the reference) ────────────────────────────
    private val linkListener = object : Link.Listener {
        override fun onControlOpen() {
            controlOpen = true
            checkBothOpen()
        }

        override fun onTelemetryOpen() {
            telemetryOpen = true
            checkBothOpen()
        }

        override fun onControlFrame(bytes: ByteArray) {
            val cmd = try {
                Command.decode(bytes)
            } catch (_: ProtoDecodeException) {
                return
            }
            handleCommand(cmd)
        }

        override fun onClosed(code: Int?, failure: String?) {
            controlOpen = false
            telemetryOpen = false
            if (!everUpThisAttempt && code == null) {
                connectError = "Laptop not reachable — same Wi-Fi? Backend running?"
            }
            everUpThisAttempt = false
            onEvent(Ev.LinkLost(code, System.currentTimeMillis()))
        }

        private fun checkBothOpen() {
            if (controlOpen && telemetryOpen && !everUpThisAttempt) {
                everUpThisAttempt = true
                handler.removeCallbacks(handshakeTimeout)
                connectError = null
                onEvent(Ev.LinkEstablished(System.currentTimeMillis()))
            }
        }
    }

    private val link = Link(linkClient, { handler.post(it) }, linkListener)
    private val sampler = Sampler(app) { reading -> handler.post { onReading(reading) } }
    private val uploader = RecoveryUploader(
        RecoveryUploader.buildClient(),
        legacyDirs,
        { recorder.listSessions() },
        { recorder.isOpen && it.path == recorder.path },
    )
    private val uploadExecutor = Executors.newSingleThreadExecutor { r -> Thread(r, "upload") }
    private val flushExecutor = Executors.newSingleThreadExecutor { r -> Thread(r, "buffer-flush") }

    // ── timers (re-arm FIRST, so stopTimers() during an event kills the next tick too) ──
    private val tickRunnable = object : Runnable {
        override fun run() {
            handler.postDelayed(this, TICK_MS)
            if (logic.linkUp) {
                link.sendControl(
                    Command(type = CommandType.PING, issuedAtMs = System.currentTimeMillis())
                        .encode(),
                )
            }
            onEvent(Ev.Tick(System.currentTimeMillis()))
        }
    }
    private val flushRunnable = object : Runnable {
        override fun run() {
            handler.postDelayed(this, FLUSH_MS)
            recorder.flush()
        }
    }
    private val samplingEventRunnable = object : Runnable {
        override fun run() {
            handler.postDelayed(this, SAMPLING_EVENT_MS)
            if (recorder.isOpen) {
                recorder.logEvent(
                    buildJsonObject {
                        put("type", "sampling")
                        put("time_ms", System.currentTimeMillis())
                        put("expected_hz", Sampler.EXPECTED_HZ.toInt())
                        put("observed_acc_hz", round2(sampler.observedAccHz))
                        put("observed_gyro_hz", round2(sampler.observedGyroHz))
                    },
                )
            }
        }
    }
    private val resyncRunnable = object : Runnable {
        override fun run() {
            handler.postDelayed(this, ClockSync.RESYNC_INTERVAL_MS)
            if (logic.linkUp) startBurst()
        }
    }
    private val handshakeTimeout = Runnable {
        if (!everUpThisAttempt && (logic.connecting || logic.linkUp)) {
            onEvent(Ev.LinkLost(null, System.currentTimeMillis()))
        }
    }
    private var reconnectRunnable: Runnable? = null
    private val clearNotice = Runnable {
        notice = null
        publish()
    }

    // ── lifecycle ────────────────────────────────────────────────────────────

    /** Starts acquisition and restores identity, checkpoint and desired endpoint. */
    fun start() {
        handler.post { init() }
    }

    private fun init() {
        identity = identityStore.load(
            legacyDirs.map { File(it, "device_config.json") },
            parseFlutterPrefs(prefsXml),
        )
        migrateLegacyBuffer()
        sampler.start()
        handler.postDelayed(tickRunnable, TICK_MS)
        handler.postDelayed(flushRunnable, FLUSH_MS)
        handler.postDelayed(samplingEventRunnable, SAMPLING_EVENT_MS)

        // Restore: a persisted desired endpoint reconnects without the UI; a RECORDING
        // checkpoint resumes its session. Order matters: RestoreDesired sets logic.role,
        // which the resumed recorder's file NAME needs. (The Dart client restored the clock
        // offset from the checkpoint into nothing — parity: the connect burst resyncs it.)
        val cp = checkpoint.loadInterrupted(identity.deviceId)
        val desired = checkpoint.loadDesired()
        if (desired != null) {
            onEvent(Ev.RestoreDesired(desired.ip, desired.role))
        } else if (cp != null && cp.serverIp.isNotEmpty()) {
            onEvent(Ev.RestoreDesired(cp.serverIp, cp.deviceRole))
        }
        if (cp != null) {
            onEvent(
                Ev.ResumeCheckpoint(
                    cp.sessionId,
                    cp.lastSequenceNumber,
                    cp.subject,
                    cp.sessionTag,
                    cp.operator,
                ),
            )
        }
        publish()
    }

    /**
     * One-time upgrade step: the Flutter app kept `fallback_buffer*.bin` in its documents dir,
     * which depending on path_provider version was `<filesDir>` or `<filesDir>/app_flutter`.
     * A surviving buffer under the legacy dir is MOVED (never deleted) so its bytes can still
     * be flushed or quarantined; only when the new location holds nothing.
     */
    private fun migrateLegacyBuffer() {
        try {
            if (File(filesDir, "fallback_buffer.meta.json").exists()) return
            for (legacy in legacyDirs.drop(1)) {
                if (!File(legacy, "fallback_buffer.meta.json").exists()) continue
                (legacy.listFiles() ?: emptyArray())
                    .filter { it.name.startsWith("fallback_buffer") }
                    .forEach { it.renameTo(File(filesDir, it.name)) }
                return
            }
        } catch (_: Exception) {
            // Worst case a legacy buffer is not replayed live; the rescue CSV still has the rows.
        }
    }

    /** Tears everything down. Idempotent; called from the service's onDestroy. */
    fun shutdown() {
        handler.removeCallbacksAndMessages(null)
        runCatching { sampler.release() }
        runCatching { alarm.release() }
        runCatching { link.close() }
        runCatching { recorder.stop() }
        runCatching { buffer.deactivate() }
        uploadExecutor.shutdownNow()
        flushExecutor.shutdownNow()
        thread.quitSafely()
    }

    // ── commands (callable from any thread; each hops onto the pipeline thread) ──

    fun connect(ip: String, role: String) {
        handler.post {
            val trimmed = ip.trim()
            if (!IPV4.matches(trimmed) && !HOSTNAME.matches(trimmed)) {
                connectError = "That does not look like an IP address."
                publish()
                return@post
            }
            connectError = null
            onEvent(Ev.Connect(trimmed, role))
        }
    }

    fun disconnect() {
        handler.post { onEvent(Ev.DisconnectRequest) }
    }

    fun silenceAlarm() {
        handler.post {
            alarm.silence()
            publish()
        }
    }

    fun resetDevice() {
        handler.post { onEvent(Ev.ResetDevice) }
    }

    /** Directory listing on the pipeline thread; the callback also runs there. */
    fun listLocalFiles(cb: (List<LocalFileInfo>) -> Unit) {
        handler.post {
            val files = recorder.listSessions().map { f ->
                LocalFileInfo(
                    name = f.name,
                    path = f.path,
                    bytes = f.length(),
                    uploaded = recorder.isUploaded(f),
                    sessionId = uploader.parseMeta(f)?.get("session_id"),
                    modifiedMs = f.lastModified(),
                )
            }
            cb(files)
        }
    }

    // ── the event funnel ─────────────────────────────────────────────────────

    private fun onEvent(ev: Ev) {
        for (act in logic.handle(ev)) runAct(act)
        publish()
    }

    private fun runAct(act: Act) {
        when (act) {
            is Act.PersistDesired -> {
                checkpoint.saveDesired(act.ip, act.role)
                identity = identityStore.update { it.copy(role = act.role, lastServerIp = act.ip) }
            }
            is Act.ClearDesired -> checkpoint.clearDesired()
            is Act.OpenSockets -> openSockets()
            is Act.CloseSockets -> closeSockets()
            is Act.ScheduleReconnect -> {
                removeReconnect()
                val r = Runnable { onEvent(Ev.ReconnectTimer(System.currentTimeMillis())) }
                reconnectRunnable = r
                handler.postDelayed(r, act.delayMs)
            }
            is Act.StopReconnect -> removeReconnect()
            is Act.StopService -> {
                stopTimers()
                sampler.stop()
                alarm.release()
                link.close()
                onStopService()
            }
            is Act.OpenRecorder -> {
                recorder.start(
                    act.sessionId,
                    logic.role,
                    identity.deviceId,
                    act.subject,
                    act.sessionTag,
                    act.operator,
                )
                sessionStartedAtMs = System.currentTimeMillis()
                buffer.activate(act.sessionId) // re-attaches a surviving buffer via its meta
                sessionPacketsSent = 0
            }
            is Act.CloseRecorder -> {
                recorder.stop()
                buffer.deactivate() // bytes stay on disk for the next reconnect's flush decision
                sessionStartedAtMs = null
            }
            is Act.SaveCheckpoint -> logic.activeSessionId?.let { sid ->
                saveCheckpoint(sid, act.lastSequence)
            }
            is Act.ClearCheckpoint -> checkpoint.clear()
            is Act.UploadRescue -> scheduleUpload(act.sessionId)
            is Act.LogEvent -> logEvent(act.type)
            is Act.SetError -> connectError = null // sticky error supersedes the inline one
            is Act.ClearError -> connectError = null
            is Act.Notice -> {
                notice = act.message
                handler.removeCallbacks(clearNotice)
                handler.postDelayed(clearNotice, NOTICE_MS)
            }
            is Act.StartAlarm -> alarm.startAlarm()
            is Act.StopAlarm -> alarm.stopAlarm()
            is Act.StopAlarmSilently -> alarm.silence()
            is Act.ProbeBuffer ->
                onEvent(Ev.BufferProbe(buffer.sessionId, buffer.pendingOnDisk()))
            is Act.QuarantineBuffer -> {
                val moved = buffer.quarantine()
                lastQuarantineCount = moved.size
                lastQuarantineSid = act.sessionId
            }
            is Act.FlushBuffer -> startFlush()
            is Act.StartClockSyncBurst -> {
                startBurst()
                handler.removeCallbacks(resyncRunnable)
                handler.postDelayed(resyncRunnable, ClockSync.RESYNC_INTERVAL_MS)
            }
            is Act.ResetIdentity -> {
                identityStore.reset()
                identity = identityStore.load(
                    legacyDirs.map { File(it, "device_config.json") },
                    parseFlutterPrefs(prefsXml),
                )
            }
        }
    }

    private fun removeReconnect() {
        reconnectRunnable?.let { handler.removeCallbacks(it) }
        reconnectRunnable = null
    }

    private fun saveCheckpoint(sid: String, lastSequence: Long) {
        checkpoint.save(
            sessionId = sid,
            deviceId = identity.deviceId,
            serverIp = logic.desired?.ip ?: "",
            clockOffsetMs = clockSync.offsetMs,
            lastSequenceNumber = lastSequence,
            deviceRole = logic.role,
            subject = logic.subject,
            sessionTag = logic.sessionTag,
            operator = logic.operator,
        )
    }

    // ── sockets ──────────────────────────────────────────────────────────────

    private fun register(): DeviceRegister = DeviceRegister(
        deviceId = identity.deviceId,
        deviceRole = logic.role,
        // Byte-parity with the Dart client, which reported the literal 'Android' and an empty
        // android_version; only app_version is the real one now (BuildConfig, never hard-coded).
        deviceModel = "Android",
        androidVersion = "",
        appVersion = BuildConfig.VERSION_NAME,
        schemaVersion = DeviceRegister.SCHEMA_VERSION,
    )

    private fun openSockets() {
        val ip = logic.desired?.ip ?: return
        controlOpen = false
        telemetryOpen = false
        everUpThisAttempt = false
        connectError = null
        uploader.configure(ip)
        link.connect(ip, register())
        handler.removeCallbacks(handshakeTimeout)
        handler.postDelayed(handshakeTimeout, HANDSHAKE_TIMEOUT_MS)
    }

    private fun closeSockets() {
        handler.removeCallbacks(handshakeTimeout)
        handler.removeCallbacks(resyncRunnable)
        syncOffsets.clear()
        pendingSyncs.clear()
        link.close()
        controlOpen = false
        telemetryOpen = false
        everUpThisAttempt = false
    }

    private fun handleCommand(cmd: Command) {
        when (cmd.type) {
            CommandType.PONG -> {
                val p = PongPayload.parse(cmd.payload)
                p?.telemetryPackets?.let { backendTelemetryPackets = it }
                p?.telemetryAgeMs?.let { backendTelemetryAgeMs = it }
                onEvent(Ev.Pong(p, System.currentTimeMillis()))
            }
            CommandType.START_SESSION -> {
                // scheduled_start_ms is deliberately NOT honoured: waiting on the pipeline
                // thread would freeze acquisition and the heartbeat (see final report).
                val p = StartSessionPayload.parse(cmd.payload) ?: return
                val sid = p.sessionId ?: return
                onEvent(Ev.Start(sid, p.subject, p.sessionTag, p.operator))
            }
            CommandType.STOP_SESSION -> onEvent(Ev.Stop(null))
            CommandType.SET_LABEL -> {
                val p = SetLabelPayload.parse(cmd.payload) ?: return
                onEvent(Ev.Label(p.labelId ?: logic.labelId, p.labelName))
            }
            CommandType.CLOCK_SYNC -> handleClockSyncResponse(cmd)
            CommandType.ERROR_ALERT -> {
                val p = ErrorAlertPayload.parse(cmd.payload)
                onEvent(Ev.ErrorAlert(p.code, p.detail))
            }
            // The sidecar does not record acks; the operator sees delivery via the counters.
            CommandType.ACK -> Unit
        }
    }

    // ── clock sync ───────────────────────────────────────────────────────────

    private fun startBurst() {
        syncOffsets.clear()
        for (i in 0 until ClockSync.BURST_SIZE) {
            handler.postDelayed({
                if (!logic.linkUp) return@postDelayed
                val t0 = System.currentTimeMillis()
                val id = UUID.randomUUID().toString()
                pendingSyncs[id] = t0
                link.sendControl(
                    Command(
                        type = CommandType.CLOCK_SYNC,
                        payload = clockSyncRequestPayload(t0),
                        issuedAtMs = t0,
                        commandId = id,
                    ).encode(),
                )
            }, i * ClockSync.BURST_SPACING_MS)
        }
    }

    private fun handleClockSyncResponse(cmd: Command) {
        val t3 = System.currentTimeMillis()
        // t0 comes from the pending map keyed by commandId (Dart parity): a reply to a burst
        // from a previous connection has no pending entry and is ignored instead of poisoning
        // the median with a stale round-trip.
        val t0 = pendingSyncs.remove(cmd.commandId) ?: return
        val resp = ClockSyncResponse.parse(cmd.payload) ?: return
        val t1 = resp.t1Ms ?: return
        val t2 = resp.t2Ms ?: return
        val offset = clockSync.processResponse(t0, t1, t2, t3) ?: return
        syncOffsets.add(offset)
        if (syncOffsets.size >= ClockSync.BURST_SIZE) {
            clockSync.applyOffsets(syncOffsets)
            syncOffsets.clear()
            pendingSyncs.clear()
            publish()
        }
    }

    // ── sensor readings (100/s, on the pipeline thread) ─────────────────────

    private fun onReading(r: Sampler.Reading) {
        lastMagnitude = r.accMagnitude
        val rawNow = System.currentTimeMillis()
        val correctedNow = clockSync.correctedNow(rawNow)
        val accTs = correctedHwTs(r.accEventMs, clockSync.offsetMs, correctedNow)
        val gyroTs = correctedHwTs(r.gyroEventMs, clockSync.offsetMs, correctedNow)
        val kind = if (r.held) 1 else 0

        val sid = logic.activeSessionId
        if (sid != null) {
            val seq = logic.nextSessionSequence()
            val sample = sample(r, correctedNow, rawNow, seq, accTs, gyroTs, kind)
            // The local guarantee: written whether or not the network exists.
            recorder.write(sample)
            val bytes = sample.toProto().encode()
            if (logic.linkUp && link.sendTelemetry(bytes)) {
                logic.onSessionPacketSent()
                packetsSent++
                sessionPacketsSent++
            } else {
                buffer.enqueue(bytes)
            }
            // Checkpoint on BOTH paths (Dart parity): a restart while offline must still
            // resume near the tail, not replay thousands of deduped sequence numbers.
            if (seq % SessionLogic.CHECKPOINT_EVERY == 0L) saveCheckpoint(sid, seq)
        } else if (logic.linkUp) {
            // Idle telemetry: live socket ONLY — never the CSV, never the buffer. The backend
            // drops sequence >= 2^40 from session files, so idle packets cannot pollute them.
            val sample = sample(r, correctedNow, rawNow, logic.nextIdleSequence(), accTs, gyroTs, kind)
            if (link.sendTelemetry(sample.toProto().encode())) packetsSent++
        }
        publish()
    }

    private fun sample(
        r: Sampler.Reading,
        correctedNow: Long,
        rawNow: Long,
        seq: Long,
        accTs: Long,
        gyroTs: Long,
        kind: Int,
    ) = Sample(
        accX = r.accX, accY = r.accY, accZ = r.accZ,
        gyroX = r.gyroX, gyroY = r.gyroY, gyroZ = r.gyroZ,
        timestampMs = correctedNow,
        rawTimestampMs = rawNow,
        sequence = seq,
        deviceId = identity.deviceId,
        accTsMs = accTs,
        gyroTsMs = gyroTs,
        sampleKind = kind,
        labelId = logic.labelId,
        labelName = logic.labelName,
    )

    // ── buffer flush (reconnect replay) ──────────────────────────────────────

    private fun startFlush() {
        if (flushing) return
        val snapshots = buffer.snapshotForFlush()
        if (snapshots.isEmpty()) {
            scheduleUpload(null) // "then upload any finished, unmarked rescue CSVs"
            return
        }
        flushing = true
        flushExecutor.execute {
            var stopped = false
            val completed = buffer.readRecords(snapshots) { bytes ->
                if (link.sendTelemetry(bytes)) {
                    true
                } else {
                    stopped = true
                    false
                }
            }
            val truncated = buffer.truncatedRecords
            val malformed = buffer.malformedRecords
            handler.post {
                flushing = false
                if (completed && !stopped && logic.linkUp) {
                    if (truncated == 0 && malformed == 0) {
                        // Delete ONLY after a clean parse (spec): backend dedup on
                        // (device, session, sequence) makes any re-send idempotent, but an
                        // unparsed tail must survive for the next reconnect.
                        buffer.clearAfterFlush()
                    } else {
                        recorder.logEvent(
                            buildJsonObject {
                                put("type", "fallback_buffer_parse_warning")
                                put("time_ms", System.currentTimeMillis())
                                put("dropped_overflow", buffer.droppedOverflow)
                                put("truncated_records", truncated)
                                put("malformed_records", malformed)
                            },
                        )
                    }
                }
                scheduleUpload(null)
                publish()
            }
        }
    }

    private fun scheduleUpload(onlySessionId: String?) {
        val ip = logic.desired?.ip ?: return
        uploader.configure(ip)
        uploadExecutor.execute {
            val n = runCatching { uploader.uploadPending(onlySessionId) }.getOrDefault(0)
            if (n > 0) {
                handler.post {
                    uploadsFinished += n
                    publish()
                }
            }
        }
    }

    // ── sidecar events ───────────────────────────────────────────────────────

    private fun logEvent(type: String) {
        val now = System.currentTimeMillis()
        val o = buildJsonObject {
            put("type", type)
            when (type) {
                "service_start", "service_restart" -> {
                    put("engine_id", engineId)
                    put("time_ms", now)
                }
                "connection_lost" -> put("time_ms", now)
                "connection_restored" -> {
                    put("time_ms", now)
                    logic.lostDurationMs(now)?.let { put("duration_ms", it) }
                }
                "telemetry_stale" -> {
                    put("telemetry_age_ms", backendTelemetryAgeMs)
                    put("telemetry_packets", backendTelemetryPackets)
                    put("time_ms", now)
                }
                "buffer_drop" -> {
                    // QuarantineBuffer ran just before this act (SessionLogic emits them in
                    // that order), so the counts describe exactly this drop.
                    put("time_ms", now)
                    put("count", lastQuarantineCount)
                    put("session_id", lastQuarantineSid)
                }
                else -> put("time_ms", now)
            }
        }
        recorder.logEvent(o)
    }

    // ── snapshot publishing (conflated, ≤ 10/s) ──────────────────────────────

    private fun publish() {
        val now = System.currentTimeMillis()
        val elapsed = now - lastPublishMs
        if (elapsed >= PUBLISH_MIN_INTERVAL_MS) {
            lastPublishMs = now
            stateFlow.value = buildSnapshot(now)
        } else if (!publishScheduled) {
            publishScheduled = true
            handler.postDelayed({
                publishScheduled = false
                publish()
            }, PUBLISH_MIN_INTERVAL_MS - elapsed)
        }
    }

    private fun buildSnapshot(now: Long): StateSnapshot {
        if (now - batteryReadAtMs > BATTERY_CACHE_MS) {
            batteryReadAtMs = now
            batteryPercent = try {
                (app.getSystemService(Context.BATTERY_SERVICE) as BatteryManager)
                    .getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
            } catch (_: Exception) {
                -1
            }
        }
        val linkState = when {
            logic.linkUp -> LinkState.CONNECTED
            logic.connecting -> LinkState.CONNECTING
            logic.desired != null -> LinkState.OFFLINE
            else -> LinkState.DISCONNECTED
        }
        return StateSnapshot(
            deviceId = identity.deviceId,
            role = logic.desired?.let { logic.role } ?: identity.role,
            desired = logic.desired,
            // Falls back to the identity file's last server so the Setup screen can prefill
            // the IP field (and preselect an mDNS match) even with no desired endpoint.
            serverIp = logic.desired?.ip ?: identity.lastServerIp,
            link = linkState,
            connecting = logic.connecting,
            reconnecting = logic.desired != null && !logic.linkUp && logic.error == null &&
                logic.offlineSinceMs != null,
            offlineSinceMs = logic.offlineSinceMs,
            connectError = connectError,
            error = logic.error,
            notice = notice,
            activeSessionId = logic.activeSessionId,
            serverState = logic.serverState,
            sessionStartedAtMs = sessionStartedAtMs,
            subject = logic.subject,
            sessionTag = logic.sessionTag,
            labelId = logic.labelId,
            labelName = logic.labelName,
            packetsSent = packetsSent,
            sessionPacketsSent = sessionPacketsSent,
            packetsBuffered = buffer.bufferedCount,
            localRows = recorder.rows,
            localBackupOk = recorder.lastError == null,
            localError = recorder.lastError,
            uploadsFinished = uploadsFinished,
            accHz = sampler.observedAccHz,
            gyroHz = sampler.observedGyroHz,
            accMagnitude = lastMagnitude,
            clockOffsetMs = clockSync.offsetMs,
            clockSynced = clockSync.isSynced,
            lastRttMs = clockSync.lastRttMs,
            alarmSounding = alarm.isAlarming && !alarm.isSilenced,
            alarmSilenced = alarm.isAlarming && alarm.isSilenced,
            batteryPercent = batteryPercent,
            versionName = BuildConfig.VERSION_NAME,
        )
    }

    private fun stopTimers() {
        handler.removeCallbacks(tickRunnable)
        handler.removeCallbacks(flushRunnable)
        handler.removeCallbacks(samplingEventRunnable)
        handler.removeCallbacks(resyncRunnable)
        handler.removeCallbacks(handshakeTimeout)
        removeReconnect()
        handler.removeCallbacks(clearNotice)
    }
}

private fun round2(v: Double): Double = Math.round(v * 100.0) / 100.0

/** Engine id, same shape as the Dart original: 6 hex of µs-time + 6 random hex. */
private fun newEngineId(): String {
    val t = (System.nanoTime() / 1000) and 0xFFFFFFL
    val rnd = UUID.randomUUID().toString().replace("-", "").take(6)
    return String.format(java.util.Locale.ROOT, "%06x", t) + "-" + rnd
}
