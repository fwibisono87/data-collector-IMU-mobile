package com.example.sensors_app.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.sensors_app.Runtime
import com.example.sensors_app.core.LocalFileInfo

/**
 * Local rescue CSVs with size and uploaded/not-uploaded status. [load] defaults to
 * [Runtime.listLocalFiles] (callback returns on the main thread); tests inject a fake.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FilesSheet(
    onDismiss: () -> Unit,
    load: ((List<LocalFileInfo>) -> Unit) -> Unit = { cb -> Runtime.listLocalFiles(cb) },
) {
    var files by remember { mutableStateOf<List<LocalFileInfo>?>(null) }
    LaunchedEffect(Unit) { load { files = it } }
    ModalBottomSheet(onDismissRequest = onDismiss) {
        Column(Modifier.padding(horizontal = 20.dp)) {
            Text("Local rescue files", fontSize = 22.sp, fontWeight = FontWeight.Bold)
            Spacer(Modifier.height(12.dp))
            val list = files
            when {
                list == null -> Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.height(20.dp))
                    Spacer(Modifier.height(12.dp))
                    Text("Reading…")
                }

                list.isEmpty() -> Text(
                    "No rescue CSVs on this phone.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )

                else -> LazyColumn(Modifier.heightIn(max = 480.dp)) {
                    items(list, key = { it.path }) { f ->
                        Row(
                            Modifier.fillMaxWidth().padding(vertical = 8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column(Modifier.weight(1f)) {
                                Text(f.name, fontSize = 14.sp, maxLines = 2)
                                Text(
                                    buildString {
                                        append("${f.bytes / 1024} KB")
                                        f.sessionId?.let { append(" · session ${it.take(8)}") }
                                    },
                                    fontSize = 12.sp,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                            Text(
                                if (f.uploaded) "UPLOADED" else "NOT uploaded",
                                fontSize = 13.sp,
                                fontWeight = FontWeight.SemiBold,
                                color = if (f.uploaded) {
                                    MaterialTheme.colorScheme.primary
                                } else {
                                    BlockOfflineColor
                                },
                            )
                        }
                    }
                }
            }
            Spacer(Modifier.height(32.dp))
        }
    }
}
