package com.vortex.driver

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Bundle
import android.os.Looper
import androidx.core.content.ContextCompat
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONObject
import kotlin.coroutines.resume

/**
 * Native equivalent of the Python agent's `op_location` -- one
 * GPS / network fix via Android's [LocationManager], returned as JSON
 * with the same field names termux-location uses so the hub's
 * Theft Mode UI renders cleanly.
 *
 * Strategy:
 *   1. Check `getLastKnownLocation` on GPS + NETWORK. If the freshest
 *      hit is < [maxStaleMs] old, return it immediately (no battery
 *      hit, no perceptible delay).
 *   2. Otherwise race a fresh fix on GPS + NETWORK: first non-null
 *      callback wins, the other listener is detached. 30 s timeout.
 *
 * Permission contract: throws RuntimeException with a clear "Settings
 * → Apps → Vortex Driver → Permissions → Location → Allow" instruction
 * if [Manifest.permission.ACCESS_FINE_LOCATION] is missing -- the user
 * grants from the system Settings UI, no in-app prompt.
 */
object LocationOp {

    private const val DEFAULT_TIMEOUT_MS = 30_000L
    private const val MAX_STALE_MS = 30_000L

    suspend fun fix(ctx: Context, providerHint: String? = null): JSONObject {
        if (ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_FINE_LOCATION)
                != PackageManager.PERMISSION_GRANTED &&
            ContextCompat.checkSelfPermission(ctx, Manifest.permission.ACCESS_COARSE_LOCATION)
                != PackageManager.PERMISSION_GRANTED
        ) {
            throw RuntimeException(
                "Location permission not granted. Open Settings -> Apps -> " +
                "Vortex Driver -> Permissions -> Location -> Allow."
            )
        }
        val mgr = ctx.getSystemService(Context.LOCATION_SERVICE) as? LocationManager
            ?: throw RuntimeException("LOCATION_SERVICE unavailable on this device")

        val wanted = when (providerHint) {
            "gps"     -> listOf(LocationManager.GPS_PROVIDER)
            "network" -> listOf(LocationManager.NETWORK_PROVIDER)
            "passive" -> listOf(LocationManager.PASSIVE_PROVIDER)
            else      -> listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)
        }.filter { mgr.isProviderEnabled(it) }
        if (wanted.isEmpty()) {
            throw RuntimeException(
                "No usable location provider is enabled. Turn on Location " +
                "in the system Settings (top-level toggle)."
            )
        }

        // Fast path: a recent last-known fix is good enough.
        val fast = wanted.mapNotNull { p ->
            try { mgr.getLastKnownLocation(p) } catch (_: SecurityException) { null }
        }.maxByOrNull { it.time }
        if (fast != null && (System.currentTimeMillis() - fast.time) < MAX_STALE_MS) {
            return locationToJson(fast)
        }

        // Slow path: request a fresh single update; race providers.
        val loc = requestFreshFix(mgr, wanted, DEFAULT_TIMEOUT_MS)
            ?: throw RuntimeException(
                "No location fix within ${DEFAULT_TIMEOUT_MS / 1000}s. Is " +
                "Location enabled and is the device near a window or with " +
                "Wi-Fi/cell coverage?"
            )
        return locationToJson(loc)
    }

    private suspend fun requestFreshFix(
        mgr: LocationManager,
        providers: List<String>,
        timeoutMs: Long,
    ): Location? = suspendCancellableCoroutine { cont ->
        val listeners = mutableListOf<LocationListener>()
        val finished = java.util.concurrent.atomic.AtomicBoolean(false)

        fun detachAll() {
            for (l in listeners) try { mgr.removeUpdates(l) } catch (_: Exception) {}
            listeners.clear()
        }

        val timeoutJob = Thread {
            try { Thread.sleep(timeoutMs) } catch (_: InterruptedException) { return@Thread }
            if (finished.compareAndSet(false, true)) {
                detachAll()
                if (cont.isActive) cont.resume(null)
            }
        }.apply { isDaemon = true; start() }

        for (p in providers) {
            val listener = object : LocationListener {
                override fun onLocationChanged(location: Location) {
                    if (finished.compareAndSet(false, true)) {
                        timeoutJob.interrupt()
                        detachAll()
                        if (cont.isActive) cont.resume(location)
                    }
                }
                override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
                override fun onProviderEnabled(provider: String) {}
                override fun onProviderDisabled(provider: String) {}
            }
            try {
                mgr.requestLocationUpdates(p, 0L, 0f, listener, Looper.getMainLooper())
                listeners += listener
            } catch (_: SecurityException) { /* permission revoked mid-call */ }
            catch (_: IllegalArgumentException) { /* provider gone */ }
        }
        cont.invokeOnCancellation {
            timeoutJob.interrupt()
            detachAll()
        }
    }

    private fun locationToJson(loc: Location): JSONObject {
        val out = JSONObject()
        out.put("latitude",  loc.latitude)
        out.put("longitude", loc.longitude)
        out.put("accuracy",  loc.accuracy.toDouble())
        if (loc.hasAltitude()) out.put("altitude", loc.altitude)
        if (loc.hasSpeed())    out.put("speed",    loc.speed.toDouble())
        if (loc.hasBearing())  out.put("bearing",  loc.bearing.toDouble())
        out.put("provider", loc.provider ?: "unknown")
        out.put("time",     loc.time)
        return out
    }
}
