package id.ac.ui.cs.csl.fams.sapper.core

/**
 * The pure decision core of the pipeline: `handle(event) -> List<Action>`, no Android imports,
 * no I/O, no threads. Everything stateful about a session — sequence numbering, PONG resync,
 * reconnect policy, flush-vs-quarantine — is decided here so it can be unit-tested, and the
 * [Pipeline] merely executes the returned actions on its single thread.
 *
 * The old app spread this across concurrent Dart callbacks, which is how a heartbeat followed
 * by START reset the sequence counter mid-stream and got real samples dropped as duplicates.
 * Here state changes only in arrival order on one thread.
 */

/** Sequence base for packets emitted while no session is active (SPEC.md §6.3). The backend
 *  drops sequence numbers >= 2^40 from session files, so idle telemetry can never pollute a
 *  session's dedup namespace. */
const val IDLE_SEQ_BASE: Long = 1L shl 40

sealed interface Ev {
    /** Operator (or auto-reconnect) asked for a specific endpoint. */
    data class Connect(val ip: String, val role: String) : Ev

    /** Operator asked to disconnect. Refused while a session is active. */
    data object DisconnectRequest : Ev

    /** Both sockets are open and the DeviceRegister was sent. */
    data class LinkEstablished(val nowMs: Long) : Ev

    /** A socket closed or errored, or the PONG/watchdog timeout fired. */
    data class LinkLost(val code: Int?, val nowMs: Long) : Ev

    data class Pong(val payload: PongPayload?, val nowMs: Long) : Ev

    data class Start(
        val sessionId: String,
        val subject: String?,
        val sessionTag: String?,
        val operator: String?,
    ) : Ev

    data class Stop(val sessionId: String?) : Ev

    data class Label(val labelId: Int, val labelName: String?) : Ev

    data class ErrorAlert(val code: String?, val detail: String?) : Ev

    /** 1 s service tick: PONG timeout, telemetry watchdog, flush-probe deadline. */
    data class Tick(val nowMs: Long) : Ev

    /** Answer to [Act.ProbeBuffer]: what the offline buffer on disk belongs to. */
    data class BufferProbe(val sessionId: String?, val pendingBytes: Long) : Ev

    /** The scheduled reconnect backoff timer fired. */
    data class ReconnectTimer(val nowMs: Long) : Ev

    /** A checkpoint from a previous process says this session was RECORDING. The metadata
     *  fields are empty for checkpoints written by an older build. */
    data class ResumeCheckpoint(
        val sessionId: String,
        val lastSequence: Long,
        /** First sequence number to use after the restart, derived from the checkpoint and
         *  the rescue CSV tail by the Pipeline (which knows the flush interval). */
        val resumeSequence: Long,
        val subject: String = "",
        val sessionTag: String = "",
        val operator: String = "",
    ) : Ev

    /** In-app device reset: fresh identity, back to Setup. */
    data object ResetDevice : Ev

    /** The app was relaunched with a persisted desired endpoint but no connect yet. */
    data class RestoreDesired(val ip: String, val role: String) : Ev
}

sealed interface Act {
    data class PersistDesired(val ip: String, val role: String) : Act
    data object ClearDesired : Act
    data object OpenSockets : Act
    data object CloseSockets : Act
    data class ScheduleReconnect(val delayMs: Long) : Act
    data object StopReconnect : Act
    data object StopService : Act

    data class OpenRecorder(
        val sessionId: String,
        val subject: String,
        val sessionTag: String,
        val operator: String,
    ) : Act

    data object CloseRecorder : Act

    /** Persists `last_sequence` for resume-after-kill (sid = the active session). */
    data class SaveCheckpoint(val lastSequence: Long) : Act
    data object ClearCheckpoint : Act

    data class UploadRescue(val sessionId: String) : Act

    /** Sidecar events.jsonl marker; the pipeline adds engine_id/time_ms/counters. */
    data class LogEvent(val type: String) : Act

    data class SetError(val code: String, val detail: String) : Act
    data object ClearError : Act
    data class Notice(val message: String) : Act

    data object StartAlarm : Act
    data object StopAlarm : Act
    data object StopAlarmSilently : Act

    /** Asks the pipeline to answer with [Ev.BufferProbe] (buffer state is I/O). */
    data object ProbeBuffer : Act
    data class QuarantineBuffer(val sessionId: String?) : Act
    data object FlushBuffer : Act

