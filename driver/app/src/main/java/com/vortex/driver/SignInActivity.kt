package com.vortex.driver

import android.content.Intent
import android.graphics.Typeface
import android.net.Uri
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

/**
 * B6/B7: in-app sign-in + register, restyled in B10 to match the
 * webapp, and rebuilt in B11 to talk to Turso directly (no FastAPI
 * hub server in the middle).
 *
 * Auth path:
 *   - Sign in   ->  [Auth.signIn]     -> SELECT users WHERE username=?
 *                                        + PBKDF2 verify.
 *   - Register  ->  [Auth.register]   -> COUNT(users) for bootstrap +
 *                                        invite check + INSERT user.
 *
 * After success we save (user_id, username, is_admin) to [Prefs] and
 * route to [DevicesActivity]. Device enrollment-as-row (writing into
 * the `devices` table for this phone) is a separate B11.2 step; for
 * now the sign-in just gets the user signed in.
 *
 * The Node-URL field from B10 is gone -- the database creds live in
 * [Prefs] (set via [SetupActivity]) and apply to every API call.
 */
class SignInActivity : AppCompatActivity() {

    private lateinit var b: ActivitySignInBinding
    private val scope = CoroutineScope(Dispatchers.Main + Job())

    private enum class Mode { SIGN_IN, REGISTER }
    private var mode: Mode = Mode.SIGN_IN

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        b = ActivitySignInBinding.inflate(layoutInflater)
        setContentView(b.root)

        // Pre-fill device name with the saved one (or the phone model).
        b.deviceName.setText(Prefs.deviceName(this) ?: android.os.Build.MODEL ?: "")

        b.modeSignin.setOnClickListener { setMode(Mode.SIGN_IN) }
        b.modeRegister.setOnClickListener { setMode(Mode.REGISTER) }
        b.signinBtn.setOnClickListener { onSubmit() }
        b.cancelBtn.setOnClickListener { finish() }

        // "Have an account token instead?" used to open EnrollActivity
        // (B1's account-token paste path). With the Turso-direct backend,
        // there's no account-token round-trip; repurpose this link to
        // open SetupActivity so the user can re-edit their DB creds.
        b.useTokenBtn.text = getString(R.string.signin_open_setup)
        b.useTokenBtn.setOnClickListener {
            startActivity(Intent(this, SetupActivity::class.java))
        }
        // The "Open hub register page in browser" link has no meaning
        // when there's no hub URL. Hide it entirely.
        b.registerBtn.visibility = View.GONE

        setMode(Mode.SIGN_IN)
    }

    override fun onDestroy() { scope.cancel(); super.onDestroy() }

    private fun setMode(m: Mode) {
        mode = m
        val isReg = (m == Mode.REGISTER)
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
        // The invite field is only meaningful for register. We always
        // show it -- can't probe a `registration_mode` setting without
        // a hub. The user leaves it blank if their first user; if
        // their DB requires invites, they paste one. Either way the
        // Auth helper validates and surfaces a clear error.
        b.inviteLabel.visibility = if (isReg) View.VISIBLE else View.GONE
        b.invite.visibility = if (isReg) View.VISIBLE else View.GONE
    }

    private fun onSubmit() {
        // Guard: Turso must be configured first.
        if (!Prefs.isTursoConfigured(this)) {
            setStatus("Database not configured. Tap \"Database setup\" below.", err = true)
            return
        }
        val username = b.username.text.toString().trim()
        val password = b.password.text.toString()
        if (username.isEmpty() || password.isEmpty()) {
            setStatus("Username and password are required.", err = true); return
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
            val devName = b.deviceName.text.toString().trim().ifBlank {
                android.os.Build.MODEL ?: "Android"
            }
            val result = if (mode == Mode.REGISTER) {
                Auth.register(this@SignInActivity, username, password, invite)
            } else {
                Auth.signIn(this@SignInActivity, username, password)
            }
            setBusy(false)
            when (result) {
                is Auth.Result.Ok -> {
                    Prefs.saveSession(
                        this@SignInActivity,
                        userId = result.userId,
                        username = result.username,
                        isAdmin = result.isAdmin,
                    )
                    // Stash the device-name preference so DevicesActivity
                    // / future enrollment can use it as the default.
                    val p = Prefs.prefs(this@SignInActivity)
                    p.edit().putString("device_name", devName).apply()
                    val verb = if (mode == Mode.REGISTER) "Registered" else "Signed in"
                    setStatus("$verb as ${result.username}.", err = false)
                    val i = Intent(this@SignInActivity, DevicesActivity::class.java)
                    i.flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
                    startActivity(i)
                    finish()
                }
                is Auth.Result.Err -> {
                    val verb = if (mode == Mode.REGISTER) "Register" else "Sign-in"
                    setStatus("$verb failed: ${result.message}", err = true)
                }
            }
        }
    }

    private fun setStatus(text: String, err: Boolean) {
        b.statusText.text = text
        b.statusText.visibility = View.VISIBLE
        b.statusText.setTextColor(if (err) 0xFFEF4444.toInt() else 0xFF67E8F9.toInt())
    }

    private fun setBusy(busy: Boolean) {
        b.signinBtn.isEnabled = !busy
        b.cancelBtn.isEnabled = !busy
        b.useTokenBtn.isEnabled = !busy
        b.modeSignin.isEnabled = !busy
        b.modeRegister.isEnabled = !busy
        b.progress.visibility = if (busy) View.VISIBLE else View.GONE
    }
}
