package com.vortex.driver

import android.content.Context
import android.content.res.Configuration
import android.content.Intent
import android.provider.Settings
import android.util.DisplayMetrics
import android.util.Log
import android.view.WindowManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import kotlin.coroutines.coroutineContext

/**
 * Request/response JSON server on 127.0.0.1:[port] (default 5097) for the
 * Termux Python agent to send input commands. Different protocol from the
 * stream servers because input is low-bandwidth + needs per-call success
 * feedback so the browser can show errors.
 *
 * Wire format both directions:
 *   [u32 BE length][JSON bytes]
 *
 * Supported command types (request -> response):
 *   {"type":"screen_size"}                   -> {"ok":true,"result":{"w":1080,"h":2400}}
 *   {"type":"a11y_state"}                    -> {"ok":true,"result":{"enabled":true|false}}
 *   {"type":"tap","x":540,"y":1200,
 *    "duration_ms":50}                       -> {"ok":true} | {"ok":false,"error":...}
 *   {"type":"long_press","x":..,"y":..,
 *    "duration_ms":600}                      -> {"ok":true}
 *   {"type":"swipe","from":[x,y],"to":[x,y],
 *    "duration_ms":300}                      -> {"ok":true}
 *   {"type":"back" | "home" | "recents"
 *    | "notifications"}                      -> {"ok":true}
 *
 * Unlike StreamServer, this accepts MANY clients in parallel (each in its
 * own coroutine) so a slow read on one socket doesn't block input from
 * another. In practice there's only ever one (the agent), but the math is
 * the same and avoids a "previous client dropping prevents new connection"
 * footgun.
 */
