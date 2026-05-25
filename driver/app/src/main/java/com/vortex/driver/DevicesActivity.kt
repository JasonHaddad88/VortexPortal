package com.vortex.driver

import android.content.Intent
import android.graphics.drawable.GradientDrawable
import android.net.Uri
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.widget.PopupMenu
import androidx.appcompat.app.AppCompatActivity
import com.vortex.driver.databinding.ActivityDevicesBinding
import com.vortex.driver.databinding.RowDeviceBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

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

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityDevicesBinding.inflate(layoutInflater)
        setContentView(b.root)
        b.refreshBtn.setOnClickListener { refresh() }
        b.overflowBtn.setOnClickListener { showOverflowMenu(it) }
    }

    /** Top-right kebab. B11: cleanly separates DB config (Turso URL +
     *  token, lives across sessions) from user session (clears on
     *  sign-out). "Node settings" only shows when a legacy hub URL is
     *  still in play. */
    private fun showOverflowMenu(anchor: View) {
        val popup = PopupMenu(this, anchor)
        popup.menu.add(getString(R.string.dashboard_settings)).setOnMenuItemClickListener {
            startActivity(Intent(this, MainActivity::class.java)); true
        }
        popup.menu.add(getString(R.string.dashboard_db_setup)).setOnMenuItemClickListener {
            startActivity(Intent(this, SetupActivity::class.java)); true
        }
        val hubUrl = pickHubUrl()
        if (hubUrl != null) {
            popup.menu.add(getString(R.string.dashboard_node_settings)).setOnMenuItemClickListener {
                DeviceWebActivity.startPath(this, hubUrl, "/settings",
                                            getString(R.string.dashboard_node_settings))
                true
            }
        }
        popup.menu.add(getString(R.string.devices_refresh)).setOnMenuItemClickListener {
            refresh(); true
        }
        popup.menu.add(getString(R.string.dashboard_sign_out)).setOnMenuItemClickListener {
            // B11: clear the user session only. DB creds + device
            // enrollment stay so the user can sign in with a different
            // account against the same Turso, or just sign back in.
            Prefs.clearSession(this)
            stopService(Intent(this, DriverService::class.java))
            val i = Intent(this, EntryActivity::class.java)
            i.flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
            startActivity(i)
            finish()
            true
        }
        popup.show()
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
        // B11: read directly from Turso for the signed-in user. No hub
        // /api/account/devices round-trip; the device's enrollment is
        // implicit (any row in `devices` with owner_id == userId).
        if (!Prefs.isTursoConfigured(this)) {
            setStatus("Database not configured -- open Setup.", err = true)
            renderList(emptyList()); return
        }
        val userId = Prefs.userId(this)
        if (userId <= 0L) {
            setStatus("Not signed in.", err = true)
            renderList(emptyList()); return
        }
        setBusy(true)
        setStatus(getString(R.string.devices_loading), err = false)
        scope.launch {
            val result = runCatching { fetchListFromTurso(userId) }
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

    private suspend fun fetchListFromTurso(userId: Long): List<DeviceItem> =
        withContext(Dispatchers.IO) {
            val url = Prefs.tursoUrl(this@DevicesActivity)
                ?: throw RuntimeException("Turso URL not set")
            val tok = Prefs.tursoToken(this@DevicesActivity)
                ?: throw RuntimeException("Turso token not set")
            val client = TursoClient(url, tok)
            val rows = client.execute(
                "SELECT id, name, last_seen, paired_at " +
                "FROM devices WHERE owner_id = ? ORDER BY paired_at DESC",
                listOf(userId),
            ).rows
            // B11.2: cross-reference against device_peers for live
            // presence (fresh row within STALE_AFTER_SEC means online).
            // The webapp's `device_presence` table is hub-written and
            // would be empty for direct-Turso deploys, so we don't read
            // it here.
            val presence = PeerRegistry.listFresh(client)
            val thisDeviceId = Prefs.deviceId(this@DevicesActivity)
            rows.map { r ->
                val id = r["id"] as? String ?: ""
                DeviceItem(
                    id = id,
                    name = (r["name"] as? String) ?: "Unnamed",
                    online = presence.containsKey(id),
                    elsewhere = null,            // single-DB peer model: no "elsewhere"
                    lastSeen = r["last_seen"] as? Long,
                    thisDevice = (thisDeviceId == id),
                )
            }
        }

    /** Pick the URL to hit. Bootstrap was saved on enrollment; node list
     *  is the failover. If both empty, refuse. */
    private fun pickHubUrl(): String? {
        val boot = Prefs.bootstrapUrl(this)?.takeIf { it.isNotBlank() && it.startsWith("http") }
        if (boot != null) return boot.trimEnd('/')
        return Prefs.nodes(this).firstOrNull { it.startsWith("http") }?.trimEnd('/')
    }

    // B11: fetchList(hub) was the old hub-relay path. Now we read
    // straight from Turso (fetchListFromTurso above). The OkHttp + JSON
    // hub-API client is no longer needed here.

    private fun renderList(items: List<DeviceItem>) {
        b.deviceList.removeAllViews()
        // B11: in-app per-device control is a B11.2 milestone (it'll
        // dial the device's direct-WS port discovered via the
        // device_presence table in Turso). Until then, a tap shows
        // a status hint. The hubUrl may be null when no hub is in
        // play (Turso-direct deploy); WebView tap-through only fires
        // when there's a hub URL available from a previous session.
        val hubUrl = pickHubUrl()
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
                if (item.thisDevice) {
                    // Tapping THIS device on itself would dial localhost
                    // and just race a self-handshake. Skip with a hint;
                    // the user is already "here".
                    setStatus("This is the current device. Open Vortex on another " +
                              "phone to control this one.", err = false)
                    return@setOnClickListener
                }
                // B11.3: native peer viewer. Dials the device's
                // published direct-WS endpoint from `device_peers` and
                // renders Screen / Camera / Info in the APK.
                PeerControlActivity.start(this, item.id, item.name)
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