    data object StartClockSyncBurst : Act
    data object ResetIdentity : Act
}

class SessionLogic(private val nowMs: () -> Long = { System.currentTimeMillis() }) {
    companion object {
        const val PING_INTERVAL_MS = 1000L
        const val PONG_TIMEOUT_MS = 8000L
        const val HANDSHAKE_TIMEOUT_MS = 6000L
        const val TELEMETRY_STALE_MS = 12000L
        const val CHECKPOINT_EVERY = 250L
        const val STATE_WAIT_MS = 3000L
        val BACKOFF_MS = longArrayOf(1000, 2000, 3000)

        const val ROLE_COLLISION = "ROLE_COLLISION"
        const val REFUSED_DISCONNECT =
            "An active session can only be stopped by the backend operator."
    }

    // ── state ────────────────────────────────────────────────────────────────
    var activeSessionId: String? = null
        private set
    /** Next session sequence number: 0,1,2,… per session. */
    var sequence: Long = 0
        private set
    private var idleCounter: Long = 0

    var subject: String = ""
        private set
    var sessionTag: String = ""
        private set
    var operator: String = ""
        private set
    var labelId: Int = 0
        private set
    var labelName: String = "0"
        private set

    var desired: Endpoint? = null
        private set
    var role: String = DEFAULT_ROLE
        private set
    var linkUp: Boolean = false
        private set
    var connecting: Boolean = false
        private set
    var reconnectAttempt: Int = 0
        private set
    var error: AlertError? = null
        private set

    var serverState: String? = null
        private set
    var serverLateSid: String? = null
        private set
    var lastStateAtMs: Long? = null
        private set
    var offlineSinceMs: Long? = null
        private set

    private var lastPongAtMs: Long? = null
    private var lostAtMs: Long? = null
    private var backendTelemetryPackets: Long? = null
    private var backendTelemetryAgeMs: Long? = null
    private var lastTelemetryProgressAtMs: Long? = null
    private var sentThisSession: Long = 0
    private var reconnectScheduled = false

    /** Set on LinkEstablished; the FIRST PONG carrying a state (or a 3 s deadline) triggers
     *  the buffer flush decision — never before, because the target session is PONG's job. */
    private var flushProbePending = false
    private var flushProbeDeadlineMs: Long? = null

    // ── sequence allocation (called by the pipeline on every emit) ──────────
    fun nextSessionSequence(): Long = sequence++
    fun nextIdleSequence(): Long = IDLE_SEQ_BASE + idleCounter++

    /** Called after a session packet actually hit the telemetry socket. */
    fun onSessionPacketSent() {
        sentThisSession++
    }

    // ── the decision function ────────────────────────────────────────────────
    fun handle(event: Ev): List<Act> {
        val acts = mutableListOf<Act>()
        when (event) {
            is Ev.Connect -> handleConnect(event, acts)
            is Ev.DisconnectRequest -> handleDisconnect(acts)
            is Ev.LinkEstablished -> handleLinkUp(event.nowMs, acts)
            is Ev.LinkLost -> handleLinkLost(event.code, event.nowMs, acts)
            is Ev.Pong -> handlePong(event, acts)
            is Ev.Start -> handleStart(event, acts)
            is Ev.Stop -> handleStop(acts)
            is Ev.Label -> {
                labelId = event.labelId
                labelName = event.labelName ?: event.labelId.toString()
            }
            is Ev.ErrorAlert -> handleErrorAlert(event, acts)
            is Ev.Tick -> handleTick(event.nowMs, acts)
            is Ev.BufferProbe -> handleBufferProbe(event, acts)
            is Ev.ReconnectTimer -> {
                reconnectScheduled = false
                if (!linkUp && !connecting && desired != null && error == null) {
                    connecting = true
                    acts += Act.OpenSockets
                }
            }
            is Ev.ResumeCheckpoint -> handleResume(event, acts)
            is Ev.ResetDevice -> {
                desired = null
                error = null
                connecting = false
                reconnectScheduled = false
                acts += Act.CloseSockets
                acts += Act.StopReconnect
                acts += Act.ClearDesired
                acts += Act.ResetIdentity
                acts += Act.StopService
            }
            is Ev.RestoreDesired -> {
                // Relaunch with a persisted endpoint: reconnect without the UI.
                if (desired == null) {
                    desired = Endpoint(event.ip, event.role)
                    role = event.role
                }
                if (!linkUp && !connecting) {
                    connecting = true
                    acts += Act.OpenSockets
                }
            }
        }
        return acts
    }

