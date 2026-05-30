package com.vortex.driver

import kotlinx.coroutines.CancellationException
import org.json.JSONObject

/**
 * Routes inbound `{type:"request", id, op, args}` text frames to a
 * registered handler.
 *
 * Two handler flavors (B2.2):
 *
 *  - **Unary** -- returns a JSONObject result that gets wrapped into a
 *    single `{type:"response", id, ok, result}` text frame. The dispatcher
 *    handles serialization + error wrapping. Used by `device_info`,
 *    `input`, etc.
 *
 *  - **Stream** -- a suspend handler that owns a [WsStreamSink] and pushes
 *    `stream_start` + N `stream_chunk_header`+binary pairs + `stream_end`.
 *    The handler is invoked in its own coroutine by [HubClient] and runs
 *    until cancellation. Used by `screen_stream`, `camera_stream`.
 *
 * [classify] is the entry point: it parses the frame and returns an
 * [Outcome] describing what to do. [HubClient] then sends Unary responses
 * synchronously and launches Stream handlers as long-lived coroutines that
 * it can cancel on disconnect.
 */
class OpDispatcher {

    fun interface UnaryHandler {
        @Throws(Exception::class)
        fun call(args: JSONObject): JSONObject
    }

    /**
     * Stream handler signature. Receives args + a sink wired to the WS;
     * MUST call sink.sendStart() at least once, push chunks, and is
     * expected to suspend (e.g. awaitCancellation) until the WebSocket
     * cancels it. The dispatcher will call sink.sendEnd() / sink.sendError()
     * for you if the handler throws or returns.
     */
    fun interface StreamHandler {
        suspend fun run(args: JSONObject, sink: WsStreamSink)
    }

    private val unary  = java.util.concurrent.ConcurrentHashMap<String, UnaryHandler>()
    private val stream = java.util.concurrent.ConcurrentHashMap<String, StreamHandler>()

    fun register(op: String, handler: UnaryHandler) { unary[op] = handler }
    fun registerStream(op: String, handler: StreamHandler) { stream[op] = handler }

    /** B11.15: synchronous unary call for self-dispatch from background
     *  jobs (e.g. the queued-command poller). Skips the WS request /
     *  response wrapping classify() does for inbound frames. */
    @Throws(Exception::class)
    fun runUnary(op: String, args: JSONObject): JSONObject {
        val h = unary[op] ?: throw RuntimeException("unknown unary op: $op")
        return h.call(args)
    }

    sealed class Outcome {
        /** A complete response text frame, ready to send. */
        data class Unary(val responseJson: String) : Outcome()
        /** Caller should launch a coroutine that runs [handler] with a
         *  fresh WsStreamSink for this rid. */
        data class Stream(
            val rid: String,
            val op: String,
            val args: JSONObject,
            val handler: StreamHandler,
        ) : Outcome()
        /** Frame wasn't a request, or was malformed. */
        data class Reject(val responseJson: String?) : Outcome()
    }

    /** Parse one inbound text frame and decide what to do. */
    fun classify(text: String): Outcome {
        val msg = try { JSONObject(text) } catch (_: Exception) {
            return Outcome.Reject(null)
        }
        if (msg.optString("type") != "request") return Outcome.Reject(null)
        val rid = msg.optString("id", "")
        val op  = msg.optString("op",  "")
        val args = msg.optJSONObject("args") ?: JSONObject()
        if (rid.isEmpty() || op.isEmpty()) {
            return Outcome.Reject(errorResponse(rid, "bad request: missing id/op"))
        }
        // Stream handlers take precedence -- same-named unary is not allowed,
        // and registering both is a bug we'd rather fail loudly than silently.
        stream[op]?.let { return Outcome.Stream(rid, op, args, it) }
        val h = unary[op]
            ?: return Outcome.Reject(errorResponse(rid, "unknown op: $op"))
        return try {
            val result = h.call(args)
            Outcome.Unary(JSONObject()
                .put("type", "response")
                .put("id", rid)
                .put("ok", true)
                .put("result", result)
                .toString())
        } catch (e: Exception) {
            Outcome.Reject(errorResponse(rid, "${e.javaClass.simpleName}: ${e.message ?: ""}"))
        }
    }

    /** Wrap an exception thrown by a stream handler into the response
     *  frame the dispatcher would have sent for a unary op. Used by
     *  HubClient when a stream handler fails BEFORE sending stream_start
     *  (so the hub sees a normal {ok:false} and can return an HTTP 502
     *  instead of a half-opened MJPEG body). */
    fun streamSetupError(rid: String, t: Throwable): String {
        val name = (t as? CancellationException)?.message ?: t.javaClass.simpleName
        val msg = t.message ?: ""
        return errorResponse(rid, "$name: $msg")
    }

    private fun errorResponse(rid: String, msg: String): String =
        JSONObject()
            .put("type", "response")
            .put("id", rid)
            .put("ok", false)
            .put("error", msg)
            .toString()
}
