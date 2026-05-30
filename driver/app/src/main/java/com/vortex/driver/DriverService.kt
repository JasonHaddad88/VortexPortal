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
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

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

    // ---- Input (M3) ----
    private lateinit var inputServer: InputServer

    // ---- B1: outbound hub WS (standalone client role). Optional —
    // ----- only starts when Prefs.isEnrolled().
    private var hubClient: HubClient? = null
    @Volatile private var hubStatus: String = ""

    // ---- B3: inbound direct-WS server (browser ↔ APK direct). One
    // ----- instance per service lifetime; HubClient pushes its port +
    // ----- ticket + reachable hosts in direct_info on each auth_ok.
    private var directServerImpl: DirectServer? = null
    fun directServer(): DirectServer? = directServerImpl

    @Volatile private var lastError: String? = null
    @Volatile private var frameCount: Long = 0L

    // ---- B2.2: native stream ops ----
    // Independent of the loopback StreamServer pipeline -- these are owned
    // by HubClient stream coroutines (one per inbound rid) and pump frames
    // straight into a WsStreamSink. We keep separate engine instances so
    // a Termux-side loopback consumer and a hub-side stream consumer can
    // theoretically run in parallel on different cameras / sessions.
    private var nativeCamera: CameraEngine? = null
    private var nativeScreen: ScreenEngine? = null
    private var nativeScreenH264: ScreenH264Encoder? = null
    private var nativeCameraH264: CameraH264Encoder? = null
    // B11.10: optional system-audio capture that piggybacks on the
    // active MediaProjection consent. Started/stopped alongside the
    // H.264 screen encoder when the consumer asks for audio.
    private var nativeScreenAudio: ScreenAudioCapture? = null
    @Volatile private var nativeCameraSink: CameraEngine.FrameSink? = null
    @Volatile private var nativeScreenSink: CameraEngine.FrameSink? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
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

        // M3: input server is independent of camera + screen. Always on
        // while the service is up, so the agent can control the phone
        // without needing to also be streaming video.
        inputServer = InputServer(this, port = INPUT_PORT).also { it.start() }

        // B11.2: the DirectServer is what other devices dial to control
        // this one (peer-to-peer, no hub). Bring it up as soon as the
        // user is signed in to Turso. HubClient (outbound WS to a hub)
        // is gated on the legacy Prefs.isEnrolled() pair so it only
        // dials when a hub URL is present in the saved nodes list --
        // direct-Turso deploys never start it.
        if (Prefs.isSignedIn(this) || Prefs.isEnrolled(this)) {
            try {
                directServerImpl = DirectServer(this, requestedPort = 0).also { it.start() }
                Log.i(TAG, "DirectServer started")
            } catch (t: Throwable) {
                Log.w(TAG, "DirectServer failed to start: ${t.javaClass.simpleName}: ${t.message}")
                directServerImpl = null
            }
        }
        if (Prefs.isEnrolled(this) && Prefs.nodes(this).isNotEmpty()) {
            hubClient = HubClient(this) { s ->
                hubStatus = s
                updateNotification()
            }.also { it.start() }
        }
        // B11.2: publish this device's direct-WS endpoint to the Turso
        // `device_peers` table so other peers can find us. Periodic
        // refresh (60 s) inside startPeerPublisher().
        startPeerPublisher()
        // B11.15: pull + execute any pending commands the user
        // queued while this device was offline.
        startCommandPoller()
        // B11.16: refresh the discovered relay URL from Turso's
        // node_endpoints. Lets a Termux hub's rotating cloudflared
        // URL flow to the APK without the user re-pasting.
        startHubDiscoveryPoller()
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

    /**
     * B11.3.1: some OEM Android skins (MIUI, ColorOS, HyperOS) stop
     * the foreground service when the user swipes the app card out of
     * Recents -- even with `START_STICKY` + a sticky notification.
     * Re-assert our foreground state here so we survive the swipe;
     * the system will not kill an actively-foreground service.
     */
    override fun onTaskRemoved(rootIntent: Intent?) {
        super.onTaskRemoved(rootIntent)
        try {
            promoteForeground()
        } catch (t: Throwable) {
            Log.w(TAG, "onTaskRemoved re-foreground failed: $t")
        }
    }

    override fun onDestroy() {
        try { camera?.stop() } catch (_: Exception) {}
        try { screen?.stop() } catch (_: Exception) {}
        try { nativeCamera?.stop() } catch (_: Exception) {}
        try { nativeScreen?.stop() } catch (_: Exception) {}
        try { nativeScreenH264?.stop() } catch (_: Exception) {}
        try { nativeCameraH264?.stop() } catch (_: Exception) {}
        try { nativeScreenAudio?.stop() } catch (_: Exception) {}
        try { cameraServer.stop() } catch (_: Exception) {}
        try { screenServer.stop() } catch (_: Exception) {}
        try { inputServer.stop() } catch (_: Exception) {}
        try { hubClient?.stop() } catch (_: Exception) {}
        try { directServerImpl?.shutdown() } catch (_: Exception) {}
        try { peerPublisherJob?.cancel() } catch (_: Exception) {}
        try { commandPollerJob?.cancel() } catch (_: Exception) {}
        try { hubDiscoveryJob?.cancel() } catch (_: Exception) {}
        try { peerScope.cancel() } catch (_: Exception) {}
        // Best-effort retract our peer row so other devices see us
        // offline immediately instead of waiting for the stale TTL.
        retractPeerSync()
        directServerImpl = null
        nativeCamera = null
        nativeScreen = null
        nativeCameraSink = null
        nativeScreenSink = null
        if (instance === this) instance = null
        super.onDestroy()
    }

    // ---- B11.2: peer presence publisher (Turso device_peers) ----

    private val peerScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var peerPublisherJob: Job? = null
    private var commandPollerJob: Job? = null
    private var hubDiscoveryJob: Job? = null
    @Volatile private var currentTicket: String = ""

    /** B11.15: dispatcher used by the queued-command poller. Lives for
     *  the whole service lifetime; reuses the same op handlers the
     *  HubClient + DirectServer dispatchers register. */
    private val bgDispatcher: OpDispatcher by lazy {
        OpDispatcher().also { Ops.registerAll(this, it) }
    }

    /** Spin up a background coroutine that refreshes our peer-presence
     *  row every 30 s. Idempotent: a second call is a no-op.
     *
     *  B11.3.1: wait for `DirectServer.port()` to bind before the
     *  FIRST publish (Java-WebSocket's start() is async). Without
     *  this, the initial pass would run with port=0 -> publishPeerOnce
     *  bails -> the device is invisible for the first full 60 s.
     *  Also shortened the refresh cadence so a stale row expires
     *  quickly when the user backgrounds the app.
     */
    private fun startPeerPublisher() {
        if (peerPublisherJob != null) return
        if (!Prefs.isSignedIn(this)) return
        peerPublisherJob = peerScope.launch {
            val cli = tursoClientFrom(this@DriverService) ?: return@launch
            try { PeerRegistry.ensureSchema(cli) } catch (_: TursoError) { /* retry next pass */ }
            // Wait up to ~15 s for the kernel-assigned port to land.
            var attempts = 0
            while ((directServerImpl?.port() ?: 0) <= 0 && attempts < 30) {
                delay(500L); attempts++
            }
            // delay() is cancellable -- loop exits cleanly when the
            // surrounding job is cancelled by onDestroy.
            while (true) {
                publishPeerOnce()
                delay(30_000L)
            }
        }
    }

    /** B11.3.1: public so DevicesActivity's "Republish this device"
     *  kebab entry can trigger an out-of-cadence refresh -- useful
     *  when the user has just joined a new Wi-Fi and wants to be
     *  reachable immediately instead of waiting up to 30 s. */
    fun publishPeerNow() = publishPeerOnce()

    /** B11.15: drain any pending queued commands for THIS device and
     *  execute them through the background OpDispatcher.
     *
     *  Runs ONCE immediately (so a freshly-online device fires
     *  queued ops within seconds) then on a 30 s cadence, matching
     *  the peer publisher. Cheap: a single SELECT per tick when the
     *  queue is empty. */
    private fun startCommandPoller() {
        if (commandPollerJob != null) return
        if (!Prefs.isSignedIn(this)) return
        commandPollerJob = peerScope.launch {
            val cli = tursoClientFrom(this@DriverService) ?: return@launch
            try { CommandQueue.ensureSchema(cli) } catch (_: TursoError) { /* retry next pass */ }
            while (true) {
                drainCommandQueueOnce(cli)
                delay(30_000L)
            }
        }
    }

    /** B11.16: pull the freshest hub URL from Turso `node_endpoints`
     *  every 60 s + once immediately. Caches into Prefs so the
     *  fallback path in PeerControlActivity (and DeviceWebActivity)
     *  sees the latest value without re-querying. A user-set manual
     *  relay URL still wins -- this only fills the auto-discovered
     *  slot. */
    private fun startHubDiscoveryPoller() {
        if (hubDiscoveryJob != null) return
        if (!Prefs.isSignedIn(this)) return
        hubDiscoveryJob = peerScope.launch {
            val cli = tursoClientFrom(this@DriverService) ?: return@launch
            while (true) {
                val freshest = HubDiscovery.queryFreshest(cli)
                Prefs.saveRelayDiscovered(this@DriverService, freshest)
                delay(60_000L)
            }
        }
    }

    private fun drainCommandQueueOnce(cli: TursoClient) {
        val deviceId = Prefs.deviceId(this) ?: return
        val pending = CommandQueue.pendingFor(cli, deviceId)
        for (cmd in pending) {
            if (cmd.op !in CommandQueue.WHITELIST) {
                CommandQueue.markExecuted(
                    cli, cmd.id, error = "op not in queue whitelist: ${cmd.op}",
                )
                continue
            }
            try {
                val result = bgDispatcher.runUnary(cmd.op, cmd.args)
                CommandQueue.markExecuted(cli, cmd.id, result = result)
            } catch (e: Throwable) {
                CommandQueue.markExecuted(
                    cli, cmd.id,
                    error = "${e.javaClass.simpleName}: ${e.message ?: ""}",
                )
            }
        }
    }

    private fun publishPeerOnce() {
        val deviceId = Prefs.deviceId(this) ?: return
        val server = directServerImpl ?: return
        val port = server.port()
        if (port <= 0) return
        val ips = DeviceHosts.reachableIps()
        if (ips.isEmpty()) return
        val hosts = ips.map { "$it:$port" }
        val ticket = PeerRegistry.newTicket()
        currentTicket = ticket
        // Also register the ticket with the DirectServer so the WS
        // handshake accepts it. The server's existing armTicket()
        // path expects a one-shot consumption; we re-arm at every
        // refresh so the published ticket stays valid.
        try { server.armTicketValue(ticket) } catch (_: Throwable) { /* ignore */ }
        val cli = tursoClientFrom(this) ?: return
        try {
            PeerRegistry.publish(cli, deviceId, hosts, port, ticket)
        } catch (e: TursoError) {
            android.util.Log.w(TAG, "peer publish failed: ${e.message}")
        }
    }

    private fun retractPeerSync() {
        val deviceId = Prefs.deviceId(this) ?: return
        val cli = tursoClientFrom(this) ?: return
        // Fire-and-forget on a thread so service shutdown doesn't block
        // on the network call.
        Thread {
            try { PeerRegistry.retract(cli, deviceId) } catch (_: Throwable) {}
        }.apply { isDaemon = true; start() }
    }

    // ---- B2.2 public API for Ops.screen_stream / camera_stream ----

    /** Returns true if the user has accepted the MediaProjection dialog
     *  (via ScreenSetupActivity) so a screen stream can start. */
    fun isScreenArmed(): Boolean = screenArmed

    /**
     * Start a native screen stream pumping JPEG frames into [sink] until
     * [stopNativeScreenStream] is called or an engine error occurs.
     *
     * Throws IllegalStateException if the user hasn't armed MediaProjection
     * yet -- caller should turn that into the standard "open Vortex Driver
     * to enable screen sharing" error message.
     */
    @Synchronized
    fun startNativeScreenStream(
        sink: CameraEngine.FrameSink,
        maxDimension: Int = 720,
        jpegQuality: Int = 50,
        fpsCap: Int = 30,
        readyToEmit: () -> Boolean = { true },
    ) {
        val data = pendingScreenResultData
        if (!screenArmed || data == null) {
            throw IllegalStateException(
                "Vortex Driver screen sharing is not armed -- open the Driver " +
                "app on this device and tap 'Enable screen sharing' to accept " +
                "the system consent dialog."
            )
        }
        // Tear down any previous native session before opening a new one.
        try { nativeScreen?.stop() } catch (_: Exception) {}
        nativeScreen = null
        nativeScreenSink = sink
        // Make sure the foreground service type includes mediaProjection
        // before we ask getMediaProjection() for a session.
        promoteForeground()
        nativeScreen = ScreenEngine(
            context = this,
            resultCode = pendingScreenResultCode,
            resultData = data,
            maxDimension = maxDimension,
            jpegQuality = jpegQuality,
            fpsCap = fpsCap,
            readyToEmit = readyToEmit,
        ).also { it.start(sink) }
        updateNotification()
    }

    @Synchronized
    fun stopNativeScreenStream() {
        try { nativeScreen?.stop() } catch (_: Exception) {}
        nativeScreen = null
        nativeScreenSink = null
        promoteForeground()
        updateNotification()
    }

    /** B5: H.264 variant of [startNativeScreenStream]. Same consent
     *  contract (MediaProjection must be armed); same lifecycle.
     *  Different sink interface ([ScreenH264Encoder.NalSink]) so the op
     *  handler gets codec-config + per-NAL keyframe metadata it needs
     *  for the WebCodecs decoder on the browser side. */
    @Synchronized
    fun startNativeScreenStreamH264(
        sink: ScreenH264Encoder.NalSink,
        maxDimension: Int = 720,
        bitrateBps: Int = 1_500_000,
        fpsCap: Int = 30,
    ) {
        val data = pendingScreenResultData
        if (!screenArmed || data == null) {
            throw IllegalStateException(
                "Vortex Driver screen sharing is not armed -- open the Driver " +
                "app on this device and tap 'Enable screen sharing' to accept " +
                "the system consent dialog."
            )
        }
        try { nativeScreenH264?.stop() } catch (_: Exception) {}
        nativeScreenH264 = null
        promoteForeground()
        nativeScreenH264 = ScreenH264Encoder(
            context = this,
            resultCode = pendingScreenResultCode,
            resultData = data,
            maxDimension = maxDimension,
            bitrateBps = bitrateBps,
            fpsCap = fpsCap,
        ).also { it.start(sink) }
        updateNotification()
    }

    @Synchronized
    fun stopNativeScreenStreamH264() {
        try { nativeScreenH264?.stop() } catch (_: Exception) {}
        nativeScreenH264 = null
        promoteForeground()
        updateNotification()
    }

    /**
     * B11.10: companion to [startNativeScreenStreamH264]. Pumps system
     * playback audio (USAGE_MEDIA + USAGE_GAME + USAGE_UNKNOWN) through
     * an AAC encoder and into [sink]. Re-uses the same MediaProjection
     * consent the video encoder is using -- no extra prompt.
     *
     * Requires API 29+ (AudioPlaybackCaptureConfiguration). On older
     * devices the sink gets an immediate error frame.
     */
    @Synchronized
    fun startNativeScreenAudio(sink: ScreenAudioCapture.NalSink) {
        val data = pendingScreenResultData
        if (!screenArmed || data == null) {
            throw IllegalStateException(
                "Vortex Driver screen sharing is not armed -- the audio " +
                "capture piggybacks on the screen-share consent."
            )
        }
        try { nativeScreenAudio?.stop() } catch (_: Exception) {}
        nativeScreenAudio = null
        promoteForeground()
        nativeScreenAudio = ScreenAudioCapture(
            context = this,
            resultCode = pendingScreenResultCode,
            resultData = data,
        ).also { it.start(sink) }
        updateNotification()
    }

    @Synchronized
    fun stopNativeScreenAudio() {
        try { nativeScreenAudio?.stop() } catch (_: Exception) {}
        nativeScreenAudio = null
        updateNotification()
    }

    /** B5.1: H.264 variant of [startNativeCameraStream]. Same Camera2
     *  permission contract as the JPEG path; the encoder is fed via
     *  Camera2 TEMPLATE_RECORD targeting MediaCodec's input surface,
     *  not the per-frame JPEG callback path. */
    @Synchronized
    fun startNativeCameraStreamH264(
        sink: CameraH264Encoder.NalSink,
        facing: Int = CameraCharacteristics.LENS_FACING_BACK,
        maxDimension: Int = 720,
        bitrateBps: Int = 2_000_000,
        fpsCap: Int = 30,
    ) {
        try { nativeCameraH264?.stop() } catch (_: Exception) {}
        nativeCameraH264 = null
        promoteForeground()
        val target = if (maxDimension >= 1080) android.util.Size(1920, 1080)
                     else android.util.Size((maxDimension * 16 / 9), maxDimension)
        nativeCameraH264 = CameraH264Encoder(
            context = this,
            cameraFacing = facing,
            targetSize = target,
            bitrateBps = bitrateBps,
            fpsCap = fpsCap,
        ).also { it.start(sink) }
        updateNotification()
    }

    @Synchronized
    fun stopNativeCameraStreamH264() {
        try { nativeCameraH264?.stop() } catch (_: Exception) {}
        nativeCameraH264 = null
        promoteForeground()
        updateNotification()
    }

    @Synchronized
    fun startNativeCameraStream(
        sink: CameraEngine.FrameSink,
        facing: Int = CameraCharacteristics.LENS_FACING_BACK,
        maxDimension: Int = 720,
        jpegQuality: Int = 70,
        fpsCap: Int = 30,
        readyToEmit: () -> Boolean = { true },
    ) {
        try { nativeCamera?.stop() } catch (_: Exception) {}
        nativeCamera = null
        nativeCameraSink = sink
        promoteForeground()
        // Convert maxDimension -> a 16:9 Size with the long side honored.
        val target = if (maxDimension >= 1080) android.util.Size(1920, 1080)
                     else android.util.Size((maxDimension * 16 / 9), maxDimension)
        nativeCamera = CameraEngine(
            context = this,
            cameraFacing = facing,
            targetSize = target,
            jpegQuality = jpegQuality,
            fpsCap = fpsCap,
            readyToEmit = readyToEmit,
        ).also { it.start(sink) }
        updateNotification()
    }

    @Synchronized
    fun stopNativeCameraStream() {
        try { nativeCamera?.stop() } catch (_: Exception) {}
        nativeCamera = null
        nativeCameraSink = null
        promoteForeground()
        updateNotification()
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
            includeCamera = cameraClientConnected || camera != null ||
                            nativeCamera != null || nativeCameraH264 != null,
            includeMediaProjection = screenArmed || nativeScreen != null || nativeScreenH264 != null,
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
        // M3: surface accessibility state so the user can see at a glance
        // whether remote input will work without opening the app.
        if (VortexAccessibilityService.isEnabled) {
            parts += getString(R.string.notif_input_ready)
        } else {
            parts += getString(R.string.notif_input_disabled)
        }
        if (parts.size == 1 && parts[0] == getString(R.string.notif_input_disabled)) {
            // Only the "input disabled" badge is showing -- prepend the idle hint.
            parts.add(0, getString(R.string.notif_idle, CAMERA_PORT))
        }
        // B1: surface the standalone hub link state (when enrolled).
        if (hubClient != null && hubStatus.isNotBlank()) {
            parts += "Hub: $hubStatus"
        }
        // B3: surface the direct-WS server port (for diagnostics).
        directServerImpl?.takeIf { it.port() > 0 }?.let {
            parts += "Direct: :${it.port()}"
        }
        val text = parts.joinToString(" · ")
        // B10: tapping the notification routes through EntryActivity so
        // the user lands in the right place (Sign-in if not enrolled,
        // Devices dashboard if enrolled). The Device-settings panel
        // (MainActivity) is reachable from the dashboard kebab.
        val openIntent = Intent(this, EntryActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        }
        val pi = android.app.PendingIntent.getActivity(
            this, 0, openIntent,
            android.app.PendingIntent.FLAG_UPDATE_CURRENT or
                android.app.PendingIntent.FLAG_IMMUTABLE,
        )
        // Secondary action: jump straight to Device settings
        // (start/stop service, arm screen, accessibility).
        val settingsIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK
        }
        val settingsPi = android.app.PendingIntent.getActivity(
            this, 1, settingsIntent,
            android.app.PendingIntent.FLAG_UPDATE_CURRENT or
                android.app.PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setCategory(NotificationCompat.CATEGORY_SERVICE)
            .setContentIntent(pi)
            .addAction(0, getString(R.string.dashboard_settings), settingsPi)
            .build()
    }

    private fun updateNotification() {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        nm.notify(NOTIF_ID, buildNotification())
    }

    companion object {
        /** Live service instance, or null if the service isn't running.
         *  B2.2: native stream ops (Ops.screen_stream / camera_stream)
         *  reach the engines through this — same lifecycle as the
         *  foreground service itself. */
        @Volatile var instance: DriverService? = null
            private set

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

        const val INPUT_PORT  = 5097   // M3

        private const val TAG = "DriverService"
    }
}
