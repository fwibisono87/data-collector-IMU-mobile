package com.example.sensors_app.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.Locale

/**
 * The rescue CSV is the system's data guarantee and its format is a hard contract with the
 * backend uploader and 17 subjects of existing data. These tests pin the exact bytes.
 */
class RescueRecorderTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var dir: File
    private lateinit var markers: File
    private var clock = 1_700_000_000_000L
    private lateinit var rec: RescueRecorder
    private lateinit var savedLocale: Locale

    @Before
    fun setUp() {
        // The field phones run Indonesian; a formatter that leaks the default locale would
        // write decimal commas. Every test runs under that locale to prove ROOT is used.
        savedLocale = Locale.getDefault()
        Locale.setDefault(Locale("in", "ID"))
        dir = tmp.newFolder("imu_sessions")
        markers = tmp.newFolder("files")
        rec = RescueRecorder(dir, listOf(markers), nowMs = { clock })
    }

    @After
    fun tearDown() {
        Locale.setDefault(savedLocale)
    }

    private fun sample(
        seq: Long = 0,
        ax: Double = 0.123456789,
        ay: Double = -0.5,
        az: Double = 0.0,
        gx: Double = 12.0,
        gy: Double = 0.0,
        gz: Double = -123.456,
        accTs: Long = 0,
        gyroTs: Long = 0,
        kind: Int = 0,
        labelId: Int = 3,
        labelName: String = "walk",
    ) = Sample(
        accX = ax, accY = ay, accZ = az,
        gyroX = gx, gyroY = gy, gyroZ = gz,
        timestampMs = 1_700_000_001_000L + seq,
        rawTimestampMs = 1_700_000_001_000L + seq,
        sequence = seq,
        deviceId = "dev-1",
        accTsMs = accTs, gyroTsMs = gyroTs, sampleKind = kind,
        labelId = labelId, labelName = labelName,
    )

    // ── naming and header ───────────────────────────────────────────────────

    @Test
    fun fileNameSkipsEmptyPartsAndSanitises() {
        assertEquals(
            "s_2026_a_b_c_p01_walk_chest_dev-1_rescue.csv",
            RescueRecorder.csvFileName("s 2026/a b!! c", "p01", "walk", "chest", "dev-1"),
        )
        // Empty subject and tag are skipped entirely, not left as double underscores.
        assertEquals(
            "sid_chest_dev-1_rescue.csv",
            RescueRecorder.csvFileName("sid", "", "", "chest", "dev-1"),
        )
    }

    @Test
    fun sanitizeIsAsciiOnlyLikeTheDartOriginal() {
        // Android's java.util.regex is ICU-based, where \w also matches Unicode letters;
        // Dart's \w was ASCII-only. With the explicit class the rule is: each RUN of chars
        // outside [A-Za-z0-9_.-] becomes one "_", then "_" runs collapse.
        // "José Ñ-1": "é " is one run → "_", "Ñ" another → "_", giving "Jos__-1" → "Jos_-1".
        assertEquals("Jos_-1", RescueRecorder.sanitize("José Ñ-1"))
        assertEquals("a_b", RescueRecorder.sanitize("a  b"))
        assertEquals("keep.-_A1", RescueRecorder.sanitize("keep.-_A1"))
        assertEquals("_jalan_", RescueRecorder.sanitize("🚶jalan🚶"))
    }

    @Test
    fun metadataLineAndHeaderOnNewFile() {
        rec.start("s1", "chest", "dev-1", "p01", "walk", "francis")
        val lines = File(dir, "s1_p01_walk_chest_dev-1_rescue.csv").readText().split("\n")
        assertEquals(
            "# session_id=s1,role=chest,device_id=dev-1,subject=p01,session_tag=walk," +
                "operator=francis,start_epoch_ms=1700000000000,source=local_node,schema_version=2",
            lines[0],
        )
        assertEquals(
            "timestamp_ms,acc_x_g,acc_y_g,acc_z_g,gyro_x_degs,gyro_y_degs,gyro_z_degs," +
                "label_id,label_name,sequence_number,device_id,acc_ts_ms,gyro_ts_ms,sample_kind",
            lines[1],
        )
        assertEquals("", lines[2]) // trailing newline, nothing after it
    }

    // ── row formatting ──────────────────────────────────────────────────────

    @Test
    fun rowFormattingIsRootLocaleSixDecimals() {
        rec.start("s1", "chest", "dev-1", "", "", "")
        rec.write(sample(seq = 7, accTs = 1_700_000_000_999L, gyroTs = 1_700_000_000_998L))
        rec.flush()
        val row = File(dir, "s1_chest_dev-1_rescue.csv").readLines()[2]
        assertEquals(
            "1700000001007,0.123457,-0.500000,0.000000,12.000000,0.000000,-123.456000," +
                "3,walk,7,dev-1,1700000000999,1700000000998,0",
            row,
        )
    }

    @Test
    fun negativeZeroAndMissingHardwareTimestamps() {
        rec.start("s1", "chest", "dev-1", "", "", "")
        // -0.0 must keep its sign (Dart toStringAsFixed parity), and a 0 hardware timestamp
        // is an EMPTY field, not "0" — 0 means unknown.
        rec.write(sample(seq = 1, az = -0.0, gy = -0.0, accTs = 0, gyroTs = 5))
        rec.flush()
        val row = File(dir, "s1_chest_dev-1_rescue.csv").readLines()[2]
        assertEquals(
            "1700000001001,0.123457,-0.500000,-0.000000,12.000000,-0.000000,-123.456000," +
                "3,walk,1,dev-1,,5,0",
            row,
        )
    }

    // ── append and recount after a kill ─────────────────────────────────────

    @Test
    fun reopenTrimsPartialLineAppendsAndRecounts() {
        val f = File(dir, "s1_chest_dev-1_rescue.csv")
        rec.start("s1", "chest", "dev-1", "", "", "")
        rec.write(sample(seq = 0))
        rec.write(sample(seq = 1))
        rec.flush()
        rec.stop()

        // Simulate a process kill mid-write: a row without its trailing newline.
        val text = f.readText()
        f.writeText(text + "1700000001002,0.1,0.2,0.3,0.4,0.5,0.6,0,0,2,dev-1,,0,0")

        val rec2 = RescueRecorder(dir, listOf(markers), nowMs = { clock })
        rec2.start("s1", "chest", "dev-1", "", "", "")
        // The partial line is gone and no second metadata/header was written.
        val lines = f.readLines()
        assertEquals(4, lines.size) // meta + header + 2 complete rows
        assertTrue(lines[3].endsWith(",1,dev-1,,,0"))
        assertEquals(2L, rec2.rows) // recounted from disk, honest integrity totals
        rec2.write(sample(seq = 2))
        rec2.flush()
        assertEquals(5, f.readLines().size) // appended, no new header
        rec2.stop()
    }

    @Test
    fun startWithMissingMetadataAppendsToExistingSessionFile() {
        // The first process wrote the file with subject/tag in its name and flushed it.
        rec.start("s1", "chest", "dev-1", "p01", "walk", "francis")
        rec.write(sample(seq = 0))
        rec.write(sample(seq = 1))
        rec.flush()
        rec.stop()

        // A restart has not restored metadata: empty subject/tag would produce a different
        // csvFileName. The lookup must reopen the existing file instead.
        val rec2 = RescueRecorder(dir, listOf(markers), nowMs = { clock })
        rec2.start("s1", "chest", "dev-1", "", "", "")
        rec2.write(sample(seq = 2))
        rec2.flush()
        rec2.stop()

        val csvs = dir.listFiles()!!.filter { it.name.endsWith("_rescue.csv") }
        assertEquals(1, csvs.size)
        assertEquals("s1_p01_walk_chest_dev-1_rescue.csv", csvs.single().name)
        val lines = csvs.single().readLines()
        assertEquals(1, lines.count { it.startsWith("# session_id=") })
        assertEquals(1, lines.count { it.startsWith("timestamp_ms,") })
        assertEquals(5, lines.size) // metadata + header + three rows
    }

    @Test
    fun restartSimulationKeepsOneFileOneHeaderAndIncreasingSequences() {
        val checkpoints = CheckpointStore(listOf(dir), nowMs = { clock })
        val first = RescueRecorder(dir, listOf(markers), nowMs = { clock })
        first.start("s1", "chest", "dev-1", "p01", "walk", "francis")
        for (seq in 0L until 250L) first.write(sample(seq = seq))
        checkpoints.save(
            sessionId = "s1", deviceId = "dev-1", serverIp = "192.168.1.5",
            clockOffsetMs = 0, lastSequenceNumber = 250, deviceRole = "chest",
            subject = "p01", sessionTag = "walk", operator = "francis",
        )
        for (seq in 250L until 300L) first.write(sample(seq = seq))
        // "Kill": drop the recorder without stop()/flush().
        first.flush() // make the pre-kill file tail observable, as after any 1 s flush tick

        val cp = checkpoints.loadInterrupted("dev-1")!!
        // No CSV-tail reader in this item: the checkpoint interval is the safe upper bound.
        val resumeAt = maxOf(250L, cp.lastSequenceNumber) + SessionLogic.CHECKPOINT_EVERY + 1
        val second = RescueRecorder(dir, listOf(markers), nowMs = { clock })
        second.start(cp.sessionId, cp.deviceRole, "dev-1", cp.subject, cp.sessionTag, cp.operator)
        for (seq in resumeAt until resumeAt + 50L) second.write(sample(seq = seq))
        second.flush()
        second.stop()

        val csvs = dir.listFiles()!!.filter { it.name.endsWith("_rescue.csv") }
        assertEquals(1, csvs.size)
        val lines = csvs.single().readLines()
        assertEquals(1, lines.count { it.startsWith("# session_id=") })
        assertEquals(1, lines.count { it.startsWith("timestamp_ms,") })
        val seqCol = RescueRecorder.CSV_HEADER.trimEnd('\n').split(',').indexOf("sequence_number")
        val sequences = lines
            .filter { it.isNotEmpty() && !it.startsWith("#") && !it.startsWith("timestamp_ms") }
            .map { it.split(',')[seqCol].toLong() }
        assertEquals("sequence numbers must never repeat", sequences.size, sequences.toSet().size)
        assertEquals("sequence numbers must increase", sequences.sorted(), sequences)

        // The uploader's directory scan + metadata parse must still see exactly one file
        // for this session/device, not two competing fragments.
        val uploader = RecoveryUploader(
            RecoveryUploader.buildClient(),
            listOf(markers),
            { second.listSessions() },
            { false },
        )
        val forSession = second.listSessions().mapNotNull { f ->
            uploader.parseMeta(f)?.takeIf {
                it["session_id"] == "s1" && it["device_id"] == "dev-1"
            }?.let { f }
        }
        assertEquals(1, forSession.size)
        val meta = uploader.parseMeta(forSession.single())!!
        assertEquals("p01", meta["subject"])
        assertEquals("walk", meta["session_tag"])
        assertEquals("francis", meta["operator"])
    }

    @Test
    fun integritySidecarCarriesRowsBytesAndSha() {
        rec.start("s1", "chest", "dev-1", "", "", "")
        rec.write(sample(seq = 0))
        rec.write(sample(seq = 1))
        val closed = rec.stop()!!
        val integrity = File(closed.path + ".integrity.json")
        assertTrue(integrity.exists())
        val o = Json.parseToJsonElement(integrity.readText()) as JsonObject
        assertEquals("s1", (o["session_id"] as JsonPrimitive).content)
        assertEquals(2L, (o["rows"] as JsonPrimitive).longOrNull)
        assertEquals(closed.length(), (o["bytes"] as JsonPrimitive).longOrNull)
        assertEquals(64, (o["sha256"] as JsonPrimitive).content.length)
        assertEquals(sha256OfFile(closed), (o["sha256"] as JsonPrimitive).content)
        assertEquals(clock, (o["verified_at_ms"] as JsonPrimitive).longOrNull)
        assertFalse(o.containsKey("close_error"))
    }

    // ── events sidecar ──────────────────────────────────────────────────────

    @Test
    fun eventsSidecarAppendsJsonLines() {
        rec.start("s1", "chest", "dev-1", "", "", "")
        rec.logEvent(buildJsonObject { put("type", "service_start"); put("engine_id", "abc"); put("time_ms", clock) })
        rec.logEvent(buildJsonObject { put("type", "connection_lost"); put("time_ms", clock + 1) })
        rec.flush()
        val ev = File(dir, "s1_chest_dev-1_rescue.csv.events.jsonl").readLines()
        assertEquals(2, ev.size)
        assertTrue(ev[0].startsWith("""{"type":"service_start""""))
        assertTrue(ev[1].contains("connection_lost"))
        rec.stop()
    }

    // ── prune rule ──────────────────────────────────────────────────────────

    @Test
    fun pruneKeeps20AndNeverReclaimsUnuploaded() {
        // 22 finished CSVs, oldest name first is deleted order (list is name-desc).
        val names = (1..22).map { "s%02d_chest_dev-1_rescue.csv".format(it) }
        for (n in names) File(dir, n).writeText("x")
        // Mark ONLY the newest file as uploaded — prune must delete nothing else, because
        // the other 21 exceed the limit but are unconfirmed.
        val newest = names.last()
        File(markers, "recovery_uploaded_s22_dev-1.json").writeText(
            """{"session_id":"s22","device_id":"dev-1","file":"${File(dir, newest).path}",
               "bytes":1,"sha256":"…","uploaded_at_ms":1}""",
        )
        rec.prune()
        val remaining = dir.listFiles()!!.map { it.name }.sorted()
        // The 20 newest are kept no matter what; of the two oldest only... neither has a
        // marker (the marker is on a KEPT file), so nothing may be deleted at all.
        assertEquals(22, remaining.size)

        // Now mark the OLDEST file uploaded: exactly it disappears.
        File(markers, "recovery_uploaded_s01_dev-1.json").writeText(
            """{"session_id":"s01","device_id":"dev-1","file":"${File(dir, names.first()).path}",
               "bytes":1,"sha256":"…","uploaded_at_ms":1}""",
        )
        rec.prune()
        val after = dir.listFiles()!!.map { it.name }
        assertFalse(after.contains(names.first()))
        assertEquals(21, after.size)
    }
}
