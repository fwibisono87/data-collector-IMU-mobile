import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:uuid/uuid.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../models/sensor_packet.dart';
import '../models/proto/sensor_packet.pb.dart';
import '../models/proto/commands.pb.dart';
import 'clock_sync_service.dart';
import 'device_id_service.dart';
import 'fallback_buffer_manager.dart';
import 'session_persistence.dart';
import 'foreground_service_handler.dart';

enum WsState { disconnected, connecting, connected, offline }

// Manages telemetry + control WebSocket channels (CLAUDE.md §8).
class WebSocketClient {
  static final WebSocketClient _instance = WebSocketClient._internal();
  factory WebSocketClient() => _instance;
  WebSocketClient._internal();

  WebSocketChannel? _telemetry;
  WebSocketChannel? _control;
  StreamSubscription? _controlSub;
  StreamSubscription? _telemetrySub;
  StreamSubscription? _sensorSub;
  Timer? _pingTimer;
  Timer? _resyncTimer;

  WsState _state = WsState.disconnected;
  WsState get state => _state;
  bool get isConnected => _state == WsState.connected;

  String _serverIp = '';
  String _deviceId = '';
  String _deviceRole = 'chest';
  int _sequence = 0;
  int _packetsSent = 0;
  int _packetsBuffered = 0;
  DateTime? _lastPong;
  String? _activeSessionId;
  String? _lastConnectError;
  String? get lastConnectError => _lastConnectError;

  String? _serverState;          // authoritative backend session state, from PONG
  String? _serverLateSid;        // session still accepting late telemetry, or null
  DateTime? _lastStateAtMs;      // when we last heard authoritative state
  DateTime? _offlineSince;       // for the UI's "offline for 00:24" timer

  String? get serverState => _serverState;
  String? get serverLateSid => _serverLateSid;
  DateTime? get lastStateAt => _lastStateAtMs;
  DateTime? get offlineSince => _offlineSince;
  /// True when we believe we are recording but have not heard from the backend
  /// for >15 s — the UI must show this as "unconfirmed", never as a confident red.
  bool get isRecordingUnconfirmed =>
      _activeSessionId != null &&
      (_lastStateAtMs == null ||
       DateTime.now().difference(_lastStateAtMs!).inSeconds > 15);

  // Pending CLOCK_SYNC requests: commandId → t0Ms
  final Map<String, int> _pendingSyncs = {};
  final List<int> _syncOffsets = [];

  // Listeners for UI state updates.
  final _stateController = StreamController<WsState>.broadcast();
  final _eventController = StreamController<Map<String, dynamic>>.broadcast();

  Stream<WsState> get stateStream => _stateController.stream;
  Stream<Map<String, dynamic>> get eventStream => _eventController.stream;

  int get packetsSent => _packetsSent;
  int get packetsBuffered => _packetsBuffered;
  String? get activeSessionId => _activeSessionId;
  String get deviceRole => _deviceRole;

  // ── Connect ──────────────────────────────────────────────────────────────

  Future<bool> connect(String serverIp) async {
    if (_state == WsState.connecting || _state == WsState.connected) {
      return true;
    }
    _serverIp = serverIp;
    _lastConnectError = null;
    _setState(WsState.connecting);

    // Cancel any stale subscriptions from a previous (now-dead) connection so their
    // onDone/onError cannot fire against the new connection (Defect D).
    await _controlSub?.cancel();
    await _telemetrySub?.cancel();
    _controlSub = null;
    _telemetrySub = null;

    _deviceId = await DeviceIdService().getDeviceId();
    _deviceRole = await DeviceIdService().getDeviceRole();
    await DeviceIdService().saveServerIp(serverIp);

    try {
      _control = WebSocketChannel.connect(
        Uri.parse('ws://$serverIp:8000/ws/control'),
      );
      // Block until the control socket is actually open. Throws on failure instead of
      // optimistically reporting "connected" (Defect B). 6 s tolerates a slow handshake;
      // a dead network errors out well before that.
      await _control!.ready.timeout(const Duration(seconds: 6));

      _controlSub = _control!.stream.listen(
        (raw) => _handleControlMessage(raw),
        onDone: _onControlDisconnect,
        onError: (_) => _onControlDisconnect(),
      );

      // Send DeviceRegister only after the control socket is confirmed open.
      await _sendDeviceRegister();

      _telemetry = WebSocketChannel.connect(
        Uri.parse('ws://$serverIp:8000/ws/telemetry'),
      );
      await _telemetry!.ready.timeout(const Duration(seconds: 6));

      // Detect server-side drops on the telemetry channel. Stored so it can be cancelled
      // on the next reconnect (Defect D).
      _telemetrySub = _telemetry!.stream.listen(
        null,
        onDone: () { if (_state == WsState.connected) _onControlDisconnect(); },
        onError: (_) { if (_state == WsState.connected) _onControlDisconnect(); },
        cancelOnError: true,
      );

      _setState(WsState.connected);
      _offlineSince = null;
      // Reset the pong clock so the first ping-timer tick after (re)connect does not
      // immediately time out on a stale _lastPong (Defect A — the critical fix).
      _lastPong = DateTime.now();
      _startPingTimer();
      _startClockSync();
      // Start foreground service to keep process alive when screen is off.
      // Guard inside start() means repeated calls on reconnect are safe.
      await ForegroundServiceHandler().start();
      return true;
    } catch (e) {
      // Real failure (network still down, handshake timed out). Go offline and let the
      // reconnect loop retry — do NOT report success, so the buffer is never flushed/cleared
      // against a dead socket (Defect B).
      _lastConnectError = _describeConnectError(e);
      _setState(WsState.offline);
      _scheduleReconnect();
      return false;
    }
  }

