package com.vortex.driver

import android.annotation.SuppressLint
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import com.vortex.driver.databinding.ActivityDeviceWebBinding
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
import java.net.URI
import java.util.concurrent.TimeUnit

/**
 * B9: open a hub's per-device manage page in an embedded WebView
 * instead of bouncing the user out to the system browser.
 *
 * Auth bridge (V5.27+):
 *   1. POST {hub}/api/device-session with X-Vortex-Device + X-Vortex-Token.
 *      Hub validates the device's credentials and returns a Set-Cookie
 *      header setting vortex_session for the device's owner.
 *   2. Copy that Set-Cookie blob into Android's CookieManager keyed on
 *      the hub's URL.
 *   3. Load {hub}/devices/{id} in the WebView. The WebView ships the
 *      cookie back automatically -> user lands on the manage page
 *      already signed in. No browser detour, no second password prompt.
 *
 * Falls back gracefully on hubs older than V5.27 (no /api/device-session):
 * shows a notice on the error overlay; the user can still tap Retry,
 * which will try the WebView load against the hub anyway and ask the
 * user to sign in via the hub's normal /login page.
 *
 * Security posture (matches what the hub already grants over the API):
 *   - File access disabled on the WebView so a hostile page can't
 *     read out app-private files.
 *   - External links escape to the system browser via shouldOverrideUrlLoading.
 *   - Cookie bridge ONLY sets the vortex_session value the hub returned;
 *     we don't echo unknown cookies into CookieManager.
 *   - Mixed content blocked (the hub is https; refuse downgrade).
 */
class DeviceWebActivity : AppCompatActivity() {

