package com.vortex.driver

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray

/**
 * Persisted credentials + bootstrap state for the APK's standalone
 * Vortex-client role (B1). When [isEnrolled] is false, [MainActivity]
 * shows an Enroll button. When true, [DriverService] starts
 * [HubClient] which dials the hub and registers this device.
 *
 * No new permissions: SharedPreferences is private-by-default on
 * Android (mode 0).
 */
object Prefs {
    private const val NAME = "vortex_driver"
    private const val K_ACCOUNT_TOKEN = "account_token"
    private const val K_BOOTSTRAP_URL = "bootstrap_url"
    private const val K_DEVICE_ID     = "device_id"
    private const val K_DEVICE_TOKEN  = "device_token"
    private const val K_DEVICE_NAME   = "device_name"
    private const val K_NODES         = "nodes"   // JSON array of urls
    // B11: direct Turso backend (no hub server).
    private const val K_TURSO_URL     = "turso_url"      // libsql:// or https://
    private const val K_TURSO_TOKEN   = "turso_token"    // bearer JWT
    private const val K_USER_ID       = "user_id"        // signed-in user PK
    private const val K_USERNAME      = "username"
    private const val K_IS_ADMIN      = "is_admin"
    // B11.16: hub URL auto-discovered from Turso node_endpoints. Lives
    // in a separate key from the user's manual override so we can
    // refresh it on a poll without overwriting their explicit pin.
    private const val K_RELAY_DISCOVERED = "relay_discovered"

    fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(NAME, Context.MODE_PRIVATE)

    // ---- B11: Turso DB credentials (Setup screen). ----
    fun tursoUrl(ctx: Context): String? =
        prefs(ctx).getString(K_TURSO_URL, null)
    fun tursoToken(ctx: Context): String? =
        prefs(ctx).getString(K_TURSO_TOKEN, null)
    fun isTursoConfigured(ctx: Context): Boolean =
        !tursoUrl(ctx).isNullOrBlank() && !tursoToken(ctx).isNullOrBlank()

    fun saveTurso(ctx: Context, url: String, token: String) {
        prefs(ctx).edit()
            .putString(K_TURSO_URL,   url.trim())
            .putString(K_TURSO_TOKEN, token.trim())
            .apply()
    }
    fun clearTurso(ctx: Context) {
        prefs(ctx).edit()
            .remove(K_TURSO_URL).remove(K_TURSO_TOKEN)
            .apply()
    }

    // ---- B11.4: optional relay URL (cross-network control). Any
    // Vortex hub running against the same Turso DB will do: HubClient
    // dials it, and DeviceWebActivity falls back to its /devices/{id}
    // page when the direct LAN connection isn't reachable. Empty
    // means LAN-only; the peer-to-peer direct path still works on a
    // shared Wi-Fi without any relay.
    //
    // B11.16: split into manual + discovered. relayUrl() = the
    // effective value (manual wins, else discovered). HubDiscovery
    // periodically refreshes the discovered cache from Turso's
    // node_endpoints table -- the same table the hub already
    // heartbeats its current cloudflared URL into, so the APK can
    // follow a rotating quick-tunnel URL without user intervention.
    fun relayUrl(ctx: Context): String? =
        relayUrlManual(ctx) ?: relayUrlDiscovered(ctx)

    /** The URL the user explicitly typed in Setup. Null when blank. */
    fun relayUrlManual(ctx: Context): String? =
        prefs(ctx).getString(K_BOOTSTRAP_URL, null)
            ?.takeIf { it.startsWith("http://") || it.startsWith("https://") }

    /** The most recent URL HubDiscovery pulled from Turso. Null until
     *  the first successful poll (so brand-new installs without a
     *  manual URL still get the LAN-only fallback). */
    fun relayUrlDiscovered(ctx: Context): String? =
        prefs(ctx).getString(K_RELAY_DISCOVERED, null)
            ?.takeIf { it.startsWith("http://") || it.startsWith("https://") }

