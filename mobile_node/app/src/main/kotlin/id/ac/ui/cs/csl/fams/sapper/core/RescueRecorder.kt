package id.ac.ui.cs.csl.fams.sapper.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.File
import java.io.FileInputStream
import java.io.FileReader
import java.io.RandomAccessFile
import java.security.MessageDigest

/**
 * Writes every packet of a session to phone-local storage, from START to STOP, REGARDLESS of
 * network state. Port of `lib/services/local_session_recorder.dart`; the CSV bytes, the file
 * naming, the sidecar keys and the prune rule are a hard compatibility contract — 17 subjects'
 * data already exists in this shape and the backend re-verifies the sha256 on upload.
 *
 * This is the system's data guarantee. Telemetry, the fallback buffer, dedup and late delivery
 * are all network-dependent optimisations layered on top; this file is not.
 *
 * Threading: all methods are called on the single pipeline thread — unlike the Dart original,
 * which needed an I/O chain because its sinks were async. Writes go through a [BufferedWriter];
 * the pipeline calls [flush] once per second and [stop] fsyncs before writing integrity.json.
 *
 * Location: `getExternalFilesDir(null)/imu_sessions/<who>_rescue.csv` (+ sidecars), the same
 * directory the old app used, so files written before the upgrade are still listed and pruned.
 */
