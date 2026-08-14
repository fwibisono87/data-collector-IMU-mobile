import 'dart:convert';
import 'dart:io';
import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';

/// Persists recording-session state so a restarted foreground-service engine
/// can recover WITHOUT waiting for the UI to re-issue commands.
///
/// File-based (not shared_preferences), so it is safe across isolates/engines
/// — the stale-cache problem that plagues SharedPreferences across multiple
/// engines does not apply. Writes are atomic (temp file + rename) so a crash
/// mid-write cannot corrupt the record.
class SessionPersistence {
  static final SessionPersistence _instance = SessionPersistence._internal();
  factory SessionPersistence() => _instance;
  SessionPersistence._internal();

  static const _fileName = 'session_state.json';
  static const _desiredFileName = 'desired_state.json';
  Future<void> _writeTail = Future<void>.value();
  int _tempCounter = 0;

  Future<File> _file(String name) async {
    final dir = await getApplicationDocumentsDirectory();
    if (!await dir.exists()) await dir.create(recursive: true);
    return File('${dir.path}/$name');
  }

  // Atomic write: write to a unique temp sibling then rename over the target. The
  // queue matters because sequence checkpoints are fire-and-forget from the 100 Hz
  // sensor path, while START/resume/STOP can also write this file. A shared fixed
  // `.tmp` name let those operations delete or rename one another's temp file.
  // Unique names also protect against a second Flutter engine writing concurrently.
  Future<void> _atomicWrite(File target, String content) {
    return _enqueue(() => _atomicWriteNow(target, content));
  }

  Future<void> _enqueue(Future<void> Function() operation) {
    final queued = _writeTail.catchError((_) {}).then<void>((_) => operation());
    // Attaching this handler does double duty: a storage failure cannot poison the queue
    // for later writes, and — because checkpoints are requested from the sensor callback
    // without being awaited — it keeps that failure from surfacing as an unhandled async
    // error in the acquisition isolate.
    _writeTail = queued.catchError((Object error) {
      debugPrint('SessionPersistence: write failed: $error');
    });
    return queued;
  }

  Future<void> _atomicWriteNow(File target, String content) async {
    final stamp = DateTime.now().microsecondsSinceEpoch;
    final tmp = File('${target.path}.$stamp.${_tempCounter++}.tmp');
    try {
      await tmp.writeAsString(content, flush: true);
      // Windows rename-over-existing needs the target removed first; Unix is fine.
      if (Platform.isWindows && await target.exists()) {
        await target.delete();
      }
      await tmp.rename(target.path);
    } finally {
      // A failed write must not leave checkpoint debris behind.
      try {
        if (await tmp.exists()) await tmp.delete();
      } catch (_) {}
    }
  }

  Future<void> save({
    required String sessionId,
    required String deviceId,
    required String serverIp,
    required int clockOffsetMs,
    required int lastSequenceNumber,
    required String deviceRole,
  }) async {
    final data = {
      'session_id': sessionId,
      'device_id': deviceId,
      'server_ip': serverIp,
      'clock_offset_ms': clockOffsetMs,
      'last_sequence_number': lastSequenceNumber,
      'device_role': deviceRole,
      'state': 'RECORDING',
      'saved_at_ms': DateTime.now().millisecondsSinceEpoch,
    };
    await _atomicWrite(await _file(_fileName), jsonEncode(data));
  }

  Future<Map<String, dynamic>?> loadInterrupted() async {
    try {
      await _writeTail.catchError((_) {});
      final f = await _file(_fileName);
      if (!await f.exists()) return null;
      final data = jsonDecode(await f.readAsString()) as Map<String, dynamic>;
      if (data['state'] == 'RECORDING') return data;
      return null;
    } catch (_) {
      return null;
    }
  }

  Future<void> clear() async {
    await _enqueue(() async {
      try {
        final f = await _file(_fileName);
        if (!await f.exists()) return;
        final data = jsonDecode(await f.readAsString()) as Map<String, dynamic>;
        data['state'] = 'IDLE';
        await _atomicWriteNow(f, jsonEncode(data));
      } catch (_) {}
    });
  }

  /// Persist the desired endpoint + role independently of any active recording,
  /// so a restarted engine knows WHERE to reconnect without the UI.
  Future<void> saveDesired(
      {required String serverIp, required String deviceRole}) async {
    await _atomicWrite(
      await _file(_desiredFileName),
      jsonEncode({
        'server_ip': serverIp,
        'device_role': deviceRole,
        'saved_at_ms': DateTime.now().millisecondsSinceEpoch,
      }),
    );
  }

  Future<Map<String, dynamic>?> loadDesired() async {
    try {
      await _writeTail.catchError((_) {});
      final f = await _file(_desiredFileName);
      if (!await f.exists()) return null;
      return jsonDecode(await f.readAsString()) as Map<String, dynamic>;
    } catch (_) {
      return null;
    }
  }

  /// In-app recovery: remove the persisted desired endpoint so a task-engine
  /// auto-reconnect can no longer re-dial a stale server IP after a reset.
  Future<void> clearDesired() async {
    await _enqueue(() async {
      try {
        final f = await _file(_desiredFileName);
        if (await f.exists()) await f.delete();
      } catch (_) {}
    });
  }
}
