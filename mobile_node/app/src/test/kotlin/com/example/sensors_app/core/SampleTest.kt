package com.example.sensors_app.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The rescue CSV's 6-decimal rendering is a hard byte contract with 17 subjects of existing
 * data and with the Python backend, which re-renders cells with `f"{v:.6f}"`. Java's
 * `String.format("%.6f")` rounds the SHORTEST DECIMAL representation (5e-7 → "0.000001"),
 * while Dart's `toStringAsFixed(6)` and Python round the EXACT BINARY value (→ "0.000000"),
 * so [Sample.f] uses BigDecimal — and this test pins the agreement against the table that
 * `tools/gen_proto_vectors.py` produced with Python (11 hand-picked edge cases + 200 seeded
 * pseudo-random doubles in [-300, 300], the sensor value range).
 */
class SampleTest {

    @Test
    fun float6MatchesPythonTableExactly() {
        val stream = javaClass.classLoader?.getResourceAsStream("proto_vectors.json")
        assertNotNull("proto_vectors.json missing — run tools/gen_proto_vectors.py", stream)
        val doc = Json.parseToJsonElement(stream!!.reader().readText()) as JsonObject
        val table = doc["float6"]!!.jsonArray
        assertTrue("expected 11 fixed + 200 random entries", table.size >= 211)
        for (e in table) {
            val o = e as JsonObject
            // Read the literal JSON text and parse it ourselves: "-0.0" must keep its sign.
            val literal = (o["value"] as JsonPrimitive).content
            val v = literal.toDouble()
            val expected = (o["text"] as JsonPrimitive).content
            assertEquals("f($literal)", expected, Sample.f(v))
        }
    }

    @Test
    fun negativeZeroSurvivesTheRoundTrip() {
        // Guards the guard: if -0.0 parsed as +0.0, the table test above would silently
        // stop covering the sign-restoration branch.
        assertTrue(1.0 / "-0.0".toDouble() < 0.0)
        assertEquals("-0.000000", Sample.f(-0.0))
        assertEquals("0.000000", Sample.f(0.0))
    }
}
