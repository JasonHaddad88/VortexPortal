package com.vortex.driver

import android.util.Log
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Enumerates IPv4 addresses other devices on the same LAN could reach the
 * APK's [DirectServer] on. We push these in `direct_info` so the hub can
 * hand the browser a host:port candidate list -- the browser tries them
 * in order and falls back to the hub relay if none work.
 *
 * What gets included: Wi-Fi addresses (10.x, 172.16-31.x, 192.168.x) and
 * any other non-loopback, non-link-local IPv4 on a non-virtual interface.
 *
 * What gets excluded:
 * - loopback (127.0.0.1) -- the agent's notion of localhost is not the
 *   browser's, so handing this out poisons the cache (V5.17 fix)
 * - link-local (169.254.x.x) -- only useful for direct cable to host
 * - IPv6 -- the browser side hasn't been audited for it; LAN reach is
 *   the goal and v4 is enough
 * - virtual interfaces (Docker, VPN tunnels) -- often non-routable from
 *   the user's actual subnet
 */
object DeviceHosts {

    fun reachableIps(): List<String> {
        val out = LinkedHashSet<String>()
        try {
            val ifaces = NetworkInterface.getNetworkInterfaces() ?: return emptyList()
            for (intf in ifaces) {
                try {
                    if (!intf.isUp || intf.isLoopback || intf.isVirtual) continue
                    val name = intf.name?.lowercase() ?: ""
                    // Skip obvious tunnel/virtual names that some Android
                    // distros expose (e.g. clatd-* for IPv4-over-v6).
                    if (name.startsWith("tun") || name.startsWith("ppp") ||
                        name.startsWith("dummy") || name.startsWith("clat") ||
                        name.startsWith("ip6tnl") || name.startsWith("sit")) continue
                    for (addr in intf.inetAddresses) {
                        if (addr is Inet4Address &&
                            !addr.isLoopbackAddress &&
                            !addr.isLinkLocalAddress &&
                            !addr.isMulticastAddress &&
                            !addr.isAnyLocalAddress
                        ) {
                            addr.hostAddress?.let { out += it }
                        }
                    }
                } catch (_: Exception) { /* per-iface best-effort */ }
            }
        } catch (e: Exception) {
            Log.w(TAG, "reachableIps failed: ${e.javaClass.simpleName}: ${e.message}")
        }
        return out.toList()
    }

    private const val TAG = "DeviceHosts"
}