    fun saveRelay(ctx: Context, url: String) {
        val trimmed = url.trim().trimEnd('/')
        prefs(ctx).edit().putString(K_BOOTSTRAP_URL, trimmed).apply()
    }
    fun clearRelay(ctx: Context) {
        prefs(ctx).edit().remove(K_BOOTSTRAP_URL).apply()
    }
    /** B11.16: HubDiscovery writes here from the background poller. */
    fun saveRelayDiscovered(ctx: Context, url: String?) {
        val e = prefs(ctx).edit()
        if (url.isNullOrBlank()) e.remove(K_RELAY_DISCOVERED)
        else                     e.putString(K_RELAY_DISCOVERED, url.trim().trimEnd('/'))
        e.apply()
    }

    // ---- B11: signed-in user (after sign-in / register). ----
    fun userId(ctx: Context): Long =
        prefs(ctx).getLong(K_USER_ID, -1L)
    fun username(ctx: Context): String? =
        prefs(ctx).getString(K_USERNAME, null)
    fun isAdmin(ctx: Context): Boolean =
        prefs(ctx).getBoolean(K_IS_ADMIN, false)
    fun isSignedIn(ctx: Context): Boolean = userId(ctx) > 0

    fun saveSession(ctx: Context, userId: Long, username: String, isAdmin: Boolean) {
        prefs(ctx).edit()
            .putLong(K_USER_ID, userId)
            .putString(K_USERNAME, username)
            .putBoolean(K_IS_ADMIN, isAdmin)
            .apply()
    }
    fun clearSession(ctx: Context) {
        prefs(ctx).edit()
            .remove(K_USER_ID).remove(K_USERNAME).remove(K_IS_ADMIN)
            .apply()
    }

    fun isEnrolled(ctx: Context): Boolean =
        !deviceId(ctx).isNullOrBlank() && !deviceToken(ctx).isNullOrBlank()

    // ---- account-level (used by EnrollActivity to talk to the hub) ----
    fun accountToken(ctx: Context): String? =
        prefs(ctx).getString(K_ACCOUNT_TOKEN, null)
    fun bootstrapUrl(ctx: Context): String? =
        prefs(ctx).getString(K_BOOTSTRAP_URL, null)

    fun saveBootstrap(ctx: Context, accountToken: String, hubUrl: String) {
        prefs(ctx).edit()
            .putString(K_ACCOUNT_TOKEN, accountToken.trim())
            .putString(K_BOOTSTRAP_URL, hubUrl.trim().trimEnd('/'))
            .apply()
    }

    // ---- device-level (the actual creds for the WS to the hub) ----
    fun deviceId(ctx: Context): String? =
        prefs(ctx).getString(K_DEVICE_ID, null)
    fun deviceToken(ctx: Context): String? =
        prefs(ctx).getString(K_DEVICE_TOKEN, null)
    fun deviceName(ctx: Context): String? =
        prefs(ctx).getString(K_DEVICE_NAME, null)

    fun nodes(ctx: Context): List<String> {
        val raw = prefs(ctx).getString(K_NODES, null) ?: return emptyList()
        return try {
            val a = JSONArray(raw)
            (0 until a.length()).mapNotNull { i ->
                a.optString(i).takeIf { it.isNotBlank() }
            }
        } catch (_: Exception) { emptyList() }
    }

    /** Persist what /api/enroll returned. Bootstrap URL is kept as the
     *  first node so reconnect always has somewhere to dial. */
    fun saveDevice(
        ctx: Context,
        deviceId: String,
        deviceToken: String,
        name: String?,
        nodes: List<String>,
    ) {
        val merged = LinkedHashSet<String>()
        bootstrapUrl(ctx)?.takeIf { it.isNotBlank() }?.let { merged += it }
        nodes.filter { it.isNotBlank() }.forEach { merged += it.trimEnd('/') }
        prefs(ctx).edit()
            .putString(K_DEVICE_ID,    deviceId)
            .putString(K_DEVICE_TOKEN, deviceToken)
            .putString(K_DEVICE_NAME,  (name ?: "").trim().ifBlank { android.os.Build.MODEL })
            .putString(K_NODES,        JSONArray(merged.toList()).toString())
            .apply()
    }

    /** Forget everything; the next launch returns to the Enroll screen. */
    fun clear(ctx: Context) {
        prefs(ctx).edit().clear().apply()
    }
}
