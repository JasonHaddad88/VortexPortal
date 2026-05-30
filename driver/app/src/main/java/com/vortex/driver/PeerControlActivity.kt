package com.vortex.driver

import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.provider.MediaStore
import android.view.LayoutInflater
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vortex.driver.databinding.ActivityPeerControlBinding
import com.vortex.driver.databinding.RowFileBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.OutputStream

/**
 * B11.3: in-app per-device viewer. Dial a peer via [PeerClient],
 * show three tabs (Screen / Camera / Info).
 *
 *   Screen tab  -> screen_stream MJPEG -> ImageView. Falls back to a
 *                  "needs Driver-side arm" hint when the peer hasn't
 *                  accepted the MediaProjection consent dialog.
 *   Camera tab  -> camera_stream MJPEG. Facing flip button.
 *   Info tab    -> device_info JSON pretty-printed.
 *
 * H.264 path + Input passthrough land in B11.4 (need MediaCodec
 * decode + tap-to-coords math + a touch listener on the ImageView).
 *
 * Wire format is the same one the webapp already uses, so the peer
 * (running DirectServer + Ops handlers from B2.2 / B5 / B5.1) needs
 * zero changes to serve this client.
 */
class PeerControlActivity : AppCompatActivity() {

    private lateinit var b: ActivityPeerControlBinding
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private val peer = PeerClient(this)
    private var deviceId: String = ""
    private var deviceName: String = ""
    private var currentStreamRid: String? = null
    private var currentTab: Tab = Tab.SCREEN
    private var cameraFacing: String = "back"

    private enum class Tab { SCREEN, CAMERA, FILES, THEFT, INFO }

    /** B11.8: held by the wake-lock toggle so the button label can
     *  flip between "Hold" and "Release". */
    @Volatile private var wakeHeld: Boolean = false
    @Volatile private var lastLocationGeoUri: android.net.Uri? = null

    /** B11.5: file-browser nav stack. Empty = root; "/photos" = inside
     *  the photos folder; etc. We send to the peer as `args.path`. */
    private var filesPath: String = ""

    /** B11.6: peer's actual screen dimensions (in phone pixels), used
     *  to translate our ImageView touch coords back to coords that
     *  make sense to the peer's AccessibilityService. Filled on the
     *  first successful `screen_size` reply; null until then. */
    @Volatile private var peerScreenW: Int = 0
    @Volatile private var peerScreenH: Int = 0

    /** B11.7 / B11.9: native H.264 decoder + video frame dims. Both
     *  are null while we're in MJPEG mode (older peer, or H.264 setup
     *  failed and we fell back). Shared across the Screen and Camera
     *  tabs -- only one tab streams at a time. The touch-coord helper
     *  picks ImageView vs SurfaceView source dims accordingly. */
    @Volatile private var videoDecoder: H264Decoder? = null
    @Volatile private var videoW: Int = 0
    @Volatile private var videoH: Int = 0
    /** B11.9: front-camera frames come out rotated 90/270 relative to
     *  the natural display; the encoder emits a `rotation` hint in
     *  stream_start so the renderer can spin the SurfaceView. */
    @Volatile private var videoRotation: Int = 0
    /** B11.10: AAC decoder for the optional multiplexed audio track
     *  on the Screen tab. Null when (a) the peer didn't emit
     *  `stream_start.audio`, (b) we're on the Camera tab (no audio
     *  there yet), or (c) decoder setup failed and we silently
     *  carried on with video-only. */
    @Volatile private var screenAudio: AacDecoder? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityPeerControlBinding.inflate(layoutInflater)
        setContentView(b.root)
        deviceId = intent.getStringExtra(EXTRA_DEVICE_ID).orEmpty()
        deviceName = intent.getStringExtra(EXTRA_DEVICE_NAME).orEmpty()
        if (deviceId.isBlank()) { finish(); return }
        b.title.text = deviceName.ifBlank { "Device" }

        b.tabScreen.setOnClickListener  { switchTab(Tab.SCREEN) }
        b.tabCamera.setOnClickListener  { switchTab(Tab.CAMERA) }
        b.tabFiles.setOnClickListener   { switchTab(Tab.FILES) }
        b.tabTheft.setOnClickListener   { switchTab(Tab.THEFT) }
        b.tabInfo.setOnClickListener    { switchTab(Tab.INFO) }
        b.filesUpBtn.setOnClickListener { filesGoUp() }

        // B11.8: theft-mode card wiring.
        b.theftLocationBtn.setOnClickListener { theftGetLocation() }
        b.theftLocationMapBtn.setOnClickListener {
            lastLocationGeoUri?.let {
                try { startActivity(Intent(Intent.ACTION_VIEW, it)) }
                catch (_: Exception) {
                    Toast.makeText(this, "No maps app installed.", Toast.LENGTH_SHORT).show()
                }
            }
        }
        b.theftAudioBtn.setOnClickListener {
            val dur = when (b.theftAudioDuration.checkedRadioButtonId) {
                R.id.theft_audio_5s  -> 5
                R.id.theft_audio_30s -> 30
                else                  -> 15
            }
            theftRecordAudio(dur)
        }
        b.theftCameraBtn.setOnClickListener {
            val facing = if (b.theftCameraFacing.checkedRadioButtonId == R.id.theft_camera_front)
                            "1" else "0"
            theftCaptureCamera(facing)
        }
        b.theftWakeBtn.setOnClickListener { theftToggleWake() }

