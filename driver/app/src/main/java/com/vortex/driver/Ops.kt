package com.vortex.driver

import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject

/**
 * Native ops for the APK's standalone client role. Initially just
 * `device_info` (no permissions). B2+ will register screen_stream /
 * camera_stream / input here, replacing the loopback-socket helper
 * model on Android.
 */
object Ops {

    fun registerAll(ctx: Context, dispatcher: OpDispatcher) {
        dispatcher.register("device_info") { _ -> deviceInfo(ctx) }
    }

    /** Native equivalent of the Python agent's `op_device_info` — no
     *  Termux:API, no shells. Pure Android Build + BatteryManager. */
    private fun deviceInfo(ctx: Context): JSONObject {
        val out = JSONObject()
        out.put("model", Build.MODEL ?: "")
        out.put("manufacturer", Build.MANUFACTURER ?: "")
        out.put("device", Build.DEVICE ?: "")
        out.put("brand", Build.BRAND ?: "")
        out.put("android_release", Build.VERSION.RELEASE ?: "")
        out.put("android_sdk", Build.VERSION.SDK_INT)
        out.put("hardware", Build.HARDWARE ?: "")
        out.put("abis", JSONArray(Build.SUPPORTED_ABIS?.toList() ?: emptyList<String>()))

        // Battery (BatteryManager works without RECEIVER_NOT_EXPORTED).
        try {
            val bm = ctx.getSystemService(Context.BATTERY_SERVICE) as? BatteryManager
            if (bm != null) {
                val pct = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
                if (pct in 0..100) out.put("battery_percent", pct)
            }
            val stickyIntent = ctx.registerReceiver(
                null, IntentFilter(Intent.ACTION_BATTERY_CHANGED),
            )
            val status = stickyIntent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1) ?: -1
            val plugged = stickyIntent?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0
            out.put("battery_status", when (status) {
                BatteryManager.BATTERY_STATUS_CHARGING -> "charging"
                BatteryManager.BATTERY_STATUS_DISCHARGING -> "discharging"
                BatteryManager.BATTERY_STATUS_FULL -> "full"
                BatteryManager.BATTERY_STATUS_NOT_CHARGING -> "not_charging"
                else -> "unknown"
            })
            out.put("battery_plugged", plugged != 0)
        } catch (_: Exception) { /* best-effort */ }

        // Agent identity (matches the existing Python op_device_info shape
        // closely enough that the existing UI info modal renders cleanly).
        out.put("agent", "vortex-driver-apk")
        out.put("agent_version", BuildConfig.VERSION_NAME)
        return out
    }
}
