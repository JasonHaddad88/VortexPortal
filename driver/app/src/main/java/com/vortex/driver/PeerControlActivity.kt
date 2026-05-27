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

    private enum class Tab { SCREEN, CAMERA, FILES, INFO }

    /** B11.5: file-browser nav stack. Empty = root; "/photos" = inside
     *  the photos folder; etc. We send to the peer as `args.path`. */
    private var filesPath: String = ""

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
        b.tabInfo.setOnClickListener    { switchTab(Tab.INFO) }
        b.filesUpBtn.setOnClickListener { filesGoUp() }
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
        styleTab(b.tabFiles,  t == Tab.FILES)
        styleTab(b.tabInfo,   t == Tab.INFO)
        // Visibility reset.
        b.frame.visibility = View.GONE
        b.frame.setImageDrawable(null)
        b.infoScroll.visibility = View.GONE
        b.filesPane.visibility = View.GONE
        b.cameraFlipBtn.visibility = View.GONE
        b.placeholder.visibility = if (peer.isOpen) View.GONE else View.VISIBLE
        b.errorText.visibility = View.GONE

        if (!peer.isOpen) return  // wait for connect

        when (t) {
            Tab.SCREEN -> startScreenStream()
            Tab.CAMERA -> { b.cameraFlipBtn.visibility = View.VISIBLE; startCameraStream() }
            Tab.FILES  -> { b.filesPane.visibility = View.VISIBLE; loadFiles(filesPath) }
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
