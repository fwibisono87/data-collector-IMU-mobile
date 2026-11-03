package id.ac.ui.cs.csl.fams.sapper

import android.content.Context
import android.content.Intent
import android.os.Handler
import android.os.Looper
import androidx.core.content.ContextCompat
import id.ac.ui.cs.csl.fams.sapper.core.CheckpointStore
import id.ac.ui.cs.csl.fams.sapper.core.IdentityStore
import id.ac.ui.cs.csl.fams.sapper.core.LocalFileInfo
import id.ac.ui.cs.csl.fams.sapper.core.Pipeline
import id.ac.ui.cs.csl.fams.sapper.core.StateSnapshot
import id.ac.ui.cs.csl.fams.sapper.core.parseFlutterPrefs
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.File

/**
 * Process-wide bridge between the UI and the service-owned [Pipeline].
 *
 * The UI renders [state] and calls the command methods here — it never touches a socket, a
 * file or a sensor. The service may not exist yet when a command arrives (the UI starts it on
 * demand and Android creates it asynchronously), so commands are queued in arrival order and
 * replayed against the pipeline the moment it [attach]es; the pipeline's own handler then
 * orders them after its `init`.
 *
 * [preload] is the "relaunch lands on the right screen immediately" fix: MainActivity calls
 * it BEFORE the first frame and it synchronously reads `desired_state.json` and the identity
 * file, so the very first composition already knows Setup-vs-Status. The files are tiny; the
 * service rereads them properly during its own init.
 */
object Runtime {

    private val _state = MutableStateFlow(StateSnapshot(versionName = BuildConfig.VERSION_NAME))

    /** The one snapshot stream the UI collects. Published by the pipeline, conflated ≤ 10/s. */
    val state: StateFlow<StateSnapshot> = _state

    /** The pipeline writes here; internal so only the service can hand it out. */
    internal val mutableState: MutableStateFlow<StateSnapshot> get() = _state

    private val main = Handler(Looper.getMainLooper())
    private var appContext: Context? = null
    private var pipeline: Pipeline? = null
    private val pending = ArrayDeque<(Pipeline) -> Unit>()
    private var preloaded = false

    // ── lifecycle ────────────────────────────────────────────────────────────

    /** Synchronous, main-thread, before setContent. Idempotent. */
    fun preload(context: Context) {
        val app = context.applicationContext
        appContext = app
        if (preloaded) return
        preloaded = true
        try {
            val files = app.filesDir
            // Legacy Flutter documents-dir candidates, same list the Pipeline uses.
            val legacy = listOf(files, File(files, "app_flutter"))
            val desired = CheckpointStore(legacy).loadDesired()
            val identity = IdentityStore(files).load(
                legacy.map { File(it, "device_config.json") },
                parseFlutterPrefs(
                    File(files.parent ?: files.path, "shared_prefs/FlutterSharedPreferences.xml"),
                ),
            )
            _state.value = _state.value.copy(
                deviceId = identity.deviceId,
                role = desired?.role ?: identity.role,
                desired = desired,
                serverIp = desired?.ip ?: identity.lastServerIp,
            )
        } catch (_: Exception) {
            // Unreadable state files mean "fresh install" for the UI: defaults show Setup,
            // and the pipeline's own init will log/report anything deeper.
        }
    }

    /** Called by [ImuService] once its pipeline exists (main thread). */
    internal fun attach(p: Pipeline) {
        pipeline = p
        while (pending.isNotEmpty()) {
            pending.removeFirst()(p)
        }
    }

    /** Called by [ImuService.onDestroy]; a later attach replays commands again. */
    internal fun detach(p: Pipeline) {
        if (pipeline === p) pipeline = null
    }

    // ── commands (main thread) ───────────────────────────────────────────────

    /** Validates nothing itself — the pipeline owns the IP/role rules. Starts the service. */
    fun connect(ip: String, role: String) {
        ensureService()
        enqueue { it.connect(ip, role) }
    }

    fun disconnect() = enqueue { it.disconnect() }

    fun silenceAlarm() = enqueue { it.silenceAlarm() }

    fun resetDevice() = enqueue { it.resetDevice() }

    /** [cb] fires on the MAIN thread (the pipeline lists files on its own thread). */
    fun listLocalFiles(cb: (List<LocalFileInfo>) -> Unit) {
        enqueue { p -> p.listLocalFiles { files -> main.post { cb(files) } } }
    }

    private fun enqueue(action: (Pipeline) -> Unit) {
        pipeline?.let {
            action(it)
        } ?: pending.addLast(action)
    }

    private fun ensureService() {
        val ctx = appContext ?: return
        ContextCompat.startForegroundService(ctx, Intent(ctx, ImuService::class.java))
    }
}