class InputServer(
    private val context: Context,
    private val port: Int = 5097,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var serverSocket: ServerSocket? = null
    @Volatile private var stopped = false

    fun start() {
        scope.launch {
            val sock: ServerSocket = try {
                ServerSocket(port, /* backlog = */ 4, InetAddress.getByName("127.0.0.1"))
            } catch (e: Exception) {
                Log.e(TAG, "ServerSocket failed: $e")
                return@launch
            }
            serverSocket = sock
            Log.i(TAG, "InputServer listening on 127.0.0.1:$port")

            while (isActive && !stopped) {
                val client = try {
                    sock.accept()
                } catch (e: Exception) {
                    if (!stopped) Log.w(TAG, "accept failed: $e")
                    break
                }
                client.tcpNoDelay = true
                launch { handleClient(client) }
            }
        }
    }

    private suspend fun handleClient(socket: Socket) {
        Log.i(TAG, "Input client connected: ${socket.inetAddress}")
        try {
            val ins = BufferedInputStream(socket.getInputStream())
            val outs = BufferedOutputStream(socket.getOutputStream())
            while (coroutineContext.isActive && !stopped) {
                val req = readFrame(ins) ?: break
                val resp = handleCommand(req)
                writeFrame(outs, resp)
                outs.flush()
            }
        } catch (_: Exception) {
            // Connection died mid-frame; treat as graceful disconnect.
        } finally {
            try { socket.close() } catch (_: Exception) {}
            Log.i(TAG, "Input client disconnected")
        }
    }

    /** Returns the JSON bytes, or null on EOF. */
    private fun readFrame(ins: BufferedInputStream): String? {
        val header = ByteArray(4)
        var read = 0
        while (read < 4) {
            val n = ins.read(header, read, 4 - read)
            if (n < 0) return null
            read += n
        }
        val len = ByteBuffer.wrap(header).int
        if (len <= 0 || len > MAX_FRAME) {
            // Malformed; bail.
            return null
        }
        val body = ByteArray(len)
        var got = 0
        while (got < len) {
            val n = ins.read(body, got, len - got)
            if (n < 0) return null
            got += n
        }
        return String(body, Charsets.UTF_8)
    }

    private fun writeFrame(outs: BufferedOutputStream, json: String) {
        val bytes = json.toByteArray(Charsets.UTF_8)
        val header = ByteBuffer.allocate(4).putInt(bytes.size).array()
        outs.write(header)
        outs.write(bytes)
    }

    /** Dispatches one command, returns the response JSON. */
    private fun handleCommand(reqJson: String): String {
        val req = try {
            JSONObject(reqJson)
        } catch (e: Exception) {
            return errResp("Malformed JSON: $e")
        }
        val type = req.optString("type")

        return when (type) {
            "screen_size" -> {
                val (w, h) = realScreenSize()
                okResp(JSONObject().put("w", w).put("h", h))
            }
            "a11y_state" -> {
                okResp(JSONObject().put("enabled", VortexAccessibilityService.isEnabled))
            }
            "tap", "long_press" -> {
                val svc = VortexAccessibilityService.current()
                    ?: return a11yMissingResp()
                val x = req.optDouble("x", -1.0).toFloat()
                val y = req.optDouble("y", -1.0).toFloat()
                if (x < 0 || y < 0) return errResp("Missing/invalid x or y")
                val duration = req.optLong(
                    "duration_ms",
                    if (type == "long_press") 600L else 50L,
                )
                val ok = if (type == "long_press") svc.longPress(x, y, duration)
                         else svc.tap(x, y, duration)
                if (ok) okResp() else errResp("dispatchGesture returned false")
            }
            "swipe" -> {
                val svc = VortexAccessibilityService.current()
                    ?: return a11yMissingResp()
                val from = req.optJSONArray("from")
                val to = req.optJSONArray("to")
                if (from == null || from.length() < 2 || to == null || to.length() < 2) {
                    return errResp("Missing/invalid 'from' or 'to' (need 2-element arrays)")
                }
                val ok = svc.swipe(
                    from.getDouble(0).toFloat(), from.getDouble(1).toFloat(),
                    to.getDouble(0).toFloat(), to.getDouble(1).toFloat(),
                    req.optLong("duration_ms", 300L),
                )
                if (ok) okResp() else errResp("dispatchGesture returned false")
            }
            "back" -> globalAction { it.back() }
            "home" -> globalAction { it.home() }
            "recents" -> globalAction { it.recents() }
            "notifications" -> globalAction { it.notifications() }
            else -> errResp("Unknown command type: $type")
        }
    }

    private fun globalAction(call: (VortexAccessibilityService) -> Boolean): String {
        val svc = VortexAccessibilityService.current() ?: return a11yMissingResp()
        return if (call(svc)) okResp() else errResp("performGlobalAction returned false")
    }

    private fun realScreenSize(): Pair<Int, Int> {
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        return if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            val b = wm.currentWindowMetrics.bounds
            b.width() to b.height()
        } else {
            @Suppress("DEPRECATION")
            val display = wm.defaultDisplay
            val m = DisplayMetrics()
            @Suppress("DEPRECATION")
            display.getRealMetrics(m)
            m.widthPixels to m.heightPixels
        }
    }

    private fun okResp(result: JSONObject? = null): String {
        val o = JSONObject().put("ok", true)
        if (result != null) o.put("result", result)
        return o.toString()
    }

    private fun errResp(msg: String): String =
        JSONObject().put("ok", false).put("error", msg).toString()

    /** Standard "service not enabled" response with the system-settings deep-link
     *  in the body so the browser can render an actionable error. */
    private fun a11yMissingResp(): String =
        JSONObject()
            .put("ok", false)
            .put("error",
                "Vortex Driver's Accessibility Service is not enabled. " +
                "On the phone: Settings -> Accessibility -> Vortex Driver -> " +
                "toggle 'Use service' on. Android won't let us enable it for you.")
            .put("settings_intent", Settings.ACTION_ACCESSIBILITY_SETTINGS)
            .toString()

    fun stop() {
        stopped = true
        try { serverSocket?.close() } catch (_: Exception) {}
        scope.cancel()
    }

    companion object {
        private const val TAG = "InputServer"
        private const val MAX_FRAME = 64 * 1024  // commands are tiny; 64K is generous
    }
}