class RescueRecorder(
    private val dir: File,
    /** Directories scanned for `recovery_uploaded_*` markers; the prune rule trusts only those. */
    private val markerDirs: List<File>,
    private val nowMs: () -> Long = { System.currentTimeMillis() },
) {
    companion object {
        const val MAX_SESSIONS_KEPT = 20

        const val CSV_HEADER: String =
            "timestamp_ms,acc_x_g,acc_y_g,acc_z_g," +
                "gyro_x_degs,gyro_y_degs,gyro_z_degs," +
                "label_id,label_name,sequence_number,device_id," +
                "acc_ts_ms,gyro_ts_ms,sample_kind\n"

        // Explicit ASCII class, NOT \w: on Android java.util.regex is ICU-based and \w also
        // matches Unicode letters, while Dart's \w (the original sanitiser) was ASCII-only.
        private val sanitizePart = Regex("[^A-Za-z0-9_.-]+")
        private val sanitizeRuns = Regex("_+")

        /** Column index of `sequence_number`, derived from the frozen header. */
        private val SEQUENCE_INDEX: Int =
            CSV_HEADER.trimEnd('\n').split(',').indexOf("sequence_number")

        /** How many bytes of the tail [lastSequence] inspects: a few thousand 100 Hz rows. */
        private const val TAIL_BYTES = 64 * 1024

        /** `[^A-Za-z0-9_.-]+`→`_` then `_+`→`_`, exactly as the Dart original (`[^\w.-]+`
         *  with Dart's ASCII `\w`). */
        fun sanitize(s: String): String =
            s.replace(sanitizePart, "_").replace(sanitizeRuns, "_")

        /**
         * `<who>_rescue.csv` where who = [sessionId, subject?, tag?, role, deviceId], empty
         * subject/tag skipped, every part sanitised, joined with `_`. One deterministic path
         * per (session, phone): if the process is killed and restarted during RECORDING the
         * new process must append to the SAME file — a timestamped second file would make the
         * uploader skip a fragment or send a second header the resumable backend cannot merge.
         */
        fun csvFileName(
            sessionId: String,
            subject: String,
            sessionTag: String,
            role: String,
            deviceId: String,
        ): String {
            val who = buildList {
                add(sessionId)
                if (subject.isNotEmpty()) add(subject)
                if (sessionTag.isNotEmpty()) add(sessionTag)
                add(role)
                add(deviceId)
            }.joinToString("_") { sanitize(it) }
            return "${who}_rescue.csv"
        }
    }

    private val json = Json { encodeDefaults = true }

    private var writer: BufferedWriter? = null
    private var csvOut: java.io.FileOutputStream? = null
    private var events: BufferedWriter? = null
    private var eventsOut: java.io.FileOutputStream? = null
    private var file: File? = null
    private var sessionId: String? = null

    var rows: Long = 0
        private set
    var lastError: String? = null
        private set

    val isOpen: Boolean get() = writer != null
    val path: String? get() = file?.path

    /**
     * The rescue CSV already on disk for this (session, phone), if any.
     *
     * The filename also contains subject/tag, which are empty after a restart when the
     * checkpoint predates them (or when a session was adopted from a PONG). Matching on the
     * stable part only — `<sessionId>_` … `_<role>_<deviceId>_rescue.csv` — makes the
     * recorder append to the file it wrote before the kill instead of opening a second
     * fragment that competes for the same (session, device) upload.
     */
    fun findExisting(sessionId: String, role: String, deviceId: String): File? {
        val prefix = sanitize(sessionId) + "_"
        val suffix = "_${sanitize(role)}_${sanitize(deviceId)}_rescue.csv"
        return dir.listFiles()
            ?.filter { it.isFile && it.name.startsWith(prefix) && it.name.endsWith(suffix) }
            ?.maxWithOrNull(compareBy<File> { it.lastModified() }.thenBy { it.name })
    }

    /**
     * Highest `sequence_number` in the complete rows of [f]'s last [TAIL_BYTES], or null.
     *
     * A restart must number new samples ABOVE everything that may already be on the backend
     * and in this file, otherwise the backend's (device_id, sequence_number, first wins)
     * dedup discards them. Only the tail is read (a 40-minute CSV can be tens of MB) and an
     * unterminated final line is ignored: it is a torn write that [start] trims on reopen, so
     * its sequence number is not evidence that the row survived.
     */
    fun lastSequence(f: File): Long? {
        if (!f.isFile) return null
        try {
            val length = f.length()
            if (length <= 0L) return null
            val start = maxOf(0L, length - TAIL_BYTES)
            val bytes = ByteArray((length - start).toInt())
            RandomAccessFile(f, "r").use { raf ->
                raf.seek(start)
                raf.readFully(bytes)
            }
            var text = String(bytes, Charsets.UTF_8)
            if (start > 0L) {
                // The window begins mid-row; everything before the first newline is a fragment.
                val nl = text.indexOf('\n')
                if (nl < 0) return null
                text = text.substring(nl + 1)
            }
            // Keep only lines terminated by '\n'; the last element after split is either a
            // torn row or the empty string after a complete trailing newline.
            var highest: Long? = null
            for (line in text.split('\n').dropLast(1)) {
                if (line.isEmpty() || line.startsWith("#") || line.startsWith("timestamp_ms")) {
                    continue
                }
                val value = line.split(',').getOrNull(SEQUENCE_INDEX)?.toLongOrNull() ?: continue
                if (highest == null || value > highest!!) highest = value
            }
            return highest
        } catch (_: Exception) {
            return null
        }
    }

    /**
     * Opens (or reopens) the rescue CSV for [sessionId]. Idempotent while the same session is
     * already open. Reopening an existing file trims any partial last line, appends, writes no
     * second header, and recounts rows from disk so a resumed session reports honest totals in
     * integrity.json instead of only its final fragment.
     */
    fun start(
        sessionId: String,
        role: String,
        deviceId: String,
        subject: String,
        sessionTag: String = "",
        operator: String = "",
    ) {
        if (this.sessionId == sessionId && writer != null) return
        stop()
        try {
            dir.mkdirs()
            // Prefer the file already on disk for this session/phone: after a restart the
            // metadata may be missing, which changes csvFileName and would otherwise open a
            // second file with a second header for the same session.
            val csv = findExisting(sessionId, role, deviceId)
                ?: File(dir, csvFileName(sessionId, subject, sessionTag, role, deviceId))
            val exists = csv.exists() && csv.length() > 0
            if (exists) trimIncompleteLine(csv)
            val out = java.io.FileOutputStream(csv, exists)
            val w = BufferedWriter(java.io.OutputStreamWriter(out, Charsets.UTF_8))
            csvOut = out
            writer = w
            file = csv
            if (!exists) {
                w.write(
                    "# session_id=${sanitize(sessionId)},role=${sanitize(role)}," +
                        "device_id=${sanitize(deviceId)},subject=${sanitize(subject)}," +
                        "session_tag=${sanitize(sessionTag)},operator=${sanitize(operator)}," +
                        "start_epoch_ms=${nowMs()},source=local_node,schema_version=2\n",
                )
                w.write(CSV_HEADER)
            }
            // Flush the header to disk immediately: a crash before the first sample must
            // still leave a well-formed (metadata + header) file the uploader can parse.
            w.flush()
            val eventsFile = File(csv.path + ".events.jsonl")
            val eventsExists = eventsFile.exists() && eventsFile.length() > 0
            val eOut = java.io.FileOutputStream(eventsFile, eventsExists)
            eventsOut = eOut
            events = BufferedWriter(java.io.OutputStreamWriter(eOut, Charsets.UTF_8))
            this.sessionId = sessionId
            // Seed from what is already in the file, not zero: on a resumed session this
            // counter lands in integrity.json next to a sha256 over the WHOLE file, so
            // zeroing it made a resume look like data loss that never happened.
            rows = if (exists) countRows(csv) else 0
            lastError = null
            prune()
        } catch (e: Exception) {
            lastError = e.toString()
            writer = null
            csvOut = null
            events = null
            eventsOut = null
            file = null
            this.sessionId = null
        }
    }

    /** Appends one row. Never throws: local persistence must not take down the pipeline. */
    fun write(sample: Sample) {
        val w = writer ?: return
        try {
            w.write(sample.csvRow())
            rows++
        } catch (e: Exception) {
            // The telemetry/buffer path still carries this packet; the error is preserved
            // for the integrity sidecar and the UI's backup-OK flag.
            lastError = e.toString()
        }
    }

    /** Appends one JSON object to the events sidecar. Never touches the CSV. */
    fun logEvent(event: JsonObject) {
        val e = events ?: return
        try {
            e.write(json.encodeToString(JsonObject.serializer(), event))
            e.write("\n")
        } catch (_: Exception) {
            // Sidecar failures must not disturb the recording.
        }
    }

    /** Called once per second by the pipeline timer. */
    fun flush() {
        try {
            writer?.flush()
        } catch (e: Exception) {
            lastError = e.toString()
        }
        try {
            events?.flush()
        } catch (_: Exception) {
        }
    }

    /**
     * Closes the session file and writes `<csv>.integrity.json` with a sha256 over the whole
     * file. Returns the closed CSV, or null when nothing was open.
     */
    fun stop(): File? {
        val closed = file
        val closedSessionId = sessionId
        val w = writer
        val e = events
        val wOut = csvOut
        val eOut = eventsOut
        writer = null
        events = null
        csvOut = null
        eventsOut = null
        file = null
        sessionId = null
        try {
            e?.flush()
            eOut?.fd?.sync()
            e?.close()
        } catch (_: Exception) {
        }
        var closeError: String? = null
        try {
            w?.flush()
            // fsync so a phone whose battery dies right after STOP still has every row on flash.
            wOut?.fd?.sync()
            w?.close()
        } catch (ex: Exception) {
            closeError = ex.toString()
            lastError = closeError
        }
        if (closed != null && closed.exists()) {
            try {
                val digest = sha256OfFile(closed)
                val integrity = buildJsonObject {
                    put("session_id", closedSessionId)
                    put("rows", rows)
                    put("bytes", closed.length())
                    put("sha256", digest)
                    put("verified_at_ms", nowMs())
                    val err = closeError ?: lastError
                    if (err != null) put("close_error", err)
                }
                File(closed.path + ".integrity.json")
                    .writeText(json.encodeToString(JsonObject.serializer(), integrity))
            } catch (_: Exception) {
                // Integrity is a sidecar; losing it must not lose the session.
            }
        }
        return closed
    }

    /** All rescue CSVs, newest name first, sidecars excluded (same order as the Dart list). */
    fun listSessions(): List<File> {
        val files = dir.listFiles() ?: return emptyList()
        return files
            .filter { !it.name.endsWith(".events.jsonl") && !it.name.endsWith(".integrity.json") }
            .sortedByDescending { it.path }
    }

    /**
     * Keeps the [MAX_SESSIONS_KEPT] newest files and deletes older ones — but NEVER one whose
     * upload the backend has not confirmed with a completion marker. Starting a 21st session
     * must not reclaim data that was never delivered.
     */
    fun prune() {
        val files = listSessions()
        if (files.size <= MAX_SESSIONS_KEPT) return
        var kept = 0
        for (f in files) {
            if (kept < MAX_SESSIONS_KEPT) {
                kept++
                continue
            }
            if (!isUploaded(f)) continue
            try {
                File(f.path + ".events.jsonl").takeIf { it.exists() }?.delete()
                File(f.path + ".integrity.json").takeIf { it.exists() }?.delete()
                f.delete()
            } catch (_: Exception) {
            }
        }
    }

    /** True when a `recovery_uploaded_*` marker names this exact path AND byte count. */
    fun isUploaded(f: File): Boolean {
        try {
            val length = f.length()
            for (d in markerDirs) {
                val markers = d.listFiles() ?: continue
                for (m in markers) {
                    if (!m.name.contains("recovery_uploaded_")) continue
                    try {
                        val o = json.parseToJsonElement(m.readText()) as? JsonObject ?: continue
                        val markerFile = (o["file"] as? JsonPrimitive)?.content
                        val markerBytes = (o["bytes"] as? JsonPrimitive)?.longOrNull
                        if (markerFile == f.path && markerBytes == length) return true
                    } catch (_: Exception) {
                    }
                }
            }
        } catch (_: Exception) {
            // If we cannot prove it was uploaded, keep it.
        }
        return false
    }

    /** Counts data rows: empty lines, `#` metadata and the header do not count. */
    internal fun countRows(f: File): Long {
        var n = 0L
        try {
            BufferedReader(FileReader(f)).use { r ->
                while (true) {
                    val line = r.readLine() ?: break
                    if (line.isEmpty() || line.startsWith("#") || line.startsWith("timestamp_ms")) {
                        continue
                    }
                    n++
                }
            }
        } catch (_: Exception) {
        }
        return n
    }

    /**
     * A killed process can leave a partial final line (no trailing newline). Trim it on reopen
     * so the merged dataset has no malformed row. Searches backwards in bounded 64 KiB windows;
     * reading a full 40-minute CSV here once duplicated the whole file in the Dart heap.
     */
    internal fun trimIncompleteLine(f: File) {
        try {
            val length = f.length()
            RandomAccessFile(f, "r").use { raf ->
                var cursor = length
                var newline = -1L
                val buf = ByteArray(64 * 1024)
                while (cursor > 0 && newline < 0) {
                    val start = maxOf(0L, cursor - buf.size)
                    val n = (cursor - start).toInt()
                    raf.seek(start)
                    raf.readFully(buf, 0, n)
                    for (i in n - 1 downTo 0) {
                        if (buf[i] == 0x0A.toByte()) {
                            newline = start + i
                            break
                        }
                    }
                    cursor = start
                }
                if (newline >= 0 && newline + 1 < length) {
                    RandomAccessFile(f, "rw").use { w -> w.setLength(newline + 1) }
                }
            }
        } catch (_: Exception) {
            // Best effort only.
        }
    }
}

/** Streaming sha256, lowercase hex — never materialises a multi-hundred-MB CSV in memory. */
fun sha256OfFile(f: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    FileInputStream(f).use { input ->
        val buf = ByteArray(64 * 1024)
        while (true) {
            val n = input.read(buf)
            if (n < 0) break
            digest.update(buf, 0, n)
        }
    }
    val sb = StringBuilder(64)
    for (b in digest.digest()) sb.append(String.format(java.util.Locale.ROOT, "%02x", b))
    return sb.toString()
}
