package com.example.sensors_app.core

/** Socket-level link state. CONNECTED means BOTH the control and the telemetry socket are up. */
enum class LinkState { DISCONNECTED, CONNECTING, CONNECTED, OFFLINE }

/**
 * The one big colour-coded block on the Status screen. The phone is strapped to a person and
 * glanced at from a metre away, so the whole state has to reduce to a single colour.
 */
enum class StatusBlock {
    /** Connected, no session running. */
    READY,

    /** Session active and streaming. */
    RECORDING,

    /** Session active (or an endpoint desired) but the link is down; data is buffered. */
    OFFLINE,

    /** A reconnect attempt is scheduled or in flight. */
    RECONNECTING,

    /** The backend refused us (role collision, invalid register). Needs an operator choice. */
    ERROR,
}

/** Where the phone should be talking to. Persisted, so a relaunch reconnects by itself. */
data class Endpoint(val ip: String, val role: String)

/** A sticky, operator-facing failure. Cleared only by an explicit connect or a device reset. */
data class AlertError(val code: String, val detail: String)

/** One local rescue CSV as listed by the Files sheet. */
data class LocalFileInfo(
    val name: String,
    val path: String,
    val bytes: Long,
    val uploaded: Boolean,
    val sessionId: String?,
    val modifiedMs: Long,
)

/** The default role, matching the old Flutter app, so a fresh install still registers. */
const val DEFAULT_ROLE = "chest"

/** The roles the Setup screen offers, in the order they are laid out (2 × 4 grid). */
val ROLES: List<Pair<String, String>> = listOf(
    "chest" to "Chest",
    "waist" to "Waist",
    "thigh_left" to "Left thigh",
    "thigh_right" to "Right thigh",
    "ankle_left" to "Left ankle",
    "ankle_right" to "Right ankle",
    "wrist_left" to "Left wrist",
    "wrist_right" to "Right wrist",
)

/**
 * Immutable snapshot of everything the UI is allowed to know. The service publishes it to
 * [com.example.sensors_app.Runtime.state] on change (conflated, at most 10/s); the UI renders
 * it and never touches a socket, a file or a sensor itself.
 */
data class StateSnapshot(
    // ── identity and endpoint ────────────────────────────────────────────────
    val deviceId: String = "",
    val role: String = DEFAULT_ROLE,
    /** Null while no endpoint is wanted; the Setup screen is shown exactly then. */
    val desired: Endpoint? = null,
    val serverIp: String = "",

    // ── link ────────────────────────────────────────────────────────────────
    val link: LinkState = LinkState.DISCONNECTED,
    /** True while a connect was asked for and has not finished (drives the inline spinner). */
    val connecting: Boolean = false,
    /** True while the backoff loop is about to retry. */
    val reconnecting: Boolean = false,
    /** Epoch ms the link went down; null while connected. Drives the OFFLINE mm:ss. */
    val offlineSinceMs: Long? = null,
    /** Plain-language connect failure for the Setup screen ("Laptop not reachable — …"). */
    val connectError: String? = null,
    /** Sticky failure that turns the Status block purple; only a connect or reset clears it. */
    val error: AlertError? = null,
    /** One-shot operator message (e.g. a refused disconnect); cleared by the next change. */
    val notice: String? = null,

    // ── session ─────────────────────────────────────────────────────────────
    val activeSessionId: String? = null,
    /** Authoritative backend state from the last PONG: IDLE, RECORDING, FINALIZING, … */
    val serverState: String? = null,
    val sessionStartedAtMs: Long? = null,
    val subject: String = "",
    val sessionTag: String = "",
    val labelId: Int = 0,
    val labelName: String = "0",

    // ── counters ────────────────────────────────────────────────────────────
    val packetsSent: Long = 0,
    val sessionPacketsSent: Long = 0,
    val packetsBuffered: Long = 0,
    val localRows: Long = 0,
    /** False when the rescue CSV could not be written — the one number that must never lie. */
    val localBackupOk: Boolean = true,
    val localError: String? = null,
    val uploadsFinished: Int = 0,

    // ── sensors and clock ───────────────────────────────────────────────────
    val accHz: Double = 0.0,
    val gyroHz: Double = 0.0,
    /** Accelerometer magnitude in g, for the single "is the sensor alive" bar. */
    val accMagnitude: Double = 0.0,
    val clockOffsetMs: Long = 0,
    val clockSynced: Boolean = false,
    val lastRttMs: Long = 0,

    // ── alarm ───────────────────────────────────────────────────────────────
    val alarmSounding: Boolean = false,
    val alarmSilenced: Boolean = false,

    // ── device ──────────────────────────────────────────────────────────────
    val batteryPercent: Int = -1,
    val versionName: String = "",
) {
    /**
     * The colour-coded block. Precedence: a sticky error beats everything (the operator has to
     * act), then "are we recording and is the link up", then the plain connected/idle case.
     */
    val block: StatusBlock
        get() = when {
            error != null -> StatusBlock.ERROR
            activeSessionId != null && link != LinkState.CONNECTED ->
                if (reconnecting || link == LinkState.CONNECTING) StatusBlock.RECONNECTING else StatusBlock.OFFLINE
            activeSessionId != null -> StatusBlock.RECORDING
            link == LinkState.CONNECTED -> StatusBlock.READY
            desired == null -> StatusBlock.READY
            reconnecting || link == LinkState.CONNECTING -> StatusBlock.RECONNECTING
            else -> StatusBlock.OFFLINE
        }

    /** True when the Status screen (rather than Setup) is the right thing to show. */
    val showStatus: Boolean get() = desired != null
}

/** `mm:ss` for the offline/reconnecting block and the notification. Locale.ROOT: these phones
 *  run an Indonesian locale and every formatted string in this app must stay ASCII. */
fun formatMmSs(durationMs: Long): String {
    val total = if (durationMs < 0) 0 else durationMs / 1000
    val m = total / 60
    val s = total % 60
    return String.format(java.util.Locale.ROOT, "%02d:%02d", m, s)
}

/** `h:mm:ss` (or `mm:ss` under an hour) for the elapsed time of a recording. */
fun formatElapsed(durationMs: Long): String {
    val total = if (durationMs < 0) 0 else durationMs / 1000
    val h = total / 3600
    val m = (total % 3600) / 60
    val s = total % 60
    return if (h > 0) {
        String.format(java.util.Locale.ROOT, "%d:%02d:%02d", h, m, s)
    } else {
        String.format(java.util.Locale.ROOT, "%02d:%02d", m, s)
    }
}