  String _describeConnectError(Object e) {
    final s = e.toString().toLowerCase();
    if (s.contains('timeout')) return 'Timed out — laptop unreachable. Same Wi-Fi? Backend running? IP correct?';
    if (s.contains('refused')) return 'Connection refused — backend not started on :8000 at this IP.';
    if (s.contains('failed host lookup') || s.contains('no address')) return 'Bad IP address — re-check the number.';
    if (s.contains('network is unreachable')) return 'Phone not on the same network as the laptop.';
    return 'Could not connect. Check Wi-Fi, backend status, and the IP.';
  }

  // ── Attach sensor stream ─────────────────────────────────────────────────

  void attachSensorStream(Stream<SensorPacket> stream) {
    _sensorSub?.cancel();
    _sensorSub = stream.listen(_onSensorPacket);
  }

  void detachSensorStream() {
    _sensorSub?.cancel();
    _sensorSub = null;
  }

  // ── Sensor packet handling ───────────────────────────────────────────────

  void _onSensorPacket(SensorPacket raw) {
    final rawNow = DateTime.now().millisecondsSinceEpoch;
    final correctedNow = ClockSyncService().nowMs;
    final seq = _sequence++;

    final proto = SensorPacketProto(
      accX: raw.accX,
      accY: raw.accY,
      accZ: raw.accZ,
      gyroX: raw.gyroX,
      gyroY: raw.gyroY,
      gyroZ: raw.gyroZ,
      timestampMs: correctedNow,
      rawTimestampMs: rawNow,
      sequenceNumber: seq,
      deviceId: _deviceId,
      schemaVersion: 1,
    );

    final bytes = proto.toBytes();

    if (_state == WsState.connected && _telemetry != null) {
      _telemetry!.sink.add(bytes);
      _packetsSent++;

      // Persist sequence number every 500 packets.
      if (_activeSessionId != null && seq % 500 == 0) {
        _persistSequence(seq);
      }
    } else {
      // Network offline — buffer to disk.
      if (!FallbackBufferManager().isActive) {
        FallbackBufferManager().activate();
      }
      FallbackBufferManager().write(bytes);
      _packetsBuffered = FallbackBufferManager().bufferedCount;
      ForegroundServiceHandler().updateNotification(
        _packetsSent,
        _packetsBuffered,
      );
    }
  }

  // ── Control channel ──────────────────────────────────────────────────────

