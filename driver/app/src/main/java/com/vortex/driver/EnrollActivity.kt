package com.vortex.driver

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vortex.driver.databinding.ActivityEnrollBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

/**
 * B1: paste an account enrollment token + a bootstrap node URL → POST
 * `{hubUrl}/api/enroll` → save device_id+token+nodes. Same flow the
 * Python agent's `enroll_now()` uses; this is its native equivalent so
 * Android devices never need Termux + Termux:API.
 */
class EnrollActivity : AppCompatActivity() {

    private lateinit var b: ActivityEnrollBinding
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private val http = OkHttpClient()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityEnrollBinding.inflate(layoutInflater)
        setContentView(b.root)

        // Prefill from any prior attempt's saved values.
        b.hubUrl.setText(Prefs.bootstrapUrl(this) ?: "")
        b.deviceName.setText(Prefs.deviceName(this) ?: Build.MODEL ?: "")

        // B2 ease-of-enrollment: vortex://enroll?token=…&hub=…&name=…
        // (typically opened by scanning the QR on the hub's token-
        // created page). Prefill what's there; auto-submit if the
        // required fields are both present.
        val uri = intent?.data
        var autoSubmit = false
        if (uri != null && uri.scheme == "vortex" && uri.host == "enroll") {
            uri.getQueryParameter("token")?.takeIf { it.isNotBlank() }
                ?.let { b.accountToken.setText(it) }
            uri.getQueryParameter("hub")?.takeIf { it.isNotBlank() }
                ?.let { b.hubUrl.setText(it) }
            uri.getQueryParameter("name")?.takeIf { it.isNotBlank() }
                ?.let { b.deviceName.setText(it) }
            autoSubmit = !b.accountToken.text.isNullOrBlank()
                && !b.hubUrl.text.isNullOrBlank()
        }

        b.enrollBtn.setOnClickListener { doEnroll() }
        b.cancelBtn.setOnClickListener { finish() }

        if (autoSubmit) {
            setStatus("Deep-link received — enrolling…", err = false)
            b.enrollBtn.post { doEnroll() }
        }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun doEnroll() {
        val token = b.accountToken.text.toString().trim()
        val hubUrl = b.hubUrl.text.toString().trim().trimEnd('/')
        val devName = b.deviceName.text.toString().trim().ifBlank { Build.MODEL ?: "Android" }
        if (token.isEmpty() || hubUrl.isEmpty()) {
            setStatus("Account token and hub URL are both required.", err = true)
            return
        }
        if (!hubUrl.startsWith("http://") && !hubUrl.startsWith("https://")) {
            setStatus("Hub URL must start with http:// or https://", err = true)
            return
        }
        setBusy(true)
        setStatus("Enrolling…", err = false)
        // Stash bootstrap immediately so a network-failure retry doesn't
        // make the user re-type the URL.
        Prefs.saveBootstrap(this, token, hubUrl)

        scope.launch {
            val result = runCatching { postEnroll(hubUrl, token, devName) }
            setBusy(false)
            result.onSuccess { (deviceId, deviceToken, name, nodes) ->
                Prefs.saveDevice(this@EnrollActivity, deviceId, deviceToken, name, nodes)
                setStatus("Enrolled as $name. Starting service…", err = false)
                // Kick the foreground service so it picks up the new creds
                // (its onCreate will start HubClient if Prefs.isEnrolled).
                val i = Intent(this@EnrollActivity, DriverService::class.java)
                ContextCompat.startForegroundService(this@EnrollActivity, i)
                finish()
            }.onFailure { e ->
                setStatus("Enrollment failed: ${e.javaClass.simpleName}: ${e.message ?: ""}",
                          err = true)
            }
        }
    }

    /** Returns (device_id, device_token, name, nodes). */
    private suspend fun postEnroll(
        hubUrl: String, accountToken: String, deviceName: String,
    ): EnrollResult = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("account_token", accountToken)
            .put("device_name", deviceName)
            .toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("$hubUrl/api/enroll")
            .post(body)
            .build()
        http.newCall(req).execute().use { rsp ->
            val text = rsp.body?.string().orEmpty()
            if (!rsp.isSuccessful) {
                val detail = try { JSONObject(text).optString("detail", text) }
                             catch (_: Exception) { text }
                throw RuntimeException("HTTP ${rsp.code}: ${detail.take(200)}")
            }
            val j = JSONObject(text)
            val deviceId = j.optString("device_id")
            val deviceToken = j.optString("token")
            if (deviceId.isBlank() || deviceToken.isBlank())
                throw RuntimeException("Server response missing device_id / token")
            val nodes = j.optJSONArray("nodes") ?: JSONArray()
            val nodeList = (0 until nodes.length()).mapNotNull {
                nodes.optString(it).takeIf { s -> s.isNotBlank() }
            }
            EnrollResult(deviceId, deviceToken, j.optString("name", deviceName), nodeList)
        }
    }

    private fun setStatus(text: String, err: Boolean) {
        b.statusText.text = text
        b.statusText.visibility = View.VISIBLE
        b.statusText.setTextColor(
            if (err) 0xFFEF4444.toInt() else 0xFF67E8F9.toInt()
        )
    }

    private fun setBusy(busy: Boolean) {
        b.enrollBtn.isEnabled = !busy
        b.cancelBtn.isEnabled = !busy
        b.progress.visibility = if (busy) View.VISIBLE else View.GONE
    }

    private data class EnrollResult(
        val deviceId: String,
        val deviceToken: String,
        val name: String,
        val nodes: List<String>,
    )
}
