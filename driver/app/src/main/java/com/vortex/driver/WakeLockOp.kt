package com.vortex.driver

import android.content.Context
import android.os.PowerManager
import org.json.JSONObject

/**
 * Native equivalent of the Python agent's `op_keepawake`. Holds a
 * `PARTIAL_WAKE_LOCK` so the CPU stays on -- the existing foreground
 * service already keeps the *process* alive; this just makes sure
 * we don't enter Doze between WS pings.
 *
 * Important caveat (matches the Python agent's response):
 *   - This CANNOT block the system lock screen or a hardware power-off.
 *     Doing so requires device-owner / MDM enrollment, which a regular
 *     app cannot opt into. The `best_effort: true` field in the
 *     response makes that explicit to the hub UI.
 */
object WakeLockOp {

    private val sync = Any()
    @Volatile private var lock: PowerManager.WakeLock? = null

    fun set(ctx: Context, on: Boolean): JSONObject = synchronized(sync) {
        val pm = ctx.getSystemService(Context.POWER_SERVICE) as? PowerManager
            ?: throw RuntimeException("POWER_SERVICE unavailable on this device")

        if (on) {
            if (lock?.isHeld != true) {
                val l = pm.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK,
                    "Vortex::TheftMode",
                )
                l.setReferenceCounted(false)
                try { l.acquire() } catch (e: Exception) {
                    throw RuntimeException("acquire wake lock failed: ${e.message}")
                }
                lock = l
            }
        } else {
            try { lock?.takeIf { it.isHeld }?.release() } catch (_: Exception) {}
            lock = null
        }
        JSONObject()
            .put("keepawake", on)
            .put("best_effort", true)
            .put("held", lock?.isHeld == true)
    }
}
