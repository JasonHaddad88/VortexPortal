package com.vortex.driver

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.os.Build
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay
import java.io.File

/**
 * Native equivalent of the Python agent's `op_record_audio`. Records
 * a short clip to a tempfile and returns the bytes to a stream op.
 * Output format matches termux-microphone-record (`audio/mp4`, AAC)
 * so the hub's audio player Just Works.
 *
 * Permission contract: throws RuntimeException with a clear
 * "Settings -> Apps -> Vortex Driver -> Permissions -> Microphone"
 * instruction if [Manifest.permission.RECORD_AUDIO] is missing.
 */
object RecordAudioOp {

    /**
     * Records [durationSec] seconds (clamped 1..120) and returns the
     * resulting file. Caller is responsible for streaming the bytes
     * out and then deleting the file.
     */
    suspend fun record(ctx: Context, durationSec: Int): File {
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED
        ) {
            throw RuntimeException(
                "Microphone permission not granted. Open Settings -> Apps -> " +
                "Vortex Driver -> Permissions -> Microphone -> Allow."
            )
        }
        val dur = durationSec.coerceIn(1, 120)
        val out = File.createTempFile("vortex-aud-", ".m4a", ctx.cacheDir)

        // Use the context-aware constructor on Android 12+; fall back to
        // the deprecated one on older platforms (still works through 14).
        val recorder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            MediaRecorder(ctx)
        } else {
            @Suppress("DEPRECATION") MediaRecorder()
        }
        try {
            recorder.setAudioSource(MediaRecorder.AudioSource.MIC)
            recorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            recorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            recorder.setAudioEncodingBitRate(128_000)
            recorder.setAudioSamplingRate(44_100)
            recorder.setOutputFile(out.absolutePath)
            recorder.setMaxDuration(dur * 1000)
            recorder.prepare()
            recorder.start()
            // Suspend rather than Thread.sleep so the WS coroutine
            // stays cancellable while the mic runs.
            delay(dur * 1000L)
            try { recorder.stop() } catch (_: Exception) { /* short clips can throw */ }
        } finally {
            try { recorder.reset() } catch (_: Exception) {}
            try { recorder.release() } catch (_: Exception) {}
        }
        if (out.length() == 0L) {
            try { out.delete() } catch (_: Exception) {}
            throw RuntimeException(
                "Recording produced an empty file. The microphone may be " +
                "held by another app, or the system rejected the request " +
                "(privacy indicator / mic block)."
            )
        }
        return out
    }
}
