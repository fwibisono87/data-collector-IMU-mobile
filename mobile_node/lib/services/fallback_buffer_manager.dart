import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';
import 'package:path_provider/path_provider.dart';

// Length-delimited protobuf binary buffer for network-offline periods (CLAUDE.md §9.1).
// Format: [4-byte BE length][proto bytes] repeated.
class FallbackBufferManager {
  static final FallbackBufferManager _instance =
      FallbackBufferManager._internal();
  factory FallbackBufferManager() => _instance;
  FallbackBufferManager._internal();

  static const _maxFileSizeBytes = 500 * 1024 * 1024; // 500 MB
  static const _maxRotations = 3;
  static const _fsyncIntervalMs = 1000;

  RandomAccessFile? _raf;
  int _currentFileIndex = 1;
  int _bufferedCount = 0;
  Timer? _fsyncTimer;
  bool _isActive = false;

  // Packets are produced at 100 Hz from a synchronous sensor callback, but
  // RandomAccessFile rejects overlapping async operations. Every packet therefore goes
  // into an in-memory queue and a single-flight chain drains it in order. Firing
  // un-awaited writeFrom() calls (the previous behaviour) both dropped packets and could
  // emit a length prefix without its payload, desynchronising the reader for the rest of
  // the file (plan D4).
  final List<Uint8List> _pending = [];
  Future<void> _chain = Future.value();
  static const int _maxPending = 200000;   // ~12 MB; only reachable if storage stalls
  int _droppedOverflow = 0;

  String? _sessionId;                       // the session these bytes belong to
  String? get sessionId => _sessionId;
  int get droppedOverflow => _droppedOverflow;

  int get bufferedCount => _bufferedCount;
  bool get isActive => _isActive;

  Future<File> _fileForIndex(int index) async {
    final dir = await getApplicationDocumentsDirectory();
    final suffix = index == 1 ? '' : '.$index';
    return File('${dir.path}/fallback_buffer$suffix.bin');
  }

  Future<File> _metaFile() async {
    final dir = await getApplicationDocumentsDirectory();
    return File('${dir.path}/fallback_buffer.meta.json');
  }

  /// Restore _sessionId from disk metadata — used so a buffer that survived process
  /// death is still attributable to its session before we decide whether to flush,
  /// quarantine, or keep it (plan T17 / R3).
  Future<void> loadMeta() async {
    if (_sessionId != null) return; // already known (active, or already loaded this run)
    try {
      final f = await _metaFile();
      if (!await f.exists()) return;
      final data = jsonDecode(await f.readAsString()) as Map<String, dynamic>;
      _sessionId = data['session_id']?.toString();
    } catch (_) {}
  }

  Future<void> _writeMeta() async {
    try {
      final f = await _metaFile();
      await f.writeAsString(jsonEncode({
        'session_id': _sessionId,
        'started_at_ms': DateTime.now().millisecondsSinceEpoch,
      }));
    } catch (_) {}
  }

  /// Called before any write. If bytes from a DIFFERENT session are still on disk, move
  /// them aside first — appending to them would mix two sessions into one stream and the
  /// flush would write session A's rows into session B's CSV (plan D13).
  Future<void> activate({String? sessionId}) async {
    if (_isActive) return;
    await loadMeta();
    if (_sessionId != null && _sessionId != sessionId) {
      await quarantine();
    }
    _isActive = true;
    _bufferedCount = 0;
    _droppedOverflow = 0;
    _sessionId = sessionId;
    await _openCurrentFile();
    await _writeMeta();
    _fsyncTimer = Timer.periodic(
      const Duration(milliseconds: _fsyncIntervalMs),
      (_) => _chain = _chain.then((_) async { await _raf?.flush(); }).catchError((_) {}),
    );
    _kick();
  }

  Future<void> _openCurrentFile() async {
    final f = await _fileForIndex(_currentFileIndex);
    _raf = await f.open(mode: FileMode.append);
    // Rotate if over size limit
    final stat = await f.stat();
    if (stat.size >= _maxFileSizeBytes) {
      await _rotate();
    }
  }

  Future<void> _rotate() async {
    await _raf?.close();
    _currentFileIndex++;
    if (_currentFileIndex > _maxRotations) {
      // Overwrite oldest (index 1)
      _currentFileIndex = 1;
      final f = await _fileForIndex(1);
      await f.writeAsBytes([], mode: FileMode.write); // truncate
    }
    _raf = await (await _fileForIndex(_currentFileIndex))
        .open(mode: FileMode.append);
  }

