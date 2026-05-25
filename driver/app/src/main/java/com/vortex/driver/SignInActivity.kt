package com.vortex.driver

import android.content.Intent
import android.graphics.Typeface
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
 * B6: in-app sign-in. B7: in-app register too -- the same activity
 * with a Sign-in / Create-account toggle so the user can switch
 * direction without leaving the screen.
 *
 * Flows (single tap on the user's part):
 *
 *   SIGN-IN:
 *     1. POST {hub}/login                  (form, 303 + Set-Cookie)
 *     2. POST {hub}/api/session-enroll     (JSON, cookie auto-forwarded)
 *     3. Prefs.saveDevice + start service.
 *
 *   REGISTER:
 *     0. GET  {hub}/api/registration-mode  (to hide invite when not needed)
 *     1. POST {hub}/api/session-register   (JSON in/out; sets cookie on ok)
 *     2. POST {hub}/api/session-enroll     (same as above; chain)
 *     3. Prefs.saveDevice + start service.
 *
 * Token-paste fallback ([EnrollActivity]) still reachable via
 * "Have an account token instead?" inside this activity AND via
 * the `vortex://enroll` deep-link.
 */
class SignInActivity : AppCompatActivity() {

    private lateinit var b: ActivitySignInBinding
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private val cookieJar = InMemoryCookieJar()
    private val http: OkHttpClient by lazy {
        OkHttpClient.Builder()
            .cookieJar(cookieJar)
            .followRedirects(false)
            .followSslRedirects(false)
            .build()
    }

    private enum class Mode { SIGN_IN, REGISTER }
    private var mode: Mode = Mode.SIGN_IN
    /** Cached from GET /api/registration-mode; null until first probe. */
    private var hubRegistrationMode: String? = null
    private var hubIsBootstrap: Boolean = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivitySignInBinding.inflate(layoutInflater)
        setContentView(b.root)

        b.hubUrl.setText(Prefs.bootstrapUrl(this) ?: "")
        b.deviceName.setText(Prefs.deviceName(this) ?: Build.MODEL ?: "")

        b.modeSignin.setOnClickListener { setMode(Mode.SIGN_IN) }
        b.modeRegister.setOnClickListener { setMode(Mode.REGISTER) }
        b.signinBtn.setOnClickListener { onSubmit() }
        b.cancelBtn.setOnClickListener { finish() }
        b.useTokenBtn.setOnClickListener {
            startActivity(Intent(this, EnrollActivity::class.java))
            finish()
        }
        b.registerBtn.setOnClickListener { openRegisterInBrowser() }

        setMode(Mode.SIGN_IN)
    }

    override fun onDestroy() {
        scope.cancel()
        super.onDestroy()
    }

    private fun setMode(m: Mode) {
        mode = m
        val isReg = (m == Mode.REGISTER)
        // Visual: swap the toggle-pill background drawables and adjust
        // text color/weight so the active pill reads as selected.
        b.modeSignin.setBackgroundResource(
            if (isReg) R.drawable.vortex_pill_off else R.drawable.vortex_pill_on
        )
        b.modeRegister.setBackgroundResource(
            if (isReg) R.drawable.vortex_pill_on else R.drawable.vortex_pill_off
        )
        b.modeSignin.setTextColor(
            ContextCompat.getColor(this,
                if (isReg) R.color.vortex_text_subtle else R.color.vortex_text)
        )
        b.modeRegister.setTextColor(
            ContextCompat.getColor(this,
                if (isReg) R.color.vortex_text else R.color.vortex_text_subtle)
        )
        b.modeSignin.setTypeface(null, if (isReg) Typeface.NORMAL else Typeface.BOLD)
        b.modeRegister.setTypeface(null, if (isReg) Typeface.BOLD else Typeface.NORMAL)
        b.title.text = getString(
            if (isReg) R.string.signin_title_register else R.string.signin_title
        )
        b.subtitle.text = getString(
            if (isReg) R.string.signin_subtitle_register else R.string.signin_subtitle
        )
        b.signinBtn.text = getString(
            if (isReg) R.string.signin_btn_register else R.string.signin_btn
        )
        b.password2Label.visibility = if (isReg) View.VISIBLE else View.GONE
        b.password2.visibility = if (isReg) View.VISIBLE else View.GONE
        // Invite field visibility depends on the hub's mode -- we probe
        // when the user enters Register mode so we don't ask for an
        // invite if registration is open or this is the bootstrap user.
        if (isReg) {
            // Optimistic: show until we know better.
            b.inviteLabel.visibility = View.VISIBLE
            b.invite.visibility = View.VISIBLE
            probeRegistrationMode()
        } else {
            b.inviteLabel.visibility = View.GONE
            b.invite.visibility = View.GONE
        }
        // Reset the bottom-link "register in browser" -- still useful
        // as a fallback if the hub doesn't expose /api/session-register
        // (pre-V5.25).
    }

    private fun probeRegistrationMode() {
        val hubUrl = b.hubUrl.text.toString().trim().trimEnd('/')
        if (hubUrl.isEmpty()) return
        scope.launch {
            val r = runCatching { getRegistrationMode(hubUrl) }
            r.onSuccess { info ->
                hubRegistrationMode = info.optString("mode", "invite")
                hubIsBootstrap = info.optBoolean("bootstrap", false)
                val needsInvite = !hubIsBootstrap && hubRegistrationMode == "invite"
                b.inviteLabel.visibility = if (needsInvite) View.VISIBLE else View.GONE
                b.invite.visibility = if (needsInvite) View.VISIBLE else View.GONE
                if (hubRegistrationMode == "closed") {
                    setStatus(
                        "This hub has registration closed. Ask an admin for an " +
                        "invite code or use Sign-in if you already have an account.",
                        err = true,
                    )
                } else if (hubIsBootstrap) {
                    setStatus(
                        "First-user setup: this account will be the hub admin.",
                        err = false,
                    )
                }
            }
            // Probe failure isn't fatal -- we just leave the invite
            // field visible and let the hub return a clear error.
        }
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

    private fun onSubmit() {
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
        if (mode == Mode.REGISTER) {
            val pw2 = b.password2.text.toString()
            if (password != pw2) {
                setStatus("Passwords do not match.", err = true); return
            }
            if (password.length < 8) {
                setStatus("Password must be at least 8 characters.", err = true); return
            }
        }
        setBusy(true)
        setStatus(if (mode == Mode.REGISTER) "Creating account…" else "Signing in…", err = false)

        scope.launch {
            val invite = b.invite.text.toString().trim()
            val result = runCatching {
                cookieJar.clear()
                if (mode == Mode.REGISTER) {
                    register(hubUrl, invite, username, password)
                } else {
                    login(hubUrl, username, password)
                }
                sessionEnroll(hubUrl, devName)
            }
            setBusy(false)
            result.onSuccess { (deviceId, deviceToken, name, nodes) ->
                Prefs.saveBootstrap(this@SignInActivity, "session", hubUrl)
                Prefs.saveDevice(this@SignInActivity, deviceId, deviceToken, name, nodes)
                val verb = if (mode == Mode.REGISTER) "Registered" else "Signed in"
                setStatus("$verb as $username. Enrolled as $name. Starting service…",
                          err = false)
                val i = Intent(this@SignInActivity, DriverService::class.java)
                ContextCompat.startForegroundService(this@SignInActivity, i)
                finish()
            }.onFailure { e ->
                val verb = if (mode == Mode.REGISTER) "Register" else "Sign-in"
                setStatus("$verb failed: ${e.javaClass.simpleName}: ${e.message ?: ""}",
                          err = true)
            }
        }
    }

    private suspend fun login(hubUrl: String, username: String, password: String): Unit =
        withContext(Dispatchers.IO) {
            val form = FormBody.Builder()
                .add("username", username)
                .add("password", password)
                .add("next", "/")
                .build()
            val req = Request.Builder().url("$hubUrl/login").post(form).build()
            http.newCall(req).execute().use { rsp ->
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
                        "Unexpected sign-in response (HTTP ${rsp.code}). Is the Hub URL correct?"
                    )
                }
                if (!cookieJar.has(hubUrl, "vortex_session")) throw RuntimeException(
                    "Hub returned 303 but no session cookie. The hub may be older " +
                    "than V5.24 -- tap \"Have an account token instead?\" for the legacy flow."
                )
            }
        }

    private suspend fun register(
        hubUrl: String, invite: String, username: String, password: String,
    ): Unit = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("invite", invite)
            .put("username", username)
            .put("password", password)
            .toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url("$hubUrl/api/session-register").post(body).build()
        http.newCall(req).execute().use { rsp ->
            val text = rsp.body?.string().orEmpty()
            if (!rsp.isSuccessful) {
                if (rsp.code == 404) throw RuntimeException(
                    "This hub doesn't support in-app registration (added in V5.25). " +
                    "Tap \"Open hub register page in browser\" below for the web flow."
                )
                val detail = try { JSONObject(text).optString("detail", text) }
                             catch (_: Exception) { text }
                throw RuntimeException("HTTP ${rsp.code}: ${detail.take(200)}")
            }
            if (!cookieJar.has(hubUrl, "vortex_session")) throw RuntimeException(
                "Hub returned 200 OK but no session cookie -- unexpected."
            )
        }
    }

    private suspend fun getRegistrationMode(hubUrl: String): JSONObject =
        withContext(Dispatchers.IO) {
            val req = Request.Builder().url("$hubUrl/api/registration-mode").build()
            http.newCall(req).execute().use { rsp ->
                if (!rsp.isSuccessful) throw RuntimeException("HTTP ${rsp.code}")
                JSONObject(rsp.body?.string().orEmpty())
            }
        }

    private suspend fun sessionEnroll(
        hubUrl: String, deviceName: String,
    ): EnrollResult = withContext(Dispatchers.IO) {
        val body = JSONObject()
            .put("device_name", deviceName)
            .toString()
            .toRequestBody("application/json".toMediaType())
        val req = Request.Builder().url("$hubUrl/api/session-enroll").post(body).build()
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
        b.modeSignin.isEnabled = !busy
        b.modeRegister.isEnabled = !busy
        b.progress.visibility = if (busy) View.VISIBLE else View.GONE
    }

    private data class EnrollResult(
        val deviceId: String,
        val deviceToken: String,
        val name: String,
        val nodes: List<String>,
    )

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
