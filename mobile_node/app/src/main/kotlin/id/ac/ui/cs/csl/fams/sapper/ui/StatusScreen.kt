package id.ac.ui.cs.csl.fams.sapper.ui

import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.PowerManager
import android.provider.Settings
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import id.ac.ui.cs.csl.fams.sapper.Runtime
import id.ac.ui.cs.csl.fams.sapper.core.LinkState
import id.ac.ui.cs.csl.fams.sapper.core.StateSnapshot
import id.ac.ui.cs.csl.fams.sapper.core.formatElapsed
import id.ac.ui.cs.csl.fams.sapper.core.formatMmSs
import id.ac.ui.cs.csl.fams.sapper.roleLabel
import kotlinx.coroutines.delay
import java.util.Locale

/**
 * Shown whenever a desired endpoint exists. One full-width colour block first (glanceable
 * from a metre), then the giant SILENCE while the alarm sounds, then details, non-blocking
 * readiness warnings, one acc-magnitude bar (proof of life, no charts) and Disconnect behind
 * a confirm. Callbacks default to [Runtime] so tests can inject their own.
 */
@Composable
fun StatusScreen(
    state: StateSnapshot,
    onSilence: () -> Unit = { Runtime.silenceAlarm() },
    onChangeRole: () -> Unit = { Runtime.disconnect() },
    onDisconnect: () -> Unit = { Runtime.disconnect() },
) {
    val context = LocalContext.current
    var now by remember { mutableStateOf(System.currentTimeMillis()) }
    LaunchedEffect(Unit) {
        while (true) {
            delay(1000)
            now = System.currentTimeMillis()
        }
    }
    var showFiles by remember { mutableStateOf(false) }
    var confirmDisconnect by remember { mutableStateOf(false) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState()),
    ) {
        StatusBlock(state, now, onChangeRole)

        Column(Modifier.padding(20.dp)) {
            if (state.alarmSounding) {
                Button(
                    onClick = onSilence,
                    modifier = Modifier
                        .fillMaxWidth()
                        .height(120.dp)
                        .testTag("silenceButton"),
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.error,
                        contentColor = MaterialTheme.colorScheme.onError,
                    ),
                ) {
                    Text("SILENCE", fontSize = 34.sp, fontWeight = FontWeight.Bold)
                }
                Spacer(Modifier.height(16.dp))
            } else if (state.alarmSilenced) {
                Text(
                    "Alarm silenced — still offline",
                    color = BlockOfflineColor,
                    fontSize = 18.sp,
                )
                Spacer(Modifier.height(16.dp))
            }

            Details(state)
            AccelBar(state.accMagnitude)
            ReadinessWarnings(state, context)

            state.notice?.let {
                Spacer(Modifier.height(8.dp))
                Text(it, color = MaterialTheme.colorScheme.onSurfaceVariant, fontSize = 15.sp)
            }

            Spacer(Modifier.height(16.dp))
            TextButton(onClick = { showFiles = true }, modifier = Modifier.testTag("filesButton")) {
                Text("Local files (${state.uploadsFinished} uploaded)", fontSize = 16.sp)
            }

            Spacer(Modifier.height(8.dp))
            val recording = state.activeSessionId != null
            TextButton(
                onClick = { confirmDisconnect = true },
                enabled = !recording,
                modifier = Modifier.testTag("disconnectButton"),
            ) {
                Text("Disconnect", fontSize = 16.sp)
            }
            if (recording) {
                Text(
                    "Disabled while recording — only the dashboard can stop a session.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    fontSize = 13.sp,
                )
            }

            Spacer(Modifier.height(16.dp))
            Text(
                "v${state.versionName} · ${state.deviceId.take(8)}",
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                fontSize = 12.sp,
            )
            Spacer(Modifier.height(24.dp))
        }
    }

    if (showFiles) {
        FilesSheet(onDismiss = { showFiles = false })
    }
    if (confirmDisconnect) {
        AlertDialog(
            onDismissRequest = { confirmDisconnect = false },
            title = { Text("Disconnect?") },
            text = { Text("The phone stops streaming and will not reconnect until you connect again.") },
            confirmButton = {
                TextButton(
                    onClick = {
                        confirmDisconnect = false
                        onDisconnect()
                    },
                ) { Text("DISCONNECT") }
            },
            dismissButton = {
                TextButton(onClick = { confirmDisconnect = false }) { Text("Cancel") }
            },
        )
    }
}

/** The full-width colour block: grey READY, red RECORDING, amber OFFLINE/RECONNECTING, purple ERROR. */
@Composable
internal fun StatusBlock(state: StateSnapshot, now: Long, onChangeRole: () -> Unit) {
    val block = state.block
    val color = statusColor(block)
    val title = when (block) {
        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.READY -> "READY"
        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.RECORDING -> "RECORDING"
        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.OFFLINE -> "OFFLINE"
        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.RECONNECTING -> "RECONNECTING"
        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.ERROR -> "ERROR"
    }
    val sub: String? = when (block) {
        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.RECORDING ->
            formatElapsed(now - (state.sessionStartedAtMs ?: now))

        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.OFFLINE,
        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.RECONNECTING,
        -> formatMmSs(now - (state.offlineSinceMs ?: now))

        id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.ERROR ->
            state.error?.detail?.ifEmpty { state.error.code }
                ?: "${state.error?.code ?: "ERROR"} — backend refused this phone"

        else -> null
    }
    Surface(
        color = color,
        modifier = Modifier
            .fillMaxWidth()
            .testTag("statusBlock")
            .semantics { statusBlockColor = color },
    ) {
        Column(
            Modifier.padding(vertical = 28.dp, horizontal = 20.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                title,
                fontSize = 52.sp,
                fontWeight = FontWeight.Black,
                color = MaterialTheme.colorScheme.onSurface,
                modifier = Modifier.testTag("statusTitle"),
            )
            if (sub != null) {
                Text(
                    sub,
                    fontSize = 24.sp,
                    color = MaterialTheme.colorScheme.onSurface,
                    maxLines = 3,
                    modifier = Modifier.testTag("statusSub"),
                )
            }
            if (block == id.ac.ui.cs.csl.fams.sapper.core.StatusBlock.ERROR) {
                Spacer(Modifier.height(12.dp))
                Button(onClick = onChangeRole, modifier = Modifier.testTag("changeRoleButton")) {
                    Text("Change role", fontSize = 18.sp)
                }
            }
        }
    }
}

