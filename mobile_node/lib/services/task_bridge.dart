import 'dart:async';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'conn_debug.dart';
import 'foreground_service_handler.dart';
import 'websocket_client.dart';

/// UI-isolate facade over the foreground-task engine.
///
/// The pipeline (WebSocket, sensors, recorder, buffers) runs in the task engine's
/// separate isolate. This facade forwards connect/disconnect commands to it via
/// [FlutterForegroundTask.sendDataToTask] and mirrors its state back through an
/// [addTaskDataCallback] listener that [init] registers once in `main()`.
///
/// The UI reads ONLY this facade — never the task-isolate singletons — so the UI
/// can be torn down and recreated without affecting acquisition.
class TaskBridge {
  static final TaskBridge _instance = TaskBridge._();
  factory TaskBridge() => _instance;
  TaskBridge._();

  final _stateController = StreamController<WsState>.broadcast();
  final _eventController = StreamController<Map<String, dynamic>>.broadcast();
  Stream<WsState> get stateStream => _stateController.stream;
  Stream<Map<String, dynamic>> get eventStream => _eventController.stream;

  // Mirrored pipeline state.
  WsState _state = WsState.disconnected;
  int _packetsSent = 0;
  int _packetsBuffered = 0;
  String? _activeSessionId;
  String? _serverState;
  DateTime? _offlineSince;
  DateTime? _lastStateAt;
  bool _isRecordingUnconfirmed = false;
  String? _lastConnectError;
  String _deviceRole = 'chest';
  int _localRows = 0;
  bool _localOpen = false;
  String? _localError;
  bool _clockSynced = false;
  int _clockOffsetMs = 0;
  int _lastRttMs = 0;

  WsState get state => _state;
  int get packetsSent => _packetsSent;
  int get packetsBuffered => _packetsBuffered;
  String? get activeSessionId => _activeSessionId;
  String? get serverState => _serverState;
  DateTime? get offlineSince => _offlineSince;
  DateTime? get lastStateAt => _lastStateAt;
  bool get isRecordingUnconfirmed => _isRecordingUnconfirmed;
  String? get lastConnectError => _lastConnectError;
  String get deviceRole => _deviceRole;
  int get localRows => _localRows;
  bool get localOpen => _localOpen;
  String? get localError => _localError;
  bool get clockSynced => _clockSynced;
  int get clockOffsetMs => _clockOffsetMs;
  int get lastRttMs => _lastRttMs;

  String? _engineId;
  Completer<void>? _taskReady;
  Completer<bool>? _pendingConnect;
  Completer<bool>? _pendingConnected;
  bool _listening = false;

  /// Register the task-data callback. Call once from `main()` before any UI.
  void init() {
    if (_listening) return;
    _listening = true;
    // Register the main-isolate receive port that the task engine's sendDataToMain()
    // delivers into. Without this, every task->UI message (task_ready, snapshot,
    // connect_result) is silently dropped: sendDataToMain() does an IsolateNameServer
    // lookup that returns null and is a no-op. This is required by flutter_foreground_task
    // (README §6) and was the root cause of connect() never resolving despite the phone
    // being fully connected server-side.
    FlutterForegroundTask.initCommunicationPort();
    FlutterForegroundTask.addTaskDataCallback(_onTaskData);
  }

  void _onTaskData(Object data) {
    if (data is! Map) return;
    final m = Map<String, dynamic>.from(data);
    final kind = m['k'];
    if (kind == 'task_ready') {
      _engineId = m['engineId']?.toString();
      final tr = _taskReady;
      _taskReady = null;
      tr?.complete();
      return;
    }
    if (kind == 'snapshot') {
      _applySnapshot(m);
      return;
    }
    if (kind == 'connect_result') {
      // Carry the real failure reason into the mirrored state — previously the error
      // on connect_result was dropped and the UI fell back to a misleading generic
      // "Could not connect to IP:8000" whenever the snapshot hadn't yet propagated.
      final err = m['error'];
      if (err is String && err.isNotEmpty) _lastConnectError = err;
      final c = _pendingConnect;
      _pendingConnect = null;
      final ok = m['ok'] == true;
      if (!ok && _lastConnectError == null) {
        _lastConnectError = 'Connection failed — see conn_debug.log on this phone.';
      }
      if (c != null) c.complete(ok);
      return;
    }
  }

