package com.vortex.driver

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.security.SecureRandom

/**
 * B11.2: peer-to-peer discovery via Turso.
 *
 * Each enrolled device publishes its direct-WS endpoint to a small
 * `device_peers` table; other devices read that table to learn where
 * to dial. The webapp's own `device_presence` table stays untouched
 * (it's hub-written) so this is purely additive.
 *
 * Schema (created lazily on first publish):
 *
 *   CREATE TABLE IF NOT EXISTS device_peers (
 *     device_id  TEXT PRIMARY KEY,
 *     hosts      TEXT NOT NULL,   -- JSON array of "host:port" strings
 *     port       INTEGER,         -- redundant convenience; null if 0
 *     ticket     TEXT NOT NULL,   -- short-lived auth token
 *     updated_at INTEGER NOT NULL
 *   );
 *
 * `ticket` is regenerated on every publish so a stolen value expires
 * the next time the device refreshes (default 60 s).  The
 * `DirectServer.armTicket()` machinery on the peer side validates one-
 * shot consumption.
 *
 * Stale-row TTL: callers should treat rows older than 90 s as
 * "offline" (gives a 30 s slop window over the 60 s publish cadence).
 */
object PeerRegistry {

    private const val SCHEMA = """
        CREATE TABLE IF NOT EXISTS device_peers (
            device_id  TEXT PRIMARY KEY,
            hosts      TEXT NOT NULL,
            port       INTEGER,
            ticket     TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """

    /** Stale threshold for "is this peer online?" Default 90 s; matches
     *  the publish cadence (60 s) plus a generous slop window for
     *  flaky links. */
    const val STALE_AFTER_SEC: Long = 90

    /** Run a schema-create. Safe to call repeatedly; uses IF NOT EXISTS. */
    fun ensureSchema(client: TursoClient) {
        client.execute(SCHEMA.trimIndent())
    }

    /**
     * Publish (or refresh) this device's direct-WS endpoint. The list of
     * `hosts` should already include the port (e.g. "192.168.1.5:41423");
     * we also store `port` separately for convenience.
     *
     * Throws [TursoError] on transport or SQL failure.
     */
    fun publish(
        client: TursoClient,
        deviceId: String,
        hosts: List<String>,
        port: Int,
        ticket: String,
    ) {
        val hostsJson = JSONArray(hosts).toString()
        val now = System.currentTimeMillis() / 1000L
        // UPSERT via INSERT OR REPLACE -- the row's a single-key table
        // so a full-row write each refresh is fine; nothing else owns
        // these columns.
        client.execute(
            "INSERT OR REPLACE INTO device_peers " +
            "(device_id, hosts, port, ticket, updated_at) VALUES (?, ?, ?, ?, ?)",
            listOf(
                deviceId, hostsJson,
                if (port > 0) port.toLong() else null,
                ticket, now,
            ),
        )
    }

    /** Best-effort wipe of THIS device's row (called on sign-out). */
    fun retract(client: TursoClient, deviceId: String) {
        try {
            client.execute("DELETE FROM device_peers WHERE device_id = ?", listOf(deviceId))
        } catch (_: TursoError) { /* best-effort */ }
    }

    /** Map of device_id -> presence info, for every fresh row (
     *  updated_at within [STALE_AFTER_SEC]). Devices not in the map are
     *  treated as offline by the caller. */
    fun listFresh(client: TursoClient): Map<String, PeerInfo> {
        val cutoff = (System.currentTimeMillis() / 1000L) - STALE_AFTER_SEC
        val rows = try {
            client.execute(
                "SELECT device_id, hosts, port, ticket, updated_at " +
                "FROM device_peers WHERE updated_at >= ?",
                listOf(cutoff),
            ).rows
        } catch (_: TursoError) {
            return emptyMap()  // table may not exist yet on a fresh DB
        }
        val out = LinkedHashMap<String, PeerInfo>(rows.size)
        for (r in rows) {
            val id = (r["device_id"] as? String) ?: continue
            val hostsRaw = (r["hosts"] as? String) ?: continue
            val hosts = try {
                val a = JSONArray(hostsRaw)
                (0 until a.length()).mapNotNull { i -> a.optString(i).takeIf { it.isNotBlank() } }
            } catch (_: Throwable) { emptyList() }
            out[id] = PeerInfo(
                deviceId  = id,
                hosts     = hosts,
                port      = (r["port"] as? Long)?.toInt() ?: 0,
                ticket    = (r["ticket"] as? String) ?: "",
                updatedAt = (r["updated_at"] as? Long) ?: 0L,
            )
        }
        return out
    }

    /** Mint a fresh ticket. We don't try to be too clever -- 32 chars
     *  of url-safe-ish entropy. Validated server-side by [DirectServer]
     *  which already implements one-shot consumption + TTL. */
    fun newTicket(): String {
        val bytes = ByteArray(24).also { SecureRandom().nextBytes(it) }
        return android.util.Base64.encodeToString(
            bytes, android.util.Base64.NO_WRAP or android.util.Base64.URL_SAFE
                   or android.util.Base64.NO_PADDING,
        )
    }

    data class PeerInfo(
        val deviceId: String,
        val hosts: List<String>,
        val port: Int,
        val ticket: String,
        val updatedAt: Long,
    )
}

/** Convenience: pull Turso creds from Prefs, hand back a configured
 *  client. Returns null if the user hasn't completed Setup. */
internal fun tursoClientFrom(ctx: Context): TursoClient? {
    val url = Prefs.tursoUrl(ctx)?.takeIf { it.isNotBlank() } ?: return null
    val tok = Prefs.tursoToken(ctx)?.takeIf { it.isNotBlank() } ?: return null
    return TursoClient(url, tok)
}
