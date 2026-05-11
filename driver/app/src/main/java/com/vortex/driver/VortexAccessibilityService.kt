package com.vortex.driver

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.os.Build
import android.util.Log
import android.view.accessibility.AccessibilityEvent

/**
 * The ONLY non-root way for an Android app to inject touch input.
 *
 * Android requires:
 *   - This class extends [AccessibilityService] and is declared in the
 *     manifest with permission BIND_ACCESSIBILITY_SERVICE.
 *   - The user MANUALLY enables it from Settings -> Accessibility -> Vortex
 *     Driver -> "Use service". The OS forbids us from enabling it ourselves.
 *   - The accompanying meta-data XML at @xml/accessibility_service_config
 *     declares android:canPerformGestures="true". Without it, dispatchGesture
 *     silently no-ops.
 *
 * Once bound, Android holds an instance of this class in our process. We
 * stash it in [Companion.instance] so [InputServer] can call
 * [tap]/[swipe]/[globalAction] directly. When the user disables the
 * service (or revokes from Settings) [onUnbind] fires and the static
 * reference goes back to null -- callers then know to surface the
 * "service not enabled" error to the agent / hub / browser.
 *
 * onAccessibilityEvent is required by the base class but we don't use
 * accessibility events for anything; we're here purely for input
 * injection.
 */
class VortexAccessibilityService : AccessibilityService() {

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // intentionally empty -- we don't react to UI events
    }

    override fun onInterrupt() {
        // called when feedback should be interrupted -- no-op for us
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        Log.i(TAG, "VortexAccessibilityService connected")
        instance = this
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        Log.i(TAG, "VortexAccessibilityService unbound")
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }

    /** Single-point gesture. duration = how long the finger "rests" on screen. */
    fun tap(x: Float, y: Float, durationMs: Long = 50L): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs.coerceIn(1, 60_000))
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    fun longPress(x: Float, y: Float, durationMs: Long = 600L): Boolean = tap(x, y, durationMs)

    /** Linear swipe from (x1,y1) to (x2,y2) over durationMs. */
    fun swipe(x1: Float, y1: Float, x2: Float, y2: Float, durationMs: Long = 300L): Boolean {
        val path = Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs.coerceIn(1, 60_000))
        return dispatchGesture(GestureDescription.Builder().addStroke(stroke).build(), null, null)
    }

    /**
     * Hardware-button equivalents -- back, home, recents. These don't need
     * coordinates because they trigger system actions directly. Returns
     * false on devices that don't support a particular action (rare).
     */
    fun back(): Boolean = performGlobalAction(GLOBAL_ACTION_BACK)
    fun home(): Boolean = performGlobalAction(GLOBAL_ACTION_HOME)
    fun recents(): Boolean = performGlobalAction(GLOBAL_ACTION_RECENTS)

    /** Power dialog (lock-screen / shutdown menu). API 21+ */
    fun powerDialog(): Boolean = performGlobalAction(GLOBAL_ACTION_POWER_DIALOG)

    /** Pull down the notification shade. API 21+ */
    fun notifications(): Boolean = performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS)

    companion object {
        private const val TAG = "VortexA11yService"

        @Volatile private var instance: VortexAccessibilityService? = null

        /** True iff the user has enabled us in system Accessibility settings. */
        val isEnabled: Boolean get() = instance != null

        /** Snapshot of the current bound instance, or null if disabled. */
        fun current(): VortexAccessibilityService? = instance
    }
}
