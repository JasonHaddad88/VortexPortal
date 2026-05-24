package com.vortex.driver

import android.content.Context
import android.content.Intent
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.HandlerThread
import android.util.DisplayMetrics
import android.util.Log
import android.view.Surface
import android.view.WindowManager

/**
 * Hardware H.264 encoder for the screen capture path (B5).
 *
 * Pipeline:
 *
 *   MediaProjection -> VirtualDisplay -> MediaCodec input Surface -> NAL units
 *
 * Pro vs. the JPEG path:
 *   - Roughly an order of magnitude less bandwidth at the same perceived
 *     quality (inter-frame coding instead of per-frame JPEG).
 *   - GPU/hardware encoder, so the Java/Kotlin side spends almost no CPU.
 *   - Browser decode via WebCodecs is hardware-accelerated too.
 *
 * Output protocol:
 *   - [NalSink.onCodecConfig] fires once with the SPS+PPS as a single
 *     annex-B byte blob (the codec config; same shape MediaCodec emits
 *     with `BUFFER_FLAG_CODEC_CONFIG`). The op handler base64-encodes
 *     this into `stream_start.csd_base64` so the browser can configure
 *     its VideoDecoder BEFORE any frames arrive.
 *   - [NalSink.onFrame] fires per access unit (annex-B prefixed NAL
 *     units), with `isKeyFrame` + `ptsMicros`. Op handler encodes those
 *     onto the stream_chunk_header text frame as `kf` + `pts`.
 *   - [NalSink.onError] for any setup or runtime failure.
 *
 * Lifecycle mirrors [ScreenEngine]: [start] kicks off the pipeline,
 * [stop] tears it down. The MediaCodec callback is bound to a dedicated
 * HandlerThread so the WS dispatch thread never blocks on encode work.
 */
