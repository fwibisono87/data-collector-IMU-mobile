import 'dart:io';
import 'package:path_provider/path_provider.dart';

/// Appends a time-stamped line to `conn_debug.log` in the app documents dir.
///
/// This is a lightweight, always-on trace of the connect path so that when the
/// phone reports a failed connect the real stage + error text is recoverable
/// instead of being hidden behind the UI's generic fallback. Writing is
/// fire-and-forget: a logging failure must never break the connect flow.
class ConnDebug {
  static Future<File> _logFile() async {
    final dir = await getApplicationDocumentsDirectory();
    return File('${dir.path}/conn_debug.log');
  }

  static void log(String line) {
    try {
      final ts = DateTime.now().toIso8601String();
      _logFile().then((f) => f.writeAsString('$ts  $line\n', mode: FileMode.append));
    } catch (_) {}
  }
}
