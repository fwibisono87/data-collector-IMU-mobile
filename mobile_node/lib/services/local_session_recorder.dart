import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';
import '../models/sensor_packet.dart';

/// Writes every packet of a session to phone-local storage, from START to STOP,
/// REGARDLESS of network state.
///
/// This is the system's data guarantee. Backend CSVs, the fallback buffer, dedup
/// and late delivery are all network-dependent optimisations layered on top; this
/// file is not.
///
/// The CSV columns are byte-identical to the backend CSV (io_manager._CSV_HEADER)
/// and are NEVER changed by data-integrity bookkeeping. Integrity markers and
/// sampling statistics go into a sidecar `*.events.jsonl` file instead, keeping
/// the analysis CSV clean for generic consumers.
///
/// Location: getExternalStorageDirectory()/imu_sessions/<who>_rescue.csv (+.events.jsonl)
class LocalSessionRecorder {
  static final LocalSessionRecorder _i = LocalSessionRecorder._();
  factory LocalSessionRecorder() => _i;
  LocalSessionRecorder._();

  static const _header =
      'timestamp_ms,acc_x_g,acc_y_g,acc_z_g,'
      'gyro_x_degs,gyro_y_degs,gyro_z_degs,'
      'label_id,label_name,sequence_number,device_id,'
      'acc_ts_ms,gyro_ts_ms,sample_kind\n';
  static const int _maxSessionsKept = 20;

  IOSink? _sink;
  IOSink? _events;
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
      final stamp = DateTime.now().millisecondsSinceEpoch;
      final who = <String>[
        if (subject.isNotEmpty) subject,
        if (sessionTag.isNotEmpty) sessionTag,
        role,
        deviceId,
        '$stamp',
      ].map(_sanitize).join('_');
      final csvPath = '${d.path}/${who}_rescue.csv';
      _file = File(csvPath);
      final exists = await _file!.exists() && await _file!.length() > 0;
      if (exists) await _trimIncompleteLine(_file!);
      _sink = _file!.openWrite(mode: exists ? FileMode.append : FileMode.write);
      if (!exists) {
        _sink!.write('# session_id=$sessionId,role=$role,device_id=$deviceId,'
                     'subject=$subject,session_tag=$sessionTag,operator=$operator,'
                     'start_epoch_ms=$stamp,source=local_node,schema_version=2\n');
        _sink!.write(_header);
      }
      // Sidecar for integrity markers / sampling stats (JSONL).
      final eventsFile = File('$csvPath.events.jsonl');
      final eventsExists = await eventsFile.exists() && await eventsFile.length() > 0;
      _events = eventsFile.openWrite(mode: eventsExists ? FileMode.append : FileMode.write);
      _sessionId = sessionId;
      _rows = 0;
      _lastError = null;
      _flushTimer = Timer.periodic(const Duration(seconds: 1), (_) {
        _sink?.flush();
        _events?.flush();
      });
      await _prune();
    } catch (e) {
      _lastError = '$e';
      _sink = null;
      debugPrint('LocalSessionRecorder: open failed: $e');
    }
  }

  static String _sanitize(String s) =>
      s.replaceAll(RegExp(r'[^\w.-]+'), '_').replaceAll(RegExp(r'_+'), '_');

  /// A killed process can leave a partial final CSV line (no trailing newline).
  /// Trim it on reopen so the merged dataset has no malformed row. Keeping
  /// everything after the last `\n` would be a partial row — drop it.
  Future<void> _trimIncompleteLine(File f) async {
    try {
      final bytes = await f.readAsBytes();
      final lastNl = bytes.lastIndexOf(0x0A);
      if (lastNl >= 0 && lastNl + 1 < bytes.length) {
        await f.writeAsBytes(bytes.sublist(0, lastNl + 1), mode: FileMode.write);
      }
    } catch (_) {
      // Best effort only.
    }
  }

  /// Synchronous and cheap — IOSink buffers internally. Safe on the 100 Hz hot path.
  void write(SensorPacket p, {
    required int timestampMs,
    required int sequence,
    required String deviceId,
    required int labelId,
    required String labelName,
    required int accTsMs,
    required int gyroTsMs,
    required int sampleKind,
  }) {
    final s = _sink;
    if (s == null) return;
    final accTs = accTsMs == 0 ? '' : '$accTsMs';
    final gyroTs = gyroTsMs == 0 ? '' : '$gyroTsMs';
    s.write('$timestampMs,'
        '${p.accX.toStringAsFixed(6)},${p.accY.toStringAsFixed(6)},${p.accZ.toStringAsFixed(6)},'
        '${p.gyroX.toStringAsFixed(6)},${p.gyroY.toStringAsFixed(6)},${p.gyroZ.toStringAsFixed(6)},'
        '$labelId,$labelName,$sequence,$deviceId,'
        '$accTs,$gyroTs,$sampleKind\n');
    _rows++;
  }

  /// Append one integrity/sampling marker to the JSONL sidecar. Never touches the CSV.
  void logEvent(Map<String, dynamic> event) {
    final e = _events;
    if (e == null) return;
    e.writeln(jsonEncode(event));
  }

  Future<void> stop() async {
    _flushTimer?.cancel();
    _flushTimer = null;
    final s = _sink;
    final ev = _events;
    _sink = null;
    _events = null;
    _sessionId = null;
    try {
      await ev?.flush();
      await ev?.close();
      if (s != null) {
        await s.flush();
        await s.close();
      }
    } catch (e) {
      debugPrint('LocalSessionRecorder: close failed: $e');
    }
  }

  Future<List<File>> listSessions() async {
    final d = await _dir();
    final files = d.listSync().whereType<File>().toList()
      ..removeWhere((f) => f.path.endsWith('.events.jsonl'))
      ..sort((a, b) => b.path.compareTo(a.path));
    return files;
  }

  Future<void> _prune() async {
    final files = await listSessions();
    for (final f in files.skip(_maxSessionsKept)) {
      try {
        final sidecar = File('${f.path}.events.jsonl');
        if (await sidecar.exists()) await sidecar.delete();
        await f.delete();
      } catch (_) {}
    }
  }
}
