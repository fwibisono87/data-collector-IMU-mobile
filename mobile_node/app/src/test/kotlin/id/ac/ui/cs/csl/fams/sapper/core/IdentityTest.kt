package id.ac.ui.cs.csl.fams.sapper.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * A phone's device_id is its name in 17 subjects' worth of data. It must survive the
 * Flutter→native upgrade from either of the two places the old app could have stored it.
 */
class IdentityTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private var generated = 0

    private val freshId: () -> String = { generated++; "fresh-uuid-$generated" }

    @Test
    fun readsOldFlutterDeviceConfigJson() {
        val legacy = tmp.newFolder("app_flutter")
        File(legacy, "device_config.json").writeText(
            """{"device_id":"abc-123","device_role":"waist","last_server_ip":"192.168.1.5"}""",
        )
        val id = IdentityMigrator.migrate(listOf(File(legacy, "device_config.json")), emptyMap(), freshId)
        assertEquals("abc-123", id.deviceId)
        assertEquals("waist", id.role)
        assertEquals("192.168.1.5", id.lastServerIp)
        assertEquals(0, generated)
    }

    @Test
    fun fallsBackToLegacySharedPreferences() {
        // No JSON anywhere; the pre-file-based build kept these in FlutterSharedPreferences.
        val id = IdentityMigrator.migrate(
            listOf(File(tmp.root, "missing/device_config.json")),
            mapOf("device_id" to "prefs-id", "last_server_ip" to "10.0.0.9"),
            freshId,
        )
        assertEquals("prefs-id", id.deviceId)
        assertEquals("10.0.0.9", id.lastServerIp)
        assertEquals("role is never taken from prefs", DEFAULT_ROLE, id.role)
    }

    @Test
    fun jsonWinsOverPrefsAndFirstJsonWins() {
        val d1 = tmp.newFolder("files")
        val d2 = tmp.newFolder("app_flutter")
        File(d1, "device_config.json").writeText("""{"device_id":"first","device_role":"ankle_left"}""")
        File(d2, "device_config.json").writeText("""{"device_id":"second","last_server_ip":"1.2.3.4"}""")
        val id = IdentityMigrator.migrate(
            listOf(File(d1, "device_config.json"), File(d2, "device_config.json")),
            mapOf("device_id" to "prefs-id"),
            freshId,
        )
        assertEquals("first", id.deviceId)
        assertEquals("ankle_left", id.role)
        // A field missing from the first file is filled from the second.
        assertEquals("1.2.3.4", id.lastServerIp)
    }

    @Test
    fun freshPhoneGetsUuidAndDefaultRole() {
        val id = IdentityMigrator.migrate(emptyList(), emptyMap(), freshId)
        assertEquals("fresh-uuid-1", id.deviceId)
        assertEquals(DEFAULT_ROLE, id.role)
        assertEquals("", id.lastServerIp)
    }

    @Test
    fun realUuidShapeWhenNoGeneratorGiven() {
        val id = IdentityMigrator.migrate(emptyList(), emptyMap())
        assertTrue(id.deviceId.matches(Regex("[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")))
    }

    @Test
    fun corruptLegacyJsonIsSkippedNotFatal() {
        val legacy = tmp.newFolder("app_flutter")
        File(legacy, "device_config.json").writeText("{not json at all")
        val id = IdentityMigrator.migrate(listOf(File(legacy, "device_config.json")), emptyMap(), freshId)
        assertEquals("fresh-uuid-1", id.deviceId)
    }

    @Test
    fun parsesFlutterSharedPreferencesXml() {
        val xml = tmp.newFile("FlutterSharedPreferences.xml")
        xml.writeText(
            """<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
            <map>
              <string name="flutter.device_id">old-id</string>
              <string name="flutter.last_server_ip">192.168.0.7</string>
              <boolean name="flutter.some_flag" value="true" />
            </map>""",
        )
        val prefs = parseFlutterPrefs(xml)
        assertEquals("old-id", prefs["device_id"])
        assertEquals("192.168.0.7", prefs["last_server_ip"])
        assertEquals(2, prefs.size)
        assertEquals(emptyMap<String, String>(), parseFlutterPrefs(File(tmp.root, "nope.xml")))
    }

    @Test
    fun storeRoundTripsAndResetRemigrates() {
        val dir = tmp.newFolder("files")
        val legacy = File(dir, "app_flutter").apply { mkdirs() }
        File(legacy, "device_config.json").writeText("""{"device_id":"keep-me"}""")

        val store = IdentityStore(dir)
        val first = store.load(listOf(File(legacy, "device_config.json")), emptyMap())
        assertEquals("keep-me", first.deviceId)
        assertTrue(File(dir, IdentityMigrator.FILE_NAME).exists())

        store.update { it.copy(role = "thigh_right", lastServerIp = "192.168.1.7") }
        val store2 = IdentityStore(dir) // fresh process
        val reloaded = store2.load(listOf(File(legacy, "device_config.json")), emptyMap())
        assertEquals("keep-me", reloaded.deviceId)
        assertEquals("thigh_right", reloaded.role)
        assertEquals("192.168.1.7", reloaded.lastServerIp)

        // Reset deletes only the NEW file; the legacy id is re-migrated, not regenerated.
        store2.reset()
        val store3 = IdentityStore(dir)
        val afterReset = store3.load(listOf(File(legacy, "device_config.json")), emptyMap())
        assertEquals("keep-me", afterReset.deviceId)
        // Old files are never deleted by the new app.
        assertTrue(File(legacy, "device_config.json").exists())
    }
}
