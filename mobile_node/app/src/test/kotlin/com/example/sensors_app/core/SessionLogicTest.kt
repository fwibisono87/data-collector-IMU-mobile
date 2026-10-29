package com.example.sensors_app.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The race-fix contract: session state changes only in arrival order, a START for an
 * already-adopted session never resets the sequence, PONG is the source of truth, and a
 * role collision must never reconnect.
 */
class SessionLogicTest {

    private var clock = 1_700_000_000_000L
    private lateinit var l: SessionLogic

    @Before
    fun setUp() {
        l = SessionLogic(nowMs = { clock })
    }

    private fun connect() {
        l.handle(Ev.Connect("192.168.1.5", "chest"))
        l.handle(Ev.LinkEstablished(clock))
    }

    private fun start(sid: String = "s1") =
        l.handle(Ev.Start(sid, "p01", "walk", "francis"))

    private fun pongRecording(sid: String?) = l.handle(
        Ev.Pong(PongPayload("RECORDING", sid, null, 10, 100), clock),
    )

    private fun pongIdle(lateSid: String? = null) = l.handle(
        Ev.Pong(PongPayload("IDLE", null, lateSid, 10, 100), clock),
    )

    // ── 1. START then START with the same sid does not reset the sequence ───

    @Test
    fun duplicateStartDoesNotResetSequence() {
        connect()
        val acts1 = start("s1")
        assertTrue(acts1.any { it is Act.OpenRecorder })
        repeat(100) { l.nextSessionSequence() }
        assertEquals(100L, l.sequence)

        val acts2 = start("s1") // backend re-broadcasts START (phone was mid-reconnect)
        assertFalse("same-sid START must not reopen the recorder", acts2.any { it is Act.OpenRecorder })
        assertEquals(100L, l.sequence) // the fix: no reset to 0
        assertEquals(100L, l.nextSessionSequence())
    }

    // ── 2. PONG adoption then START with the same sid ───────────────────────

    @Test
    fun pongAdoptThenStartSameSidKeepsSequence() {
        connect()
        val adopt = pongRecording("s9")
        assertTrue(adopt.any { it is Act.OpenRecorder && it.sessionId == "s9" })
        assertTrue(adopt.any { it is Act.LogEvent && it.type == "service_start" })
        repeat(50) { l.nextSessionSequence() }

        val acts = start("s9") // the broadcast START arrives late, after adoption
        assertFalse(acts.any { it is Act.OpenRecorder })
        assertEquals(50L, l.sequence)
        // …but metadata from the START is recorded for later use.
        assertEquals("p01", l.subject)
    }

    @Test
    fun pongAdoptResetsSequenceForNewSession() {
        connect()
        start("s1")
        repeat(10) { l.nextSessionSequence() }
        pongRecording("s2") // backend moved on while we were dark
        assertEquals(0L, l.sequence)
        assertEquals("s2", l.activeSessionId)
    }

    // ── 3. a missed STOP detected via PONG ──────────────────────────────────

    @Test
    fun missedStopDetectedViaPong() {
        connect()
        start("s1")
        val acts = pongIdle() // backend says IDLE while we think we record
        assertNull(l.activeSessionId)
        assertTrue(acts.any { it is Act.CloseRecorder })
        assertTrue(acts.any { it is Act.ClearCheckpoint })
        assertTrue(acts.any { it is Act.StopAlarm })
        // The rescue CSV must be offered for upload even though no STOP ever arrived.
        assertTrue(acts.any { it is Act.UploadRescue && it.sessionId == "s1" })
    }

    // ── 4. flush vs quarantine decisions ────────────────────────────────────

    @Test
    fun flushWhenBufferMatchesRecordingSession() {
        connect()
        start("s1")
        pongRecording("s1") // first PONG of the connection triggers the probe
        val acts = l.handle(Ev.BufferProbe("s1", 4096))
        assertTrue(acts.any { it is Act.FlushBuffer })
        assertFalse(acts.any { it is Act.QuarantineBuffer })
    }

    @Test
    fun flushWhenBufferMatchesLateSidWhileIdle() {
        connect()
        l.handle(Ev.Pong(PongPayload("IDLE", null, "s0", null, null), clock))
        val acts = l.handle(Ev.BufferProbe("s0", 4096))
        assertTrue(acts.any { it is Act.FlushBuffer })
    }

