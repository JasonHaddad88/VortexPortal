package com.vortex.driver

import org.json.JSONObject

/**
 * B11.15: queued commands for offline peers.
 *
 * The pattern matches what Google's "Find My Device → Erase" does:
 * the controller writes a row describing what to do; the peer device
 * polls when it's online and executes any pending rows.
 *
 * Schema:
 *
 *   CREATE TABLE IF NOT EXISTS device_commands (
 *     id           INTEGER PRIMARY KEY AUTOINCREMENT,
 *     device_id    TEXT NOT NULL,
 *     op           TEXT NOT NULL,
 *     args_json    TEXT NOT NULL DEFAULT '{}',
 *     created_by   TEXT,                    -- owner_id of the controller
 *     created_at   INTEGER NOT NULL,        -- unix seconds
 *     executed_at  INTEGER,                 -- null until run
 *     result_json  TEXT,                    -- JSON result on success
 *     error        TEXT                     -- non-null on failure
 *   );
 *   CREATE INDEX IF NOT EXISTS idx_device_commands_pending
 *     ON device_commands(device_id) WHERE executed_at IS NULL;
 *
 * Whitelist v1: only ops that produce small JSON results (no streams).
 * Stream-producing ops (camera_capture, record_audio) deferred to a
 * follow-up that also needs somewhere for the blob to land.
 */
object CommandQueue {

    /** Ops the peer is willing to dequeue + run unattended.  Anything
     *  not in this set is silently skipped (a malicious or buggy
     *  controller can't force the peer to run, e.g., a screen_stream
     *  through the queue path). */
    val WHITELIST: Set<String> = setOf(
        "keepawake",      // existing B4 unary
        "location_once",  // B11.15: unary wrapper around the B4 `location` stream
        "play_sound",     // B11.15: "where is my phone?" alarm
    )

    /** Cap on how many pending rows we'll pull in a single poll.
     *  Prevents a long backlog from blocking the publisher loop. */
    const val POLL_LIMIT: Int = 16

    private const val SCHEMA = """
        CREATE TABLE IF NOT EXISTS device_commands (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id    TEXT NOT NULL,
            op           TEXT NOT NULL,
            args_json    TEXT NOT NULL DEFAULT '{}',
            created_by   TEXT,
            created_at   INTEGER NOT NULL,
            executed_at  INTEGER,
            result_json  TEXT,
            error        TEXT
        )
    """

    private const val INDEX = """
        CREATE INDEX IF NOT EXISTS idx_device_commands_pending
            ON device_commands(device_id) WHERE executed_at IS NULL
    """

    /** Create the table + partial index. Idempotent. */
    fun ensureSchema(client: TursoClient) {
        client.execute(SCHEMA.trimIndent())
        // The partial index syntax (`WHERE executed_at IS NULL`) is
        // SQLite-only but Turso uses SQLite under the hood, so this
        // works. If a future Turso engine rejects it, swap to a plain
        // index on (device_id, executed_at) -- minor speed hit only.
        try { client.execute(INDEX.trimIndent()) } catch (_: TursoError) {}
    }

    /** Controller side: drop a row to be picked up by the peer on its
     *  next poll. Returns the new row's id on success, throws on
     *  TursoError. */
    fun enqueue(
        client: TursoClient,
        deviceId: String,
        op: String,
        args: JSONObject = JSONObject(),
        createdBy: String? = null,
    ): Long {
        val now = System.currentTimeMillis() / 1000L
        val r = client.execute(
            "INSERT INTO device_commands " +
            "(device_id, op, args_json, created_by, created_at) " +
            "VALUES (?, ?, ?, ?, ?)",
            listOf(deviceId, op, args.toString(), createdBy, now),
        )
        return r.lastInsertRowId ?: 0L
    }

    /** Peer side: pull pending commands for [deviceId]. Oldest first
     *  so a backlog drains in submission order. */
    fun pendingFor(client: TursoClient, deviceId: String): List<Pending> {
        val rows = try {
            client.execute(
                "SELECT id, op, args_json, created_at FROM device_commands " +
                "WHERE device_id = ? AND executed_at IS NULL " +
                "ORDER BY id ASC LIMIT ?",
                listOf(deviceId, POLL_LIMIT.toLong()),
            ).rows
        } catch (_: TursoError) {
            return emptyList()
        }
        return rows.mapNotNull { r ->
            val id = (r["id"] as? Long) ?: return@mapNotNull null
            val op = (r["op"] as? String) ?: return@mapNotNull null
            val argsRaw = (r["args_json"] as? String) ?: "{}"
            val args = try { JSONObject(argsRaw) } catch (_: Throwable) { JSONObject() }
            val createdAt = (r["created_at"] as? Long) ?: 0L
            Pending(id, op, args, createdAt)
        }
    }

    /** Mark a row done. Either [result] or [error] should be non-null;
     *  both null is treated as "executed with no result". */
    fun markExecuted(
        client: TursoClient,
        id: Long,
        result: JSONObject? = null,
        error: String? = null,
    ) {
        val now = System.currentTimeMillis() / 1000L
        try {
            client.execute(
                "UPDATE device_commands SET executed_at = ?, " +
                "result_json = ?, error = ? WHERE id = ?",
                listOf(now, result?.toString(), error, id),
            )
        } catch (_: TursoError) { /* best-effort -- next poll will retry */ }
    }

    data class Pending(
        val id: Long,
        val op: String,
        val args: JSONObject,
        val createdAt: Long,
    )
}
