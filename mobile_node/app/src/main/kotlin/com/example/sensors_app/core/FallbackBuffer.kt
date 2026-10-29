package com.example.sensors_app.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import java.io.File
import java.io.RandomAccessFile

/**
 * Length-delimited protobuf buffer for network-offline periods. Port of
 * `lib/services/fallback_buffer_manager.dart`.
 *
 * On-disk format (must stay decodable by `tools/decode_fallback_buffer.py`):
 * `[4-byte big-endian length][SensorPacket proto bytes]`, repeated, appended to
 * `fallback_buffer.bin` (rotating to `.2.bin`, `.3.bin` past 500 MB). Session attribution
 * lives in `fallback_buffer.meta.json`.
 *
 * Never delete bytes that were not proven delivered: an unflushable buffer is renamed to
 * `orphan_<sid>_<stamp>_*.bin` and kept (10 newest). Deleting a buffer the backend never
 * accepted is how the old plan-D3 bug lost a whole session.
 *
 * Threading: everything runs on the pipeline thread; unlike the Dart original there is no
 * in-memory queue because the single-threaded pipeline already serialises writes, and
 * [RandomAccessFile] writes go straight to the kernel (no user-space buffer to flush).
 */
class FallbackBuffer(
    private val dir: File,
    private val nowMs: () -> Long = { System.currentTimeMillis() },
    private val nowMicros: () -> Long = { System.currentTimeMillis() * 1000 },
) {
    companion object {
        const val MAX_FILE_SIZE_BYTES = 500L * 1024 * 1024
        const val MAX_ROTATIONS = 3
        const val MAX_ORPHANS_KEPT = 10

        /** A record longer than this means the length prefix is corrupt, not a real packet. */
        const val MAX_RECORD_BYTES = 4 * 1024 * 1024
    }

    private val json = Json { encodeDefaults = true }
    private val metaFile: File get() = File(dir, "fallback_buffer.meta.json")

    private var raf: RandomAccessFile? = null
    private var active = false
    private var currentFileIndex = 1
    private val flushSnapshots = mutableListOf<File>()

    var sessionId: String? = null
        private set
    var bufferedCount: Long = 0
        private set
    var droppedOverflow: Int = 0
        private set
    var truncatedRecords: Int = 0
        private set
    var malformedRecords: Int = 0
        private set
    var lastError: String? = null
        private set

    val isActive: Boolean get() = active

    fun fileForIndex(index: Int): File {
        val suffix = if (index == 1) "" else ".$index"
        return File(dir, "fallback_buffer$suffix.bin")
    }

    /**
     * Restores [sessionId] (and the rotation index / snapshot list) from disk metadata, so a
     * buffer that survived process death is still attributable to its session before anything
     * decides to flush, quarantine or keep it.
     */
    fun loadMeta() {
        if (sessionId != null) return
        try {
            val f = metaFile
            if (!f.exists()) return
            val o = json.parseToJsonElement(f.readText()) as? JsonObject ?: return
            sessionId = (o["session_id"] as? JsonPrimitive)?.takeIf { it.isString }?.content
            val index = (o["current_file_index"] as? JsonPrimitive)?.intOrNull
            if (index != null && index in 1..MAX_ROTATIONS) currentFileIndex = index
            flushSnapshots.clear()
            flushSnapshots.addAll(
                (dir.listFiles() ?: emptyArray())
                    .filter { it.name.contains("fallback_buffer") && it.name.contains(".bin.flush.") }
                    .sortedBy { it.path },
            )
        } catch (_: Exception) {
        }
    }

    private fun writeMeta() {
        try {
            dir.mkdirs()
            val tmp = File(metaFile.path + ".tmp")
            val o = buildJsonObject {
                put("session_id", sessionId)
                put("current_file_index", currentFileIndex)
                put("started_at_ms", nowMs())
            }
            tmp.writeText(json.encodeToString(JsonObject.serializer(), o))
            if (!tmp.renameTo(metaFile)) {
                metaFile.delete()
                tmp.renameTo(metaFile)
            }
        } catch (_: Exception) {
        }
    }

    /**
     * Called before any write. If bytes from a DIFFERENT session are still on disk they are
     * quarantined first — appending to them would mix two sessions into one stream and the
     * flush would write session A's rows into session B's CSV (plan D13).
     */
    fun activate(sessionId: String?) {
        if (active) return
        bufferedCount = 0
        droppedOverflow = 0
        truncatedRecords = 0
        malformedRecords = 0
        lastError = null
        loadMeta()
        if (this.sessionId != null && this.sessionId != sessionId) quarantine()
        this.sessionId = sessionId
        // AFTER quarantine(): quarantine deactivates, and a cross-session activate must end
        // with a live writer or every subsequent enqueue is silently dropped.
        active = true
        openCurrentFile()
        writeMeta()
    }

    private fun openCurrentFile() {
        try {
            dir.mkdirs()
            val f = fileForIndex(currentFileIndex)
            raf = RandomAccessFile(f, "rw").also { it.seek(it.length()) }
            if (f.length() >= MAX_FILE_SIZE_BYTES) rotate()
        } catch (e: Exception) {
            lastError = e.toString()
        }
    }

    private fun rotate() {
        try {
            raf?.close()
            currentFileIndex++
            if (currentFileIndex > MAX_ROTATIONS) {
                // Wrap around and truncate the oldest — the ring is bounded by design.
                currentFileIndex = 1
                fileForIndex(1).writeBytes(ByteArray(0))
            }
            val f = fileForIndex(currentFileIndex)
            raf = RandomAccessFile(f, "rw").also { it.seek(it.length()) }
            writeMeta()
        } catch (e: Exception) {
            lastError = e.toString()
        }
    }

    /** Appends one packet. Never throws: a storage failure must not kill the pipeline. */
    fun enqueue(packetBytes: ByteArray) {
        val r = raf ?: return
        if (!active) return
        try {
            val frame = ByteArray(4 + packetBytes.size)
            frame[0] = ((packetBytes.size ushr 24) and 0xFF).toByte()
            frame[1] = ((packetBytes.size ushr 16) and 0xFF).toByte()
            frame[2] = ((packetBytes.size ushr 8) and 0xFF).toByte()
            frame[3] = (packetBytes.size and 0xFF).toByte()
            packetBytes.copyInto(frame, 4)
            r.write(frame)
            bufferedCount++
            if (r.length() >= MAX_FILE_SIZE_BYTES) rotate()
        } catch (e: Exception) {
            lastError = e.toString()
        }
    }

    /**
     * Total bytes left on disk from a previous process — including a buffer this run never
     * activated. The flush decision must react to a surviving buffer without depending on
     * in-memory state (plan T17).
     */
    fun pendingOnDisk(): Long {
        loadMeta()
        var total = 0L
        for (idx in 1..MAX_ROTATIONS) {
            val f = fileForIndex(idx)
            if (f.exists()) total += f.length()
        }
        for (s in flushSnapshots) if (s.exists()) total += s.length()
        return total
    }

    /**
     * Moves everything currently on disk into immutable `*.flush.<stamp>.<idx>` snapshots and
     * reopens a fresh live file, so packets arriving DURING a replay are not lost or replayed
     * twice. Returns the snapshot list (in replay order).
     */
    fun snapshotForFlush(): List<File> {
        try {
            raf?.close()
            raf = null
        } catch (_: Exception) {
        }
        bufferedCount = 0
        val stamp = nowMicros()
        for (idx in 1..MAX_ROTATIONS) {
            val f = fileForIndex(idx)
            if (!f.exists() || f.length() == 0L) continue
            val snapshot = File(f.path + ".flush.$stamp.$idx")
            if (f.renameTo(snapshot)) flushSnapshots.add(snapshot)
        }
        currentFileIndex = 1
        openCurrentFile()
        writeMeta()
        return flushSnapshots.toList()
    }

    /**
     * Reads every record from [files] in order, streaming (a rotation file is up to 500 MB and
     * must never be materialised in memory). A truncated tail or a corrupt length prefix is
     * COUNTED and ends that file's parse — reported, never thrown; the caller decides whether
     * the prefix it did receive was cleanly delivered.
     *
     * @return true when every file was scanned to the end; false when the [onRecord] callback
     *   vetoed (e.g. the telemetry socket died mid-replay) or an I/O error aborted the scan.
     *   Truncated/malformed tails do NOT make this false — they are counted instead, exactly
     *   like the Dart original, and the caller checks those counters for parse cleanliness.
     */
    fun readRecords(files: List<File>, onRecord: (ByteArray) -> Boolean): Boolean {
        for (f in files) {
            try {
                RandomAccessFile(f, "r").use { r ->
                    while (true) {
                        val prefix = ByteArray(4)
                        val got = readFully(r, prefix)
                        if (got == 0) break
                        if (got != 4) {
                            truncatedRecords++
                            break
                        }
                        val len = ((prefix[0].toInt() and 0xFF) shl 24) or
                            ((prefix[1].toInt() and 0xFF) shl 16) or
                            ((prefix[2].toInt() and 0xFF) shl 8) or
                            (prefix[3].toInt() and 0xFF)
                        if (len <= 0 || len > MAX_RECORD_BYTES) {
                            malformedRecords++
                            break
                        }
                        val payload = ByteArray(len)
                        if (readFully(r, payload) != len) {
                            truncatedRecords++
                            break
                        }
                        if (!onRecord(payload)) return false
                    }
                }
            } catch (e: Exception) {
                lastError = e.toString()
                return false
            }
        }
        return true
    }

    private fun readFully(r: RandomAccessFile, buf: ByteArray): Int {
        var off = 0
        while (off < buf.size) {
            val n = r.read(buf, off, buf.size - off)
            if (n < 0) break
            off += n
        }
        return off
    }

    /**
     * Deletes the snapshots after a CLEAN, complete replay. Kept separate from the replay so
     * an interrupted or malformed flush leaves the bytes alone for the next reconnect — backend
     * dedup on (device_id, session_id, sequence_number) makes a re-send idempotent.
     */
    fun clearAfterFlush() {
        for (s in flushSnapshots) {
            try {
                if (s.exists()) s.delete()
            } catch (_: Exception) {
            }
        }
        flushSnapshots.clear()
        writeMeta()
        droppedOverflow = 0
        truncatedRecords = 0
        malformedRecords = 0
        // This is a reconnect flush during an active recording: keep the live writer open for
        // the next network drop. Only STOP/quarantine deactivates it.
        if (sessionId == null) active = false
    }

    /** Closes the live file without touching the bytes on disk. */
    fun deactivate() {
        try {
            raf?.close()
        } catch (_: Exception) {
        }
        raf = null
        active = false
    }

    /**
     * Preserves bytes that could not be proven delivered: renames snapshots and live rotation
     * files to `orphan_<sid>_<stamp>_*.bin`, deletes the meta, prunes to the 10 newest orphans
     * (orphans are forensics — the rescue CSV is the analysis-ready copy of the same bytes).
     * Returns the paths moved.
     */
    fun quarantine(): List<String> {
        try {
            raf?.close()
        } catch (_: Exception) {
        }
        raf = null
        val stamp = nowMs()
        val sid = sessionId ?: "unknown"
        val moved = mutableListOf<String>()
        for (s in flushSnapshots) {
            if (!s.exists()) continue
            val target = File(dir, "orphan_${sid}_${stamp}_flush_${moved.size}.bin")
            if (s.renameTo(target)) moved.add(target.path)
        }
        flushSnapshots.clear()
        for (idx in 1..MAX_ROTATIONS) {
            val f = fileForIndex(idx)
            if (!f.exists() || f.length() == 0L) continue
            val target = File(dir, "orphan_${sid}_${stamp}_$idx.bin")
            if (f.renameTo(target)) moved.add(target.path)
        }
        try {
            if (metaFile.exists()) metaFile.delete()
        } catch (_: Exception) {
        }
        bufferedCount = 0
        droppedOverflow = 0
        currentFileIndex = 1
        active = false
        sessionId = null
        pruneOrphans()
        return moved
    }

    fun listOrphans(): List<File> =
        (dir.listFiles() ?: emptyArray()).filter { it.name.contains("orphan_") }

    private fun pruneOrphans() {
        // Filenames are epoch-stamped, so a reverse path sort is newest-first.
        val orphans = listOrphans().sortedByDescending { it.path }
        for (f in orphans.drop(MAX_ORPHANS_KEPT)) {
            try {
                f.delete()
            } catch (_: Exception) {
            }
        }
    }

    /**
     * One-time upgrade path: the old Flutter app kept its buffer in path_provider's documents
     * dir, which depending on plugin version was `<filesDir>` or `<filesDir>/app_flutter`.
     * Move any survivors into [dir] so their bytes stay attributable and quarantinable.
     * Never deletes the source it could not move; a same-directory call is a no-op.
     */
    fun adoptLegacy(legacyDirs: List<File>) {
        dir.mkdirs()
        for (legacy in legacyDirs) {
            if (legacy.path == dir.path) continue
            val files = legacy.listFiles() ?: continue
            for (f in files) {
                val name = f.name
                val wanted = name.startsWith("fallback_buffer") || name.startsWith("orphan_")
                if (!wanted || !f.isFile) continue
                val target = File(dir, name)
                if (target.exists()) continue
                try {
                    f.renameTo(target)
                } catch (_: Exception) {
                }
            }
        }
    }
}