    private fun handleConnect(e: Ev.Connect, acts: MutableList<Act>) {
        val same = desired == Endpoint(e.ip, e.role)
        if (same && (linkUp || connecting)) return // no-op, already on it
        if (linkUp || connecting) acts += Act.CloseSockets // clean switch of ip/role
        linkUp = false
        connecting = true
        reconnectAttempt = 0
        reconnectScheduled = false
        desired = Endpoint(e.ip, e.role)
        role = e.role
        if (error != null) {
            error = null
            acts += Act.ClearError
        }
        acts += Act.PersistDesired(e.ip, e.role)
        acts += Act.OpenSockets
        // No ScheduleReconnect here: a failed handshake always arrives as LinkLost, and that
        // is what arms the 1/2/3 s backoff — scheduling here would burn the first slot.
    }

    private fun handleDisconnect(acts: MutableList<Act>) {
        if (activeSessionId != null) {
            acts += Act.Notice(REFUSED_DISCONNECT)
            return
        }
        desired = null
        connecting = false
        linkUp = false
        reconnectScheduled = false
        offlineSinceMs = null
        acts += Act.CloseSockets
        acts += Act.StopReconnect
        acts += Act.ClearDesired
        // Close the file cleanly even though no session should be open (no unflushed tail).
        acts += Act.CloseRecorder
        acts += Act.StopService
    }

    private fun handleLinkUp(now: Long, acts: MutableList<Act>) {
        val wasUp = linkUp
        linkUp = true
        connecting = false
        reconnectAttempt = 0
        reconnectScheduled = false
        offlineSinceMs = null
        lastPongAtMs = now
        backendTelemetryPackets = null
        backendTelemetryAgeMs = null
        lastTelemetryProgressAtMs = now
        flushProbePending = true
        flushProbeDeadlineMs = now + STATE_WAIT_MS
        if (activeSessionId != null && !wasUp) {
            acts += Act.StopAlarm
            acts += Act.LogEvent("connection_restored")
        }
        acts += Act.StartClockSyncBurst
    }

    private fun handleLinkLost(code: Int?, now: Long, acts: MutableList<Act>) {
        // Only a live (or in-flight) connection can trigger drop→reconnect; duplicate signals
        // from a socket that is already down must not stack reconnect attempts.
        if (!linkUp && !connecting) return
        val wasUp = linkUp
        linkUp = false
        connecting = false
        offlineSinceMs = now
        // Never gate a later buffer flush on a stale "RECORDING".
        serverState = null
        serverLateSid = null
        flushProbePending = false
        flushProbeDeadlineMs = null
        acts += Act.CloseSockets

        if (code == 4001 || code == 4002) {
            // The backend refused us. Reconnecting every 3 s forever (the old role-collision
            // loop) just spams the backend; an operator has to pick another role.
            val (errCode, detail) = if (code == 4002) {
                ROLE_COLLISION to "Role ${roleLabel()} is already used by another phone — pick another role"
            } else {
                "REGISTER_REJECTED" to "Backend rejected this phone's registration (close 4001)."
            }
            error = AlertError(errCode, detail)
            acts += Act.SetError(errCode, detail)
            acts += Act.StopReconnect
            reconnectScheduled = false
            return
        }

        if (activeSessionId != null && wasUp) {
            acts += Act.StartAlarm
            lostAtMs = now
            acts += Act.LogEvent("connection_lost")
        }
        if (error == null && desired != null) {
            if (!reconnectScheduled) {
                reconnectScheduled = true
                acts += Act.ScheduleReconnect(backoffDelayMs())
            }
        }
    }

