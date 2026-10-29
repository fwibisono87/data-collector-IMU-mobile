package com.example.sensors_app.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/**
 * The wire format is a hard compatibility contract: 17 subjects' data already exists in these
 * bytes and the backend's Python decoder must accept what this app sends. The fixed vectors
 * below come from PHONE.md; `proto_vectors.json` was produced by `tools/gen_proto_vectors.py`
 * using `master_backend.proto` itself, so a Kotlin/Python encoder divergence fails here.
 */
class ProtoTest {

    private fun hex(s: String): ByteArray {
        val out = ByteArray(s.length / 2)
        for (i in out.indices) out[i] = ((s[2 * i].digitToInt(16) shl 4) or s[2 * i + 1].digitToInt(16)).toByte()
        return out
    }

    private fun toHex(b: ByteArray): String = b.joinToString("") { "%02x".format(it) }

    // ── fixed vectors from PHONE.md ─────────────────────────────────────────

    @Test
    fun commandPingWithIssuedAtOne() {
        // type=PING is the enum default 0 → omitted; only issued_at_ms remains.
        assertEquals("1801", toHex(Command(type = CommandType.PING, issuedAtMs = 1).encode()))
    }

    @Test
    fun commandPongWithEmptyObjectPayload() {
        val c = Command(type = CommandType.PONG, payload = "{}")
        assertEquals("080112027b7d", toHex(c.encode()))
    }

    @Test
    fun minimalSensorPacketIsOnlyAccX() {
        assertEquals("0d0000803f", toHex(SensorPacket(accX = 1.0f).encode()))
    }

    // ── round-trips ─────────────────────────────────────────────────────────

    @Test
    fun sensorPacketRoundTripAllFields() {
        val p = SensorPacket(
            accX = 0.123f, accY = -1.0f, accZ = 9.8f,
            gyroX = 12.5f, gyroY = -0.001f, gyroZ = 359.9f,
            timestampMs = 1761800000123L,
            sequenceNumber = IDLE_SEQ_BASE + 7,
            deviceId = "dev-é☃",
            schemaVersion = 2,
            rawTimestampMs = 1761799999999L,
            accTsMs = 1761800000100L,
            gyroTsMs = 1761800000101L,
            sampleKind = 1,
        )
        val decoded = SensorPacket.decode(p.encode())
        assertEquals(p, decoded)
    }

    @Test
    fun commandRoundTrip() {
        val c = Command(
            type = CommandType.START_SESSION,
            payload = """{"session_id":"s1","subject":"p01"}""",
            issuedAtMs = 1761800000123L,
            commandId = "b1e2c3d4-0000-4000-8000-000000000001",
        )
        assertEquals(c, Command.decode(c.encode()))
    }

    @Test
    fun deviceRegisterRoundTrip() {
        val m = DeviceRegister(
            deviceId = "d1", deviceRole = "ankle_left", deviceModel = "2510DRA23E",
            androidVersion = "15", appVersion = "3.0.0", schemaVersion = 1,
        )
        assertEquals(m, DeviceRegister.decode(m.encode()))
    }

    @Test
    fun negativeInt64IsCanonicalTenByteVarint() {
        // -1 as an int64 varint is ten 0xff bytes plus a final 0x01, the two's complement
        // form every protobuf implementation (including the backend's) expects.
        val c = Command(type = CommandType.PING, issuedAtMs = -1)
        assertEquals("18ffffffffffffffffff01", toHex(c.encode()))
        assertEquals(-1L, Command.decode(c.encode()).issuedAtMs)
    }

    @Test
    fun unknownFieldsAreSkippedWhenDecoding() {
        // Take a valid Command and splice in an unknown varint field 9 and an unknown
        // length-delimited field 10; decoding must ignore both and keep the known fields.
        val base = Command(type = CommandType.PONG, payload = "{}", issuedAtMs = 5, commandId = "x")
        val extra = byteArrayOf(
            0x48, 0x7F, // field 9, varint, value 127
            0x52, 0x02, 0x68, 0x69, // field 10, string "hi"
        )
        val combined = base.encode() + extra
        val decoded = Command.decode(combined)
        assertEquals(CommandType.PONG, decoded.type)
        assertEquals("{}", decoded.payload)
        assertEquals(5L, decoded.issuedAtMs)
        assertEquals("x", decoded.commandId)
    }

    @Test
    fun truncatedFrameRaisesDecodeException() {
        try {
            SensorPacket.decode(byteArrayOf(0x0D, 0x00, 0x00)) // float field cut short
            fail("expected ProtoDecodeException")
        } catch (_: ProtoDecodeException) {
        }
    }

    // ── cross-check against the Python-generated vectors ────────────────────

