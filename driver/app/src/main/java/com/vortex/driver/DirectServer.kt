package com.vortex.driver

import android.content.Context
import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.java_websocket.WebSocket
import org.java_websocket.handshake.ClientHandshake
import org.java_websocket.server.WebSocketServer
import org.json.JSONArray
import org.json.JSONObject
import java.net.InetSocketAddress
import java.net.URI
import java.nio.ByteBuffer
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * In-APK WebSocket server (B3). Hosts the same op surface as the hub-bound
 * [HubClient] but accepts connections **directly** from browsers on the
 * LAN, so screen/camera/input frames can skip the hub entirely. Mirrors
 * the V5.20/V5.21 Python agent's direct-WS phase A1+A2 on Android.
 *
 * Lifecycle: owned by [DriverService]. Started once per service lifetime,
 * port assigned by the kernel (bind port 0). The hub-bound [HubClient]
 * pushes `(port, hosts, ticket)` in `direct_info` on every auth_ok so
 * the hub knows where to send browsers.
 *
 * Auth: a one-time ticket per browser session. Tickets are registered via
 * [armTicket] right before [HubClient] pushes direct_info. The browser
 * connects to `ws://<host>:<port>/ws/direct?ticket=...` and the ticket
 * is consumed (one-shot) on successful handshake. Stale tickets time
 * out after [ticketTtlMs] so a leaked-but-unused ticket can't be
 * replayed forever.
 *
 * Dispatch: each connection gets its own per-rid coroutine map (same
 * pattern as HubClient). On close, every active stream coroutine for
 * that connection is cancelled so engines release promptly.
 *
 * What's NOT served here: the `auth` handshake protocol the hub uses
 * (device_id+token). The direct path's authn IS the ticket -- the
 * device_id authentication already happened on the hub WS.
 */
