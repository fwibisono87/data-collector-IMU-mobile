import 'dart:convert';
import 'dart:io';
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

  Future<File> _file(String name) async {
    final dir = await getApplicationDocumentsDirectory();
    return File('${dir.path}/$name');
  }

  // Atomic write: write to a temp sibling then rename over the target. Rename is
  // atomic on the same filesystem, so a reader never observes a torn JSON blob.
  Future<void> _atomicWrite(File target, String content) async {
    final tmp = File('${target.path}.tmp');
    await tmp.writeAsString(content, flush: true);
    // Windows rename-over-existing needs the target removed first; Unix is fine.
    if (Platform.isWindows && await target.exists()) {
      await target.delete();
    }
    await tmp.rename(target.path);
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
    try {
      final f = await _file(_fileName);
      if (!await f.exists()) return;
      final data = jsonDecode(await f.readAsString()) as Map<String, dynamic>;
      data['state'] = 'IDLE';
      await _atomicWrite(f, jsonEncode(data));
    } catch (_) {}
  }

  /// Persist the desired endpoint + role independently of any active recording,
  /// so a restarted engine knows WHERE to reconnect without the UI.
  Future<void> saveDesired({required String serverIp, required String deviceRole}) async {
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
    try {
      final f = await _file(_desiredFileName);
      if (await f.exists()) await f.delete();
    } catch (_) {}
  }
}
