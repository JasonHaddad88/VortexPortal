package com.vortex.driver

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import java.nio.ByteBuffer

/**
 * B11.10: AAC-LC -> PCM decoder + AudioTrack player for the peer's
 * multiplexed audio track.
 *
 * Pipeline:
 *
 *   stream_start.audio.csd_base64 -> MediaCodec.configure(...)
 *   per stream_chunk_header{track:"a"}: queueInputBuffer(AAC bytes)
 *   PCM out -> AudioTrack.write(...) in MODE_STREAM
 *
 * Companion to [ScreenAudioCapture] on the producer side. Wire shape
 * is symmetric -- the producer's CSD is the AAC-LC AudioSpecificConfig
 * blob MediaCodec expects in csd-0.
 *
 * Threading: a dedicated HandlerThread runs the MediaCodec callbacks
 * so we don't serialise through the WS dispatch thread. AudioTrack
 * writes happen inside the decoder's onOutputBufferAvailable callback;
 * the call is blocking but only ever as long as one buffer's worth of
 * PCM (~20 ms at 48 kHz stereo), which is well under our budget.
 *
 * Lifecycle:
 *   - [configure]: call once with the CSD + sample rate + channel
 *     count from stream_start. Returns true on success.
 *   - [feed]: per AAC chunk. Non-blocking; drops the frame if no
 *     input buffer is available (audio glitches are preferable to
 *     unbounded latency).
 *   - [stop]: tear down. Idempotent.
 */
class AacDecoder {

    private val workThread = HandlerThread("AacDecoder").apply { start() }
    private val workHandler = Handler(workThread.looper)
    @Volatile private var codec: MediaCodec? = null
    @Volatile private var track: AudioTrack? = null
    @Volatile private var stopped = false

    fun configure(csd: ByteArray, sampleRate: Int, channels: Int): Boolean {
        if (stopped) return false
        return try {
            val format = MediaFormat.createAudioFormat(MIME, sampleRate, channels).apply {
                setByteBuffer("csd-0", ByteBuffer.wrap(csd))
                setInteger(MediaFormat.KEY_IS_ADTS, 0)
            }
            val c = MediaCodec.createDecoderByType(MIME)
            c.setCallback(object : MediaCodec.Callback() {
                override fun onInputBufferAvailable(codec: MediaCodec, index: Int) {
                    // Claimed inline in feed(); nothing to do here.
                }
                override fun onOutputBufferAvailable(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
                    if (stopped) {
                        try { codec.releaseOutputBuffer(index, false) } catch (_: Throwable) {}
                        return
                    }
                    try {
                        val buf = codec.getOutputBuffer(index)
                        if (buf != null && info.size > 0) {
                            buf.position(info.offset)
                            buf.limit(info.offset + info.size)
                            // ByteBuffer overload writes straight from
                            // the codec buffer -- no per-frame heap
                            // allocation. Blocking is harmless on this
                            // dedicated worker thread.
                            track?.write(buf, info.size, AudioTrack.WRITE_BLOCKING)
                        }
                        codec.releaseOutputBuffer(index, false)
                    } catch (e: Throwable) {
                        Log.w(TAG, "audio output buffer failed: $e")
                    }
                }
                override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) {
                    Log.w(TAG, "AAC decoder error: ${e.diagnosticInfo}")
                }
                override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
                    Log.i(TAG, "AAC decoder output format: $format")
                }
            }, workHandler)
            c.configure(format, null, null, 0)
            c.start()
            codec = c
            // Set up the AudioTrack only after the decoder is up so a
            // configure failure doesn't leak a started track.
            track = buildAudioTrack(sampleRate, channels).also { it.play() }
            true
        } catch (e: Throwable) {
            Log.w(TAG, "AAC decoder configure failed: ${e.javaClass.simpleName}: ${e.message}")
            try { track?.release() } catch (_: Throwable) {}
            track = null
            false
        }
    }

    private fun buildAudioTrack(sampleRate: Int, channels: Int): AudioTrack {
        val channelMask = if (channels == 2) AudioFormat.CHANNEL_OUT_STEREO
                          else AudioFormat.CHANNEL_OUT_MONO
        val minBuf = AudioTrack.getMinBufferSize(
            sampleRate, channelMask, AudioFormat.ENCODING_PCM_16BIT,
        ).coerceAtLeast(4096)
        val attrs = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_MOVIE)
            .build()
        val fmt = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(sampleRate)
            .setChannelMask(channelMask)
            .build()
        return AudioTrack.Builder()
            .setAudioAttributes(attrs)
            .setAudioFormat(fmt)
            .setBufferSizeInBytes(minBuf * 2)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .build()
    }

    /**
     * Feed one AAC access unit (raw, no ADTS). Non-blocking: returns
     * false on full input queue, which means we're audio-CPU-bound
     * and a dropped frame is preferable to unbounded latency.
     */
    fun feed(aacBytes: ByteArray, ptsMicros: Long): Boolean {
        val c = codec ?: return false
        if (stopped) return false
        return try {
            val index = c.dequeueInputBuffer(0L)
            if (index < 0) return false
            val buf = c.getInputBuffer(index) ?: return false
            buf.clear()
            buf.put(aacBytes)
            c.queueInputBuffer(index, 0, aacBytes.size, ptsMicros, 0)
            true
        } catch (e: Throwable) {
            Log.w(TAG, "audio feed threw: ${e.javaClass.simpleName}: ${e.message}")
            false
        }
    }

    /** Idempotent tear-down. */
    fun stop() {
        stopped = true
        workHandler.post {
            try { track?.pause() } catch (_: Throwable) {}
            try { track?.flush() } catch (_: Throwable) {}
            try { track?.release() } catch (_: Throwable) {}
            try { codec?.flush() } catch (_: Throwable) {}
            try { codec?.stop() } catch (_: Throwable) {}
            try { codec?.release() } catch (_: Throwable) {}
            codec = null; track = null
        }
        try { workThread.quitSafely() } catch (_: Throwable) {}
    }

    companion object {
        private const val TAG = "AacDecoder"
        private const val MIME = MediaFormat.MIMETYPE_AUDIO_AAC
    }
}
