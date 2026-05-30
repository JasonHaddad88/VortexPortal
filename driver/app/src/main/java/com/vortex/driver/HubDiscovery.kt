package com.vortex.driver

import android.content.Context

/**
 * B11.16: auto-discover the hub's public URL from Turso.
 *
 * The hub already publishes its current reachable URL to
 * `node_endpoints` every 30 s (V5.9). That table holds whatever
 * URL the hub is currently reachable at -- including a rotating
 * `*.trycloudflare.com` quick-tunnel URL the hub picked at boot.
 * If we pull from there on the APK side, the user never has to
 * re-paste the URL when the tunnel rotates.
 *
 * Resolution order (used by [resolveRelayUrl]):
 *
 *   1. **Manual override**: user typed a URL into Setup.
 *      Wins so the user can pin to a specific named tunnel /
 *      VPS / Tailscale Funnel hostname even when other URLs
 *      are also live.
 *   2. **Cached discovery**: the freshest URL the last poll
 *      saved to Prefs. Survives offline-startup and avoids
 *      blocking the UI thread waiting on Turso.
 *   3. **Null**: no relay -- LAN-direct only.
 *
 * The actual Turso query happens off-thread (called from
 * [DriverService] every ~60 s). This file just owns the
 * cache key + the query/parsing logic.
 */
object HubDiscovery {

    /** Treat node_endpoints rows older than this as stale. The hub
     *  heartbeats every 30 s, so 5 minutes is generous -- if the hub
     *  has been silent that long, the URL is probably wrong. */
    const val STALE_AFTER_SEC: Long = 300

    /** Pull the freshest non-loopback node_endpoints row from Turso.
     *  Returns null on any error (no creds, table missing, network
     *  down). Caller is responsible for off-thread invocation. */
    fun queryFreshest(client: TursoClient): String? {
        val cutoff = (System.currentTimeMillis() / 1000L) - STALE_AFTER_SEC
        val rows = try {
            client.execute(
                "SELECT url, last_seen FROM node_endpoints " +
                "WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT 8",
                listOf(cutoff),
            ).rows
        } catch (_: TursoError) {
            return null  // table may not exist yet on a fresh DB
        }
        // Skip loopback URLs (a misconfigured hub might publish 127.0.0.1
        // from a Termux self-loop; that's useless to an APK on a
        // different device). Prefer the newest non-loopback row.
        for (r in rows) {
            val url = (r["url"] as? String)?.trim()?.trimEnd('/') ?: continue
            if (url.isEmpty()) continue
            if (isLoopback(url)) continue
            return url
        }
        return null
    }

    /** Combine the manual override + the cached discovery into a single
     *  URL to use. Manual wins. */
    fun resolveRelayUrl(ctx: Context): String? {
        val manual = Prefs.relayUrlManual(ctx)
        if (!manual.isNullOrBlank()) return manual
        return Prefs.relayUrlDiscovered(ctx)
    }

    private fun isLoopback(url: String): Boolean {
        // Lightweight host extraction -- avoid pulling in Uri for a
        // 30-call-per-startup path.
        val schemeEnd = url.indexOf("://")
        if (schemeEnd < 0) return false
        var host = url.substring(schemeEnd + 3)
        val slash = host.indexOf('/')
        if (slash >= 0) host = host.substring(0, slash)
        val colon = host.indexOf(':')
        if (colon >= 0) host = host.substring(0, colon)
        host = host.lowercase()
        return host == "localhost" || host == "::1" ||
               host == "0.0.0.0" || host.startsWith("127.")
    }
}
