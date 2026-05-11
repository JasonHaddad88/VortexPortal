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
 * Foreground service that owns:
 *   - Camera StreamServer on 127.0.0.1:5099  (M1)
 *   - Screen  StreamServer on 127.0.0.1:5098  (M2)
 *
 * Each engine (CameraEngine / ScreenEngine) is started lazily when its
 * stream server gets a client, and stopped as soon as that client hangs
 * up. The screen engine additionally needs a "consent armed" state: the
 * user must have tapped through the system MediaProjection dialog before
 * any agent-side screen connect can succeed. That dialog is summoned by
 * [ScreenSetupActivity], which posts the resulting (resultCode, data)
 * pair back here via ACTION_ARM_SCREEN.
 *
 * Service-type bookkeeping: `dataSync|camera|mediaProjection` declared in
 * the manifest, but [startForegroundCompat] only enables the union of
 * what's actually being used so we don't lie to Android about active
 * service types (which would crash on API 35+).
 */
class DriverService : Service(), CameraEngine.FrameSink {

    // ---- Camera (M1) ----
    private lateinit var cameraServer: StreamServer
    private var camera: CameraEngine? = null
    @Volatile private var cameraClientConnected: Boolean = false

    // ---- Screen (M2) ----
    private lateinit var screenServer: StreamServer
    private var screen: ScreenEngine? = null
    @Volatile private var screenClientConnected: Boolean = false
    @Volatile private var screenArmed: Boolean = false
    private var pendingScreenResultCode: Int = 0
    private var pendingScreenResultData: Intent? = null

    @Volatile private var lastError: String? = null
    @Volatile private var frameCount: Long = 0L

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        ensureChannel()
        // Start as data_sync only (the most permissive baseline that doesn't
        // require yet-unobtained perms). We'll add camera / mediaProjection
        // as they come into play.
        startForegroundCompat(buildNotification(), includeMediaProjection = false, includeCamera = false)

        cameraServer = StreamServer(
            port = CAMERA_PORT,
            onClientConnected = ::onCameraClientConnected,
            onClientDisconnected = ::onCameraClientDisconnected,
        ).also { it.start() }

