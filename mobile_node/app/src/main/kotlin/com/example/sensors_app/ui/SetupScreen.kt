package com.example.sensors_app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.sensors_app.core.Discovery
import com.example.sensors_app.core.ROLES
import com.example.sensors_app.core.StateSnapshot

/**
 * Role picker + backend picker + one CONNECT button. Shown exactly while no desired endpoint
 * exists. Pure function of ([state], [discovered]); the caller owns discovery lifetime.
 */
@Composable
fun SetupScreen(
    state: StateSnapshot,
    discovered: List<Discovery.Found>,
    onConnect: (ip: String, role: String) -> Unit,
) {
    var role by remember { mutableStateOf(state.role) }
    var ip by remember { mutableStateOf(state.serverIp) }
    var editedManually by remember { mutableStateOf(false) }

    // Preselect the found backend when there is exactly one AND it does not contradict the
    // last-used IP (spec). Never clobbers what the operator typed.
    LaunchedEffect(discovered, state.serverIp) {
        if (editedManually || discovered.size != 1) return@LaunchedEffect
        val host = discovered[0].host ?: return@LaunchedEffect
        if (state.serverIp.isEmpty() || state.serverIp == host) ip = host
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(24.dp),
    ) {
        Text("IMU Node", fontSize = 34.sp, fontWeight = FontWeight.Bold)
        Text(
            "v${state.versionName}",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(Modifier.height(28.dp))
        Text("Where is this phone?", fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(12.dp))

        // 2 × 4 grid of big targets; a phone strapped to an ankle is configured with gloves on.
        ROLES.chunked(2).forEach { pair ->
            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                pair.forEach { (id, label) ->
                    RoleButton(
                        label = label,
                        selected = id == role,
                        modifier = Modifier.weight(1f),
                    ) { role = id }
                }
            }
            Spacer(Modifier.height(12.dp))
        }

        Spacer(Modifier.height(16.dp))
        Text("Backend", fontSize = 22.sp, fontWeight = FontWeight.SemiBold)
        Spacer(Modifier.height(12.dp))
        if (discovered.isEmpty()) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                Spacer(Modifier.size(12.dp))
                Text(
                    "Searching for the backend…",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        } else {
            discovered.forEach { found ->
                val host = found.host
                val selected = host != null && host == ip
                Surface(
                    onClick = { if (host != null) ip = host },
                    color = if (selected) {
                        MaterialTheme.colorScheme.primaryContainer
                    } else {
                        MaterialTheme.colorScheme.surfaceVariant
                    },
                    shape = MaterialTheme.shapes.medium,
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(vertical = 4.dp)
                        .testTag("mdns-${found.name}"),
                ) {
                    Text(
                        if (host != null) "${found.name} — $host" else "${found.name} — resolving…",
                        fontSize = 20.sp,
                        modifier = Modifier.padding(16.dp),
                    )
                }
            }
        }

        Spacer(Modifier.height(16.dp))
        Text("Enter IP manually", fontSize = 16.sp)
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = ip,
            onValueChange = {
                ip = it
                editedManually = true
            },
            // Decimal, not Number: the number pad on Samsung keyboards has no "." and the
            // operator cannot type an IP at all (a real field failure of the old app).
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
            singleLine = true,
            placeholder = { Text("192.168.1.23") },
            modifier = Modifier
                .fillMaxWidth()
                .testTag("ipField"),
        )

        state.connectError?.let {
            Spacer(Modifier.height(12.dp))
            Text(it, color = MaterialTheme.colorScheme.error, fontSize = 16.sp)
        }

        Spacer(Modifier.height(24.dp))
        Button(
            onClick = { onConnect(ip.trim(), role) },
            enabled = ip.isNotBlank() && !state.connecting,
            modifier = Modifier
                .fillMaxWidth()
                .height(76.dp)
                .testTag("connectButton"),
        ) {
            if (state.connecting) {
                CircularProgressIndicator(
                    modifier = Modifier.size(28.dp),
                    strokeWidth = 3.dp,
                    color = MaterialTheme.colorScheme.onPrimary,
                )
            } else {
                Text("CONNECT", fontSize = 24.sp, fontWeight = FontWeight.Bold)
            }
        }
        Spacer(Modifier.height(24.dp))
    }
}

@Composable
private fun RoleButton(
    label: String,
    selected: Boolean,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Button(
        onClick = onClick,
        modifier = modifier
            .height(64.dp)
            .testTag("role-$label"),
        colors = if (selected) {
            ButtonDefaults.buttonColors()
        } else {
            ButtonDefaults.buttonColors(
                containerColor = MaterialTheme.colorScheme.surfaceVariant,
                contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        },
    ) {
        Text(
            label,
            fontSize = 17.sp,
            fontWeight = if (selected) FontWeight.Bold else FontWeight.Normal,
            maxLines = 1,
        )
    }
}
