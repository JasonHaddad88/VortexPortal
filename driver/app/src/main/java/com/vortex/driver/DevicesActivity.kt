package com.vortex.driver

import android.content.Intent
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import com.vortex.driver.databinding.ActivityDevicesBinding
import com.vortex.driver.databinding.RowDeviceBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * B8: in-app device list. Shows every device in the signed-in user's
 * account with online/offline status, last-seen, and a "THIS DEVICE"
 * badge for the row that's running this APK.
 *
 * Auth: the device's own enrollment proves account membership. We hit
 * `GET /api/account/devices` with `X-Vortex-Device` + `X-Vortex-Token`,
 * the same header pair `/api/nodes` uses. No persistent hub session
 * cookie needed.
 *
 * Tapping a row opens `{bootstrapUrl}/devices/{id}` in the system
 * browser -- in-APK control of OTHER devices is a future milestone
 * (would need a full hub UI client embedded; out of scope here).
 *
 * Refresh: pulls automatically on activity show + manual refresh
 * button. Last-seen is formatted as a friendly relative duration.
 */
class DevicesActivity : AppCompatActivity() {

    private lateinit var b: ActivityDevicesBinding
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .callTimeout(15, TimeUnit.SECONDS)
            .build()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityDevicesBinding.inflate(layoutInflater)
        setContentView(b.root)
        b.refreshBtn.setOnClickListener { refresh() }
    }

    override fun onResume() {
        super.onResume()
        refresh()
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun refresh() {
        val deviceId = Prefs.deviceId(this)
        val token = Prefs.deviceToken(this)
        val hubUrl = pickHubUrl()
        if (deviceId.isNullOrBlank() || token.isNullOrBlank() || hubUrl.isNullOrBlank()) {
            setStatus("Not enrolled yet -- sign in first.", err = true)
            renderList(emptyList())
            return
        }
        setBusy(true)
        setStatus(getString(R.string.devices_loading), err = false)
        scope.launch {
            val result = runCatching { fetchList(hubUrl, deviceId, token) }
            setBusy(false)
            result.onSuccess { items ->
                renderList(items)
                if (items.isEmpty()) {
                    setStatus("", err = false)
                    b.emptyText.visibility = View.VISIBLE
                } else {
                    b.emptyText.visibility = View.GONE
                    b.statusText.visibility = View.GONE
                }
            }.onFailure { e ->
                setStatus(getString(R.string.devices_load_error,
                                    e.javaClass.simpleName + ": " + (e.message ?: "")),
                          err = true)
            }
        }
    }

    /** Pick the URL to hit. Bootstrap was saved on enrollment; node list
     *  is the failover. If both empty, refuse. */
    private fun pickHubUrl(): String? {
        val boot = Prefs.bootstrapUrl(this)?.takeIf { it.isNotBlank() && it.startsWith("http") }
        if (boot != null) return boot.trimEnd('/')
        return Prefs.nodes(this).firstOrNull { it.startsWith("http") }?.trimEnd('/')
    }

    private suspend fun fetchList(
        hubUrl: String, deviceId: String, token: String,
    ): List<DeviceItem> = withContext(Dispatchers.IO) {
        val req = Request.Builder()
            .url("$hubUrl/api/account/devices")
            .header("X-Vortex-Device", deviceId)
            .header("X-Vortex-Token", token)
            .build()
        http.newCall(req).execute().use { rsp ->
            val text = rsp.body?.string().orEmpty()
            if (rsp.code == 404) throw RuntimeException(
                "This hub doesn't expose /api/account/devices (added in V5.26)."
            )
            if (!rsp.isSuccessful) {
                val detail = try { JSONObject(text).optString("detail", text) }
                             catch (_: Exception) { text }
                throw RuntimeException("HTTP ${rsp.code}: ${detail.take(200)}")
            }
            val arr = JSONObject(text).optJSONArray("devices") ?: return@use emptyList()
            (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                DeviceItem(
                    id = o.optString("id"),
                    name = o.optString("name", "Unnamed"),
                    online = o.optBoolean("online", false),
                    elsewhere = o.optString("elsewhere", "").takeIf { it.isNotBlank() },
                    lastSeen = if (o.isNull("last_seen")) null else o.optLong("last_seen"),
                    thisDevice = o.optBoolean("this_device", false),
                )
            }
        }
    }

    private fun renderList(items: List<DeviceItem>) {
        b.deviceList.removeAllViews()
        val hubUrl = pickHubUrl() ?: return
        // Sort: this-device first, then online, then elsewhere, then offline.
        val sorted = items.sortedWith(compareByDescending<DeviceItem> { it.thisDevice }
            .thenByDescending { it.online }
            .thenByDescending { it.elsewhere != null })
        val inflater = LayoutInflater.from(this)
        for (item in sorted) {
            val row = RowDeviceBinding.inflate(inflater, b.deviceList, false)
            row.deviceName.text = item.name
            row.deviceMeta.text = describeMeta(item)
            row.statusDot.background = dotFor(item)
            row.thisBadge.visibility = if (item.thisDevice) View.VISIBLE else View.GONE
            row.thisBadge.setTextColor(0xFF67E8F9.toInt())
            row.root.setOnClickListener {
                try {
                    startActivity(Intent(Intent.ACTION_VIEW,
                        Uri.parse("$hubUrl/devices/${item.id}")))
                } catch (_: Exception) {
                    setStatus("No browser available to open this device's page.", err = true)
                }
            }
            b.deviceList.addView(row.root)
        }
    }

    /** "Online · last seen 2m ago" / "On its node (othernode.example) · …" / "Offline · …". */
    private fun describeMeta(d: DeviceItem): String {
        val ago = formatLastSeen(d.lastSeen)
        return when {
            d.online -> getString(R.string.devices_meta_online, ago)
            d.elsewhere != null -> {
                val host = try { Uri.parse(d.elsewhere).host ?: d.elsewhere }
                           catch (_: Exception) { d.elsewhere }
                getString(R.string.devices_meta_elsewhere, host ?: d.elsewhere, ago)
            }
            else -> getString(R.string.devices_meta_offline, ago)
        }
    }

    private fun formatLastSeen(epochSec: Long?): String {
        if (epochSec == null || epochSec <= 0L) return getString(R.string.devices_meta_never)
        val nowSec = System.currentTimeMillis() / 1000L
        val deltaSec = (nowSec - epochSec).coerceAtLeast(0L)
        return when {
            deltaSec < 60 -> "${deltaSec}s ago"
            deltaSec < 3600 -> "${deltaSec / 60}m ago"
            deltaSec < 86_400 -> "${deltaSec / 3600}h ago"
            else -> "${deltaSec / 86_400}d ago"
        }
    }

    private fun dotFor(d: DeviceItem): GradientDrawable {
        val color = when {
            d.online -> 0xFF34D399.toInt()        // emerald
            d.elsewhere != null -> 0xFFFBBF24.toInt()  // amber
            else -> 0xFF6B7280.toInt()             // grey
        }
        return GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(color)
        }
    }

    private fun setStatus(text: String, err: Boolean) {
        if (text.isBlank()) { b.statusText.visibility = View.GONE; return }
        b.statusText.text = text
        b.statusText.visibility = View.VISIBLE
        b.statusText.setTextColor(if (err) 0xFFEF4444.toInt() else 0xFF67E8F9.toInt())
    }

    private fun setBusy(busy: Boolean) {
        b.progress.visibility = if (busy) View.VISIBLE else View.GONE
        b.refreshBtn.isEnabled = !busy
    }

    private data class DeviceItem(
        val id: String,
        val name: String,
        val online: Boolean,
        val elsewhere: String?,   // node URL or null
        val lastSeen: Long?,      // epoch seconds; null = never
        val thisDevice: Boolean,
    )
}
