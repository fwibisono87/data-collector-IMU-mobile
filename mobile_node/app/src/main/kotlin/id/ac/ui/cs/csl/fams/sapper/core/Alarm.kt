package id.ac.ui.cs.csl.fams.sapper.core

import android.content.Context
import android.content.res.AssetFileDescriptor
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager

/**
 * Audible + haptic connectivity alarm. The phone is strapped to the subject several metres
 * from the operator; a colour on a screen nobody is looking at is not a signal. The alarm
 * loops until the connection is restored or the operator silences it (plan T13).
 *
 * Native on purpose: the Flutter build played audio through a plugin inside the background
 * engine, where platform calls intermittently failed — with the screen off, mid-trial, which
 * is exactly when the alarm matters.
 *
 * Threading: all methods are called on the pipeline thread; MediaPlayer and the vibration
 * repeater live on the main looper, which [main] posts to.
 */
class Alarm(private val context: Context) {
    private val main = Handler(Looper.getMainLooper())

    private var player: MediaPlayer? = null
    private var alarming = false
    private var silenced = false
    private val haptic = object : Runnable {
        override fun run() {
            if (!alarming || silenced) return
            buzz()
            main.postDelayed(this, 2000)
        }
    }

    val isAlarming: Boolean get() = alarming
    val isSilenced: Boolean get() = silenced

    fun startAlarm() {
        if (alarming) return
        alarming = true
        main.post {
            main.removeCallbacks(haptic)
            haptic.run()
            if (!silenced) playLooping("sounds/alert.wav")
        }
    }

    /** Stops the alarm. Plays the ok chime on restore unless the operator silenced it. */
    fun stopAlarm() {
        if (!alarming) return
        alarming = false
        val wasSilenced = silenced
        silenced = false // re-arm for the next drop
        main.post {
            main.removeCallbacks(haptic)
            stopPlayer()
            if (!wasSilenced) playOnce("sounds/ok.wav")
        }
    }

    /** Operator acknowledged (SILENCE button or notification action): everything stops until
     *  the next drop re-arms it. */
    fun silence() {
        silenced = true
        main.post {
            main.removeCallbacks(haptic)
            stopPlayer()
        }
    }

    fun release() {
        alarming = false
        silenced = false
        main.post {
            main.removeCallbacks(haptic)
            stopPlayer()
        }
    }

    // ── audio ───────────────────────────────────────────────────────────────

    private fun playLooping(asset: String) {
        stopPlayer()
        player = newPlayer(asset, looping = true)
    }

    private fun playOnce(asset: String) {
        // A second player for the chime: the loop player may still be releasing.
        val p = newPlayer(asset, looping = false)
        p?.setOnCompletionListener { runCatching { it.release() } }
    }

    private fun newPlayer(asset: String, looping: Boolean): MediaPlayer? = try {
        val afd: AssetFileDescriptor = context.assets.openFd(asset)
        val p = MediaPlayer().apply {
            // USAGE_ALARM: audible even with media volume low or DND media-exempt.
            setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
            setDataSource(afd.fileDescriptor, afd.startOffset, afd.length)
            isLooping = looping
            setVolume(1f, 1f)
            prepare()
            start()
        }
        afd.close()
        p
    } catch (_: Exception) {
        // No audio device (rare handsets, focus loss): haptics + notification still alert.
        null
    }

    private fun stopPlayer() {
        player?.let { p ->
            runCatching { if (p.isPlaying) p.stop() }
            runCatching { p.release() }
        }
        player = null
    }

    // ── haptics ─────────────────────────────────────────────────────────────

    private fun buzz() {
        try {
            val vibrator = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                (context.getSystemService(Context.VIBRATOR_MANAGER_SERVICE) as VibratorManager)
                    .defaultVibrator
            } else {
                @Suppress("DEPRECATION")
                context.getSystemService(Context.VIBRATOR_SERVICE) as Vibrator
            }
            // 80 ms strong tick, roughly the old HapticFeedback.heavyImpact every 2 s.
            vibrator.vibrate(
                VibrationEffect.createOneShot(80, VibrationEffect.DEFAULT_AMPLITUDE),
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build(),
            )
        } catch (_: Exception) {
        }
    }
}
