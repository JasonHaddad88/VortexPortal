package com.vortex.driver

import android.content.Context
import android.hardware.camera2.CameraCharacteristics
import android.util.Size
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * Native equivalent of the Python agent's `op_camera_capture`. Takes
 * one photo and returns the JPEG bytes. Reuses [CameraEngine]'s
 * Camera2 plumbing -- starts a session, grabs the first frame, stops.
 *
 * Why not a dedicated single-shot path: CameraEngine already handles
 * camera selection, YUV->NV21->JPEG, and HAL quirks. A one-frame
 * stream is ~200 ms slower than a true still-capture session but the
 * latency is dominated by HAL warm-up either way, and reusing the
 * engine keeps the code surface small.
 */
object CameraCaptureOp {

    /**
     * Capture one JPEG.
     *
     * @param facing CameraCharacteristics.LENS_FACING_FRONT / _BACK
     * @param maxDim longest side of the captured image (clamped 320..1920)
     * @param quality JPEG quality 10..95
     * @param timeoutMs how long to wait for the first frame before giving up
     */
    suspend fun captureOne(
        ctx: Context,
        facing: Int = CameraCharacteristics.LENS_FACING_BACK,
        maxDim: Int = 1080,
        quality: Int = 85,
        timeoutMs: Long = 8_000L,
    ): ByteArray = suspendCancellableCoroutine { cont ->
        val md = maxDim.coerceIn(320, 1920)
        // 16:9 target; engine downscales to the nearest supported size.
        val target = if (md >= 1080) Size(1920, 1080) else Size(md * 16 / 9, md)
        val q = quality.coerceIn(10, 95)
        val engine = CameraEngine(
            context = ctx,
            cameraFacing = facing,
            targetSize = target,
            jpegQuality = q,
            fpsCap = 30,
        )
        val finished = java.util.concurrent.atomic.AtomicBoolean(false)

        // Independent timeout so a stuck HAL doesn't hang the WS coroutine
        // forever. The op's coroutine cancellation also tears the engine down.
        val timer = Thread {
            try { Thread.sleep(timeoutMs) } catch (_: InterruptedException) { return@Thread }
            if (finished.compareAndSet(false, true)) {
                try { engine.stop() } catch (_: Exception) {}
                if (cont.isActive) cont.resumeWithException(
                    RuntimeException("Camera capture timed out after ${timeoutMs}ms. " +
                                     "Is another app holding the camera?")
                )
            }
        }.apply { isDaemon = true; start() }

        engine.start(object : CameraEngine.FrameSink {
            override fun onFrame(jpegBytes: ByteArray, width: Int, height: Int, sensorRotation: Int) {
                if (finished.compareAndSet(false, true)) {
                    timer.interrupt()
                    try { engine.stop() } catch (_: Exception) {}
                    if (cont.isActive) cont.resume(jpegBytes)
                }
            }
            override fun onError(message: String) {
                if (finished.compareAndSet(false, true)) {
                    timer.interrupt()
                    try { engine.stop() } catch (_: Exception) {}
                    if (cont.isActive) cont.resumeWithException(RuntimeException(message))
                }
            }
        })

        cont.invokeOnCancellation {
            timer.interrupt()
            try { engine.stop() } catch (_: Exception) {}
        }
    }
}
