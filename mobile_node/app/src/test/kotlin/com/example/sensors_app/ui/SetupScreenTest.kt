package com.example.sensors_app.ui

import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import com.example.sensors_app.core.Discovery
import com.example.sensors_app.core.StateSnapshot
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.GraphicsMode

/**
 * Setup screen driven by fake state and a fake discovery list — no NsdManager involved
 * (AppRoot owns discovery and passes the resolved results down).
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [34])
@GraphicsMode(GraphicsMode.Mode.NATIVE)
class SetupScreenTest {

    @get:Rule
    val compose = createComposeRule()

    private fun show(
        state: StateSnapshot = StateSnapshot(versionName = "3.0.0"),
        discovered: List<Discovery.Found> = emptyList(),
        onConnect: (String, String) -> Unit = { _, _ -> },
    ) {
        compose.setContent {
            SensorsTheme {
                SetupScreen(state = state, discovered = discovered, onConnect = onConnect)
            }
        }
    }

    @Test
    fun allEightRolesAreOffered() {
        show()
        listOf(
            "Chest", "Waist", "Left thigh", "Right thigh",
            "Left ankle", "Right ankle", "Left wrist", "Right wrist",
        ).forEach { label ->
            compose.onNodeWithTag("role-$label").assertExists()
        }
    }

    @Test
    fun connectIsDisabledWithoutAnIp() {
        show()
        compose.onNodeWithTag("connectButton").assertIsNotEnabled()
    }

    @Test
    fun lastIpIsPrefilled() {
        show(state = StateSnapshot(serverIp = "192.168.1.77", versionName = "3.0.0"))
        compose.onNodeWithTag("ipField").assertTextContains("192.168.1.77", substring = true)
        compose.onNodeWithTag("connectButton").assertIsEnabled()
    }

    @Test
    fun typedIpConnectsWithTheSelectedRole() {
        var got: Pair<String, String>? = null
        show(onConnect = { ip, role -> got = ip to role })
        // The Robolectric window is small; scrollTo before clicking or the tap lands off-screen.
        compose.onNodeWithTag("role-Left ankle").performScrollTo().performClick()
        compose.onNodeWithTag("ipField").performScrollTo().performTextInput("10.0.0.7")
        compose.onNodeWithTag("connectButton").performScrollTo().performClick()
        assertEquals("10.0.0.7" to "ankle_left", got)
    }

    @Test
    fun singleDiscoveryIsListedAndPreselected() {
        var got: Pair<String, String>? = null
        show(
            discovered = listOf(Discovery.Found("IMU Backend", "192.168.1.23", 8000)),
            onConnect = { ip, role -> got = ip to role },
        )
        compose.onNodeWithText("IMU Backend — 192.168.1.23").assertExists()
        // Preselected with no last IP → CONNECT is enabled and carries the resolved literal.
        compose.onNodeWithTag("connectButton").performScrollTo().performClick()
        assertTrue(got != null && got!!.first == "192.168.1.23")
    }

    @Test
    fun discoveryDoesNotClobberADifferentLastIp() {
        show(
            state = StateSnapshot(serverIp = "192.168.1.99", versionName = "3.0.0"),
            discovered = listOf(Discovery.Found("IMU Backend", "192.168.1.23", 8000)),
        )
        compose.onNodeWithTag("ipField").assertTextContains("192.168.1.99", substring = true)
    }
}
