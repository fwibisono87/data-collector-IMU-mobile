package com.example.sensors_app.core

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import android.os.Handler
import android.os.Looper

/**
 * mDNS discovery of the backend's `_imu-telemetry._tcp` advertisement (service name
 * "IMU Backend", port 8000). Used ONLY while the Setup screen is visible: [start] acquires a
 * MulticastLock (without it a phone on power-saving Wi-Fi never hears the advertisements),
 * discovers, resolves each hit to an IP literal and reports the list on the main thread;
 * [stop] releases everything.
 *
 * Resolves strictly one service at a time — NsdManager's concurrent-resolve budget is small
 * and a second backend on the LAN is vanishingly rare; chaining is the documented safe
 * pattern. The UI connects to the RESOLVED IP, never the service name, so reconnects after
 * discovery stops never depend on mDNS.
 */
class Discovery(context: Context) {

    /** One resolved backend. [host] is an IP literal string, or null when resolution failed. */
    data class Found(val name: String, val host: String?, val port: Int)

    companion object {
        const val SERVICE_TYPE = "_imu-telemetry._tcp"
    }

    private val app = context.applicationContext
    private val nsd = app.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val wifi = app.getSystemService(Context.WIFI_SERVICE) as WifiManager
    private val main = Handler(Looper.getMainLooper())

    private var onChange: ((List<Found>) -> Unit)? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var running = false

    // Main-thread-only mutable state.
    private val found = LinkedHashMap<String, Found>() // service name → resolved
    private val unresolved = ArrayDeque<NsdServiceInfo>()
    private var resolving = false

    /** Begins discovery; [onChange] fires on the main thread after every change. */
    fun start(onChange: (List<Found>) -> Unit) {
        if (running) {
            this.onChange = onChange
            return
        }
        running = true
        this.onChange = onChange
        found.clear()
        unresolved.clear()
        resolving = false
        try {
            multicastLock = wifi.createMulticastLock("imu-discovery").also {
                it.setReferenceCounted(false)
                it.acquire()
            }
        } catch (_: Exception) {
            // No multicast lock (e.g. no Wi-Fi): unicast discoveries may still arrive.
        }
        val listener = object : NsdManager.DiscoveryListener {
            override fun onStartDiscoveryFailed(type: String, errorCode: Int) {
                // Nothing to retry with; the manual-IP field remains the fallback.
            }

            override fun onStopDiscoveryFailed(type: String, errorCode: Int) = Unit

            override fun onDiscoveryStarted(type: String) = Unit

            override fun onDiscoveryStopped(type: String) = Unit

            override fun onServiceFound(info: NsdServiceInfo) {
                main.post {
                    if (!running) return@post
                    unresolved.addLast(info)
                    resolveNext()
                }
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                main.post {
                    if (found.remove(info.serviceName) != null) notifyChange()
                }
            }
        }
        discoveryListener = listener
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener)
        } catch (_: Exception) {
            // An in-flight discovery from a previous screen instance: stop and stay silent;
            // the operator still has manual IP entry.
            discoveryListener = null
        }
    }

    /** Stops discovery and releases the multicast lock. Idempotent. */
    fun stop() {
        if (!running) return
        running = false
        onChange = null
        val listener = discoveryListener
        discoveryListener = null
        if (listener != null) {
            try {
                nsd.stopServiceDiscovery(listener)
            } catch (_: Exception) {
                // Throws when no discovery is active — nothing to stop.
            }
        }
        try {
            multicastLock?.takeIf { it.isHeld }?.release()
        } catch (_: Exception) {
            // Release is best-effort; a leaked lock only costs a little Wi-Fi power.
        }
        multicastLock = null
    }

    // ── main-thread resolution chain ─────────────────────────────────────────

    private fun resolveNext() {
        if (resolving) return
        val info = unresolved.removeFirstOrNull() ?: return
        resolving = true
        try {
            // The Executor-based resolveService and NsdServiceInfo.getHost() replacements are
            // API 34+; at minSdk 26 the deprecated pair is the only one that works everywhere
            // and remains fully functional.
            @Suppress("DEPRECATION")
            nsd.resolveService(info, object : NsdManager.ResolveListener {
                override fun onResolveFailed(failed: NsdServiceInfo, errorCode: Int) {
                    main.post {
                        resolving = false
                        resolveNext()
                    }
                }

                override fun onServiceResolved(resolved: NsdServiceInfo) {
                    main.post {
                        if (running) {
                            @Suppress("DEPRECATION")
                            val hostAddr = resolved.host?.hostAddress
                            found[resolved.serviceName] = Found(
                                name = resolved.serviceName,
                                host = hostAddr,
                                port = resolved.port,
                            )
                            notifyChange()
                        }
                        resolving = false
                        resolveNext()
                    }
                }
            })
        } catch (_: Exception) {
            resolving = false
            resolveNext()
        }
    }

    private fun notifyChange() {
        val cb = onChange ?: return
        val list = found.values.toList()
        cb(list)
    }
}