    @Test
    fun quarantineWhenBufferSessionIsNotOpen() {
        connect()
        l.handle(Ev.Pong(PongPayload("IDLE", null, null, null, null), clock))
        val acts = l.handle(Ev.BufferProbe("s_old", 4096))
        assertTrue(acts.any { it is Act.QuarantineBuffer && it.sessionId == "s_old" })
        assertTrue(acts.any { it is Act.LogEvent && it.type == "buffer_drop" })
        assertTrue(acts.any { it is Act.Notice })
        assertFalse(acts.any { it is Act.FlushBuffer })
    }

    @Test
    fun quarantineWhenStateUnknownAfterDeadline() {
        connect()
        // No state-carrying PONG within 3 s: the tick deadline fires the probe anyway.
        val acts = l.handle(Ev.Tick(clock + SessionLogic.STATE_WAIT_MS + 1))
        assertTrue(acts.any { it is Act.ProbeBuffer })
        val q = l.handle(Ev.BufferProbe("s_old", 100))
        assertTrue(q.any { it is Act.QuarantineBuffer })
    }

    @Test
    fun emptyBufferNeedsNoDecision() {
        connect()
        pongRecording(null)
        val acts = l.handle(Ev.BufferProbe(null, 0))
        assertTrue(acts.isEmpty())
    }

    // ── 5. idle sequence numbering ──────────────────────────────────────────

    @Test
    fun idleSequencesStartAtIdleSeqBase() {
        assertEquals(IDLE_SEQ_BASE, l.nextIdleSequence())
        assertEquals(IDLE_SEQ_BASE + 1, l.nextIdleSequence())
        assertEquals(1L shl 40, IDLE_SEQ_BASE)
        // Session sequences are a separate namespace.
        assertEquals(0L, l.nextSessionSequence())
        assertEquals(1L, l.nextSessionSequence())
    }

    // ── 6. role-collision close stops reconnecting ──────────────────────────

    @Test
    fun roleCollisionCloseStopsReconnect() {
        connect()
        val acts = l.handle(Ev.LinkLost(4002, clock + 1))
        assertTrue(acts.any { it is Act.StopReconnect })
        assertFalse("must never schedule another reconnect", acts.any { it is Act.ScheduleReconnect })
        val err = l.error
        assertEquals("ROLE_COLLISION", err?.code)
        assertTrue(err!!.detail.contains("already used by another phone"))
        // Even a later tick does not reconnect while the error is sticky.
        val ticks = l.handle(Ev.Tick(clock + 5000))
        assertFalse(ticks.any { it is Act.ScheduleReconnect })
        assertFalse(ticks.any { it is Act.OpenSockets })
    }

    @Test
    fun errorAlertRoleCollisionStopsReconnect() {
        connect()
        val acts = l.handle(Ev.ErrorAlert("ROLE_COLLISION", "Role chest in use by 192.168.1.9"))
        assertTrue(acts.any { it is Act.StopReconnect })
        assertEquals("ROLE_COLLISION", l.error?.code)
        // The 4002 close that follows must not add a reconnect on top.
        val lost = l.handle(Ev.LinkLost(4002, clock + 1))
        assertFalse(lost.any { it is Act.ScheduleReconnect })
    }

    @Test
    fun explicitConnectClearsTheStickyError() {
        connect()
        l.handle(Ev.LinkLost(4002, clock))
        val acts = l.handle(Ev.Connect("192.168.1.5", "waist"))
        assertTrue(acts.any { it is Act.ClearError })
        assertTrue(acts.any { it is Act.OpenSockets })
        assertNull(l.error)
    }

    // ── 7. disconnect refused during a session ──────────────────────────────

    @Test
    fun disconnectRefusedDuringSession() {
        connect()
        start("s1")
        val acts = l.handle(Ev.DisconnectRequest)
        val notice = acts.filterIsInstance<Act.Notice>().singleOrNull()
        assertEquals(SessionLogic.REFUSED_DISCONNECT, notice?.message)
        assertFalse(acts.any { it is Act.CloseSockets })
        assertFalse(acts.any { it is Act.StopService })
        assertEquals("s1", l.activeSessionId)
    }

    @Test
    fun disconnectAllowedWhenIdleClearsDesired() {
        connect()
        val acts = l.handle(Ev.DisconnectRequest)
        assertTrue(acts.any { it is Act.CloseSockets })
        assertTrue(acts.any { it is Act.ClearDesired })
        assertTrue(acts.any { it is Act.StopService })
        assertNull("nothing may auto-reconnect later", l.desired)
    }

