import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:crypto/crypto.dart';
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

  static const _header = 'timestamp_ms,acc_x_g,acc_y_g,acc_z_g,'
      'gyro_x_degs,gyro_y_degs,gyro_z_degs,'
      'label_id,label_name,sequence_number,device_id,'
      'acc_ts_ms,gyro_ts_ms,sample_kind\n';
  static const int _maxSessionsKept = 20;

  IOSink? _sink;
  IOSink? _events;
  File? _file;
  Timer? _flushTimer;
  // IOSink is a StreamSink: write(), flush(), and close() must not overlap an
  // addStream operation. All recorder I/O goes through this chain so a slow
  // phone filesystem cannot turn a periodic flush into an unhandled exception.
  Future<void> _ioChain = Future<void>.value();
  bool _acceptWrites = false;
  bool _flushQueued = false;
  String? _sessionId;
  int _rows = 0;
  String? _lastError;

  String? get path => _file?.path;
  int get rows => _rows;
  bool get isOpen => _sink != null && _acceptWrites;
  String? get lastError => _lastError;

  Future<Directory> _dir() async {
    final base = await getExternalStorageDirectory() ??
        await getApplicationDocumentsDirectory();
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
    if (_sessionId == sessionId && _sink != null) {
      return; // idempotent on resync
    }
    await stop();
    try {
      final d = await _dir();
      // One deterministic path per (session, phone). If the foreground engine is killed
      // and restarted during RECORDING, the new engine must append to the same rescue CSV;
      // a timestamped second file would make the uploader either skip a fragment or send a
      // second header that the resumable backend cannot append after a verified upload.
      final who = <String>[
        sessionId,
        if (subject.isNotEmpty) subject,
        if (sessionTag.isNotEmpty) sessionTag,
        role,
        deviceId,
      ].map(_sanitize).join('_');
      final csvPath = '${d.path}/${who}_rescue.csv';
      _file = File(csvPath);
      final exists = await _file!.exists() && await _file!.length() > 0;
      if (exists) await _trimIncompleteLine(_file!);
      _sink = _file!.openWrite(mode: exists ? FileMode.append : FileMode.write);
      if (!exists) {
        final startMs = DateTime.now().millisecondsSinceEpoch;
        _sink!.write(
            '# session_id=${_sanitize(sessionId)},role=${_sanitize(role)},'
            'device_id=${_sanitize(deviceId)},subject=${_sanitize(subject)},'
            'session_tag=${_sanitize(sessionTag)},operator=${_sanitize(operator)},'
            'start_epoch_ms=$startMs,source=local_node,schema_version=2\n');
        _sink!.write(_header);
      }
      // Sidecar for integrity markers / sampling stats (JSONL).
      final eventsFile = File('$csvPath.events.jsonl');
      final eventsExists =
          await eventsFile.exists() && await eventsFile.length() > 0;
      _events = eventsFile.openWrite(
          mode: eventsExists ? FileMode.append : FileMode.write);
      _sessionId = sessionId;
      _rows = 0;
      _lastError = null;
      _ioChain = Future<void>.value();
      _flushQueued = false;
      _acceptWrites = true;
      final sink = _sink!;
      unawaited(sink.done.catchError((Object error, StackTrace stack) {
        _lastError = '$error';
        debugPrint('LocalSessionRecorder: CSV sink failed: $error');
      }));
      _flushTimer = Timer.periodic(const Duration(seconds: 1), (_) {
        _scheduleFlush();
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
    RandomAccessFile? raf;
    try {
      // Search backwards in bounded windows. Reading a full 40-minute rescue CSV here
      // after a process kill used to duplicate the entire file in the Dart heap.
      raf = await f.open();
      final length = await raf.length();
      var cursor = length;
      var newline = -1;
      while (cursor > 0 && newline < 0) {
        final start = (cursor - 64 * 1024).clamp(0, cursor).toInt();
        await raf.setPosition(start);
        final bytes = await raf.read(cursor - start);
        final local = bytes.lastIndexOf(0x0A);
        if (local >= 0) newline = start + local;
        cursor = start;
      }
      await raf.close();
      raf = null;
      if (newline >= 0 && newline + 1 < length) {
        final writer = await f.open(mode: FileMode.write);
        await writer.truncate(newline + 1);
        await writer.close();
      }
    } catch (_) {
      // Best effort only.
      await raf?.close();
    }
  }

  /// Queue one packet write. The method remains synchronous for the 100 Hz hot
  /// path, while the actual IOSink operation is serialized with flushes.
  void write(
    SensorPacket p, {
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
    if (s == null || !_acceptWrites) return;
    final accTs = accTsMs == 0 ? '' : '$accTsMs';
    final gyroTs = gyroTsMs == 0 ? '' : '$gyroTsMs';
    final line = '$timestampMs,'
        '${p.accX.toStringAsFixed(6)},${p.accY.toStringAsFixed(6)},${p.accZ.toStringAsFixed(6)},'
        '${p.gyroX.toStringAsFixed(6)},${p.gyroY.toStringAsFixed(6)},${p.gyroZ.toStringAsFixed(6)},'
        '$labelId,$labelName,$sequence,$deviceId,'
        '$accTs,$gyroTs,$sampleKind\n';
    _ioChain = _ioChain.then<void>((_) async {
      try {
        s.write(line);
        _rows++;
      } catch (e) {
        // Never let local persistence take down the sensor stream. The backend/fallback
        // path still receives this packet, and the error is preserved in the sidecar.
        _lastError = '$e';
        debugPrint('LocalSessionRecorder: CSV write failed: $e');
      }
    });
  }

  /// Append one integrity/sampling marker to the JSONL sidecar. Never touches the CSV.
  void logEvent(Map<String, dynamic> event) {
    final e = _events;
    if (e == null || !_acceptWrites) return;
    final line = jsonEncode(event);
    _ioChain = _ioChain.then<void>((_) async {
      try {
        e.writeln(line);
      } catch (error) {
        debugPrint('LocalSessionRecorder: event write failed: $error');
      }
    });
  }

  /// Queue at most one flush at a time. It follows all writes already queued,
  /// and writes arriving during the flush follow it, so IOSink never overlaps
  /// a write with its internal addStream operation.
  void _scheduleFlush() {
    final sink = _sink;
    if (sink == null || !_acceptWrites || _flushQueued) return;
    final events = _events;
    _flushQueued = true;
    _ioChain = _ioChain.then<void>((_) async {
      try {
        await _flushSinks(sink, events);
      } catch (error) {
        _lastError = '$error';
        debugPrint('LocalSessionRecorder: flush coordinator failed: $error');
      } finally {
        _flushQueued = false;
      }
    });
  }

  Future<void> _flushSinks(IOSink sink, IOSink? events) async {
    try {
      await sink.flush();
    } catch (error) {
      _lastError = '$error';
      debugPrint('LocalSessionRecorder: CSV flush failed: $error');
    }
    if (events != null) {
      try {
        await events.flush();
      } catch (error) {
        debugPrint('LocalSessionRecorder: event flush failed: $error');
      }
    }
  }

  Future<void> stop() async {
    _flushTimer?.cancel();
    _flushTimer = null;
    final s = _sink;
    final ev = _events;
    final closedSessionId = _sessionId;
    // Stop accepting new writes immediately, but keep the sinks assigned until
    // the queued writes and flush have drained. Clearing them first races with
    // the I/O chain and can leave the rescue CSV short by its final packets.
    _acceptWrites = false;
    try {
      await _ioChain;
    } catch (e) {
      _lastError = '$e';
    }

    // Keep a local reference for close, but make the public path cease to identify this file
    // as active before recovery upload is triggered by STOP_SESSION.
    final closedFile = _file;
    try {
      await ev?.flush();
      await ev?.close();
    } catch (e) {
      debugPrint('LocalSessionRecorder: event close failed: $e');
    }
    try {
      await s?.flush();
      await s?.close();
    } catch (e) {
      _lastError = '$e';
      debugPrint('LocalSessionRecorder: CSV close failed: $e');
    }
    _file = null;
    _sink = null;
    _events = null;
    _sessionId = null;
    _ioChain = Future<void>.value();
    _flushQueued = false;
    if (closedFile != null) {
      try {
        if (await closedFile.exists()) {
          // The backend independently verifies this digest on upload. Keeping the local
          // sidecar makes a phone-only rescue auditable even before Wi-Fi returns.
          final digest = await sha256.bind(closedFile.openRead()).first;
          final integrity = File('${closedFile.path}.integrity.json');
          await integrity.writeAsString(jsonEncode({
            'session_id': closedSessionId,
            'rows': _rows,
            'bytes': await closedFile.length(),
            'sha256': digest.toString(),
            'verified_at_ms': DateTime.now().millisecondsSinceEpoch,
            if (_lastError != null) 'close_error': _lastError,
          }));
        }
      } catch (e) {
        debugPrint('LocalSessionRecorder: integrity sidecar failed: $e');
      }
    }
  }

  Future<List<File>> listSessions() async {
    final d = await _dir();
    final files = d.listSync().whereType<File>().toList()
      ..removeWhere((f) => f.path.endsWith('.events.jsonl'))
      ..removeWhere((f) => f.path.endsWith('.integrity.json'))
      ..sort((a, b) => b.path.compareTo(a.path));
    return files;
  }

  Future<void> _prune() async {
    final files = await listSessions();
    for (final f in files.skip(_maxSessionsKept)) {
      try {
        final sidecar = File('${f.path}.events.jsonl');
        if (await sidecar.exists()) await sidecar.delete();
        final integrity = File('${f.path}.integrity.json');
        if (await integrity.exists()) await integrity.delete();
        await f.delete();
      } catch (_) {}
    }
  }
}
