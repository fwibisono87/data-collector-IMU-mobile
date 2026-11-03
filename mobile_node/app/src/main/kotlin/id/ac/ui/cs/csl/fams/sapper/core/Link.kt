package id.ac.ui.cs.csl.fams.sapper.core

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString

/**
 * The two OkHttp WebSockets (control + telemetry) to `ws://<ip>:8000`, reporting every
 * lifecycle event into the pipeline [android.os.Handler] so nothing here runs state
 * decisions concurrently.
 *
 * Order is a contract: the control socket opens FIRST, [DeviceRegister] is its first binary
 * frame (the backend closes with 4001 otherwise), and only then does the telemetry socket
 * open. The link is "up" only when both are open — that is the pipeline's job to decide; this
 * class just reports opens/closes.
 */
class Link(
    private val client: OkHttpClient,
    private val post: (Runnable) -> Unit,
    private val listener: Listener,
) {
    interface Listener {
        fun onControlOpen()
        fun onControlFrame(bytes: ByteArray)
        fun onTelemetryOpen()
        /** Close code 4001/4002 carry registration/role meaning; null code = network failure. */
        fun onClosed(code: Int?, failure: String?)
    }

    @Volatile
    private var control: WebSocket? = null

    @Volatile
    private var telemetry: WebSocket? = null

    /** Generation counter: callbacks from a socket that was already replaced are ignored, so
     *  a dead connection's onClose cannot fire against the new one (old Defect D). */
    @Volatile
    private var generation = 0

    fun connect(ip: String, register: DeviceRegister) {
        val gen = ++generation
        control?.let { runCatching { it.close(1000, null) } }
        telemetry?.let { runCatching { it.close(1000, null) } }
        control = null
        telemetry = null

        val controlReq = Request.Builder().url("ws://$ip:8000/ws/control").build()
        control = client.newWebSocket(controlReq, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (gen != generation) return
                // FIRST binary frame on /ws/control, before anything else is sent.
                webSocket.send(register.encode().toByteString())
                post { if (gen == generation) listener.onControlOpen() }
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (gen != generation) return
                post { if (gen == generation) listener.onControlFrame(bytes.toByteArray()) }
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                // The protocol is binary-only; ignore stray text frames.
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (gen != generation) return
                post {
                    if (gen == generation) {
                        listener.onClosed(response?.code, t.message ?: t.javaClass.simpleName)
                    }
                }
            }

            // The server closing (e.g. 4002 role collision) arrives here first; OkHttp only
            // calls onClosed after we close our side, so without this the link hangs half-open.
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                runCatching { webSocket.close(code, null) }
                if (gen != generation) return
                post { if (gen == generation) listener.onClosed(code, reason.ifEmpty { null }) }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (gen != generation) return
                post { if (gen == generation) listener.onClosed(code, reason.ifEmpty { null }) }
            }
        })

        // Open telemetry immediately too; "connected" only counts once BOTH report open,
        // which the pipeline tracks. OkHttp queues the handshake on its own dispatcher.
        val telemetryReq = Request.Builder().url("ws://$ip:8000/ws/telemetry").build()
        telemetry = client.newWebSocket(telemetryReq, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (gen != generation) return
                post { if (gen == generation) listener.onTelemetryOpen() }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (gen != generation) return
                post {
                    if (gen == generation) {
                        listener.onClosed(response?.code, t.message ?: t.javaClass.simpleName)
                    }
                }
            }

            // The server closing (e.g. 4002 role collision) arrives here first; OkHttp only
            // calls onClosed after we close our side, so without this the link hangs half-open.
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                runCatching { webSocket.close(code, null) }
                if (gen != generation) return
                post { if (gen == generation) listener.onClosed(code, reason.ifEmpty { null }) }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (gen != generation) return
                post { if (gen == generation) listener.onClosed(code, reason.ifEmpty { null }) }
            }
        })
    }

    /** Sends on the control socket. Returns false when it is not open (OkHttp queues, but a
     *  closed socket's send is refused — the caller treats that as a drop). */
    fun sendControl(bytes: ByteArray): Boolean = control?.send(bytes.toByteString()) ?: false

    fun sendTelemetry(bytes: ByteArray): Boolean = telemetry?.send(bytes.toByteString()) ?: false

    fun close() {
        generation++ // ignore every callback from the sockets being torn down
        control?.let { runCatching { it.close(1000, null) } }
        telemetry?.let { runCatching { it.close(1000, null) } }
        control = null
        telemetry = null
    }
}
