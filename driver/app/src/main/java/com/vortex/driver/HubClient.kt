package com.vortex.driver

import android.content.Context
import android.util.Log
import kotlinx.coroutines.*
import okhttp3.*
import okio.ByteString
import org.json.JSONArray
import org.json.JSONObject

/**
 * APK's outbound WebSocket to the hub — the same role the Python agent
 * plays in Termux. Authenticates with the device_id+token written by
 * [EnrollActivity], handles inbound op requests via [OpDispatcher],
 * and reconnects with backoff on any disconnect.
 *
 * One instance per [DriverService]. Use [start]/[stop]; the internal
 * coroutine scope is cancelled on stop so there's no leak.
 */
class HubClient(
    private val ctx: Context,
    private val onStatus: (String) -> Unit,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val http = OkHttpClient.Builder()
        .pingInterval(30, java.util.concurrent.TimeUnit.SECONDS)
        .build()
    private val dispatcher = OpDispatcher().also { Ops.registerAll(ctx, it) }

    @Volatile private var ws: WebSocket? = null
    @Volatile private var stopped = false
    @Volatile private var backoffSec = 1.0

    fun start() {
        if (!Prefs.isEnrolled(ctx)) {
            onStatus("not enrolled")
            return
        }
        scope.launch { runLoop() }
    }

    fun stop() {
        stopped = true
        try { ws?.close(1000, "stopping") } catch (_: Exception) {}
        scope.cancel()
    }

    private suspend fun runLoop() {
        while (!stopped) {
            val candidates = candidateUrls()
            if (candidates.isEmpty()) {
                onStatus("no hub URL configured")
                delay(5_000)
                continue
            }
            var connected = false
            for (base in candidates) {
                if (stopped) return
                connected = connectOnce(base)
                if (connected) break
            }
            if (stopped) return
            backoffSec = (backoffSec * 1.7).coerceAtMost(60.0)
            onStatus("reconnecting in ${backoffSec.toInt()}s")
            delay((backoffSec * 1000).toLong())
        }
    }

    /** Returns true once we cleanly disconnected from a successful
     *  session; false if this candidate failed (try the next one). */
    private suspend fun connectOnce(baseUrl: String): Boolean = withContext(Dispatchers.IO) {
        val wsUrl = baseUrl.replaceFirst("https://", "wss://")
            .replaceFirst("http://", "ws://")
            .trimEnd('/') + "/ws/agent"
        val deviceId = Prefs.deviceId(ctx) ?: return@withContext false
        val token    = Prefs.deviceToken(ctx) ?: return@withContext false

        val gate = kotlinx.coroutines.CompletableDeferred<Boolean>()
        val req = Request.Builder().url(wsUrl).build()
        Log.i(TAG, "connecting -> $wsUrl")
        onStatus("connecting…")
        val listener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                ws = webSocket
                val auth = JSONObject()
                    .put("type", "auth")
                    .put("device_id", deviceId)
                    .put("token", token)
                    .put("agent_version", BuildConfig.VERSION_NAME)
                webSocket.send(auth.toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleText(webSocket, text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                // B1: we don't accept inbound binary frames (uploads — write_file
                // — are a B2+ feature on the APK). Ignore silently.
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "closing: $code $reason")
                try { webSocket.close(1000, null) } catch (_: Exception) {}
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "closed: $code $reason")
                ws = null
                onStatus("disconnected ($code)")
                if (!gate.isCompleted) gate.complete(true)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "failure: ${t.javaClass.simpleName}: ${t.message}")
                ws = null
                onStatus("connect failed: ${t.javaClass.simpleName}")
                if (!gate.isCompleted) gate.complete(false)
            }
        }
        http.newWebSocket(req, listener)
        // Suspend until close or failure — returns true on clean close
        // (reset backoff), false on transport failure (try next node).
        val ok = gate.await()
        if (ok) backoffSec = 1.0
        ok
    }

    private fun handleText(webSocket: WebSocket, text: String) {
        // Cheap fast-path: dispatch op requests without parsing the
        // whole frame twice.
        val msg = try { JSONObject(text) } catch (_: Exception) { return }
        when (msg.optString("type")) {
            "auth_ok" -> {
                val name = msg.optString("name", Prefs.deviceName(ctx) ?: "")
                onStatus("connected as $name")
                // Persist any node list the hub just gave us so the next
                // reconnect has a fresh candidate set.
                val nodes = msg.optJSONArray("nodes")
                if (nodes != null) saveNodes(nodes)
                // Stub direct_info: we don't host a direct-WS server yet
                // (B3 will). Sending it (with port=0) makes the hub
                // broker correctly return "no candidates" and browsers
                // fall back to the hub path.
                val di = JSONObject()
                    .put("type", "direct_info")
                    .put("port", 0)
                    .put("hosts", JSONArray())
                    .put("ticket", "")
                webSocket.send(di.toString())
            }
            "auth_fail" -> {
                val err = msg.optString("error", "")
                onStatus("auth rejected: $err")
                if (err.lowercase().contains("credentials")) {
                    stopped = true   // fatal — wait for re-enroll
                    try { webSocket.close(4001, "fatal auth") } catch (_: Exception) {}
                }
            }
            "request" -> {
                // Run the op off the WS thread so a slow handler can't
                // block keepalive / next inbound frame.
                scope.launch(Dispatchers.Default) {
                    val out = dispatcher.handle(text)
                    if (out != null) {
                        try { webSocket.send(out) } catch (_: Exception) {}
                    }
                }
            }
            else -> { /* ignore */ }
        }
    }

    private fun saveNodes(nodes: JSONArray) {
        val list = (0 until nodes.length()).mapNotNull { i ->
            nodes.optString(i).takeIf { it.isNotBlank() }
        }
        if (list.isEmpty()) return
        Prefs.saveDevice(
            ctx,
            deviceId = Prefs.deviceId(ctx) ?: return,
            deviceToken = Prefs.deviceToken(ctx) ?: return,
            name = Prefs.deviceName(ctx),
            nodes = list,
        )
    }

    private fun candidateUrls(): List<String> {
        val seen = LinkedHashSet<String>()
        Prefs.bootstrapUrl(ctx)?.takeIf { it.isNotBlank() }
            ?.let { seen += it.trimEnd('/') }
        Prefs.nodes(ctx).forEach { seen += it.trimEnd('/') }
        return seen.toList()
    }

    companion object {
        private const val TAG = "HubClient"
    }
}
