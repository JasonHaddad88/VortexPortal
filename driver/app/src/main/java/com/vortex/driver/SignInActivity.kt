package com.vortex.driver

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vortex.driver.databinding.ActivitySignInBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.Cookie
import okhttp3.CookieJar
import okhttp3.FormBody
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject

/**
 * B6: in-app sign-in -- the new default enrollment path.
 *
 * Flow (one tap on the user's part):
 *
 *   1. POST {hub}/login  (form-encoded, username + password)
 *      -> 303 redirect on success. We don't follow the redirect; the
 *         Set-Cookie response header is what we want, which OkHttp's
 *         [CookieJar] captures for us into [InMemoryCookieJar].
 *
 *   2. POST {hub}/api/session-enroll  (JSON, device_name)
 *      -> cookie sent automatically by OkHttp's cookie jar
 *      -> JSON {device_id, token, name, nodes:[url,...]}  (same shape
 *         as /api/enroll; the new V5.24 hub endpoint).
 *
 *   3. Prefs.saveBootstrap + Prefs.saveDevice + start DriverService.
 *
 * If the hub doesn't expose /api/session-enroll (older than V5.24),
 * step 2 returns 404 -- we surface a clear message pointing the user
 * at the legacy "Have a token?" link.
 *
 * The token-paste flow ([EnrollActivity]) stays available for:
 *   - QR / `vortex://enroll` deep-links (no UI change needed)
 *   - users on older hubs without /api/session-enroll
 *   - headless setups that pre-mint a long-lived token
 */
class SignInActivity : AppCompatActivity() {

    private lateinit var b: ActivitySignInBinding
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private val cookieJar = InMemoryCookieJar()
    private val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .cookieJar(cookieJar)
            // Don't follow the /login 303 -- we want to inspect the
            // status + grab the Set-Cookie ourselves, not chase the
            // dashboard HTML that comes after.
            .followRedirects(false)
            .followSslRedirects(false)
            .build()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivitySignInBinding.inflate(layoutInflater)
        setContentView(b.root)

        // Prefill from any prior attempt's saved values.
        b.hubUrl.setText(Prefs.bootstrapUrl(this) ?: "")
        b.deviceName.setText(Prefs.deviceName(this) ?: Build.MODEL ?: "")

