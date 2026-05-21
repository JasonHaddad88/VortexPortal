package com.vortex.driver

import org.json.JSONObject

/**
 * Routes `{type:"request", id, op, args}` text frames to a registered
 * handler and produces `{type:"response", id, ok, result|error}` strings.
 *
 * Stream ops are out of scope for B1 (B2 will add them — they need to
 * push `stream_start` + N `stream_chunk_header`+binary pairs +
 * `stream_end`, which means handlers will also need a sink).
 *
 * Thread-safe registration via a concurrent map; dispatch itself is
 * called from the HubClient's IO thread.
 */
class OpDispatcher {

    fun interface UnaryHandler {
        @Throws(Exception::class)
        fun call(args: JSONObject): JSONObject
    }

    private val handlers = java.util.concurrent.ConcurrentHashMap<String, UnaryHandler>()

    fun register(op: String, handler: UnaryHandler) {
        handlers[op] = handler
    }

    /** Parse + dispatch one inbound text frame. Returns the response
     *  text to send back (or null if the frame wasn't a request). */
    fun handle(text: String): String? {
        val msg = try { JSONObject(text) } catch (_: Exception) { return null }
        if (msg.optString("type") != "request") return null
        val rid = msg.optString("id", "")
        val op  = msg.optString("op",  "")
        val args = msg.optJSONObject("args") ?: JSONObject()
        if (rid.isEmpty() || op.isEmpty()) {
            return errorResponse(rid, "bad request: missing id/op")
        }
        val handler = handlers[op]
            ?: return errorResponse(rid, "unknown op: $op")
        return try {
            val result = handler.call(args)
            JSONObject()
                .put("type", "response")
                .put("id", rid)
                .put("ok", true)
                .put("result", result)
                .toString()
        } catch (e: Exception) {
            errorResponse(rid, "${e.javaClass.simpleName}: ${e.message ?: ""}")
        }
    }

    private fun errorResponse(rid: String, msg: String): String =
        JSONObject()
            .put("type", "response")
            .put("id", rid)
            .put("ok", false)
            .put("error", msg)
            .toString()
}