  /// Synchronous, non-blocking, never drops while under the cap. Safe to call from
  /// the sensor hot path.
  void enqueue(Uint8List packetBytes) {
    if (!_isActive) return;
    if (_pending.length >= _maxPending) {
      _droppedOverflow++;
      return;
    }
    _pending.add(packetBytes);
    _bufferedCount++;
    _kick();
  }

  void _kick() {
    _chain = _chain.then((_) => _drain()).catchError((_) {
      // Best-effort: the next enqueue()'s _kick() retries the drain.
    });
  }

  Future<void> _drain() async {
    if (_raf == null || _pending.isEmpty) return;
    while (_pending.isNotEmpty) {
      final b = _pending.removeAt(0);
      final len = ByteData(4)..setUint32(0, b.length, Endian.big);
      await _raf!.writeFrom(len.buffer.asUint8List());
      await _raf!.writeFrom(b);
    }
    final pos = await _raf!.position();
    if (pos >= _maxFileSizeBytes) await _rotate();
  }

  // Yields each buffered packet in order from all rotation files.
  Stream<Uint8List> flushStream() async* {
    await _chain;              // ensure nothing is still queued in memory
    await _raf?.flush();
    await _raf?.close();
    _raf = null;

    for (int idx = 1; idx <= _maxRotations; idx++) {
      final f = await _fileForIndex(idx);
      if (!await f.exists()) continue;
      final bytes = await f.readAsBytes();
      int pos = 0;
      while (pos + 4 <= bytes.length) {
        final len = ByteData.sublistView(bytes, pos, pos + 4).getUint32(0, Endian.big);
        pos += 4;
        if (pos + len > bytes.length) break;
        yield Uint8List.sublistView(bytes, pos, pos + len);
        pos += len;
      }
    }
  }

  Future<void> clearAfterFlush() async {
    for (int idx = 1; idx <= _maxRotations; idx++) {
      final f = await _fileForIndex(idx);
      if (await f.exists()) await f.writeAsBytes([], mode: FileMode.write);
    }
    try {
      final m = await _metaFile();
      if (await m.exists()) await m.delete();
    } catch (_) {}
    _bufferedCount = 0;
    _currentFileIndex = 1;
    _isActive = false;
    _sessionId = null;
    _fsyncTimer?.cancel();
  }

  Future<void> deactivate() async {
    _fsyncTimer?.cancel();
    await _chain;      // drain anything still queued before closing the file (plan R8)
    await _raf?.flush();
    await _raf?.close();
    _raf = null;
    _isActive = false;
  }

  /// Preserve bytes that could not be proven delivered. Never truncate them — the
  /// previous code erased the phone's only copy after streaming into a backend that had
  /// already stopped and discarded every packet (plan D3).
  Future<List<String>> quarantine() async {
    await _chain;
    await _raf?.flush();
    await _raf?.close();
    _raf = null;
    final dir = await getApplicationDocumentsDirectory();
    final stamp = DateTime.now().millisecondsSinceEpoch;
    final sid = _sessionId ?? 'unknown';
    final moved = <String>[];
    for (int idx = 1; idx <= _maxRotations; idx++) {
      final f = await _fileForIndex(idx);
      if (!await f.exists()) continue;
      if ((await f.length()) == 0) continue;
      final target = File('${dir.path}/orphan_${sid}_${stamp}_$idx.bin');
      await f.rename(target.path);
      moved.add(target.path);
    }
    try {
      final m = await _metaFile();
      if (await m.exists()) await m.delete();
    } catch (_) {}
    _bufferedCount = 0;
    _currentFileIndex = 1;
    _isActive = false;
    _sessionId = null;
    _fsyncTimer?.cancel();
    return moved;
  }

  /// Total bytes left on disk from a previous process — including a buffer this run
  /// never called activate() on. Lets the flush-decision logic react to a surviving
  /// buffer without depending on in-memory `isActive` (plan T17).
  Future<int> pendingOnDisk() async {
    await loadMeta();
    int total = 0;
    for (int idx = 1; idx <= _maxRotations; idx++) {
      final f = await _fileForIndex(idx);
      if (await f.exists()) total += await f.length();
    }
    return total;
  }

  Future<List<FileSystemEntity>> listOrphans() async {
    final dir = await getApplicationDocumentsDirectory();
    return dir.listSync().where((e) => e.path.contains('/orphan_')).toList();
  }
}
