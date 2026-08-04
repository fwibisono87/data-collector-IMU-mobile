import 'dart:convert';
import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:path_provider/path_provider.dart';
import 'local_session_recorder.dart';

/// Uploads the phone's local rescue CSVs (LocalSessionRecorder) to the backend over plain
/// HTTP, resumably, so a session whose WebSocket deliver path was too flaky can still be
/// pulled on the desktop dashboard — with no adb/USB.
///
/// Each session CSV is chunked and POSTed with a byte offset; the backend tracks how many
/// bytes it has so a dropped upload resumes instead of restarting. A per-session marker is
/// written locally once the upload is complete + sha256-verified, so reconnects don't
/// re-send the same session's data forever.
///
/// Schema is byte-identical to the backend CSV, and the backend dedups on
/// (device_id, sequence_number) when merging, so overlaps with live WS telemetry are clean.
class RecoveryUploader {
  static final RecoveryUploader _i = RecoveryUploader._();
  factory RecoveryUploader() => _i;
  RecoveryUploader._();

  static const int _chunkBytes = 256 * 1024;
  static const int _maxRetries = 5;

  String _baseUrl = '';

  void configure(String serverIp) {
    _baseUrl = 'http://$serverIp:8000';
  }

  bool get isConfigured => _baseUrl.isNotEmpty;

  // ── Public entry: upload every finished, not-yet-uploaded session CSV ──────

  Future<int> uploadPending({String? onlySessionId}) async {
    if (!isConfigured) return 0;
    final files = await LocalSessionRecorder().listSessions();
    int uploaded = 0;
    for (final f in files) {
      try {
        if (await _isOpenFile(f)) continue;          // still recording this session
        final meta = await _parseMeta(f);
        if (meta == null) continue;
        if (onlySessionId != null && meta['session_id'] != onlySessionId) continue;
        if (await _isMarked(meta['session_id']!)) continue;
        final ok = await _uploadOne(f, meta);
        if (ok) {
          uploaded++;
          await _markDone(meta['session_id']!, f, meta['sha256'] ?? '');
        }
      } catch (e) {
        // Keep going; a later reconnect retries.
      }
    }
    return uploaded;
  }

  Future<bool> _isOpenFile(File f) async {
    // The active session's recorder holds an open IOSink; harmless to skip it. We identify
    // it by asking the recorder whether its current path matches.
    return LocalSessionRecorder().path == f.path;
  }

  /// Parse the metadata # header line of a rescue CSV into a map.
  Future<Map<String, String>?> _parseMeta(File f) async {
    final raf = await f.open();
    try {
      final first = utf8.decode(await raf.read(512), allowMalformed: true);
      if (first.startsWith('#')) {
        final m = <String, String>{};
        final id = RegExp(r'(\w+)=([^,\s]+)').allMatches(first);
        for (final a in id) {
          m[a.group(1)!] = a.group(2)!;
        }
        if (m.containsKey('session_id')) {
          m['role'] = m['role'] ?? 'unknown';
          m['device_id'] = m['device_id'] ?? 'unknown';
          return m;
        }
      }
      return null;
    } finally {
      await raf.close();
    }
  }

  Future<bool> _uploadOne(File f, Map<String, String> meta) async {
    final sessionId = meta['session_id']!;
    final deviceId = meta['device_id']!;
    final bytes = await f.readAsBytes();
    if (bytes.isEmpty) return true; // nothing to upload — treat as done
    final sha = sha256.convert(bytes).toString();

    final client = HttpClient();
    try {
      // Resume point already stored on the backend.
      var offset = await _status(deviceId, sessionId, client);
      final total = bytes.length;

      while (offset < total) {
        // clamp() returns num; force int for sublist()/sublist boundaries.
        final end = (offset + _chunkBytes).clamp(offset, total) as int;
        final chunk = bytes.sublist(offset, end);
        final response = await _postChunk(
          client, deviceId, sessionId, meta, chunk, offset, total,
          last: end >= total, sha: sha,
        );
        final statusCode = response.statusCode;
        await response.drain<void>(); // consume/discard the response body
        if (statusCode != 200) return false;
        offset = end;
        if (end >= total) break;
      }

      return true;
    } finally {
      client.close(force: true);
    }
  }

  Future<HttpClientResponse> _postChunk(
    HttpClient client,
    String deviceId,
    String sessionId,
    Map<String, String> meta,
    List<int> chunk,
    int offset,
    int total, {
    required bool last,
    required String sha,
  }) async {
    final uri = Uri.parse('$_baseUrl/upload/csv').replace(queryParameters: {
      'device_id': deviceId,
      'session_id': sessionId,
      'role': meta['role'] ?? '',
      'subject': meta['subject'] ?? '',
      'session_tag': meta['session_tag'] ?? '',
      'operator': meta['operator'] ?? '',
    });
    final req = await client.postUrl(uri);
    req.headers.set(HttpHeaders.contentTypeHeader, 'application/octet-stream');
    req.headers.set('X-Offset', '$offset');
    req.headers.set('X-Total', '$total');
    if (last) {
      req.headers.set('X-Complete', '1');
      req.headers.set('X-Sha256', sha);
    }
    req.add(chunk);
    return await req.close();
  }

  Future<int> _status(String deviceId, String sessionId, HttpClient client) async {
    for (int i = 0; i < _maxRetries; i++) {
      try {
        final uri = Uri.parse('$_baseUrl/upload/status').replace(queryParameters: {
          'device_id': deviceId,
          'session_id': sessionId,
        });
        final req = await client.getUrl(uri);
        final res = await req.close();
        if (res.statusCode == 200) {
          final body = await res.transform(utf8.decoder).join();
          final json = jsonDecode(body) as Map<String, dynamic>;
          return (json['received_bytes'] as num?)?.toInt() ?? 0;
        }
      } catch (_) {}
      final delayMs = 400 * (i + 1);
      await Future.delayed(Duration(milliseconds: delayMs));
    }
    return 0;
  }

  // ── Local completion markers ───────────────────────────────────────────────

  Future<File> _markerFile(String sessionId) async {
    final dir = await getApplicationDocumentsDirectory();
    return File('${dir.path}/recovery_uploaded_${sessionId.replaceAll(RegExp(r'[^\w-]'), '_')}.json');
  }

  Future<bool> _isMarked(String sessionId) async {
    try {
      return await (await _markerFile(sessionId)).exists();
    } catch (_) {
      return false;
    }
  }

  Future<void> _markDone(String sessionId, File src, String sha) async {
    try {
      await (await _markerFile(sessionId)).writeAsString(jsonEncode({
        'session_id': sessionId,
        'file': src.path,
        'sha256': sha,
        'uploaded_at_ms': DateTime.now().millisecondsSinceEpoch,
      }));
    } catch (_) {}
  }
}
