import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';
import 'package:uuid/uuid.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'conn_debug.dart';
import '../models/sensor_packet.dart';
import '../models/proto/sensor_packet.pb.dart';
import '../models/proto/commands.pb.dart';
import 'alert_service.dart';
import 'clock_sync_service.dart';
import 'device_id_service.dart';
import 'internal_sensor_manager.dart';
import 'fallback_buffer_manager.dart';
import 'local_session_recorder.dart';
import 'recovery_uploader.dart';
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
  Timer? _reconnectTimer;
  Timer? _telemetryWatchdog;

  WsState _state = WsState.disconnected;
  WsState get state => _state;
  bool get isConnected => _state == WsState.connected;

  String _serverIp = '';
  String _deviceId = '';
  String _deviceRole = 'chest';
  int _sequence = 0;
  int _packetsSent = 0;
  int _sessionPacketsSentBaseline = 0;
  int _packetsBuffered = 0;
  int _flushCounter = 0;
  DateTime? _lastPong;
  String? _activeSessionId;
  String? _lastConnectError;
  String? get lastConnectError => _lastConnectError;
  String get serverIp => _serverIp;

  // Last label this phone was told about, applied to LocalSessionRecorder rows. May be
  // stale if the phone was offline when the operator changed it — the backend CSV is
  // authoritative for labels; this is the local rescue copy's best effort.
  int _activeLabelId = 0;
  String _activeLabelName = '0';

  // Session metadata from the latest START_SESSION, used to name & header the phone-local
  // rescue CSV (e.g. "<subject>_<tag>_<role>_<deviceId>_<epoch>.csv"). Unknown for a
  // resume/resync adoption, in which case the recorder falls back to session_id alone.
  String _sessionSubject = '';
  String _sessionTag = '';
  String _sessionOperator = '';

  String? _serverState; // authoritative backend session state, from PONG
  String? _serverLateSid; // session still accepting late telemetry, or null
  DateTime? _lastStateAtMs; // when we last heard authoritative state
  DateTime? _offlineSince; // for the UI's "offline for 00:24" timer
  DateTime? _lostAtMs; // when the last connection gap began (sidecar)
  DateTime? _lastTelemetryProgressAt;
  int? _backendTelemetryPackets;
  int? _backendTelemetryAgeMs;

  // Keep this aligned with pubspec.yaml. It is sent to the backend so operators can see
  // whether a phone is running the build that was actually tested.
  // Bumped for the session-lifecycle work: rescue upload on state-resync stop, upload
  // timeouts, prune that never reclaims un-uploaded data, honest resumed row counts.
  // The dashboard surfaces this per device — without a bump, a phone carrying these
  // fixes is indistinguishable from one that does not, which is the exact failure the
  // device card exists to catch.
  static const String _reportedAppVersion = '2.3.0';
  static const Duration _telemetryStaleAfter = Duration(seconds: 12);

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
      ConnDebug.log('connect($serverIp) early-return: state=$_state');
      return true;
    }
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _serverIp = serverIp;
    _lastConnectError = null;
    RecoveryUploader().configure(_serverIp);
    _setState(WsState.connecting);
    ConnDebug.log('connect begin -> $serverIp, state=connecting');

    try {
      // Cancel stale subscriptions AND close the old channels before creating
      // replacements: a dead connection's onDone/onError must not fire against the new
      // one (Defect D), and a half-open telemetry socket left alive lets sink.add keep
      // accepting bytes locally that no server will ever read.
      await _disposeChannels();

      _deviceId = await DeviceIdService().getDeviceId();
      _deviceRole = await DeviceIdService().getDeviceRole();
      await DeviceIdService().saveServerIp(serverIp);
      // Persist the desired endpoint so a restarted foreground-task engine can
      // reconnect without the UI re-issuing a connect command.
      await SessionPersistence()
          .saveDesired(serverIp: serverIp, deviceRole: _deviceRole);
      await _restoreSequenceIfInterrupted();
    } catch (e) {
      _lastConnectError = _describeConnectError(e);
      ConnDebug.log('connect preparation failed -> $serverIp: $e');
      _setState(WsState.offline);
      _scheduleReconnect();
      return false;
    }

    try {
      _control = WebSocketChannel.connect(
        Uri.parse('ws://$serverIp:8000/ws/control'),
      );
      // Block until the control socket is actually open. Throws on failure instead of
      // optimistically reporting "connected" (Defect B). 6 s tolerates a slow handshake;
      // a dead network errors out well before that.
      await _control!.ready.timeout(const Duration(seconds: 6));
      ConnDebug.log('control socket open (ws/control)');

      _controlSub = _control!.stream.listen(
        _handleControlMessageSafe,
        onDone: _onControlDisconnect,
        onError: (_) => _onControlDisconnect(),
      );

      // Send DeviceRegister only after the control socket is confirmed open.
      await _sendDeviceRegister();
      ConnDebug.log('DeviceRegister sent -> $_deviceId');

      _telemetry = WebSocketChannel.connect(
        Uri.parse('ws://$serverIp:8000/ws/telemetry'),
      );
      await _telemetry!.ready.timeout(const Duration(seconds: 6));
      ConnDebug.log('telemetry socket open (ws/telemetry)');

      // Detect server-side drops on the telemetry channel. Stored so it can be cancelled
      // on the next reconnect (Defect D).
      _telemetrySub = _telemetry!.stream.listen(
        null,
        onDone: () {
          if (_state == WsState.connected) _onControlDisconnect();
        },
        onError: (_) {
          if (_state == WsState.connected) _onControlDisconnect();
        },
        cancelOnError: true,
      );

      _setState(WsState.connected);
      _offlineSince = null;
      // Reset the pong clock so the first ping-timer tick after (re)connect does not
      // immediately time out on a stale _lastPong (Defect A — the critical fix).
      _lastPong = DateTime.now();
      _backendTelemetryPackets = null;
      _backendTelemetryAgeMs = null;
      _lastTelemetryProgressAt = DateTime.now();
      _startPingTimer();
      _startClockSync();
      _startTelemetryWatchdog();
      // Start foreground service to keep process alive when screen is off.
      // Guard inside start() means repeated calls on reconnect are safe.
      await ForegroundServiceHandler().start();
      // Reconcile against the backend, then decide what to do with any buffered bytes.
      // This must run for EVERY connect path (first connect, reconnect, resume-after-kill),
      // not just the reconnect timer (plan R3).
      unawaited(_afterConnectReconcile());
      ConnDebug.log('connect OK -> $serverIp');
      return true;
    } catch (e) {
      // Real failure (network still down, handshake timed out). Go offline and let the
      // reconnect loop retry — do NOT report success, so the buffer is never flushed/cleared
      // against a dead socket (Defect B).
      _lastConnectError = _describeConnectError(e);
      ConnDebug.log('connect FAILED -> $serverIp: $_lastConnectError | raw=$e');
      await _disposeChannels();
      _setState(WsState.offline);
      _scheduleReconnect();
      return false;
    }
  }

  String _describeConnectError(Object e) {
    final s = e.toString().toLowerCase();
    if (s.contains('timeout')) {
      return 'Timed out — laptop unreachable. Same Wi-Fi? Backend running? IP correct?';
    }
    if (s.contains('refused')) {
      return 'Connection refused — backend not started on :8000 at this IP.';
    }
    if (s.contains('failed host lookup') || s.contains('no address')) {
      return 'Bad IP address — re-check the number.';
    }
    if (s.contains('network is unreachable')) {
      return 'Phone not on the same network as the laptop.';
    }
    return 'Could not connect. Check Wi-Fi, backend status, and the IP.';
  }

  Future<void> _disposeChannels() async {
    await _controlSub?.cancel();
    await _telemetrySub?.cancel();
    _controlSub = null;
    _telemetrySub = null;
    for (final channel in <WebSocketChannel?>[_control, _telemetry]) {
      try {
        await channel?.sink.close().timeout(const Duration(seconds: 1));
      } catch (_) {
        // A dead socket is already in the desired state.
      }
    }
    _control = null;
    _telemetry = null;
  }

  void _startTelemetryWatchdog() {
    _telemetryWatchdog?.cancel();
    _telemetryWatchdog = Timer.periodic(const Duration(seconds: 1), (_) {
      if (_state != WsState.connected || _activeSessionId == null) return;
      if (_packetsSent <= _sessionPacketsSentBaseline) return;

      final lastProgress = _lastTelemetryProgressAt;
      final backendAge = _backendTelemetryAgeMs;
      final staleByAge = backendAge != null &&
          backendAge >= _telemetryStaleAfter.inMilliseconds;
      final staleByCounter = lastProgress != null &&
          DateTime.now().difference(lastProgress) >= _telemetryStaleAfter;
      if (!staleByAge && !staleByCounter) return;

      ConnDebug.log(
        'telemetry stalled; reconnecting '
        '(backend_age_ms=$backendAge, backend_packets=$_backendTelemetryPackets)',
      );
      LocalSessionRecorder().logEvent({
        'type': 'telemetry_stale',
        'telemetry_age_ms': backendAge,
        'telemetry_packets': _backendTelemetryPackets,
        'time_ms': DateTime.now().millisecondsSinceEpoch,
      });
      _emitEvent(
          {'type': 'telemetry_reconnect', 'telemetry_age_ms': backendAge});
      _onControlDisconnect();
    });
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

  void _bufferPacket(Uint8List bytes) {
    if (_activeSessionId == null) return;
    final buf = FallbackBufferManager();
    if (!buf.isActive) {
      // activate() marks the manager active before its asynchronous file setup,
      // so packets arriving during that setup remain in the in-memory queue.
      unawaited(buf.activate(sessionId: _activeSessionId).catchError((error) {
        ConnDebug.log('fallback buffer activation failed: $error');
      }));
    }
    buf.enqueue(bytes);
    _packetsBuffered = buf.bufferedCount;
    try {
      ForegroundServiceHandler()
          .updateNotification(_packetsSent, _packetsBuffered);
    } catch (error) {
      ConnDebug.log('buffer notification failed: $error');
    }
  }

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
      schemaVersion: 2,
      accTsMs: _hwTsToCorrectedMs(raw.accTs, correctedNow),
      gyroTsMs: _hwTsToCorrectedMs(raw.gyroTs, correctedNow),
      sampleKind: raw.isHeld ? 1 : 0,
    );

    final bytes = proto.toBytes();

    // Local guarantee: this write happens whether or not the network exists (plan T12).
    LocalSessionRecorder().write(raw,
        timestampMs: correctedNow,
        sequence: seq,
        deviceId: _deviceId,
        labelId: _activeLabelId,
        labelName: _activeLabelName,
        accTsMs: _hwTsToCorrectedMs(raw.accTs, correctedNow),
        gyroTsMs: _hwTsToCorrectedMs(raw.gyroTs, correctedNow),
        sampleKind: raw.isHeld ? 1 : 0);

    // Persist the sequence counter on BOTH the online and offline paths. The checkpoint is
    // deliberately fire-and-forget so the sensor callback never waits on disk; the
    // persistence service serializes these writes. A restart resumes at the checkpoint's
    // next sequence and relies on backend/local deduplication for the small uncertain tail.
    if (_activeSessionId != null && seq % 250 == 0) {
      unawaited(_persistSequence(seq));
    }

    if (_state == WsState.connected && _telemetry != null) {
      try {
        _telemetry!.sink.add(bytes);
        _packetsSent++;
      } catch (error) {
        // A socket can close between the state check and sink.add(). Never let
        // that synchronous exception terminate the sensor stream; preserve the
        // packet locally and enter the normal reconnect path.
        ConnDebug.log('telemetry write failed: $error');
        _onControlDisconnect();
        _bufferPacket(bytes);
      }
    } else {
      // Only buffer while a session is actually running. Buffering during idle offline
      // periods filled storage with data nobody asked for; and _activeSessionId is
      // exactly the tag we need to prove, later, which session these bytes belong to
      // (plan T10).
      _bufferPacket(bytes);
    }
  }

  /// Convert a hardware sensor event time into the same corrected-epoch domain as
  /// timestampMs. Returns 0 when the value is missing or clearly not epoch-based
  /// (some platforms report uptime), so downstream consumers can treat it as unknown
  /// rather than trusting a wrong number.
  int _hwTsToCorrectedMs(DateTime? ts, int correctedNow) {
    if (ts == null) return 0;
    final candidate =
        ts.millisecondsSinceEpoch + ClockSyncService().clockOffsetMs;
    if ((candidate - correctedNow).abs() > 3600000) {
      return 0; // > 1h off => not epoch-based
    }
    return candidate;
  }

  // ── Control channel ──────────────────────────────────────────────────────

  void _handleControlMessageSafe(dynamic raw) {
    unawaited(() async {
      try {
        await _handleControlMessage(raw);
      } catch (error) {
        // Stream listeners do not await an async callback. Contain unexpected
        // command/storage failures so one malformed command cannot kill control
        // processing or the acquisition isolate.
        ConnDebug.log('control message failed: $error');
      }
    }());
  }

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
          final sid = payload['session_id']?.toString();
          _sessionSubject = payload['subject']?.toString() ?? _sessionSubject;
          _sessionTag = payload['session_tag']?.toString() ?? _sessionTag;
          _sessionOperator =
              payload['operator']?.toString() ?? _sessionOperator;
          _sequence = 0; // Reset sequence counter for new session
          await _setActiveSession(sid);

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
        final ended = _activeSessionId;
        await _setActiveSession(null);
        _emitEvent({'type': 'stop_session'});
        SessionPersistence().clear();
        if (ended != null) {
          unawaited(RecoveryUploader().uploadPending(onlySessionId: ended));
        }

      case CommandType.SET_LABEL:
        try {
          final payload = jsonDecode(cmd.payload) as Map<String, dynamic>;
          _activeLabelId =
              int.tryParse(payload['label_id'].toString()) ?? _activeLabelId;
          _activeLabelName =
              payload['label_name']?.toString() ?? _activeLabelId.toString();
        } catch (_) {}
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
    if (payload.isEmpty) {
      return; // old backend → no information, keep today's behaviour
    }
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

    final telemetryPackets = p['telemetry_packets'];
    if (telemetryPackets is num) {
      final next = telemetryPackets.toInt();
      if (_backendTelemetryPackets == null ||
          next != _backendTelemetryPackets) {
        _lastTelemetryProgressAt = DateTime.now();
      }
      _backendTelemetryPackets = next;
    }
    final telemetryAge = p['telemetry_age_ms'];
    if (telemetryAge is num) {
      _backendTelemetryAgeMs = telemetryAge.toInt();
    }

    final sid = nonEmpty(p['session_id']);
    final serverRecording = state == 'RECORDING';

    if (!serverRecording && _activeSessionId != null) {
      // We think we are recording; the backend is not. We missed the STOP.
      final ended = _activeSessionId!;
      await _setActiveSession(null);
      SessionPersistence().clear();
      _emitEvent({
        'type': 'stop_session',
        'reason': 'state_resync',
        'session_id': ended
      });
      // Offer the rescue CSV exactly as the explicit STOP_SESSION path does. Without
      // this, a phone that learned the session ended from the heartbeat — which is what
      // happens whenever the backend restarts and finalizes the session at startup — sat
      // on its local copy indefinitely: it is still *connected*, so no reconnect is
      // coming to trigger _afterConnectReconcile, and nothing else offers the file.
      unawaited(RecoveryUploader().uploadPending(onlySessionId: ended));
    } else if (serverRecording && sid != null && _activeSessionId != sid) {
      // A session is running that we are not part of — we missed the START, or a new
      // session began while we were dark. Adopt it and start a fresh dedup namespace.
      _sequence = 0;
      await _setActiveSession(sid);
      _emitEvent({
        'type': 'start_session',
        'reason': 'state_resync',
        'session_id': sid
      });
    }
  }

  /// Single choke point for every place _activeSessionId changes, so the phone-local
  /// recorder (the data guarantee — plan T12) is always opened/closed in lockstep with
  /// the session the phone believes is active, whether that belief came from a direct
  /// START/STOP_SESSION push or from a PONG state resync.
  Future<void> _setActiveSession(String? sid) async {
    if (_activeSessionId == sid) return;
    _activeSessionId = sid;
    _sessionPacketsSentBaseline = _packetsSent;
    _backendTelemetryPackets = null;
    _backendTelemetryAgeMs = null;
    _lastTelemetryProgressAt = DateTime.now();
    if (sid == null) {
      // Stop accepting sensor samples before closing the recorder so the file
      // drains on a clean boundary (task-engine lifecycle, plan T23).
      InternalSensorManager().stop();
      await LocalSessionRecorder().stop();
    } else {
      await LocalSessionRecorder().start(
          sessionId: sid,
          role: _deviceRole,
          deviceId: _deviceId,
          subject: _sessionSubject,
          sessionTag: _sessionTag,
          operator: _sessionOperator);
      // Sensors are acquired here — owned by the running isolate (the task
      // engine), gated to an active session, not by any widget lifecycle.
      InternalSensorManager().start(frequency: 100);
      // Checkpoint the active-session flag immediately (before any ack/read) so a process
      // kill right after START still resumes this recording. On a resumed session, keep
      // the restored checkpoint instead of overwriting it with zero; the next packet will
      // then use the checkpoint's next sequence number.
      final checkpoint = _sequence == 0 ? 0 : _sequence - 1;
      unawaited(_persistSequence(checkpoint));
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
    _serverState = null; // never gate a buffer flush on a stale "RECORDING"
    _serverLateSid = null;
    _offlineSince = DateTime.now();
    _pingTimer?.cancel();
    _resyncTimer?.cancel();
    _telemetryWatchdog?.cancel();
    _telemetryWatchdog = null;
    unawaited(_disposeChannels());
    if (_activeSessionId != null) {
      ForegroundServiceHandler()
          .updateStatus('⚠ DISCONNECTED — buffering locally');
    }
    _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_reconnectTimer != null) return;
    _reconnectTimer = Timer(const Duration(seconds: 3), () async {
      _reconnectTimer = null;
      if (_state != WsState.offline) return;
      try {
        await connect(_serverIp);
      } catch (error) {
        ConnDebug.log('reconnect failed: $error');
        _setState(WsState.offline);
        _scheduleReconnect();
      }
      // Reconciliation + flush now happen inside connect()'s success path for every
      // entry point, not just this timer (plan R3).
    });
  }

  Future<void> _afterConnectReconcile() async {
    try {
      await _waitForServerState(const Duration(seconds: 3));
      await _flushFallbackBuffer();
      // Also push any finished, not-yet-uploaded local rescue CSVs to the backend so the
      // desktop can pull them (resumable HTTP, no adb).
      unawaited(RecoveryUploader().uploadPending());
    } catch (error) {
      // Reconciliation is supplemental to the local recorder. Keep the bytes on
      // disk and let the next reconnect retry instead of surfacing an unhandled
      // Future from the successful socket handshake.
      ConnDebug.log('post-connect reconcile failed: $error');
    }
  }

  Future<void> _flushFallbackBuffer() async {
    final buf = FallbackBufferManager();
    // Not just in-memory state: a buffer that survived a process death has isActive==false
    // and bufferedCount==0 in this fresh process, but real bytes still sit on disk (plan T17).
    if (!buf.isActive && (await buf.pendingOnDisk()) == 0) return;

    // Deliver ONLY into the session these bytes belong to. The backend discards telemetry
    // that does not match an open (or late-window) session, and the old code then erased
    // the local copy on "flush completed" — which only meant the socket accepted the
    // bytes, never that anything was written (plan D3).
    final target =
        (_serverState == 'RECORDING') ? _activeSessionId : _serverLateSid;
    if (target == null || buf.sessionId == null || buf.sessionId != target) {
      final orphanSessionId = buf.sessionId;
      final moved = await buf.quarantine();
      _packetsBuffered = 0;
      LocalSessionRecorder().logEvent({
        'type': 'buffer_drop',
        'time_ms': DateTime.now().millisecondsSinceEpoch,
        'count': moved.length,
        'session_id': orphanSessionId,
      });
      // One event, carrying the session the bytes actually belonged to. This used to be
      // emitted twice, the second time reading buf.sessionId — which quarantine() has
      // already cleared, so the operator saw a duplicate alert naming a null session.
      _emitEvent({
        'type': 'buffer_orphaned',
        'files': moved,
        'session_id': orphanSessionId
      });
      return;
    }

    bool completed = true;
    await for (final bytes in buf.flushStream()) {
      if (_state != WsState.connected) {
        completed = false;
        break;
      }
      try {
        _telemetry?.sink.add(bytes);
      } catch (error) {
        completed = false;
        ConnDebug.log('fallback replay failed: $error');
        _onControlDisconnect();
        break;
      }
      if (++_flushCounter % 200 == 0) {
        // Re-check the target: a new session may have started mid-flush, and the rest of
        // this buffer does NOT belong to it (plan R6).
        final stillValid = (_serverState == 'RECORDING')
            ? (_activeSessionId == target)
            : (_serverLateSid == target);
        if (!stillValid) {
          completed = false;
          break;
        }
        await Future.delayed(
            const Duration(milliseconds: 2)); // pace the sink, no backpressure
      }
    }
    if (completed && _state == WsState.connected) {
      final parseClean = buf.truncatedRecords == 0 && buf.malformedRecords == 0;
      if (buf.droppedOverflow > 0 || !parseClean) {
        LocalSessionRecorder().logEvent({
          'type': 'fallback_buffer_parse_warning',
          'time_ms': DateTime.now().millisecondsSinceEpoch,
          'dropped_overflow': buf.droppedOverflow,
          'truncated_records': buf.truncatedRecords,
          'malformed_records': buf.malformedRecords,
        });
      }
      if (parseClean) {
        await buf.clearAfterFlush();
        _packetsBuffered = buf.bufferedCount;
        _emitEvent({'type': 'buffer_flushed'});
      } else {
        // Keep the immutable snapshot. A malformed/truncated tail means the parser could
        // not prove that every record in that file was delivered; deleting it here would
        // turn a recoverable forensic copy into a silent loss. Valid prefixes are safe to
        // replay on the next reconnect because backend dedup is sequence-keyed.
        _emitEvent({
          'type': 'buffer_flush_incomplete',
          'truncated_records': buf.truncatedRecords,
          'malformed_records': buf.malformedRecords
        });
      }
    }
    // If the socket dropped mid-flush, leave the buffer intact; the next reconnect
    // re-flushes it. Backend dedup (device_id, session_id, sequence_number) makes the
    // re-send idempotent, so no duplicate rows are written.
  }

  // ── Commands ─────────────────────────────────────────────────────────────

  Future<void> sendCommand(CommandProto cmd) async {
    if (_state != WsState.connected) return;
    try {
      _control?.sink.add(cmd.toBytes());
    } catch (error) {
      ConnDebug.log('control write failed: $error');
      _onControlDisconnect();
    }
  }

  Future<void> _sendDeviceRegister() async {
    final proto = DeviceRegisterProto(
      deviceId: _deviceId,
      deviceRole: _deviceRole,
      deviceModel: 'Android',
      androidVersion: '',
      appVersion: _reportedAppVersion,
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
    _lastPong =
        DateTime.now(); // fresh grace window each time the timer (re)starts
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
    try {
      await SessionPersistence().save(
        sessionId: _activeSessionId!,
        deviceId: _deviceId,
        serverIp: _serverIp,
        clockOffsetMs: ClockSyncService().clockOffsetMs,
        lastSequenceNumber: seq,
        deviceRole: _deviceRole,
      );
    } catch (error) {
      // A checkpoint is recovery metadata, not a reason to terminate acquisition.
      ConnDebug.log('sequence checkpoint failed at $seq: $error');
    }
  }

  // The backend dedups on (device_id, session_id, sequence_number). After a process kill
  // (MIUI does this routinely), resume from the last persisted checkpoint rather than
  // restarting at zero. The local rescue CSV supplies the uncertain tail and the backend
  // drops any duplicate sequence values that were already accepted.
  Future<void> _restoreSequenceIfInterrupted() async {
    if (_activeSessionId != null || _sequence != 0) return;
    final saved = await SessionPersistence().loadInterrupted();
    if (saved == null) return;
    if (saved['device_id'] != _deviceId) return;
    final sid = saved['session_id']?.toString();
    final last = (saved['last_sequence_number'] as num?)?.toInt();
    if (sid == null || last == null) return;
    // The old +5000 safety margin made every process restart look like thousands of lost
    // samples to the integrity validator. Resume at the checkpoint's next sequence instead:
    // packets emitted before the crash are safely de-duplicated by (device, session, seq),
    // while the local rescue CSV supplies any tail the backend did not receive.
    _sequence = last + 1;
    // Resume the local recorder immediately, even before the control socket reconnects —
    // it is the guarantee that must not wait on the network (plan T12).
    await _setActiveSession(sid);
    await FallbackBufferManager()
        .loadMeta(); // re-attach any surviving buffer to its session
    _emitEvent({
      'type': 'session_resumed',
      'session_id': sid,
      'from_sequence': _sequence
    });
  }

  // ── Disconnect ───────────────────────────────────────────────────────────

  Future<void> disconnect() async {
    if (_activeSessionId != null) {
      _emitEvent({
        'type': 'disconnect_refused',
        'session_id': _activeSessionId,
        'reason':
            'An active session can only be stopped by the backend operator.',
      });
      return;
    }
    _pingTimer?.cancel();
    _resyncTimer?.cancel();
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _telemetryWatchdog?.cancel();
    _telemetryWatchdog = null;
    _sensorSub?.cancel();
    await _disposeChannels();
    await LocalSessionRecorder()
        .stop(); // close the file cleanly; no unflushed tail (plan R8)
    _setState(WsState.disconnected);
    await FallbackBufferManager().deactivate();
    // Stop foreground service only on explicit disconnect, not on temporary drops.
    await ForegroundServiceHandler().stop();
  }

  void _setState(WsState s) {
    final prev = _state;
    _state = s;
    // Alarm only matters while a session is running — a disconnect on the connect screen
    // is already visible and self-explanatory (plan T13).
    if (_activeSessionId != null) {
      final nowMs = DateTime.now().millisecondsSinceEpoch;
      if (prev == WsState.connected && s != WsState.connected) {
        AlertService().startAlarm();
        _lostAtMs = DateTime.now();
        LocalSessionRecorder()
            .logEvent({'type': 'connection_lost', 'time_ms': nowMs});
      } else if (prev != WsState.connected && s == WsState.connected) {
        AlertService().stopAlarm();
        final dur = _lostAtMs != null
            ? DateTime.now().difference(_lostAtMs!).inMilliseconds
            : null;
        _lostAtMs = null;
        LocalSessionRecorder().logEvent({
          'type': 'connection_restored',
          'time_ms': nowMs,
          if (dur != null) 'duration_ms': dur,
        });
      }
    }
    _stateController.add(s);
  }

  void _emitEvent(Map<String, dynamic> e) => _eventController.add(e);
}