  Future<void> _handleControlMessage(dynamic raw) async {
    Uint8List bytes;
    if (raw is List<int>) {
      bytes = Uint8List.fromList(raw);
    } else if (raw is Uint8List) {
      bytes = raw;
    } else {
      return;
    }

    final cmd = CommandProto.fromBytes(bytes);
    switch (cmd.type) {
      case CommandType.PONG:
        _lastPong = DateTime.now();
        await _applyServerState(cmd.payload);
        _emitEvent({'type': 'pong'});

      case CommandType.CLOCK_SYNC:
        final t3Ms = DateTime.now().millisecondsSinceEpoch;
        final t0Ms = _pendingSyncs.remove(cmd.commandId);
        if (t0Ms == null) return;
        final parsed = ClockSyncService.parsePayload(cmd.payload);
        if (parsed == null) return;
        final offset = ClockSyncService().processResponse(
          t0Ms: t0Ms,
          t1Ms: parsed['t1_ms']!,
          t2Ms: parsed['t2_ms']!,
          t3Ms: t3Ms,
        );
        if (offset != null) {
          _syncOffsets.add(offset);
          if (_syncOffsets.length >= 5) {
            ClockSyncService().applyOffsets(_syncOffsets);
            _syncOffsets.clear();
            _emitEvent({
              'type': 'clock_synced',
              'offset_ms': ClockSyncService().clockOffsetMs,
              'rtt_ms': ClockSyncService().lastRttMs,
            });
          }
        }

      case CommandType.START_SESSION:
        try {
          final payload = jsonDecode(cmd.payload) as Map<String, dynamic>;
          _activeSessionId = payload['session_id']?.toString();
          _sequence = 0;   // Reset sequence counter for new session

          // Coordinated start: wait until scheduled_start_ms (CLAUDE.md §22.5)
          final scheduledStartMs = payload['scheduled_start_ms'] as int?;
          if (scheduledStartMs != null) {
            final nowMs = ClockSyncService().nowMs;
            final delayMs = scheduledStartMs - nowMs;
            if (delayMs > 0) {
              await Future.delayed(Duration(milliseconds: delayMs));
            }
          }
        } catch (_) {}
        _emitEvent({'type': 'start_session', 'payload': cmd.payload});
        ForegroundServiceHandler().updateNotification(_packetsSent, 0);

      case CommandType.STOP_SESSION:
        _activeSessionId = null;
        _emitEvent({'type': 'stop_session'});
        SessionPersistence().clear();

      case CommandType.SET_LABEL:
        _emitEvent({'type': 'set_label', 'payload': cmd.payload});

      case CommandType.ACK:
        _emitEvent({'type': 'ack', 'command_id': cmd.commandId});

      case CommandType.ERROR_ALERT:
        _emitEvent({'type': 'error_alert', 'payload': cmd.payload});
    }
  }

  // The backend is the single source of truth for session state. It rides on the 1 Hz
  // PONG heartbeat and on one unsolicited PONG right after registration, so a phone that
  // missed a START or a STOP while offline is corrected within ~1 s of reconnecting
  // instead of staying wrong forever (plan D1).
  Future<void> _applyServerState(String payload) async {
    if (payload.isEmpty) return;            // old backend → no information, keep today's behaviour
    Map<String, dynamic> p;
    try {
      p = jsonDecode(payload) as Map<String, dynamic>;
    } catch (_) {
      return;
    }
    final state = p['state']?.toString();
    if (state == null || state.isEmpty) return;

    String? nonEmpty(Object? v) {
      final s = v?.toString() ?? '';
      return s.isEmpty ? null : s;
    }

    _serverState = state;
    _serverLateSid = nonEmpty(p['late_sid']);
    _lastStateAtMs = DateTime.now();

    final sid = nonEmpty(p['session_id']);
    final serverRecording = state == 'RECORDING';

    if (!serverRecording && _activeSessionId != null) {
      // We think we are recording; the backend is not. We missed the STOP.
      final ended = _activeSessionId!;
      _activeSessionId = null;
      SessionPersistence().clear();
      _emitEvent({'type': 'stop_session', 'reason': 'state_resync', 'session_id': ended});
    } else if (serverRecording && sid != null && _activeSessionId != sid) {
      // A session is running that we are not part of — we missed the START, or a new
      // session began while we were dark. Adopt it and start a fresh dedup namespace.
      _activeSessionId = sid;
      _sequence = 0;
      _emitEvent({'type': 'start_session', 'reason': 'state_resync', 'session_id': sid});
    }
  }

  /// Wait briefly for the first authoritative state after (re)connect.
  Future<bool> _waitForServerState(Duration timeout) async {
    final deadline = DateTime.now().add(timeout);
    while (_serverState == null && DateTime.now().isBefore(deadline)) {
      await Future.delayed(const Duration(milliseconds: 100));
      if (_state != WsState.connected) return false;
    }
    return _serverState != null;
  }

  void _onControlDisconnect() {
    // Only a live, connected channel can trigger a drop→reconnect. If we are already
    // disconnected (explicit), offline (reconnect pending), or connecting, ignore the
    // duplicate signal so reconnect attempts never stack (Defect C).
    if (_state != WsState.connected) return;
    _setState(WsState.offline);
    _serverState = null;      // never gate a buffer flush on a stale "RECORDING"
    _serverLateSid = null;
    _offlineSince = DateTime.now();
    _pingTimer?.cancel();
    _resyncTimer?.cancel();
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    Future.delayed(const Duration(seconds: 3), () async {
      if (_state != WsState.offline) return;
      final ok = await connect(_serverIp);
      if (ok && FallbackBufferManager().isActive) {
        await _flushFallbackBuffer();
      }
    });
  }