        // B11.6: input passthrough. Touch listener on the frame; the
        // peer's screen_size answer drives coord translation. System
        // keys map to the same `input` sub-commands the agent's
        // InputDispatch handles (back / home / recents).
        attachInputToFrame()
        b.keyBack.setOnClickListener    { sendInput(JSONObject().put("type", "back")) }
        b.keyHome.setOnClickListener    { sendInput(JSONObject().put("type", "home")) }
        b.keyRecents.setOnClickListener { sendInput(JSONObject().put("type", "recents")) }
        b.cameraFlipBtn.setOnClickListener {
            cameraFacing = if (cameraFacing == "back") "front" else "back"
            // Re-open the camera stream with the new facing. B11.9:
            // also tear down the active H.264 decoder + clear surface
            // transform so the next stream_start can bind a fresh
            // decoder + apply the right rotation.
            stopCurrentStream()
            videoDecoder?.stop(); videoDecoder = null
            videoW = 0; videoH = 0; videoRotation = 0
            b.frameVideo.rotation = 0f
            b.frameVideo.visibility = View.GONE
            b.frame.visibility = View.GONE
            b.frame.setImageDrawable(null)
            startCameraStream()
        }

        // Connect first, then route the user to their initial tab.
        connectAndStart()
    }

    override fun onDestroy() {
        stopCurrentStream()
        videoDecoder?.stop(); videoDecoder = null
        screenAudio?.stop(); screenAudio = null
        peer.close()
        scope.cancel()
        super.onDestroy()
    }

    // ---- connection ----------------------------------------------------

    private fun connectAndStart() {
        b.placeholder.visibility = View.VISIBLE
        b.placeholderText.setText(R.string.peer_connecting)
        b.connStatus.text = ""
        scope.launch {
            val res = runCatching { peer.connectTo(deviceId) }
            res.onSuccess {
                b.connStatus.text = getString(R.string.peer_connected)
                b.connStatus.setTextColor(
                    ContextCompat.getColor(this@PeerControlActivity, R.color.vortex_success)
                )
                // B11.6: pull the peer's actual screen dims so taps can
                // be translated. Best-effort; viewer still works
                // without it, taps will just use a reasonable default.
                fetchPeerScreenSize()
                switchTab(currentTab)
            }.onFailure { e ->
                // B11.4: direct LAN connection failed. If the user
                // configured a relay URL in Setup, fall back to the
                // embedded WebView pointed at relay/devices/{id}.
                // That path uses the B9 auth bridge (POST
                // /api/device-session) to land signed-in, and the
                // hub-side WebSocket relay handles NAT traversal so
                // we don't have to. If no relay is configured we
                // just show the original direct-connection error.
                val relay = Prefs.relayUrl(this@PeerControlActivity)
                if (relay != null) {
                    setStatus(getString(R.string.peer_falling_back_to_relay))
                    // Hand off to DeviceWebActivity; finish this
                    // activity so back lands on Devices, not a
                    // half-failed connect screen.
                    DeviceWebActivity.start(this@PeerControlActivity,
                                            relay, deviceId, deviceName)
                    finish()
                } else {
                    showError(getString(R.string.peer_connect_failed_no_relay,
                                        "${e.javaClass.simpleName}: ${e.message ?: ""}"))
                }
            }
        }
    }

    private fun setStatus(text: String) {
        b.placeholderText.text = text
    }

    // ---- tab routing ---------------------------------------------------

    private fun switchTab(t: Tab) {
        currentTab = t
        stopCurrentStream()
        // Pill styling.
        styleTab(b.tabScreen, t == Tab.SCREEN)
        styleTab(b.tabCamera, t == Tab.CAMERA)
        styleTab(b.tabFiles,  t == Tab.FILES)
        styleTab(b.tabTheft,  t == Tab.THEFT)
        styleTab(b.tabInfo,   t == Tab.INFO)
        // Visibility reset.
        b.frame.visibility = View.GONE
        b.frame.setImageDrawable(null)
        b.frameVideo.visibility = View.GONE
        b.infoScroll.visibility = View.GONE
        b.filesPane.visibility = View.GONE
        b.theftPane.visibility = View.GONE
        b.cameraFlipBtn.visibility = View.GONE
        b.sysKeys.visibility = View.GONE
        // B11.7 / B11.9: tear down any active H.264 decoder when
        // leaving a video tab so its MediaCodec releases the surface.
        videoDecoder?.stop(); videoDecoder = null
        videoW = 0; videoH = 0; videoRotation = 0
        // Reset any rotation transform applied by a previous camera
        // stream so the next stream starts from a clean surface.
        b.frameVideo.rotation = 0f
        // B11.10: stop any active audio playback when leaving the
        // screen tab. Don't carry an AAC decoder + AudioTrack into a
        // tab where they're meaningless.
        screenAudio?.stop(); screenAudio = null
        b.placeholder.visibility = if (peer.isOpen) View.GONE else View.VISIBLE
        b.errorText.visibility = View.GONE

        if (!peer.isOpen) return  // wait for connect

        when (t) {
            Tab.SCREEN -> { b.sysKeys.visibility = View.VISIBLE; startScreenStream() }
            Tab.CAMERA -> { b.cameraFlipBtn.visibility = View.VISIBLE; startCameraStream() }
            Tab.FILES  -> { b.filesPane.visibility = View.VISIBLE; loadFiles(filesPath) }
            Tab.THEFT  -> { b.theftPane.visibility = View.VISIBLE }
            Tab.INFO   -> { b.infoScroll.visibility = View.VISIBLE; loadInfo() }
        }
    }

    private fun styleTab(btn: android.widget.Button, active: Boolean) {
        btn.setBackgroundResource(
            if (active) R.drawable.vortex_pill_on else R.drawable.vortex_pill_off
        )
        btn.setTextColor(ContextCompat.getColor(
            this,
            if (active) R.color.vortex_text else R.color.vortex_text_subtle,
        ))
    }

    // ---- ops -----------------------------------------------------------

    private fun startScreenStream() {
        // B11.7: ask for h264 first; the handler factory below
        // routes by stream_start.content_type. If the peer is older
        // (or chose to honour mjpeg) we transparently fall back.
        // B11.10: opt in to multiplexed system audio. Older peers
        // ignore the flag (no audio track ships) so the same args
        // are safe everywhere.
        b.placeholder.visibility = View.VISIBLE
        b.placeholderText.setText(R.string.peer_loading_screen)
        currentStreamRid = peer.stream(
            "screen_stream",
            JSONObject()
                .put("codec", "h264")
                .put("audio", true)
                .put("max_dim", 720)
                .put("fps_cap", 30)
                .put("bitrate", 2_000_000),
            videoHandlers(
                label = "Screen",
                opName = "screen_stream",
                mjpegArgs = JSONObject()
                    .put("codec", "mjpeg")
                    .put("max_dim", 720)
                    .put("fps_cap", 15),
            ),
        )
        if (currentStreamRid == null) showError(getString(R.string.peer_stream_send_failed))
    }

    /** B11.7 (Screen) / B11.9 (Camera): hybrid handler that switches
     *  between H.264 SurfaceView decode and MJPEG ImageView blit based
     *  on what the peer actually sent in stream_start.content_type.
     *
     *  @param label   shown in conn_status while streaming
     *  @param opName  stream op name used for the MJPEG fall-back retry
     *  @param mjpegArgs args to send on the MJPEG retry when H.264 fails */
    private fun videoHandlers(
        label: String,
        opName: String,
        mjpegArgs: JSONObject,
    ) = object : PeerClient.StreamHandlers {
        @Volatile private var mode: String = "?"  // "h264" | "mjpeg"
        @Volatile private var firstFrame = true

        override fun onStart(meta: JSONObject) {
            runOnUiThread {
                val ct = meta.optString("content_type", "")
                if (ct == "video/h264") {
                    val csdB64 = meta.optString("csd_base64", "")
                    val csd = try {
                        android.util.Base64.decode(csdB64, android.util.Base64.NO_WRAP)
                    } catch (_: Throwable) { ByteArray(0) }
                    val w = meta.optInt("width", 0)
                    val h = meta.optInt("height", 0)
                    val rot = meta.optInt("rotation", 0)
                    val dec = H264Decoder()
                    b.frameVideo.visibility = View.VISIBLE
                    b.frame.visibility = View.GONE
                    // Apply rotation BEFORE sizing so aspect is right
                    // when the camera sensor is sideways relative to
                    // the natural orientation.
                    b.frameVideo.rotation = rot.toFloat()
                    sizeSurfaceAspect(w, h, rot)
                    val surface = b.frameVideo.holder.surface
                    val ok = surface != null && csd.isNotEmpty() &&
                             dec.configure(surface, csd, w, h)
                    if (ok) {
                        videoDecoder = dec
                        videoW = w; videoH = h; videoRotation = rot
                        mode = "h264"
                        // B11.10: bring up an AAC decoder if the peer
                        // multiplexed audio. Failure is non-fatal --
                        // we silently fall back to video-only.
                        val audioMeta = meta.optJSONObject("audio")
                        if (audioMeta != null) {
                            val aCsdB64 = audioMeta.optString("csd_base64", "")
                            val aCsd = try {
                                android.util.Base64.decode(aCsdB64, android.util.Base64.NO_WRAP)
                            } catch (_: Throwable) { ByteArray(0) }
                            val sr = audioMeta.optInt("sample_rate", 48_000)
                            val ch = audioMeta.optInt("channels", 2)
                            if (aCsd.isNotEmpty()) {
                                val ad = AacDecoder()
                                if (ad.configure(aCsd, sr, ch)) {
                                    screenAudio = ad
                                } else {
                                    ad.stop()
                                }
                            }
                        }
                        b.placeholder.visibility = View.GONE
                        val streamingLabel = if (screenAudio != null)
                            "$label (H.264 + audio)" else "$label (H.264)"
                        b.connStatus.text = getString(R.string.peer_streaming, streamingLabel)
                    } else {
                        // Surface not ready yet OR MediaCodec choked.
                        // Drop to MJPEG by stopping this stream and
                        // re-requesting without the h264 hint.
                        dec.stop()
                        b.frameVideo.visibility = View.GONE
                        b.frameVideo.rotation = 0f
                        b.frame.visibility = View.VISIBLE
                        currentStreamRid = peer.stream(
                            opName, mjpegArgs, mjpegHandlers(label),
                        )
                        mode = "mjpeg"
                    }
                } else {
                    // Peer fell back to MJPEG (older agent or codec
                    // unsupported). Use the ImageView path.
                    b.frame.visibility = View.VISIBLE
                    b.frameVideo.visibility = View.GONE
                    mode = "mjpeg"
                    b.placeholder.visibility = View.GONE
                    b.connStatus.text = getString(R.string.peer_streaming, label)
                }
            }
        }

        override fun onFrame(bytes: ByteArray, header: JSONObject?) {
            when (mode) {
                "h264" -> {
                    // B11.10: route on header.track. Default to "v"
                    // for back-compat with B11.7/B11.9 producers that
                    // never tagged their chunks.
                    val track = header?.optString("track", "v") ?: "v"
                    if (track == "a") {
                        val dec = screenAudio ?: return
                        val pts = header?.optLong("pts", 0L) ?: 0L
                        dec.feed(bytes, pts)
                        return
                    }
                    val dec = videoDecoder ?: return
                    val kf = header?.optBoolean("kf", false) ?: false
                    dec.feed(bytes, kf)
                    if (firstFrame) {
                        firstFrame = false
                        runOnUiThread { b.placeholder.visibility = View.GONE }
                    }
                }
                "mjpeg" -> {
                    val bmp = try { BitmapFactory.decodeByteArray(bytes, 0, bytes.size) }
                              catch (_: Throwable) { null } ?: return
                    runOnUiThread { b.frame.setImageBitmap(bmp) }
                }
                else -> { /* pre-handshake junk, ignore */ }
            }
        }

        override fun onEnd(error: JSONObject?) {
            currentStreamRid = null
            runOnUiThread {
                videoDecoder?.stop(); videoDecoder = null
                screenAudio?.stop(); screenAudio = null
                if (error != null) {
                    showError("$label stream ended: ${error.optString("error", "")}")
                }
            }
        }
    }

    /** Resize the SurfaceView so the H.264 frame fills the stage with
     *  the right aspect ratio. Without this the surface either
     *  stretches to fill (ugly) or shows letterboxing inside the
     *  surface (also ugly + breaks touch math).
     *
     *  B11.9: `rotation` (0/90/180/270) is the View rotation we'll
     *  apply via [View.setRotation]. When the rotation swaps the
     *  axes (90 or 270) we have to size the View using the SWAPPED
     *  natural dims so that, post-rotation, the visible bbox stays
     *  inside the parent. */
    private fun sizeSurfaceAspect(w: Int, h: Int, rotation: Int = 0) {
        if (w <= 0 || h <= 0) return
        val parentW = (b.frameVideo.parent as? View)?.width ?: return
        val parentH = (b.frameVideo.parent as? View)?.height ?: return
        if (parentW <= 0 || parentH <= 0) return
        val swap = rotation == 90 || rotation == 270
        // Post-rotation bbox is (h x w) when swapping. Compute the
        // scale that fits the bbox into the parent.
        val bboxW = if (swap) h else w
        val bboxH = if (swap) w else h
        val scale = kotlin.math.min(parentW / bboxW.toFloat(), parentH / bboxH.toFloat())
        // The View's own width/height are pre-rotation, so they use
        // the un-swapped dims.
        val targetW = (w * scale).toInt()
        val targetH = (h * scale).toInt()
        val lp = b.frameVideo.layoutParams
        lp.width = targetW
        lp.height = targetH
        b.frameVideo.layoutParams = lp
    }

    private fun startCameraStream() {
        // B11.9: same H.264-first negotiation the screen tab does.
        // The shared videoHandlers() factory flips to the SurfaceView
        // when the peer answers video/h264 and to the ImageView when
        // the peer answers image/jpeg. MJPEG retry fires only if our
        // local MediaCodec setup fails (rare).
        b.placeholder.visibility = View.VISIBLE
        b.placeholderText.setText(R.string.peer_loading_camera)
        currentStreamRid = peer.stream(
            "camera_stream",
            JSONObject()
                .put("codec", "h264")
                .put("facing", cameraFacing)
                .put("max_dim", 720)
                .put("fps_cap", 30)
                .put("bitrate", 2_000_000),
            videoHandlers(
                label = "Camera",
                opName = "camera_stream",
                mjpegArgs = JSONObject()
                    .put("codec", "mjpeg")
                    .put("facing", cameraFacing)
                    .put("max_dim", 720)
                    .put("fps_cap", 15),
            ),
        )
        if (currentStreamRid == null) showError(getString(R.string.peer_stream_send_failed))
    }

    /** Renders MJPEG frames to b.frame and surfaces stream-end errors. */
    private fun mjpegHandlers(label: String) = object : PeerClient.StreamHandlers {
        override fun onStart(meta: JSONObject) {
            runOnUiThread {
                b.placeholder.visibility = View.GONE
                b.connStatus.text = getString(R.string.peer_streaming, label)
            }
        }
        override fun onFrame(bytes: ByteArray, header: JSONObject?) {
            // BitmapFactory off-the-UI-thread; post the bitmap once
            // it's ready. The peer caps to 15 fps in args so this
            // doesn't flood the main looper.
            val bmp = try { BitmapFactory.decodeByteArray(bytes, 0, bytes.size) }
                      catch (_: Throwable) { null } ?: return
            runOnUiThread { b.frame.setImageBitmap(bmp) }
        }
        override fun onEnd(error: JSONObject?) {
            currentStreamRid = null
            runOnUiThread {
                if (error != null) {
                    showError("$label stream ended: " + (error.optString("error", "")))
                }
            }
        }
    }

    private fun loadInfo() {
        b.placeholder.visibility = View.VISIBLE
        b.placeholderText.setText(R.string.peer_loading_info)
        scope.launch {
            val res = runCatching { peer.unary("device_info") }
            res.onSuccess { info ->
                b.placeholder.visibility = View.GONE
                b.infoText.text = info.toString(2)
            }.onFailure { e ->
                showError("device_info failed: ${e.message ?: ""}")
            }
        }
    }

    private fun stopCurrentStream() {
        currentStreamRid?.let { peer.stopStream(it) }
        currentStreamRid = null
    }

    // ---- error display -------------------------------------------------

    private fun showError(msg: String) {
        b.errorText.text = msg
        b.errorText.visibility = View.VISIBLE
        b.placeholder.visibility = View.GONE
    }

    // ---- B11.8: theft-mode controls ----------------------------------

    /** Stream a JSON location fix from the peer (one chunk). */
    private fun theftGetLocation() {
        b.theftLocationStatus.text = getString(R.string.peer_theft_busy)
        b.theftLocationMapBtn.visibility = View.GONE
        lastLocationGeoUri = null
        val accumulator = StringBuilder()
        peer.stream(
            "location",
            JSONObject(),
            object : PeerClient.StreamHandlers {
                override fun onFrame(bytes: ByteArray, header: JSONObject?) {
                    accumulator.append(String(bytes, Charsets.UTF_8))
                }
                override fun onEnd(error: JSONObject?) {
                    runOnUiThread {
                        if (error != null) {
                            b.theftLocationStatus.text =
                                "Error: " + error.optString("error", "")
                            return@runOnUiThread
                        }
                        try {
                            val j = JSONObject(accumulator.toString())
                            val lat = j.optDouble("latitude")
                            val lon = j.optDouble("longitude")
                            val acc = j.optDouble("accuracy", 0.0)
                            val src = j.optString("provider", "?")
                            b.theftLocationStatus.text = getString(
                                R.string.peer_theft_location_result,
                                lat, lon, acc, src,
                            )
                            lastLocationGeoUri = android.net.Uri.parse(
                                "geo:%f,%f?q=%f,%f(Peer)".format(lat, lon, lat, lon)
                            )
                            b.theftLocationMapBtn.visibility = View.VISIBLE
                        } catch (e: Throwable) {
                            b.theftLocationStatus.text =
                                "Parse error: ${e.message ?: ""}"
                        }
                    }
                }
            },
        ) ?: run {
            b.theftLocationStatus.text = "Not connected."
        }
    }

    /** Record an audio clip on the peer and stream the file back to
     *  Downloads/Vortex via MediaStore. Same save pipeline the file
     *  browser uses (see downloadFile + accumulator pattern). */
    private fun theftRecordAudio(durationSec: Int) {
        b.theftAudioStatus.text = getString(R.string.peer_theft_audio_recording, durationSec)
        val ts = java.text.SimpleDateFormat("yyyyMMdd-HHmmss", java.util.Locale.US)
            .format(java.util.Date())
        val displayName = "vortex-audio-$ts.m4a"
        saveStreamToCollection(
            op = "record_audio",
            args = JSONObject().put("duration", durationSec),
            displayName = displayName,
            collection = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                MediaStore.Downloads.EXTERNAL_CONTENT_URI else null,
            relativePath = Environment.DIRECTORY_DOWNLOADS + "/Vortex",
            mimeType = "audio/mp4",
            onDone = { ok ->
                b.theftAudioStatus.text = if (ok)
                    getString(R.string.peer_theft_audio_saved, displayName)
                else "Recording failed."
            },
        )
    }

    /** Single-frame capture from the peer's back/front camera into
     *  Pictures/Vortex via MediaStore. */
    private fun theftCaptureCamera(cameraId: String) {
        b.theftCameraStatus.text = getString(R.string.peer_theft_busy)
        val ts = java.text.SimpleDateFormat("yyyyMMdd-HHmmss", java.util.Locale.US)
            .format(java.util.Date())
        val displayName = "vortex-photo-$ts.jpg"
        saveStreamToCollection(
            op = "camera_capture",
            args = JSONObject().put("camera_id", cameraId),
            displayName = displayName,
            collection = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q)
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI else null,
            relativePath = Environment.DIRECTORY_PICTURES + "/Vortex",
            mimeType = "image/jpeg",
            onDone = { ok ->
                b.theftCameraStatus.text = if (ok)
                    getString(R.string.peer_theft_camera_saved, displayName)
                else "Capture failed."
            },
        )
    }

    /** Toggle the peer's PARTIAL_WAKE_LOCK. Unary op; immediate result. */
    private fun theftToggleWake() {
        val on = !wakeHeld
        scope.launch {
            val res = runCatching {
                peer.unary("keepawake", JSONObject().put("on", on), timeoutMs = 3_000)
            }
            res.onSuccess { r ->
                wakeHeld = r.optBoolean("keepawake", on)
                b.theftWakeStatus.text = getString(
                    if (wakeHeld) R.string.peer_theft_wake_held
                    else R.string.peer_theft_wake_idle
                )
                b.theftWakeBtn.text = getString(
                    if (wakeHeld) R.string.peer_theft_wake_off
                    else R.string.peer_theft_wake_on
                )
            }.onFailure { e ->
                b.theftWakeStatus.text = "Error: ${e.message ?: ""}"
            }
        }
    }

    /**
     * Shared helper for theft ops that produce a file (audio + photo).
     * Streams the peer's `read_file_stream`-style output into the
     * given MediaStore collection (Downloads / Images.Media) or onto
     * disk on Android <= 9. Mirrors the file-browser download flow.
     */
    private fun saveStreamToCollection(
        op: String,
        args: JSONObject,
        displayName: String,
        collection: Uri?,
        relativePath: String,
        mimeType: String,
        onDone: (ok: Boolean) -> Unit,
    ) {
        var pendingUri: Uri? = null
        var output: OutputStream? = null
        val resolver = contentResolver
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && collection != null) {
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, displayName)
                    put(MediaStore.MediaColumns.MIME_TYPE, mimeType)
                    put(MediaStore.MediaColumns.IS_PENDING, 1)
                    put(MediaStore.MediaColumns.RELATIVE_PATH, relativePath)
                }
                pendingUri = resolver.insert(collection, values)
                output = pendingUri?.let { resolver.openOutputStream(it) }
            } else {
                @Suppress("DEPRECATION")
                val dir = Environment.getExternalStoragePublicDirectory(
                    if (mimeType.startsWith("image/")) Environment.DIRECTORY_PICTURES
                    else Environment.DIRECTORY_DOWNLOADS
                )
                val target = java.io.File(dir, displayName)
                output = target.outputStream()
            }
        } catch (e: Throwable) {
            onDone(false); return
        }
        if (output == null) { onDone(false); return }
        val rid = peer.stream(op, args, object : PeerClient.StreamHandlers {
            override fun onFrame(bytes: ByteArray, header: JSONObject?) {
                try { output.write(bytes) } catch (_: Throwable) {}
            }
            override fun onEnd(error: JSONObject?) {
                try { output.close() } catch (_: Throwable) {}
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && pendingUri != null) {
                    try {
                        resolver.update(pendingUri,
                            ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) },
                            null, null)
                    } catch (_: Throwable) {}
                }
                runOnUiThread { onDone(error == null) }
            }
        })
        if (rid == null) {
            try { output.close() } catch (_: Throwable) {}
            onDone(false)
        }
    }

    // ---- B11.6: input passthrough ------------------------------------

    /** Ask the peer for its current screen dimensions via the existing
     *  `input` op's `screen_size` sub-command. Best-effort -- on
     *  failure we leave `peerScreenW/H` at 0 and the touch handler
     *  falls back to the peer phone's reported `naturalWidth`. */
    private fun fetchPeerScreenSize() {
        scope.launch {
            val res = runCatching {
                peer.unary(
                    "input",
                    JSONObject().put("command", JSONObject().put("type", "screen_size")),
                    timeoutMs = 3_000,
                )
            }
            res.onSuccess { r ->
                peerScreenW = r.optInt("w", 0)
                peerScreenH = r.optInt("h", 0)
            }
            // Silent failure -- we don't want a transient screen_size
            // timeout to bubble up to the user; taps will just use
            // 1080x2400 defaults until a fresh fetch succeeds.
        }
    }

    /** Wire mouse-style + tap gestures on the Screen tab's visible
     *  view (ImageView for MJPEG, SurfaceView for H.264). Same
     *  listener attached to both; `toPeerCoords` picks the source
     *  dims from whichever is showing. */
    private fun attachInputToFrame() {
        var downCoords: IntArray? = null
        var downAtMs: Long = 0L
        val dragThreshPx = 8
        val touchListener = View.OnTouchListener { v, ev ->
            if (currentTab != Tab.SCREEN) return@OnTouchListener false
            when (ev.action) {
                android.view.MotionEvent.ACTION_DOWN -> {
                    downCoords = toPeerCoordsForView(v, ev.x, ev.y)
                    downAtMs = System.currentTimeMillis()
                    true
                }
                android.view.MotionEvent.ACTION_UP -> {
                    val down = downCoords ?: return@OnTouchListener true
                    val up = toPeerCoordsForView(v, ev.x, ev.y) ?: down
                    val dx = up[0] - down[0]; val dy = up[1] - down[1]
                    val dist = kotlin.math.hypot(dx.toDouble(), dy.toDouble())
                    val elapsed = (System.currentTimeMillis() - downAtMs).toInt().coerceAtLeast(50)
                    if (dist >= dragThreshPx) {
                        sendInput(JSONObject()
                            .put("type", "swipe")
                            .put("from", JSONArray().put(down[0]).put(down[1]))
                            .put("to",   JSONArray().put(up[0]).put(up[1]))
                            .put("duration_ms", elapsed.coerceIn(80, 800)))
                    } else if (elapsed >= 500) {
                        sendInput(JSONObject()
                            .put("type", "long_press")
                            .put("x", down[0]).put("y", down[1])
                            .put("duration_ms", 600))
                    } else {
                        sendInput(JSONObject()
                            .put("type", "tap")
                            .put("x", down[0]).put("y", down[1]))
                    }
                    downCoords = null
                    true
                }
                android.view.MotionEvent.ACTION_CANCEL -> { downCoords = null; true }
                else -> false
            }
        }
        b.frame.setOnTouchListener(touchListener)
        b.frameVideo.setOnTouchListener(touchListener)
    }

    /** Route to the right toPeerCoords overload by which view fired
     *  the touch event. ImageView reads drawable.intrinsic*; the
     *  SurfaceView uses the H.264 stream_start width/height. */
    private fun toPeerCoordsForView(v: View, viewX: Float, viewY: Float): IntArray? {
        return if (v === b.frameVideo) toPeerCoordsSurface(viewX, viewY)
               else                    toPeerCoords(viewX, viewY)
    }

    /** Counterpart for the SurfaceView (H.264 mode). The view is
     *  already sized to the video aspect by sizeSurfaceAspect, so
     *  there's no letterboxing inside the view itself -- coords
     *  scale linearly from view-pixels to peer-screen-pixels. */
    private fun toPeerCoordsSurface(viewX: Float, viewY: Float): IntArray? {
        val viewW = b.frameVideo.width.toFloat()
        val viewH = b.frameVideo.height.toFloat()
        if (viewW <= 0 || viewH <= 0) return null
        if (viewX < 0 || viewY < 0 || viewX > viewW || viewY > viewH) return null
        val xFrac = viewX / viewW; val yFrac = viewY / viewH
        val targetW = if (peerScreenW > 0) peerScreenW else (videoW.takeIf { it > 0 } ?: 1080)
        val targetH = if (peerScreenH > 0) peerScreenH else (videoH.takeIf { it > 0 } ?: 2400)
        return intArrayOf((xFrac * targetW).toInt(), (yFrac * targetH).toInt())
    }

    /** Translate (viewX, viewY) on the ImageView into peer-pixel
     *  coords. Honors `fitCenter` letterboxing -- a tap on the black
     *  bars returns null and is suppressed. */
    private fun toPeerCoords(viewX: Float, viewY: Float): IntArray? {
        val drawable = b.frame.drawable ?: return null
        val viewW = b.frame.width.toFloat()
        val viewH = b.frame.height.toFloat()
        if (viewW <= 0 || viewH <= 0) return null
        val intrW = drawable.intrinsicWidth.toFloat()
        val intrH = drawable.intrinsicHeight.toFloat()
        if (intrW <= 0 || intrH <= 0) return null
        // fitCenter: scale uniformly to fit, centered.
        val scale = kotlin.math.min(viewW / intrW, viewH / intrH)
        val drawnW = intrW * scale; val drawnH = intrH * scale
        val offX = (viewW - drawnW) / 2f; val offY = (viewH - drawnH) / 2f
        val xInImg = viewX - offX; val yInImg = viewY - offY
        if (xInImg < 0 || yInImg < 0 || xInImg > drawnW || yInImg > drawnH) return null
        val xFrac = xInImg / drawnW; val yFrac = yInImg / drawnH
        // Prefer the peer's reported screen size; fall back to the
        // current bitmap's intrinsic dims (close enough for most
        // phones, off-by-status-bar at worst).
        val targetW = if (peerScreenW > 0) peerScreenW else intrW.toInt()
        val targetH = if (peerScreenH > 0) peerScreenH else intrH.toInt()
        return intArrayOf((xFrac * targetW).toInt(), (yFrac * targetH).toInt())
    }

    /** Send one `input` op to the peer. Errors get surfaced as a tiny
     *  toast so the user knows the gesture didn't land (typically
     *  because the peer hasn't enabled the Vortex AccessibilityService). */
    private fun sendInput(command: JSONObject) {
        scope.launch {
            val res = runCatching {
                peer.unary(
                    "input",
                    JSONObject().put("command", command),
                    timeoutMs = 4_000,
                )
            }
            res.onFailure { e ->
                val msg = e.message ?: ""
                val hint = if (msg.contains("Accessibility", ignoreCase = true) ||
                               msg.contains("a11y", ignoreCase = true))
                    getString(R.string.peer_input_needs_a11y)
                else
                    msg.take(120)
                Toast.makeText(this@PeerControlActivity, hint, Toast.LENGTH_SHORT).show()
            }
        }
    }

    // ---- B11.5: file browser -----------------------------------------

    private fun loadFiles(path: String) {
        filesPath = path
        b.filesPath.text = if (path.isBlank()) "/" else path
        b.filesList.removeAllViews()
        scope.launch {
            val res = runCatching {
                peer.unary("list_dir", JSONObject().put("path", path), timeoutMs = 10_000)
            }
            res.onSuccess { result -> renderFiles(result.optJSONArray("entries") ?: JSONArray()) }
                .onFailure { e ->
                    showError("list_dir failed: ${e.message ?: ""}")
                }
        }
    }

    private fun renderFiles(entries: JSONArray) {
        b.filesList.removeAllViews()
        if (entries.length() == 0) {
            val empty = TextViewCompat().apply { text = getString(R.string.peer_files_empty) }
            b.filesList.addView(empty)
            return
        }
        val inflater = LayoutInflater.from(this)
        for (i in 0 until entries.length()) {
            val e = entries.optJSONObject(i) ?: continue
            val name = e.optString("name")
            val isDir = e.optBoolean("is_dir", false)
            val size = if (e.has("size")) e.optLong("size") else -1L
            val row = RowFileBinding.inflate(inflater, b.filesList, false)
            row.fileIcon.text = if (isDir) "▸" else "·"
            row.fileIcon.setTextColor(ContextCompat.getColor(this,
                if (isDir) R.color.vortex_purple else R.color.vortex_cyan))
            row.fileName.text = name
            row.fileMeta.text = when {
                isDir -> getString(R.string.peer_files_dir)
                size >= 0 -> humanSize(size)
                else -> ""
            }
            row.root.setOnClickListener {
                if (isDir) loadFiles(joinPath(filesPath, name))
                else downloadFile(joinPath(filesPath, name), name)
            }
            b.filesList.addView(row.root)
        }
    }

    private fun filesGoUp() {
        if (filesPath.isBlank() || filesPath == "/") return
        val parent = filesPath.trimEnd('/').substringBeforeLast('/', "")
        loadFiles(parent)
    }

    private fun joinPath(parent: String, child: String): String {
        val left = parent.trimEnd('/')
        return if (left.isEmpty()) child else "$left/$child"
    }

    private fun humanSize(bytes: Long): String {
        if (bytes < 1024) return "$bytes B"
        val kb = bytes / 1024.0
        if (kb < 1024) return "%.1f KB".format(kb)
        val mb = kb / 1024.0
        if (mb < 1024) return "%.1f MB".format(mb)
        return "%.2f GB".format(mb / 1024.0)
    }

    /**
     * Stream a peer file into the user's Downloads via MediaStore.
     * On Android 10+ that's a publicly-visible Downloads entry the
     * system Files app shows; on older Android we fall back to
     * Environment.getExternalStoragePublicDirectory(DIRECTORY_DOWNLOADS).
     */
    private fun downloadFile(remotePath: String, displayName: String) {
        Toast.makeText(this, getString(R.string.peer_files_downloading, displayName), Toast.LENGTH_SHORT).show()
        var bytesWritten = 0L
        var output: OutputStream? = null
        var pendingUri: Uri? = null
        val resolver = contentResolver

        // Open the destination first so we can stream into it as
        // chunks arrive.
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val values = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, displayName)
                    put(MediaStore.Downloads.IS_PENDING, 1)
                    put(MediaStore.Downloads.RELATIVE_PATH,
                        Environment.DIRECTORY_DOWNLOADS + "/Vortex")
                }
                pendingUri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                output = pendingUri?.let { resolver.openOutputStream(it) }
            } else {
                @Suppress("DEPRECATION")
                val dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS)
                val target = java.io.File(dir, displayName)
                output = target.outputStream()
            }
        } catch (e: Throwable) {
            showError("download setup failed: ${e.message ?: ""}"); return
        }
        if (output == null) { showError("Couldn't open Downloads for writing."); return }

        val rid = peer.stream(
            "read_file_stream",
            JSONObject().put("path", remotePath),
            object : PeerClient.StreamHandlers {
                override fun onStart(meta: JSONObject) {}
                override fun onFrame(bytes: ByteArray, header: JSONObject?) {
                    try { output.write(bytes); bytesWritten += bytes.size }
                    catch (e: Throwable) { /* ignore, end will surface */ }
                }
                override fun onEnd(error: JSONObject?) {
                    try { output.close() } catch (_: Throwable) {}
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && pendingUri != null) {
                        try {
                            resolver.update(pendingUri,
                                ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) },
                                null, null)
                        } catch (_: Throwable) {}
                    }
                    runOnUiThread {
                        if (error != null) {
                            showError("Download failed: ${error.optString("error", "")}")
                        } else {
                            Toast.makeText(this@PeerControlActivity,
                                getString(R.string.peer_files_downloaded, displayName, humanSize(bytesWritten)),
                                Toast.LENGTH_LONG).show()
                        }
                    }
                }
            },
        )
        if (rid == null) {
            try { output.close() } catch (_: Throwable) {}
            showError("Couldn't start file download — connection lost.")
        }
    }

    /** Tiny TextView factory for the empty-folder placeholder so we
     *  don't need a separate row layout for it. */
    private inner class TextViewCompat : androidx.appcompat.widget.AppCompatTextView(this@PeerControlActivity) {
        init {
            setPadding(24, 24, 24, 24)
            setTextColor(ContextCompat.getColor(context, R.color.vortex_text_muted))
            textSize = 13f
        }
    }

    companion object {
        const val EXTRA_DEVICE_ID   = "device_id"
        const val EXTRA_DEVICE_NAME = "device_name"

        fun start(ctx: Context, deviceId: String, deviceName: String) {
            ctx.startActivity(Intent(ctx, PeerControlActivity::class.java).apply {
                putExtra(EXTRA_DEVICE_ID, deviceId)
                putExtra(EXTRA_DEVICE_NAME, deviceName)
            })
        }
    }
}
