package com.vortex.driver

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Foreground service that, in M0, just announces itself with a persistent
 * notification and polls for the Termux Python agent on a local TCP port.
 *
 * In M1+ this is where Camera2 capture, MediaProjection screen capture,
 * and the AccessibilityService bridge for touch input will be wired up,
 * each behind its own foregroundServiceType when added.
 *
 * Why a foreground service: Android aggressively backgrounds and kills
 * normal services. Foreground + a sticky notification is the only way to
 * keep camera/screen pipelines alive when the user switches apps or the
 * screen turns off.
 */
class DriverService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var probeJob: Job? = null

    @Volatile private var lastConnected: Boolean = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
        // Start the foreground notification as soon as we're created --
        // Android will kill us within ~5s otherwise.
        startForeground(NOTIF_ID, buildNotification(connected = false))
        probeJob = scope.launch { probeAgentLoop() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // START_STICKY = restart us if Android kills us under memory pressure.
        return START_STICKY
    }

    override fun onDestroy() {
        probeJob?.cancel()
        scope.cancel()
        super.onDestroy()
    }

    /**
     * Try to connect to the Termux Python agent's loopback listener every
     * few seconds. The agent server doesn't exist yet (lands in M1) so for
     * now this just toggles the notification text between "Waiting for
     * agent" and (eventually) "Connected to agent". Cheap heartbeat that
     * proves the lifecycle works on real hardware.
     */
    private suspend fun probeAgentLoop() {
        while (scope.isActive) {
            val ok = withContext(Dispatchers.IO) { tryConnect() }
            if (ok != lastConnected) {
                lastConnected = ok
                postNotification(buildNotification(connected = ok))
            }
            delay(if (ok) HEARTBEAT_OK_MS else HEARTBEAT_FAIL_MS)
        }
    }

    private fun tryConnect(): Boolean {
        return try {
            Socket().use { s ->
                s.connect(InetSocketAddress(AGENT_HOST, AGENT_PORT), CONNECT_TIMEOUT_MS)
                true
            }
        } catch (_: Exception) {
            false
        }
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = getSystemService(NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.channel_desc)
            setShowBadge(false)
        }
        nm.createNotificationChannel(channel)
    }

    private fun buildNotification(connected: Boolean): Notification {
        val text = if (connected)
            getString(R.string.notif_connected)
        else
            getString(R.string.notif_waiting, AGENT_PORT)
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .build()
    }

    private fun postNotification(notif: Notification) {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        nm.notify(NOTIF_ID, notif)
    }

    companion object {
        const val CHANNEL_ID = "vortex_driver"
        // Arbitrary stable notification id. 0xC0FFEE so it's recognisable in
        // a logcat / `dumpsys notification` dump.
        const val NOTIF_ID = 0xC0FFEE

        // Loopback port the Termux Python agent will eventually listen on.
        // 5099 is unassigned by IANA and well above the privileged range.
        const val AGENT_HOST = "127.0.0.1"
        const val AGENT_PORT = 5099

        const val CONNECT_TIMEOUT_MS = 800
        const val HEARTBEAT_OK_MS = 10_000L
        const val HEARTBEAT_FAIL_MS = 3_000L
    }
}