  Future<void> _flushFallbackBuffer() async {
    if (!FallbackBufferManager().isActive) return;
    bool completed = true;
    await for (final bytes in FallbackBufferManager().flushStream()) {
      if (_state != WsState.connected) { completed = false; break; }
      _telemetry?.sink.add(bytes);
    }
    if (completed && _state == WsState.connected) {
      await FallbackBufferManager().clearAfterFlush();
      _packetsBuffered = 0;
    }
    // If the socket dropped mid-flush, leave the buffer intact; the next reconnect
    // re-flushes it. Backend dedup (device_id, session_id, sequence_number) makes the
    // re-send idempotent, so no duplicate rows are written.
  }

  // ── Commands ─────────────────────────────────────────────────────────────

  Future<void> sendCommand(CommandProto cmd) async {
    if (_state != WsState.connected) return;
    _control?.sink.add(cmd.toBytes());
  }

  Future<void> _sendDeviceRegister() async {
    final proto = DeviceRegisterProto(
      deviceId: _deviceId,
      deviceRole: _deviceRole,
      deviceModel: 'Android',
      androidVersion: '',
      appVersion: '2.0.0',
      schemaVersion: 1,
    );
    _control?.sink.add(proto.toBytes());
  }

  // Seconds without a PONG before declaring the control channel offline.
  // 8 s tolerates brief Wi-Fi degradation during subject motion (falls, rapid
  // walking) without triggering a spurious reconnect cycle. A genuine dropout
  // (phone dead, strap removed) is still detected within this window so the
  // backend integrity report can flag the exact offline interval.
  static const int _pongTimeoutSec = 8;

  void _startPingTimer() {
    _pingTimer?.cancel();
    _lastPong = DateTime.now();   // fresh grace window each time the timer (re)starts
    _pingTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      sendCommand(CommandProto(
        type: CommandType.PING,
        issuedAtMs: DateTime.now().millisecondsSinceEpoch,
      ));
      if (_lastPong != null &&
          DateTime.now().difference(_lastPong!).inSeconds > _pongTimeoutSec) {
        _onControlDisconnect();
      }
    });
  }

  void _startClockSync() {
    _syncOffsets.clear();
    // Send 5 syncs with 200ms gap, then repeat every 5 minutes.
    _doSyncBurst();
    _resyncTimer = Timer.periodic(const Duration(minutes: 5), (_) {
      _syncOffsets.clear();
      _doSyncBurst();
    });
  }

  void _doSyncBurst() {
    for (int i = 0; i < 5; i++) {
      Future.delayed(Duration(milliseconds: i * 200), () {
        if (_state != WsState.connected) return;
        final t0Ms = DateTime.now().millisecondsSinceEpoch;
        final id = const Uuid().v4();
        _pendingSyncs[id] = t0Ms;
        sendCommand(CommandProto(
          type: CommandType.CLOCK_SYNC,
          payload: ClockSyncService.buildPayload(t0Ms),
          issuedAtMs: t0Ms,
          commandId: id,
        ));
      });
    }
  }

  Future<void> _persistSequence(int seq) async {
    if (_activeSessionId == null) return;
    await SessionPersistence().save(
      sessionId: _activeSessionId!,
      deviceId: _deviceId,
      serverIp: _serverIp,
      clockOffsetMs: ClockSyncService().clockOffsetMs,
      lastSequenceNumber: seq,
      deviceRole: _deviceRole,
    );
  }

  // ── Disconnect ───────────────────────────────────────────────────────────

  Future<void> disconnect() async {
    _pingTimer?.cancel();
    _resyncTimer?.cancel();
    _sensorSub?.cancel();
    _controlSub?.cancel();
    await _control?.sink.close();
    await _telemetry?.sink.close();
    _setState(WsState.disconnected);
    await FallbackBufferManager().deactivate();
    // Stop foreground service only on explicit disconnect, not on temporary drops.
    await ForegroundServiceHandler().stop();
  }

  void _setState(WsState s) {
    _state = s;
    _stateController.add(s);
  }

  void _emitEvent(Map<String, dynamic> e) => _eventController.add(e);
}
