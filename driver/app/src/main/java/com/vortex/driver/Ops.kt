package com.vortex.driver

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.camera2.CameraCharacteristics
import android.os.BatteryManager
import android.os.Build
import android.util.Base64
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
        // B5: screen_stream also branches on args.codec="h264" -> H.264
        // via MediaCodec instead of MJPEG. camera_stream stays JPEG for now.
        dispatcher.registerStream("screen_stream") { args, sink ->
            val codec = args?.optString("codec", "mjpeg") ?: "mjpeg"
            if (codec.equals("h264", ignoreCase = true)) {
                runScreenH264Stream(sink, args)
            } else {
                runNativeStream(StreamKind.SCREEN, sink, args)
            }
        }
        dispatcher.registerStream("camera_stream") { args, sink ->
            runNativeStream(StreamKind.CAMERA, sink, args)
        }
        // B4: native theft-mode ops -- replaces the Termux:API surface on
        // Android. Wire shapes match the Python agent so the hub's Theft
        // Mode UI (and Theft Dashboard) need zero changes.
        registerB4(ctx, dispatcher)
    }

    private fun registerB4(ctx: Context, dispatcher: OpDispatcher) {
        // keepawake: unary; PARTIAL_WAKE_LOCK acquire/release.
        dispatcher.register("keepawake") { args ->
            WakeLockOp.set(ctx, args.optBoolean("on", true))
        }
        // location: stream one JSON fix back. Same content_type the
        // Python agent uses so the hub parses it identically.
        dispatcher.registerStream("location") { args, sink ->
            val hint = args.optString("provider", "").takeIf { it.isNotBlank() }
            val fix = LocationOp.fix(ctx, hint)
            val bytes = fix.toString().toByteArray(Charsets.UTF_8)
            sink.sendStartWith { m ->
                m.put("content_type", "application/json")
                m.put("size", bytes.size)
            }
            sink.sendChunk(bytes)
            // sendEnd is called by HubClient/DirectServer when this
            // suspend handler returns -- nothing more to do.
        }
        // record_audio: stream the MP4/AAC bytes back in 256 KiB chunks.
        dispatcher.registerStream("record_audio") { args, sink ->
            val dur = args.optInt("duration", 15)
            val file = RecordAudioOp.record(ctx, dur)
            try {
                sink.sendStartWith { m ->
                    m.put("content_type", "audio/mp4")
                    m.put("size", file.length())
                }
                file.inputStream().use { input ->
                    val buf = ByteArray(256 * 1024)
                    while (true) {
                        val n = input.read(buf)
                        if (n <= 0) break
                        val out = if (n == buf.size) buf else buf.copyOf(n)
                        sink.sendChunk(out)
                    }
                }
            } finally {
                try { file.delete() } catch (_: Exception) {}
            }
        }
        // camera_capture: one-shot JPEG; reuses CameraEngine via
        // CameraCaptureOp.captureOne.
        dispatcher.registerStream("camera_capture") { args, sink ->
            val cidArg = args.optString("camera_id", "0")
            val facing = if (cidArg == "1" || cidArg.equals("front", true))
                            CameraCharacteristics.LENS_FACING_FRONT
                         else CameraCharacteristics.LENS_FACING_BACK
            val jpeg = CameraCaptureOp.captureOne(ctx, facing)
            sink.sendStartWith { m ->
                m.put("content_type", "image/jpeg")
                m.put("size", jpeg.size)
            }
            sink.sendChunk(jpeg)
        }
    }

    private enum class StreamKind { SCREEN, CAMERA }

    /**
     * Request args understood by both stream ops (all optional):
     *
     *   - `quality`  Int 1-100  -- JPEG quality. Default 70 (camera), 50 (screen).
     *   - `max_dim`  Int        -- longest side in pixels. Default 720; capped
     *                              at 1080 to keep encode time reasonable.
     *   - `fps_cap`  Int        -- max frames per second. Default 30; 0 = unlimited.
     *   - `facing`   "front"|"back"  -- camera only; default "back".
     *
     * camera_stream defaults sized for a quick low-latency preview;
     * screen_stream defaults sized for a UI mirror. Browsers wanting
     * something different should pass args explicitly.
     */
    private suspend fun runNativeStream(
        kind: StreamKind,
        sink: WsStreamSink,
        args: JSONObject?,
    ) {
        val svc = DriverService.instance
            ?: throw RuntimeException("Vortex Driver service is not running")

        val maxDim  = (args?.optInt("max_dim", 0) ?: 0).let { if (it <= 0) 720 else it.coerceIn(160, 1080) }
        val fpsCap  = (args?.optInt("fps_cap", -1) ?: -1).let { if (it < 0) 30 else it.coerceIn(0, 60) }
        val quality = (args?.optInt("quality", 0) ?: 0).let {
            val def = if (kind == StreamKind.SCREEN) 50 else 70
            if (it <= 0) def else it.coerceIn(10, 95)
        }

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
            StreamKind.SCREEN -> svc.startNativeScreenStream(
                sink = engineSink,
                maxDimension = maxDim,
                jpegQuality = quality,
                fpsCap = fpsCap,
                readyToEmit = { sink.isReady() },
            )
            StreamKind.CAMERA -> {
                val facing = when (args?.optString("facing", "back")) {
                    "front" -> CameraCharacteristics.LENS_FACING_FRONT
                    else    -> CameraCharacteristics.LENS_FACING_BACK
                }
                svc.startNativeCameraStream(
                    sink = engineSink,
                    facing = facing,
                    maxDimension = maxDim,
                    jpegQuality = quality,
                    fpsCap = fpsCap,
                    readyToEmit = { sink.isReady() },
                )
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

    /**
     * B5: screen_stream H.264 variant. Wire shape:
     *
     *   stream_start: {
     *     content_type: "video/h264",
     *     codec:        "avc1.42E01E",       // codec string for VideoDecoder.configure
     *     width, height,                      // pixel dims (encoder picks)
     *     csd_base64:   "<base64 SPS+PPS>",   // VideoDecoder.configure.description
     *   }
     *   stream_chunk_header: { kf: bool, pts: int }     // per access unit
     *   <binary>: annex-B prefixed NAL units
     *
     * Args understood (all optional):
     *   max_dim: longest side in px, clamped 160..1080  (default 720)
     *   fps_cap: target fps (encoder hint)               (default 30)
     *   bitrate: bits/s; default scales with max_dim     (default 1.5 Mbps @ 720p)
     *
     * Browsers without WebCodecs should not request codec=h264. The op
     * still works for them, but they'd need a pure-JS H.264 decoder to
     * render the frames.
     */
    private suspend fun runScreenH264Stream(sink: WsStreamSink, args: JSONObject?) {
        val svc = DriverService.instance
            ?: throw RuntimeException("Vortex Driver service is not running")

        val maxDim  = (args?.optInt("max_dim", 0) ?: 0).let { if (it <= 0) 720 else it.coerceIn(160, 1080) }
        val fpsCap  = (args?.optInt("fps_cap", -1) ?: -1).let { if (it < 0) 30 else it.coerceIn(0, 60) }
        val bitrate = (args?.optInt("bitrate", 0) ?: 0).let {
            if (it > 0) it.coerceIn(200_000, 8_000_000)
            else        defaultBitrateFor(maxDim)
        }

        // Bridge from encoder callbacks back into the coroutine.
        val failure = CompletableDeferred<Throwable>()
        val started = CompletableDeferred<Boolean>()
        val nalSink = object : ScreenH264Encoder.NalSink {
            override fun onCodecConfig(csdBytes: ByteArray, width: Int, height: Int, codecString: String) {
                val csdB64 = Base64.encodeToString(csdBytes, Base64.NO_WRAP)
                sink.sendStartWith { m ->
                    m.put("content_type", "video/h264")
                    m.put("codec", codecString)
                    m.put("width", width)
                    m.put("height", height)
                    m.put("csd_base64", csdB64)
                }
                if (!started.isCompleted) started.complete(true)
            }
            override fun onFrame(nalBytes: ByteArray, isKeyFrame: Boolean, ptsMicros: Long) {
                sink.sendChunkAnnotated(nalBytes) { hdr ->
                    hdr.put("kf", isKeyFrame)
                    hdr.put("pts", ptsMicros)
                }
            }
            override fun onError(message: String) {
                if (!failure.isCompleted) failure.complete(RuntimeException(message))
            }
        }

        svc.startNativeScreenStreamH264(
            sink = nalSink,
            maxDimension = maxDim,
            bitrateBps = bitrate,
            fpsCap = fpsCap,
        )

        try {
            while (true) {
                if (failure.isCompleted) throw failure.getCompleted()
                delay(100)
            }
        } finally {
            try { svc.stopNativeScreenStreamH264() } catch (_: Exception) {}
        }
    }

    private fun defaultBitrateFor(maxDim: Int): Int = when {
        maxDim >= 1080 -> 3_500_000
        maxDim >= 900  -> 2_500_000
        maxDim >= 600  -> 1_500_000
        else           -> 800_000
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