    private fun handlePong(e: Ev.Pong, acts: MutableList<Act>) {
        lastPongAtMs = e.nowMs
        val p = e.payload
        if (p != null) {
            p.telemetryPackets?.let { next ->
                if (backendTelemetryPackets == null || next != backendTelemetryPackets) {
                    lastTelemetryProgressAtMs = e.nowMs
                }
                backendTelemetryPackets = next
            }
            p.telemetryAgeMs?.let { backendTelemetryAgeMs = it }
            val state = p.state
            if (!state.isNullOrEmpty()) {
                serverState = state
                serverLateSid = p.lateSid
                lastStateAtMs = e.nowMs

                val sid = p.sessionId
                val serverRecording = state == "RECORDING"
                if (!serverRecording && activeSessionId != null) {
                    // We think we are recording; the backend is not. We missed the STOP.
                    val ended = activeSessionId!!
                    activeSessionId = null
                    sentThisSession = 0
                    acts += Act.CloseRecorder
                    acts += Act.ClearCheckpoint
                    acts += Act.StopAlarm
                    acts += Act.UploadRescue(ended)
                } else if (serverRecording && sid != null && sid != activeSessionId) {
                    // A session we are not part of is running — adopt it with a fresh dedup
                    // namespace (sequence = 0) and whatever metadata is known.
                    activeSessionId = sid
                    sequence = 0
                    sentThisSession = 0
                    acts += Act.OpenRecorder(sid, subject, sessionTag, operator)
                    acts += Act.SaveCheckpoint(0)
                    acts += Act.LogEvent("service_start")
                }
            }
        }
        // Reconnect flush: decided after the FIRST PONG of a new connection, once the
        // authoritative state is known (plan R3 — every connect path reconciles).
        if (flushProbePending && (serverState != null || !flushPendingState(e.nowMs))) {
            flushProbePending = false
            flushProbeDeadlineMs = null
            acts += Act.ProbeBuffer
        }
    }

    private fun flushPendingState(now: Long): Boolean =
        flushProbeDeadlineMs != null && now < flushProbeDeadlineMs!!

    private fun handleStart(e: Ev.Start, acts: MutableList<Act>) {
        // Keep the last known metadata for any field the payload omits.
        e.subject?.let { subject = it }
        e.sessionTag?.let { sessionTag = it }
        e.operator?.let { operator = it }
        if (e.sessionId == activeSessionId) {
            // Already adopted (e.g. from a PONG resync): DO NOT reset the sequence — the old
            // unconditional reset is exactly the mid-stream race this rebuild fixes. Just
            // record the metadata.
            return
        }
        activeSessionId = e.sessionId
        sequence = 0
        sentThisSession = 0
        acts += Act.OpenRecorder(e.sessionId, subject, sessionTag, operator)
        acts += Act.SaveCheckpoint(0)
        acts += Act.LogEvent("service_start")
    }

    private fun handleStop(acts: MutableList<Act>) {
        val ended = activeSessionId ?: return
        activeSessionId = null
        sentThisSession = 0
        acts += Act.CloseRecorder
        acts += Act.ClearCheckpoint
        acts += Act.StopAlarm
        acts += Act.UploadRescue(ended)
    }

    private fun handleErrorAlert(e: Ev.ErrorAlert, acts: MutableList<Act>) {
        if (e.code == ROLE_COLLISION) {
            val detail = e.detail
                ?: "Role ${roleLabel()} is already used by another phone — pick another role"
            error = AlertError(ROLE_COLLISION, detail)
            acts += Act.SetError(ROLE_COLLISION, detail)
            acts += Act.StopReconnect
            reconnectScheduled = false
        } else if (e.code != null) {
            acts += Act.Notice("Backend alert ${e.code}: ${e.detail ?: ""}".trim())
        }
    }

    private fun handleTick(now: Long, acts: MutableList<Act>) {
        if (linkUp) {
            // PONG timeout: 8 s tolerates brief Wi-Fi degradation during subject motion
            // without a spurious reconnect cycle, and still catches a genuine dropout.
            val lastPong = lastPongAtMs
            if (lastPong != null && now - lastPong > PONG_TIMEOUT_MS) {
                acts += handleLinkLostAct(now)
                return
            }
            // Telemetry watchdog: the sockets look fine but the backend stopped seeing our
            // packets (half-open NAT, dead telemetry socket). Force a reconnect.
            if (activeSessionId != null && sentThisSession > 0) {
                val staleByAge =
                    backendTelemetryAgeMs != null && backendTelemetryAgeMs!! >= TELEMETRY_STALE_MS
                val staleByCounter = lastTelemetryProgressAtMs != null &&
                    now - lastTelemetryProgressAtMs!! >= TELEMETRY_STALE_MS
                if (staleByAge || staleByCounter) {
                    acts += Act.LogEvent("telemetry_stale")
                    acts += handleLinkLostAct(now)
                    return
                }
            }
        }
        // Flush-probe deadline: the backend gave no state within 3 s of connecting; decide
        // with what we have (an unknown target quarantines, exactly like the old client).
        if (flushProbePending && !flushPendingState(now)) {
            flushProbePending = false
            flushProbeDeadlineMs = null
            acts += Act.ProbeBuffer
        }
    }

