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

    private val notifPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        binding.notifStatus.text = if (granted)
            getString(R.string.notif_granted)
        else
            getString(R.string.notif_denied)
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

        ensureNotificationPermission()
        refreshNotifStatus()
    }

    private fun ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            // < Android 13: notifications don't need a runtime permission.
            return
        }
        val granted = ContextCompat.checkSelfPermission(
            this, Manifest.permission.POST_NOTIFICATIONS,
        ) == PackageManager.PERMISSION_GRANTED
        if (!granted) {
            notifPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
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
