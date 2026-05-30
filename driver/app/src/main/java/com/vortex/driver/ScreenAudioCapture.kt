package com.vortex.driver

import android.annotation.TargetApi
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioPlaybackCaptureConfiguration
import android.media.AudioRecord
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Log

/**
 * B11.10: system-audio capture for the screen-share pipeline.
 *
 * Pipeline:
 *
 *   MediaProjection -> AudioPlaybackCaptureConfiguration -> AudioRecord
 *   -> MediaCodec (AAC-LC, 48 kHz, stereo) -> [NalSink]
 *
 * Mirrors the [ScreenH264Encoder] sink contract so [Ops] can emit the
 * resulting access units onto the same `screen_stream` WS sink as the
 * video, distinguished only by a `track:"a"` field on the chunk
 * header.
 *
 * Requirements:
 *   - API 29+ (Android 10) for [AudioPlaybackCaptureConfiguration].
 *     The screen-share consent dialog the user already accepted to
 *     grant MediaProjection ALSO covers playback capture -- no extra
 *     prompt.
 *   - Apps with `android:allowAudioPlaybackCapture="false"` (mostly
 *     DRM streamers) silently emit silence; that's a platform-level
 *     guarantee we can't override.
 *   - RECORD_AUDIO runtime permission is declared in the manifest;
 *     [start] surfaces an error via the sink if it's missing.
 *
 * Lifecycle: [start] kicks off capture in a background thread,
 * [stop] tears it down. Both are idempotent.
 */
