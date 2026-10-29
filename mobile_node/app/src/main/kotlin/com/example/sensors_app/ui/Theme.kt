package com.example.sensors_app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.SemanticsPropertyKey
import androidx.compose.ui.semantics.SemanticsPropertyReceiver
import com.example.sensors_app.core.StatusBlock

// ── state-block colours (the metre-away glance) ──────────────────────────────
// Also asserted by the Robolectric Status tests through the [StatusBlockColor] semantics key.

/** Grey: connected, idle. */
val BlockReadyColor = Color(0xFF3A3A42)

/** Red: recording. */
val BlockRecordingColor = Color(0xFFB3261E)

/** Amber: offline or reconnecting. */
val BlockOfflineColor = Color(0xFFB26A00)

/** Purple: the backend refused us; an operator choice is needed. */
val BlockErrorColor = Color(0xFF6750A4)

/** Custom semantics key so UI tests can assert the block colour without parsing pixels. */
val StatusBlockColor = SemanticsPropertyKey<Color>(name = "StatusBlockColor")

var SemanticsPropertyReceiver.statusBlockColor by StatusBlockColor

fun statusColor(block: StatusBlock): Color = when (block) {
    StatusBlock.READY -> BlockReadyColor
    StatusBlock.RECORDING -> BlockRecordingColor
    StatusBlock.OFFLINE, StatusBlock.RECONNECTING -> BlockOfflineColor
    StatusBlock.ERROR -> BlockErrorColor
}

/**
 * Dark-only Material 3. The app is read in dim rooms on a phone strapped to a person; the
 * window background in `colors.xml` matches [DarkColors.background] so a cold launch does not
 * flash white before the first frame.
 */
private val DarkColors = darkColorScheme(
    primary = Color(0xFFA8C7FA),
    onPrimary = Color(0xFF062E6F),
    primaryContainer = Color(0xFF0842A0),
    onPrimaryContainer = Color(0xFFD9E2FF),
    secondary = Color(0xFFC4C6DD),
    onSecondary = Color(0xFF2D3042),
    background = Color(0xFF101018),
    onBackground = Color(0xFFE2E1EC),
    surface = Color(0xFF1A1A24),
    onSurface = Color(0xFFE2E1EC),
    surfaceVariant = Color(0xFF2A2A36),
    onSurfaceVariant = Color(0xFFC6C5D0),
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005),
    outline = Color(0xFF5A5C6E),
)

@Composable
fun SensorsTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = DarkColors, content = content)
}
