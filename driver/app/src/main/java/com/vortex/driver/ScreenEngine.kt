package com.vortex.driver

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Handler
import android.os.HandlerThread
import android.util.DisplayMetrics
import android.util.Log
import android.view.Display
import android.view.WindowManager
import java.io.ByteArrayOutputStream

/**
 * Captures the device screen via [MediaProjection] and emits JPEG frames
 * via [CameraEngine.FrameSink] (same interface as the camera; downstream
 * consumers don't care about the source).
 *
 * Consent contract: the caller MUST have a valid (resultCode, resultData)
 * pair from [MediaProjectionManager.createScreenCaptureIntent] -> Activity
 * result. The dialog can ONLY be summoned from an Activity context, so
 * we get it via [ScreenSetupActivity] and hand it to [DriverService] which
 * passes it here.
 *
 * Lifecycle: [start] creates the MediaProjection + VirtualDisplay +
 * ImageReader. [stop] tears them all down and *releases the projection*,
 * which means the user must consent again to start a new session. That's
 * the right default for privacy -- a stale grant lingering in memory is
 * a footgun.
 */
class ScreenEngine(
    private val context: Context,
    private val resultCode: Int,
    private val resultData: Intent,
    /** Longest side of the captured frame, in pixels. We downscale to keep
     *  bandwidth + JPEG encode time reasonable. 720 by default; can be
     *  raised by the request args up to 1080 on capable phones. */
    private val maxDimension: Int = 720,
    private val jpegQuality: Int = 50,
    /** Frames per second cap. <=0 means unlimited. Default 30 -- screen
     *  content rarely benefits from higher (the UI compositor caps repaint
     *  anyway) and we save a lot of CPU + Bitmap allocations. */
    private val fpsCap: Int = 30,
    /** Pre-encode backpressure gate (HubClient passes a WS-queue probe). */
    private val readyToEmit: () -> Boolean = { true },
) {
    private val workThread = HandlerThread("ScreenEngine").apply { start() }
    private val workHandler = Handler(workThread.looper)

    private var projection: MediaProjection? = null
    private var virtualDisplay: VirtualDisplay? = null
    private var imageReader: ImageReader? = null
    private var sink: CameraEngine.FrameSink? = null
    private val minFrameIntervalNanos: Long =
        if (fpsCap > 0) 1_000_000_000L / fpsCap else 0L
    @Volatile private var lastEmitNanos: Long = 0L

    fun start(sink: CameraEngine.FrameSink) {
        this.sink = sink
        workHandler.post { openInternal(sink) }
    }

    fun stop() {
        workHandler.post {
            try { virtualDisplay?.release() } catch (_: Exception) {}
            try { imageReader?.close() } catch (_: Exception) {}
            try { projection?.stop() } catch (_: Exception) {}
            virtualDisplay = null
            imageReader = null
            projection = null
            sink = null
        }
    }

    private fun openInternal(sink: CameraEngine.FrameSink) {
        val mgr = context.getSystemService(Context.MEDIA_PROJECTION_SERVICE)
                  as? MediaProjectionManager
            ?: run { sink.onError("MEDIA_PROJECTION_SERVICE unavailable"); return }

        // getMediaProjection requires the host service to already be in
        // foreground with FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION on
        // Android 14+. DriverService takes care of that before instantiating
        // us; if it didn't, this throws SecurityException.
        val mp = try {
            mgr.getMediaProjection(resultCode, resultData)
        } catch (e: SecurityException) {
            sink.onError(
                "MediaProjection denied: ${e.message ?: "no message"}. " +
                "On Android 14+ the foreground service must declare " +
                "FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION before this call."
            )
            return
        } catch (e: Throwable) {
            sink.onError("getMediaProjection threw: ${e::class.simpleName}: ${e.message}")
            return
        }
        projection = mp

        // If the user revokes from the system UI ("Stop sharing" notification),
        // we get this callback. Tear down so we don't keep a dead projection.
        mp.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() {
                Log.i(TAG, "MediaProjection stopped externally")
                this@ScreenEngine.sink?.onError("Screen sharing was revoked from the system UI")
                stop()
            }
        }, workHandler)

        val (w, h, dpi) = pickCaptureSize()
        Log.i(TAG, "Capturing at ${w}x${h}@${dpi}dpi")

        val reader = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, /* maxImages = */ 2)
        imageReader = reader
        reader.setOnImageAvailableListener({ r ->
            val image = r.acquireLatestImage() ?: return@setOnImageAvailableListener
            try {
                if (!shouldEmitNow()) return@setOnImageAvailableListener
                val jpeg = encodeRgbaToJpeg(image, jpegQuality)
                sink.onFrame(jpeg, image.width, image.height, /* sensorRotation = */ 0)
                lastEmitNanos = System.nanoTime()
            } catch (e: Throwable) {
                sink.onError("screen frame encode failed: ${e::class.simpleName}: ${e.message}")
            } finally {
                image.close()
            }
        }, workHandler)

        try {
            virtualDisplay = mp.createVirtualDisplay(
                "VortexScreen",
                w, h, dpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                reader.surface,
                /* callback = */ null,
                workHandler,
            )
        } catch (e: Throwable) {
            sink.onError("createVirtualDisplay threw: ${e::class.simpleName}: ${e.message}")
        }
    }

    private fun shouldEmitNow(): Boolean {
        if (!readyToEmit()) return false
        if (minFrameIntervalNanos <= 0L) return true
        return (System.nanoTime() - lastEmitNanos) >= minFrameIntervalNanos
    }

    /**
     * Downscale the real screen to keep frame size manageable. We preserve
     * aspect ratio by capping the longest side to [maxDimension]. Density
     * is interpolated linearly so UI elements scale proportionally and
     * we don't end up with tiny text on a low-DPI virtual display.
     */
    private fun pickCaptureSize(): Triple<Int, Int, Int> {
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val realW: Int
        val realH: Int
        val realDpi: Int
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            val bounds = wm.currentWindowMetrics.bounds
            realW = bounds.width()
            realH = bounds.height()
            realDpi = context.resources.configuration.densityDpi
        } else {
            @Suppress("DEPRECATION")
            val display: Display = wm.defaultDisplay
            val metrics = DisplayMetrics()
            @Suppress("DEPRECATION")
            display.getRealMetrics(metrics)
            realW = metrics.widthPixels
            realH = metrics.heightPixels
            realDpi = metrics.densityDpi
        }
        val longest = maxOf(realW, realH)
        if (longest <= maxDimension) {
            return Triple(realW, realH, realDpi)
        }
        val scale = maxDimension.toDouble() / longest
        val w = (realW * scale).toInt() and 0x7FFFFFFE   // even (encoder hates odd dims)
        val h = (realH * scale).toInt() and 0x7FFFFFFE
        val dpi = (realDpi * scale).toInt().coerceAtLeast(120)
        return Triple(w, h, dpi)
    }

    private fun encodeRgbaToJpeg(image: Image, quality: Int): ByteArray {
        val w = image.width
        val h = image.height
        val plane = image.planes[0]
        val buffer = plane.buffer
        val pixelStride = plane.pixelStride        // typically 4
        val rowStride = plane.rowStride            // may exceed w * pixelStride
        val rowPadding = rowStride - pixelStride * w

        // Bitmap row width = our requested width + padding pixels expressed
        // back as count. We'll crop the padding off below.
        val paddedWidth = w + rowPadding / pixelStride
        val bitmap = Bitmap.createBitmap(paddedWidth, h, Bitmap.Config.ARGB_8888)
        bitmap.copyPixelsFromBuffer(buffer)

        val cropped = if (rowPadding == 0) bitmap
                      else Bitmap.createBitmap(bitmap, 0, 0, w, h)
        val baos = ByteArrayOutputStream(w * h / 8)
        cropped.compress(Bitmap.CompressFormat.JPEG, quality, baos)
        if (cropped !== bitmap) cropped.recycle()
        bitmap.recycle()
        return baos.toByteArray()
    }

    companion object { private const val TAG = "ScreenEngine" }
}
