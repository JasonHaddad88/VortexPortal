package com.vortex.driver

import android.content.Context
import android.util.Log
import kotlinx.coroutines.*
import okhttp3.*
import okio.ByteString
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap

/**
 * APK's outbound WebSocket to the hub -- the same role the Python agent
 * plays in Termux. Authenticates with the device_id+token written by
 * [EnrollActivity], handles inbound op requests via [OpDispatcher], and
 * reconnects with backoff on any disconnect.
 *
 * B2.2: streaming ops. The dispatcher can now return [OpDispatcher.Outcome.Stream]
 * for long-lived handlers (screen_stream, camera_stream). We launch each
 * stream in its own coroutine, track them by rid, and cancel all of them
 * when the WebSocket closes (so engines stop and the camera/projection
 * is released promptly).
 *
 * Send serialization: every outbound text + binary frame goes through
 * [sendLock] so a stream's atomic `stream_chunk_header`+binary pair can't
 * be interleaved by an unrelated response/direct_info send.
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

    /** Per-WS connection. Reset on each (re)connect so a stale lock
     *  from a previous session can't accidentally serialize new sends. */
    private var sendLock: Any = Any()
    private val streamJobs = ConcurrentHashMap<String, Job>()

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
        cancelAllStreams("client stopping")
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

        // Fresh lock per connection -- old streams' references stay valid
        // (their sends just no-op against the closed socket) but new
        // streams get a clean mutex.
        sendLock = Any()

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
                sendText(webSocket, auth.toString())
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                handleText(webSocket, text)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                // B2.2: still no inbound binary (write_file remains a B-later
                // feature on the APK). Ignore silently.
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "closing: $code $reason")
                try { webSocket.close(1000, null) } catch (_: Exception) {}
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "closed: $code $reason")
                cancelAllStreams("ws closed ($code)")
                ws = null
                onStatus("disconnected ($code)")
                if (!gate.isCompleted) gate.complete(true)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "failure: ${t.javaClass.simpleName}: ${t.message}")
                cancelAllStreams("ws failure")
                ws = null
                onStatus("connect failed: ${t.javaClass.simpleName}")
                if (!gate.isCompleted) gate.complete(false)
            }
        }
        http.newWebSocket(req, listener)
        // Suspend until close or failure -- returns true on clean close
        // (reset backoff), false on transport failure (try next node).
        val ok = gate.await()
        if (ok) backoffSec = 1.0
        ok
    }

    private fun handleText(webSocket: WebSocket, text: String) {
        val msg = try { JSONObject(text) } catch (_: Exception) { return }
        when (msg.optString("type")) {
            "auth_ok" -> {
                val name = msg.optString("name", Prefs.deviceName(ctx) ?: "")
                onStatus("connected as $name")
                val nodes = msg.optJSONArray("nodes")
                if (nodes != null) saveNodes(nodes)
                // B3: push real direct_info if the DirectServer is up.
                // The hub stores this against the device and hands it to
                // browsers so they can connect direct (skipping the hub
                // from the data path entirely on Android). If the server
                // isn't ready, we still send {port:0} so the broker can
                // tell the browser to fall back to the relay path.
                val server = DriverService.instance?.directServer()
                val di = if (server != null && server.port() > 0) {
                    val ticket = server.armTicket()
                    val hosts = DeviceHosts.reachableIps()
                    buildDirectInfo(server.port(), hosts, ticket)
                } else {
                    buildDirectInfo(0, emptyList(), "")
                }
                sendText(webSocket, di.toString())
            }
            "auth_fail" -> {
                val err = msg.optString("error", "")
                onStatus("auth rejected: $err")
                if (err.lowercase().contains("credentials")) {
                    stopped = true   // fatal -- wait for re-enroll
                    try { webSocket.close(4001, "fatal auth") } catch (_: Exception) {}
                }
            }
            "request" -> dispatchRequest(webSocket, text)
            else -> { /* ignore */ }
        }
    }

    /** Route a `{type:request}` frame through the dispatcher and either
     *  send back a Unary response or spin up a Stream coroutine. */
    private fun dispatchRequest(webSocket: WebSocket, text: String) {
        when (val outcome = dispatcher.classify(text)) {
            is OpDispatcher.Outcome.Unary -> sendText(webSocket, outcome.responseJson)
            is OpDispatcher.Outcome.Reject -> {
                val r = outcome.responseJson
                if (r != null) sendText(webSocket, r)
            }
            is OpDispatcher.Outcome.Stream -> launchStream(webSocket, outcome)
        }
    }

    private fun launchStream(webSocket: WebSocket, s: OpDispatcher.Outcome.Stream) {
        val rid = s.rid
        // If a previous stream with this rid is still running (shouldn't
        // normally happen but the hub could retry), cancel it first.
        streamJobs.remove(rid)?.cancel()
        val sink = WsStreamSink(OkHttpWsBackend(webSocket), rid, sendLock)
        val job = scope.launch(Dispatchers.Default) {
            try {
                s.handler.run(s.args, sink)
                // Handler returned normally -- close the stream if it
                // didn't already.
                sink.sendEnd()
            } catch (ce: CancellationException) {
                // WebSocket closed or new request superseded this stream.
                sink.sendEnd()
                throw ce
            } catch (t: Throwable) {
                Log.w(TAG, "stream $rid (${s.op}) failed: ${t.javaClass.simpleName}: ${t.message}")
                // If the handler died before sending any chunks, surface
                // it as a unary error response so the hub returns a 502.
                if (sink.framesSent == 0L) {
                    sendText(webSocket, dispatcher.streamSetupError(rid, t))
                } else {
                    sink.sendError("${t.javaClass.simpleName}: ${t.message ?: ""}")
                }
            } finally {
                streamJobs.remove(rid)
            }
        }
        streamJobs[rid] = job
    }

    private fun cancelAllStreams(reason: String) {
        val snap = streamJobs.toMap()
        streamJobs.clear()
        for ((rid, j) in snap) {
            Log.i(TAG, "cancel stream $rid ($reason)")
            j.cancel()
        }
    }

    /** Always send via this helper so concurrent ops + the direct_info
     *  push can't interleave with a stream's atomic header+binary pair. */
    private fun sendText(ws: WebSocket, text: String) {
        synchronized(sendLock) {
            try { ws.send(text) } catch (_: Exception) {}
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
