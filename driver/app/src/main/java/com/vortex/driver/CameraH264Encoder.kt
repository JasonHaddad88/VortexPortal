package com.vortex.driver

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.OutputConfiguration
import android.hardware.camera2.params.SessionConfiguration
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.util.Size
import android.view.Surface
import androidx.core.content.ContextCompat

/**
 * Hardware H.264 encoder for the camera capture path (B5.1).
 *
 * Pipeline:
 *
 *   Camera2 capture session (TEMPLATE_RECORD) -> MediaCodec input Surface -> NAL units
 *
 * Counterpart to [ScreenH264Encoder] -- same output protocol
 * ([NalSink]: onCodecConfig once, then onFrame per access unit), so
 * the [Ops] handler can reuse exactly the same wire-format code path
 * regardless of source.
 *
 * Pro vs the JPEG/MJPEG path:
 *   - Orders-of-magnitude less bandwidth at the same perceived quality
 *     (inter-frame coding instead of per-frame JPEG).
 *   - Hardware encoder, so the CPU isn't burning on YUV->JPEG conversion.
 *   - Browser decode is hardware-accelerated via WebCodecs.
 */
class CameraH264Encoder(
    private val context: Context,
    private var cameraFacing: Int = CameraCharacteristics.LENS_FACING_BACK,
    /** Target preferred resolution. Encoder will round to the nearest
     *  size the camera actually supports. Default 720p / 16:9. */
    private val targetSize: Size = Size(1280, 720),
    private val bitrateBps: Int = 2_000_000,
    private val fpsCap: Int = 30,
    private val keyFrameIntervalSec: Int = 1,
) {
    /** Same sink contract as ScreenH264Encoder. */
    interface NalSink {
        fun onCodecConfig(csdBytes: ByteArray, width: Int, height: Int, codecString: String)
        fun onFrame(nalBytes: ByteArray, isKeyFrame: Boolean, ptsMicros: Long)
        fun onError(message: String)
    }

    private val cameraManager =
        context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    private val workThread = HandlerThread("CameraH264").apply { start() }
    private val workHandler = Handler(workThread.looper)

    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var codec: MediaCodec? = null
    private var inputSurface: Surface? = null
    private var sink: NalSink? = null
    @Volatile private var stopRequested = false
    @Volatile private var widthPx = 0
    @Volatile private var heightPx = 0

    fun start(sink: NalSink) {
        this.sink = sink
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            sink.onError("CAMERA permission not granted")
            return
        }
        workHandler.post { openInternal(sink) }
    }

    fun stop() {
        stopRequested = true
        workHandler.post {
            try { session?.close() } catch (_: Exception) {}
            try { device?.close() } catch (_: Exception) {}
            try { codec?.signalEndOfInputStream() } catch (_: Exception) {}
            try { codec?.stop() } catch (_: Exception) {}
            try { codec?.release() } catch (_: Exception) {}
            try { inputSurface?.release() } catch (_: Exception) {}
            session = null; device = null
            codec = null; inputSurface = null
            sink = null
        }
    }

    private fun openInternal(sink: NalSink) {
        val cameraId = pickCameraId() ?: run {
            sink.onError("No camera with facing=$cameraFacing on this device"); return
        }
        // Pick the best supported size near our target. Falls back to the
        // requested target if the characteristics query goes sideways.
        val pickedSize = pickEncoderSize(cameraId) ?: targetSize
        widthPx = pickedSize.width
        heightPx = pickedSize.height

        try {
            val format = MediaFormat.createVideoFormat(MIME, widthPx, heightPx).apply {
                setInteger(MediaFormat.KEY_COLOR_FORMAT,
                           MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
                setInteger(MediaFormat.KEY_BIT_RATE, bitrateBps)
                setInteger(MediaFormat.KEY_FRAME_RATE, fpsCap.coerceAtLeast(15))
                setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, keyFrameIntervalSec)
                // Baseline / 3.1: widest WebCodecs support, no B-frames =
                // lowest decode latency. Some encoders refuse explicit
                // profile/level; we retry without on failure.
                setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline)
                setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel31)
            }
            val c = MediaCodec.createEncoderByType(MIME)
            try {
                c.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            } catch (_: Exception) {
                format.setInteger(MediaFormat.KEY_PROFILE, 0)
                format.setInteger(MediaFormat.KEY_LEVEL, 0)
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

            openCamera(cameraId, sink)
        } catch (e: SecurityException) {
            sink.onError("CAMERA permission denied at openCamera")
        } catch (e: Throwable) {
            sink.onError("H.264 encoder setup failed: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun openCamera(cameraId: String, sink: NalSink) {
        try {
            cameraManager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(d: CameraDevice) {
                    device = d
                    startSession(d, sink)
                }
                override fun onDisconnected(d: CameraDevice) {
                    d.close(); device = null
                }
                override fun onError(d: CameraDevice, e: Int) {
                    d.close(); device = null
                    sink.onError("Camera open error: code=$e")
                }
            }, workHandler)
        } catch (e: SecurityException) {
            sink.onError("CAMERA permission denied at openCamera")
        } catch (e: Throwable) {
            sink.onError("openCamera threw: ${e::class.simpleName}: ${e.message}")
        }
    }

    private fun startSession(d: CameraDevice, sink: NalSink) {
        val surf = inputSurface ?: run { sink.onError("encoder input surface is null"); return }
        val builder = d.createCaptureRequest(CameraDevice.TEMPLATE_RECORD)
        builder.addTarget(surf)
        builder.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
        builder.set(CaptureRequest.CONTROL_AF_MODE, CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
        // Cap FPS via AE target range so the camera doesn't push more
        // frames than the encoder asked for (saves both pipeline pressure
        // and battery on phones whose HAL defaults to 60 fps).
        val fpsCapped = fpsCap.coerceAtLeast(15)
        builder.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE,
                    android.util.Range(fpsCapped, fpsCapped))

        val onConfigured = object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                session = s
                try {
                    s.setRepeatingRequest(builder.build(), null, workHandler)
                } catch (e: Throwable) {
                    sink.onError("setRepeatingRequest failed: $e")
                }
            }
            override fun onConfigureFailed(s: CameraCaptureSession) {
                sink.onError("Camera capture session configure failed")
            }
        }
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                val out = OutputConfiguration(surf)
                val cfg = SessionConfiguration(
                    SessionConfiguration.SESSION_REGULAR,
                    listOf(out),
                    { it.run() },          // run on caller; we serialize via workHandler from there
                    onConfigured,
                )
                d.createCaptureSession(cfg)
            } else {
                @Suppress("DEPRECATION")
                d.createCaptureSession(listOf(surf), onConfigured, workHandler)
            }
        } catch (e: Throwable) {
            sink.onError("createCaptureSession threw: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    private fun pickCameraId(): String? = try {
        cameraManager.cameraIdList.firstOrNull { id ->
            cameraManager.getCameraCharacteristics(id)
                .get(CameraCharacteristics.LENS_FACING) == cameraFacing
        }
    } catch (e: Throwable) {
        Log.w(TAG, "pickCameraId failed: $e"); null
    }

    private fun pickEncoderSize(cameraId: String): Size? {
        return try {
            val chars = cameraManager.getCameraCharacteristics(cameraId)
            val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
                ?: return null
            // SurfaceTexture as a proxy for "what the encoder surface can take"
            // -- StreamConfigurationMap doesn't expose MediaCodec.class on every
            // API level, but SurfaceTexture is always present and is the same
            // format class internally.
            val sizes = map.getOutputSizes(android.graphics.SurfaceTexture::class.java)
                ?: return null
            // Prefer 16:9 sizes closest to (but not exceeding) the target.
            val target = targetSize
            val want = target.width.toLong() * target.height.toLong()
            sizes.filter { it.width >= it.height }
                .minByOrNull { kotlin.math.abs(it.width.toLong() * it.height.toLong() - want) }
        } catch (_: Throwable) { null }
    }

    /** Same SPS-parsing helper as ScreenH264Encoder. */
    private fun avcCodecString(csd: ByteArray): String {
        var i = 0
        while (i <= csd.size - 5) {
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
        return "avc1.42E01E"
    }

    companion object {
        private const val TAG = "CameraH264Encoder"
        private const val MIME = "video/avc"
    }
}
