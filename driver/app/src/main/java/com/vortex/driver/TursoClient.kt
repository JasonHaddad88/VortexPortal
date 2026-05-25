package com.vortex.driver

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * Hrana-over-HTTP client for Turso, mirroring `hub/db.py::_TursoHttpBackend`
 * byte-for-byte. The APK can read/write the same `users`, `devices`, etc.
 * tables the webapp uses -- no FastAPI server in between.
 *
 * Wire shape:
 *   POST {base}/v2/pipeline
 *   Authorization: Bearer {token}
 *   Body: {"requests":[
 *     {"type":"execute","stmt":{"sql":"...","args":[...],"want_rows":true}},
 *     ...,
 *     {"type":"close"}
 *   ]}
 *
 * Each argument is `{"type":"text|integer|float|null|blob", "value":...}`
 * (integers + bools are strings to preserve i64 precision). Result rows
 * are columns + cells with the same wrapper.
 *
 * Call from a background thread (OkHttp is sync here). All methods throw
 * [TursoError] on transport / protocol / SQL errors.
 */
class TursoClient(rawUrl: String, private val authToken: String) {

    private val endpoint: String = run {
        val u = rawUrl.trim().trimEnd('/')
        when {
            u.startsWith("libsql://") -> "https://" + u.removePrefix("libsql://")
            u.startsWith("wss://")    -> "https://" + u.removePrefix("wss://")
            u.startsWith("ws://")     -> "http://"  + u.removePrefix("ws://")
            else                      -> u
        } + "/v2/pipeline"
    }

    private val http: OkHttpClient = OkHttpClient.Builder()
        .callTimeout(15, TimeUnit.SECONDS)
        .connectTimeout(8, TimeUnit.SECONDS)
        .build()

    /** A single statement. Convenience over [pipeline]. */
    fun execute(sql: String, args: List<Any?> = emptyList()): Result =
        pipeline(listOf(Stmt(sql, args))).first()

    /** Execute a list of statements in one HTTP round-trip. Server
     *  auto-commits per-pipeline; for a real transaction you can wrap
     *  in BEGIN / COMMIT statements (Turso accepts those). */
    fun pipeline(stmts: List<Stmt>): List<Result> {
        if (stmts.isEmpty()) return emptyList()
        val requests = JSONArray()
        for (s in stmts) {
            val stmtJson = JSONObject()
                .put("sql", s.sql)
                .put("args", argsToHrana(s.args))
                .put("want_rows", true)
            requests.put(JSONObject().put("type", "execute").put("stmt", stmtJson))
        }
        requests.put(JSONObject().put("type", "close"))
        val body = JSONObject().put("requests", requests).toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url(endpoint)
            .post(body)
            .header("Content-Type", "application/json")
            .also { if (authToken.isNotBlank()) it.header("Authorization", "Bearer $authToken") }
            .build()
        val resp = try { http.newCall(req).execute() }
                   catch (e: Throwable) { throw TursoError("transport: ${e.javaClass.simpleName}: ${e.message}") }
        val text = resp.use { it.body?.string().orEmpty() }
        if (!resp.isSuccessful) throw TursoError("HTTP ${resp.code}: ${text.take(200)}")
        return parsePipelineResponse(text, stmts.size)
    }

    // ---- response parsing -----------------------------------------------

    private fun parsePipelineResponse(text: String, expected: Int): List<Result> {
        val root = try { JSONObject(text) }
                   catch (e: Throwable) { throw TursoError("bad json: ${e.message}") }
        val results = root.optJSONArray("results") ?: throw TursoError("missing results: ${text.take(200)}")
        val out = ArrayList<Result>(expected)
        for (i in 0 until results.length()) {
            val item = results.optJSONObject(i) ?: continue
            val t = item.optString("type")
            if (t == "error") {
                val err = item.optJSONObject("error")
                throw TursoError("Turso error: ${err?.optString("message") ?: item}")
            }
            val response = item.optJSONObject("response") ?: continue
            if (response.optString("type") != "execute") continue
            val res = response.optJSONObject("result") ?: JSONObject()
            out.add(parseExecuteResult(res))
        }
        return out
    }

    private fun parseExecuteResult(r: JSONObject): Result {
        val colsArr = r.optJSONArray("cols") ?: JSONArray()
        val cols = (0 until colsArr.length()).map {
            colsArr.optJSONObject(it)?.optString("name", "") ?: ""
        }
        val rowsArr = r.optJSONArray("rows") ?: JSONArray()
        val rows = ArrayList<Map<String, Any?>>(rowsArr.length())
        for (i in 0 until rowsArr.length()) {
            val rowArr = rowsArr.optJSONArray(i) ?: continue
            val map = LinkedHashMap<String, Any?>(cols.size)
            for (c in cols.indices) {
                map[cols[c]] = hranaCell(rowArr.opt(c))
            }
            rows.add(map)
        }
        val rowCount = r.optInt("affected_row_count", 0)
        val lastRowId = when (val v = r.opt("last_insert_rowid")) {
            null, JSONObject.NULL -> null
            is String -> v.toLongOrNull()
            is Number -> v.toLong()
            else -> null
        }
        return Result(cols = cols, rows = rows, rowCount = rowCount, lastInsertRowId = lastRowId)
    }

    private fun hranaCell(c: Any?): Any? {
        if (c !is JSONObject) return null
        return when (c.optString("type")) {
            "null", "" -> null
            "integer"  -> c.optString("value").toLongOrNull()
            "float"    -> c.optString("value").toDoubleOrNull() ?: c.opt("value")
            "blob"     -> try { android.util.Base64.decode(c.optString("base64", ""), android.util.Base64.NO_WRAP) }
                          catch (_: Throwable) { ByteArray(0) }
            else       -> c.optString("value")     // text + unknown -> string
        }
    }

    private fun argsToHrana(args: List<Any?>): JSONArray {
        val out = JSONArray()
        for (v in args) {
            val wrapped = when (v) {
                null            -> JSONObject().put("type", "null")
                is Boolean      -> JSONObject().put("type", "integer").put("value", if (v) "1" else "0")
                is Int, is Long -> JSONObject().put("type", "integer").put("value", v.toString())
                is Float, is Double -> JSONObject().put("type", "float").put("value", v)
                is ByteArray    -> JSONObject().put("type", "blob")
                    .put("base64", android.util.Base64.encodeToString(v, android.util.Base64.NO_WRAP))
                else            -> JSONObject().put("type", "text").put("value", v.toString())
            }
            out.put(wrapped)
        }
        return out
    }

    // ---- value types ----------------------------------------------------

    data class Stmt(val sql: String, val args: List<Any?> = emptyList())

    data class Result(
        val cols: List<String>,
        val rows: List<Map<String, Any?>>,
        val rowCount: Int,
        val lastInsertRowId: Long?,
    ) {
        fun firstRow(): Map<String, Any?>? = rows.firstOrNull()
    }
}

/** Thrown by [TursoClient] for any transport / protocol / SQL failure. */
class TursoError(message: String) : RuntimeException(message)
