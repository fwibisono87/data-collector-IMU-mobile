package id.ac.ui.cs.csl.fams.sapper

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Target of the notification's SILENCE action. A receiver rather than a service start:
 * startService() from a PendingIntent is restricted once the app is backgrounded, and the
 * alarm usually sounds exactly then (phone in a pocket, screen off).
 */
class AlarmActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context?, intent: Intent?) {
        Runtime.silenceAlarm()
    }
}
