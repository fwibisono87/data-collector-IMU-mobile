package id.ac.ui.cs.csl.fams.sapper.ui

import androidx.compose.ui.graphics.Color
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import id.ac.ui.cs.csl.fams.sapper.core.AlertError
import id.ac.ui.cs.csl.fams.sapper.core.Endpoint
import id.ac.ui.cs.csl.fams.sapper.core.LinkState
import id.ac.ui.cs.csl.fams.sapper.core.StateSnapshot
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * Status screen driven by fake [StateSnapshot]s (spec: "every state-block colour and the
 * SILENCE button"). The block colour is asserted through the custom [StatusBlockColor]
 * semantics key rather than pixels.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class StatusScreenTest {

    @get:Rule
    val compose = createComposeRule()

    private val base = StateSnapshot(
        deviceId = "abcdef12-3456-4abc-8def-0123456789ab",
        desired = Endpoint("192.168.1.5", "chest"),
        serverIp = "192.168.1.5",
        versionName = "3.0.0",
    )

    private fun show(
        state: StateSnapshot,
        onSilence: () -> Unit = {},
        onChangeRole: () -> Unit = {},
        onDisconnect: () -> Unit = {},
    ) {
        // The screen's 1 Hz "now" ticker never goes idle under auto-advance; the tests use
        // fixed snapshots, so freeze the clock — initial composition still runs.
        compose.mainClock.autoAdvance = false
        compose.setContent {
            SensorsTheme {
                StatusScreen(
                    state,
                    onSilence = onSilence,
                    onChangeRole = onChangeRole,
                    onDisconnect = onDisconnect,
                )
            }
        }
    }

    private fun assertBlock(color: Color, title: String) {
        compose.onNodeWithTag("statusBlock")
            .assert(SemanticsMatcher.expectValue(StatusBlockColor, color))
        compose.onNodeWithTag("statusTitle").assertTextContains(title)
    }

    @Test
    fun readyIsGrey() {
        show(base.copy(link = LinkState.CONNECTED))
        assertBlock(BlockReadyColor, "READY")
    }

    @Test
    fun recordingIsRedAndBlocksDisconnect() {
        show(
            base.copy(
                link = LinkState.CONNECTED,
                activeSessionId = "sess-12345678",
                sessionStartedAtMs = System.currentTimeMillis() - 65_000,
            ),
        )
        assertBlock(BlockRecordingColor, "RECORDING")
        compose.onNodeWithTag("disconnectButton").assertIsNotEnabled()
    }

    @Test
    fun offlineIsAmberWithElapsed() {
        show(
            base.copy(
                activeSessionId = "s",
                link = LinkState.OFFLINE,
                offlineSinceMs = System.currentTimeMillis() - 65_000,
            ),
        )
        assertBlock(BlockOfflineColor, "OFFLINE")
        // substring=true explicitly: this Compose version's assertTextContains default
        // matches the FULL text (verified empirically), unlike the documented default.
        compose.onNodeWithTag("statusSub").assertTextContains("01:0", substring = true)
    }

    @Test
    fun reconnectingIsAmber() {
        show(
            base.copy(
                activeSessionId = "s",
                link = LinkState.OFFLINE,
                reconnecting = true,
                offlineSinceMs = System.currentTimeMillis() - 5_000,
            ),
        )
        assertBlock(BlockOfflineColor, "RECONNECTING")
    }

    @Test
    fun errorIsPurpleWithChangeRole() {
        var changed = false
        show(
            base.copy(error = AlertError("ROLE_COLLISION", "Role Chest is already used by another phone")),
            onChangeRole = { changed = true },
        )
        assertBlock(BlockErrorColor, "ERROR")
        compose.onNodeWithTag("changeRoleButton").assertIsDisplayed().performClick()
        assertTrue(changed)
    }

    @Test
    fun silenceButtonAppearsOnlyWhileAlarming() {
        var silenced = false
        show(
            base.copy(
                activeSessionId = "s",
                link = LinkState.OFFLINE,
                offlineSinceMs = System.currentTimeMillis() - 2_000,
                alarmSounding = true,
            ),
            onSilence = { silenced = true },
        )
        compose.onNodeWithTag("silenceButton").assertIsDisplayed().performClick()
        assertTrue(silenced)
    }

    @Test
    fun noSilenceButtonWhenQuiet() {
        show(base.copy(link = LinkState.CONNECTED))
        compose.onNodeWithTag("silenceButton").assertDoesNotExist()
    }
}