    @Test
    fun matchesPythonVectorsExactly() {
        val stream = javaClass.classLoader?.getResourceAsStream("proto_vectors.json")
        assertNotNull("proto_vectors.json missing — run tools/gen_proto_vectors.py", stream)
        val doc = Json.parseToJsonElement(stream!!.reader().readText()) as JsonObject
        val vectors = doc["vectors"]!!.jsonArray
        assertTrue("expected ~10 vectors", vectors.size >= 10)
        for (v in vectors) {
            val o = v as JsonObject
            val name = (o["name"] as JsonPrimitive).content
            val full = hex((o["hex"] as JsonPrimitive).content)
            // Two vectors append unknown fields after a valid encoding. The contract for a
            // proto3 endpoint: the encoder reproduces the KNOWN-field bytes exactly, and the
            // decoder skips the appended unknown fields instead of failing or misparsing.
            val tail = (o["trailing_unknown_hex"] as? JsonPrimitive)?.content?.let(::hex)
            val expected = if (tail == null) full else full.copyOf(full.size - tail.size)
            val fields = o["fields"] as JsonObject
            when ((o["message"] as JsonPrimitive).content) {
                "Command" -> {
                    val c = Command(
                        type = fields.num("type").toInt(),
                        payload = fields.str("payload"),
                        issuedAtMs = fields.ulong("issued_at_ms"),
                        commandId = fields.str("command_id"),
                    )
                    assertArrayEquals("$name: kotlin encode != python encode", expected, c.encode())
                    assertEquals("$name: decode mismatch", c, Command.decode(full))
                }
                "SensorPacket" -> {
                    val p = SensorPacket(
                        accX = fields.f32("acc_x"), accY = fields.f32("acc_y"), accZ = fields.f32("acc_z"),
                        gyroX = fields.f32("gyro_x"), gyroY = fields.f32("gyro_y"), gyroZ = fields.f32("gyro_z"),
                        timestampMs = fields.ulong("timestamp_ms"),
                        sequenceNumber = fields.ulong("sequence_number"),
                        deviceId = fields.str("device_id"),
                        schemaVersion = fields.num("schema_version").toInt(),
                        rawTimestampMs = fields.ulong("raw_timestamp_ms"),
                        accTsMs = fields.ulong("acc_ts_ms"),
                        gyroTsMs = fields.ulong("gyro_ts_ms"),
                        sampleKind = fields.num("sample_kind").toInt(),
                    )
                    assertArrayEquals("$name: kotlin encode != python encode", expected, p.encode())
                    // Byte comparison, not assertEquals: the vector set includes gyro_x=-0.0,
                    // and JSON prints -0.0 as 0.0, so the parsed expectation loses the sign
                    // bit while encode()/decode() provably preserve it (the encode assertion
                    // above pins the exact IEEE bytes).
                    assertArrayEquals("$name: decode mismatch", expected, SensorPacket.decode(full).encode())
                }
                "DeviceRegister" -> {
                    val m = DeviceRegister(
                        deviceId = fields.str("device_id"),
                        deviceRole = fields.str("device_role"),
                        deviceModel = fields.str("device_model"),
                        androidVersion = fields.str("android_version"),
                        appVersion = fields.str("app_version"),
                        schemaVersion = fields.num("schema_version").toInt(),
                    )
                    assertArrayEquals("$name: kotlin encode != python encode", expected, m.encode())
                    assertEquals("$name: decode mismatch", m, DeviceRegister.decode(full))
                }
                else -> fail("$name: unknown message type")
            }
        }
    }

    // JSON field accessors. int64/uint64 values span the whole signed range plus uint64
    // magnitudes above Long.MAX_VALUE (the vector set deliberately includes both
    // IDLE_SEQ_BASE-scale numbers and negatives), so try signed first, then unsigned.
    private fun JsonObject.str(key: String): String = (this[key] as JsonPrimitive).content
    private fun JsonObject.num(key: String): Long = (this[key] as JsonPrimitive).content.toLong()
    private fun JsonObject.ulong(key: String): Long {
        val s = (this[key] as JsonPrimitive).content
        return s.toLongOrNull() ?: s.toULong().toLong()
    }

    private fun JsonObject.f32(key: String): Float =
        (this[key] as JsonPrimitive).content.toFloat()

    // ── payload schemas ─────────────────────────────────────────────────────

    @Test
    fun pongPayloadParsesEveryOptionalField() {
        val p = PongPayload.parse(
            """{"state":"RECORDING","session_id":"s1","late_sid":"s0",
               "telemetry_packets":123,"telemetry_age_ms":-1}""",
        )
        assertNotNull(p)
        assertEquals("RECORDING", p!!.state)
        assertEquals("s1", p.sessionId)
        assertEquals("s0", p.lateSid)
        assertEquals(123L, p.telemetryPackets)
        assertEquals(-1L, p.telemetryAgeMs)
    }

    @Test
    fun emptyPongPayloadMeansNoInformation() {
        assertEquals(null, PongPayload.parse(""))
        assertEquals(null, PongPayload.parse("not json"))
        val p = PongPayload.parse("{}")
        assertNotNull(p)
        assertEquals(null, p!!.state)
    }

    @Test
    fun startSessionPayloadParses() {
        val s = StartSessionPayload.parse(
            """{"session_id":"s2","scheduled_start_ms":1761800000000,
               "subject":"p01","session_tag":"walk","operator":"francis"}""",
        )!!
        assertEquals("s2", s.sessionId)
        assertEquals(1761800000000L, s.scheduledStartMs)
        assertEquals("p01", s.subject)
        assertEquals("walk", s.sessionTag)
        assertEquals("francis", s.operator)
    }
}