        screenServer = StreamServer(
            port = SCREEN_PORT,
            onClientConnected = ::onScreenClientConnected,
            onClientDisconnected = ::onScreenClientDisconnected,
        ).also { it.start() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_ARM_SCREEN -> {
                val rc = intent.getIntExtra(EXTRA_RESULT_CODE, 0)
                val data = intent.getParcelableExtra<Intent>(EXTRA_RESULT_DATA)
                if (rc != 0 && data != null) {
                    armScreen(rc, data)
                }
            }
            ACTION_DISARM_SCREEN -> disarmScreen()
        }
        return START_STICKY
    }

    override fun onDestroy() {
        try { camera?.stop() } catch (_: Exception) {}
        try { screen?.stop() } catch (_: Exception) {}
        try { cameraServer.stop() } catch (_: Exception) {}
        try { screenServer.stop() } catch (_: Exception) {}
        super.onDestroy()
    }

    // ---- Camera lifecycle ----

    private fun onCameraClientConnected() {
        cameraClientConnected = true
        lastError = null
        if (camera == null) {
            camera = CameraEngine(this, CameraCharacteristics.LENS_FACING_BACK)
        }
        // Bump our service-type to include camera now that we're actually
        // using it.
        promoteForeground()
        camera?.start(this)
        updateNotification()
    }

    private fun onCameraClientDisconnected() {
        cameraClientConnected = false
        try { camera?.stop() } catch (_: Exception) {}
        camera = null
        promoteForeground()  // drop camera type if nothing else needs it
        updateNotification()
    }

    // ---- Screen lifecycle ----

    /**
     * Stash consent and (re)bind the projection. Called from
     * ScreenSetupActivity after the user accepts the dialog.
     *
     * MediaProjection on Android 14+ refuses to start until the host
     * service is foreground with FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
     * declared, so we promote the service type FIRST.
     */
    private fun armScreen(resultCode: Int, resultData: Intent) {
        pendingScreenResultCode = resultCode
        pendingScreenResultData = resultData
        screenArmed = true
        promoteForeground()
        // If a client is already waiting (rare but possible), spin up the
        // engine now.
        if (screenClientConnected) startScreenEngine()
        updateNotification()
    }

    private fun disarmScreen() {
        screenArmed = false
        try { screen?.stop() } catch (_: Exception) {}
        screen = null
        pendingScreenResultData = null
        pendingScreenResultCode = 0
        promoteForeground()
        updateNotification()
    }

    private fun onScreenClientConnected() {
        screenClientConnected = true
        if (screenArmed) startScreenEngine() else updateNotification()
    }

    private fun onScreenClientDisconnected() {
        screenClientConnected = false
        try { screen?.stop() } catch (_: Exception) {}
        screen = null
        updateNotification()
    }

    private fun startScreenEngine() {
        val data = pendingScreenResultData ?: return
        if (screen != null) return
        val sink = object : CameraEngine.FrameSink {
            override fun onFrame(jpegBytes: ByteArray, width: Int, height: Int, sensorRotation: Int) {
                screenServer.pushFrame(jpegBytes)
                if (++frameCount % 30L == 0L) updateNotification()
            }
            override fun onError(message: String) {
                Log.w(TAG, "Screen error: $message")
                lastError = message
                try { screen?.stop() } catch (_: Exception) {}
                screen = null
                screenArmed = false   // force re-consent
                pendingScreenResultData = null
                promoteForeground()
                updateNotification()
            }
        }
        screen = ScreenEngine(this, pendingScreenResultCode, data).also { it.start(sink) }
        updateNotification()
    }

    // ---- CameraEngine.FrameSink (camera path) ----

    override fun onFrame(jpegBytes: ByteArray, width: Int, height: Int, sensorRotation: Int) {
        cameraServer.pushFrame(jpegBytes)
        if (++frameCount % 30L == 0L) updateNotification()
    }

    override fun onError(message: String) {
        Log.w(TAG, "Camera error: $message")
        lastError = message
        cameraClientConnected = false
        try { camera?.stop() } catch (_: Exception) {}
        camera = null
        promoteForeground()
        updateNotification()
    }

    // ---- Notification + foreground service plumbing ----

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val nm = getSystemService(NotificationManager::class.java) ?: return
        if (nm.getNotificationChannel(CHANNEL_ID) != null) return
        val ch = NotificationChannel(
            CHANNEL_ID, getString(R.string.channel_name),
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = getString(R.string.channel_desc)
            setShowBadge(false)
        }
        nm.createNotificationChannel(ch)
    }

    private fun promoteForeground() {
        startForegroundCompat(
            buildNotification(),
            includeCamera = cameraClientConnected || camera != null,
            includeMediaProjection = screenArmed,
        )
    }

    private fun startForegroundCompat(
        notif: Notification,
        includeCamera: Boolean,
        includeMediaProjection: Boolean,
    ) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            var type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            if (includeCamera)
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
            if (includeMediaProjection)
                type = type or ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION
            startForeground(NOTIF_ID, notif, type)
        } else {
            @Suppress("DEPRECATION")
            startForeground(NOTIF_ID, notif)
        }
    }

    private fun buildNotification(): Notification {
        val parts = mutableListOf<String>()
        if (lastError != null) {
            parts += getString(R.string.notif_error, lastError)
        }
        if (cameraClientConnected) {
            parts += getString(R.string.notif_streaming_camera, frameCount)
        }
        if (screenArmed) {
            parts += if (screenClientConnected)
                        getString(R.string.notif_streaming_screen)
                     else
                        getString(R.string.notif_screen_armed, SCREEN_PORT)
        }
        if (parts.isEmpty()) {
            parts += getString(R.string.notif_idle, CAMERA_PORT)
        }
        val text = parts.joinToString(" · ")
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
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
        const val CAMERA_PORT = 5099   // M1
        const val SCREEN_PORT = 5098   // M2
        // Kept for backwards source-compat with older M0/M1 references.
        const val AGENT_PORT = CAMERA_PORT

        const val ACTION_ARM_SCREEN    = "com.vortex.driver.ACTION_ARM_SCREEN"
        const val ACTION_DISARM_SCREEN = "com.vortex.driver.ACTION_DISARM_SCREEN"
        const val EXTRA_RESULT_CODE = "resultCode"
        const val EXTRA_RESULT_DATA = "resultData"

        private const val TAG = "DriverService"
    }
}
