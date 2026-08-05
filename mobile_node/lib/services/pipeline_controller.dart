import 'dart:async';
import 'dart:math';
import 'package:flutter_foreground_task/flutter_foreground_task.dart';
import 'clock_sync_service.dart';
import 'internal_sensor_manager.dart';
import 'local_session_recorder.dart';
import 'session_persistence.dart';
import 'websocket_client.dart';

/// Task-engine owner of the acquisition pipeline.
///
/// Lives in the foreground-task engine's isolate — NOT the UI isolate. It drives
/// [WebSocketClient] (which owns sensors + recorder + buffers + clock sync), and
/// mirrors pipeline state to the UI facade ([TaskBridge]) via
/// [FlutterForegroundTask.sendDataToMain].
///
/// On engine restart it recovers from persisted state (desired endpoint + active
/// recording) without waiting for the UI, and each restart logs an engine-id so
/// gaps become detectable evidence in the JSONL sidecar.
class PipelineController {
  String _engineId = '';
  StreamSubscription<WsState>? _stateSub;
  StreamSubscription<Map<String, dynamic>>? _eventSub;
  bool _initialized = false;

  String get engineId => _engineId;

  /// Called from TaskHandler.onStart. Subscribes to the pipeline and advertises
  /// readiness to the UI, then auto-reconnects from persisted state.
  Future<void> init() async {
    if (_initialized) return;
    _initialized = true;
    _engineId = _newEngineId();

    _stateSub = WebSocketClient().stateStream.listen((_) => _sendSnapshot());
    _eventSub =
        WebSocketClient().eventStream.listen(_onPipelineEvent, onError: (_) {});

    // Horizontally: whenever the UI attaches late it must see current state.
    _sendSnapshot();

    // task_ready handshake: the UI waits for this before issuing connect.
    FlutterForegroundTask.sendDataToMain({'k': 'task_ready', 'engineId': _engineId});

    await _autoReconnect();
  }

  Future<void> _autoReconnect() async {
    final desired = await SessionPersistence().loadDesired();
    final ip = desired?['server_ip']?.toString() ?? '';
    if (ip.isNotEmpty) {
      // Idempotent; also resumes an interrupted recording via
      // WebSocketClient._restoreSequenceIfInterrupted().
      await WebSocketClient().connect(ip);
    }
  }

  void _onPipelineEvent(Map<String, dynamic> event) {
    final t = DateTime.now().millisecondsSinceEpoch;
    switch (event['type']) {
      case 'start_session':
        LocalSessionRecorder()
            .logEvent({'type': 'service_start', 'engine_id': _engineId, 'time_ms': t});
      case 'session_resumed':
        LocalSessionRecorder()
            .logEvent({'type': 'service_restart', 'engine_id': _engineId, 'time_ms': t});
      default:
        break;
    }
    _sendSnapshot(event: event);
  }

  /// Called from TaskHandler.onReceiveData with a UI command.
  void onCommand(Object? data) {
    if (data is! Map) return;
    final cmd = data['cmd'];
    if (cmd == 'connect') {
      final ip = data['ip']?.toString() ?? '';
      unawaited(_connectAndReport(ip));
    } else if (cmd == 'disconnect') {
      unawaited(WebSocketClient().disconnect());
    }
  }

  Future<void> _connectAndReport(String ip) async {
    final ok = await WebSocketClient().connect(ip);
    FlutterForegroundTask.sendDataToMain({
      'k': 'connect_result',
      'ok': ok,
      'error': ok ? null : WebSocketClient().lastConnectError,
    });
  }

  /// Periodic (5 s) statistics report to the sidecar + a UI snapshot.
  void onTick() {
    final rec = LocalSessionRecorder();
    if (rec.isOpen) {
      final acc = InternalSensorManager();
      rec.logEvent({
        'type': 'sampling',
        'time_ms': DateTime.now().millisecondsSinceEpoch,
        'expected_hz': acc.currentFrequency.round(),
        'observed_acc_hz': _round2(acc.accObservedHz),
        'observed_gyro_hz': _round2(acc.gyroObservedHz),
      });
    }
    _sendSnapshot();
  }

  Future<void> shutdown() async {
    await _stateSub?.cancel();
    await _eventSub?.cancel();
    _stateSub = null;
    _eventSub = null;
  }

  void _sendSnapshot({Object? event}) {
    final ws = WebSocketClient();
    final rec = LocalSessionRecorder();
    FlutterForegroundTask.sendDataToMain({
      'k': 'snapshot',
      'state': ws.state.index,
      'packetsSent': ws.packetsSent,
      'packetsBuffered': ws.packetsBuffered,
      'activeSessionId': ws.activeSessionId,
      'serverState': ws.serverState,
      'offlineSinceMs': ws.offlineSince?.millisecondsSinceEpoch,
      'lastStateAtMs': ws.lastStateAt?.millisecondsSinceEpoch,
      'isRecordingUnconfirmed': ws.isRecordingUnconfirmed,
      'lastConnectError': ws.lastConnectError,
      'deviceRole': ws.deviceRole,
      'localRows': rec.rows,
      'localOpen': rec.isOpen,
      'localError': rec.lastError,
      'clockSynced': ClockSyncService().isSynced,
      'clockOffsetMs': ClockSyncService().clockOffsetMs,
      'lastRttMs': ClockSyncService().lastRttMs,
      'engineId': _engineId,
      'event': event,
    });
  }

  static String _newEngineId() {
    final r = DateTime.now().microsecondsSinceEpoch & 0xFFFFFF;
    final rnd = _randomHex(6);
    return '${r.toRadixString(16).padLeft(6, '0')}-$rnd';
  }

  static String _randomHex(int len) {
    final b = List.generate(len, (_) => Random().nextInt(16).toRadixString(16)).join();
    return b;
  }

  static double _round2(double v) => (v * 100).roundToDouble() / 100;
}
