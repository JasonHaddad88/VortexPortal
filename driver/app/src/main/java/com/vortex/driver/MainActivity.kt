package com.vortex.driver

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.vortex.driver.databinding.ActivityMainBinding

/**
 * The launcher screen. Two jobs in M0:
 *   1. Ask for the POST_NOTIFICATIONS runtime permission (Android 13+).
 *   2. Start / stop the foreground [DriverService] so the user can
 *      verify the lifecycle from outside.
 *
 * Real device-control happens in the service, not the activity. This UI is
 * intentionally minimal -- once paired, the user shouldn't need to open it.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding

    /**
     * Request both POST_NOTIFICATIONS (foreground-service notification UI)
     * and CAMERA (M1: actual camera use) in one prompt. Doing them
     * together up front avoids the "service starts but every client
     * connection errors out with no-perm" failure mode on a fresh install.
     */
    private val permissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ ->
        refreshNotifStatus()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.title.text = getString(R.string.app_name)
        binding.subtitle.text = getString(
            R.string.subtitle_version, BuildConfig.VERSION_NAME,
        )

        binding.startBtn.setOnClickListener { startDriver() }
        binding.stopBtn.setOnClickListener { stopDriver() }
        binding.armScreenBtn.setOnClickListener { armScreenSharing() }
        binding.disarmScreenBtn.setOnClickListener { disarmScreenSharing() }

        ensureNotificationPermission()
        refreshNotifStatus()
    }

    /** Launch the system MediaProjection consent dialog (M2). */
    private fun armScreenSharing() {
        startActivity(Intent(this, ScreenSetupActivity::class.java))
        binding.disarmScreenBtn.visibility = View.VISIBLE
    }

    private fun disarmScreenSharing() {
        val i = Intent(this, DriverService::class.java).apply {
            action = DriverService.ACTION_DISARM_SCREEN
        }
        ContextCompat.startForegroundService(this, i)
        binding.disarmScreenBtn.visibility = View.GONE
    }

    private fun ensureNotificationPermission() {
        val needed = mutableListOf<String>()
        // POST_NOTIFICATIONS only on Android 13+
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            needed += Manifest.permission.POST_NOTIFICATIONS
        }
        // CAMERA needed since the very first agent connection (M1).
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA)
                != PackageManager.PERMISSION_GRANTED) {
            needed += Manifest.permission.CAMERA
        }
        if (needed.isNotEmpty()) {
            permissionLauncher.launch(needed.toTypedArray())
        }
    }

    private fun refreshNotifStatus() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            binding.notifStatus.text = getString(R.string.notif_pre13)
            return
        }
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS,
        ) == PackageManager.PERMISSION_GRANTED
        binding.notifStatus.text = if (granted)
            getString(R.string.notif_granted)
        else
            getString(R.string.notif_denied)
    }

    private fun startDriver() {
        val i = Intent(this, DriverService::class.java)
        ContextCompat.startForegroundService(this, i)
        binding.serviceStatus.text = getString(R.string.service_started)
        binding.stopBtn.visibility = View.VISIBLE
    }

    private fun stopDriver() {
        stopService(Intent(this, DriverService::class.java))
        binding.serviceStatus.text = getString(R.string.service_stopped)
        binding.stopBtn.visibility = View.GONE
    }
}
