package id.ac.ui.cs.csl.fams.sapper.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.io.RandomAccessFile
import java.util.concurrent.TimeUnit

/**
 * Resumable HTTP upload of finished rescue CSVs to the backend, so a session whose WebSocket
 * path was too flaky can still be pulled on the desktop with no adb/USB. Port of
 * `lib/services/recovery_uploader.dart` — the chunking, retries, timeouts, zero-byte final
 * request, 409 resume and completion markers are copied exactly, because the backend's
 * `/upload/csv` handler and the dedup merge expect precisely this behaviour.
 *
 * Runs on a single worker executor off the pipeline thread; a big upload must never block the
 * 100 Hz acquisition loop.
 */
class RecoveryUploader(
    private val client: OkHttpClient,
    /** Marker dirs, write target first. Legacy Flutter markers may sit in either candidate
     *  (`<filesDir>` or `<filesDir>/app_flutter`), so reads scan all of them — a missed
     *  marker only costs a redundant idempotent re-upload, never data. */
    private val markerDirs: List<File>,
    private val listSessions: () -> List<File>,
    private val isRecorderOpen: (File) -> Boolean,
    private val nowMs: () -> Long = { System.currentTimeMillis() },
) {
    companion object {
        const val CHUNK_BYTES = 256 * 1024
        const val MAX_RETRIES = 5
        const val CONNECT_TIMEOUT_S = 10L
        const val REQUEST_TIMEOUT_S = 45L
        private val OCTET_STREAM = "application/octet-stream".toMediaType()
        // Explicit ASCII classes, NOT \w: Android's ICU regex would make \w Unicode-aware,
        // while the Dart originals these were ported from used ASCII-only \w.
        private val META_PAIR = Regex("([A-Za-z0-9_]+)=([^,\\s]+)")
        private val MARKER_SANITIZE = Regex("[^A-Za-z0-9_-]")

        /** OkHttp client with the exact upload timeouts (a half-open Wi-Fi association at the
         *  edge of range would otherwise hang a request forever and never write the marker). */
        fun buildClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(CONNECT_TIMEOUT_S, TimeUnit.SECONDS)
            .readTimeout(REQUEST_TIMEOUT_S, TimeUnit.SECONDS)
            .writeTimeout(REQUEST_TIMEOUT_S, TimeUnit.SECONDS)
            .retryOnConnectionFailure(false)
            .build()
    }

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    @Volatile
    private var baseUrl = ""

    fun configure(serverIp: String) {
        baseUrl = "http://$serverIp:8000"
    }

    val isConfigured: Boolean get() = baseUrl.isNotEmpty()

    /** Uploads every finished, not-yet-uploaded session CSV. Returns how many completed. */
    fun uploadPending(onlySessionId: String? = null): Int {
        if (!isConfigured) return 0
        var uploaded = 0
        try {
            for (f in listSessions()) {
                try {
                    if (isRecorderOpen(f)) continue // still recording this session
                    val meta = parseMeta(f) ?: continue
                    if (onlySessionId != null && meta["session_id"] != onlySessionId) continue
                    val sid = meta["session_id"]!!
                    val dev = meta["device_id"]!!
                    if (isMarked(sid, dev, f)) continue
                    if (uploadOne(f, meta)) {
                        uploaded++
                        markDone(sid, dev, f, meta["sha256"] ?: "")
                    }
                } catch (_: Exception) {
                    // Keep going; a later reconnect retries this file.
                }
            }
        } catch (_: Exception) {
        }
        return uploaded
    }

    /** Parses the `#` metadata header line into a map; null when there is no usable session_id. */
    internal fun parseMeta(f: File): Map<String, String>? {
        try {
            RandomAccessFile(f, "r").use { raf ->
                val buf = mutableListOf<Byte>()
                while (buf.size < 8192) {
                    val b = raf.read()
                    if (b < 0) break
                    if (b == 0x0A) break
                    buf.add(b.toByte())
                }
                val first = String(buf.toByteArray(), Charsets.UTF_8)
                if (!first.startsWith("#")) return null
                val m = mutableMapOf<String, String>()
                for (match in META_PAIR.findAll(first)) {
                    m[match.groupValues[1]] = match.groupValues[2]
                }
                if (!m.containsKey("session_id")) return null
                m.putIfAbsent("role", "unknown")
                m.putIfAbsent("device_id", "unknown")
                return m
            }
        } catch (_: Exception) {
            return null
        }
    }

    private fun uploadOne(f: File, meta: Map<String, String>): Boolean {
        val sessionId = meta["session_id"]!!
        val deviceId = meta["device_id"]!!
        val total = f.length()
        if (total == 0L) return true // nothing to upload — treat as done
        val sha = sha256OfFile(f)

        val status = status(deviceId, sessionId) ?: return false
        var offset = status.receivedBytes
        if (offset < 0 || offset > total) return false
        if (offset == total) {
            if (status.complete && status.sha256Verified) return true
            // The server may have durably appended the final bytes before a Wi-Fi drop took the
            // response. Send an idempotent zero-byte final request so it can run the digest
            // check instead of leaving a permanently "receiving" file every reconnect skips.
            val resp = postChunk(deviceId, sessionId, meta, ByteArray(0), offset, total, last = true, sha = sha)
                ?: return false
            return resp.code == 200 && resp.complete && resp.sha256Verified
        }

        RandomAccessFile(f, "r").use { reader ->
            while (offset < total) {
                val end = minOf(offset + CHUNK_BYTES, total)
                val n = (end - offset).toInt()
                reader.seek(offset)
                val chunk = ByteArray(n)
                reader.readFully(chunk)
                val resp = postChunk(deviceId, sessionId, meta, chunk, offset, total, last = end >= total, sha = sha)
                    ?: return false
                if (resp.code == 409) {
                    val expected = resp.expectedOffset
                    // Backend is authoritative after an interrupted request. Resume where it
                    // says, but reject a nonsensical/non-progressing value to avoid a loop.
                    if (expected == null || expected < 0 || expected > total || expected == offset) {
                        return false
                    }
                    offset = expected
                    continue
                }
                if (resp.code != 200) return false
                val received = resp.receivedBytes
                if (received != end) return false
                offset = received
                if (end >= total) {
                    // Do not create the marker until the backend has both the complete byte
                    // count and a digest match.
                    return resp.complete && resp.sha256Verified
                }
            }
        }
        return false
    }

    private data class UploadResponse(
        val code: Int,
        val receivedBytes: Long,
        val complete: Boolean,
        val sha256Verified: Boolean,
        val expectedOffset: Long?,
    )

    private fun postChunk(
        deviceId: String,
        sessionId: String,
        meta: Map<String, String>,
        chunk: ByteArray,
        offset: Long,
        total: Long,
        last: Boolean,
        sha: String,
    ): UploadResponse? {
        val url = baseUrl + "/upload/csv" + query(
            "device_id" to deviceId,
            "session_id" to sessionId,
            "role" to (meta["role"] ?: ""),
            "subject" to (meta["subject"] ?: ""),
            "session_tag" to (meta["session_tag"] ?: ""),
            "operator" to (meta["operator"] ?: ""),
        )
        val body = chunk.toRequestBody(OCTET_STREAM, 0, chunk.size)
        val req = Request.Builder().url(url).post(body)
            .header("Content-Type", "application/octet-stream")
            .header("X-Offset", offset.toString())
            .header("X-Total", total.toString())
            .apply {
                if (last) {
                    header("X-Complete", "1")
                    header("X-Sha256", sha)
                }
            }
            .build()
        return try {
            client.newCall(req).execute().use { resp ->
                val text = resp.body?.string() ?: ""
                val o = try {
                    json.parseToJsonElement(text) as? JsonObject
                } catch (_: Exception) {
                    null
                }
                UploadResponse(
                    code = resp.code,
                    receivedBytes = (o?.get("received_bytes") as? JsonPrimitive)?.longOrNull ?: 0,
                    complete = (o?.get("complete") as? JsonPrimitive)?.booleanOrNull ?: false,
                    sha256Verified = (o?.get("sha256_verified") as? JsonPrimitive)?.booleanOrNull ?: false,
                    expectedOffset = (o?.get("expected_offset") as? JsonPrimitive)?.longOrNull,
                )
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun status(deviceId: String, sessionId: String): UploadResponse? {
        for (i in 0 until MAX_RETRIES) {
            try {
                val url = baseUrl + "/upload/status" + query(
                    "device_id" to deviceId,
                    "session_id" to sessionId,
                )
                val req = Request.Builder().url(url).get().build()
                client.newCall(req).execute().use { resp ->
                    if (resp.code == 200) {
                        val text = resp.body?.string() ?: ""
                        val o = json.parseToJsonElement(text) as? JsonObject
                        return UploadResponse(
                            code = 200,
                            receivedBytes = (o?.get("received_bytes") as? JsonPrimitive)?.longOrNull ?: 0,
                            complete = (o?.get("complete") as? JsonPrimitive)?.booleanOrNull ?: false,
                            sha256Verified = (o?.get("sha256_verified") as? JsonPrimitive)?.booleanOrNull ?: false,
                            expectedOffset = (o?.get("expected_offset") as? JsonPrimitive)?.longOrNull,
                        )
                    }
                }
            } catch (_: Exception) {
            }
            try {
                Thread.sleep(400L * (i + 1))
            } catch (_: InterruptedException) {
                return null
            }
        }
        return null
    }

    private fun query(vararg pairs: Pair<String, String>): String =
        pairs.joinToString(prefix = "?", separator = "&") { (k, v) ->
            "$k=${java.net.URLEncoder.encode(v, "UTF-8")}"
        }

    // ── local completion markers ────────────────────────────────────────────

    private fun markerFile(sessionId: String, deviceId: String): File {
        val sid = sessionId.replace(MARKER_SANITIZE, "_")
        val dev = deviceId.replace(MARKER_SANITIZE, "_")
        return File(markerDirs[0], "recovery_uploaded_${sid}_${dev}.json")
    }

    private fun isMarked(sessionId: String, deviceId: String, source: File): Boolean {
        try {
            val sid = sessionId.replace(MARKER_SANITIZE, "_")
            val dev = deviceId.replace(MARKER_SANITIZE, "_")
            val name = "recovery_uploaded_${sid}_${dev}.json"
            for (d in markerDirs) {
                val marker = File(d, name)
                if (!marker.exists()) continue
                val o = json.parseToJsonElement(marker.readText()) as? JsonObject ?: continue
                val mFile = (o["file"] as? JsonPrimitive)?.content
                val mBytes = (o["bytes"] as? JsonPrimitive)?.longOrNull
                if (mFile == source.path && mBytes == source.length()) return true
            }
            return false
        } catch (_: Exception) {
            return false
        }
    }

    private fun markDone(sessionId: String, deviceId: String, src: File, sha: String) {
        try {
            val o = buildJsonObject {
                put("session_id", sessionId)
                put("device_id", deviceId)
                put("file", src.path)
                put("bytes", src.length())
                put("sha256", sha)
                put("uploaded_at_ms", nowMs())
            }
            markerFile(sessionId, deviceId).writeText(json.encodeToString(JsonObject.serializer(), o))
        } catch (_: Exception) {
        }
    }
}
