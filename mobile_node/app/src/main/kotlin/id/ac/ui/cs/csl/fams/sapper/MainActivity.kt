package id.ac.ui.cs.csl.fams.sapper

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import id.ac.ui.cs.csl.fams.sapper.ui.AppRoot
import id.ac.ui.cs.csl.fams.sapper.ui.SensorsTheme

/**
 * Hosts the Compose UI. [Runtime.preload] runs BEFORE setContent so the first frame already
 * shows the right screen (Setup vs Status) — "app relaunch lands on the right screen
 * immediately" is a spec'd fix, and a flash of Setup on every recording launch would scare
 * operators into tapping things.
 *
 * System back on the Status screen moves the task to the background instead of finishing:
 * the operator's instinct is "get this off my screen", not "stop the service".
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        Runtime.preload(applicationContext)
        super.onCreate(savedInstanceState)
        requestRuntimePermissions()
        setContent {
            SensorsTheme {
                val state by Runtime.state.collectAsStateWithLifecycle()
                BackHandler(enabled = state.showStatus) { moveTaskToBack(true) }
                AppRoot(state)
            }
        }
    }

    private fun requestRuntimePermissions() {
        val needed = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            !granted(Manifest.permission.POST_NOTIFICATIONS)
        ) {
            needed += Manifest.permission.POST_NOTIFICATIONS
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q &&
            !granted(Manifest.permission.ACTIVITY_RECOGNITION)
        ) {
            needed += Manifest.permission.ACTIVITY_RECOGNITION
        }
        if (needed.isNotEmpty()) {
            @Suppress("DEPRECATION") // Activity.requestPermissions is fine at minSdk 26
            requestPermissions(needed.toTypedArray(), 1001)
        }
    }

    private fun granted(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED
}
