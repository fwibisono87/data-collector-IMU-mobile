package com.example.sensors_app.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import java.io.File

/**
 * Crash-safe persistence of the recording state and the desired endpoint, so a restarted
 * process (START_STICKY, or MIUI killing the app mid-session, which it does routinely)
 * resumes WITHOUT waiting for the UI. Port of `lib/services/session_persistence.dart`.
 *
 * `session_state.json` keeps the old file name and keys byte-for-byte — a phone upgraded
 * mid-recording must resume the checkpoint the Flutter build wrote, and a downgrade must
 * still work. Writes are atomic: unique temp sibling + rename, so a crash mid-write cannot
 * corrupt the record.
 */
data class CheckpointData(
    val sessionId: String,
    val deviceId: String,
    val serverIp: String,
    val clockOffsetMs: Long,
    val lastSequenceNumber: Long,
    val deviceRole: String,
    val state: String,
    val savedAtMs: Long,
    /** Session metadata, so a restart reopens the SAME rescue CSV. Optional keys in the
     *  JSON: a checkpoint written by an older build simply loads them as empty. */
    val subject: String = "",
    val sessionTag: String = "",
    val operator: String = "",
)

class CheckpointStore(
    /** Candidate dirs for legacy reads; [0] is where the new app writes (= filesDir). */
    private val dirs: List<File>,
    private val nowMs: () -> Long = { System.currentTimeMillis() },
) {
    companion object {
        const val FILE_NAME = "session_state.json"
        const val DESIRED_FILE_NAME = "desired_state.json"
    }

    private val json = Json { encodeDefaults = true }
    private var tempCounter = 0

    private fun file(name: String): File = File(dirs[0], name)

    private fun atomicWrite(target: File, content: String) {
        try {
            target.parentFile?.mkdirs()
            val stamp = nowMs() * 1000
            val tmp = File(target.path + ".$stamp.${tempCounter++}.tmp")
            try {
                tmp.writeText(content)
                if (!tmp.renameTo(target)) {
                    target.delete()
                    if (!tmp.renameTo(target)) throw java.io.IOException("rename failed")
                }
            } finally {
                if (tmp.exists()) tmp.delete()
            }
        } catch (_: Exception) {
            // A checkpoint is recovery metadata, not a reason to stop acquisition.
        }
    }

    fun save(
        sessionId: String,
        deviceId: String,
        serverIp: String,
        clockOffsetMs: Long,
        lastSequenceNumber: Long,
        deviceRole: String,
        subject: String = "",
        sessionTag: String = "",
        operator: String = "",
    ) {
        val o = buildJsonObject {
            put("session_id", sessionId)
            put("device_id", deviceId)
            put("server_ip", serverIp)
            put("clock_offset_ms", clockOffsetMs)
            put("last_sequence_number", lastSequenceNumber)
            put("device_role", deviceRole)
            put("state", "RECORDING")
            put("saved_at_ms", nowMs())
            // New optional keys: SessionLogic is otherwise empty on restart and the rescue
            // file name (which includes subject + tag) would change, splitting one session
            // across two files that compete for the same server-side upload.
            put("subject", subject)
            put("session_tag", sessionTag)
            put("operator", operator)
        }
        atomicWrite(file(FILE_NAME), json.encodeToString(JsonObject.serializer(), o))
    }

    /** The persisted RECORDING checkpoint, from either legacy candidate dir, or null. */
    fun loadInterrupted(deviceId: String): CheckpointData? {
        val o = readFirst(FILE_NAME) ?: return null
        try {
            if ((o["state"] as? JsonPrimitive)?.content != "RECORDING") return null
            val data = CheckpointData(
                sessionId = (o["session_id"] as? JsonPrimitive)?.content ?: return null,
                deviceId = (o["device_id"] as? JsonPrimitive)?.content ?: return null,
                serverIp = (o["server_ip"] as? JsonPrimitive)?.content ?: "",
                clockOffsetMs = (o["clock_offset_ms"] as? JsonPrimitive)?.longOrNull ?: 0,
                lastSequenceNumber = (o["last_sequence_number"] as? JsonPrimitive)?.longOrNull
                    ?: return null,
                deviceRole = (o["device_role"] as? JsonPrimitive)?.content ?: DEFAULT_ROLE,
                state = "RECORDING",
                savedAtMs = (o["saved_at_ms"] as? JsonPrimitive)?.longOrNull ?: 0,
                // All three are optional: a checkpoint written before this change has no
                // such keys and must still load (resuming then falls back to filename
                // discovery in RescueRecorder.start).
                subject = (o["subject"] as? JsonPrimitive)?.content ?: "",
                sessionTag = (o["session_tag"] as? JsonPrimitive)?.content ?: "",
                operator = (o["operator"] as? JsonPrimitive)?.content ?: "",
            )
            // A checkpoint from a different phone identity (restored backup, shared image)
            // must never hijack this device's sequence namespace.
            if (data.deviceId != deviceId) return null
            return data
        } catch (_: Exception) {
            return null
        }
    }

    /** Marks the checkpoint IDLE (the old app rewrote the state in place; same here). */
    fun clear() {
        val o = readFirst(FILE_NAME) ?: return
        try {
            val updated = JsonObject(o.toMutableMap().apply { put("state", JsonPrimitive("IDLE")) })
            atomicWrite(file(FILE_NAME), json.encodeToString(JsonObject.serializer(), updated))
        } catch (_: Exception) {
        }
    }

    fun saveDesired(serverIp: String, deviceRole: String) {
        val o = buildJsonObject {
            put("server_ip", serverIp)
            put("device_role", deviceRole)
            put("saved_at_ms", nowMs())
        }
        atomicWrite(file(DESIRED_FILE_NAME), json.encodeToString(JsonObject.serializer(), o))
    }

    fun loadDesired(): Endpoint? {
        val o = readFirst(DESIRED_FILE_NAME) ?: return null
        val ip = (o["server_ip"] as? JsonPrimitive)?.content
        val role = (o["device_role"] as? JsonPrimitive)?.content
        if (ip.isNullOrEmpty()) return null
        return Endpoint(ip, role ?: DEFAULT_ROLE)
    }

    /** In-app recovery: remove the persisted endpoint so nothing auto-redials a stale IP. */
    fun clearDesired() {
        for (d in dirs) {
            try {
                val f = File(d, DESIRED_FILE_NAME)
                if (f.exists()) f.delete()
            } catch (_: Exception) {
            }
        }
    }

    private fun readFirst(name: String): JsonObject? {
        for (d in dirs) {
            try {
                val f = File(d, name)
                if (!f.exists()) continue
                return Json.parseToJsonElement(f.readText()) as? JsonObject
            } catch (_: Exception) {
                // try the next candidate
            }
        }
        return null
    }
}