    // ── 8. resume-from-checkpoint sequence ──────────────────────────────────

    @Test
    fun resumeFromCheckpointContinuesSequence() {
        val acts = l.handle(Ev.ResumeCheckpoint("s1", 7499))
        assertEquals(7500L, l.sequence)
        assertEquals("s1", l.activeSessionId)
        assertTrue(acts.any { it is Act.OpenRecorder && it.sessionId == "s1" })
        assertTrue(acts.any { it is Act.SaveCheckpoint && it.lastSequence == 7499L })
        assertTrue(acts.any { it is Act.LogEvent && it.type == "service_restart" })
        assertEquals(7500L, l.nextSessionSequence())
    }

    @Test
    fun resumeIsIgnoredWhenASessionIsAlreadyActive() {
        connect()
        start("s2")
        repeat(5) { l.nextSessionSequence() }
        val acts = l.handle(Ev.ResumeCheckpoint("s1", 7499))
        assertTrue(acts.isEmpty())
        assertEquals("s2", l.activeSessionId)
        assertEquals(5L, l.sequence)
    }

    // ── reconnect backoff and PONG timeout ──────────────────────────────────

    @Test
    fun backoffIs123ThenThreeForever() {
        connect()
        fun lost(): Long = l.handle(Ev.LinkLost(null, clock))
            .filterIsInstance<Act.ScheduleReconnect>().single().delayMs.also {
            // Let the timer fire so the next loss schedules again.
            l.handle(Ev.ReconnectTimer(clock))
        }

        assertEquals(1000L, lost())
        assertEquals(2000L, lost())
        assertEquals(3000L, lost())
        assertEquals(3000L, lost())
        assertEquals(3000L, lost())
    }

    @Test
    fun reconnectTimerOpensSocketsOnlyWhenDown() {
        connect()
        l.handle(Ev.LinkLost(null, clock))
        val acts = l.handle(Ev.ReconnectTimer(clock + 1000))
        assertTrue(acts.any { it is Act.OpenSockets })
        // A stray timer while connected is a no-op.
        val none = l.handle(Ev.ReconnectTimer(clock + 2000))
        assertFalse(none.any { it is Act.OpenSockets })
    }

    @Test
    fun pongTimeoutGoesOfflineAndAlarmsDuringSession() {
        connect()
        start("s1")
        // Fresh link: 8 s without a PONG is still inside the grace window.
        val early = l.handle(Ev.Tick(clock + 8000))
        assertFalse(early.any { it is Act.CloseSockets })
        val acts = l.handle(Ev.Tick(clock + 9001))
        assertTrue(acts.any { it is Act.CloseSockets })
        assertTrue(acts.any { it is Act.StartAlarm })
        assertTrue(acts.any { it is Act.LogEvent && it.type == "connection_lost" })
        assertTrue(acts.any { it is Act.ScheduleReconnect })
        assertFalse(l.linkUp)
    }

    @Test
    fun linkLostWithoutSessionDoesNotAlarm() {
        connect()
        val acts = l.handle(Ev.LinkLost(null, clock))
        assertFalse(acts.any { it is Act.StartAlarm })
        assertFalse(acts.any { it is Act.LogEvent })
    }

    @Test
    fun reconnectRestoresAlarmAndLogsDuration() {
        connect()
        start("s1")
        l.handle(Ev.Tick(clock + 9001)) // drop at base+9001
        clock += 13_001 // link comes back 4 s later
        val acts = l.handle(Ev.LinkEstablished(clock))
        assertTrue(acts.any { it is Act.StopAlarm })
        assertTrue(acts.any { it is Act.LogEvent && it.type == "connection_restored" })
        assertEquals(4000L, l.lostDurationMs(clock))
    }

    // ── telemetry watchdog ──────────────────────────────────────────────────

    @Test
    fun telemetryStaleByAgeForcesReconnect() {
        connect()
        start("s1")
        l.onSessionPacketSent()
        // Backend keeps heartbeating (PONG arrives) but reports age >= 12 s.
        l.handle(Ev.Pong(PongPayload("RECORDING", "s1", null, 10, 12_000), clock + 1000))
        val acts = l.handle(Ev.Tick(clock + 2000))
        assertTrue(acts.any { it is Act.LogEvent && it.type == "telemetry_stale" })
        assertTrue(acts.any { it is Act.CloseSockets })
        assertTrue(acts.any { it is Act.ScheduleReconnect })
    }

