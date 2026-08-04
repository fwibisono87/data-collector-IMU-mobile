import 'dart:async';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';
import '../models/sensor_packet.dart';

/// Writes every packet of a session to phone-local storage, from START to STOP,
/// REGARDLESS of network state.
///
/// This is the system's data guarantee. Backend CSVs, the fallback buffer, dedup and
/// late delivery are all network-dependent optimisations layered on top; this file is
/// not. If Wi-Fi dies for an entire session, the operator still has a complete recording
/// and only needs to pull it over USB.
///
/// Location: getExternalStorageDirectory()/imu_sessions/<session_id>_<role>.csv
///   -> /sdcard/Android/data/<package>/files/imu_sessions/
///   App-private external storage: no runtime permission needed, visible over MTP/adb.
///
/// Schema is byte-identical to the backend CSV (io_manager._CSV_HEADER) so the two can be
/// diffed or merged directly. NOTE: label_id/label_name reflect the last SET_LABEL this
/// phone received; if the phone was offline when the operator changed the label, the
/// backend CSV is authoritative for labels. The local file is a data rescue, not a
/// replacement for the synchronised capture.
class LocalSessionRecorder {
  static final LocalSessionRecorder _i = LocalSessionRecorder._();
  factory LocalSessionRecorder() => _i;
  LocalSessionRecorder._();

  static const _header =
      'timestamp_ms,acc_x_g,acc_y_g,acc_z_g,'
      'gyro_x_degs,gyro_y_degs,gyro_z_degs,'
      'label_id,label_name,sequence_number,device_id\n';
  static const int _maxSessionsKept = 20;

  IOSink? _sink;
  File? _file;
  Timer? _flushTimer;
  String? _sessionId;
  int _rows = 0;
  String? _lastError;

  String? get path => _file?.path;
  int get rows => _rows;
  bool get isOpen => _sink != null;
  String? get lastError => _lastError;

  Future<Directory> _dir() async {
    final base = await getExternalStorageDirectory()
        ?? await getApplicationDocumentsDirectory();
    final d = Directory('${base.path}/imu_sessions');
    if (!await d.exists()) await d.create(recursive: true);
    return d;
  }

  Future<void> start({
    required String sessionId,
    required String role,
    required String deviceId,
    required String subject,
    String sessionTag = '',
    String operator = '',
  }) async {
    if (_sessionId == sessionId && _sink != null) return;   // idempotent on resync
    await stop();
    try {
      final d = await _dir();
      // Describe the file in a human-readable, nameable way: subject, tag, role, device and
      // the wall-clock start stamp. Falls back gracefully to session_id when metadata is
      // unknown (resume/resync adoption). The backend stores the same session under
      // SSD_PATH/Data_Riset_IMU/<subject>_<tag>/, so this name is directly greppable there.
      final stamp = DateTime.now().millisecondsSinceEpoch;
      final who = <String>[
        if (subject.isNotEmpty) subject,
        if (sessionTag.isNotEmpty) sessionTag,
        role,
        deviceId,
        '$stamp',
      ].map(_sanitize).join('_');
      _file = File('${d.path}/${who}_rescue.csv');
      final exists = await _file!.exists() && await _file!.length() > 0;
      _sink = _file!.openWrite(mode: exists ? FileMode.append : FileMode.write);
      if (!exists) {
        _sink!.write('# session_id=$sessionId,role=$role,device_id=$deviceId,'
                     'subject=$subject,session_tag=$sessionTag,operator=$operator,'
                     'start_epoch_ms=$stamp,source=local_node,schema_version=1\n');
        _sink!.write(_header);
      }
      _sessionId = sessionId;
      _rows = 0;
      _lastError = null;
      _flushTimer = Timer.periodic(const Duration(seconds: 1), (_) => _sink?.flush());
      await _prune();
    } catch (e) {
      _lastError = '$e';
      _sink = null;
      debugPrint('LocalSessionRecorder: open failed: $e');
    }
  }

  static String _sanitize(String s) =>
      s.replaceAll(RegExp(r'[^\w.-]+'), '_').replaceAll(RegExp(r'_+'), '_');

  /// Synchronous and cheap — IOSink buffers internally. Safe on the 100 Hz hot path.
  void write(SensorPacket p, {
    required int timestampMs,
    required int sequence,
    required String deviceId,
    required int labelId,
    required String labelName,
  }) {
    final s = _sink;
    if (s == null) return;
    s.write('$timestampMs,'
        '${p.accX.toStringAsFixed(6)},${p.accY.toStringAsFixed(6)},${p.accZ.toStringAsFixed(6)},'
        '${p.gyroX.toStringAsFixed(6)},${p.gyroY.toStringAsFixed(6)},${p.gyroZ.toStringAsFixed(6)},'
        '$labelId,$labelName,$sequence,$deviceId\n');
    _rows++;
  }

  Future<void> stop() async {
    _flushTimer?.cancel();
    _flushTimer = null;
    final s = _sink;
    _sink = null;
    _sessionId = null;
    if (s == null) return;
    try {
      await s.flush();
      await s.close();
    } catch (e) {
      debugPrint('LocalSessionRecorder: close failed: $e');
    }
  }

  Future<List<File>> listSessions() async {
    final d = await _dir();
    final files = d.listSync().whereType<File>().toList()
      ..sort((a, b) => b.path.compareTo(a.path));
    return files;
  }

  Future<void> _prune() async {
    final files = await listSessions();
    for (final f in files.skip(_maxSessionsKept)) {
      try {
        await f.delete();
      } catch (_) {}
    }
  }
}
