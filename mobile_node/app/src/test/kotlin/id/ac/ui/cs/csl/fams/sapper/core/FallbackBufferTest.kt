package id.ac.ui.cs.csl.fams.sapper.core

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * The offline buffer format is shared with `tools/decode_fallback_buffer.py` and with
 * buffers already written by the Flutter app, so framing, rotation metadata and the
 * quarantine naming are pinned here.
 */
class FallbackBufferTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var dir: File
    private var clock = 1_700_000_000_000L
    private lateinit var buf: FallbackBuffer

    @Before
    fun setUp() {
        dir = tmp.newFolder("files")
        buf = FallbackBuffer(dir, nowMs = { clock }, nowMicros = { clock * 1000 })
    }

    private fun packet(seq: Long): ByteArray =
        SensorPacket(accX = 1.0f, sequenceNumber = seq, deviceId = "d", schemaVersion = 2).encode()

    @Test
    fun framingRoundTrip() {
        buf.activate("s1")
        buf.enqueue(packet(1))
        buf.enqueue(packet(2))
        assertEquals(2L, buf.bufferedCount)

        val snapshots = buf.snapshotForFlush()
        assertEquals(1, snapshots.size)
        val seen = mutableListOf<ByteArray>()
        buf.readRecords(snapshots) { seen.add(it); true }
        assertEquals(2, seen.size)
        assertEquals(1L, SensorPacket.decode(seen[0]).sequenceNumber)
        assertEquals(2L, SensorPacket.decode(seen[1]).sequenceNumber)
        assertEquals(0, buf.truncatedRecords)
        assertEquals(0, buf.malformedRecords)

        buf.clearAfterFlush()
        assertEquals(0L, buf.pendingOnDisk())
        assertTrue(snapshots.none { it.exists() })
    }

    @Test
    fun fileLayoutMatchesDecodeScript() {
        buf.activate("s1")
        buf.enqueue(packet(9))
        val f = File(dir, "fallback_buffer.bin")
        assertTrue(f.exists())
        val bytes = f.readBytes()
        val expected = packet(9)
        // 4-byte BE length prefix, then the proto bytes — nothing else.
        assertEquals(expected.size + 4, bytes.size)
        assertEquals(0, bytes[0].toInt())
        assertEquals(0, bytes[1].toInt())
        assertEquals((expected.size shr 8) and 0xFF, bytes[2].toInt() and 0xFF)
        assertEquals(expected.size and 0xFF, bytes[3].toInt() and 0xFF)
        assertArrayEquals(expected, bytes.copyOfRange(4, bytes.size))
        // Meta attributes the bytes to their session.
        val meta = File(dir, "fallback_buffer.meta.json").readText()
        assertTrue(meta.contains("\"session_id\":\"s1\""))
    }

    @Test
    fun truncatedTailIsReportedNotThrown() {
        buf.activate("s1")
        buf.enqueue(packet(1))
        val snapshots = buf.snapshotForFlush()
        // Chop the last byte off the snapshot: a killed process mid-write.
        val f = snapshots.single()
        f.writeBytes(f.readBytes().dropLast(1).toByteArray())
        val seen = mutableListOf<ByteArray>()
        buf.readRecords(snapshots) { seen.add(it); true }
        // The valid first record still parses; the truncated one is counted, not thrown.
        assertEquals(0, seen.size) // the single record IS the truncated one
        assertEquals(1, buf.truncatedRecords)
        assertEquals(0, buf.malformedRecords)
    }

    @Test
    fun validPrefixSurvivesTruncatedTail() {
        buf.activate("s1")
        buf.enqueue(packet(1))
        buf.enqueue(packet(2))
        val snapshots = buf.snapshotForFlush()
        val f = snapshots.single()
        f.writeBytes(f.readBytes().dropLast(3).toByteArray())
        val seen = mutableListOf<ByteArray>()
        buf.readRecords(snapshots) { seen.add(it); true }
        assertEquals(1, seen.size) // first record delivered
        assertEquals(1, buf.truncatedRecords)
    }

    @Test
    fun malformedLengthStopsParseOfThatFile() {
        val f = File(dir, "junk.bin")
        f.writeBytes(byteArrayOf(0x7F, 0xFF.toByte(), 0xFF.toByte(), 0xFF.toByte(), 1, 2)) // absurd length prefix
        buf.readRecords(listOf(f)) { true }
        assertEquals(1, buf.malformedRecords)
    }

    @Test
    fun quarantineRenamesAndPreservesBytes() {
        buf.activate("s1")
        buf.enqueue(packet(1))
        val before = File(dir, "fallback_buffer.bin").readBytes()
        val moved = buf.quarantine()
        assertEquals(1, moved.size)
        val orphan = File(moved[0])
        assertTrue(orphan.name.startsWith("orphan_s1_${clock}_"))
        assertTrue(orphan.name.endsWith(".bin"))
        assertArrayEquals(before, orphan.readBytes()) // never deleted, never truncated
        assertFalse(File(dir, "fallback_buffer.bin").exists())
        assertFalse(File(dir, "fallback_buffer.meta.json").exists())
        assertEquals(null, buf.sessionId)
        assertFalse(buf.isActive)
    }

    @Test
    fun activateForDifferentSessionQuarantinesFirst() {
        buf.activate("s1")
        buf.enqueue(packet(1))
        buf.deactivate()
        // Fresh process: a new session must not append into s1's stream (plan D13).
        val buf2 = FallbackBuffer(dir, nowMs = { clock + 1 }, nowMicros = { (clock + 1) * 1000 })
        buf2.activate("s2")
        buf2.enqueue(packet(2))
        val orphans = buf2.listOrphans()
        assertEquals(1, orphans.size)
        assertTrue(orphans[0].name.contains("orphan_s1_"))
        assertEquals("s2", buf2.sessionId)
        // Only s2's packet is in the live file.
        val seen = mutableListOf<ByteArray>()
        val snapshots = buf2.snapshotForFlush()
        buf2.readRecords(snapshots) { seen.add(it); true }
        assertEquals(1, seen.size)
        assertEquals(2L, SensorPacket.decode(seen[0]).sequenceNumber)
    }

    @Test
    fun orphanPruneKeepsTenNewest() {
        for (i in 1..12) {
            File(dir, "orphan_s_${1_700_000_000_000L + i}_1.bin").writeBytes(byteArrayOf(1))
        }
        buf.activate("s1")
        buf.quarantine() // triggers pruneOrphans
        val orphans = buf.listOrphans()
        assertEquals(10, orphans.size)
        // The two oldest stamps are gone.
        assertFalse(orphans.any { it.name.contains("_1700000000001_") })
        assertFalse(orphans.any { it.name.contains("_1700000000002_") })
    }

    @Test
    fun adoptLegacyMovesOldFlutterFiles() {
        val legacy = tmp.newFolder("app_flutter")
        File(legacy, "fallback_buffer.bin").writeBytes(byteArrayOf(1, 2, 3))
        File(legacy, "fallback_buffer.meta.json").writeText("""{"session_id":"old"}""")
        File(legacy, "orphan_x_1.bin").writeBytes(byteArrayOf(4))
        File(legacy, "unrelated.txt").writeText("keep me")

        buf.adoptLegacy(listOf(legacy))
        assertArrayEquals(byteArrayOf(1, 2, 3), File(dir, "fallback_buffer.bin").readBytes())
        assertTrue(File(dir, "fallback_buffer.meta.json").exists())
        assertTrue(File(dir, "orphan_x_1.bin").exists())
        // Non-buffer files stay put, and the legacy copies are untouched when already adopted.
        assertTrue(File(legacy, "unrelated.txt").exists())
        buf.loadMeta()
        assertEquals("old", buf.sessionId)
    }
}