    @Test
    fun telemetryStaleByCounterForcesReconnect() {
        connect()
        start("s1")
        l.onSessionPacketSent()
        // PONGs keep arriving but telemetry_packets never advances for 12 s.
        l.handle(Ev.Pong(PongPayload("RECORDING", "s1", null, 42, 100), clock + 1000))
        for (t in 2..12) {
            l.handle(Ev.Pong(PongPayload("RECORDING", "s1", null, 42, 100), clock + t * 1000L))
        }
        val acts = l.handle(Ev.Tick(clock + 13_500))
        assertTrue(acts.any { it is Act.LogEvent && it.type == "telemetry_stale" })
    }

    @Test
    fun advancingTelemetryCounterNeverGoesStale() {
        connect()
        start("s1")
        l.onSessionPacketSent()
        for (t in 1..30) {
            l.handle(Ev.Pong(PongPayload("RECORDING", "s1", null, (42 + t).toLong(), 100), clock + t * 1000L))
            val acts = l.handle(Ev.Tick(clock + t * 1000L + 1))
            assertFalse(acts.any { it is Act.LogEvent && it.type == "telemetry_stale" })
        }
    }

    // ── connect semantics ───────────────────────────────────────────────────

    @Test
    fun connectSameEndpointWhileUpIsNoOp() {
        connect()
        assertTrue(l.handle(Ev.Connect("192.168.1.5", "chest")).isEmpty())
    }

    @Test
    fun connectDifferentRoleReconnectsCleanly() {
        connect()
        val acts = l.handle(Ev.Connect("192.168.1.5", "waist"))
        assertEquals(listOf<Act>(Act.CloseSockets), acts.take(1))
        assertTrue(acts.any { it is Act.OpenSockets })
        assertTrue(acts.any { it is Act.PersistDesired && it.role == "waist" })
        assertEquals("waist", l.role)
    }

    @Test
    fun stopClosesRecorderClearsCheckpointAndUploads() {
        connect()
        start("s1")
        val acts = l.handle(Ev.Stop(null))
        assertTrue(acts.any { it is Act.CloseRecorder })
        assertTrue(acts.any { it is Act.ClearCheckpoint })
        assertTrue(acts.any { it is Act.StopAlarm })
        assertTrue(acts.any { it is Act.UploadRescue && it.sessionId == "s1" })
        assertNull(l.activeSessionId)
    }

    @Test
    fun startNewSessionWhileAnotherIsActiveSwitches() {
        connect()
        start("s1")
        repeat(10) { l.nextSessionSequence() }
        val acts = l.handle(Ev.Start("s2", null, null, null))
        assertEquals(0L, l.sequence)
        assertTrue(acts.any { it is Act.OpenRecorder && it.sessionId == "s2" })
        // Metadata omitted from the payload keeps the previous values.
        assertEquals("p01", l.subject)
    }

    @Test
    fun setLabelUpdatesRowMetadata() {
        connect()
        start("s1")
        l.handle(Ev.Label(4, "stairs"))
        assertEquals(4, l.labelId)
        assertEquals("stairs", l.labelName)
        // A null name falls back to the id string, like the old client.
        l.handle(Ev.Label(5, null))
        assertEquals("5", l.labelName)
    }

    @Test
    fun resetDeviceClearsEverythingAndStopsService() {
        connect()
        l.handle(Ev.LinkLost(4002, clock))
        val acts = l.handle(Ev.ResetDevice)
        assertTrue(acts.any { it is Act.ResetIdentity })
        assertTrue(acts.any { it is Act.ClearDesired })
        assertTrue(acts.any { it is Act.StopService })
        assertNull(l.desired)
        assertNull(l.error)
    }

    @Test
    fun restoreDesiredReconnectsOnRelaunch() {
        val acts = l.handle(Ev.RestoreDesired("10.0.0.2", "ankle_left"))
        assertEquals(Endpoint("10.0.0.2", "ankle_left"), l.desired)
        assertTrue(acts.any { it is Act.OpenSockets })
    }

    @Test
    fun recordingUnconfirmedAfterFifteenSecondsOfSilence() {
        connect()
        start("s1")
        pongRecording("s1") // sets lastStateAtMs; PONGs then stop arriving
        assertFalse(l.recordingUnconfirmed(clock + 14_000))
        assertTrue(l.recordingUnconfirmed(clock + 16_000))
    }
}
