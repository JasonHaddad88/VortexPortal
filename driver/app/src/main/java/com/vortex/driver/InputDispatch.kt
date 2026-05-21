package com.vortex.driver

import android.content.Context
import android.provider.Settings
import android.util.DisplayMetrics
import android.view.WindowManager
import org.json.JSONObject

/**
 * Shared input-command dispatch. Used by:
 *   - [InputServer] (the legacy loopback-socket helper for the Termux
 *     Python agent — wraps this in its frame protocol).
 *   - [Ops] `op_input` (the native B2 path — the APK speaks straight
 *     to the hub, no Termux involved).
 *
 * Returns the result JSON the hub expects (or null for a bare "acked"
 * response). Throws [RuntimeException] with a human-readable message
 * on failure so the caller can surface it as `ok:false, error:…`.
 */
object InputDispatch {

    @Throws(RuntimeException::class)
    fun dispatchInput(ctx: Context, cmd: JSONObject): JSONObject? {
        val type = cmd.optString("type")
        return when (type) {
            "screen_size" -> realScreenSize(ctx).let { (w, h) ->
                JSONObject().put("w", w).put("h", h)
            }
            "a11y_state" -> JSONObject()
                .put("enabled", VortexAccessibilityService.isEnabled)
            "tap", "long_press" -> {
                val svc = requireA11y()
                val x = cmd.optDouble("x", -1.0).toFloat()
                val y = cmd.optDouble("y", -1.0).toFloat()
                if (x < 0 || y < 0) throw RuntimeException("Missing/invalid x or y")
                val duration = cmd.optLong(
                    "duration_ms",
                    if (type == "long_press") 600L else 50L,
                )
                val ok = if (type == "long_press") svc.longPress(x, y, duration)
                         else svc.tap(x, y, duration)
                if (!ok) throw RuntimeException("dispatchGesture returned false")
                null
            }
            "swipe" -> {
                val svc = requireA11y()
                val from = cmd.optJSONArray("from")
                val to = cmd.optJSONArray("to")
                if (from == null || from.length() < 2 || to == null || to.length() < 2) {
                    throw RuntimeException("Missing/invalid 'from' or 'to' (need 2-element arrays)")
                }
                val ok = svc.swipe(
                    from.getDouble(0).toFloat(), from.getDouble(1).toFloat(),
                    to.getDouble(0).toFloat(), to.getDouble(1).toFloat(),
                    cmd.optLong("duration_ms", 300L),
                )
                if (!ok) throw RuntimeException("dispatchGesture returned false")
                null
            }
            "back" -> { if (!requireA11y().back()) throw RuntimeException("back failed"); null }
            "home" -> { if (!requireA11y().home()) throw RuntimeException("home failed"); null }
            "recents" -> { if (!requireA11y().recents()) throw RuntimeException("recents failed"); null }
            "notifications" -> { if (!requireA11y().notifications()) throw RuntimeException("notifications failed"); null }
            else -> throw RuntimeException("Unknown input command type: $type")
        }
    }

    private fun requireA11y(): VortexAccessibilityService =
        VortexAccessibilityService.current()
            ?: throw RuntimeException(
                "Vortex Driver's Accessibility Service is not enabled. " +
                "On the phone: Settings → Accessibility → Vortex Driver → " +
                "toggle 'Use service' on. Android won't let us enable it for you."
            )

    private fun realScreenSize(ctx: Context): Pair<Int, Int> {
        val wm = ctx.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        return if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            val b = wm.currentWindowMetrics.bounds
            b.width() to b.height()
        } else {
            @Suppress("DEPRECATION")
            val display = wm.defaultDisplay
            val m = DisplayMetrics()
            @Suppress("DEPRECATION")
            display.getRealMetrics(m)
            m.widthPixels to m.heightPixels
        }
    }
}