class ScreenH264Encoder(
    private val context: Context,
    private val resultCode: Int,
    private val resultData: Intent,
    private val maxDimension: Int = 720,
    private val bitrateBps: Int = 1_500_000,
    private val fpsCap: Int = 30,
    private val keyFrameIntervalSec: Int = 1,
) {
    interface NalSink {
        fun onCodecConfig(csdBytes: ByteArray, width: Int, height: Int, codecString: String)
        fun onFrame(nalBytes: ByteArray, isKeyFrame: Boolean, ptsMicros: Long)
        fun onError(message: String)
    }

    private val workThread = HandlerThread("ScreenH264").apply { start() }
    private val workHandler = Handler(workThread.looper)

    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var codec: MediaCodec? = null
    private var inputSurface: Surface? = null
    private var sink: NalSink? = null
    @Volatile private var stopRequested = false
    @Volatile private var widthPx = 0
    @Volatile private var heightPx = 0

    fun start(sink: NalSink) {
        this.sink = sink
        workHandler.post { openInternal(sink) }
    }

    fun stop() {
        stopRequested = true
        workHandler.post {
            try { codec?.signalEndOfInputStream() } catch (_: Exception) {}
            try { codec?.stop() } catch (_: Exception) {}
            try { codec?.release() } catch (_: Exception) {}
            try { inputSurface?.release() } catch (_: Exception) {}
            try { virtualDisplay?.release() } catch (_: Exception) {}
            try { projection?.stop() } catch (_: Exception) {}
            codec = null; inputSurface = null; virtualDisplay = null
            projection = null; sink = null
        }
    }

    private fun openInternal(sink: NalSink) {
        val (w, h, dpi) = pickCaptureSize()
        widthPx = w; heightPx = h
        try {
            val format = MediaFormat.createVideoFormat(MIME, w, h).apply {
                setInteger(MediaFormat.KEY_COLOR_FORMAT,
                           MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
                setInteger(MediaFormat.KEY_BIT_RATE, bitrateBps)
                setInteger(MediaFormat.KEY_FRAME_RATE, fpsCap.coerceAtLeast(15))
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, keyFrameIntervalSec)
                // Baseline level 3.1 -- widest WebCodecs support; H.264
                // baseline has no B-frames, which also means lowest decode
                // latency. If the encoder rejects the profile we fall back
                // to the default below.
                setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline)
                setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel31)
            }
            val c = MediaCodec.createEncoderByType(MIME)
            try {
                c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            } catch (_: Exception) {
                // Some encoders refuse explicit profile/level -- retry without.
                format.containsKey(MediaFormat.KEY_PROFILE).also {
                    format.setInteger(MediaFormat.KEY_PROFILE, 0)
                    format.setInteger(MediaFormat.KEY_LEVEL, 0)
                }
                c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            }
            inputSurface = c.createInputSurface()
            c.setCallback(object : MediaCodec.Callback() {
                override fun onInputBufferAvailable(codec: MediaCodec, index: Int) {
                    // Surface-input encoders never have CPU-fed input buffers.
                }
                override fun onOutputBufferAvailable(codec: MediaCodec, index: Int, info: MediaCodec.BufferInfo) {
                    if (stopRequested) {
                        try { codec.releaseOutputBuffer(index, false) } catch (_: Exception) {}
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
                        val isKey    = (info.flags and MediaCodec.BUFFER_FLAG_KEY_FRAME)   != 0
                        if (isConfig) {
                            val codecString = avcCodecString(out)
                            sink.onCodecConfig(out, widthPx, heightPx, codecString)
                        } else {
                            sink.onFrame(out, isKey, info.presentationTimeUs)
                        }
                        codec.releaseOutputBuffer(index, false)
                    } catch (e: Throwable) {
                        Log.w(TAG, "output buffer handling failed: $e")
                        sink.onError("H.264 output failed: ${e.javaClass.simpleName}: ${e.message}")
                    }
                }
                override fun onError(codec: MediaCodec, e: MediaCodec.CodecException) {
                    sink.onError("MediaCodec error: ${e.diagnosticInfo}")
                }
                override fun onOutputFormatChanged(codec: MediaCodec, format: MediaFormat) {
                    Log.i(TAG, "encoder output format: $format")
                }
            }, workHandler)
            c.start()
            codec = c

            val mgr = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE)
                      as? MediaProjectionManager
                ?: run { sink.onError("MEDIA_PROJECTION_SERVICE unavailable"); return }
            val mp = mgr.getMediaProjection(resultCode, resultData)
            projection = mp
            mp.registerCallback(object : MediaProjection.Callback() {
                override fun onStop() {
                    sink.onError("Screen sharing was revoked from the system UI")
                    stop()
                }
            }, workHandler)
            virtualDisplay = mp.createVirtualDisplay(
                "VortexScreenH264", w, h, dpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                inputSurface, null, workHandler,
            )
            Log.i(TAG, "H.264 encoder up at ${w}x${h}@${dpi}dpi, ${bitrateBps / 1000} kbps")
        } catch (e: Throwable) {
            sink.onError("H.264 encoder setup failed: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    /**
     * Parse the AVC SPS NAL out of the CSD and emit an `avc1.PPCCLL`
     * codec string the browser's VideoDecoder.configure can take.
     * CSD format: 0x00000001 SPS 0x00000001 PPS; SPS bytes 1..3 carry
     * profile_idc, constraint flags, level_idc.
     */
    private fun avcCodecString(csd: ByteArray): String {
        var i = 0
        while (i <= csd.size - 5) {
            // 3-byte (0x000001) or 4-byte (0x00000001) annex-B start code.
            val sc4 = i + 4 < csd.size &&
                csd[i] == 0x00.toByte() && csd[i+1] == 0x00.toByte() &&
                csd[i+2] == 0x00.toByte() && csd[i+3] == 0x01.toByte()
            val sc3 = csd[i] == 0x00.toByte() && csd[i+1] == 0x00.toByte() &&
                      csd[i+2] == 0x01.toByte()
            val hdrLen = when { sc4 -> 4; sc3 -> 3; else -> 0 }
            if (hdrLen == 0) { i++; continue }
            val nalType = csd[i + hdrLen].toInt() and 0x1F
            if (nalType == 7 && i + hdrLen + 3 < csd.size) {
                val profile = csd[i + hdrLen + 1].toInt() and 0xFF
                val constraints = csd[i + hdrLen + 2].toInt() and 0xFF
                val level = csd[i + hdrLen + 3].toInt() and 0xFF
                return "avc1.%02X%02X%02X".format(profile, constraints, level)
            }
            i += hdrLen
        }
        return "avc1.42E01E"  // baseline 3.0 default
    }

    private fun pickCaptureSize(): Triple<Int, Int, Int> {
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val realW: Int; val realH: Int; val realDpi: Int
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            val bounds = wm.currentWindowMetrics.bounds
            realW = bounds.width(); realH = bounds.height()
            realDpi = context.resources.configuration.densityDpi
        } else {
            @Suppress("DEPRECATION") val display = wm.defaultDisplay
            val metrics = DisplayMetrics()
            @Suppress("DEPRECATION") display.getRealMetrics(metrics)
            realW = metrics.widthPixels; realH = metrics.heightPixels
            realDpi = metrics.densityDpi
        }
        val longest = maxOf(realW, realH)
        if (longest <= maxDimension) {
            // Still align to 16 (macroblock); H.264 encoders are happier.
            val w = realW and 0x7FFFFFF0
            val h = realH and 0x7FFFFFF0
            return Triple(w, h, realDpi)
        }
        val scale = maxDimension.toDouble() / longest
        val w = (realW * scale).toInt() and 0x7FFFFFF0
        val h = (realH * scale).toInt() and 0x7FFFFFF0
        val dpi = (realDpi * scale).toInt().coerceAtLeast(120)
        return Triple(w, h, dpi)
    }

    companion object {
        private const val TAG = "ScreenH264Encoder"
        private const val MIME = "video/avc"
    }
}
