package com.vortex.driver

import android.content.Intent
import android.os.Bundle
import androidx.appcompat.app.AppCompatActivity

/**
 * B10: launcher router. The APK's front door is now the auth
 * experience (Sign-in / Register), matching the webapp's flow --
 * NOT the Enable-Driver / Start-Service control panel, which now
 * lives in [MainActivity] (still reachable from the notification
 * + a "Device settings" link in the dashboard).
 *
 * Decision tree:
 *   - If [Prefs.isEnrolled] -> go to [DevicesActivity] (the
 *     dashboard / "My devices" peer list).
 *   - Otherwise -> go to [SignInActivity] (the webapp-styled
 *     sign-in / register form).
 *
 * Doesn't render any UI itself; finishes immediately after
 * forwarding. Keeps the launcher intent clean and lets the rest
 * of the app evolve without touching the AndroidManifest each time.
 */
class EntryActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // B11 routing tree:
        //   1. No Turso URL+token yet  -> SetupActivity (paste DB creds)
        //   2. Not signed in            -> SignInActivity
        //   3. Signed in                -> DevicesActivity
        val next = when {
            !Prefs.isTursoConfigured(this) -> Intent(this, SetupActivity::class.java)
            !Prefs.isSignedIn(this)         -> Intent(this, SignInActivity::class.java)
            else                            -> Intent(this, DevicesActivity::class.java)
        }
        // Clear-task on the new front door so the back button from
        // either destination exits the app cleanly instead of
        // bouncing through this no-UI router.
        next.flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
        startActivity(next)
        finish()
    }
}