class ScreenAudioCapture(
    private val context: Context,
    private val resultCode: Int,
    private val resultData: Intent,
    private val sampleRate: Int = 48_000,
    private val channelCount: Int = 2,
    private val bitrateBps: Int = 128_000,
) {

    /** Same sink contract as [ScreenH264Encoder.NalSink] -- emits
     *  CSD once on first output, then AAC ADTS-free access units. */
    interface NalSink {
        fun onCodecConfig(csdBytes: ByteArray, sampleRate: Int, channels: Int, codecString: String)
        fun onFrame(aacBytes: ByteArray, ptsMicros: Long)
        fun onError(message: String)
    }

    private val workThread = HandlerThread("ScreenAudio").apply { start() }
    private val workHandler = Handler(workThread.looper)

    @Volatile private var projection: MediaProjection? = null
    @Volatile private var record: AudioRecord? = null
    @Volatile private var codec: MediaCodec? = null
    @Volatile private var stopRequested = false
    @Volatile private var pumpThread: Thread? = null

    fun start(sink: NalSink) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            sink.onError("System audio capture needs Android 10 (API 29) or newer")
            return
        }
        workHandler.post { openInternal(sink) }
    }

    fun stop() {
        stopRequested = true
        workHandler.post {
            try { record?.stop() } catch (_: Throwable) {}
            try { record?.release() } catch (_: Throwable) {}
            try { codec?.stop() } catch (_: Throwable) {}
            try { codec?.release() } catch (_: Throwable) {}
            try { projection?.stop() } catch (_: Throwable) {}
            record = null; codec = null; projection = null
        }
        try { workThread.quitSafely() } catch (_: Throwable) {}
    }

    @TargetApi(Build.VERSION_CODES.Q)
    private fun openInternal(sink: NalSink) {
        try {
            val mgr = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE)
                      as? MediaProjectionManager
                ?: run { sink.onError("MEDIA_PROJECTION_SERVICE unavailable"); return }
            // Important: this call returns a NEW MediaProjection -- the
            // one the video encoder owns is a different session. Both
            // are valid against the same consent result; the platform
            // ref-counts the underlying screen-capture session.
            val mp = mgr.getMediaProjection(resultCode, resultData)
            projection = mp

            // Pull system audio playback (music, video, game) and skip
            // recents like voice calls or system sounds the user
            // probably doesn't want broadcast over the share.
            val cfgBuilder = AudioPlaybackCaptureConfiguration.Builder(mp)
                .addMatchingUsage(AudioAttributes.USAGE_MEDIA)
                .addMatchingUsage(AudioAttributes.USAGE_GAME)
                .addMatchingUsage(AudioAttributes.USAGE_UNKNOWN)

            val channelMask = if (channelCount == 2) AudioFormat.CHANNEL_IN_STEREO
                              else AudioFormat.CHANNEL_IN_MONO
            val audioFormat = AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(sampleRate)
                .setChannelMask(channelMask)
                .build()

            val minBuf = AudioRecord.getMinBufferSize(
                sampleRate, channelMask, AudioFormat.ENCODING_PCM_16BIT,
            ).coerceAtLeast(4096)

            val rec = AudioRecord.Builder()
                .setAudioFormat(audioFormat)
                .setBufferSizeInBytes(minBuf * 2)
                .setAudioPlaybackCaptureConfig(cfgBuilder.build())
                .build()
            record = rec
            rec.startRecording()

            // ---- MediaCodec AAC encoder (LC profile, ADTS-free raw frames) ----
            val format = MediaFormat.createAudioFormat(MIME, sampleRate, channelCount).apply {
                setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
                setInteger(MediaFormat.KEY_BIT_RATE, bitrateBps)
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, minBuf)
            }
            val c = MediaCodec.createEncoderByType(MIME)
            c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            c.setCallback(object : MediaCodec.Callback() {
                override fun onInputBufferAvailable(codec: MediaCodec, index: Int) {
                    // Driven from the pump thread below; nothing to do here.
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
                            sink.onCodecConfig(
                                csdBytes = out,
                                sampleRate = sampleRate,
                                channels = channelCount,
                                codecString = "mp4a.40.2",  // AAC-LC
                            )
                        } else {
                            sink.onFrame(out, info.presentationTimeUs)
                        }
                        codec.releaseOutputBuffer(index, false)
                    } catch (e: Throwable) {
                        Log.w(TAG, "audio output buffer failed: $e")
                        sink.onError("AAC output failed: ${e.javaClass.simpleName}: ${e.message}")
                    }
                }
                override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) {
                    sink.onError("AAC MediaCodec error: ${e.diagnosticInfo}")
                }
                override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
                    Log.i(TAG, "AAC encoder output format: $format")
                }
            }, workHandler)
            c.start()
            codec = c

            // ---- pump PCM from AudioRecord into the encoder's input buffers ----
            //
            // MediaCodec's input-buffer side does NOT post async callbacks
            // when using the default (non-Surface) input mode; the safest
            // pattern is a small dedicated reader thread that blocks on
            // dequeueInputBuffer with a short timeout. Easier than wiring
            // callbacks + a queue.
            val pump = Thread({
                val pcmBuf = ByteArray(minBuf)
                var samplesRead = 0L
                while (!stopRequested) {
                    val n = try { rec.read(pcmBuf, 0, pcmBuf.size) }
                            catch (e: Throwable) { sink.onError("AudioRecord.read failed: $e"); -1 }
                    if (n <= 0) {
                        // EOS or transient -- back off slightly.
                        Thread.sleep(10); continue
                    }
                    val cc = codec ?: return@Thread
                    var idx = -1
                    try { idx = cc.dequeueInputBuffer(20_000L) } catch (_: Throwable) {}
                    if (idx < 0) continue
                    val ib = try { cc.getInputBuffer(idx) } catch (_: Throwable) { null }
                    if (ib == null) continue
                    ib.clear()
                    ib.put(pcmBuf, 0, n)
                    // pts in microseconds based on running sample count.
                    val ptsUs = samplesRead * 1_000_000L / sampleRate
                    samplesRead += (n / (2 * channelCount)).toLong()
                    try {
                        cc.queueInputBuffer(idx, 0, n, ptsUs, 0)
                    } catch (e: Throwable) {
                        Log.w(TAG, "queueInputBuffer failed: $e")
                    }
                }
            }, "ScreenAudioPump").apply { isDaemon = true }
            pumpThread = pump
            pump.start()

            Log.i(TAG, "audio capture up: ${sampleRate}Hz x ${channelCount}ch, ${bitrateBps / 1000} kbps")
        } catch (e: SecurityException) {
            sink.onError("RECORD_AUDIO not granted: ${e.message}")
        } catch (e: Throwable) {
            sink.onError("audio capture setup failed: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "ScreenAudioCapture"
        private const val MIME = MediaFormat.MIMETYPE_AUDIO_AAC
    }
}
