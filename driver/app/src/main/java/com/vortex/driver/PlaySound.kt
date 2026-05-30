package com.vortex.driver

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.MediaPlayer
import android.media.RingtoneManager
import android.util.Log
import org.json.JSONObject

/**
 * B11.15: "Where is my phone?" — play an alarm-volume sound on the
 * peer for a bounded number of seconds. Useful for queued commands
 * fired against an offline-then-online device (theft scenario) or
 * just a "find it under the couch" tap.
 *
 * Implementation:
 *   - Pull the system default alarm ringtone (always present); fall
 *     back to notification, then ringtone if alarm is null.
 *   - Force alarm-stream volume to max for the duration; restore
 *     when finished. We leave ringer profile untouched.
 *   - MediaPlayer with USAGE_ALARM + isLooping, stopped by a
 *     postDelayed on the main looper.
 *
 * Safe: no microphone, no camera, no network -- a self-contained
 * playback. Worst case the user hears a 30 s alarm and presses
 * Volume Down on the phone.
 */
object PlaySound {

    private const val TAG = "PlaySound"
    private const val DEFAULT_SEC = 10
    private const val MAX_SEC = 60

    @Volatile private var current: MediaPlayer? = null
    @Volatile private var savedAlarmVolume: Int = -1

    /** Synchronous from the caller's POV; playback runs async. */
    fun play(ctx: Context, durationSec: Int = DEFAULT_SEC): JSONObject {
        val dur = durationSec.coerceIn(1, MAX_SEC)
        stopInternal(ctx)  // cancel any prior alarm

        val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
            ?: RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE)
            ?: return JSONObject().put("ok", false).put("error", "No system tone available")

        // Max alarm volume, remember the prior for restore.
        val am = ctx.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
        if (am != null) {
            try {
                savedAlarmVolume = am.getStreamVolume(AudioManager.STREAM_ALARM)
                val max = am.getStreamMaxVolume(AudioManager.STREAM_ALARM)
                am.setStreamVolume(AudioManager.STREAM_ALARM, max, 0)
            } catch (_: Throwable) { /* some OEMs lock alarm volume -- continue */ }
        }

        val mp = MediaPlayer()
        try {
            mp.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                    .build()
            )
            mp.setDataSource(ctx, uri)
            mp.isLooping = true
            mp.prepare()
            mp.start()
            current = mp
        } catch (e: Throwable) {
            Log.w(TAG, "MediaPlayer failed: ${e.message}")
            try { mp.release() } catch (_: Throwable) {}
            restoreVolume(ctx)
            return JSONObject().put("ok", false).put("error", e.message ?: "playback failed")
        }

        // Schedule a stop. Use a HandlerThread-free post via the
        // application context's main looper -- this runs even when no
        // activity is in the foreground.
        android.os.Handler(android.os.Looper.getMainLooper())
            .postDelayed({ stopInternal(ctx) }, dur * 1000L)

        return JSONObject().put("ok", true).put("duration", dur)
    }

    private fun stopInternal(ctx: Context) {
        val mp = current
        current = null
        if (mp != null) {
            try { mp.stop() } catch (_: Throwable) {}
            try { mp.release() } catch (_: Throwable) {}
        }
        restoreVolume(ctx)
    }

    private fun restoreVolume(ctx: Context) {
        val v = savedAlarmVolume
        savedAlarmVolume = -1
        if (v < 0) return
        try {
            val am = ctx.getSystemService(Context.AUDIO_SERVICE) as? AudioManager
            am?.setStreamVolume(AudioManager.STREAM_ALARM, v, 0)
        } catch (_: Throwable) {}
    }
}
