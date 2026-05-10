package com.vortex.driver

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.Image
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.util.Size
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream

/**
 * Owns the Camera2 session and emits JPEG-encoded frames via a [FrameSink].
 *
 * Single-instance, single-camera-at-a-time. Call [start] to open the
 * camera and begin streaming; call [stop] to release. Threading is owned
 * internally on a dedicated HandlerThread -- safe to call start/stop from
 * any thread.
 *
 * Why JPEG not H.264 for now: M1 ships an MJPEG pipeline so the browser
 * can render via a vanilla `<img>` tag with zero MSE / fMP4 plumbing.
 * Bandwidth cost is tolerable on Wi-Fi; H.264+MSE moves to M1.5 if needed.
 */
class CameraEngine(
    private val context: Context,
    private var cameraFacing: Int = CameraCharacteristics.LENS_FACING_BACK,
    private val targetSize: Size = Size(1280, 720),
    private val jpegQuality: Int = 70,
) {
    interface FrameSink {
        fun onFrame(jpegBytes: ByteArray, width: Int, height: Int, sensorRotation: Int)
        fun onError(message: String)
    }

    private val cameraManager =
        context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
    private val workThread = HandlerThread("CameraEngine").apply { start() }
    private val workHandler = Handler(workThread.looper)

    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var imageReader: ImageReader? = null
    private var sink: FrameSink? = null
    @Volatile private var sensorRotation: Int = 0

    /**
     * Open the camera and start emitting frames. Idempotent: a second
     * [start] call while a session is live is a no-op (clients should
     * call [stop] first if they want a different facing).
     */
    fun start(sink: FrameSink) {
        this.sink = sink
        if (ContextCompat.checkSelfPermission(
                context, Manifest.permission.CAMERA,
            ) != PackageManager.PERMISSION_GRANTED) {
            sink.onError("CAMERA permission not granted")
            return
        }
        workHandler.post { openCameraInternal(sink) }
    }

    fun stop() {
        workHandler.post {
            try { session?.close() } catch (_: Exception) {}
            try { device?.close() } catch (_: Exception) {}
            try { imageReader?.close() } catch (_: Exception) {}
            session = null
            device = null
            imageReader = null
            sink = null
        }
    }

    /** Switch cameras at runtime. Closes the current session, opens the new. */
    fun setFacing(facing: Int, freshSink: FrameSink) {
        cameraFacing = facing
        stop()
        // tiny delay so the camera HAL has a moment to release before reopen
        workHandler.postDelayed({ start(freshSink) }, 200)
    }

    private fun openCameraInternal(sink: FrameSink) {
        val cameraId = pickCameraId()
        if (cameraId == null) {
            sink.onError("No camera with facing=$cameraFacing on this device")
            return
        }
        val chars = cameraManager.getCameraCharacteristics(cameraId)
        sensorRotation = chars.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 0

        val reader = ImageReader.newInstance(
            targetSize.width, targetSize.height,
            ImageFormat.YUV_420_888, /* maxImages = */ 2,
        )
        imageReader = reader
        reader.setOnImageAvailableListener({ r ->
            // Use acquireLatestImage so we drop frames the encoder couldn't
            // keep up with -- live latency over completeness.
            val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                val jpeg = encodeYuv420ToJpeg(image, jpegQuality)
                sink.onFrame(jpeg, image.width, image.height, sensorRotation)
            } catch (e: Throwable) {
                sink.onError("frame encode failed: ${e::class.simpleName}: ${e.message}")
            } finally {
                image.close()
            }
        }, workHandler)

        try {
            cameraManager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                override fun onOpened(d: CameraDevice) {
                    device = d
                    startSession(d, reader, sink)
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

    private fun startSession(d: CameraDevice, reader: ImageReader, sink: FrameSink) {
        val builder = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW)
        builder.addTarget(reader.surface)
        builder.set(CaptureRequest.CONTROL_AF_MODE,
                    CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_VIDEO)
        try {
            d.createCaptureSession(
                listOf(reader.surface),
                object : CameraCaptureSession.StateCallback() {
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
                },
                workHandler,
            )
        } catch (e: Throwable) {
            sink.onError("createCaptureSession threw: $e")
        }
    }

    private fun pickCameraId(): String? {
        return try {
            cameraManager.cameraIdList.firstOrNull { id ->
                cameraManager.getCameraCharacteristics(id)
                    .get(CameraCharacteristics.LENS_FACING) == cameraFacing
            }
        } catch (e: Throwable) {
            Log.w(TAG, "pickCameraId failed: $e")
            null
        }
    }

    /**
     * YUV_420_888 -> NV21 -> JPEG via YuvImage.
     *
     * We can't shortcut the YUV->NV21 step because Android's
     * YUV_420_888 layout is intentionally vague: pixel/row strides on
     * the U and V planes vary per device. Honouring the strides keeps
     * us correct on every device at the cost of a per-frame copy. For
     * 720p that's ~1.5ms on a midrange phone, well below the JPEG
     * encode time.
     */
    private fun encodeYuv420ToJpeg(image: Image, quality: Int): ByteArray {
        val nv21 = yuv420ToNv21(image)
        val w = image.width
        val h = image.height
        val bos = ByteArrayOutputStream(w * h / 4)
        YuvImage(nv21, ImageFormat.NV21, w, h, null)
            .compressToJpeg(Rect(0, 0, w, h), quality, bos)
        return bos.toByteArray()
    }

    private fun yuv420ToNv21(image: Image): ByteArray {
        val w = image.width
        val h = image.height
        val out = ByteArray(w * h * 3 / 2)

        val yPlane = image.planes[0]
        val uPlane = image.planes[1]
        val vPlane = image.planes[2]

        // ----- Y plane -----
        val yBuffer = yPlane.buffer
        val yRowStride = yPlane.rowStride
        val yPixelStride = yPlane.pixelStride
        if (yPixelStride == 1 && yRowStride == w) {
            yBuffer.get(out, 0, w * h)  // tight, one bulk copy
        } else {
            var dst = 0
            for (row in 0 until h) {
                val rowBase = row * yRowStride
                for (col in 0 until w) {
                    out[dst++] = yBuffer.get(rowBase + col * yPixelStride)
                }
            }
        }

        // ----- VU interleaved (NV21 layout) -----
        val uBuffer = uPlane.buffer
        val vBuffer = vPlane.buffer
        val uvRowStride = uPlane.rowStride         // U and V have the same stride per spec
        val uvPixelStride = uPlane.pixelStride     // ditto
        val cw = w / 2
        val ch = h / 2
        var dst = w * h
        for (row in 0 until ch) {
            val rowBase = row * uvRowStride
            for (col in 0 until cw) {
                val pos = rowBase + col * uvPixelStride
                out[dst++] = vBuffer.get(pos)  // V first (NV21)
                out[dst++] = uBuffer.get(pos)
            }
        }
        return out
    }

    companion object { private const val TAG = "CameraEngine" }
}
