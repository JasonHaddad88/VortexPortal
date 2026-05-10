package com.vortex.driver

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.hardware.camera2.CameraCharacteristics
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat

/**
 * Foreground service that owns the [StreamServer] (loopback :5099) and the
 * [CameraEngine]. The camera only runs while a Termux agent client is
 * connected -- we save battery and avoid the on-device "camera in use"
 * indicator pulsing forever in the status bar when nobody is watching.
 *
 * Notification states:
 *   - "Idle. Waiting for Termux agent on 127.0.0.1:5099"   (no client)
 *   - "Streaming camera to Termux agent" + frame counter   (client connected)
 *   - "Camera error: ..."                                  (camera failed)
 *
 * Service type bumped from M0's "dataSync" to "dataSync|camera" so we can
 * legally hold the camera open inside a foreground service on Android 14+.
 * The matching FOREGROUND_SERVICE_CAMERA permission is requested in the
 * manifest.
 */
class DriverService : Service(), CameraEngine.FrameSink {

    private lateinit var server: StreamServer
    private var camera: CameraEngine? = null

    @Volatile private var clientConnected: Boolean = false
    @Volatile private var lastError: String? = null
    @Volatile private var frameCount: Long = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
        startForegroundCompat(buildNotification())

        server = StreamServer(
            port = AGENT_PORT,
            onClientConnected = ::handleClientConnected,
            onClientDisconnected = ::handleClientDisconnected,
        )
        server.start()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Restart us if Android kills us under memory pressure.
        return START_STICKY
    }

    override fun onDestroy() {
        try { camera?.stop() } catch (_: Exception) {}
        try { server.stop() } catch (_: Exception) {}
        super.onDestroy()
    }

    // --- Client lifecycle (called from StreamServer's accept thread) ---

    private fun handleClientConnected() {
        clientConnected = true
        lastError = null
        frameCount = 0L
        // Start the camera lazily on first connect.
        if (camera == null) {
            camera = CameraEngine(
                context = this,
                cameraFacing = CameraCharacteristics.LENS_FACING_BACK,
            )
        }
        camera?.start(this)
        updateNotification()
    }

    private fun handleClientDisconnected() {
        clientConnected = false
        // Release the camera so the privacy indicator goes away and the
        // camera HAL doesn't burn battery for nothing.
        try { camera?.stop() } catch (_: Exception) {}
        camera = null
        updateNotification()
    }

    // --- CameraEngine.FrameSink ---

    override fun onFrame(jpegBytes: ByteArray, width: Int, height: Int, sensorRotation: Int) {
        // Push to the connected client. If they hung up between frames,
        // pushFrame will fire onClientDisconnected via the stream server.
        server.pushFrame(jpegBytes)
        // Update the notification at most every ~30 frames so we don't
        // flood NotificationManager with cosmetic text changes.
        if (++frameCount % 30L == 0L) updateNotification()
    }

    override fun onError(message: String) {
        Log.w(TAG, "Camera error: $message")
        lastError = message
        clientConnected = false
        try { camera?.stop() } catch (_: Exception) {}
        camera = null
        updateNotification()
    }

    // --- Notification helpers ---

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = getSystemService(NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(CHANNEL_ID) != null) return
        val ch = NotificationChannel(
            CHANNEL_ID,
            getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.channel_desc)
            setShowBadge(false)
        }
        nm.createNotificationChannel(ch)
    }

    private fun startForegroundCompat(notif: Notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14 (API 34): must declare the type at startForeground time.
            // dataSync covers the loopback agent ping; camera covers the actual
            // CameraEngine session when a client connects.
            val type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC or
                       ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
            startForeground(NOTIF_ID, notif, type)
        } else {
            @Suppress("DEPRECATION")
            startForeground(NOTIF_ID, notif)
        }
    }

    private fun buildNotification(): Notification {
        val text = when {
            lastError != null -> getString(R.string.notif_error, lastError)
            clientConnected   -> getString(R.string.notif_streaming, frameCount)
            else              -> getString(R.string.notif_idle, AGENT_PORT)
        }
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

    private fun updateNotification() {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        nm.notify(NOTIF_ID, buildNotification())
    }

    companion object {
        const val CHANNEL_ID = "vortex_driver"
        const val NOTIF_ID = 0xC0FFEE
        const val AGENT_HOST = "127.0.0.1"
        const val AGENT_PORT = 5099
        private const val TAG = "DriverService"
    }
}
