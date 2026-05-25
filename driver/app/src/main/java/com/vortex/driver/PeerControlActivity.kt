package com.vortex.driver

import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vortex.driver.databinding.ActivityPeerControlBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import org.json.JSONObject

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

    private enum class Tab { SCREEN, CAMERA, INFO }

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
        b.tabInfo.setOnClickListener    { switchTab(Tab.INFO) }
        b.cameraFlipBtn.setOnClickListener {
            cameraFacing = if (cameraFacing == "back") "front" else "back"
            // Re-open the camera stream with the new facing.
            stopCurrentStream()
            startCameraStream()
        }

        // Connect first, then route the user to their initial tab.
        connectAndStart()
    }

    override fun onDestroy() {
        stopCurrentStream()
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
        styleTab(b.tabInfo,   t == Tab.INFO)
        // Visibility reset.
        b.frame.visibility = View.GONE
        b.frame.setImageDrawable(null)
        b.infoScroll.visibility = View.GONE
        b.cameraFlipBtn.visibility = View.GONE
        b.placeholder.visibility = if (peer.isOpen) View.GONE else View.VISIBLE
        b.errorText.visibility = View.GONE

        if (!peer.isOpen) return  // wait for connect

        when (t) {
            Tab.SCREEN -> startScreenStream()
            Tab.CAMERA -> { b.cameraFlipBtn.visibility = View.VISIBLE; startCameraStream() }
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
        b.frame.visibility = View.VISIBLE
        b.placeholder.visibility = View.VISIBLE
        b.placeholderText.setText(R.string.peer_loading_screen)
        currentStreamRid = peer.stream(
            "screen_stream",
            // Always MJPEG for now; H.264 needs MediaCodec on this side (B11.4).
            JSONObject().put("codec", "mjpeg").put("max_dim", 720).put("fps_cap", 15),
            mjpegHandlers("Screen"),
        )
        if (currentStreamRid == null) showError(getString(R.string.peer_stream_send_failed))
    }

    private fun startCameraStream() {
        b.frame.visibility = View.VISIBLE
        b.placeholder.visibility = View.VISIBLE
        b.placeholderText.setText(R.string.peer_loading_camera)
        currentStreamRid = peer.stream(
            "camera_stream",
            JSONObject().put("codec", "mjpeg").put("facing", cameraFacing)
                        .put("max_dim", 720).put("fps_cap", 15),
            mjpegHandlers("Camera"),
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
