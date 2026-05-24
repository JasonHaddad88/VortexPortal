package com.vortex.driver

import okio.ByteString.Companion.toByteString
import java.nio.ByteBuffer

/**
 * Backend-agnostic view of a WebSocket connection. Lets [WsStreamSink] +
 * [OpDispatcher] work against both the OkHttp client socket (HubClient,
 * hub-bound) AND the Java-WebSocket server socket (DirectServer, browser-
 * bound) without caring which library actually owns the connection.
 *
 * Methods are best-effort and never throw -- failures are reported as a
 * `false` return so callers can react with a frame-drop or stream-end.
 */
interface WsBackend {
    /** Enqueue a text frame. Returns false if the socket is closed/broken. */
    fun send(text: String): Boolean
    /** Enqueue a binary frame. Returns false if the socket is closed/broken. */
    fun send(bytes: ByteArray): Boolean
    /** Bytes already buffered for transport. Used for the pre-encode
     *  backpressure gate so we drop frames at the source when the network
     *  can't keep up. May return 0 if the backend doesn't expose a real
     *  measurement (best-effort -- frame-drop only fires when this is
     *  large, so a constant 0 just means backpressure doesn't kick in). */
    fun queueSize(): Long
}

/** OkHttp client-side WebSocket. Used by [HubClient]. */
class OkHttpWsBackend(private val ws: okhttp3.WebSocket) : WsBackend {
    override fun send(text: String): Boolean = try { ws.send(text) } catch (_: Exception) { false }
    override fun send(bytes: ByteArray): Boolean =
        try { ws.send(bytes.toByteString()) } catch (_: Exception) { false }
    override fun queueSize(): Long =
        try { ws.queueSize() } catch (_: Exception) { Long.MAX_VALUE }
}

/** Java-WebSocket server-side connection. Used by [DirectServer].
 *
 * Java-WebSocket doesn't expose write-buffer depth directly. For LAN
 * direct connections this is usually fine -- the OS-level send buffer
 * fills only when the browser stops reading, at which point the
 * connection is effectively dead and the backend's [send] will start
 * throwing. We return 0 so the backpressure gate is a no-op; the [send]
 * failure path takes care of the rest. */
class JavaWsBackend(private val ws: org.java_websocket.WebSocket) : WsBackend {
    override fun send(text: String): Boolean = try {
        if (!ws.isOpen) return false
        ws.send(text); true
    } catch (_: Exception) { false }

    override fun send(bytes: ByteArray): Boolean = try {
        if (!ws.isOpen) return false
        ws.send(ByteBuffer.wrap(bytes)); true
    } catch (_: Exception) { false }

    override fun queueSize(): Long = 0L
}
