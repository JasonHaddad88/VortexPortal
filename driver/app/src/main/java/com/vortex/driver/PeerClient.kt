package com.vortex.driver

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import org.json.JSONObject
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * B11.3: outbound peer-to-peer WS client. The other side of
 * [DirectServer] -- one PeerClient per (this device -> remote peer)
 * direction. Reads the remote's published endpoint from
 * [PeerRegistry] (Turso `device_peers` table) and connects directly,
 * no hub broker in between.
 *
 * Wire shape (same as HubClient + DirectServer):
 *   client -> { type:"hello", ticket }              // handshake
 *   server -> { type:"hello_ok" | "hello_fail" }
 *   client -> { type:"request", id, op, args }      // op call
 *   server -> { type:"response", id, ok, result|error }   (unary)
 *
 *   For streams:
 *   server -> { type:"stream_start", id, ... }
 *   server -> { type:"stream_chunk_header", id, ... }  + binary frame
 *   ...repeat...
 *   server -> { type:"stream_end", id, frames }
 *
 * Binary-frame routing: the WS reader tags the next inbound binary
 * with the rid from the most recent stream_chunk_header (matches
 * DirectServer's own routing).
 *
 * Caller owns lifecycle: connect, run ops, then `close()`. Coroutine
 * scopes are NOT owned here; the activity should cancel its scope
 * which lets the unary `await`s unwind, then call `close()`.
 */
class PeerClient(private val ctx: Context) {

    interface StreamHandlers {
        fun onStart(meta: JSONObject) {}
        /** [header] is the JSON we received as stream_chunk_header just
         *  before the binary frame -- handy for H.264 `kf` + `pts`. */
        fun onFrame(bytes: ByteArray, header: JSONObject?) {}
        /** [error] is null on clean end, or the {ok:false, error: ...}
         *  response when the stream failed before/during. */
        fun onEnd(error: JSONObject?) {}
    }

    @Volatile private var ws: WebSocket? = null
    private val http: OkHttpClient = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)
        .build()
    private val nextRid = AtomicLong(1)
    private val pendingUnary = ConcurrentHashMap<String, CompletableDeferred<JSONObject>>()
    private val streamHandlers = ConcurrentHashMap<String, StreamHandlers>()
    @Volatile private var pendingBinaryRid: String? = null
    @Volatile private var pendingBinaryHeader: JSONObject? = null
    @Volatile private var peerName: String = ""

    val isOpen: Boolean get() = ws != null

    /**
     * Resolve [deviceId] via [PeerRegistry] and race its published
     * hosts; first 1.5 s handshake wins. Returns true on success;
     * null on no-peer-info; or throws on transport failure with a
     * clear message.
     */
    suspend fun connectTo(deviceId: String): Boolean = withContext(Dispatchers.IO) {
        val cli = tursoClientFrom(ctx) ?: throw RuntimeException("Database not configured.")
        val peer = PeerRegistry.listFresh(cli)[deviceId]
            ?: throw RuntimeException(
                "Peer is offline (no fresh device_peers row). " +
                "Open the Vortex app on the target device so it can publish itself."
            )
        if (peer.hosts.isEmpty()) throw RuntimeException(
            "Peer published 0 hosts -- it may be on a network with no IPv4 LAN address."
        )
        // Try each host:port sequentially. A LAN address is usually
        // first; failure typically means cross-network and the user
        // needs a hub / mesh.
        for (host in peer.hosts) {
            val wsUrl = "ws://$host/ws/direct?ticket=${peer.ticket}"
            Log.i(TAG, "peer dial -> $wsUrl")
            val ok = tryHandshake(wsUrl)
            if (ok) return@withContext true
        }
        throw RuntimeException(
            "Couldn't reach the peer at any of: " +
            peer.hosts.joinToString(", ") +
            ". This usually means a different network or a firewall."
        )
    }

    private suspend fun tryHandshake(wsUrl: String): Boolean {
        val gate = CompletableDeferred<Boolean>()
        val ticket = wsUrl.substringAfter("ticket=", "").substringBefore("&", "")
        // One listener handles both phases: until `connected` flips
        // true we only react to hello_ok / hello_fail; afterwards
        // every text + binary frame is routed to pendingUnary /
        // streamHandlers via routeText / routeBinary.
        val listener = object : WebSocketListener() {
            @Volatile var connected = false
            override fun onOpen(s: WebSocket, response: Response) {
                // DirectServer accepts the ticket via the URL query
                // string AND honors a hello frame; sending both keeps
                // us forward-compat with the existing browser protocol.
                try {
                    s.send(JSONObject().put("type", "hello").put("ticket", ticket).toString())
                } catch (_: Exception) {}
            }
            override fun onMessage(s: WebSocket, text: String) {
                if (connected) { routeText(s, text); return }
                val msg = try { JSONObject(text) } catch (_: Throwable) { return }
                when (msg.optString("type")) {
                    "auth_ok", "hello_ok" -> {
                        peerName = msg.optString("device_name", peerName)
                        if (!gate.isCompleted) {
                            ws = s
                            connected = true
                            gate.complete(true)
                        }
                    }
                    "hello_fail" -> {
                        try { s.close(1008, "bad ticket") } catch (_: Exception) {}
                        if (!gate.isCompleted) gate.complete(false)
                    }
                    else -> { /* ignore pre-handshake noise */ }
                }
            }
            override fun onMessage(s: WebSocket, bytes: ByteString) {
                if (connected) routeBinary(s, bytes)
                // Pre-handshake binary is undefined; drop silently.
            }
            override fun onFailure(s: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "peer dial failure $wsUrl: ${t.javaClass.simpleName}: ${t.message}")
                if (!gate.isCompleted) gate.complete(false)
                // Wake awaiters; the user-facing message will say
                // "connection lost".
                if (connected) onConnectionLost("transport ${t.javaClass.simpleName}")
            }
            override fun onClosed(s: WebSocket, code: Int, reason: String) {
                if (!gate.isCompleted) gate.complete(false)
                if (connected) onConnectionLost("closed ($code)")
                connected = false
                if (ws === s) ws = null
            }
        }
        val candidate: WebSocket = http.newWebSocket(
            Request.Builder().url(wsUrl).build(),
            listener,
        )
        val ok = withTimeoutOrNull(1500) { gate.await() } ?: false
        if (!ok) try { candidate.close(1000, "timeout") } catch (_: Exception) {}
        return ok
    }

    /** Wake all pending awaiters with an `{ok:false}` so the activity
     *  doesn't hang on a dead socket. */
    private fun onConnectionLost(reason: String) {
        val err = JSONObject().put("ok", false).put("error", reason)
        val snapU = pendingUnary.toMap(); pendingUnary.clear()
        for ((_, d) in snapU) try { d.complete(err) } catch (_: Exception) {}
        val snapS = streamHandlers.toMap(); streamHandlers.clear()
        for ((_, h) in snapS) try { h.onEnd(err) } catch (_: Throwable) {}
    }

    /** Send a unary op and await its `response`. Throws on transport
     *  failure / timeout / `{ok:false}` from the peer. */
    suspend fun unary(
        op: String, args: JSONObject? = null, timeoutMs: Long = 5_000,
    ): JSONObject = withContext(Dispatchers.IO) {
        val s = ws ?: throw RuntimeException("Not connected.")
        val id = "u-${nextRid.getAndIncrement()}"
        val deferred = CompletableDeferred<JSONObject>()
        pendingUnary[id] = deferred
        val req = JSONObject().put("type", "request").put("id", id).put("op", op)
            .put("args", args ?: JSONObject())
        try { s.send(req.toString()) }
        catch (e: Throwable) { pendingUnary.remove(id); throw e }
        val resp = withTimeoutOrNull(timeoutMs) { deferred.await() }
            ?: run { pendingUnary.remove(id); throw RuntimeException("Op '$op' timed out after ${timeoutMs}ms.") }
        if (!resp.optBoolean("ok", false)) {
            throw RuntimeException(resp.optString("error", "peer returned ok=false"))
        }
        resp.optJSONObject("result") ?: JSONObject()
    }

    /** Open a stream. Returns the rid so callers can [stopStream]. */
    fun stream(op: String, args: JSONObject?, handlers: StreamHandlers): String? {
        val s = ws ?: return null
        val id = "s-${nextRid.getAndIncrement()}"
        streamHandlers[id] = handlers
        val req = JSONObject().put("type", "request").put("id", id).put("op", op)
            .put("args", args ?: JSONObject())
        return try { s.send(req.toString()); id }
        catch (_: Throwable) { streamHandlers.remove(id); null }
    }

    /** Stops a stream by closing the WS -- mirrors the webapp's
     *  shutdown idiom (peer cancels its server-side coroutine on
     *  WS close, which tears down the engine). */
    fun stopStream(rid: String) {
        streamHandlers.remove(rid)
        // No in-band "abort" message in the protocol; the engine
        // tear-down only fires on WS close. If multiple streams are
        // in flight, callers should call close() to stop them all.
    }

    fun close() {
        val s = ws; ws = null
        try { s?.close(1000, "client closing") } catch (_: Exception) {}
        // Wake any awaiters so the activity unwinds cleanly.
        for ((_, d) in pendingUnary) try { d.complete(JSONObject().put("ok", false).put("error", "closed")) } catch (_: Exception) {}
        pendingUnary.clear()
        for ((_, h) in streamHandlers) try { h.onEnd(JSONObject().put("ok", false).put("error", "closed")) } catch (_: Exception) {}
        streamHandlers.clear()
    }

    /** Route a post-handshake text frame. Called by the listener
     *  inside [tryHandshake] once `connected` flips true. */
    private fun routeText(s: WebSocket, text: String) {
        val msg = try { JSONObject(text) } catch (_: Throwable) { return }
        when (msg.optString("type")) {
            "response" -> {
                val id = msg.optString("id"); val d = pendingUnary.remove(id) ?: return
                d.complete(msg)
            }
            "stream_chunk_header" -> {
                pendingBinaryRid = msg.optString("id"); pendingBinaryHeader = msg
            }
            "stream_start" -> {
                val id = msg.optString("id"); val h = streamHandlers[id] ?: return
                try { h.onStart(msg) } catch (_: Throwable) {}
            }
            "stream_end" -> {
                val id = msg.optString("id"); val h = streamHandlers.remove(id) ?: return
                val err = if (msg.has("error")) JSONObject().put("ok", false).put("error", msg.optString("error")) else null
                try { h.onEnd(err) } catch (_: Throwable) {}
            }
        }
    }

    private fun routeBinary(s: WebSocket, bytes: ByteString) {
        val rid = pendingBinaryRid; val hdr = pendingBinaryHeader
        pendingBinaryRid = null; pendingBinaryHeader = null
        val h = rid?.let { streamHandlers[it] } ?: return
        try { h.onFrame(bytes.toByteArray(), hdr) } catch (_: Throwable) {}
    }

    companion object { private const val TAG = "PeerClient" }
}
