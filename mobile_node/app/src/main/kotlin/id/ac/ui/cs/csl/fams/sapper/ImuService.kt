package id.ac.ui.cs.csl.fams.sapper

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import androidx.core.app.ServiceCompat
import id.ac.ui.cs.csl.fams.sapper.core.LinkState
import id.ac.ui.cs.csl.fams.sapper.core.Pipeline
import id.ac.ui.cs.csl.fams.sapper.core.ROLES
import id.ac.ui.cs.csl.fams.sapper.core.StateSnapshot
import id.ac.ui.cs.csl.fams.sapper.core.formatMmSs
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * The one foreground service. Owns the [Pipeline] for the lifetime of the process, holds a
 * partial WakeLock and a high-performance WifiLock so acquisition survives the screen going
 * off, and renders the ongoing notification from [Runtime.state].
 *
 * START_STICKY: MIUI kills background processes routinely; the pipeline resumes from the
 * checkpoint on restart. The notification is re-rendered at most 1/s (a 1 Hz ticker with
 * change detection — the snapshot alone does not tick, so `mm:ss` counters would freeze).
 */
class ImuService : Service() {

    companion object {
        const val CHANNEL_ID = "imu_status"
        const val NOTIFICATION_ID = 4242
        private const val RENDER_INTERVAL_MS = 1000L
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private var pipeline: Pipeline? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null

    // Change detection so an unchanged state never re-posts (≤ 1 update/s is a hard rule).
    private var lastText: String? = null
    private var lastSilence = false

    override fun onCreate() {
        super.onCreate()
        createChannel()
        acquireLocks()
        val initial = Runtime.state.value
        val now = System.currentTimeMillis()
        lastText = notificationText(initial, now)
        lastSilence = initial.alarmSounding
        if (!startForegroundCompat(buildNotification(initial, now))) return
        val p = Pipeline(applicationContext, Runtime.mutableState) { stopSelf() }
        pipeline = p
        // start() BEFORE attach(): attach replays queued UI commands (the first Connect) onto
        // the pipeline thread, and they need the identity that start()'s init loads. In the
        // other order the first Connect ran before init and crashed ("identity not loaded").
        p.start()
        Runtime.attach(p)
        scope.launch {
            while (isActive) {
                delay(RENDER_INTERVAL_MS)
                render(Runtime.state.value)
            }
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        scope.cancel()
        pipeline?.let {
            Runtime.detach(it)
            it.shutdown()
        }
        pipeline = null
        releaseLocks()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onTaskRemoved(rootIntent: Intent?) {
        // stopWithTask=false already keeps the service alive when the app is swiped away;
        // do NOT stopSelf here or a recording would die with the Recents entry.
        super.onTaskRemoved(rootIntent)
    }

    // ── notification ─────────────────────────────────────────────────────────

    private fun render(s: StateSnapshot) {
        val now = System.currentTimeMillis()
        val text = notificationText(s, now)
        if (text == lastText && s.alarmSounding == lastSilence) return
        lastText = text
        lastSilence = s.alarmSounding
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, buildNotification(s, now))
    }

    private fun createChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.notification_channel_name),
            // HIGH so the OFFLINE/alarm state is unmissable; sound and vibration stay OFF on
            // the channel — the Alarm (MediaPlayer + Vibrator) owns audio, and onlyAlertOnce
            // stops the per-second re-posts from pinging.
            NotificationManager.IMPORTANCE_HIGH,
        ).apply {
            description = getString(R.string.notification_channel_description)
            setSound(null, null)
            enableVibration(false)
        }
        getSystemService(NotificationManager::class.java).createNotificationChannel(channel)
    }

    private fun buildNotification(s: StateSnapshot, now: Long): Notification {
        val tap = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val builder = Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.notification_title))
            .setContentText(notificationText(s, now))
            .setContentIntent(tap)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .setShowWhen(false)
        if (s.alarmSounding) {
            val silence = PendingIntent.getBroadcast(
                this,
                1,
                Intent(this, AlarmActionReceiver::class.java),
                PendingIntent.FLAG_IMMUTABLE,
            )
            builder.addAction(
                Notification.Action.Builder(
                    null,
                    getString(R.string.notification_action_silence),
                    silence,
                ).build(),
            )
        }
        return builder.build()
    }

    private fun startForegroundCompat(n: Notification): Boolean {
        return try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ServiceCompat.startForeground(
                    this,
                    NOTIFICATION_ID,
                    n,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_HEALTH,
                )
            } else {
                startForeground(NOTIFICATION_ID, n)
            }
            true
        } catch (_: Exception) {
            // Android 14+ refuses the health type unless ACTIVITY_RECOGNITION (or a Health
            // Connect permission) is granted; MainActivity requests it, but a denial must
            // degrade instead of crash-looping under START_STICKY. The two-arg retry helps
            // on API 29–33, where the type prerequisites are not enforced.
            val ok = try {
                startForeground(NOTIFICATION_ID, n)
                true
            } catch (_: Exception) {
                false
            }
            if (!ok) stopSelf()
            ok
        }
    }

    // ── locks ────────────────────────────────────────────────────────────────

    private fun acquireLocks() {
        try {
            val pm = getSystemService(PowerManager::class.java)
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "sapper:pipeline")
                .also {
                    it.setReferenceCounted(false)
                    it.acquire()
                }
        } catch (_: Exception) {
            // Without the lock the CPU may doze between sensor batches; acquisition still runs.
        }
        try {
            val wifi = getSystemService(WifiManager::class.java)
            @Suppress("DEPRECATION") // HIGH_PERF is the mode that keeps Wi-Fi up while dozing
            wifiLock = wifi.createWifiLock(WifiManager.WIFI_MODE_FULL_HIGH_PERF, "sapper:link")
                .also {
                    it.setReferenceCounted(false)
                    it.acquire()
                }
        } catch (_: Exception) {
            // Same story: reconnect backoff covers Wi-Fi naps.
        }
    }

    private fun releaseLocks() {
        runCatching { wakeLock?.takeIf { it.isHeld }?.release() }
        runCatching { wifiLock?.takeIf { it.isHeld }?.release() }
        wakeLock = null
        wifiLock = null
    }
}

/** Human label for a role id ("thigh_left" → "Left thigh"), used by notification and UI. */
internal fun roleLabel(role: String): String =
    ROLES.firstOrNull { it.first == role }?.second ?: role

/**
 * The ongoing notification's text. Pure so it is JVM-testable; spec'd shapes:
 *  - session + link down → `⚠ OFFLINE <mm:ss> · <n> buffered locally`
 *  - session + link up   → `● REC <role label> · <n> sent`
 *  - idle + link up      → `Idle — connected to <ip>`
 *  - otherwise           → `Reconnecting…`
 */
internal fun notificationText(s: StateSnapshot, nowMs: Long): String = when {
    s.activeSessionId != null && s.link != LinkState.CONNECTED ->
        "⚠ OFFLINE ${formatMmSs(nowMs - (s.offlineSinceMs ?: nowMs))} · " +
            "${s.packetsBuffered} buffered locally"

    s.activeSessionId != null ->
        "● REC ${roleLabel(s.role)} · ${s.sessionPacketsSent} sent"

    s.link == LinkState.CONNECTED ->
        "Idle — connected to ${s.serverIp.ifEmpty { "server" }}"

    else -> "Reconnecting…"
}
