package id.ac.ui.cs.csl.fams.sapper.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * `session_state.json` is read by a process that knows nothing about the one that wrote it.
 * It must round-trip the session metadata (so a restart reopens the same rescue CSV) and
 * keep loading checkpoints written by older builds, where those keys do not exist.
 */
class CheckpointTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private lateinit var dir: File
    private lateinit var store: CheckpointStore

    @Before
    fun setUp() {
        dir = tmp.newFolder("files")
        store = CheckpointStore(listOf(dir), nowMs = { 1_700_000_000_000L })
    }

    @Test
    fun sessionMetadataRoundTrips() {
        store.save(
            sessionId = "s1",
            deviceId = "dev-1",
            serverIp = "192.168.1.5",
            clockOffsetMs = 12,
            lastSequenceNumber = 7499,
            deviceRole = "chest",
            subject = "p01",
            sessionTag = "walk",
            operator = "francis",
        )

        val cp = store.loadInterrupted("dev-1")!!
        assertEquals("s1", cp.sessionId)
        assertEquals(7499L, cp.lastSequenceNumber)
        assertEquals("p01", cp.subject)
        assertEquals("walk", cp.sessionTag)
        assertEquals("francis", cp.operator)
    }

    @Test
    fun oldCheckpointWithoutMetadataStillLoads() {
        // Exactly the keys the previous build wrote; no subject/session_tag/operator.
        File(dir, CheckpointStore.FILE_NAME).writeText(
            """{"session_id":"s1","device_id":"dev-1","server_ip":"192.168.1.5",
               "clock_offset_ms":0,"last_sequence_number":250,"device_role":"chest",
               "state":"RECORDING","saved_at_ms":1700000000000}""",
        )

        val cp = store.loadInterrupted("dev-1")
        assertNotNull(cp)
        assertEquals(250L, cp!!.lastSequenceNumber)
        assertEquals("", cp.subject)
        assertEquals("", cp.sessionTag)
        assertEquals("", cp.operator)
    }
}