package com.vortex.driver

import org.json.JSONObject

/**
 * A streaming sink for outbound binary frames (B2.2; backend-agnostic B3).
 *
 * The stream protocol (matches the Python agent + hub):
 *
 *   {"type":"stream_start", "id":rid, "content_type":"image/jpeg"}
 *   {"type":"stream_chunk_header", "id":rid}    <-- text
 *   <binary frame>                              <-- the JPEG bytes
 *   ... repeat ...
 *   {"type":"stream_end", "id":rid, "frames":N}
 *
 * The `stream_chunk_header` text frame MUST be immediately followed by the
 * binary frame with no other writer interleaving. Multiple concurrent ops
 * on the same socket would otherwise race -- so we serialize all sends
 * through a shared send lock (one per backend connection, passed in by
 * the owner: HubClient for hub-bound, DirectServer for browser-bound).
 *
 * Backend-agnostic: the actual socket type lives behind [WsBackend], so
 * the same dispatcher + handlers work for both OkHttp client and
 * Java-WebSocket server connections.
 */
class WsStreamSink(
    private val backend: WsBackend,
    private val rid: String,
    private val sendLock: Any,
    /** Bytes already buffered for transport at which we start dropping
     *  binary chunks. Default ~256 KB. */
    private val backpressureBytes: Long = 256L * 1024L,
) {
    @Volatile private var started = false
    @Volatile private var ended = false
    @Volatile var framesSent: Long = 0L; private set
    @Volatile var framesDropped: Long = 0L; private set

    /** True if the WS write queue is below the backpressure threshold.
     *  Engines call this before encoding the next frame -- skipping
     *  here saves us the JPEG encode CPU, not just the send. */
    fun isReady(): Boolean {
        if (ended) return false
        return backend.queueSize() < backpressureBytes
    }

    fun sendStart(contentType: String = "image/jpeg", size: Long? = null): Boolean {
        if (started || ended) return false
        started = true
        val msg = JSONObject()
            .put("type", "stream_start")
            .put("id", rid)
            .put("content_type", contentType)
        if (size != null) msg.put("size", size)
        return synchronized(sendLock) { backend.send(msg.toString()) }
    }

    /** B5: variant that lets the op handler attach extra fields to the
     *  stream_start frame -- needed for video/h264 streams (codec string,
     *  width, height, base64 SPS+PPS in `csd_base64`). */
    fun sendStartWith(annotate: (JSONObject) -> Unit): Boolean {
        if (started || ended) return false
        started = true
        val msg = JSONObject().put("type", "stream_start").put("id", rid)
        annotate(msg)
        return synchronized(sendLock) { backend.send(msg.toString()) }
    }

    fun sendChunk(bytes: ByteArray): Boolean = sendChunkAnnotated(bytes, null)

    /** B5: variant that lets the op handler attach extra fields to the
     *  stream_chunk_header (e.g. `kf` keyframe flag + `pts` micros for
     *  video/h264). Direct-WS browsers thread these through to their
     *  VideoDecoder; relay-path consumers ignore them silently. */
    fun sendChunkAnnotated(bytes: ByteArray, annotate: ((JSONObject) -> Unit)?): Boolean {
        if (ended) return false
        if (!isReady()) { framesDropped++; return false }
        if (!started) sendStart()
        val header = JSONObject().put("type", "stream_chunk_header").put("id", rid)
        annotate?.invoke(header)
        return synchronized(sendLock) {
            val a = backend.send(header.toString())
            if (!a) return@synchronized false
            val b = backend.send(bytes)
            if (b) framesSent++ else framesDropped++
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
        synchronized(sendLock) { backend.send(msg) }
    }

    fun sendError(message: String) {
        if (ended) return
        ended = true
        val end = JSONObject()
            .put("type", "stream_end")
            .put("id", rid)
            .put("frames", framesSent)
            .put("error", message)
            .toString()
        synchronized(sendLock) { backend.send(end) }
    }
}
