import 'dart:async';
import 'package:flutter/material.dart';
import '../services/alert_service.dart';
import '../services/internal_sensor_manager.dart';
import '../services/local_session_recorder.dart';
import '../services/task_bridge.dart';
import '../services/websocket_client.dart' show WsState;
import '../widgets/graph_widget.dart';
import '../models/sensor_packet.dart';
import 'connection_screen.dart';

// NOTE: The dashboard's InternalSensorManager instance is a DISPLAY-ONLY preview
// of live accelerometer/gyroscope data for the graphs. Recording acquisition is
// owned by the foreground-task engine (task isolate) and is gated to an active
// session; this preview can be torn down with the UI without affecting recording.
class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key});

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  StreamSubscription? _stateSub;
  StreamSubscription? _eventSub;
  Timer? _tickTimer;
  Stream<SensorPacket>? _sensorStream;

  WsState _wsState = WsState.disconnected;
  bool _isRecording = false;
  int _packetsSent = 0;
  int _packetsBuffered = 0;
  String? _sessionId;
  String? _serverState;
  int _activeLabel = 0;

  @override
  void initState() {
    super.initState();
    // Live graph preview (display only). Recording acquisition lives in the task engine.
    InternalSensorManager().start(frequency: 100);
    _sensorStream = InternalSensorManager().dataStream;

    _stateSub = TaskBridge().stateStream.listen((s) {
      setState(() => _wsState = s);
    });

    _eventSub = TaskBridge().eventStream.listen(_onEvent);

    // Reflect current state immediately.
    _wsState = TaskBridge().state;

    // Drives time-based UI (unconfirmed-recording badge, offline timer) that would
    // otherwise only refresh when a new bridge event arrives.
    _tickTimer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  void _onEvent(Map<String, dynamic> e) {
    final type = e['type'] as String?;
    setState(() {
      _packetsSent = TaskBridge().packetsSent;
      _packetsBuffered = TaskBridge().packetsBuffered;
      _sessionId = TaskBridge().activeSessionId;
      _serverState = TaskBridge().serverState;
      if (type == 'start_session') _isRecording = true;
      if (type == 'stop_session') {
        _isRecording = false;
        _activeLabel = 0;
      }
      if (type == 'set_label') {
        try {
          final payload = e['payload'] as String;
          final match = RegExp(r'"label_id"\s*:\s*(\d+)').firstMatch(payload);
          if (match != null) _activeLabel = int.parse(match.group(1)!);
        } catch (_) {}
      }
    });
  }

  @override
  void dispose() {
    _stateSub?.cancel();
    _eventSub?.cancel();
    _tickTimer?.cancel();
    InternalSensorManager().stop(); // preview only; recording is task-owned
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: const Color(0xFF1A1A2E),
      appBar: AppBar(
        backgroundColor: _isRecording ? Colors.red.shade900 : const Color(0xFF16213E),
        title: Text(
          'IMU Node · ${TaskBridge().deviceRole.toUpperCase()}',
          style: const TextStyle(color: Colors.white, fontWeight: FontWeight.bold),
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.folder_outlined, color: Colors.white54),
            onPressed: () => _showLocalFiles(context),
            tooltip: 'Local files',
          ),
          _WsStatusDot(_wsState),
          const SizedBox(width: 12),
        ],
        leading: IconButton(
          icon: const Icon(Icons.logout, color: Colors.white54),
          onPressed: _disconnect,
          tooltip: 'Disconnect',
        ),
      ),
      body: Column(
        children: [
          if (_wsState != WsState.connected && _isRecording) _DisconnectBanner(_packetsBuffered),
          _StatusBar(
            role: TaskBridge().deviceRole,
            isRecording: _isRecording,
            unconfirmed: TaskBridge().isRecordingUnconfirmed,
            serverState: _serverState,
            sessionId: _sessionId,
            sent: _packetsSent,
            buffered: _packetsBuffered,
            activeLabel: _activeLabel,
            localRows: TaskBridge().localRows,
            localOpen: TaskBridge().localOpen,
            localError: TaskBridge().localError,
          ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.all(8),
              children: [
                _sectionLabel('Accelerometer (g)'),
                GraphWidget(
                    size: const Size(double.infinity, 80),
                    maxPoints: 100,
                    dataStream: _sensorStream!,
                    sensorType: 'accel',
                    axis: 'x'),
                GraphWidget(
                    size: const Size(double.infinity, 80),
                    maxPoints: 100,
                    dataStream: _sensorStream!,
                    sensorType: 'accel',
                    axis: 'y'),
                GraphWidget(
                    size: const Size(double.infinity, 80),
                    maxPoints: 100,
                    dataStream: _sensorStream!,
                    sensorType: 'accel',
                    axis: 'z'),
                const SizedBox(height: 8),
                _sectionLabel('Gyroscope (°/s)'),
                GraphWidget(
                    size: const Size(double.infinity, 80),
                    maxPoints: 100,
                    dataStream: _sensorStream!,
                    sensorType: 'gyro',
                    axis: 'x'),
                GraphWidget(
                    size: const Size(double.infinity, 80),
                    maxPoints: 100,
                    dataStream: _sensorStream!,
                    sensorType: 'gyro',
                    axis: 'y'),
                GraphWidget(
                    size: const Size(double.infinity, 80),
                    maxPoints: 100,
                    dataStream: _sensorStream!,
                    sensorType: 'gyro',
                    axis: 'z'),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _sectionLabel(String text) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 4, horizontal: 4),
        child: Text(text,
            style: const TextStyle(
                color: Colors.white54,
                fontSize: 12,
                fontWeight: FontWeight.bold)),
      );

  Future<void> _disconnect() async {
    await TaskBridge().disconnect();
    if (!mounted) return;
    Navigator.pushReplacement(
      context,
      MaterialPageRoute(builder: (_) => const ConnectionScreen()),
    );
  }

  Future<void> _showLocalFiles(BuildContext context) async {
    final files = await LocalSessionRecorder().listSessions();
    if (!context.mounted) return;
    showModalBottomSheet(
      context: context,
      backgroundColor: const Color(0xFF16213E),
      isScrollControlled: true,
      builder: (_) => DraggableScrollableSheet(
        expand: false,
        initialChildSize: 0.6,
        builder: (_, scrollController) => Column(
          children: [
            const Padding(
              padding: EdgeInsets.all(12),
              child: Text('Local session recordings',
                  style: TextStyle(color: Colors.white, fontWeight: FontWeight.bold)),
            ),
            if (files.isEmpty)
              const Padding(
                padding: EdgeInsets.all(16),
                child: Text('No local recordings yet.',
                    style: TextStyle(color: Colors.white38)),
              ),
            Expanded(
              child: ListView.builder(
                controller: scrollController,
                itemCount: files.length,
                itemBuilder: (_, i) {
                  final f = files[i];
                  final sizeMb = f.lengthSync() / (1024 * 1024);
                  return ListTile(
                    dense: true,
                    title: Text(f.uri.pathSegments.last,
                        style: const TextStyle(color: Colors.white, fontSize: 12)),
                    subtitle: SelectableText(f.path,
                        style: const TextStyle(color: Colors.white38, fontSize: 10)),
                    trailing: Text('${sizeMb.toStringAsFixed(1)} MB',
                        style: const TextStyle(color: Colors.cyanAccent, fontSize: 11)),
                  );
                },
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _WsStatusDot extends StatelessWidget {
  final WsState state;
  const _WsStatusDot(this.state);

  @override
  Widget build(BuildContext context) {
    final (color, label) = switch (state) {
      WsState.connected => (Colors.greenAccent, 'LIVE'),
      WsState.connecting => (Colors.amber, 'CONNECTING'),
      WsState.offline => (Colors.orange, 'OFFLINE'),
      WsState.disconnected => (Colors.red, 'DISCONNECTED'),
    };
    return Row(
      children: [
        Container(
            width: 8,
            height: 8,
            decoration:
                BoxDecoration(color: color, shape: BoxShape.circle)),
        const SizedBox(width: 4),
        Text(label,
            style: TextStyle(
                color: color, fontSize: 11, fontWeight: FontWeight.bold)),
      ],
    );
  }
}

/// Full-width alarm banner shown while offline mid-recording.
class _DisconnectBanner extends StatelessWidget {
  final int buffered;
  const _DisconnectBanner(this.buffered);

  String _elapsed() {
    final since = TaskBridge().offlineSince;
    if (since == null) return '00:00';
    final s = DateTime.now().difference(since).inSeconds;
    final m = (s ~/ 60).toString().padLeft(2, '0');
    final r = (s % 60).toString().padLeft(2, '0');
    return '$m:$r';
  }

  @override
  Widget build(BuildContext context) {
    final localOk = TaskBridge().localOpen && TaskBridge().localError == null;
    return Material(
      color: Colors.red.shade900,
      child: InkWell(
        onTap: () => AlertService().silence(),
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          child: Row(
            children: [
              const Icon(Icons.warning_amber_rounded, color: Colors.white, size: 16),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  'DISCONNECTED  ·  ${_elapsed()}  ·  '
                  '${buffered.toString()} packets buffered  ·  '
                  'local backup ${localOk ? 'OK' : 'FAILED'}',
                  style: const TextStyle(
                      color: Colors.white, fontSize: 12, fontWeight: FontWeight.bold),
                ),
              ),
              TextButton(
                onPressed: () => AlertService().silence(),
                style: TextButton.styleFrom(
                  minimumSize: Size.zero,
                  padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                ),
                child: Text(
                  AlertService().isSilenced ? 'SILENCED' : 'SILENCE',
                  style: const TextStyle(
                      color: Colors.white, fontSize: 11, fontWeight: FontWeight.bold),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _StatusBar extends StatelessWidget {
  final String role;
  final bool isRecording;
  final bool unconfirmed;
  final String? serverState;
  final String? sessionId;
  final int sent;
  final int buffered;
  final int activeLabel;
  final int localRows;
  final bool localOpen;
  final String? localError;

  const _StatusBar({
    required this.role,
    required this.isRecording,
    required this.unconfirmed,
    required this.serverState,
    required this.sessionId,
    required this.sent,
    required this.buffered,
    required this.activeLabel,
    required this.localRows,
    required this.localOpen,
    required this.localError,
  });

  @override
  Widget build(BuildContext context) {
    return Container(
      color: const Color(0xFF0F3460),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.spaceBetween,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
            decoration: BoxDecoration(
              color: Colors.deepPurpleAccent.withOpacity(0.25),
              borderRadius: BorderRadius.circular(6),
              border: Border.all(color: Colors.deepPurpleAccent.withOpacity(0.6)),
            ),
            child: Text(
              role.toUpperCase(),
              style: const TextStyle(
                  color: Colors.white,
                  fontSize: 12,
                  fontWeight: FontWeight.bold,
                  letterSpacing: 0.5),
            ),
          ),
          Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                !isRecording
                    ? '○ STANDBY'
                    : (unconfirmed ? '● RECORDING (unconfirmed)' : '● RECORDING'),
                style: TextStyle(
                    color: !isRecording
                        ? Colors.white38
                        : (unconfirmed ? Colors.amber : Colors.redAccent),
                    fontWeight: FontWeight.bold,
                    fontSize: 12),
              ),
              if (isRecording && unconfirmed)
                TaskBridge().lastStateAt != null
                    ? Text(
                        'No confirmation from backend for '
                        '${DateTime.now().difference(TaskBridge().lastStateAt!).inSeconds}s',
                        style: const TextStyle(color: Colors.amber, fontSize: 10),
                      )
                    : const Text('No confirmation from backend yet',
                        style: TextStyle(color: Colors.amber, fontSize: 10)),
              if (serverState != null && !(isRecording && unconfirmed))
                Text('Backend: $serverState',
                    style: const TextStyle(color: Colors.white24, fontSize: 10)),
              if (sessionId != null)
                Text('Session: ${sessionId!.substring(0, 8)}…',
                    style: const TextStyle(
                        color: Colors.white38, fontSize: 10)),
            ],
          ),
          Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Text('Sent: $sent',
                  style: const TextStyle(color: Colors.white70, fontSize: 11)),
              if (buffered > 0)
                Text('Buffered: $buffered',
                    style: const TextStyle(
                        color: Colors.orange, fontSize: 11)),
              if (isRecording)
                Text('Label: $activeLabel',
                    style: TextStyle(
                        color: activeLabel == 0 ? Colors.white54 : Colors.greenAccent,
                        fontSize: 11)),
              if (localError != null)
                Text('Local backup FAILED: $localError',
                    style: const TextStyle(
                        color: Colors.redAccent, fontSize: 10, fontWeight: FontWeight.bold))
              else if (localOpen)
                Text('Local: ${localRows.toString()} rows',
                    style: const TextStyle(color: Colors.cyanAccent, fontSize: 11)),
            ],
          ),
        ],
      ),
    );
  }
}