    private lateinit var b: ActivityDeviceWebBinding
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .callTimeout(15, TimeUnit.SECONDS)
            .followRedirects(false)
            .followSslRedirects(false)
            .build()
    }

    private var deviceId: String = ""
    private var deviceName: String = ""
    private var hubUrl: String = ""
    private var hubHost: String = ""

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivityDeviceWebBinding.inflate(layoutInflater)
        setContentView(b.root)

        deviceId = intent.getStringExtra(EXTRA_DEVICE_ID).orEmpty()
        deviceName = intent.getStringExtra(EXTRA_DEVICE_NAME).orEmpty()
        hubUrl = (intent.getStringExtra(EXTRA_HUB_URL).orEmpty()).trimEnd('/')
        if (deviceId.isBlank() || hubUrl.isBlank()) {
            showError("Missing device id or hub URL."); return
        }
        hubHost = runCatching { URI(hubUrl).host.orEmpty() }.getOrDefault("")
        if (hubHost.isBlank()) {
            showError("Hub URL has no host: $hubUrl"); return
        }
        title = deviceName.ifBlank { getString(R.string.devweb_title) }

        with(b.web.settings) {
            javaScriptEnabled = true       // hub UI relies on JS
            domStorageEnabled = true       // hub session/local storage
            allowFileAccess = false
            allowContentAccess = false
            @Suppress("DEPRECATION") allowFileAccessFromFileURLs = false
            @Suppress("DEPRECATION") allowUniversalAccessFromFileURLs = false
            mediaPlaybackRequiresUserGesture = false  // /screen + /camera auto-play
            mixedContentMode = android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW
            userAgentString = "$userAgentString VortexDriver/${BuildConfig.VERSION_NAME}"
        }
        b.web.webViewClient = makeWebViewClient()
        b.web.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, newProgress: Int) {
                b.progress.progress = newProgress
                b.progress.visibility = if (newProgress in 1..99) View.VISIBLE else View.GONE
            }
        }
        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(b.web, true)

        b.retryBtn.setOnClickListener { startBridgeAndLoad() }
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (b.web.canGoBack()) b.web.goBack() else finish()
            }
        })
        startBridgeAndLoad()
    }

    override fun onDestroy() {
        scope.cancel()
        b.web.stopLoading()
        b.web.destroy()
        super.onDestroy()
    }

    private fun makeWebViewClient(): WebViewClient = object : WebViewClient() {
        // Keep hub navigation inside the WebView; external links open
        // in the system browser. Same rule the dashboard expects.
        override fun shouldOverrideUrlLoading(view: WebView?, req: WebResourceRequest?): Boolean {
            val u = req?.url ?: return false
            val host = u.host ?: return false
            // Allow same-host navigation. Send anything else to the
            // system browser -- "Open hub help in browser" etc.
            return if (host.equals(hubHost, ignoreCase = true)) {
                false
            } else {
                try { startActivity(Intent(Intent.ACTION_VIEW, u)) } catch (_: Exception) {}
                true
            }
        }
    }

    /** Run the auth bridge POST, push the resulting cookie into the
     *  WebView's CookieManager, and load the manage URL. Hides any
     *  previous error overlay on success. */
    private fun startBridgeAndLoad() {
        b.errorBox.visibility = View.GONE
        val devId = Prefs.deviceId(this)
        val tok = Prefs.deviceToken(this)
        if (devId.isNullOrBlank() || tok.isNullOrBlank()) {
            showError("Not enrolled."); return
        }
        b.progress.visibility = View.VISIBLE
        b.progress.progress = 5

        scope.launch {
            val r = runCatching { postDeviceSession(hubUrl, devId, tok) }
            r.onSuccess { cookie ->
                if (cookie != null) installSessionCookie(cookie)
                // Either way (cookie or none), load the page -- if the
                // bridge failed with a non-fatal reason the user can
                // still log in via the hub's /login page inside the
                // WebView.
                loadManagePage()
            }.onFailure { e ->
                // 404 / 405 -> probably an older hub without
                // /api/device-session. Surface the notice but still
                // try to load the page so the user can sign in
                // manually in the WebView.
                if (e is BridgeUnavailable) {
                    showError(getString(R.string.devweb_old_hub))
                } else {
                    showError(getString(
                        R.string.devweb_auth_failed,
                        "${e.javaClass.simpleName}: ${e.message ?: ""}"
                    ))
                }
                loadManagePage()
            }
        }
    }

    private fun loadManagePage() {
        b.web.loadUrl("$hubUrl/devices/$deviceId")
    }

    /**
     * POST {hub}/api/device-session and return the vortex_session
     * Set-Cookie blob, or null if the hub returned 200 but no
     * matching Set-Cookie. Throws [BridgeUnavailable] on 404/405
     * (older hub), or a plain RuntimeException with the hub's detail
     * on other failures.
     */
    private suspend fun postDeviceSession(
        hubUrl: String, deviceId: String, token: String,
    ): String? = withContext(Dispatchers.IO) {
        // Empty JSON body keeps content-type-aware middleware happy.
        val body = "{}".toRequestBody("application/json".toMediaType())   // (kotlin ext)
        val req = Request.Builder()
            .url("$hubUrl/api/device-session")
            .header("X-Vortex-Device", deviceId)
            .header("X-Vortex-Token", token)
            .post(body)
            .build()
        http.newCall(req).execute().use { rsp ->
            if (rsp.code == 404 || rsp.code == 405) throw BridgeUnavailable()
            if (!rsp.isSuccessful) {
                val txt = rsp.body?.string().orEmpty().take(200)
                throw RuntimeException("HTTP ${rsp.code}: $txt")
            }
            // OkHttp doesn't expose raw Set-Cookie headers as a Cookie
            // string by default; grab them directly so we can copy the
            // exact value the WebView will need to send back.
            val rawSetCookies = rsp.headers("Set-Cookie")
            rawSetCookies.firstOrNull { it.startsWith("vortex_session=") }
        }
    }

    /** Push a single Set-Cookie line into Android's CookieManager,
     *  keyed on the hub URL so the WebView sees it on the next load. */
    private fun installSessionCookie(setCookieHeader: String) {
        val cm = CookieManager.getInstance()
        cm.setCookie(hubUrl, setCookieHeader)
        cm.flush()
    }

    private fun showError(msg: String) {
        b.errorText.text = msg
        b.errorBox.visibility = View.VISIBLE
        b.progress.visibility = View.GONE
    }

    private class BridgeUnavailable : RuntimeException("Hub does not expose /api/device-session")

    companion object {
        const val EXTRA_DEVICE_ID   = "device_id"
        const val EXTRA_DEVICE_NAME = "device_name"
        const val EXTRA_HUB_URL     = "hub_url"

        fun start(ctx: android.content.Context, hubUrl: String, deviceId: String, deviceName: String) {
            ctx.startActivity(Intent(ctx, DeviceWebActivity::class.java).apply {
                putExtra(EXTRA_HUB_URL, hubUrl)
                putExtra(EXTRA_DEVICE_ID, deviceId)
                putExtra(EXTRA_DEVICE_NAME, deviceName)
            })
        }
    }
}
