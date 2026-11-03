package id.ac.ui.cs.csl.fams.sapper

import id.ac.ui.cs.csl.fams.sapper.core.LinkState
import id.ac.ui.cs.csl.fams.sapper.core.StateSnapshot
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The ongoing notification's four spec'd shapes. [notificationText] is a pure function of the
 * snapshot + now, so this runs on the plain JVM (no Robolectric) and pins the exact strings,
 * including the non-ASCII markers the old Dart app used.
 */
class NotificationTextTest {

    private val now = 1_700_000_000_000L

    @Test
    fun offlineDuringSessionShowsMmSsAndBufferCount() {
        val s = StateSnapshot(
            activeSessionId = "sid",
            link = LinkState.OFFLINE,
            offlineSinceMs = now - 65_000,
            packetsBuffered = 42,
        )
        assertEquals("⚠ OFFLINE 01:05 · 42 buffered locally", notificationText(s, now))
    }

    @Test
    fun recordingWhileConnectedShowsRoleLabelAndSent() {
        val s = StateSnapshot(
            activeSessionId = "sid",
            link = LinkState.CONNECTED,
            role = "thigh_left",
            sessionPacketsSent = 1234,
        )
        assertEquals("● REC Left thigh · 1234 sent", notificationText(s, now))
    }

    @Test
    fun idleConnectedShowsServerIp() {
        val s = StateSnapshot(link = LinkState.CONNECTED, serverIp = "192.168.1.5")
        assertEquals("Idle — connected to 192.168.1.5", notificationText(s, now))
    }

    @Test
    fun anythingElseIsReconnecting() {
        assertEquals("Reconnecting…", notificationText(StateSnapshot(serverIp = "x"), now))
        assertEquals("Reconnecting…", notificationText(StateSnapshot(link = LinkState.CONNECTING), now))
        // A session with no offline timestamp yet must not crash or render a negative clock.
        val s = StateSnapshot(activeSessionId = "sid", link = LinkState.CONNECTING, packetsBuffered = 0)
        assertEquals("⚠ OFFLINE 00:00 · 0 buffered locally", notificationText(s, now))
    }

    @Test
    fun roleLabelIsHumanAndFallsBackToTheId() {
        assertEquals("Chest", roleLabel("chest"))
        assertEquals("Left thigh", roleLabel("thigh_left"))
        assertEquals("Right wrist", roleLabel("wrist_right"))
        assertEquals("weird_role", roleLabel("weird_role"))
    }
}
