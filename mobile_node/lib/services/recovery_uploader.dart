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
  // A half-open Wi-Fi association — common as a phone drifts to the edge of range, which
  // is exactly when rescue upload matters — leaves an HttpClient request waiting forever
  // by default. Without these the upload neither completed nor failed, so the completion
  // marker was never written and the dashboard waited on a phone that had stopped making
  // progress.
  static const Duration _connectTimeout = Duration(seconds: 10);
  static const Duration _requestTimeout = Duration(seconds: 45);

  String _baseUrl = '';

  void configure(String serverIp) {
    _baseUrl = 'http://$serverIp:8000';
  }

  bool get isConfigured => _baseUrl.isNotEmpty;

  // ── Public entry: upload every finished, not-yet-uploaded session CSV ──────

  Future<int> uploadPending({String? onlySessionId}) async {
    if (!isConfigured) return 0;
    int uploaded = 0;
    try {
      final files = await LocalSessionRecorder().listSessions();
      for (final f in files) {
        try {
          if (await _isOpenFile(f)) continue; // still recording this session
          final meta = await _parseMeta(f);
          if (meta == null) continue;
          if (onlySessionId != null && meta['session_id'] != onlySessionId) {
            continue;
          }
          if (await _isMarked(meta['session_id']!, meta['device_id']!, f)) {
            continue;
          }
          final ok = await _uploadOne(f, meta);
          if (ok) {
            uploaded++;
            await _markDone(meta['session_id']!, meta['device_id']!, f,
                meta['sha256'] ?? '');
          }
        } catch (_) {
          // Keep going; a later reconnect retries this file.
        }
      }
    } catch (_) {
      // Directory enumeration/storage failures are recoverable on the next
      // reconnect and must not escape an unawaited upload task.
    }
    return uploaded;
  }

  Future<bool> _isOpenFile(File f) async {
    // The active session's recorder holds an open IOSink; harmless to skip it. We identify
    // it by asking the recorder whether its current path matches.
    final recorder = LocalSessionRecorder();
    return recorder.isOpen && recorder.path == f.path;
  }

  /// Parse the metadata # header line of a rescue CSV into a map.
  Future<Map<String, String>?> _parseMeta(File f) async {
    final raf = await f.open();
    try {
      // Read up to the first newline rather than a fixed 512 bytes. A long
      // subject/tag/operator could push device_id past that window, and a truncated value
      // was silently accepted — uploading the session under a mangled device id.
      final buffer = <int>[];
      while (buffer.length < 8192) {
        final block = await raf.read(512);
        if (block.isEmpty) break;
        final newline = block.indexOf(0x0A);
        if (newline >= 0) {
          buffer.addAll(block.sublist(0, newline));
          break;
        }
        buffer.addAll(block);
      }
      final first = utf8.decode(buffer, allowMalformed: true);
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
    final total = await f.length();
    if (total == 0) return true; // nothing to upload — treat as done
    // Recordings can be hundreds of MB. Hash and upload bounded chunks from disk instead
    // of materialising the whole CSV in the Dart heap (which could kill the app mid-recovery).
    final sha = await _sha256File(f);

    final client = HttpClient()..connectionTimeout = _connectTimeout;
    RandomAccessFile? reader;
    try {
      // Resume point already stored on the backend.
      final status = await _status(deviceId, sessionId, client);
      if (status == null) return false;
      var offset = (status['received_bytes'] as num?)?.toInt() ?? 0;
      if (offset < 0 || offset > total) return false;
      if (offset == total) {
        if (status['complete'] == true && status['sha256_verified'] == true) {
          return true;
        }
        // The server may have durably appended the final bytes before a Wi-Fi drop took
        // the response. Send an idempotent zero-byte final request so it can run the digest
        // check and mark the upload verified instead of leaving a permanently "receiving"
        // file that every reconnect silently skips.
        final response = await _postChunk(
          client,
          deviceId,
          sessionId,
          meta,
          const <int>[],
          offset,
          total,
          last: true,
          sha: sha,
        );
        final body =
            await response.transform(utf8.decoder).join().timeout(_requestTimeout);
        try {
          final finalized = jsonDecode(body) as Map<String, dynamic>;
          return response.statusCode == 200 &&
              finalized['complete'] == true &&
              finalized['sha256_verified'] == true;
        } catch (_) {
          return false;
        }
      }
      reader = await f.open();

      while (offset < total) {
        final end = (offset + _chunkBytes).clamp(offset, total);
        await reader.setPosition(offset);
        final chunk = await reader.read(end - offset);
        if (chunk.length != end - offset) return false;
        final response = await _postChunk(
          client,
          deviceId,
          sessionId,
          meta,
          chunk,
          offset,
          total,
          last: end >= total,
          sha: sha,
        );
        final statusCode = response.statusCode;
        final responseText =
            await response.transform(utf8.decoder).join().timeout(_requestTimeout);
        Map<String, dynamic>? server;
        try {
          server = jsonDecode(responseText) as Map<String, dynamic>;
        } catch (_) {}
        if (statusCode == 409) {
          final expected = (server?['expected_offset'] as num?)?.toInt();
          // The backend is authoritative after an interrupted request. Resume exactly where
          // it says, but reject a nonsensical/non-progressing response to avoid an infinite loop.
          if (expected == null ||
              expected < 0 ||
              expected > total ||
              expected == offset) {
            return false;
          }
          offset = expected;
          continue;
        }
        if (statusCode != 200) return false;
        final received = (server?['received_bytes'] as num?)?.toInt();
        if (received != end) return false;
        offset = received!;
        if (end >= total) {
          // Do not create the local completion marker until the backend has both the
          // complete byte count and the digest match.
          return server?['complete'] == true &&
              server?['sha256_verified'] == true;
        }
      }

      return false;
    } finally {
      await reader?.close();
      client.close(force: true);
    }
  }

  Future<String> _sha256File(File file) async {
    // File.openRead feeds the digest incrementally; it never creates a byte array for the
    // complete recording.
    final digest = await sha256.bind(file.openRead()).first;
    return digest.toString();
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
    return await req.close().timeout(_requestTimeout);
  }

  Future<Map<String, dynamic>?> _status(
      String deviceId, String sessionId, HttpClient client) async {
    for (int i = 0; i < _maxRetries; i++) {
      try {
        final uri =
            Uri.parse('$_baseUrl/upload/status').replace(queryParameters: {
          'device_id': deviceId,
          'session_id': sessionId,
        });
        final req = await client.getUrl(uri);
        final res = await req.close().timeout(_requestTimeout);
        if (res.statusCode == 200) {
          final body =
              await res.transform(utf8.decoder).join().timeout(_requestTimeout);
          return jsonDecode(body) as Map<String, dynamic>;
        }
      } catch (_) {}
      final delayMs = 400 * (i + 1);
      await Future.delayed(Duration(milliseconds: delayMs));
    }
    return null;
  }

  // ── Local completion markers ───────────────────────────────────────────────

  Future<File> _markerFile(String sessionId, String deviceId) async {
    final dir = await getApplicationDocumentsDirectory();
    final sid = sessionId.replaceAll(RegExp(r'[^\w-]'), '_');
    final dev = deviceId.replaceAll(RegExp(r'[^\w-]'), '_');
    return File('${dir.path}/recovery_uploaded_${sid}_$dev.json');
  }

  Future<bool> _isMarked(String sessionId, String deviceId, File source) async {
    try {
      final marker = await _markerFile(sessionId, deviceId);
      if (!await marker.exists()) return false;
      final data =
          jsonDecode(await marker.readAsString()) as Map<String, dynamic>;
      return data['file'] == source.path &&
          data['bytes'] == await source.length();
    } catch (_) {
      return false;
    }
  }

  Future<void> _markDone(
      String sessionId, String deviceId, File src, String sha) async {
    try {
      await (await _markerFile(sessionId, deviceId)).writeAsString(jsonEncode({
        'session_id': sessionId,
        'device_id': deviceId,
        'file': src.path,
        'bytes': await src.length(),
        'sha256': sha,
        'uploaded_at_ms': DateTime.now().millisecondsSinceEpoch,
      }));
    } catch (_) {}
  }
}
