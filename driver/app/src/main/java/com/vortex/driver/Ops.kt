package com.vortex.driver

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.camera2.CameraCharacteristics
import android.os.BatteryManager
import android.os.Build
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.delay
import org.json.JSONArray
import org.json.JSONObject

/**
 * Native ops for the APK's standalone client role.
 *
 *   B1   -- device_info (no permissions)
 *   B2.1 -- input (AccessibilityService dispatch, no loopback hop)
 *   B2.2 -- screen_stream + camera_stream (native streams via the
 *           same engines the loopback StreamServer drives, but
 *           routed straight into HubClient WebSocket frames)
 */
object Ops {

    fun registerAll(ctx: Context, dispatcher: OpDispatcher) {
        dispatcher.register("device_info") { _ -> deviceInfo(ctx) }
        // B2.1: native input -- same dispatch as InputServer but invoked
        // directly by the hub's WS, no Termux/loopback hop.
        dispatcher.register("input") { args ->
            val cmd = args.optJSONObject("command")
                ?: throw RuntimeException("op_input expects args.command to be an object")
            InputDispatch.dispatchInput(ctx, cmd)
                ?: JSONObject().put("acked", true)
        }
        // B2.2: native screen + camera streams. Handler runs in a HubClient
        // coroutine; cancellation tears down the engine via the finally.
        dispatcher.registerStream("screen_stream") { _, sink ->
            runNativeStream(StreamKind.SCREEN, sink, args = null)
        }
        dispatcher.registerStream("camera_stream") { args, sink ->
            runNativeStream(StreamKind.CAMERA, sink, args)
        }
    }

    private enum class StreamKind { SCREEN, CAMERA }

    /** Shared driver between screen_stream and camera_stream -- they only
     *  differ in which engine they start. */
    private suspend fun runNativeStream(
        kind: StreamKind,
        sink: WsStreamSink,
        args: JSONObject?,
    ) {
        val svc = DriverService.instance
            ?: throw RuntimeException("Vortex Driver service is not running")

        // Bridge from the engine's FrameSink callbacks back into the
        // coroutine. Errors complete `failure` so the suspending await
        // wakes up and we can rethrow with a clean message.
        val failure = CompletableDeferred<Throwable>()
        val engineSink = object : CameraEngine.FrameSink {
            override fun onFrame(jpegBytes: ByteArray, width: Int, height: Int, sensorRotation: Int) {
                // sink.sendChunk swallows transport errors -- if the WS is
                // gone the HubClient will cancel us shortly anyway.
                sink.sendChunk(jpegBytes)
            }
            override fun onError(message: String) {
                if (!failure.isCompleted) failure.complete(RuntimeException(message))
            }
        }

        // Start the engine BEFORE stream_start so a setup failure surfaces
        // as a normal {ok:false} response (same contract as the Python
        // agent's op_camera_stream).
        when (kind) {
            StreamKind.SCREEN -> svc.startNativeScreenStream(engineSink)
            StreamKind.CAMERA -> {
                val facing = when (args?.optString("facing", "back")) {
                    "front" -> CameraCharacteristics.LENS_FACING_FRONT
                    else    -> CameraCharacteristics.LENS_FACING_BACK
                }
                svc.startNativeCameraStream(engineSink, facing)
            }
        }

        sink.sendStart(contentType = "image/jpeg")

        try {
            // Either the engine reports an error first, or the surrounding
            // coroutine gets cancelled (WS closed). Whichever fires first
            // wins -- we await both via a tiny poll loop so the cancellation
            // still works while we wait on `failure`.
            while (true) {
                if (failure.isCompleted) throw failure.getCompleted()
                delay(100)
            }
        } finally {
            when (kind) {
                StreamKind.SCREEN -> try { svc.stopNativeScreenStream() } catch (_: Exception) {}
                StreamKind.CAMERA -> try { svc.stopNativeCameraStream() } catch (_: Exception) {}
            }
        }
    }

    /** Native equivalent of the Python agent's `op_device_info` -- no
     *  Termux:API, no shells. Pure Android Build + BatteryManager. */
    private fun deviceInfo(ctx: Context): JSONObject {
        val out = JSONObject()
        out.put("model", Build.MODEL ?: "")
        out.put("manufacturer", Build.MANUFACTURER ?: "")
        out.put("device", Build.DEVICE ?: "")
        out.put("brand", Build.BRAND ?: "")
        out.put("android_release", Build.VERSION.RELEASE ?: "")
        out.put("android_sdk", Build.VERSION.SDK_INT)
        out.put("hardware", Build.HARDWARE ?: "")
        out.put("abis", JSONArray(Build.SUPPORTED_ABIS?.toList() ?: emptyList<String>()))

        // Battery (BatteryManager works without RECEIVER_NOT_EXPORTED).
        try {
            val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager
            if (bm != null) {
                val pct = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
                if (pct in 0..100) out.put("battery_percent", pct)
            }
            val stickyIntent = ctx.registerReceiver(
                null, IntentFilter(Intent.ACTION_BATTERY_CHANGED),
            )
            val status = stickyIntent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
            val plugged = stickyIntent?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
            out.put("battery_status", when (status) {
                BatteryManager.BATTERY_STATUS_CHARGING -> "charging"
                BatteryManager.BATTERY_STATUS_DISCHARGING -> "discharging"
                BatteryManager.BATTERY_STATUS_FULL -> "full"
                BatteryManager.BATTERY_STATUS_NOT_CHARGING -> "not_charging"
                else -> "unknown"
            })
            out.put("battery_plugged", plugged != 0)
        } catch (_: Exception) { /* best-effort */ }

        out.put("agent", "vortex-driver-apk")
        out.put("agent_version", BuildConfig.VERSION_NAME)
        return out
    }
}
