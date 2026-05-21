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

    fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(NAME, Context.MODE_PRIVATE)

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