        b.signinBtn.setOnClickListener { doSignIn() }
        b.cancelBtn.setOnClickListener { finish() }
        b.useTokenBtn.setOnClickListener {
            startActivity(Intent(this, EnrollActivity::class.java))
            finish()
        }
        b.registerBtn.setOnClickListener { openRegisterInBrowser() }
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun openRegisterInBrowser() {
        val url = b.hubUrl.text.toString().trim().trimEnd('/')
        if (url.isEmpty() || (!url.startsWith("http://") && !url.startsWith("https://"))) {
            setStatus("Enter a Hub URL first so we know where to register.", err = true)
            return
        }
        try {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("$url/register")))
        } catch (_: Exception) {
            setStatus("No browser is set up to open $url/register.", err = true)
        }
    }

    private fun doSignIn() {
        val hubUrl = b.hubUrl.text.toString().trim().trimEnd('/')
        val username = b.username.text.toString().trim()
        val password = b.password.text.toString()
        val devName = b.deviceName.text.toString().trim().ifBlank { Build.MODEL ?: "Android" }
        if (hubUrl.isEmpty() || username.isEmpty() || password.isEmpty()) {
            setStatus("Hub URL, username, and password are all required.", err = true)
            return
        }
        if (!hubUrl.startsWith("http://") && !hubUrl.startsWith("https://")) {
            setStatus("Hub URL must start with http:// or https://", err = true)
            return
        }
        setBusy(true)
        setStatus("Signing in…", err = false)

        scope.launch {
            val result = runCatching {
                cookieJar.clear()
                login(hubUrl, username, password)
                sessionEnroll(hubUrl, devName)
            }
            setBusy(false)
            result.onSuccess { (deviceId, deviceToken, name, nodes) ->
                // Save bootstrap so HubClient can reconnect even if the
                // user later edits the URL (we still keep this one as
                // the first candidate).
                Prefs.saveBootstrap(this@SignInActivity, "session", hubUrl)
                Prefs.saveDevice(this@SignInActivity, deviceId, deviceToken, name, nodes)
                setStatus("Signed in as $username. Enrolled as $name. Starting service…",
                          err = false)
                val i = Intent(this@SignInActivity, DriverService::class.java)
                ContextCompat.startForegroundService(this@SignInActivity, i)
                finish()
            }.onFailure { e ->
                setStatus("Sign-in failed: ${e.javaClass.simpleName}: ${e.message ?: ""}",
                          err = true)
            }
        }
    }

    /**
     * POST {hub}/login (form-encoded). On success the hub returns 303
     * with `Set-Cookie: vortex_session=…`; we read the cookie via the
     * cookie jar (already wired into the OkHttp client) and forward it
     * automatically on the next request.
     */
    private suspend fun login(hubUrl: String, username: String, password: String): Unit =
        withContext(Dispatchers.IO) {
            val form = FormBody.Builder()
                .add("username", username)
                .add("password", password)
                .add("next", "/")
                .build()
            val req = Request.Builder()
                .url("$hubUrl/login")
                .post(form)
                .build()
            http.newCall(req).execute().use { rsp ->
                // 303 = success. 401 = bad creds. 429 = rate-limited.
                // 200 with an HTML body = we got the login page again,
                // which means creds were wrong but the server didn't
                // bother with a 401 (rare; handle defensively).
                when (rsp.code) {
                    303 -> { /* good */ }
                    401 -> throw RuntimeException("Wrong username or password.")
                    429 -> throw RuntimeException(
                        "Too many failed sign-in attempts. Wait a few minutes and try again."
                    )
                    in 500..599 -> throw RuntimeException(
                        "Hub error ${rsp.code}: ${rsp.body?.string().orEmpty().take(160)}"
                    )
                    else -> throw RuntimeException(
                        "Unexpected sign-in response (HTTP ${rsp.code}). " +
                        "Is the Hub URL correct?"
                    )
                }
                // Sanity check: the session cookie must actually be in the jar now.
                val haveSession = cookieJar.has(hubUrl, "vortex_session")
                if (!haveSession) {
                    throw RuntimeException(
                        "Hub returned 303 but no session cookie -- this may be an " +
                        "old hub version without /api/session-enroll. Tap " +
                        "\"Have an account token instead?\" below."
                    )
                }
            }
        }

    private suspend fun sessionEnroll(
        hubUrl: String, deviceName: String,
    ): EnrollResult = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("device_name", deviceName)
            .toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder()
            .url("$hubUrl/api/session-enroll")
            .post(body)
            .build()
        http.newCall(req).execute().use { rsp ->
            val text = rsp.body?.string().orEmpty()
            if (!rsp.isSuccessful) {
                val detail = try { JSONObject(text).optString("detail", text) }
                             catch (_: Exception) { text }
                if (rsp.code == 404) throw RuntimeException(
                    "This hub doesn't support sign-in enrollment (added in V5.24). " +
                    "Tap \"Have an account token instead?\" below for the legacy flow."
                )
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
        b.signinBtn.isEnabled = !busy
        b.cancelBtn.isEnabled = !busy
        b.useTokenBtn.isEnabled = !busy
        b.registerBtn.isEnabled = !busy
        b.progress.visibility = if (busy) View.VISIBLE else View.GONE
    }

    private data class EnrollResult(
        val deviceId: String,
        val deviceToken: String,
        val name: String,
        val nodes: List<String>,
    )

    /**
     * Minimal in-memory cookie jar. The hub sets one cookie
     * (`vortex_session`) on /login; we need to forward it on the
     * subsequent /api/session-enroll. No persistence -- the cookie
     * is single-use here and we'd rather not leave a usable session
     * on disk after the activity finishes.
     */
    private class InMemoryCookieJar : CookieJar {
        private val store = mutableMapOf<String, MutableList<Cookie>>()

        @Synchronized
        override fun saveFromResponse(url: HttpUrl, cookies: List<Cookie>) {
            val host = url.host
            val bucket = store.getOrPut(host) { mutableListOf() }
            for (c in cookies) {
                bucket.removeAll { it.name == c.name }
                bucket.add(c)
            }
        }

        @Synchronized
        override fun loadForRequest(url: HttpUrl): List<Cookie> {
            val now = System.currentTimeMillis()
            val bucket = store[url.host] ?: return emptyList()
            return bucket.filter { it.expiresAt > now && it.matches(url) }
        }

        @Synchronized
        fun has(hubUrl: String, name: String): Boolean = try {
            val host = hubUrl.toHttpUrl().host
            store[host]?.any { it.name == name && it.expiresAt > System.currentTimeMillis() } == true
        } catch (_: Exception) { false }

        @Synchronized
        fun clear() { store.clear() }
    }
}
