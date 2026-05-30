package com.vortex.driver

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.MediaRecorder
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * B11.12: microphone capture for push-to-talk upstream.
 *
 * Pipeline:
 *
 *   AudioRecord(MIC, 48 kHz, mono, PCM16)
 *     -> MediaCodec AAC-LC encoder
 *     -> [NalSink] (CSD once, then per-frame raw access units)
 *
 * Symmetric NalSink contract with [ScreenAudioCapture], so the
 * PeerControlActivity wrapper can encode + ship to the peer's
 * `mic_open` / `mic_chunk` ops using a single bridge function.
 *
 * Requires `RECORD_AUDIO`; the manifest already declares it (we use
 * it for B4 record_audio). `MediaRecorder.AudioSource.MIC` is the
 * unprocessed-mic source; system AGC/echo-cancellation can be
 * layered on top via AcousticEchoCanceler if needed (deferred).
 *
 * Lifecycle: [start] kicks off capture on a background thread;
 * [stop] tears down. Both idempotent.
 */
class MicCapture(
    private val context: Context,
    private val sampleRate: Int = 48_000,
    private val channelCount: Int = 1,
    private val bitrateBps: Int = 64_000,
) {

    interface NalSink {
        fun onCodecConfig(csdBytes: ByteArray, sampleRate: Int, channels: Int, codecString: String)
        fun onFrame(aacBytes: ByteArray, ptsMicros: Long)
        fun onError(message: String)
    }

    private val workThread = HandlerThread("MicCapture").apply { start() }
    private val workHandler = Handler(workThread.looper)

    @Volatile private var record: AudioRecord? = null
    @Volatile private var codec: MediaCodec? = null
    @Volatile private var stopRequested = false

    fun start(sink: NalSink) {
        workHandler.post { openInternal(sink) }
    }

    fun stop() {
        stopRequested = true
        workHandler.post {
            try { record?.stop() } catch (_: Throwable) {}
            try { record?.release() } catch (_: Throwable) {}
            try { codec?.stop() } catch (_: Throwable) {}
            try { codec?.release() } catch (_: Throwable) {}
            record = null; codec = null
        }
        try { workThread.quitSafely() } catch (_: Throwable) {}
    }

    private fun openInternal(sink: NalSink) {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            sink.onError("RECORD_AUDIO permission not granted")
            return
        }
        try {
            val channelMask = if (channelCount == 2) AudioFormat.CHANNEL_IN_STEREO
                              else AudioFormat.CHANNEL_IN_MONO
            val minBuf = AudioRecord.getMinBufferSize(
                sampleRate, channelMask, AudioFormat.ENCODING_PCM_16BIT,
            ).coerceAtLeast(4096)
            val rec = AudioRecord(
                MediaRecorder.AudioSource.MIC,
                sampleRate, channelMask, AudioFormat.ENCODING_PCM_16BIT,
                minBuf * 2,
            )
            record = rec
            rec.startRecording()

            val format = MediaFormat.createAudioFormat(MIME, sampleRate, channelCount).apply {
                setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
                setInteger(MediaFormat.KEY_BIT_RATE, bitrateBps)
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, minBuf)
            }
            val c = MediaCodec.createEncoderByType(MIME)
            c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            c.setCallback(object : MediaCodec.Callback() {
                override fun onInputBufferAvailable(codec: MediaCodec, index: Int) {
                    // Driven from the pump thread below.
                }
                override fun onOutputBufferAvailable(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
                    if (stopRequested) {
                        try { codec.releaseOutputBuffer(index, false) } catch (_: Throwable) {}
                        return
                    }
                    try {
                        val buf = codec.getOutputBuffer(index)
                        if (buf == null || info.size <= 0) {
                            codec.releaseOutputBuffer(index, false); return
                        }
                        buf.position(info.offset)
                        buf.limit(info.offset + info.size)
                        val out = ByteArray(info.size)
                        buf.get(out)
                        val isConfig = (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0
                        if (isConfig) {
                            sink.onCodecConfig(out, sampleRate, channelCount, "mp4a.40.2")
                        } else {
                            sink.onFrame(out, info.presentationTimeUs)
                        }
                        codec.releaseOutputBuffer(index, false)
                    } catch (e: Throwable) {
                        Log.w(TAG, "mic output buffer failed: $e")
                        sink.onError("AAC output failed: ${e.javaClass.simpleName}: ${e.message}")
                    }
                }
                override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) {
                    sink.onError("AAC encoder error: ${e.diagnosticInfo}")
                }
                override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
                    Log.i(TAG, "mic encoder output format: $format")
                }
            }, workHandler)
            c.start()
            codec = c

            // PCM pump. Same pattern as ScreenAudioCapture; AudioRecord
            // is blocking and MediaCodec's input-buffer side isn't
            // async-callback-driven, so a small dedicated thread is the
            // cleanest fit.
            Thread({
                val pcm = ByteArray(minBuf)
                var samplesRead = 0L
                while (!stopRequested) {
                    val n = try { rec.read(pcm, 0, pcm.size) }
                            catch (e: Throwable) { sink.onError("AudioRecord.read failed: $e"); -1 }
                    if (n <= 0) { Thread.sleep(10); continue }
                    val cc = codec ?: return@Thread
                    var idx = -1
                    try { idx = cc.dequeueInputBuffer(20_000L) } catch (_: Throwable) {}
                    if (idx < 0) continue
                    val ib = try { cc.getInputBuffer(idx) } catch (_: Throwable) { null } ?: continue
                    ib.clear(); ib.put(pcm, 0, n)
                    val ptsUs = samplesRead * 1_000_000L / sampleRate
                    samplesRead += (n / (2 * channelCount)).toLong()
                    try { cc.queueInputBuffer(idx, 0, n, ptsUs, 0) }
                    catch (e: Throwable) { Log.w(TAG, "queueInputBuffer failed: $e") }
                }
            }, "MicCapturePump").apply { isDaemon = true }.start()

            Log.i(TAG, "mic up: ${sampleRate}Hz x ${channelCount}ch, ${bitrateBps / 1000} kbps")
        } catch (e: SecurityException) {
            sink.onError("RECORD_AUDIO not granted: ${e.message}")
        } catch (e: Throwable) {
            sink.onError("mic setup failed: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "MicCapture"
        private const val MIME = MediaFormat.MIMETYPE_AUDIO_AAC
    }
}
