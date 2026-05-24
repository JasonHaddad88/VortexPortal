package com.vortex.driver

import okhttp3.WebSocket
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject

/**
 * A streaming sink for hub-bound binary frames (B2.2).
 *
 * The hub's stream protocol is:
 *
 *   {"type":"stream_start", "id":rid, "content_type":"image/jpeg"}
 *   {"type":"stream_chunk_header", "id":rid}    <-- text
 *   <binary frame>                              <-- the JPEG bytes
 *   ... repeat ...
 *   {"type":"stream_end", "id":rid, "frames":N}
 *
 * The `stream_chunk_header` text frame MUST be immediately followed by the
 * binary frame with no other writer interleaving. Multiple concurrent ops
 * on the same WebSocket would otherwise race -- so we serialize all sends
 * through a shared send lock (one per WebSocket, passed in by HubClient).
 *
 * Idempotent close: [sendEnd] / [sendError] short-circuit if already ended,
 * so finally-blocks can call sendEnd safely after onError already fired.
 */
class WsStreamSink(
    private val ws: WebSocket,
    private val rid: String,
    /** Shared mutex from HubClient -- the SAME object instance for every
     *  stream on this WebSocket. Guards atomic header+binary send pairs
     *  and serializes against unrelated text sends (responses, direct_info). */
    private val sendLock: Any,
) {
    @Volatile private var started = false
    @Volatile private var ended = false
    @Volatile var framesSent: Long = 0L; private set

    fun sendStart(contentType: String = "image/jpeg", size: Long? = null): Boolean {
        if (started || ended) return false
        started = true
        val msg = JSONObject()
            .put("type", "stream_start")
            .put("id", rid)
            .put("content_type", contentType)
        if (size != null) msg.put("size", size)
        return synchronized(sendLock) { trySend(msg.toString()) }
    }

    /** Atomic header+binary pair. Returns false if the socket rejected the
     *  send (queue full / closed) -- caller should stop producing frames. */
    fun sendChunk(bytes: ByteArray): Boolean {
        if (ended) return false
        if (!started) sendStart()  // be forgiving -- streams in the wild forget
        val header = JSONObject()
            .put("type", "stream_chunk_header")
            .put("id", rid)
            .toString()
        return synchronized(sendLock) {
            val a = trySend(header)
            if (!a) return@synchronized false
            val b = trySend(bytes.toByteString())
            if (b) framesSent++
            b
        }
    }

    fun sendEnd() {
        if (ended) return
        ended = true
        val msg = JSONObject()
            .put("type", "stream_end")
            .put("id", rid)
            .put("frames", framesSent)
            .toString()
        synchronized(sendLock) { trySend(msg) }
    }

    /** End the stream with an error response frame. Used when the engine
     *  fails mid-stream (camera lost, projection revoked). The hub treats
     *  a {ok:false} response after stream_start as a fatal stream error. */
    fun sendError(message: String) {
        if (ended) return
        ended = true
        // Send the stream_end first so the hub stops reading chunks, then
        // an error response so the browser gets a clear failure mode.
        val end = JSONObject()
            .put("type", "stream_end")
            .put("id", rid)
            .put("frames", framesSent)
            .put("error", message)
            .toString()
        synchronized(sendLock) { trySend(end) }
    }

    private fun trySend(text: String): Boolean = try { ws.send(text) } catch (_: Exception) { false }
    private fun trySend(bytes: ByteString): Boolean = try { ws.send(bytes) } catch (_: Exception) { false }
}
