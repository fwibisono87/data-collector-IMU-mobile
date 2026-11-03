package id.ac.ui.cs.csl.fams.sapper.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.File
import java.util.UUID

/**
 * Stable per-device identity: device_id, role and the last server IP.
 *
 * A phone's device_id is its name in 17 subjects' worth of backend data — it MUST survive the
 * Flutter→native upgrade. The old app kept `device_config.json` in path_provider's documents
 * dir, which depending on plugin version resolved to `<filesDir>` or `<filesDir>/app_flutter`;
 * both are checked. Before that it used SharedPreferences, whose Flutter file is
 * `shared_prefs/FlutterSharedPreferences.xml` with `flutter.`-prefixed keys; those are checked
 * too. Only if none has a device_id is a fresh UUIDv4 generated.
 *
 * The ROLE is migrated from the JSON file only (never from prefs): the old app learned the
 * hard way that silently reinstating a stale pref role caused role-collision rejects with no
 * in-app recovery.
 *
 * The new app writes `<filesDir>/device_identity.json` and never deletes the old files.
 */
data class Identity(
    val deviceId: String,
    val role: String,
    val lastServerIp: String,
)

object IdentityMigrator {
    const val FILE_NAME = "device_identity.json"

    /**
     * Pure migration decision, unit-testable without Android.
     *
     * @param legacyJsonFiles old `device_config.json` candidates, most authoritative first;
     *   unreadable/missing files are skipped.
     * @param legacyPrefs parsed `FlutterSharedPreferences` string values (keys WITHOUT the
     *   `flutter.` prefix, i.e. `device_id`, `last_server_ip`).
     */
    fun migrate(
        legacyJsonFiles: List<File>,
        legacyPrefs: Map<String, String>,
        freshId: () -> String = { UUID.randomUUID().toString() },
    ): Identity {
        var deviceId: String? = null
        var role: String? = null
        var lastIp: String? = null
        for (f in legacyJsonFiles) {
            val o = readJsonObject(f) ?: continue
            if (deviceId == null) deviceId = o.string("device_id")
            if (role == null) role = o.string("device_role")
            if (lastIp == null) lastIp = o.string("last_server_ip")
        }
        if (deviceId == null) deviceId = legacyPrefs["device_id"]?.ifEmpty { null }
        if (lastIp == null) lastIp = legacyPrefs["last_server_ip"]?.ifEmpty { null }
        return Identity(
            deviceId = deviceId ?: freshId(),
            role = role ?: DEFAULT_ROLE,
            lastServerIp = lastIp ?: "",
        )
    }

    private fun readJsonObject(f: File): JsonObject? =
        try {
            if (f.exists()) Json.parseToJsonElement(f.readText()) as? JsonObject else null
        } catch (_: Exception) {
            null
        }

    private fun JsonObject.string(key: String): String? {
        val v = this[key] as? JsonPrimitive ?: return null
        if (!v.isString) return null
        return v.content.ifEmpty { null }
    }
}

/** Read/write access to the new identity file, on the pipeline thread. */
class IdentityStore(private val dir: File) {
    private val json = Json { encodeDefaults = true }
    private val file: File get() = File(dir, IdentityMigrator.FILE_NAME)

    private var cached: Identity? = null

    /** Loads the identity, running the one-time migration from the Flutter stores if needed. */
    fun load(legacyJsonFiles: List<File>, legacyPrefs: Map<String, String>): Identity {
        cached?.let { return it }
        val existing = try {
            if (file.exists()) {
                val o = json.parseToJsonElement(file.readText()) as JsonObject
                Identity(
                    deviceId = (o["device_id"] as JsonPrimitive).content,
                    role = (o["device_role"] as? JsonPrimitive)?.content ?: DEFAULT_ROLE,
                    lastServerIp = (o["last_server_ip"] as? JsonPrimitive)?.content ?: "",
                )
            } else {
                null
            }
        } catch (_: Exception) {
            null
        }
        val id = existing ?: IdentityMigrator.migrate(legacyJsonFiles, legacyPrefs)
        cached = id
        persist(id)
        return id
    }

    fun update(transform: (Identity) -> Identity): Identity {
        val next = transform(cached ?: error("identity not loaded"))
        cached = next
        persist(next)
        return next
    }

    private fun persist(id: Identity) {
        try {
            dir.mkdirs()
            val o = buildJsonObject {
                put("device_id", id.deviceId)
                put("device_role", id.role)
                put("last_server_ip", id.lastServerIp)
            }
            val tmp = File(file.path + ".tmp")
            tmp.writeText(json.encodeToString(JsonObject.serializer(), o))
            if (!tmp.renameTo(file)) {
                file.delete()
                tmp.renameTo(file)
            }
        } catch (_: Exception) {
            // Identity is also cached in memory for this process; the next load retries.
        }
    }

    /**
     * In-app recovery for a phone stuck on a colliding role: delete the identity file. The
     * next load re-migrates from the legacy stores (which are never deleted), so the
     * device_id survives a reset as long as the old app wrote one — only truly fresh phones
     * get a new UUID.
     */
    fun reset() {
        cached = null
        try {
            if (file.exists()) file.delete()
        } catch (_: Exception) {
        }
    }
}

/**
 * Parses the legacy `FlutterSharedPreferences.xml` into plain string values with the
 * `flutter.` prefix stripped. Returns an empty map when the file does not exist — a phone
 * that never ran the old app has nothing to migrate.
 */
fun parseFlutterPrefs(xml: File): Map<String, String> {
    val out = mutableMapOf<String, String>()
    if (!xml.exists()) return out
    try {
        val text = xml.readText()
        // The file is Android's standard prefs XML: <string name="flutter.device_id">…</string>.
        // A regex is enough here and keeps this function free of Android imports for tests.
        val re = Regex("<string name=\"flutter\\.([^\"]+)\">([^<]*)</string>")
        for (m in re.findAll(text)) {
            out[m.groupValues[1]] = unescapeXml(m.groupValues[2])
        }
    } catch (_: Exception) {
    }
    return out
}

private fun unescapeXml(s: String): String = s
    .replace("&lt;", "<")
    .replace("&gt;", ">")
    .replace("&quot;", "\"")
    .replace("&apos;", "'")
    .replace("&#39;", "'")
    .replace("&amp;", "&")