@Composable
private fun Details(state: StateSnapshot) {
    Text(roleLabel(state.role), fontSize = 36.sp, fontWeight = FontWeight.Bold)
    Spacer(Modifier.height(12.dp))
    val rows = buildList {
        if (state.activeSessionId != null) {
            add("Session" to state.activeSessionId!!.take(8))
            add("Subject" to state.subject.ifEmpty { "—" })
        }
        add("Label" to "${state.labelId} · ${state.labelName}")
        add("Sent" to "${state.sessionPacketsSent} this session · ${state.packetsSent} total")
        add("Buffered offline" to "${state.packetsBuffered}")
        add(
            "Local backup" to if (state.localBackupOk) {
                "${state.localRows} rows — OK"
            } else {
                "FAILED — ${state.localError ?: "write error"}"
            },
        )
        add("Sampling" to "acc ${hz(state.accHz)} Hz · gyro ${hz(state.gyroHz)} Hz")
        add(
            "Clock" to if (state.clockSynced) {
                "synced · offset ${state.clockOffsetMs} ms · rtt ${state.lastRttMs} ms"
            } else {
                "NOT SYNCED"
            },
        )
        state.serverState?.let { add("Backend state" to it) }
    }
    rows.forEach { (k, v) ->
        Row(Modifier.fillMaxWidth().padding(vertical = 3.dp)) {
            Text(
                k,
                fontSize = 15.sp,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.weight(0.42f),
            )
            Text(
                v,
                fontSize = 15.sp,
                color = if (v.startsWith("FAILED") || v == "NOT SYNCED") {
                    MaterialTheme.colorScheme.error
                } else {
                    MaterialTheme.colorScheme.onSurface
                },
                modifier = Modifier.weight(0.58f),
            )
        }
    }
}

/** The one allowed "chart": a bar proving the accelerometer is alive. */
@Composable
private fun AccelBar(magnitude: Double) {
    Spacer(Modifier.height(14.dp))
    Text(
        "Accel ${String.format(Locale.ROOT, "%.2f", magnitude)} g",
        fontSize = 13.sp,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
    // At rest the magnitude is ~1 g; 2 g full-scale makes a held phone visibly flatline.
    LinearProgressIndicator(
        progress = { (magnitude / 2.0).coerceIn(0.0, 1.0).toFloat() },
        modifier = Modifier.fillMaxWidth().height(10.dp).testTag("accelBar"),
    )
}

private data class Warning(val text: String, val fix: Intent? = null)

/**
 * Non-blocking readiness checklist: battery %, battery-optimisation exemption, notifications,
 * sampling ≥ 90 Hz, clock synced. "Fix" opens the relevant system screen where one exists.
 */
@Composable
private fun ReadinessWarnings(state: StateSnapshot, context: Context) {
    val pm = context.getSystemService(Context.POWER_SERVICE) as? PowerManager
    val nm = context.getSystemService(Context.NOTIFICATION_SERVICE) as? NotificationManager
    val exempt = pm?.isIgnoringBatteryOptimizations(context.packageName) ?: true
    val notificationsOn = nm?.areNotificationsEnabled() ?: true
    val warnings = buildList {
        if (state.batteryPercent in 0..19) {
            add(Warning("Battery low: ${state.batteryPercent}%"))
        }
        if (!exempt) {
            add(
                Warning(
                    "Battery optimisation can kill the service",
                    runCatching {
                        Intent(
                            Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                            Uri.parse("package:${context.packageName}"),
                        )
                    }.getOrNull(),
                ),
            )
        }
        if (!notificationsOn) {
            add(
                Warning(
                    "Notifications are off — you will not see the offline alarm",
                    runCatching {
                        Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
                    }.getOrNull(),
                ),
            )
        }
        if (state.link == LinkState.CONNECTED && state.accHz in 0.1..89.9) {
            add(Warning("Sampling only ${hz(state.accHz)} Hz (expected ~100)"))
        }
        if (state.link == LinkState.CONNECTED && !state.clockSynced) {
            add(Warning("Clock not synced yet"))
        }
    }
    if (warnings.isEmpty()) return
    Spacer(Modifier.height(14.dp))
    warnings.forEach { w ->
        Row(
            Modifier.fillMaxWidth().padding(vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                "⚠ ${w.text}",
                color = BlockOfflineColor,
                fontSize = 14.sp,
                modifier = Modifier.weight(1f),
            )
            w.fix?.let { intent ->
                TextButton(onClick = { runCatching { context.startActivity(intent) } }) {
                    Text("Fix")
                }
            }
        }
    }
}

private fun hz(v: Double): String = String.format(Locale.ROOT, "%.1f", v)
