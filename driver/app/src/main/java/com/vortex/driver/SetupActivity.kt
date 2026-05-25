package com.vortex.driver

import android.content.Intent
import android.os.Bundle
import android.view.View
import androidx.appcompat.app.AppCompatActivity
import com.vortex.driver.databinding.ActivitySetupBinding
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * B11: first-run database setup. The APK talks to Turso directly
 * (no FastAPI hub server). The user pastes:
 *
 *   - a libsql:// URL (or https://… for the Hrana HTTP endpoint)
 *   - a JWT auth token (from `turso db tokens create <db>`)
 *
 * We probe the connection with `SELECT 1` before saving, so a typo
 * or expired token fails here instead of confusingly later on the
 * Sign-in screen. On success we save the pair to [Prefs] and route
 * the user into the sign-in flow (which now reads from Turso
 * directly via [Auth]).
 *
 * If the URL/token combo can't authenticate (HTTP 401) or the host
 * doesn't resolve, the error string is surfaced verbatim. The most
 * common failures are wrong URL scheme (libsql:// vs https://) and
 * stale tokens, both of which produce clear messages from Turso.
 */
class SetupActivity : AppCompatActivity() {

    private lateinit var b: ActivitySetupBinding
    private val scope = CoroutineScope(Dispatchers.Main + Job())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivitySetupBinding.inflate(layoutInflater)
        setContentView(b.root)
        // Pre-fill if the user is reconfiguring an existing Turso setup.
        b.tursoUrl.setText(Prefs.tursoUrl(this) ?: "")
        b.tursoToken.setText(Prefs.tursoToken(this) ?: "")
        b.relayUrl.setText(Prefs.relayUrl(this) ?: "")
        b.saveBtn.setOnClickListener { onSave() }
        b.testBtn.setOnClickListener { onTest() }
    }

    override fun onDestroy() { scope.cancel(); super.onDestroy() }

    private fun onSave() {
        val (url, token) = readForm() ?: return
        val relay = b.relayUrl.text.toString().trim().trimEnd('/')
        if (relay.isNotEmpty() && !relay.startsWith("http://") && !relay.startsWith("https://")) {
            setStatus("Relay URL must start with http:// or https://", err = true); return
        }
        setBusy(true)
        setStatus("Verifying connection…", err = false)
        scope.launch {
            val r = probe(url, token)
            setBusy(false)
            if (r == null) {
                Prefs.saveTurso(this@SetupActivity, url, token)
                if (relay.isNotEmpty()) Prefs.saveRelay(this@SetupActivity, relay)
                else                    Prefs.clearRelay(this@SetupActivity)
                setStatus("Connected. Saved.", err = false)
                // Forward to sign-in; clear-task so back doesn't bounce
                // back into Setup until the user explicitly Resets.
                val i = Intent(this@SetupActivity, SignInActivity::class.java)
                i.flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                startActivity(i)
                finish()
            } else {
                setStatus("Couldn't connect: $r", err = true)
            }
        }
    }

    private fun onTest() {
        val (url, token) = readForm() ?: return
        setBusy(true)
        setStatus("Probing…", err = false)
        scope.launch {
            val r = probe(url, token)
            setBusy(false)
            if (r == null) setStatus("Connection OK -- ready to save.", err = false)
            else            setStatus("Couldn't connect: $r", err = true)
        }
    }

    /** Returns null on success, error string on failure. */
    private suspend fun probe(url: String, token: String): String? =
        withContext(Dispatchers.IO) {
            try {
                TursoClient(url, token).execute("SELECT 1")
                null
            } catch (e: TursoError) { e.message ?: "unknown error" }
            catch (e: Throwable)    { "${e.javaClass.simpleName}: ${e.message ?: ""}" }
        }

    private fun readForm(): Pair<String, String>? {
        val url = b.tursoUrl.text.toString().trim()
        val token = b.tursoToken.text.toString().trim()
        if (url.isEmpty() || token.isEmpty()) {
            setStatus("Both fields are required.", err = true); return null
        }
        if (!url.startsWith("libsql://") && !url.startsWith("https://") &&
            !url.startsWith("http://") && !url.startsWith("wss://")) {
            setStatus("URL should start with libsql:// (or https://).", err = true); return null
        }
        return url to token
    }

    private fun setStatus(text: String, err: Boolean) {
        b.statusText.text = text
        b.statusText.visibility = View.VISIBLE
        b.statusText.setTextColor(if (err) 0xFFEF4444.toInt() else 0xFF67E8F9.toInt())
    }
    private fun setBusy(busy: Boolean) {
        b.saveBtn.isEnabled = !busy
        b.testBtn.isEnabled = !busy
        b.progress.visibility = if (busy) View.VISIBLE else View.GONE
    }
}
