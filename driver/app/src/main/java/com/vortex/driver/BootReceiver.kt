package com.vortex.driver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log
import androidx.core.content.ContextCompat

/**
 * Driver-M4: auto-start the foreground [DriverService] after a reboot
 * so the user doesn't have to re-open the app every time the phone
 * power-cycles.
 *
 * Triggered by:
 *   - `ACTION_BOOT_COMPLETED` -- the normal "user-unlocked, system up"
 *     broadcast every Android version sends.
 *   - `ACTION_LOCKED_BOOT_COMPLETED` (Android 7+) -- the earlier
 *     direct-boot signal that fires while the device is still locked.
 *     Since we don't currently use direct-boot storage, we treat both
 *     identically and let the service handle the rest.
 *   - `ACTION_MY_PACKAGE_REPLACED` -- after an APK update, restart so
 *     the new code is the one running (Android otherwise just kills
 *     the old process and waits for the next intent).
 *
 * Idempotent: a second start while the service is already up is a
 * no-op (Android coalesces foreground-service starts). We only start
 * if [Prefs.isEnrolled] -- a fresh install with no creds has nothing
 * to dial, and starting the service would just show an unhelpful
 * "not enrolled" notification.
 *
 * Manufacturer caveat: many OEM Android skins (Xiaomi MIUI, Huawei
 * EMUI, OnePlus OxygenOS, ColorOS, ...) ignore BOOT_COMPLETED for
 * apps that aren't on their "autostart allow-list". The user has to
 * grant that from the OEM's app-info screen. We document this in
 * driver/README.md; the receiver itself does the right thing on AOSP
 * and any vendor that respects the broadcast.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        when (action) {
            Intent.ACTION_BOOT_COMPLETED,
            Intent.ACTION_LOCKED_BOOT_COMPLETED,
            Intent.ACTION_MY_PACKAGE_REPLACED -> { /* handled below */ }
            else -> return
        }
        if (!Prefs.isEnrolled(context)) {
            Log.i(TAG, "boot signal '$action' -- not enrolled, skipping autostart")
            return
        }
        try {
            val i = Intent(context, DriverService::class.java)
            ContextCompat.startForegroundService(context, i)
            Log.i(TAG, "DriverService autostart requested ($action)")
        } catch (e: Throwable) {
            // Some OEMs return a SecurityException for background
            // FGS-starts even from BOOT_COMPLETED -- nothing we can do
            // here except log and trust the user will open the app.
            Log.w(TAG, "autostart failed: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    companion object { private const val TAG = "BootReceiver" }
}
