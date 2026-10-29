package com.example.sensors_app.core

/**
 * One emitted reading in DOUBLE precision, the form the rescue CSV is written in.
 *
 * The wire proto carries float32 ([SensorPacket]), but the local CSV keeps the full double
 * the sensor HAL produced, exactly like the old Dart `SensorPacket` model did — the rescue
 * file is the analysis-grade copy and must not lose precision the live stream already lost.
 *
 * Units: acc in g, gyro in deg/s. `accTsMs`/`gyroTsMs` are hardware event times in the
 * corrected epoch domain, 0 = unknown. `sampleKind`: 0 = fresh, 1 = held.
 */
data class Sample(
    val accX: Double,
    val accY: Double,
    val accZ: Double,
    val gyroX: Double,
    val gyroY: Double,
    val gyroZ: Double,
    val timestampMs: Long,
    val rawTimestampMs: Long,
    val sequence: Long,
    val deviceId: String,
    val accTsMs: Long = 0,
    val gyroTsMs: Long = 0,
    val sampleKind: Int = 0,
    val labelId: Int = 0,
    val labelName: String = "0",
) {
    /** The float32 wire form sent on `/ws/telemetry` and written to the offline buffer. */
    fun toProto(): SensorPacket = SensorPacket(
        accX = accX.toFloat(),
        accY = accY.toFloat(),
        accZ = accZ.toFloat(),
        gyroX = gyroX.toFloat(),
        gyroY = gyroY.toFloat(),
        gyroZ = gyroZ.toFloat(),
        timestampMs = timestampMs,
        sequenceNumber = sequence,
        deviceId = deviceId,
        schemaVersion = SensorPacket.SCHEMA_VERSION,
        rawTimestampMs = rawTimestampMs,
        accTsMs = accTsMs,
        gyroTsMs = gyroTsMs,
        sampleKind = sampleKind,
    )

    /** One CSV data row, terminated with `\n`. Byte-identical to the old Dart output. */
    fun csvRow(): String = buildString(96) {
        append(timestampMs).append(',')
        append(f(accX)).append(',')
        append(f(accY)).append(',')
        append(f(accZ)).append(',')
        append(f(gyroX)).append(',')
        append(f(gyroY)).append(',')
        append(f(gyroZ)).append(',')
        append(labelId).append(',')
        append(labelName).append(',')
        append(sequence).append(',')
        append(deviceId).append(',')
        if (accTsMs != 0L) append(accTsMs)
        append(',')
        if (gyroTsMs != 0L) append(gyroTsMs)
        append(',')
        append(sampleKind).append('\n')
    }

    companion object {
        /**
         * Rounds the EXACT binary value, half-up, to 6 decimals — byte-identical to Dart's
         * `toStringAsFixed(6)` and Python's `f"{v:.6f}"` (the backend re-renders CSV cells).
         * `String.format("%.6f")` is NOT equivalent: Java's formatter rounds the shortest
         * decimal representation, so 5e-7 → "0.000001" where Dart/Python give "0.000000",
         * and 0.1234565 → "0.123457" where they give "0.123456".
         *
         * BigDecimal also drops the sign of values that round to zero (-1e-9, -0.0), which
         * both Dart and Python keep, hence the explicit sign restoration. Locale is
         * irrelevant on this path (toPlainString is always "."-decimal) — the field phones
         * run an Indonesian locale, where the default formatter would write decimal commas.
         */
        fun f(v: Double): String {
            if (!v.isFinite()) return v.toString() // "NaN"/"Infinity": never throw mid-session
            var s = java.math.BigDecimal(v)
                .setScale(6, java.math.RoundingMode.HALF_UP)
                .toPlainString()
            val negative = v < 0.0 || (v == 0.0 && 1.0 / v < 0.0) // -0.0 → 1/v is -Infinity
            if (negative && !s.startsWith("-")) s = "-$s"
            return s
        }
    }
}
