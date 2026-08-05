import 'dart:convert';
import 'dart:io';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:uuid/uuid.dart';

/// Stable per-device identity + role + last server IP.
///
/// Stored in a small JSON file in the app documents dir, NOT SharedPreferences.
/// flutter_foreground_task runs the pipeline in a separate FlutterEngine, and each
/// engine keeps its own SharedPreferences cache — so a role written by the UI engine
/// was silently read as a stale value by the task engine, and the phone registered
/// under the wrong role (e.g. still "chest" after the operator picked "waist", causing
/// role-collision rejects). File reads are always fresh across engines.
class DeviceIdService {
  static final DeviceIdService _instance = DeviceIdService._internal();
  factory DeviceIdService() => _instance;
  DeviceIdService._internal();

  static const _fileName = 'device_config.json';

  Future<File> _file() async {
    final dir = await getApplicationDocumentsDirectory();
    return File('${dir.path}/$_fileName');
  }

  Future<Map<String, dynamic>> _read() async {
    try {
      final f = await _file();
      if (await f.exists()) {
        final decoded = jsonDecode(await f.readAsString());
        if (decoded is Map<String, dynamic>) return decoded;
      }
    } catch (_) {}
    return {};
  }

  Future<void> _write(Map<String, dynamic> data) async {
    final f = await _file();
    final tmp = File('${f.path}.tmp');
    await tmp.writeAsString(jsonEncode(data), flush: true);
    if (Platform.isWindows && await f.exists()) {
      await f.delete();
    }
    await tmp.rename(f.path);
  }

  // One-time migration from the legacy SharedPreferences store (old build) so
  // existing device_id / ip survive the switch to file-based storage. The role is
  // deliberately NOT migrated: roles must be explicit, and migrating a stale role
  // silently reinstated "chest" on phones whose real role was different, causing
  // role-collision rejects with no in-app way to recover. device_id + ip are stable
  // non-colliding values, so only those carry over.
  Future<void> _migrateFromPrefs(Map<String, dynamic> data, List<String> keys) async {
    if (keys.every((k) => data[k] != null)) return;
    final prefs = await SharedPreferences.getInstance();
    var changed = false;
    for (final k in keys) {
      if (data[k] == null) {
        final v = prefs.getString(k);
        if (v != null && v.isNotEmpty) {
          data[k] = v;
          changed = true;
        }
      }
    }
    if (changed) await _write(data);
  }

  Future<String> getDeviceId() async {
    final data = await _read();
    await _migrateFromPrefs(data, ['device_id']);
    var id = data['device_id'] as String?;
    if (id == null) {
      id = const Uuid().v4();
      data['device_id'] = id;
      await _write(data);
    }
    return id;
  }

  Future<String> getDeviceRole() async {
    final data = await _read();
    await _migrateFromPrefs(data, ['device_role']);
    return (data['device_role'] as String?) ?? 'chest';
  }

  Future<void> setDeviceRole(String role) async {
    final data = await _read();
    data['device_role'] = role;
    await _write(data);
  }

  Future<String> getLastServerIp() async {
    final data = await _read();
    await _migrateFromPrefs(data, ['last_server_ip']);
    return (data['last_server_ip'] as String?) ?? '';
  }

  Future<void> saveServerIp(String ip) async {
    final data = await _read();
    data['last_server_ip'] = ip;
    await _write(data);
  }

  /// In-app recovery: wipe the device identity + role + IP file entirely. The
  /// next access regenerates a fresh device_id and resets to the default role,
  /// so a phone stuck on a bad/colliding role can recover without the operator
  /// clearing app data via the OS.
  Future<void> resetConfig() async {
    try {
      final f = await _file();
      if (await f.exists()) await f.delete();
    } catch (_) {}
  }
}