    /** Inline go-offline for the tick paths; mirrors handleLinkLost minus the dedup guard. */
    private fun handleLinkLostAct(now: Long): MutableList<Act> {
        val acts = mutableListOf<Act>()
        linkUp = false
        offlineSinceMs = now
        serverState = null
        serverLateSid = null
        flushProbePending = false
        flushProbeDeadlineMs = null
        acts += Act.CloseSockets
        if (activeSessionId != null) {
            acts += Act.StartAlarm
            lostAtMs = now
            acts += Act.LogEvent("connection_lost")
        }
        if (error == null && desired != null && !reconnectScheduled) {
            reconnectScheduled = true
            acts += Act.ScheduleReconnect(backoffDelayMs())
        }
        return acts
    }

    private fun handleBufferProbe(e: Ev.BufferProbe, acts: MutableList<Act>) {
        if (e.pendingBytes == 0L && e.sessionId == null) return
        // Deliver ONLY into the session these bytes belong to. The backend discards telemetry
        // that does not match an open (or late-window) session, and the old code then erased
        // the local copy — plan D3.
        val target = if (serverState == "RECORDING") activeSessionId else serverLateSid
        if (target == null || e.sessionId == null || e.sessionId != target) {
            acts += Act.QuarantineBuffer(e.sessionId)
            acts += Act.LogEvent("buffer_drop")
            val sid = e.sessionId ?: "unknown"
            acts += Act.Notice(
                "Buffered data from session $sid does not belong to any open session — " +
                    "kept as orphan files, not lost.",
            )
        } else {
            acts += Act.FlushBuffer
        }
    }

    private fun handleResume(e: Ev.ResumeCheckpoint, acts: MutableList<Act>) {
        if (activeSessionId != null) return
        // Restore the metadata the checkpoint carried, otherwise the reopened rescue CSV is
        // named from empty subject/tag and a second file appears for the same (session,
        // phone) — the uploader identifies a transfer by (session, device) and the two
        // fragments would compete for it.
        subject = e.subject
        sessionTag = e.sessionTag
        operator = e.operator
        // Resume ABOVE every sequence number that may already be on the backend or in the rescue
        // CSV — the Pipeline derives it from the CSV tail plus the flush margin. Samples
        // written after the last checkpoint are NOT necessarily absent: up to one flush
        // interval can have been sent while still buffered in the writer. Reusing their
        // numbers makes the backend's (device_id, session_id, sequence_number, first wins)
        // dedup discard the new readings; a gap is harmless. The old code resumed at
        // lastSequence + 1 precisely because it wrongly assumed the backend dedup handled it.
        activeSessionId = e.sessionId
        sequence = e.resumeSequence
        sentThisSession = 0
        acts += Act.OpenRecorder(e.sessionId, subject, sessionTag, operator)
        acts += Act.SaveCheckpoint(e.resumeSequence)
        acts += Act.LogEvent("service_restart")
    }

    /** 1 s, 2 s, then 3 s forever while a desired endpoint exists. */
    private fun backoffDelayMs(): Long {
        val d = BACKOFF_MS[minOf(reconnectAttempt, BACKOFF_MS.size - 1)]
        reconnectAttempt++
        return d
    }

    private fun roleLabel(): String =
        ROLES.firstOrNull { it.first == role }?.second ?: role

    /** True when [now] is still inside the checkpoint grace of the last known state. */
    fun recordingUnconfirmed(now: Long): Boolean =
        activeSessionId != null &&
            (lastStateAtMs == null || now - lastStateAtMs!! > 15000)

    /** Duration of the ongoing connection gap, for `connection_restored` and the alarm UI. */
    fun lostDurationMs(now: Long): Long? = lostAtMs?.let { now - it }
}
