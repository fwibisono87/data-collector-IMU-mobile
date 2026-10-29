package com.example.sensors_app.ui

import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.example.sensors_app.Runtime
import com.example.sensors_app.core.Discovery
import com.example.sensors_app.core.StateSnapshot

/**
 * The whole app is this switch: Status whenever a desired endpoint exists, Setup otherwise.
 * mDNS discovery lives here, not inside SetupScreen, so it is started exactly while Setup is
 * composed and stopped the moment the operator connects (or leaves) — the Setup screen itself
 * stays a pure function of (state, discovered) and is testable without NsdManager.
 */
@Composable
fun AppRoot(state: StateSnapshot) {
    if (state.showStatus) {
        StatusScreen(state)
    } else {
        val context = LocalContext.current
        var discovered by remember { mutableStateOf(emptyList<Discovery.Found>()) }
        val discovery = remember { Discovery(context) }
        DisposableEffect(Unit) {
            discovery.start { discovered = it }
            onDispose { discovery.stop() }
        }
        SetupScreen(
            state = state,
            discovered = discovered,
            onConnect = { ip, role -> Runtime.connect(ip, role) },
        )
    }
}
