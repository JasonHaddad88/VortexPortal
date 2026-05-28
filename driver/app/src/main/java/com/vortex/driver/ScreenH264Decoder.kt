package com.vortex.driver

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.view.Surface
import java.nio.ByteBuffer

/**
 * B11.7: native H.264 decode for incoming peer screen frames.
 *
 * Pipeline:
 *
 *   stream_start.csd_base64 (SPS+PPS, annex-B) -> MediaCodec.configure(...)
 *   per stream_chunk_header: queueInputBuffer(NALs, kf?)
 *   output rendered directly onto the provided Surface (no per-frame
 *   bitmap copy -- the decoder writes straight to the GPU).
 *
 * This is the counterpart to [ScreenH264Encoder] on the producer side
 * (B5). The encoder ships baseline / level 3.1 + a 1 s I-frame interval;
 * the decoder makes no assumptions about codec params beyond what
 * arrives in `csd` -- it just feeds MediaCodec the bytes the peer
 * sent.
 *
 * Threading: all MediaCodec calls happen on a dedicated HandlerThread
 * so the WebSocket coroutine doesn't end up serialised through the
 * decoder. Public methods are thread-safe (volatile + delegate to the
 * worker via post()).
 *
 * Lifecycle:
 *   - [configure]: call once with the surface + CSD blob from
 *     stream_start. Returns true on success.
 *   - [feed]: per chunk. Non-blocking; drops the frame if the
 *     decoder's input queue is full (better than introducing
 *     latency).
 *   - [stop]: tear down. Idempotent.
 */
class ScreenH264Decoder {

    private val workThread = HandlerThread("ScreenH264Decoder").apply { start() }
    private val workHandler = Handler(workThread.looper)
    @Volatile private var codec: MediaCodec? = null
    @Volatile private var stopped = false
    /** Monotonic clock for queueInputBuffer's presentationTimeUs --
     *  decoder doesn't care about the actual wall time as long as
     *  values are increasing. */
    private var ptsCounter: Long = 0L

    /**
     * Configure the decoder with the codec config (SPS+PPS) the peer
     * shipped in stream_start.csd_base64 and the SurfaceView surface
     * to render into. Width/height come from stream_start as well.
     * Returns false on any setup failure (the caller should fall back
     * to MJPEG).
     */
    fun configure(surface: Surface, csd: ByteArray, width: Int, height: Int): Boolean {
        if (stopped) return false
        val safeW = width.coerceAtLeast(16)
        val safeH = height.coerceAtLeast(16)
        return try {
            val c = MediaCodec.createDecoderByType(MIME)
            val format = MediaFormat.createVideoFormat(MIME, safeW, safeH).apply {
                // csd-0 = annex-B SPS+PPS blob exactly as the encoder
                // emitted it (BUFFER_FLAG_CODEC_CONFIG output).
                setByteBuffer("csd-0", ByteBuffer.wrap(csd))
            }
            c.setCallback(object : MediaCodec.Callback() {
                override fun onInputBufferAvailable(codec: MediaCodec, index: Int) {
                    // Available input buffers are claimed inline in
                    // feed(); nothing to do here.
                }
                override fun onOutputBufferAvailable(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
                    if (stopped) {
                        try { codec.releaseOutputBuffer(index, false) } catch (_: Throwable) {}
                        return
                    }
                    try {
                        // render=true makes the decoder push the
                        // frame to the configured surface; no CPU
                        // copy through Java land.
                        codec.releaseOutputBuffer(index, info.size > 0)
                    } catch (e: Throwable) {
                        Log.w(TAG, "releaseOutputBuffer threw: $e")
                    }
                }
                override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) {
                    Log.w(TAG, "decoder error: ${e.diagnosticInfo}")
                }
                override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
                    Log.i(TAG, "decoder output format: $format")
                }
            }, workHandler)
            c.configure(format, surface, null, 0)
            c.start()
            codec = c
            true
        } catch (e: Throwable) {
            Log.w(TAG, "decoder configure failed: ${e.javaClass.simpleName}: ${e.message}")
            false
        }
    }

    /**
     * Feed one access unit (annex-B NAL bytes from a
     * stream_chunk_header binary frame). Non-blocking: returns false
     * if there's no input buffer available right now, which means
     * we're CPU-bound and dropping is preferable to back-pressuring
     * the WS reader.
     */
    fun feed(nalBytes: ByteArray, isKeyFrame: Boolean): Boolean {
        val c = codec ?: return false
        if (stopped) return false
        return try {
            // dequeueInputBuffer with a 0 ms timeout: don't block.
            val index = c.dequeueInputBuffer(0L)
            if (index < 0) return false
            val buf = c.getInputBuffer(index) ?: return false
            buf.clear()
            buf.put(nalBytes)
            val flags = if (isKeyFrame) MediaCodec.BUFFER_FLAG_KEY_FRAME else 0
            ptsCounter += 33_333  // ~30 fps; values just need to be monotonic
            c.queueInputBuffer(index, 0, nalBytes.size, ptsCounter, flags)
            true
        } catch (e: Throwable) {
            Log.w(TAG, "feed threw: ${e.javaClass.simpleName}: ${e.message}")
            false
        }
    }

    /** Idempotent tear-down. */
    fun stop() {
        stopped = true
        workHandler.post {
            try { codec?.flush() } catch (_: Throwable) {}
            try { codec?.stop() } catch (_: Throwable) {}
            try { codec?.release() } catch (_: Throwable) {}
            codec = null
        }
        try { workThread.quitSafely() } catch (_: Throwable) {}
    }

    companion object {
        private const val TAG = "ScreenH264Decoder"
        private const val MIME = "video/avc"
    }
}