class DirectServer(
    private val ctx: Context,
    requestedPort: Int = 0,
) : WebSocketServer(InetSocketAddress(requestedPort)) {

    private val dispatcher = OpDispatcher().also { Ops.registerAll(ctx, it) }
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    // Per-connection state: each conn gets a fresh sendLock + jobs map.
    private val conns = ConcurrentHashMap<WebSocket, ConnState>()

    // Registered (still-valid) tickets and their expiry. Removed on
    // successful first handshake OR on TTL expiry.
    private val tickets = ConcurrentHashMap<String, Long>()
    private val ticketTtlMs: Long = 5 * 60 * 1000L

    @Volatile private var boundPort: Int = 0
    @Volatile private var hasStarted: Boolean = false

    /** Port the kernel actually assigned us, or 0 if not yet bound. */
    fun port(): Int = boundPort

    /** Register a fresh ticket good for one successful handshake. The
     *  ticket is also dropped after [ticketTtlMs] if never used. */
    fun armTicket(): String {
        // Prune expired before issuing a new one (cheap, bounded).
        val now = System.currentTimeMillis()
        tickets.entries.removeIf { it.value < now }
        val t = UUID.randomUUID().toString().replace("-", "")
        tickets[t] = now + ticketTtlMs
        return t
    }

    /** B11.2: register a SPECIFIC ticket value (e.g. one that was
     *  published to the Turso `device_peers` table). Same TTL +
     *  one-shot consume rules as [armTicket]. Idempotent: re-arming
     *  the same value just refreshes its expiry. */
    fun armTicketValue(value: String) {
        if (value.isBlank()) return
        val now = System.currentTimeMillis()
        tickets.entries.removeIf { it.value < now }
        tickets[value] = now + ticketTtlMs
    }

    override fun onStart() {
        // Java-WebSocket sets the bound port AFTER onStart fires.
        // Re-read from the address it actually listens on.
        boundPort = address?.port ?: 0
        hasStarted = true
        Log.i(TAG, "direct-WS server listening on 0.0.0.0:$boundPort")
        connectionLostTimeout = 30
    }

    override fun onOpen(conn: WebSocket, handshake: ClientHandshake) {
        val path = handshake.resourceDescriptor ?: ""
        // path is something like "/ws/direct?ticket=abc"
        val pathOnly: String
        val ticket: String?
        try {
            // URI(...) needs an absolute URI; prepend a stub scheme/host.
            val u = URI.create("ws://x" + (if (path.startsWith("/")) path else "/$path"))
            pathOnly = u.path ?: ""
            ticket = u.rawQuery
                ?.split('&')
                ?.map { it.split('=', limit = 2) }
                ?.firstOrNull { it.firstOrNull() == "ticket" }
                ?.getOrNull(1)
        } catch (_: Exception) {
            conn.close(1008, "bad uri")
            return
        }

        if (pathOnly != "/ws/direct") {
            conn.close(1008, "unknown path")
            return
        }
        if (ticket.isNullOrBlank()) {
            conn.close(1008, "missing ticket")
            return
        }
        val exp = tickets[ticket]
        if (exp == null || exp < System.currentTimeMillis()) {
            tickets.remove(ticket)
            conn.close(1008, "bad ticket")
            return
        }
        // One-shot: consume on accept so a leaked ticket can't be reused.
        tickets.remove(ticket)

        val state = ConnState()
        conns[conn] = state
        val hello = JSONObject()
            .put("type", "auth_ok")
            .put("device_name", Prefs.deviceName(ctx) ?: "")
            .put("agent_version", BuildConfig.VERSION_NAME)
            .toString()
        synchronized(state.sendLock) {
            try { conn.send(hello) } catch (_: Exception) {}
        }
        Log.i(TAG, "direct WS accepted from ${conn.remoteSocketAddress}")
    }

    override fun onMessage(conn: WebSocket, message: String) {
        val state = conns[conn] ?: return
        // Same {type:request,id,op,args} envelope the hub uses.
        when (val outcome = dispatcher.classify(message)) {
            is OpDispatcher.Outcome.Unary -> sendText(conn, state, outcome.responseJson)
            is OpDispatcher.Outcome.Reject -> outcome.responseJson?.let { sendText(conn, state, it) }
            is OpDispatcher.Outcome.Stream -> launchStream(conn, state, outcome)
        }
    }

    override fun onMessage(conn: WebSocket, message: ByteBuffer) {
        // B3 doesn't accept inbound binary (no write_file from direct path).
    }

    override fun onClose(conn: WebSocket, code: Int, reason: String?, remote: Boolean) {
        Log.i(TAG, "direct WS closed (${conn.remoteSocketAddress}): $code $reason")
        val state = conns.remove(conn) ?: return
        for ((rid, job) in state.streamJobs) {
            Log.i(TAG, "cancel direct stream $rid (conn closed)")
            job.cancel()
        }
        state.streamJobs.clear()
    }

    override fun onError(conn: WebSocket?, ex: Exception) {
        Log.w(TAG, "direct WS error on ${conn?.remoteSocketAddress}: ${ex.javaClass.simpleName}: ${ex.message}")
    }

    /** Stop the server + cancel all active streams. Idempotent. */
    fun shutdown() {
        try {
            for ((_, state) in conns) {
                for ((_, job) in state.streamJobs) job.cancel()
                state.streamJobs.clear()
            }
            conns.clear()
            scope.cancel()
            stop(2_000)
        } catch (_: Exception) { /* best-effort */ }
    }

    private fun launchStream(conn: WebSocket, state: ConnState, s: OpDispatcher.Outcome.Stream) {
        val rid = s.rid
        state.streamJobs.remove(rid)?.cancel()
        val sink = WsStreamSink(JavaWsBackend(conn), rid, state.sendLock)
        val job = scope.launch(Dispatchers.Default) {
            try {
                s.handler.run(s.args, sink)
                sink.sendEnd()
            } catch (ce: CancellationException) {
                sink.sendEnd()
                throw ce
            } catch (t: Throwable) {
                Log.w(TAG, "direct stream $rid (${s.op}) failed: ${t.javaClass.simpleName}: ${t.message}")
                if (sink.framesSent == 0L) {
                    sendText(conn, state, dispatcher.streamSetupError(rid, t))
                } else {
                    sink.sendError("${t.javaClass.simpleName}: ${t.message ?: ""}")
                }
            } finally {
                state.streamJobs.remove(rid)
            }
        }
        state.streamJobs[rid] = job
    }

    private fun sendText(conn: WebSocket, state: ConnState, text: String) {
        synchronized(state.sendLock) {
            try { conn.send(text) } catch (_: Exception) {}
        }
    }

    /** Per-connection mutable state. One mutex per connection so
     *  unrelated browser sessions don't serialize against each other. */
    private class ConnState {
        val sendLock = Any()
        val streamJobs = ConcurrentHashMap<String, Job>()
    }

    companion object {
        private const val TAG = "DirectServer"
    }
}

/** Helper: shape the direct_info frame HubClient pushes after auth_ok. */
internal fun buildDirectInfo(port: Int, hosts: List<String>, ticket: String): JSONObject =
    JSONObject()
        .put("type", "direct_info")
        .put("port", port)
        .put("hosts", JSONArray(hosts))
        .put("ticket", ticket)