  void _applySnapshot(Map<String, dynamic> m) {
    WsState? newState;
    if (m['state'] is int) newState = WsState.values[m['state'] as int];
    _packetsSent = m['packetsSent'] as int? ?? _packetsSent;
    _packetsBuffered = m['packetsBuffered'] as int? ?? _packetsBuffered;
    _activeSessionId = m['activeSessionId'] as String? ?? _activeSessionId;
    _serverState = m['serverState'] as String? ?? _serverState;
    _lastConnectError = m['lastConnectError'] as String? ?? _lastConnectError;
    _deviceRole = m['deviceRole'] as String? ?? _deviceRole;
    _localRows = m['localRows'] as int? ?? _localRows;
    _localOpen = m['localOpen'] as bool? ?? _localOpen;
    _localError = m['localError'] as String? ?? _localError;
    _clockSynced = m['clockSynced'] as bool? ?? _clockSynced;
    _clockOffsetMs = m['clockOffsetMs'] as int? ?? _clockOffsetMs;
    _lastRttMs = m['lastRttMs'] as int? ?? _lastRttMs;
    _isRecordingUnconfirmed = m['isRecordingUnconfirmed'] as bool? ?? _isRecordingUnconfirmed;

    final offMs = m['offlineSinceMs'];
    _offlineSince =
        offMs is int ? DateTime.fromMillisecondsSinceEpoch(offMs) : null;
    final lastMs = m['lastStateAtMs'];
    _lastStateAt =
        lastMs is int ? DateTime.fromMillisecondsSinceEpoch(lastMs) : null;

    if (newState != null && newState != _state) {
      _state = newState;
      if (newState == WsState.connected) {
        final pc = _pendingConnected;
        _pendingConnected = null;
        pc?.complete(true);
        // The task reaching "connected" is authoritative proof the sockets are up.
        // Also resolve any in-flight connect() so a lost/raced connect_result ack can
        // never turn a real, healthy connection into a false "Could not connect" error.
        final pend = _pendingConnect;
        _pendingConnect = null;
        pend?.complete(true);
      } else if (newState == WsState.offline) {
        final pend = _pendingConnect;
        if (pend != null && !pend.isCompleted) {
          _pendingConnect = null;
          pend.complete(false);
        }
      }
      ConnDebug.log('UI state -> ${newState.name} (${m['state']})');
      if (!_stateController.isClosed) _stateController.add(newState);
    }

    final ev = m['event'];
    if (ev is Map && !_eventController.isClosed) {
      _eventController.add(Map<String, dynamic>.from(ev));
    }
  }

  Future<void> _waitForTaskReady() async {
    if (_engineId != null) return;
    final c = Completer<void>();
    _taskReady = c;
    await c.future.timeout(const Duration(seconds: 8), onTimeout: () {});
  }

  /// Starts the foreground service (once) and issues connect. Resolves with
  /// whether the control channel opened.
  Future<bool> connect(String ip) async {
    await ForegroundServiceHandler().start();
    await _waitForTaskReady();
    final c = Completer<bool>();
    _pendingConnect = c;
    ConnDebug.log('UI -> sendDataToTask connect $ip');
    FlutterForegroundTask.sendDataToTask({'cmd': 'connect', 'ip': ip});
    final ok = await c.future
        .timeout(const Duration(seconds: 15), onTimeout: () => false);
    if (!ok && _lastConnectError == null) {
      _lastConnectError =
          'No reply from task engine in 15s — check conn_debug.log on the phone.';
    }
    ConnDebug.log('UI connect($ip) resolved ok=$ok');
    return ok;
  }

  Future<void> disconnect() async {
    FlutterForegroundTask.sendDataToTask({'cmd': 'disconnect'});
  }

  /// Waits until the task reports WsState.connected (used for automatic resume).
  Future<bool> waitUntilConnected(Duration timeout) async {
    if (_state == WsState.connected) return true;
    final c = Completer<bool>();
    _pendingConnected = c;
    return c.future.timeout(timeout, onTimeout: () => false);
  }
}
