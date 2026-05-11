package com.vortex.driver

import android.content.Context
import android.content.Intent
import android.media.projection.MediaProjectionManager
import android.os.Bundle
import android.util.Log
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/**
 * Tiny transparent activity whose only job is to summon the system
 * screen-capture consent dialog and forward the result to [DriverService].
 *
 * Why this exists at all: [MediaProjectionManager.createScreenCaptureIntent]
 * returns an Intent that MUST be passed to `startActivityForResult` from an
 * Activity context -- a Service can't host the consent dialog. So we route
 * through this activity, grab the (resultCode, data) pair, hand it to the
 * service via an explicit intent extra, and finish ourselves immediately.
 *
 * Launched by MainActivity's "Arm screen sharing" button. The activity's
 * theme is fully transparent so the consent dialog appears to overlay
 * whatever the user was looking at.
 */
class ScreenSetupActivity : AppCompatActivity() {

    private val captureLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        val data = result.data
        if (result.resultCode == RESULT_OK && data != null) {
            // Hand the consent token to the service and close.
            val svc = Intent(this, DriverService::class.java).apply {
                action = DriverService.ACTION_ARM_SCREEN
                putExtra(DriverService.EXTRA_RESULT_CODE, result.resultCode)
                putExtra(DriverService.EXTRA_RESULT_DATA, data)
            }
            ContextCompat.startForegroundService(this, svc)
            Toast.makeText(this, R.string.screen_armed, Toast.LENGTH_SHORT).show()
        } else {
            Toast.makeText(this, R.string.screen_denied, Toast.LENGTH_SHORT).show()
        }
        finish()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val mgr = getSystemService(Context.MEDIA_PROJECTION_SERVICE)
                  as? MediaProjectionManager
        if (mgr == null) {
            Log.e(TAG, "MEDIA_PROJECTION_SERVICE unavailable on this device")
            Toast.makeText(this, R.string.screen_unavailable, Toast.LENGTH_LONG).show()
            finish()
            return
        }
        captureLauncher.launch(mgr.createScreenCaptureIntent())
    }

    companion object { private const val TAG = "ScreenSetupActivity" }
}
