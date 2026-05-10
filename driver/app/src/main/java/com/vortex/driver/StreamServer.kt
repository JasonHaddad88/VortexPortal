package com.vortex.driver

import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.io.IOException
import java.net.BindException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer

/**
 * TCP server on 127.0.0.1:[port]. The Termux Python agent connects, reads
 * a length-prefixed stream of JPEG frames, and forwards them over the
 * existing WebSocket to the hub.
 *
 * Why loopback only: the APK never accepts connections from the network.
 * The only thing on the device that can reach 127.0.0.1 from outside the
 * APK's process is another local app -- Termux in our case.
 *
 * One client at a time. A new connection boots the previous client. When
 * a client connects, [onClientConnected] fires (which the service uses
 * to start the camera). When it disconnects, [onClientDisconnected]
 * fires (service stops the camera to save battery).
 *
 * Wire format:
 *   per frame: [u32 BE length][JPEG bytes]
 *   no handshake; the client just reads until socket close.
 */
class StreamServer(
    private val port: Int,
    private val onClientConnected: () -> Unit,
    private val onClientDisconnected: () -> Unit,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private var serverSocket: ServerSocket? = null
    @Volatile private var currentClient: Socket? = null
    @Volatile private var stopped = false
    private var acceptJob: Job? = null

    val isClientConnected: Boolean get() = currentClient != null

    /**
     * Push a single frame to the connected client. No-op if no client.
     * The header + JPEG bytes are sent under a per-socket lock so two
     * concurrent push calls never interleave.
     */
    fun pushFrame(jpeg: ByteArray) {
        val client = currentClient ?: return
        try {
            val header = ByteBuffer.allocate(4).putInt(jpeg.size).array()
            val out = client.getOutputStream()
            synchronized(client) {
                out.write(header)
                out.write(jpeg)
                out.flush()
            }
        } catch (_: IOException) {
            disconnectClient()
        } catch (_: Exception) {
            disconnectClient()
        }
    }

    fun start() {
        acceptJob = scope.launch {
            val sock: ServerSocket = try {
                ServerSocket(port, /* backlog = */ 1, InetAddress.getByName("127.0.0.1"))
            } catch (e: BindException) {
                Log.e(TAG, "Port $port already in use: $e")
                return@launch
            } catch (e: Exception) {
                Log.e(TAG, "ServerSocket failed: $e")
                return@launch
            }
            serverSocket = sock
            Log.i(TAG, "Listening on 127.0.0.1:$port")

            while (isActive && !stopped) {
                val newClient = try {
                    sock.accept()
                } catch (e: Exception) {
                    if (!stopped) Log.w(TAG, "accept failed: $e")
                    break
                }
                newClient.tcpNoDelay = true
                // Boot any existing client.
                currentClient?.let {
                    Log.i(TAG, "Superseding previous client")
                    try { it.close() } catch (_: Exception) {}
                }
                currentClient = newClient
                Log.i(TAG, "Client connected: ${newClient.inetAddress}")
                onClientConnected()
                // Watch this client for disconnect on a separate coroutine
                // so we can also accept the next connection meanwhile.
                launch { watchForDisconnect(newClient) }
            }
        }
    }

    private suspend fun watchForDisconnect(client: Socket) {
        try {
            val ins = client.getInputStream()
            // Block on read; client never sends anything, so any return
            // (-1 on close, or an EOFException) means they hung up.
            while (isActive && client === currentClient) {
                val r = ins.read()
                if (r == -1) break
            }
        } catch (_: Exception) {
            // socket closed mid-read; treat as disconnect
        } finally {
            if (client === currentClient) {
                currentClient = null
                try { client.close() } catch (_: Exception) {}
                onClientDisconnected()
                Log.i(TAG, "Client disconnected")
            }
        }
    }

    private fun disconnectClient() {
        val c = currentClient ?: return
        currentClient = null
        try { c.close() } catch (_: Exception) {}
        onClientDisconnected()
    }

    fun stop() {
        stopped = true
        try { currentClient?.close() } catch (_: Exception) {}
        try { serverSocket?.close() } catch (_: Exception) {}
        scope.cancel()
    }

    companion object { private const val TAG = "StreamServer" }
}
