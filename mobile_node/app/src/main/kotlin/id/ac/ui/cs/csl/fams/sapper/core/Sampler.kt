package id.ac.ui.cs.csl.fams.sapper.core

import android.content.Context
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * Accelerometer + gyroscope acquisition on a dedicated [HandlerThread], emitting one reading
 * every [EMIT_PERIOD_MS] into the pipeline. Port of `internal_sensor_manager.dart` with the
 * Dart `Timer.periodic` replaced by a [ScheduledExecutorService] — sampling must not share a
 * thread with UI or sockets, and the emit cadence must not drift under load.
 *
 * Sensors run whenever the service runs (connected or reconnecting), NOT only during a
 * session: the readiness checklist and the acc-magnitude bar need live values while idle, and
 * a session that starts must find the HAL already streaming at rate.
 *
 * Units: Android reports acc in m/s² and gyro in rad/s; the contract wants g and deg/s, so
 * `acc ÷ 9.80665` and `gyro × 180/π`, computed in double (the rescue CSV keeps that
 * precision; the wire proto narrows to float32).
 */
class Sampler(
    context: Context,
    /** Receives every emitted reading ON THE PIPELINE THREAD (the poster does the hop). */
    private val onReading: (Reading) -> Unit,
) {
    companion object {
        /**
         * 200 Hz request — deliberately faster than the 100 Hz emit. The 2510DRA23E is
         * dual-sourced: Bosch bmi3xy handsets ran the physical sensor at 50 Hz and DUPLICATED
         * readings to satisfy a 100 Hz request (49% byte-identical rows). maxRate is 400 Hz on
         * both parts, so 50 Hz was a HAL power-choice, not a ceiling; asking for 200 Hz pushes
         * it up a step. It is a hint the OS may round down.
         */
        const val SENSOR_SAMPLING_PERIOD_US = 5000
        const val EMIT_PERIOD_MS = 10L
        const val EXPECTED_HZ = 100.0
        private const val GRAVITY = 9.80665
        private const val RAD_TO_DEG = 180.0 / Math.PI
        /** A hardware timestamp further than this from corrected-now is not epoch-based. */
        private const val MAX_HW_TS_DRIFT_MS = 3_600_000L
    }

    /** One emitted reading: values in g / deg-s, hardware event times in epoch ms (raw, not
     *  yet clock-offset corrected — the pipeline applies the offset and the 1 h sanity gate). */
    data class Reading(
        val accX: Double,
        val accY: Double,
        val accZ: Double,
        val gyroX: Double,
        val gyroY: Double,
        val gyroZ: Double,
        val accEventMs: Long, // 0 = no event yet
        val gyroEventMs: Long,
        val held: Boolean,
        val accHz: Double,
        val gyroHz: Double,
        val accMagnitude: Double,
    )

    private val sensorManager =
        context.getSystemService(Context.SENSOR_SERVICE) as SensorManager
    private val thread = HandlerThread("sensors").also { it.start() }
    private val sensorHandler = Handler(thread.looper)
    private var scheduler: ScheduledExecutorService? = null

    // Latest raw values, written on the sensor thread, read on the emit thread. Volatile is
    // enough: each emit takes a consistent-enough snapshot, exactly like the Dart original.
    @Volatile private var ax = 0.0
    @Volatile private var ay = 0.0
    @Volatile private var az = 0.0
    @Volatile private var gx = 0.0
    @Volatile private var gy = 0.0
    @Volatile private var gz = 0.0
    @Volatile private var accEventMs = 0L
    @Volatile private var gyroEventMs = 0L

    // Previous EMITTED reading, for held detection. Emit-thread only.
    private var prevAccEventMs = -1L
    private var prevGyroEventMs = -1L
    private var prevAx = Double.NaN
    private var prevAy = Double.NaN
    private var prevAz = Double.NaN
    private var prevGx = Double.NaN
    private var prevGy = Double.NaN
    private var prevGz = Double.NaN

    // Observed-rate windows, counted on the sensor thread, harvested on the emit thread.
    private var accWindowStartMs = 0L
    private var accWindowCount = 0
    private var gyroWindowStartMs = 0L
    private var gyroWindowCount = 0
    @Volatile var observedAccHz = 0.0
        private set
    @Volatile var observedGyroHz = 0.0
        private set

    @Volatile
    var isRunning = false
        private set

    private val listener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            val nowMs = System.currentTimeMillis()
            // event.timestamp is elapsedRealtimeNanos; convert to epoch ms immediately, while
            // the clock reading is closest to the event.
            val epochMs = nowMs - (SystemClock.elapsedRealtimeNanos() - event.timestamp) / 1_000_000
            val v = event.values
            when (event.sensor.type) {
                Sensor.TYPE_ACCELEROMETER -> {
                    ax = v[0].toDouble(); ay = v[1].toDouble(); az = v[2].toDouble()
                    accEventMs = epochMs
                    countAcc(nowMs)
                }
                Sensor.TYPE_GYROSCOPE -> {
                    gx = v[0].toDouble(); gy = v[1].toDouble(); gz = v[2].toDouble()
                    gyroEventMs = epochMs
                    countGyro(nowMs)
                }
            }
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) = Unit
    }

    private fun countAcc(nowMs: Long) {
        if (accWindowStartMs == 0L) accWindowStartMs = nowMs
        accWindowCount++
        val secs = (nowMs - accWindowStartMs) / 1000.0
        if (secs >= 1.0) {
            observedAccHz = accWindowCount / secs
            accWindowStartMs = nowMs
            accWindowCount = 0
        }
    }

    private fun countGyro(nowMs: Long) {
        if (gyroWindowStartMs == 0L) gyroWindowStartMs = nowMs
        gyroWindowCount++
        val secs = (nowMs - gyroWindowStartMs) / 1000.0
        if (secs >= 1.0) {
            observedGyroHz = gyroWindowCount / secs
            gyroWindowStartMs = nowMs
            gyroWindowCount = 0
        }
    }

    fun start() {
        if (isRunning) return
        isRunning = true
        val acc = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER)
        val gyro = sensorManager.getDefaultSensor(Sensor.TYPE_GYROSCOPE)
        if (acc != null) {
            sensorManager.registerListener(listener, acc, SENSOR_SAMPLING_PERIOD_US, sensorHandler)
        }
        if (gyro != null) {
            sensorManager.registerListener(listener, gyro, SENSOR_SAMPLING_PERIOD_US, sensorHandler)
        }
        val s = Executors.newSingleThreadScheduledExecutor { r ->
            Thread(r, "sampler-emit").apply { priority = Thread.MAX_PRIORITY }
        }
        scheduler = s
        s.scheduleAtFixedRate(::emit, EMIT_PERIOD_MS, EMIT_PERIOD_MS, TimeUnit.MILLISECONDS)
    }

    fun stop() {
        isRunning = false
        scheduler?.shutdownNow()
        scheduler = null
        try {
            sensorManager.unregisterListener(listener)
        } catch (_: Exception) {
        }
        accWindowStartMs = 0; accWindowCount = 0
        gyroWindowStartMs = 0; gyroWindowCount = 0
        observedAccHz = 0.0; observedGyroHz = 0.0
        prevAccEventMs = -1; prevGyroEventMs = -1
    }

    fun release() {
        stop()
        thread.quitSafely()
    }

    private fun emit() {
        try {
            // Held == this packet carries no NEW hardware reading. Timestamps alone are not
            // enough: an upsampling HAL re-fires with a fresh timestamp but identical values,
            // so BOTH tests are needed — sameTimestamps catches "callback never fired",
            // sameValues catches "callback fired with a duplicate reading".
            val aTs = accEventMs
            val gTs = gyroEventMs
            val cax = ax; val cay = ay; val caz = az
            val cgx = gx; val cgy = gy; val cgz = gz
            val sameTs = aTs == prevAccEventMs && gTs == prevGyroEventMs
            val sameValues = cax == prevAx && cay == prevAy && caz == prevAz &&
                cgx == prevGx && cgy == prevGy && cgz == prevGz
            val held = sameTs || sameValues
            prevAccEventMs = aTs; prevGyroEventMs = gTs
            prevAx = cax; prevAy = cay; prevAz = caz
            prevGx = cgx; prevGy = cgy; prevGz = cgz

            val accXg = cax / GRAVITY
            val accYg = cay / GRAVITY
            val accZg = caz / GRAVITY
            val mag = sqrt(accXg * accXg + accYg * accYg + accZg * accZg)
            onReading(
                Reading(
                    accX = accXg, accY = accYg, accZ = accZg,
                    gyroX = cgx * RAD_TO_DEG, gyroY = cgy * RAD_TO_DEG, gyroZ = cgz * RAD_TO_DEG,
                    accEventMs = aTs, gyroEventMs = gTs,
                    held = held,
                    accHz = observedAccHz, gyroHz = observedGyroHz,
                    accMagnitude = mag,
                ),
            )
        } catch (_: Exception) {
            // A failed emit must never kill the scheduled task (that would silently stop
            // the whole acquisition); the next tick retries.
        }
    }
}

/**
 * Hardware event time → corrected epoch ms, 0 when unknown. A candidate more than 1 h from
 * corrected-now means the value was not epoch-based to begin with (some platforms report
 * uptime); downstream must treat it as unknown rather than trust a wrong number.
 */
fun correctedHwTs(eventMs: Long, clockOffsetMs: Long, correctedNowMs: Long): Long {
    if (eventMs == 0L) return 0
    val candidate = eventMs + clockOffsetMs
    return if (abs(candidate - correctedNowMs) > 3_600_000L) 0 else candidate
}
